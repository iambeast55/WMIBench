"""Native Windows WMI/DCOM adapter. No SMB, WinRM, or Impacket."""
from __future__ import annotations

import re
import sys
import time
import socket
import json
from dataclasses import replace
from contextlib import contextmanager
from threading import Timer, get_native_id
from datetime import datetime, timezone, timedelta

from remoteops.core.models import AuthMethod, CommandResult, SessionState, utc_now
from remoteops.commands.wmi_catalog import WMI_ALIASES, WmiCommandError, execute_command
from remoteops.commands.wmi_definitions import execute_query_plan
from remoteops.remoting.output import plain_value, records_text
from remoteops.remoting.session import PersistentSession, RemoteSessionError


def supported_wmi_aliases():
    return WMI_ALIASES


def _is_provider_error(exc):
    codes = [getattr(exc, "hresult", None)]
    details = getattr(exc, "excepinfo", None)
    if isinstance(details, tuple) and len(details) >= 6:
        codes.append(details[5])
    return any(isinstance(code, int) and ((code & 0xFFFFFFFF) == 0x80070005
               or 0x80041000 <= (code & 0xFFFFFFFF) < 0x80042000)
               for code in codes)


def wmi_session_factory(host, profile):
    if sys.platform == "win32":
        return WmiSession(host, profile)
    from remoteops.remoting.dcom_session import DcomWmiSession
    return DcomWmiSession(host, profile)


@contextmanager
def com_timeout(seconds):
    """Bound synchronous outbound COM calls on this host's dedicated thread."""
    import ctypes
    ole32 = ctypes.WinDLL("ole32")
    enabled = ole32.CoEnableCallCancellation(None) >= 0
    thread_id = get_native_id()
    timer = Timer(seconds, lambda: ole32.CoCancelCall(thread_id, 0))
    timer.daemon = True
    if enabled:
        timer.start()
    try:
        yield
    finally:
        if enabled:
            timer.cancel()
            timer.join()
            ole32.CoDisableCallCancellation(None)


def parse_wmi_date(value):
    match = re.fullmatch(r"(\d{14})\.(\d{6})([+-])(\d{3})", str(value))
    if not match:
        return None
    minutes = int(match[4]) * (1 if match[3] == "+" else -1)
    return datetime.strptime(match[1] + match[2], "%Y%m%d%H%M%S%f").replace(tzinfo=timezone(timedelta(minutes=minutes)))


class NativeWmiClient:
    def __init__(self, host, profile, credential, *, timeout_seconds=30):
        if sys.platform != "win32":
            raise RemoteSessionError(
                "Direct WMI/DCOM on Linux is not implemented in this build. "
                "The native WMI backend requires Windows. No SMB or WinRM fallback was attempted."
            )
        import pythoncom
        import win32com.client

        self.pythoncom = pythoncom
        self.services = None
        pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
        try:
            locator = win32com.client.Dispatch("WbemScripting.SWbemLocator")
            username = credential.username if credential else profile.username
            password = credential.secret if credential else None
            local = host.address.lower() in {".", "localhost", socket.gethostname().lower(), socket.getfqdn().lower()}
            if profile.method is AuthMethod.KERBEROS:
                authority = f"Kerberos:{host.address}"
            else:
                domain = host.address.split(".")[0]
                if "\\" in username:
                    domain, username = username.split("\\", 1)
                elif "@" in username:
                    username, domain = username.rsplit("@", 1)
                authority = f"NTLMDomain:{domain}"
            if local and not username and not password:
                authority = ""
            with com_timeout(timeout_seconds):
                self.services = locator.ConnectServer(host.address, host.wmi_namespace.replace("/", "\\"),
                                                       username or "", password or "", "", authority, 128)
            self.services.Security_.ImpersonationLevel = 3
            self.services.Security_.AuthenticationLevel = 6
        except Exception:
            self.close()
            raise

    def query(self, wql, timeout_seconds, max_rows=10000, *, take=None):
        deadline = time.monotonic() + timeout_seconds
        rows = []
        size = 0
        with com_timeout(timeout_seconds):
            objects = self.services.ExecQuery(wql, "WQL", 48)
            for obj in objects:
                if time.monotonic() > deadline:
                    raise RemoteSessionError("WMI query exceeded its time budget")
                row = {prop.Name: plain_value(prop.Value) for prop in obj.Properties_}
                size += len(json.dumps(row, ensure_ascii=True).encode("utf-8"))
                if size > 32 * 1024 * 1024 or len(rows) >= max_rows:
                    raise RemoteSessionError("WMI query exceeded its row/size limit; no partial results returned")
                rows.append(row)
                if take is not None and len(rows) >= take:
                    break
        return rows

    def query_spec(self, spec, timeout_seconds):
        rows = self.query(spec.wql, timeout_seconds, spec.max_rows, take=spec.take)
        normalized = []
        for row in rows:
            folded = {name.casefold(): value for name, value in row.items()}
            output = {}
            for name, kind in spec.properties.items():
                if name.casefold() not in folded:
                    raise RemoteSessionError(f"{spec.class_name} omitted projected property {name}")
                value = folded[name.casefold()]
                if value is not None and kind in (2, 3, 16, 17, 18, 19, 20, 21):
                    value = int(value)
                output[name] = value
            normalized.append(output)
        return normalized

    def invoke(self, path, method, inputs, timeout_seconds):
        from remoteops.remoting.wmi_methods import _validate_method

        method = _validate_method(path, method, inputs)
        with com_timeout(timeout_seconds):
            parameters = None
            if inputs:
                definition = self.services.Get(path)
                parameters = definition.Methods_(method).InParameters.SpawnInstance_()
                for name, (_, value) in inputs.items():
                    parameters.Properties_(name).Value = value
            output = self.services.ExecMethod_(path, method, parameters)
            return {prop.Name: plain_value(prop.Value) for prop in output.Properties_}

    def close(self):
        self.services = None
        self.pythoncom.CoUninitialize()


class WmiSession(PersistentSession):
    client_factory = NativeWmiClient

    def __init__(self, host, profile):
        super().__init__(host, profile)
        self.client = None
        self.namespace_clients = {}
        self.credential = None

    def connect(self, credential=None):
        if not self.profile or self.profile.method is AuthMethod.BASIC:
            raise RemoteSessionError("WMI requires a Kerberos or NTLM credential profile")
        self.state = SessionState.CONNECTING
        try:
            self.client = self.client_factory(self.host, self.profile, credential)
            self.client.query("SELECT CSName FROM Win32_OperatingSystem", 30)
            self.namespace_clients[self.host.wmi_namespace.replace("\\", "/").casefold()] = self.client
            self.credential = credential
            self.state = SessionState.CONNECTED
            self.last_error = None
        except Exception as exc:
            if self.client:
                self.client.close()
                self.client = None
            self.state = SessionState.ERROR
            self.last_error = str(exc)
            raise RemoteSessionError(self.last_error) from exc

    def run(self, command, timeout_seconds=120):
        raise RemoteSessionError("WMI only accepts validated native command operations")

    def run_prepared(self, command, timeout_seconds=120):
        if not command.native_wmi or (command.wmi_plan is None and command.alias not in WMI_ALIASES):
            raise RemoteSessionError("This command has no native WMI implementation")
        if self.state is not SessionState.CONNECTED or self.client is None:
            raise RemoteSessionError("WMI session is not connected")
        result = CommandResult(self.host.id, self.host.name, command.command_line)
        self.state = SessionState.RUNNING
        actions = []

        def remaining(deadline):
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise TimeoutError("WMI command deadline expired")
            return seconds

        def query(spec, timeout):
            actions.append(f"{spec.namespace}: {spec.wql}")
            deadline = time.monotonic() + timeout
            client = self._namespace_client(spec.namespace, timeout)
            budget = remaining(deadline)
            try:
                return client.query_spec(spec, budget)
            except Exception as exc:
                self._operation_error(client, exc)

        def method(namespace, path, name, inputs, timeout):
            actions.append(f"{namespace}: {path}.{name}({inputs!r})")
            deadline = time.monotonic() + timeout
            client = self._namespace_client(namespace, timeout)
            budget = remaining(deadline)
            try:
                return client.invoke(path, name, inputs, budget)
            except Exception as exc:
                if _is_provider_error(exc):
                    raise WmiCommandError(str(exc)) from exc
                self._operation_error(client, RuntimeError(
                    f"{name} outcome unconfirmed; do not automatically retry: {exc}"))

        try:
            if command.wmi_plan is not None:
                rows = execute_query_plan(command.wmi_plan, query, min(timeout_seconds, 60))
            else:
                rows, _ = execute_command(command.alias, command.values, query, method, min(timeout_seconds, 60))
            result.records = rows
            result.stdout = records_text(rows)
            result.exit_code = 0
        except Exception as exc:
            result.stderr = str(exc)
            result.exit_code = 1
            self.last_error = str(exc)
        finally:
            result.command = "\n".join(actions) or command.command_line
            result.completed_at = utc_now()
            if self.state is not SessionState.ERROR:
                self.state = SessionState.CONNECTED
        return result

    def _operation_error(self, client, exc):
        if _is_provider_error(exc) or isinstance(exc, (RemoteSessionError, WmiCommandError, ValueError, TimeoutError)):
            raise WmiCommandError(str(exc)) from exc
        if client is self.client:
            self.state = SessionState.ERROR
            raise exc
        for key, cached in list(self.namespace_clients.items()):
            if cached is client:
                del self.namespace_clients[key]
        detail = str(exc)
        try:
            client.close()
        except Exception as cleanup:
            detail += f"; namespace cleanup: {cleanup}"
        raise WmiCommandError(f"An auxiliary WMI connection failed: {detail}; primary session retained") from exc

    def _namespace_client(self, namespace, timeout):
        key = namespace.replace("\\", "/").casefold()
        if key not in self.namespace_clients:
            try:
                self.namespace_clients[key] = self.client_factory(
                    replace(self.host, wmi_namespace=namespace), self.profile, self.credential,
                    timeout_seconds=min(timeout, 30),
                )
            except Exception as exc:
                raise WmiCommandError(f"Could not open {key}: {exc}; primary session retained") from exc
        return self.namespace_clients[key]

    def health_check(self):
        try:
            self.client.query("SELECT CSName FROM Win32_OperatingSystem", 15)
        except Exception as exc:
            if _is_provider_error(exc) or isinstance(exc, (RemoteSessionError, WmiCommandError, ValueError, TimeoutError)):
                self.last_error = str(exc)
                return
            self.state = SessionState.ERROR
            self.last_error = str(exc)
            raise

    def disconnect(self):
        clients = list(self.namespace_clients.values())
        if self.client and self.client not in clients:
            clients.append(self.client)
        errors = []
        try:
            for client in clients:
                try:
                    client.close()
                except Exception as exc:
                    errors.append(str(exc))
        finally:
            self.client = None
            self.credential = None
            self.namespace_clients.clear()
            self.state = SessionState.DISCONNECTED
        if errors:
            raise RemoteSessionError("; ".join(errors))
