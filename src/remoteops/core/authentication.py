"""Credential validation shared by the connection backends."""
from dataclasses import dataclass
import ipaddress
import re
from spnego import NTLMHash

from remoteops.core.models import AuthMethod
from remoteops.core.network import proxychains_active, resolve_tcp_address


def secret_kind(method):
    return {AuthMethod.NTLM_HASH: "nt_hash", AuthMethod.KERBEROS_RC4: "rc4",
            AuthMethod.KERBEROS_AES: "aes"}.get(method, "password")


KERBEROS_METHODS = {AuthMethod.KERBEROS, AuthMethod.KERBEROS_RC4, AuthMethod.KERBEROS_AES}


def portable_kerberos(profile):
    return profile is not None and profile.method in KERBEROS_METHODS and (
        bool(profile.domain_controller) or profile.method != AuthMethod.KERBEROS)


def validate_secret(method, secret):
    if not isinstance(secret, str) or not secret:
        raise ValueError("Enter a credential secret.")
    if secret_kind(method) != "password":
        sizes = (32, 64) if method == AuthMethod.KERBEROS_AES else (32,)
        if len(secret) not in sizes or not re.fullmatch(r"[0-9a-fA-F]+", secret):
            raise ValueError("AES keys need 32 or 64 hexadecimal characters." if method == AuthMethod.KERBEROS_AES
                             else "NT hashes and RC4 keys need exactly 32 hexadecimal characters (NT hash only).")
        return secret.lower()
    return secret


def ntlm_identity(username, secret, method):
    if method == AuthMethod.NTLM_HASH:
        return PrivateNtlmHash(username=username, nt_hash=validate_secret(method, secret))
    return username


class PrivateNtlmHash(NTLMHash):
    def __repr__(self):
        return f"NTLMHash(username={self.username!r})"

    __str__ = __repr__


def hostname(value):
    value = value.strip().rstrip(".").lower()
    if len(value) > 253 or not value or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in value.split(".")
    ):
        raise ValueError("Enter a DNS hostname without a scheme, port, or path.")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ValueError("Kerberos needs the computer's DNS hostname, not its IP address.")


@dataclass(frozen=True)
class DomainConnection:
    """Per-connection DNS and KDC settings; never changes system resolution."""
    domain: str
    controller: str
    dns_server: str = ""

    def __post_init__(self):
        object.__setattr__(self, "domain", hostname(self.domain))
        for name in ("controller", "dns_server"):
            value = getattr(self, name).strip()
            if value or name == "controller":
                try:
                    ip = ipaddress.ip_address(value)
                except ValueError:
                    raise ValueError("Enter the domain controller IP." if name == "controller" else "Enter a DNS server IP.") from None
                if getattr(ip, "scope_id", None):
                    raise ValueError("Scoped IP addresses are not supported.")
                object.__setattr__(self, name, str(ip))

    def target(self, address, service_hostname="", *, resolver=None):
        try:
            ip = str(ipaddress.ip_address(address))
        except ValueError:
            ip = None
        target_name = hostname(service_hostname or (address if ip is None else ""))
        if "." not in target_name:
            target_name += "." + self.domain
        if ip is None:
            if proxychains_active():
                ip = str(resolve_tcp_address(target_name, allow_hostname=True))
            else:
                if resolver is None:
                    from dns.resolver import Resolver
                    resolver = Resolver(configure=False)
                    resolver.nameservers = [self.dns_server or self.controller]
                from dns.resolver import NoAnswer, NXDOMAIN
                query_name = hostname(address)
                if "." not in query_name:
                    query_name += "." + self.domain
                for record_type in ("A", "AAAA"):
                    try:
                        records = resolver.resolve(query_name,
                                                   record_type, lifetime=5, search=False)
                        ip = str(ipaddress.ip_address(str(records[0])))
                        break
                    except (NoAnswer, NXDOMAIN):
                        continue
            if ip is None:
                raise ValueError("No address found for the configured host.")
        return target_name, ip


def kerberos_user(profile, username):
    domain = profile.domain.upper()
    if "\\" in username:
        _, username = username.split("\\", 1)
    elif "@" in username:
        username, realm = username.rsplit("@", 1)
        if realm.casefold() != domain.casefold():
            raise ValueError("The username realm must match the configured domain.")
    if not username.strip() or not domain:
        raise ValueError("Kerberos needs a username and domain.")
    return username, domain


