"""Network environment helpers."""
from __future__ import annotations

import ipaddress
import os
import socket
import sys


def proxychains_active() -> bool:
    """Return whether the process was launched under proxychains-ng."""
    if sys.platform == "win32":
        return False
    override = os.environ.get("WMIBENCH_PROXYCHAINS", "").strip().casefold()
    if override in {"1", "true", "yes", "on"}:
        return True
    preload = os.environ.get("LD_PRELOAD", "")
    return any("proxychains" in entry.casefold() for entry in preload.split(os.pathsep) if entry)


def resolve_tcp_address(value: str, *, allow_hostname: bool = False):
    """Resolve a TCP target, using intercepted getaddrinfo when enabled."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if not allow_hostname:
            raise ValueError("Use a numeric IPv4 or IPv6 address") from None
        try:
            records = socket.getaddrinfo(value, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError(f"Could not resolve host: {value}") from exc
        address = None
        for _family, _socktype, _proto, _canonname, sockaddr in records:
            try:
                candidate = ipaddress.ip_address(sockaddr[0])
            except (ValueError, IndexError):
                continue
            if getattr(candidate, "scope_id", None):
                continue
            address = candidate
            break
        if address is None:
            raise ValueError(f"Could not resolve host: {value}")
    if getattr(address, "scope_id", None):
        raise ValueError("Scoped IPv6 addresses are not supported")
    return address
