from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from threading import RLock

from remoteops.commands.templates import CommandRegistry, CommandValidationError, prepare_command
from remoteops.core.credentials import CredentialStore, ResolvedCredential
from remoteops.core.database import Repository
from remoteops.core.models import AuthMethod, CommandResult, ConnectionMode, CredentialProfile, Host, SessionState, utc_now
from remoteops.remoting.session import FakeSession, PersistentSession, PsrpSession, RemoteSessionError

SessionFactory = Callable[[Host, CredentialProfile | None], PersistentSession]
CredentialProvider = Callable[[CredentialProfile], ResolvedCredential | None]


class SessionManager:
    """Each host owns one worker thread; all session operations run there."""

    def __init__(self, repository=None, credential_store=None, session_factory=None,
                 credential_provider=None, max_workers=12):
        self.repository: Repository | None = repository
        self.credential_store = credential_store or CredentialStore()
        self.session_factory = session_factory
        self.credential_provider = credential_provider
        self.max_workers = max_workers
        self._sessions = {}
        self._workers = {}
        self._pending = {}
        self._activity = {}
        self._lock = RLock()
        self._closed = False
        self.registry = CommandRegistry(repository.db_path.parent / "commands.json") if repository else None

    def _submit(self, host_id, fn, *, activity=True):
        with self._lock:
            if self._closed:
                raise RemoteSessionError("Session manager is closed")
            if host_id not in self._workers:
                self._workers[host_id] = ThreadPoolExecutor(max_workers=1, thread_name_prefix="remoteops-host")
            worker = self._workers[host_id]
            self._pending[host_id] = self._pending.get(host_id, 0) + 1

        def perform():
            try:
                return fn()
            finally:
                with self._lock:
                    self._pending[host_id] -= 1
                    if activity:
                        self._activity[host_id] = time.monotonic()
        return worker.submit(perform)

    @staticmethod
    def _key(host, profile):
        return host.id, profile.id if profile else None, host.connection_mode.value

    def _connect(self, host, profile, credential):
        key = self._key(host, profile)
        with self._lock:
            old_sessions = [self._sessions.pop(old_key) for old_key in list(self._sessions)
                            if old_key[0] == host.id and old_key != key]
        for old_session in old_sessions:
            old_session.disconnect()
        session = self._sessions.get(key)
        if session and session.state is SessionState.CONNECTED:
            return session
        if session:
            session.disconnect()
        factory = self.session_factory
        if factory is None:
            if host.connection_mode is ConnectionMode.WMI:
                from remoteops.remoting.wmi import wmi_session_factory
                factory = wmi_session_factory
            else:
                factory = PsrpSession
        session = factory(host, profile)
        with self._lock:
            self._sessions[key] = session
        try:
            session.connect(credential)
        except Exception as exc:
            session.state = SessionState.ERROR
            session.last_error = str(exc)
            raise
        return session

    def connect(self, host, profile=None):
        profile = profile or self._profile_for_host(host)
        return self.connect_with_credential(host, profile, self._resolve_credential(profile))

    def connect_with_credential(self, host, profile=None, credential=None):
        profile = profile or self._profile_for_host(host)
        return self._submit(host.id, lambda: self._connect(host, profile, credential)).result()

    def prepare(self, command_line, host=None):
        command = self.registry.prepare(command_line) if self.registry else prepare_command(command_line)
        if host and host.connection_mode is ConnectionMode.WMI and not command.help_text and not command.native_wmi:
            raise CommandValidationError(f"'{command.alias}' is not available over WMI/DCOM only. Use a native WMI command or a WinRM host.")
        if host and host.connection_mode is ConnectionMode.WMI and not command.help_text:
            from remoteops.remoting.wmi import supported_wmi_aliases
            if command.wmi_plan is None and command.alias not in supported_wmi_aliases():
                raise CommandValidationError(
                    f"'{command.alias}' has no implemented WMI command handler."
                )
        if host and host.connection_mode is ConnectionMode.WINRM and not command.help_text and not command.script.strip():
            raise CommandValidationError(f"'{command.alias}' has no WinRM implementation. Select a WMI host.")
        return command

    def run_command(self, host, command, profile=None, timeout_seconds=120):
        self.prepare(command, host)
        profile = profile or self._profile_for_host(host)
        return self.run_command_with_credential(host, command, profile, self._resolve_credential(profile), timeout_seconds)

    def run_command_with_credential(self, host, command, profile=None, credential=None, timeout_seconds=120):
        prepared = self.prepare(command, host)
        if prepared.help_text:
            return CommandResult(host.id, host.name, "", stdout=prepared.help_text, exit_code=0, command_line=command)
        profile = profile or self._profile_for_host(host)
        return self._run_prepared(host, prepared, profile, credential, timeout_seconds)

    def run_prepared_read_only(self, host, prepared, profile=None, credential=None,
                               timeout_seconds=120, *, allow_connect=False):
        if prepared.help_text or host.connection_mode.value not in prepared.read_only_modes:
            raise CommandValidationError("Multi-host execution only supports verified read-only commands for this backend")
        return self._run_prepared(host, prepared, profile, credential, timeout_seconds,
                                  allow_connect=allow_connect)

    def _run_prepared(self, host, prepared, profile, credential, timeout_seconds, *, allow_connect=True):
        command = prepared.command_line

        def run():
            result = CommandResult(host.id, host.name, prepared.script, command_line=command,
                                   connection_mode=host.connection_mode.value)
            try:
                key = self._key(host, profile)
                previous = self._sessions.get(key)
                if previous and previous.state is SessionState.ERROR:
                    raise RemoteSessionError("Session was lost. Reconnect explicitly before running another command.")
                if not allow_connect and (previous is None or previous.state is not SessionState.CONNECTED):
                    raise RemoteSessionError("The existing session is no longer connected. No new connection was authorized.")
                session = self._connect(host, profile, credential)
                result = session.run_prepared(prepared, timeout_seconds)
            except Exception as exc:
                result.stderr = str(exc)
                result.exit_code = 1
            result.command_line = command
            result.connection_mode = host.connection_mode.value
            result.completed_at = result.completed_at or utc_now()
            if self.repository:
                self.repository.save_command_result(result)
            return result
        return self._submit(host.id, run).result()

    def session_state(self, host, profile=None):
        with self._lock:
            session = self._sessions.get(self._key(host, profile))
            return session.state if session else SessionState.DISCONNECTED

    def host_busy(self, host_id):
        with self._lock:
            return bool(self._pending.get(host_id, 0))

    def run_command_many(self, hosts: Iterable[Host], command: str, timeout_seconds=120):
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = [pool.submit(self.run_command, host, command, None, timeout_seconds) for host in hosts]
            return [future.result() for future in futures]

    def disconnect(self, host, profile=None):
        def close():
            with self._lock:
                keys = [key for key in self._sessions if key[0] == host.id]
            for key in keys:
                session = self._sessions[key]
                try:
                    session.disconnect()
                finally:
                    with self._lock:
                        self._sessions.pop(key, None)
        self._submit(host.id, close).result()

    def disconnect_all(self):
        with self._lock:
            hosts = {session.host.id: session.host for session in self._sessions.values()}
        failures = []
        for host in hosts.values():
            try:
                self.disconnect(host)
            except Exception as exc:
                failures.append(f"{host.name}: {exc}")
        if failures:
            raise RemoteSessionError("Disconnect cleanup failed: " + "; ".join(failures))

    def shutdown(self):
        try:
            self.disconnect_all()
        finally:
            with self._lock:
                self._closed = True
                workers = list(self._workers.values())
            for worker in workers:
                worker.shutdown(wait=True)

    def schedule_keepalives(self, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            host = session.host
            if session.state is not SessionState.CONNECTED:
                continue
            with self._lock:
                if self._pending.get(host.id, 0):
                    continue
                health_due = (host.keepalive_seconds > 0
                              and now - self._activity.get(host.id, now) >= host.keepalive_seconds)
                if not health_due and not session.maintenance_due(now):
                    continue

            def check(session=session, health_due=health_due):
                if session.state is not SessionState.CONNECTED:
                    return
                try:
                    if health_due:
                        session.health_check()
                    else:
                        session.maintain()
                except Exception as exc:
                    session.state = SessionState.ERROR
                    label = "Idle health check" if health_due else "Protocol lifetime maintenance"
                    session.last_error = f"{label} failed: {exc}. Reconnect explicitly."
            self._submit(host.id, check, activity=health_due)

    def states(self):
        with self._lock:
            return {(key[0], key[1]): session.state for key, session in self._sessions.items()}

    def errors(self):
        with self._lock:
            return {key[0]: session.last_error for key, session in self._sessions.items()
                    if session.state is SessionState.ERROR and session.last_error}

    def transfer(self, host, direction, local_path, remote_path, overwrite, cancel, progress):
        def perform():
            profile = self._profile_for_host(host)
            session = self._sessions.get(self._key(host, profile))
            if not isinstance(session, PsrpSession) or session.state is not SessionState.CONNECTED:
                raise RemoteSessionError("File transfer requires an established WinRM session.")
            from remoteops.remoting.transfer import transfer_file
            session.state = SessionState.RUNNING
            try:
                return transfer_file(session, direction, local_path, remote_path, overwrite, cancel, progress)
            finally:
                if session.state is SessionState.RUNNING:
                    session.state = SessionState.CONNECTED
        return self._submit(host.id, perform).result()

    def use_fake_backend(self):
        self.session_factory = FakeSession

    def _profile_for_host(self, host):
        return self.repository.get_credential_profile(host.auth_profile_id) if self.repository else None

    def _resolve_credential(self, profile):
        if profile is None:
            return None
        credential = self.credential_store.resolve(profile)
        if credential.secret or profile.method not in {AuthMethod.NTLM, AuthMethod.BASIC}:
            return credential
        return self.credential_provider(profile) if self.credential_provider else credential
