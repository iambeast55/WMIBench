"""Experimental application-owned WMI adapter for non-Windows clients."""
from __future__ import annotations

import time

from remoteops.commands.wmi_catalog import WMI_ALIASES, WmiCommandError, execute_command
from remoteops.commands.wmi_definitions import execute_query_plan
from remoteops.core.models import AuthMethod, CommandResult, SessionState, utc_now
from remoteops.core.authentication import ntlm_identity, portable_kerberos
from remoteops.remoting.dcom_client import DcomClient
from remoteops.remoting.rpc_probe import _remaining
from remoteops.remoting.output import records_text
from remoteops.remoting.session import PersistentSession, RemoteSessionError
from remoteops.remoting.wmi_query import OS_QUERY, WmiQueryError


class DcomWmiSession(PersistentSession):
    client_factory = DcomClient

    def __init__(self, host, profile):
        super().__init__(host, profile)
        self.client = None
        self.namespace_clients = {}
        self.identity = None
        self.address = host.address
        self.ticket_provider = None

    def connect(self, credential=None):
        if self.client is not None:
            raise RemoteSessionError("Disconnect the existing WMI session before reconnecting")
        self.state = SessionState.CONNECTING
        try:
            kerberos = portable_kerberos(self.profile)
            if not kerberos and (self.profile is None or self.profile.method not in (AuthMethod.NTLM, AuthMethod.NTLM_HASH)):
                raise RemoteSessionError("Experimental Linux WMI requires an NTLM profile; Kerberos is not implemented")
            username = credential.username if credential else self.profile.username
            password = credential.secret if credential else None
            if not username or not password:
                raise RemoteSessionError("Experimental Linux WMI requires an explicit username and password")
            if self.profile.method is AuthMethod.NTLM_HASH:
                password = ntlm_identity(username, password, self.profile.method)
            if kerberos:
                from remoteops.remoting.kerberos import TicketProvider
                self.ticket_provider = TicketProvider(self.host, self.profile, credential)
                self.address = self.ticket_provider.address
            self.client = self.client_factory()
            options = {"ticket_provider": self.ticket_provider} if kerberos else {}
            self.client.connect(self.address, username, password, self.host.wmi_namespace, **options)
            self.namespace_clients["root/cimv2"] = self.client
            self.identity = (username, password)
            self.state = SessionState.CONNECTED
            self.last_error = None
        except Exception as exc:
            self.state = SessionState.ERROR
            self.last_error = str(exc)
            self._close_after_error()
            raise RemoteSessionError(self.last_error) from None

    def run(self, command, timeout_seconds=120):
        raise RemoteSessionError("WMI only accepts validated native command operations")

    def run_prepared(self, command, timeout_seconds=120):
        if not command.native_wmi or (command.delete_plan is None and command.method_plan is None and command.wmi_plan is None and command.alias not in WMI_ALIASES):
            raise RemoteSessionError("This command has no native WMI implementation")
        if self.state is not SessionState.CONNECTED or self.client is None:
            raise RemoteSessionError("WMI session is not connected; reconnect explicitly")
        result = CommandResult(self.host.id, self.host.name, command.command_line,
                               connection_mode=self.host.connection_mode.value)
        self.state = SessionState.RUNNING
        actions = []

        def query(spec, timeout):
            actions.append(f"{spec.namespace}: {spec.wql}")
            deadline = time.monotonic() + timeout
            client = self._namespace_client(spec.namespace, timeout)
            remaining = _remaining(deadline)
            if command.alias == "sysinfo" and command.wmi_plan is None:
                return self._operation(client, lambda: client.query_os(remaining))
            return self._operation(client, lambda: client.query(spec, remaining))

        def method(namespace, path, name, inputs, timeout, outputs=None, allow_no_output=False):
            actions.append(f"{namespace}: {path}.{name}({inputs!r})")
            deadline = time.monotonic() + timeout
            client = self._namespace_client(namespace, timeout)
            remaining = _remaining(deadline)
            options = {"outputs": outputs} if outputs is not None else {}
            if allow_no_output:
                options["allow_no_output"] = True
            return self._operation(client, lambda: client.invoke(path, name, inputs, remaining, **options))

        try:
            if command.delete_plan is not None:
                plan = command.delete_plan
                budget = min(timeout_seconds, plan.timeout_seconds)
                deadline = time.monotonic() + budget
                client = self._namespace_client(plan.namespace, budget)
                actions.append(f"{plan.namespace}: DeleteInstance {plan.path}")
                budget = _remaining(deadline)
                result.records = [self._operation(client, lambda: client.delete_instance(plan.path, budget))]
            elif command.method_plan is not None:
                plan = command.method_plan
                result.records = [method(plan.namespace, plan.path, plan.method, plan.inputs,
                                         min(timeout_seconds, plan.timeout_seconds), plan.outputs, plan.allow_no_output)]
            elif command.wmi_plan is not None:
                result.records = execute_query_plan(command.wmi_plan, query, min(timeout_seconds, 60))
            else:
                result.records, _ = execute_command(command.alias, command.values, query, method,
                                                    min(timeout_seconds, 60))
            result.stdout = records_text(result.records)
            result.exit_code = 0
            self.state = SessionState.CONNECTED
        except (WmiCommandError, WmiQueryError, ValueError, TimeoutError) as exc:
            result.stderr = str(exc)
            result.exit_code = 1
            self.state = SessionState.CONNECTED
        except Exception as exc:
            self.last_error = str(exc)
            # Only a failed transport operation can invalidate the connection.
            # Local result processing is not evidence of a broken RPC channel.
            if self.state is SessionState.ERROR:
                self._close_after_error()
            else:
                self.state = SessionState.CONNECTED
            result.stderr = self.last_error
            result.exit_code = 1
        finally:
            result.command = (OS_QUERY if command.alias == "sysinfo" and command.wmi_plan is None
                              else "\n".join(actions) or command.command_line)
            result.completed_at = utc_now()
        return result

    def _operation(self, client, operation):
        try:
            return operation()
        except (WmiQueryError, WmiCommandError, ValueError, TimeoutError):
            raise
        except Exception as exc:
            if client is self.client:
                self.state = SessionState.ERROR
                raise
            self._retire_namespace(client)
            raise WmiCommandError(f"An auxiliary WMI connection failed: {exc}; primary session retained") from exc

    def _retire_namespace(self, client):
        for key, cached in list(self.namespace_clients.items()):
            if cached is client:
                del self.namespace_clients[key]
        try:
            client.close()
        except Exception as exc:
            self.last_error = f"Auxiliary namespace cleanup failed: {exc}"

    def _namespace_client(self, namespace, timeout):
        key = namespace.replace("\\", "/").casefold()
        if key in self.namespace_clients:
            return self.namespace_clients[key]
        if len(self.namespace_clients) >= 16:
            raise WmiCommandError("WMI namespace limit reached (16 cached namespaces); existing sessions retained")
        if self.identity is None:
            raise RemoteSessionError("WMI session identity is unavailable; reconnect explicitly")
        client = self.client_factory()
        # Register before connecting so failure cleanup also covers partial sessions.
        self.namespace_clients[key] = client
        try:
            options = {"ticket_provider": self.ticket_provider} if self.ticket_provider else {}
            client.connect(self.address, *self.identity, key, timeout=min(timeout, 30), probe_os=False, **options)
        except Exception as exc:
            self.namespace_clients.pop(key)
            detail = str(exc)
            try:
                client.close()
            except Exception as cleanup:
                detail += f"; namespace cleanup: {cleanup}"
            raise WmiCommandError(f"Could not open {key}: {detail}; primary session retained") from exc
        return client

    def maintenance_due(self, now):
        return self.state is SessionState.CONNECTED and any(
            client.next_ping is not None and now >= client.next_ping
            for client in self.namespace_clients.values()
        )

    def maintain(self):
        self._check(False)

    def health_check(self):
        self._check(True)

    def _check(self, query):
        if self.state is not SessionState.CONNECTED or self.client is None:
            raise RemoteSessionError("WMI session is not connected")
        try:
            for client in list(self.namespace_clients.values()):
                if not query or client is not self.client:
                    try:
                        self._operation(client, client.maintain)
                    except WmiCommandError as exc:
                        self.last_error = str(exc)
            if query:
                self.client.query_os(15)
        except WmiQueryError as exc:
            # A provider/query error is not evidence that the RPC association died.
            self.last_error = str(exc)
        except Exception as exc:
            self.state = SessionState.ERROR
            self.last_error = str(exc)
            self._close_after_error()
            raise RemoteSessionError(self.last_error) from None

    def _close_after_error(self):
        try:
            self._close_clients()
        except Exception as exc:
            self.last_error = (self.last_error or "WMI operation failed") + f"; cleanup: {exc}"

    def _close_clients(self):
        clients = list(self.namespace_clients.values())
        if self.client is not None and self.client not in clients:
            clients.append(self.client)
        self.namespace_clients.clear()
        self.client = self.identity = None
        failures = []
        for client in clients:
            try:
                client.close()
            except Exception as exc:
                failures.append(str(exc))
        if self.ticket_provider:
            self.ticket_provider.close()
            self.ticket_provider = None
        if failures:
            raise RemoteSessionError("; ".join(failures))

    def disconnect(self):
        try:
            self._close_clients()
        except Exception as exc:
            self.last_error = str(exc)
            raise RemoteSessionError(self.last_error) from None
        finally:
            self.state = SessionState.DISCONNECTED
