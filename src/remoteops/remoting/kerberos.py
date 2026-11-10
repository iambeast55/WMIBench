"""Explicit KDC connections and per-session Kerberos message protection."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import struct
import sys
from datetime import datetime, timezone, timedelta

from Cryptodome.Cipher import ARC4
from impacket.krb5 import crypto, gssapi
from impacket.spnego import GSSAPI, SPNEGO_NegTokenInit, SPNEGO_NegTokenResp, TypesMech
from minikerberos.aioclient import AIOKerberosClient
from minikerberos.common.creds import KerberosCredential
from minikerberos.common.spn import KerberosSPN
from minikerberos.common.target import KerberosTarget
from minikerberos.protocol.asn1_structs import AP_REQ, AP_REP, Authenticator, EncAPRepPart, Ticket
from minikerberos.protocol.encryption import InvalidChecksum
from minikerberos.protocol.errors import KerberosError
from minikerberos.protocol.ticketutils import construct_apreq_from_ticket

from remoteops.core.authentication import DomainConnection, kerberos_user, validate_secret
from remoteops.core.models import AuthMethod


class KerberosFailure(ValueError):
    pass


def _failure(exc):
    if isinstance(exc, KerberosFailure):
        return exc
    if isinstance(exc, KerberosError):
        return KerberosFailure(f"Kerberos rejected the connection: {exc.errorcode.name}.")
    if isinstance(exc, TimeoutError):
        return KerberosFailure("The domain controller did not respond before the timeout.")
    return KerberosFailure(f"Kerberos failed ({type(exc).__name__}). Check the domain, DC, hostname, and credential.")


class TicketProvider:
    def __init__(self, host, profile, credential):
        self.domain = DomainConnection(profile.domain, profile.domain_controller, profile.dns_server)
        self.username, self.realm = kerberos_user(profile, credential.username if credential else profile.username)
        self.secret = validate_secret(profile.method, credential.secret if credential else None)
        self.method = profile.method
        self.hostname, self.address = self.domain.target(host.address, host.kerberos_hostname)
        self.tickets = {}

    def close(self):
        self.tickets.clear()
        self.secret = None

    def ticket(self, service, timeout=30):
        cached = self.tickets.get(service)
        if cached and cached[0] > datetime.now(timezone.utc) + timedelta(seconds=30):
            return cached[1:]
        try:
            result = asyncio.run(asyncio.wait_for(self._fetch(service), timeout))
        except Exception as exc:
            raise _failure(exc) from None
        self.tickets[service] = result
        return result[1:]

    async def _fetch(self, service):
        if self.secret is None:
            raise KerberosFailure("The Kerberos connection is closed.")
        cred = KerberosCredential()
        cred.username, cred.domain = self.username, self.realm
        if self.method == AuthMethod.KERBEROS_AES:
            setattr(cred, "kerberos_key_aes_256" if len(self.secret) == 64 else "kerberos_key_aes_128", self.secret)
            etypes = [18] if len(self.secret) == 64 else [17]
        elif self.method == AuthMethod.KERBEROS_RC4:
            cred.kerberos_key_rc4 = self.secret
            etypes = [23]
        else:
            cred.password = self.secret
            etypes = [18, 17, 23]
        target = KerberosTarget(self.domain.controller, timeout=10)
        client = AIOKerberosClient(cred, target)
        try:
            await client.get_TGT(override_etype=etypes)
        except InvalidChecksum:
            if self.method != AuthMethod.KERBEROS:
                raise
            # Password-based AES AS-REP decryption is ambiguous for accounts
            # without pre-authentication. Request an RC4 TGT instead; the
            # password is converted to the account's NT key by minikerberos.
            fallback = KerberosCredential()
            fallback.username, fallback.domain = self.username, self.realm
            fallback.password = self.secret
            client = AIOKerberosClient(fallback, target)
            try:
                await client.get_TGT(override_etype=[23])
            except InvalidChecksum:
                raise KerberosFailure("Kerberos TGT decryption failed for the supplied password.") from None
        spn = KerberosSPN.from_spn(f"{service}/{self.hostname}@{self.realm}")
        try:
            tgs, part, key = await client.get_TGS(
                spn, is_linux=sys.platform.startswith("linux")
            )
        except InvalidChecksum:
            raise KerberosFailure(
                f"Kerberos service ticket decryption failed for {service}/{self.hostname}."
            ) from None
        requested = [service.casefold(), self.hostname.casefold()]
        returned = [value.casefold() for value in tgs["ticket"]["sname"]["name-string"]]
        if returned != requested or key.enctype not in (17, 18, 23):
            raise KerberosFailure("The KDC returned a different service identity or unsupported session key.")
        return part["endtime"], Ticket(tgs["ticket"]).dump(), crypto.Key(key.enctype, key.contents)


def gss_token(payload):
    token = GSSAPI()
    token["UUID"] = TypesMech["KRB5 - Kerberos 5"]
    token["Payload"] = payload
    return token.getData()


def response_token(token):
    if not token or len(token) > 1024 * 1024:
        raise KerberosFailure("The server omitted its Kerberos authentication reply.")
    if token[0] == 0xA1:
        fields = SPNEGO_NegTokenResp(token).fields
        if fields.get("NegState") in (b"\x02", b"\x03"):
            raise KerberosFailure("The server rejected Kerberos negotiation.")
        mech = fields.get("SupportedMech")
        if mech is not None and mech not in (TypesMech["KRB5 - Kerberos 5"], TypesMech["MS KRB5 - Microsoft Kerberos 5"]):
            raise KerberosFailure("The server selected a different authentication method.")
        token = fields.get("ResponseToken", b"")
    if token[:1] == b"\x60":
        wrapper = GSSAPI(token)
        if wrapper["OID"] != TypesMech["KRB5 - Kerberos 5"] or wrapper["Payload"][:2] != b"\x02\x00":
            raise KerberosFailure("The server returned an unexpected Kerberos token.")
        token = wrapper["Payload"][2:]
    return token


class KerberosContext:
    def __init__(self, provider, service, *, dce=False, channel_binding=None, negotiate=True, timeout=30):
        self.provider = provider
        self.service = service
        self.dce = dce
        self.channel_binding = channel_binding
        self.negotiate = negotiate
        self.timeout = timeout
        self.complete = False
        self.key = None
        self.send_sequence = 0
        self.receive_sequence = 0
        self.timestamp = None

    def step(self, token=None):
        try:
            return self._step(token)
        except Exception as exc:
            self.complete = False
            raise _failure(exc) from None

    def _step(self, token):
        if self.key is None:
            if token:
                raise KerberosFailure("Unexpected initial Kerberos token.")
            ticket, self.key = self.provider.ticket(self.service, self.timeout)
            flags = 2 | 4 | 8 | 16 | 32 | (4096 if self.dce else 0)
            request = construct_apreq_from_ticket(ticket, self.key, self.provider.realm,
                        self.provider.username, flags=flags, seq_number=0,
                        ap_opts=["mutual-required"], cb_data=self.channel_binding)
            encrypted = AP_REQ.load(request).native["authenticator"]["cipher"]
            auth = Authenticator.load(crypto._enctype_table[self.key.enctype].decrypt(self.key, 11, encrypted)).native
            self.timestamp = auth["ctime"], auth["cusec"]
            request = gss_token(b"\x01\x00" + request)
            if self.negotiate:
                blob = SPNEGO_NegTokenInit()
                blob["MechTypes"] = [TypesMech["MS KRB5 - Microsoft Kerberos 5"]]
                blob["MechToken"] = request
                return blob.getData()
            return request
        if self.complete:
            raise KerberosFailure("Unexpected extra Kerberos authentication exchange.")
        reply = AP_REP.load(response_token(token), strict=True).native
        if reply["pvno"] != 5 or reply["msg-type"] != 15 or reply["enc-part"]["etype"] != self.key.enctype:
            raise KerberosFailure("Invalid Kerberos mutual authentication reply.")
        cipher = crypto._enctype_table[self.key.enctype]
        part = EncAPRepPart.load(cipher.decrypt(self.key, 12, reply["enc-part"]["cipher"]), strict=True).native
        if (part["ctime"], part["cusec"]) != self.timestamp:
            raise KerberosFailure("Kerberos mutual authentication timestamp mismatch.")
        subkey = part.get("subkey")
        if not subkey or subkey["keytype"] not in (17, 18, 23):
            raise KerberosFailure("The server did not supply a supported Kerberos session subkey.")
        result = None
        self.receive_sequence = part.get("seq-number") or 0
        if self.dce:
            # The third DCE leg proves receipt of the server's sequence number.
            now = datetime.now(timezone.utc)
            confirmation = EncAPRepPart({"ctime": now.replace(microsecond=0), "cusec": now.microsecond,
                                         "seq-number": self.receive_sequence}).dump()
            reply["enc-part"]["cipher"] = cipher.encrypt(self.key, 12, confirmation, None)
            blob = SPNEGO_NegTokenResp()
            blob["ResponseToken"] = AP_REP(reply).dump()
            result = blob.getData()
        self.key = crypto.Key(subkey["keytype"], subkey["keyvalue"])
        self.complete = True
        return result

    def wrap(self, data, *, rpc=False):
        if not self.complete:
            raise KerberosFailure("Kerberos authentication is incomplete.")
        provider = gssapi.GSSAPI(crypto._enctype_table[self.key.enctype])
        if self.key.enctype == 23 and not rpc:
            encrypted, header = provider.GSS_Wrap_LDAP(self.key, data, self.send_sequence)
        else:
            encrypted, header = provider.GSS_Wrap(self.key, data, self.send_sequence)
        self.send_sequence += 1
        return header, encrypted

    def unwrap(self, header, data, *, rpc=False):
        if not self.complete:
            raise KerberosFailure("Kerberos authentication is incomplete.")
        try:
            plaintext = unwrap_message(self.key, header, data, self.receive_sequence, rpc=rpc)
            self.receive_sequence += 1
            return plaintext
        except Exception:
            self.complete = False
            raise KerberosFailure("Kerberos message integrity or sequence check failed. Reconnect.") from None

    def wrap_winrm(self, data):
        header, encrypted = self.wrap(data)
        return header, encrypted, 0

    def unwrap_winrm(self, header, data):
        return self.unwrap(header, data)


def unwrap_message(key, header, data, sequence, *, rpc=False):
    """Check direction, sequence, and integrity before returning decrypted bytes."""
    if key.enctype in (17, 18):
        if len(header) < 16:
            raise ValueError("Truncated GSS token")
        kind, flags, filler, extra, rotation, received = struct.unpack(">HBBHHQ", header[:16])
        if (kind, flags, filler, received) != (0x0504, 7, 255, sequence) or rotation != 28:
            raise ValueError("Unexpected GSS token")
        encrypted = header[16:] + data
        if not encrypted or extra > len(encrypted):
            raise ValueError("Invalid GSS padding")
        shift = (rotation + extra) % len(encrypted)
        encrypted = encrypted[shift:] + encrypted[:shift]
        plaintext = crypto._enctype_table[key.enctype].decrypt(key, 22, encrypted)
        expected = header[:6] + b"\x00\x00" + header[8:16]
        if len(plaintext) < extra + 16 or not hmac.compare_digest(plaintext[-16:], expected):
            raise ValueError("GSS header mismatch")
        return plaintext[:-(extra + 16)]
    if key.enctype != 23:
        raise ValueError("Unsupported GSS key")
    # RPC carries a fixed mechanism header; HTTP uses a DER length for the full token.
    if rpc:
        if header[:13] != bytes.fromhex("602b06092a864886f712010202") or len(header) != 45:
            raise ValueError("Invalid RC4 GSS header")
        token = header[13:]
    else:
        wrapper = GSSAPI(header + data)
        if wrapper["OID"] != TypesMech["KRB5 - Kerberos 5"]:
            raise ValueError("Invalid RC4 GSS mechanism")
        token, data = wrapper["Payload"][:32], wrapper["Payload"][32:]
    if len(token) != 32 or token[:8] != bytes.fromhex("020111001000ffff"):
        raise ValueError("Invalid RC4 GSS token")
    digest = lambda key, value: hmac.new(key, value, hashlib.md5).digest()
    sequence_key = digest(digest(key.contents, bytes(4)), token[16:24])
    received = ARC4.new(sequence_key).decrypt(token[8:16])
    if not hmac.compare_digest(received, struct.pack(">I", sequence) + b"\xff" * 4):
        raise ValueError("GSS sequence or direction mismatch")
    local_key = bytes(value ^ 0xF0 for value in key.contents)
    encryption_key = digest(digest(local_key, bytes(4)), received[:4])
    plaintext = ARC4.new(encryption_key).decrypt(token[24:32] + data)
    checksum = hashlib.md5(struct.pack("<I", 13) + token[:8] + plaintext).digest()
    expected = digest(digest(key.contents, b"signaturekey\0"), checksum)[:8]
    if not hmac.compare_digest(expected, token[16:24]):
        raise ValueError("GSS checksum mismatch")
    plaintext = plaintext[8:]
    if not rpc:
        padding = plaintext[-1] if plaintext else 0
        if not 1 <= padding <= 8 or plaintext[-padding:] != bytes([padding]) * padding:
            raise ValueError("Invalid GSS padding")
        plaintext = plaintext[:-padding]
    return plaintext
