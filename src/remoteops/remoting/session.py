from __future__ import annotations

import subprocess
import traceback
import time
from abc import ABC, abstractmethod
from inspect import signature

from remoteops.core.credentials import ResolvedCredential
from remoteops.core.models import AuthMethod, CommandResult, CredentialProfile, Host, SessionState, utc_now
from remoteops.core.authentication import ntlm_identity, portable_kerberos
from remoteops.remoting.output import output_records, records_text


class RemoteSessionError(RuntimeError):
    pass


class CommandExecutionError(RemoteSessionError):
    """A command failed after its pipeline reached a confirmed terminal state."""


CONNECT_TIMEOUT_SECONDS = 15
OPERATION_TIMEOUT_SECONDS = 20
READ_TIMEOUT_SECONDS = 35


class PersistentSession(ABC):
    """One thread-owned connection for a host/auth profile pair."""

    def __init__(self, host: Host, profile: CredentialProfile | None) -> None:
        self.host = host
        self.profile = profile
        self.state = SessionState.DISCONNECTED
        self.last_error: str | None = None

    def health_check(self) -> None:
        if self.state is not SessionState.CONNECTED:
            raise RemoteSessionError("Session is not connected")

    def maintenance_due(self, now: float) -> bool:
        return False

    def maintain(self) -> None:
        """Optional protocol lifetime maintenance, independent of user health checks."""

    def run_prepared(self, command, timeout_seconds=120):
        if not command.script.strip():
            raise RemoteSessionError("This command has no PowerShell implementation")
        return self.run(command.script, timeout_seconds)

    @property
    def key(self) -> tuple[str, str | None]:
        return (self.host.id, self.profile.id if self.profile else None)

    @abstractmethod
    def connect(self, credential: ResolvedCredential | None = None) -> None:
        raise NotImplementedError

    @abstractmethod
    def run(self, command: str, timeout_seconds: int = 120) -> CommandResult:
        raise NotImplementedError

    @abstractmethod
    def disconnect(self) -> None:
        raise NotImplementedError


class FakeSession(PersistentSession):
    """Development backend that runs commands locally through PowerShell when available."""

    def connect(self, credential: ResolvedCredential | None = None) -> None:
        self.state = SessionState.CONNECTED
        self.last_error = None

    def run(self, command: str, timeout_seconds: int = 120) -> CommandResult:
        if self.state is SessionState.DISCONNECTED:
            raise RemoteSessionError("Session is not connected")

        result = CommandResult(host_id=self.host.id, host_name=self.host.name, command=command)
        self.state = SessionState.RUNNING
        executable = _find_local_shell()

        try:
            completed = subprocess.run(
                [executable, _shell_command_arg(executable), command],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            result.stdout = completed.stdout
            result.stderr = completed.stderr
            result.exit_code = completed.returncode
            self.last_error = result.stderr or None
        except subprocess.TimeoutExpired as exc:
            result.stderr = f"Command timed out after {timeout_seconds} seconds"
            result.exit_code = 124
            self.last_error = result.stderr
            if exc.stdout:
                result.stdout = exc.stdout if isinstance(exc.stdout, str) else exc.stdout.decode()
            if exc.stderr:
                result.stderr += "\n" + (
                    exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode()
                )
        finally:
            result.completed_at = utc_now()
            self.state = SessionState.CONNECTED

        return result

    def disconnect(self) -> None:
        self.state = SessionState.DISCONNECTED


class PsrpSession(PersistentSession):
    """PSRP/WinRM backend using pypsrp.

    The object is deliberately persistent: connect creates one runspace pool and run sends
    commands into that pool until disconnect is called.
    """

    def __init__(self, host: Host, profile: CredentialProfile | None) -> None:
        super().__init__(host, profile)
        self._pool = None
        self._wsman = None
        self._stream_text = ""
        self.ticket_provider = None

    def connect(self, credential: ResolvedCredential | None = None) -> None:
        try:
            from pypsrp.powershell import RunspacePool
            from pypsrp.wsman import WSMan
        except ImportError as exc:
            raise RemoteSessionError(
                "pypsrp is not installed. Install project dependencies to use real WinRM sessions."
            ) from exc

        if self.profile is None:
            raise RemoteSessionError("A credential profile is required for PSRP sessions")

        self.state = SessionState.CONNECTING
        try:
            username = credential.username if credential else self.profile.username
            password = credential.secret if credential else None
            if self.profile.method is AuthMethod.NTLM_HASH:
                username = ntlm_identity(username, password, self.profile.method)
                password = None
            ssl = self.host.transport.value == "https"
            address = self.host.address
            kerberos = portable_kerberos(self.profile)
            if kerberos:
                from remoteops.remoting.kerberos import TicketProvider
                self.ticket_provider = TicketProvider(self.host, self.profile, credential)
                address = self.ticket_provider.hostname
                password = None
            self._wsman = WSMan(
                address,
                username=username or None,
                password=password,
                ssl=ssl,
                port=self.host.port,
                auth="kerberos" if kerberos else "ntlm" if self.profile.method is AuthMethod.NTLM_HASH else self.profile.method.value,
                connection_timeout=CONNECT_TIMEOUT_SECONDS,
                operation_timeout=OPERATION_TIMEOUT_SECONDS,
                read_timeout=READ_TIMEOUT_SECONDS,
                reconnection_retries=0,
            )
            if kerberos:
                from remoteops.remoting.kerberos_http import configure
                configure(self._wsman, self.ticket_provider)
            self._pool = RunspacePool(self._wsman, min_runspaces=1, max_runspaces=1)
            self._pool.open()
            _output, errors = self.execute("$PSVersionTable.PSVersion.ToString()", 30)
            if errors:
                raise RemoteSessionError(errors)
            self.state = SessionState.CONNECTED
            self.last_error = None
        except RemoteSessionError as exc:
            self._close_resources(ignore_errors=True)
            self.state = SessionState.ERROR
            self.last_error = str(exc)
            raise
        except Exception as exc:  # pypsrp raises several transport-specific exceptions.
            self._close_resources(ignore_errors=True)
            self.state = SessionState.ERROR
            self.last_error = _format_exception(exc)
            raise RemoteSessionError(self.last_error) from exc

    def run(self, command: str, timeout_seconds: int = 120, *, cancel=None) -> CommandResult:
        if self._pool is None or self.state is SessionState.DISCONNECTED:
            raise RemoteSessionError("Session is not connected")

        result = CommandResult(host_id=self.host.id, host_name=self.host.name, command=command)
        self.state = SessionState.RUNNING
        try:
            output, errors = (self.execute(command, timeout_seconds, cancel=cancel) if cancel is not None
                              else self.execute(command, timeout_seconds))
            result.records = output_records(output)
            result.stdout = records_text(result.records)
            if self._stream_text:
                result.stdout += ("\n" if result.stdout else "") + self._stream_text
            result.stderr = errors
            result.exit_code = 1 if errors else 0
            self.last_error = result.stderr or None
        except Exception as exc:
            result.stderr = _format_exception(exc)
            result.exit_code = 1
            self.last_error = result.stderr
        finally:
            result.completed_at = utc_now()
            if self.state is not SessionState.ERROR:
                self.state = SessionState.CONNECTED
        return result

    def disconnect(self) -> None:
        try:
            self._close_resources()
        finally:
            self.state = SessionState.DISCONNECTED

    def execute(self, script: str, timeout_seconds: int = 120, *, cancel=None):
        from pypsrp.powershell import PowerShell, PSInvocationState

        if cancel is not None and cancel.is_set():
            raise CommandExecutionError("Command cancelled before dispatch.")
        pipeline = PowerShell(self._pool)
        pipeline.add_script(script, use_local_scope=False)
        deadline = time.monotonic() + timeout_seconds
        try:
            pipeline.begin_invoke()
            while pipeline.state == PSInvocationState.RUNNING:
                remaining = deadline - time.monotonic()
                cancelled = cancel is not None and cancel.is_set()
                if remaining <= 0 or cancelled:
                    pipeline.stop()
                    if pipeline.state not in (PSInvocationState.STOPPED, PSInvocationState.COMPLETED, PSInvocationState.FAILED):
                        raise RemoteSessionError("Timed-out pipeline could not be confirmed stopped; reconnect explicitly.")
                    reason = "Command cancelled" if cancelled else f"Command timed out after {timeout_seconds}s"
                    raise CommandExecutionError(reason + "; remote side effects may already have occurred.")
                pipeline.poll_invoke(timeout=max(1, min(5, int(remaining))))
            errors = _format_ps_error_stream(pipeline.streams)
            if pipeline.state == PSInvocationState.FAILED and not errors:
                errors = "Remote pipeline failed without an error record"
            self._stream_text = "\n".join(
                f"[{name}] {_stream_record_text(item)}" for name in ("warning", "verbose", "debug", "information")
                for item in getattr(pipeline.streams, name, [])
            )
            return pipeline.output, errors
        except CommandExecutionError:
            raise
        except Exception as exc:
            self.state = SessionState.ERROR
            self.last_error = str(exc)
            raise
        finally:
            if pipeline.state in (PSInvocationState.COMPLETED, PSInvocationState.STOPPED, PSInvocationState.FAILED):
                try:
                    pipeline.close()
                except Exception:
                    self.state = SessionState.ERROR
                    raise

    def health_check(self) -> None:
        try:
            _output, errors = self.execute("$null = 1", 15)
            if errors:
                self.last_error = errors
        except CommandExecutionError as exc:
            self.last_error = str(exc)
        except Exception as exc:
            self.state = SessionState.ERROR
            self.last_error = f"Session lost during idle check: {exc}. Reconnect to create a new session."
            raise RemoteSessionError(self.last_error) from exc

    def _close_resources(self, ignore_errors=False) -> None:
        pool, wsman = self._pool, self._wsman
        self._pool = self._wsman = None
        failure = None
        for resource in (pool, wsman):
            if resource:
                try:
                    resource.close()
                except Exception as exc:
                    failure = exc
        if self.ticket_provider:
            self.ticket_provider.close()
            self.ticket_provider = None
        if failure and not ignore_errors:
            raise failure


def _find_local_shell() -> str:
    for executable in ("pwsh", "powershell", "cmd"):
        try:
            subprocess.run(
                [executable, "/?" if executable == "cmd" else "-NoLogo"],
                check=False,
                capture_output=True,
                timeout=3,
            )
            return executable
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return "cmd"


def _shell_command_arg(executable: str) -> str:
    return "/c" if executable == "cmd" else "-Command"


def _build_client(client_type: type, server: str, **kwargs: object) -> object:
    timeout_kwargs = {
        "connection_timeout": CONNECT_TIMEOUT_SECONDS,
        "operation_timeout": OPERATION_TIMEOUT_SECONDS,
        "read_timeout": READ_TIMEOUT_SECONDS,
    }
    supported = set(signature(client_type).parameters)
    for key, value in timeout_kwargs.items():
        if key in supported:
            kwargs[key] = value
    return client_type(server, **kwargs)


def _stringify_ps_output(output: object) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, bytes):
        return output.decode(errors="replace")

    try:
        items = list(output)  # type: ignore[arg-type]
    except TypeError:
        return str(output)

    return "\n".join(str(item) for item in items if item is not None)


def _stream_record_text(record: object) -> str:
    """Extract text from pypsrp's informational stream record types."""
    for attribute in ("message_data", "message", "status_description", "activity"):
        value = getattr(record, attribute, None)
        if value is not None:
            return _stringify_ps_output(value)
    return _stringify_ps_output(record)


def _format_ps_error_stream(streams: object) -> str:
    if streams is None:
        return ""

    errors = getattr(streams, "error", None)
    if not errors:
        return ""

    lines: list[str] = []
    for error in errors:
        text = _format_ps_error_record(error)
        if text:
            lines.append(text)
    return "\n".join(lines)


def _format_ps_error_record(error: object) -> str:
    pieces: list[str] = []
    for attr in ("message", "exception", "script_stacktrace", "category_info", "fully_qualified_error_id"):
        value = getattr(error, attr, None)
        if value:
            pieces.append(str(value))

    if pieces:
        return "\n".join(pieces)
    return str(error)


def _format_exception(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()
