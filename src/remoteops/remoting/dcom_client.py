"""Thread-owned experimental WMI session using our RPC/DCOM codecs.

Associations and namespace references persist; each catalog query owns a temporary
enumerator. No retries, authentication fallback, arbitrary CLI WQL, or transport relay.
The caller schedules maintain() on the owning thread while idle.
"""
from __future__ import annotations

import math
import json
import re
import time
from contextlib import ExitStack
from threading import get_ident
from uuid import uuid4

from spnego.exceptions import SpnegoError

from remoteops.remoting import rpc_auth_probe as rpc
from remoteops.remoting.dcom_probe import discover_version
from remoteops.remoting.dcom_wire import (
    ACTIVATION, REM_UNKNOWN, WBEM_LOGIN_INTERFACE, WBEM_SERVICES_INTERFACE,
    DcomStatusError, activation_request, complex_ping_request, decode_activation,
    decode_complex_ping, decode_namespace_login, decode_release, decode_simple_ping,
    namespace_login_request, normalize_namespace, release_request, simple_ping_request,
)
from remoteops.remoting.rpc_probe import RpcProbeError, RpcProtocolError, _remaining
from remoteops.remoting.wmi_query import (
    ENUM_INTERFACE, OS_PROPERTIES, OS_QUERY, WmiQueryError, decode_next, decode_query,
    next_request, query_request,
)

PING_INTERVAL = 60
MAX_QUERY_SECONDS = 60
MAX_QUERY_BYTES = 32 * 1024 * 1024


def _error_text(exc):
    if isinstance(exc, SpnegoError):
        return f"NTLM protection failed: {type(exc).__name__} (code {exc.ERROR_CODE})"
    if isinstance(exc, KeyboardInterrupt):
        return "WMI operation cancelled"
    if isinstance(exc, TimeoutError):
        return "WMI operation deadline expired"
    if isinstance(exc, OSError):
        return f"WMI network failure (OS code {exc.errno})"
    return str(exc)


class DcomClient:
    def __init__(self):
        self.owner = get_ident()
        self.stack = ExitStack()
        self.activation = None
        self.services = None
        self.namespace = None
        self.channels = {}
        self.references = []
        self.set_id = 0
        self.sequence = 1
        self.ping_oids = set()
        self.next_ping = None
        self.uncertain = []
        self.closed = False
        self.ready = False
        self.causality = uuid4()
        self.authentication = 10

    def _thread(self):
        if get_ident() != self.owner:
            raise RpcProbeError("WMI session must be used on its owning host worker")

    def _call(self, interface, opnum, stub, deadline, object_id=None):
        return self.channels[interface].call(opnum, stub, deadline, object_id)

    def _track(self, reference):
        if reference.oxid != self.activation.oxid:
            self.uncertain.append("cross-exporter reference cleanup unconfirmed")
            raise RpcProtocolError("Cross-exporter WMI references are not supported")
        self.references.append(reference)
        if not reference.flags & 0x1000:
            reference.resolver_bindings.require_authentication(self.authentication)
        # Only the approved host's endpoint mapper is supported as object resolver.
        for protocol, binding in reference.resolver_bindings.strings:
            if protocol != 7:
                continue
            match = re.search(r"\[([0-9]+)\]$", binding)
            if "[" in binding or "]" in binding:
                if match is None or int(match[1]) != self.resolver_port:
                    raise RpcProtocolError("Non-default/cross-resolver WMI bindings are unsupported")
            return
        if not reference.flags & 0x1000:
            raise RpcProtocolError("Pingable WMI reference omitted its TCP resolver binding")

    def connect(self, address, username, password, namespace="root/cimv2", timeout=30,
                *, probe_os=True, ticket_provider=None):
        self._thread()
        if self.closed or self.activation is not None:
            raise RpcProbeError("Create a new WMI client for each explicit connection")
        ip = rpc.validate_target(address, username, password, 135, timeout)
        namespace = normalize_namespace(namespace)
        if probe_os and namespace.casefold() != "root\\cimv2":
            raise ValueError("Initial WMI sessions require namespace root/cimv2")
        self.namespace = namespace.casefold()
        address = str(ip)
        self.resolver_port = 135
        deadline = time.monotonic() + timeout
        self.authentication = 16 if ticket_provider is not None else 10

        def channel_for(interface, port):
            if ticket_provider is not None:
                from remoteops.remoting.kerberos_rpc import connection
                return connection(ticket_provider, interface, port, deadline)
            return rpc.ntlm_connection(address, username, password, interface, port, deadline)

        def open_channel(interface, port):
            self.channels[interface] = self.stack.enter_context(channel_for(interface, port))

        try:
            version = discover_version(address, 135, deadline)
            with channel_for(ACTIVATION, 135) as channel:
                self.uncertain.append("activation reference cleanup unconfirmed")
                self.activation = decode_activation(channel.call(
                    0, activation_request(version, self.causality), deadline
                ))
                self.uncertain.pop()
            self.references.append(self.activation.reference)
            self.activation.bindings.require_authentication(self.authentication)
            port = self.activation.bindings.tcp_port()
            open_channel(REM_UNKNOWN, port)
            with channel_for(WBEM_LOGIN_INTERFACE, port) as channel:
                self.uncertain.append("namespace reference cleanup unconfirmed")
                reply = channel.call(6, namespace_login_request(
                    self.activation.version, self.causality, namespace
                ), deadline, self.activation.reference.ipid)
                try:
                    services = decode_namespace_login(reply)
                except DcomStatusError:
                    self.uncertain.pop()
                    raise
                self.uncertain.pop()
                self._track(services)
                self.services = services
            self._release((self.activation.reference,), deadline)
            open_channel(rpc.OBJECT_EXPORTER, self.resolver_port)
            open_channel(WBEM_SERVICES_INTERFACE, port)
            open_channel(ENUM_INTERFACE, port)
            self._sync_ping(deadline)
            if probe_os:
                self._query(deadline)
            self.ready = True
        except (Exception, KeyboardInterrupt) as exc:
            self._fail(exc)

    def _sync_ping(self, deadline):
        desired = {ref.oid for ref in self.references if not ref.flags & 0x1000}
        if desired == self.ping_oids:
            if self.set_id:
                decode_simple_ping(self._call(rpc.OBJECT_EXPORTER, 1,
                    simple_ping_request(self.set_id), deadline))
        else:
            sequence = self.sequence if self.set_id else 1
            if sequence > 65535:
                raise RpcProbeError("DCOM ping sequence exhausted; reconnect explicitly")
            # Consume before sending: a lost reply must not reuse the sequence for cleanup.
            self.sequence = sequence + 1
            self.uncertain.append("ping-set update outcome unconfirmed")
            reply = self._call(rpc.OBJECT_EXPORTER, 2, complex_ping_request(
                self.set_id, sequence, tuple(sorted(desired - self.ping_oids)),
                tuple(sorted(self.ping_oids - desired)),
            ), deadline)
            set_id = decode_complex_ping(reply, self.set_id)
            self.uncertain.pop()
            self.set_id = set_id if desired else 0
            self.ping_oids = desired
        self.next_ping = time.monotonic() + PING_INTERVAL

    def _release(self, references, deadline):
        # Remove before dispatch: never repeat a release after an ambiguous reply.
        for reference in references:
            self.references.remove(reference)
        self.uncertain.append("remote reference release unconfirmed")
        decode_release(self._call(REM_UNKNOWN, 5, release_request(
            self.activation, self.causality, references
        ), deadline, self.activation.remote_unknown))
        self.uncertain.pop()

    def _query(self, deadline, *, wql=OS_QUERY, class_name="Win32_OperatingSystem",
               properties=None, max_rows=1, require_one=True, take=None):
        properties = OS_PROPERTIES if properties is None else properties
        request = query_request(self.activation.version, self.causality, wql)
        self.uncertain.append("query enumerator cleanup unconfirmed")
        reply = self._call(WBEM_SERVICES_INTERFACE, 20, request, deadline, self.services.ipid)
        try:
            enumerator = decode_query(reply)
        except DcomStatusError as exc:
            self.uncertain.pop()
            raise WmiQueryError(str(exc)) from exc
        self.uncertain.pop()
        self._track(enumerator)
        self._sync_ping(deadline)
        try:
            records = self._enumerate(enumerator, deadline, class_name, properties,
                                      max_rows, require_one, take)
        except WmiQueryError:
            cleanup_deadline = time.monotonic() + 5
            self._release((enumerator,), cleanup_deadline)
            self._sync_ping(cleanup_deadline)
            raise
        cleanup_deadline = max(deadline, time.monotonic() + 5)
        self._release((enumerator,), cleanup_deadline)
        self._sync_ping(cleanup_deadline)
        return records

    def _enumerate(self, enumerator, deadline, class_name, properties,
                   max_rows, require_one, take):
        records = []
        wire_bytes = decoded_bytes = 0
        for _ in range(max_rows + 1):
            if time.monotonic() >= deadline:
                raise WmiQueryError("WMI query deadline expired; no partial results returned")
            if self.next_ping is not None and time.monotonic() >= self.next_ping:
                self._sync_ping(deadline)
            try:
                timeout_ms = max(1, min(60000, int(_remaining(deadline) * 1000)))
            except TimeoutError as exc:
                raise WmiQueryError("WMI query deadline expired; no partial results returned") from exc
            self.uncertain.append("result object cleanup unconfirmed")
            reply = self._call(ENUM_INTERFACE, 4, next_request(
                self.activation.version, self.causality, timeout_ms
            ), deadline, enumerator.ipid)
            try:
                batch, status = decode_next(reply, class_name, properties)
            except (DcomStatusError, WmiQueryError) as exc:
                self.uncertain.pop()
                raise WmiQueryError(str(exc)) from exc
            self.uncertain.pop()
            wire_bytes += len(reply)
            decoded_bytes += len(json.dumps(batch, ensure_ascii=True).encode("utf-8"))
            if max(wire_bytes, decoded_bytes) > MAX_QUERY_BYTES:
                raise WmiQueryError("WMI query exceeded the aggregate size limit; no partial results returned")
            records.extend(batch)
            if len(records) > max_rows:
                message = ("OS query returned more than one instance" if require_one else
                           f"WMI query exceeded {max_rows} rows; no partial results returned")
                raise WmiQueryError(message)
            if status == 0x40004:
                raise WmiQueryError("WMI enumeration timed out; result set is incomplete")
            if status == 1 or (take is not None and len(records) >= take):
                break
        else:
            raise WmiQueryError("WMI query enumeration did not reach its end")
        if require_one and len(records) != 1:
            raise WmiQueryError("OS query returned no operating-system instance")
        return records

    def query_os(self, timeout_seconds=60):
        return self._perform_query(timeout_seconds)

    def query(self, spec, timeout_seconds=60):
        self._thread()
        if not self.ready or self.closed:
            raise RpcProbeError("WMI session is not connected; reconnect explicitly")
        if spec.namespace.replace("/", "\\").casefold() != self.namespace:
            raise ValueError("Query namespace does not match the connected WMI namespace")
        if not 1 <= spec.max_rows <= 10000:
            raise ValueError("WMI query row limit must be 1..10000")
        return self._perform_query(timeout_seconds, wql=spec.wql, class_name=spec.class_name,
                                   properties=spec.properties, max_rows=spec.max_rows,
                                   require_one=False, take=spec.take)

    def _perform_query(self, timeout_seconds, **options):
        self._thread()
        if not self.ready or self.closed:
            raise RpcProbeError("WMI session is not connected; reconnect explicitly")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("WMI query timeout must be positive and finite")
        deadline = time.monotonic() + min(timeout_seconds, MAX_QUERY_SECONDS)
        try:
            self._sync_ping(deadline)
            return self._query(deadline, **options)
        except WmiQueryError:
            raise
        except (Exception, KeyboardInterrupt) as exc:
            self._fail(exc)

    def invoke(self, path, method, inputs, timeout_seconds=60, *, outputs=None, allow_no_output=False):
        from remoteops.remoting.wmi_methods import method_request, decode_method

        self._thread()
        if type(allow_no_output) is not bool or (allow_no_output and outputs is None):
            raise ValueError("allow_no_output requires a boolean and an explicit output schema")
        if not self.ready or self.closed:
            raise RpcProbeError("WMI session is not connected; reconnect explicitly")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("WMI method timeout must be positive and finite")
        if outputs is None and method.casefold() in ("startservice", "stopservice") and self.namespace != "root\\cimv2":
            raise ValueError("Service operations require root/cimv2")
        if outputs is None and method.casefold() == "enumkey" and self.namespace not in ("root\\default", "root\\cimv2"):
            raise ValueError("Registry operations require root/default or root/cimv2")
        request = method_request(self.activation.version, self.causality, path, method, inputs, outputs=outputs)
        properties = dict(outputs) if outputs is not None else {"ReturnValue": 19}
        if outputs is None and method.casefold() == "enumkey":
            properties["sNames"] = 0x2008
        deadline = time.monotonic() + min(timeout_seconds, MAX_QUERY_SECONDS)
        try:
            self._sync_ping(deadline)
            self.uncertain.append(f"{method} outcome unconfirmed; do not automatically retry")
            reply = self._call(WBEM_SERVICES_INTERFACE, 24, request, deadline, self.services.ipid)
            try:
                result = decode_method(reply, properties, allow_no_output=allow_no_output)
            except DcomStatusError as exc:
                self.uncertain.pop()
                raise WmiQueryError(str(exc)) from exc
            except WmiQueryError:
                self.uncertain.pop()
                raise
            self.uncertain.pop()
            return result
        except WmiQueryError:
            raise
        except (Exception, KeyboardInterrupt) as exc:
            self._fail(exc)

    def delete_instance(self, path, timeout_seconds=60):
        from remoteops.remoting.wmi_methods import delete_request, decode_delete
        self._thread()
        if not self.ready or self.closed:
            raise RpcProbeError("WMI session is not connected; reconnect explicitly")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("WMI method timeout must be positive and finite")
        request = delete_request(self.activation.version, self.causality, path)
        deadline = time.monotonic() + min(timeout_seconds, MAX_QUERY_SECONDS)
        try:
            self._sync_ping(deadline)
            self.uncertain.append("DeleteInstance outcome unconfirmed; do not automatically retry")
            reply = self._call(WBEM_SERVICES_INTERFACE, 16, request, deadline, self.services.ipid)
            try:
                decode_delete(reply)
            except DcomStatusError as exc:
                self.uncertain.pop()
                raise WmiQueryError(str(exc)) from exc
            self.uncertain.pop()
            return {"Deleted": True, "Path": path}
        except WmiQueryError:
            raise
        except (Exception, KeyboardInterrupt) as exc:
            self._fail(exc)

    def maintain(self):
        self._thread()
        if not self.ready or self.closed:
            raise RpcProbeError("WMI session is not connected; reconnect explicitly")
        try:
            self._sync_ping(time.monotonic() + 15)
        except (Exception, KeyboardInterrupt) as exc:
            self._fail(exc)

    def _fail(self, exc):
        message = _error_text(exc)
        try:
            self.close()
        except RpcProbeError as cleanup:
            message += f"; {cleanup}"
        raise RpcProbeError(f"{message}. Reconnect explicitly.") from None

    def close(self):
        self._thread()
        if self.closed:
            return
        self.closed, self.ready, self.next_ping = True, False, None
        deadline = time.monotonic() + 5
        failures = []
        try:
            if self.references:
                try:
                    self._release(tuple(self.references), deadline)
                except (Exception, KeyboardInterrupt) as exc:
                    failures.append("reference cleanup failed: " + _error_text(exc))
            # Delete only a known ping set, even if a prior set update was uncertain.
            if self.set_id and self.ping_oids:
                try:
                    self._sync_ping(deadline)
                except (Exception, KeyboardInterrupt) as exc:
                    failures.append("ping-set cleanup failed: " + _error_text(exc))
        finally:
            try:
                self.stack.close()
            except (Exception, KeyboardInterrupt) as exc:
                failures.append("connection cleanup failed: " + _error_text(exc))
            self.channels.clear()
            self.next_ping = None
            self.services = None
        if failures or self.uncertain:
            raise RpcProbeError("; ".join(failures + self.uncertain))
