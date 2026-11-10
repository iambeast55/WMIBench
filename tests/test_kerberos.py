import asyncio
import base64
import struct
import sys
import time
import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from impacket.krb5 import crypto, gssapi
from impacket.spnego import GSSAPI, SPNEGO_NegTokenResp, TypesMech
from minikerberos.protocol.asn1_structs import AP_REQ, AP_REP, Authenticator, EncAPRepPart, Ticket
from minikerberos.protocol.encryption import InvalidChecksum

from remoteops.core.credentials import ResolvedCredential
from remoteops.core.models import AuthMethod, CredentialProfile, Host
from remoteops.remoting.kerberos import KerberosContext, KerberosFailure, TicketProvider, gss_token, unwrap_message
from remoteops.remoting.kerberos_http import MappedAdapter, KerberosHttpAuth
from remoteops.remoting import kerberos_rpc
from test_rpc_auth_probe import ACK_BODY


def provider_fixture(enctype=18):
    key = crypto.Key(enctype, bytes(range(32 if enctype == 18 else 16)))
    ticket = Ticket({"tkt-vno": 5, "realm": "EXAMPLE.COM",
        "sname": {"name-type": 2, "name-string": ["host", "server.example.com"]},
        "enc-part": {"etype": enctype, "cipher": b"fixture ticket"}}).dump()
    return SimpleNamespace(username="user", realm="EXAMPLE.COM", hostname="server.example.com",
                           address="192.0.2.10", ticket=Mock(return_value=(ticket, key)))


def mutual_reply(context, *, sequence=0, wrong_time=False):
    key = crypto.Key(context.key.enctype, bytes([77]) * len(context.key.contents))
    ctime, cusec = context.timestamp
    part = {"ctime": ctime, "cusec": (cusec + 1) % 1000000 if wrong_time else cusec,
            "subkey": {"keytype": key.enctype, "keyvalue": key.contents}, "seq-number": sequence}
    encrypted = crypto._enctype_table[context.key.enctype].encrypt(context.key, 12, EncAPRepPart(part).dump(), None)
    response = AP_REP({"pvno": 5, "msg-type": 15, "enc-part": {"etype": context.key.enctype, "cipher": encrypted}}).dump()
    blob = SPNEGO_NegTokenResp()
    blob["NegState"] = b"\x01" if context.dce else b"\x00"
    blob["SupportedMech"] = TypesMech["MS KRB5 - Microsoft Kerberos 5"]
    blob["ResponseToken"] = response if context.dce else gss_token(b"\x02\x00" + response)
    return blob.getData(), key


def server_message(key, data, sequence=0, rpc=False):
    if key.enctype == 23:
        wrap = gssapi.GSSAPI(crypto._enctype_table[23])
        encrypted, header = (wrap.GSS_Wrap(key, data, sequence, direction="accept") if rpc else
                              wrap.GSS_Wrap_LDAP(key, data, sequence, direction="accept"))
        return header, encrypted
    extra = -len(data) % 16
    header = struct.pack(">HBBHHQ", 0x0504, 7, 255, extra, 0, sequence)
    encrypted = crypto._enctype_table[key.enctype].encrypt(key, 22, data + b"\xff" * extra + header, None)
    rotation = 28 + extra
    encrypted = encrypted[-rotation:] + encrypted[:-rotation]
    header = header[:6] + b"\x00\x1c" + header[8:]
    split = 44 + extra
    return header + encrypted[:split], encrypted[split:]


class KerberosTests(unittest.TestCase):
    def test_mutual_exchange_and_channel_binding_for_all_key_types(self):
        for enctype in (17, 18, 23):
            context = KerberosContext(provider_fixture(enctype), "HTTP", channel_binding=b"tls-server-end-point:fixture", negotiate=False)
            token = context.step()
            request = AP_REQ.load(GSSAPI(token)["Payload"][2:]).native
            plaintext = crypto._enctype_table[enctype].decrypt(context.key, 11, request["authenticator"]["cipher"])
            auth = Authenticator.load(plaintext).native
            self.assertNotEqual(auth["cksum"]["checksum"][4:20], bytes(16))
            response, key = mutual_reply(context, sequence=12)
            self.assertIsNone(context.step(response))
            self.assertTrue(context.complete)
            header, data = server_message(key, b"test message", 12)
            self.assertEqual(context.unwrap_winrm(header, data), b"test message")
            with self.assertRaises(KerberosFailure):
                context.unwrap_winrm(header, data)
            self.assertFalse(context.complete)

    def test_mutual_auth_mismatch_and_missing_reply_fail_closed(self):
        for wrong in (True, False):
            context = KerberosContext(provider_fixture(), "HTTP")
            context.step()
            response, _ = mutual_reply(context, wrong_time=wrong)
            with self.assertRaises(KerberosFailure):
                context.step(response if wrong else None)
            self.assertFalse(context.complete)

    def test_modified_ciphertext_header_sequence_and_padding_are_rejected(self):
        for enctype in (17, 18, 23):
            key = provider_fixture(enctype).ticket()[1]
            for rpc in (False, True):
                message = bytes(range(32))
                header, data = server_message(key, message, 5, rpc=rpc)
                self.assertEqual(unwrap_message(key, header, data, 5, rpc=rpc), message)
                for bad_header, bad_data, sequence in ((header[:-1] + bytes([header[-1] ^ 1]), data, 5),
                        (header, bytes([data[0] ^ 1]) + data[1:], 5), (header, data, 6)):
                    with self.assertRaises(Exception):
                        unwrap_message(key, bad_header, bad_data, sequence, rpc=rpc)

    def test_kdc_uses_selected_secret_type_dc_and_service(self):
        for method, secret, expected in ((AuthMethod.KERBEROS, "fixture-password", [18, 17, 23]),
                (AuthMethod.KERBEROS_RC4, "a" * 32, [23]),
                (AuthMethod.KERBEROS_AES, "b" * 32, [17]), (AuthMethod.KERBEROS_AES, "c" * 64, [18])):
            profile = CredentialProfile("test", "EXAMPLE\\user", method=method, domain="example.com", domain_controller="192.0.2.1")
            host = Host("server", "192.0.2.10", kerberos_hostname="server.example.com")
            provider = TicketProvider(host, profile, ResolvedCredential(profile.username, secret))
            key = provider_fixture().ticket()[1]
            ticket = Ticket.load(provider_fixture().ticket()[0]).native
            ticket["sname"]["name-string"][0] = "HTTP"
            client = Mock()
            client.get_TGT = AsyncMock()
            client.get_TGS = AsyncMock(return_value=({"ticket": ticket}, {"endtime": datetime.now(timezone.utc) + timedelta(hours=1)}, key))
            with patch("remoteops.remoting.kerberos.AIOKerberosClient", return_value=client) as factory:
                provider.ticket("HTTP")
                provider.ticket("HTTP")
                client.get_TGT.assert_awaited_once_with(override_etype=expected)
                self.assertEqual(factory.call_args.args[1].ip, "192.0.2.1")
                self.assertEqual(client.get_TGS.call_args.args[0].service, "HTTP")
                self.assertEqual(client.get_TGS.call_args.args[0].username, "server.example.com")
                self.assertEqual(client.get_TGS.call_args.kwargs,
                                 {"is_linux": sys.platform.startswith("linux")})
            provider.close()
            self.assertIsNone(provider.secret)
            self.assertFalse(provider.tickets)

    def test_password_auth_retries_no_preauth_asrep_with_empty_aes_salt(self):
        profile = CredentialProfile("test", "EXAMPLE\\user", method=AuthMethod.KERBEROS,
                                    domain="example.com", domain_controller="192.0.2.1")
        host = Host("server", "192.0.2.10", kerberos_hostname="server.example.com")
        provider = TicketProvider(host, profile, ResolvedCredential(profile.username, "fixture-password"))
        key = provider_fixture().ticket()[1]
        ticket = Ticket.load(provider_fixture().ticket()[0]).native
        ticket["sname"]["name-string"][0] = "HTTP"
        first = Mock()
        first.get_TGT = AsyncMock(side_effect=InvalidChecksum("ciphertext integrity failure"))
        second = Mock()
        second.get_TGT = AsyncMock()
        second.get_TGS = AsyncMock(return_value=(
            {"ticket": ticket},
            {"endtime": datetime.now(timezone.utc) + timedelta(hours=1)},
            key,
        ))
        with patch("remoteops.remoting.kerberos.AIOKerberosClient", side_effect=[first, second]) as factory:
            provider.ticket("HTTP")
        self.assertEqual(factory.call_count, 2)
        fallback_credential = factory.call_args_list[1].args[0]
        self.assertTrue(fallback_credential.kerberos_key_aes_256)
        second.get_TGT.assert_awaited_once_with(override_etype=[18, 17, 23])

    def test_mapping_preserves_tls_name_and_does_not_change_shared_pool_classes(self):
        from urllib3.poolmanager import pool_classes_by_scheme
        original = dict(pool_classes_by_scheme)
        adapter = MappedAdapter("server.example.com", "192.0.2.10")
        pool = adapter.poolmanager.connection_from_host("server.example.com", port=5986, scheme="https")
        connection = pool._new_conn()
        with patch("remoteops.remoting.kerberos_http.create_connection", return_value="socket") as connect:
            self.assertEqual(connection._new_conn(), "socket")
            self.assertEqual(connect.call_args.args[0], ("192.0.2.10", 5986))
            self.assertEqual(connection.host, "server.example.com")
        self.assertEqual(pool_classes_by_scheme, original)
        from pypsrp._pool_manager import DisableNewConnectionsContext, NewConnectionDisabled
        with DisableNewConnectionsContext(), self.assertRaises(NewConnectionDisabled):
            connection._new_conn()
        adapter.close()

    def test_rpc_privacy_round_trip_and_unprotected_reply_rejection(self):
        context = KerberosContext(provider_fixture(), "host", dce=True)
        context.step()
        reply, key = mutual_reply(context)
        self.assertTrue(context.step(reply))
        payload = b"fixture response"
        header, encrypted = server_message(key, payload, rpc=True)
        call = struct.pack("<IHBB", len(payload), 0, 0, 0)
        body = call + encrypted + kerberos_rpc.trailer(0) + header
        response = kerberos_rpc.wire._header(2, len(body), len(header), 2) + body
        sock = Mock()
        binding = kerberos_rpc.build_bind(kerberos_rpc.wire.OBJECT_EXPORTER, 0, 0)
        connection = kerberos_rpc.KerberosRpcConnection(sock, context, binding, 65535)
        with patch.object(kerberos_rpc.wire, "_read_packet", return_value=response):
            self.assertEqual(connection.call(3, b"", time.monotonic() + 10), payload)
        request = sock.sendall.call_args.args[0]
        self.assertEqual(request[2], 0)
        self.assertNotIn(b"fixture response", request)
        unsigned = kerberos_rpc.wire._header(2, 8, 0, 3) + bytes(8)
        with patch.object(kerberos_rpc.wire, "_read_packet", return_value=unsigned), self.assertRaises(KerberosFailure):
            connection.call(3, b"", time.monotonic() + 10)
        self.assertFalse(connection.active)
        sock.close.assert_called_once()

    def test_rpc_bind_finishes_with_empty_final_token(self):
        provider = provider_fixture()
        context = KerberosContext(provider, "host", dce=True)
        sock = Mock()
        sock.__enter__ = Mock(return_value=sock)
        sock.__exit__ = Mock(return_value=False)
        def read(*args, **kwargs):
            if not context.complete:
                response, _ = mutual_reply(context)
                return kerberos_rpc.token_packet(12, ACK_BODY, response)
            return kerberos_rpc.wire._header(15, len(ACK_BODY), 0, 1) + ACK_BODY
        with patch.object(kerberos_rpc, "KerberosContext", return_value=context), patch.object(kerberos_rpc.socket, "socket", return_value=sock), patch.object(kerberos_rpc.wire, "_read_packet", side_effect=read):
            with kerberos_rpc.connection(provider, kerberos_rpc.wire.OBJECT_EXPORTER, 135, time.monotonic() + 10) as channel:
                self.assertTrue(context.complete)
                self.assertTrue(channel.active)
            self.assertFalse(channel.active)
            self.assertEqual([call.args[0][2] for call in sock.sendall.call_args_list], [11, 14])

    def test_http_handshake_requires_mutual_auth_and_never_repeats_it(self):
        import requests
        provider = provider_fixture()
        context = KerberosContext(provider, "HTTP", channel_binding=b"fixture-cbt")
        auth = KerberosHttpAuth(provider)
        request = requests.Request("POST", "https://server.example.com:5986/wsman", data=b"").prepare()
        response = SimpleNamespace(status_code=401, url=request.url, request=request,
                                   content=b"", raw=Mock(), connection=Mock())
        def send(*args, **kwargs):
            token, _ = mutual_reply(context)
            return SimpleNamespace(status_code=200, headers={"www-authenticate": "Negotiate " + base64.b64encode(token).decode()})
        response.connection.send.side_effect = send
        with patch("remoteops.remoting.kerberos_http.KerberosContext", return_value=context) as factory, patch.object(auth, "_get_cbt_data", return_value=b"fixture-cbt"):
            auth.handle_401(response, _pypsrp_auth_provider="Negotiate")
            self.assertTrue(context.complete)
            self.assertEqual(factory.call_args.args[1], "HTTP")
            self.assertEqual(factory.call_args.kwargs["channel_binding"], b"fixture-cbt")
            with self.assertRaises(KerberosFailure):
                auth.handle_401(response, _pypsrp_auth_provider="Negotiate")
            with self.assertRaises(KerberosFailure):
                KerberosHttpAuth(provider).handle_401(response, _pypsrp_auth_provider="NTLM")
            response.connection.send.assert_called_once()
