"""Activate WMI, optionally verify namespace/one OS query, then release references.

Only a fixed read-only OS query; no process creation or GUI integration. Authenticated calls
use NTLMv2 with packet privacy; ServerAlive2 discovery is explicitly unauthenticated.
"""
from __future__ import annotations

import argparse
import getpass
import json
import socket
import struct
import sys
import time
import warnings
from datetime import datetime, timezone
from dataclasses import dataclass
from uuid import uuid4

from remoteops.remoting import rpc_auth_probe as rpc
from remoteops.remoting.dcom_wire import (
    ACTIVATION,
    REM_UNKNOWN,
    WBEM_LOGIN_INTERFACE,
    WBEM_SERVICES_INTERFACE,
    ActivationResult,
    DcomStatusError,
    activation_request,
    decode_activation,
    decode_release,
    decode_server_alive2,
    decode_namespace_login,
    namespace_login_request,
    normalize_namespace,
    release_request,
)
from remoteops.remoting.wmi_query import (
    ENUM_INTERFACE, decode_next, decode_query, next_request, os_query_request,
)
from remoteops.remoting.rpc_probe import (
    RpcProbeError,
    RpcProtocolError,
    _remaining,
    build_bind,
    parse_bind_response,
)


class DcomProbeCancelled(RpcProbeError):
    """Cancellation with an explicit remote-reference cleanup outcome."""


@dataclass(frozen=True)
class OsQueryResult:
    activation: ActivationResult
    records: tuple[dict, ...]


def discover_version(address: str, port: int, deadline: float):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    target = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(_remaining(deadline))
        sock.connect(target)
        sock.settimeout(_remaining(deadline))
        sock.sendall(build_bind(rpc.OBJECT_EXPORTER, 0, 0))
        parse_bind_response(rpc._read_packet(sock, deadline, 1))
        request = rpc._header(0, 8, 0, 2) + struct.pack("<IHH", 0, 0, 5)
        sock.settimeout(_remaining(deadline))
        sock.sendall(request)
        response = rpc._read_packet(sock, deadline, 2)
        if struct.unpack_from("<H", response, 10)[0]:
            raise RpcProtocolError("Unexpected security trailer on ServerAlive2 discovery")
        if response[2] == 3 and len(response) >= 32:
            raise DcomStatusError("ServerAlive2 RPC fault", struct.unpack_from("<I", response, 24)[0])
        if response[2] != 2 or len(response) < 24:
            raise RpcProtocolError("Invalid ServerAlive2 response")
        if struct.unpack_from("<H", response, 20)[0] != 0:
            raise RpcProtocolError("ServerAlive2 response context mismatch")
        return decode_server_alive2(response[24:])


def probe_dcom(address: str, username: str, password: str, port: int = 135,
               timeout: float = 30, progress=None, namespace: str | None = None,
               query_os: bool = False):
    ip = rpc.validate_target(address, username, password, port, timeout)
    if query_os and namespace is None:
        namespace = "root/cimv2"
    if namespace is not None:
        namespace = normalize_namespace(namespace)
    if query_os and namespace.casefold() != "root\\cimv2":
        raise ValueError("The fixed OS query requires namespace root/cimv2")
    address = str(ip)
    deadline = time.monotonic() + timeout
    causality = uuid4()
    stage = "discovering DCOM version"
    activation = None
    activation_sent = False
    services = None
    enumerator = None
    records = []
    namespace_uncertain = False
    query_uncertain = False
    result_uncertain = False
    released = False
    cleanup_error = None
    main_error = None
    failed_stage = stage

    def report(text):
        if progress is not None:
            progress(text)

    def report_cleanup(text):
        nonlocal main_error, failed_stage
        try:
            report(text)
        except (Exception, KeyboardInterrupt) as exc:
            # A broken output sink or cancellation must not skip reference release.
            if main_error is None:
                main_error, failed_stage = exc, stage

    try:
        report("Discovering DCOM protocol version (not an authentication check)...")
        version = discover_version(address, port, deadline)
        stage = "activating IWbemLevel1Login"
        report("Authenticating and activating the WMI login object...")
        with rpc.ntlm_connection(address, username, password, ACTIVATION, port, deadline) as channel:
            request = activation_request(version, causality)
            activation_sent = True
            activation = decode_activation(channel.call(0, request, deadline))
        report("WMI login interface obtained; namespace access has not been tested.")
        if namespace is not None:
            stage = f"accessing WMI namespace {namespace}"
            activation.bindings.require_ntlm()
            exporter_port = activation.bindings.tcp_port()
            report(f"Authenticating to the WMI exporter on port {exporter_port}...")
            with rpc.ntlm_connection(
                address, username, password, WBEM_LOGIN_INTERFACE, exporter_port, deadline
            ) as channel:
                request = namespace_login_request(activation.version, causality, namespace)
                report(f"Checking WMI namespace {namespace}...")
                namespace_uncertain = True
                reply = channel.call(6, request, deadline, activation.reference.ipid)
                try:
                    services = decode_namespace_login(reply)
                except DcomStatusError:
                    # A valid failed NTLMLogin response guarantees a null output reference.
                    namespace_uncertain = False
                    raise
                namespace_uncertain = False
            if services.oxid != activation.oxid:
                namespace_uncertain = True
                raise RpcProtocolError(
                    "Namespace interface belongs to a different exporter; "
                    "cross-exporter resolution is not supported by this diagnostic"
                )
            report(f"WMI namespace {namespace} accepted; IWbemServices obtained. No query was run.")
        if query_os:
            stage = "executing the fixed read-only OS query"
            report("Executing the fixed Win32_OperatingSystem SELECT...")
            with rpc.ntlm_connection(
                address, username, password, WBEM_SERVICES_INTERFACE, exporter_port, deadline
            ) as channel:
                request = os_query_request(activation.version, causality)
                query_uncertain = True
                reply = channel.call(20, request, deadline, services.ipid)
                try:
                    enumerator = decode_query(reply)
                except DcomStatusError:
                    query_uncertain = False
                    raise
                query_uncertain = False
            if enumerator.oxid != activation.oxid:
                query_uncertain = True
                raise RpcProtocolError(
                    "Query enumerator belongs to a different exporter; "
                    "cross-exporter resolution is not supported by this diagnostic"
                )
            stage = "enumerating and decoding OS query results"
            report("OS query enumerator obtained; reading structured results...")
            with rpc.ntlm_connection(
                address, username, password, ENUM_INTERFACE, exporter_port, deadline
            ) as channel:
                # Win32_OperatingSystem is a singleton. A second object is an error,
                # not an excuse to enumerate arbitrary amounts of provider data.
                for _ in range(2):
                    timeout_ms = max(1, min(300000, int(_remaining(deadline) * 1000)))
                    request = next_request(activation.version, causality, timeout_ms)
                    result_uncertain = True
                    reply = channel.call(4, request, deadline, enumerator.ipid)
                    try:
                        batch, status = decode_next(reply)
                    except DcomStatusError:
                        result_uncertain = False
                        raise
                    result_uncertain = False
                    records.extend(batch)
                    if len(records) > 1:
                        raise RpcProtocolError("OS query returned more than one instance")
                    if status == 0x40004:
                        raise RpcProbeError("WMI enumeration timed out; result set is incomplete")
                    if status == 1:
                        break
                else:
                    raise RpcProtocolError("OS query enumeration did not reach its end")
            if len(records) != 1:
                raise RpcProbeError("OS query returned no operating-system instance")
            report("OS query decoded; releasing remote references before reporting success...")
    except (Exception, KeyboardInterrupt) as exc:
        main_error = exc
        failed_stage = stage
    finally:
        if activation is not None:
            # Cleanup gets a separate bounded allowance even after cancellation/deadline.
            # Never retry release: a lost reply can mean the references were already released.
            try:
                stage = "releasing acquired WMI interfaces"
                activation.bindings.require_ntlm()
                exporter_port = activation.bindings.tcp_port()
                report_cleanup(
                    f"Connecting to the same host's RPC exporter on port {exporter_port}..."
                )
                cleanup_deadline = time.monotonic() + 5
                with rpc.ntlm_connection(
                    address, username, password, REM_UNKNOWN, exporter_port, cleanup_deadline
                ) as channel:
                    report_cleanup("Releasing the WMI login interface...")
                    references = (activation.reference,)
                    if services is not None and services.oxid == activation.oxid:
                        references += (services,)
                    if enumerator is not None and enumerator.oxid == activation.oxid:
                        references += (enumerator,)
                    reply = channel.call(5, release_request(activation, causality, references),
                                         cleanup_deadline, activation.remote_unknown)
                    decode_release(reply)
                    released = True
            except (Exception, KeyboardInterrupt) as exc:
                cleanup_error = exc

    if main_error is not None or cleanup_error is not None:
        exc = main_error if main_error is not None else cleanup_error
        if main_error is not None:
            stage = failed_stage
        if isinstance(exc, KeyboardInterrupt):
            message = "DCOM probe cancelled"
        elif isinstance(exc, TimeoutError):
            message = f"DCOM probe timed out while {stage}"
        elif isinstance(exc, OSError):
            message = f"Network failure while {stage} (OS code {exc.errno})"
        else:
            message = f"DCOM probe failed while {stage}: {exc}"
        if cleanup_error is not None:
            message += f"; remote reference release could not be confirmed: {cleanup_error}"
        elif released:
            message += "; remote reference release confirmed"
        elif activation_sent:
            message += "; activation may have reached the server; remote reference cleanup unconfirmed"
        if namespace_uncertain:
            message += "; namespace reference cleanup unconfirmed"
        if query_uncertain:
            message += "; query enumerator cleanup unconfirmed"
        if result_uncertain:
            message += "; result object cleanup unconfirmed"
        error_type = DcomProbeCancelled if isinstance(exc, KeyboardInterrupt) else RpcProbeError
        raise error_type(message) from None
    if not released:
        raise RpcProbeError("DCOM probe did not confirm remote reference release")
    report("WMI references released; all probe connections closed.")
    return OsQueryResult(activation, tuple(records)) if query_os else activation


def _print_status(message, *, file=None):
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", file=file, flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Activate WMI over DCOM/NTLM, optionally check namespace access or run "
                    "one fixed read-only OS query, then release. Password is prompted securely."
    )
    parser.add_argument("address", help="Approved Windows host's numeric IP")
    parser.add_argument("--username", required=True)
    parser.add_argument("--port", type=int, default=135)
    parser.add_argument("--namespace", type=normalize_namespace,
                        help="Also check namespace access, for example root/cimv2")
    parser.add_argument("--query-os", action="store_true",
                        help="Query fixed OS properties in root/cimv2 and print JSON after cleanup")
    parser.add_argument("--timeout", type=float, default=30,
                        help="Total operation budget; cleanup gets up to 5 extra seconds")
    args = parser.parse_args(argv)
    if args.query_os and args.namespace not in (None, "root\\cimv2"):
        if args.namespace.casefold() != "root\\cimv2":
            parser.error("--query-os requires namespace root/cimv2")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Windows password: ")
        try:
            options = {"progress": _print_status, "namespace": args.namespace}
            if args.query_os:
                options["query_os"] = True
            result = probe_dcom(args.address, args.username, password, args.port, args.timeout,
                                **options)
        finally:
            password = None
    except ValueError as exc:
        parser.error(str(exc))
    except (getpass.GetPassWarning, EOFError):
        _print_status("A terminal with hidden password input is required.", file=sys.stderr)
        return 2
    except DcomProbeCancelled as exc:
        _print_status(str(exc), file=sys.stderr)
        return 130
    except RpcProbeError as exc:
        _print_status(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        _print_status("DCOM probe cancelled before activation.", file=sys.stderr)
        return 130
    activation = result.activation if args.query_os else result
    _print_status(
        f"DCOM activation and release verified "
        f"(version {activation.version[0]}.{activation.version[1]})."
    )
    if args.query_os:
        print(json.dumps(list(result.records), ensure_ascii=True, indent=2))
        _print_status("Read-only OS query and reference release verified.")
        _print_status("Persistent sessions and GUI integration have NOT been tested.")
    elif args.namespace is not None:
        _print_status(f"WMI namespace access verified: {args.namespace}. References released.")
        _print_status("Queries, commands, and persistent sessions have NOT been tested.")
    else:
        _print_status("WMI namespace access, commands, and persistent sessions have NOT been tested.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
