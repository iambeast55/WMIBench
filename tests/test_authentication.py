import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import spnego
from spnego._ntlm_raw.crypto import ntowfv1

from remoteops.core.authentication import DomainConnection, ntlm_identity, validate_secret
from remoteops.core.credentials import CredentialStore, ResolvedCredential
from remoteops.core.database import Repository
from remoteops.core.models import AuthMethod, CredentialProfile, Host
from remoteops.remoting.dcom_session import DcomWmiSession
from remoteops.remoting.rpc_auth_probe import _new_context
from remoteops.remoting.wmi import wmi_session_factory


class AuthenticationTests(unittest.TestCase):
    def test_domain_resolution_does_not_change_system_dns(self):
        config = DomainConnection("EXAMPLE.COM", "192.0.2.1", "192.0.2.2")
        resolver = Mock()
        resolver.resolve.return_value = ["192.0.2.10"]
        with patch("socket.getaddrinfo", side_effect=AssertionError("system DNS called")):
            self.assertEqual(config.target("server", resolver=resolver), ("server.example.com", "192.0.2.10"))
            self.assertEqual(config.target("192.0.2.9", "SERVER", resolver=resolver), ("server.example.com", "192.0.2.9"))
            resolver.resolve.assert_called_once_with("server.example.com", "A", lifetime=5, search=False)
            with self.assertRaises(ValueError):
                config.target("192.0.2.9")
        with patch("dns.resolver.Resolver", return_value=resolver):
            config.target("server")
        self.assertEqual(resolver.nameservers, ["192.0.2.2"])

    def test_invalid_secret_errors_do_not_echo_input(self):
        for method in (AuthMethod.NTLM_HASH, AuthMethod.KERBEROS_RC4, AuthMethod.KERBEROS_AES):
            with self.assertRaises(ValueError) as caught:
                validate_secret(method, "private-invalid-secret")
            self.assertNotIn("private-invalid-secret", str(caught.exception))
        self.assertEqual(validate_secret(AuthMethod.KERBEROS_AES, "A" * 64), "a" * 64)
        secret = ntlm_identity("EXAMPLE\\user", "a" * 32, AuthMethod.NTLM_HASH)
        self.assertNotIn("a" * 32, repr(secret))
        self.assertNotIn("a" * 32, str(secret))

    def test_hash_authentication_and_encryption_use_real_ntlm_exchange(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "accounts"
            path.write_text("EXAMPLE:user:fixture password\n")
            identity = ntlm_identity("EXAMPLE\\user", ntowfv1("fixture password").hex(), AuthMethod.NTLM_HASH)
            with patch.dict(os.environ, {"NTLM_USER_FILE": str(path), "LM_COMPAT_LEVEL": "3"}):
                for client in (_new_context(identity.username, identity, "192.0.2.10"),
                               spnego.client(identity, protocol="ntlm", options=spnego.NegotiateOptions.wrapping_winrm)):
                    server = spnego.server(protocol="ntlm", options=spnego.NegotiateOptions.use_ntlm)
                    server.step(client.step(server.step(client.step())))
                    self.assertTrue(server.complete)
                    data = b"test protected content"
                    self.assertEqual(server.unwrap(client.wrap(data).data).data, data)

    def test_switching_secret_kind_requires_replacement_and_preserves_database(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Repository(Path(temp) / "test.db")
            store = CredentialStore(repo)
            profile = CredentialProfile("account", "user", method=AuthMethod.NTLM, prompt_for_secret=False)
            store.save_profile(profile, "fixture password")
            profile.method = AuthMethod.NTLM_HASH
            with self.assertRaisesRegex(ValueError, "new secret"):
                store.save_profile(profile)
            self.assertEqual(repo.get_credential_profile(profile.id).method, AuthMethod.NTLM)
            store.save_profile(profile, "a" * 32)
            self.assertEqual(store.resolve(profile).secret, "a" * 32)
            self.assertNotIn(b"a" * 32, repo.db_path.read_bytes())

    def test_domain_and_host_identity_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Repository(Path(temp) / "test.db")
            profile = repo.save_credential_profile(CredentialProfile("domain", "user", domain="example.com",
                        domain_controller="192.0.2.1", dns_server="192.0.2.2", method=AuthMethod.KERBEROS_AES))
            host = repo.save_host(Host("server", "192.0.2.10", kerberos_hostname="server.example.com"))
            reopened = Repository(repo.db_path)
            self.assertEqual(reopened.get_credential_profile(profile.id).dns_server, "192.0.2.2")
            self.assertEqual(reopened.get_host(host.id).kerberos_hostname, "server.example.com")
            for platform in ("win32", "linux"):
                with patch("remoteops.remoting.wmi.sys.platform", platform):
                    for method in (AuthMethod.NTLM_HASH, AuthMethod.KERBEROS, AuthMethod.KERBEROS_RC4, AuthMethod.KERBEROS_AES):
                        profile.method = method
                        self.assertIsInstance(wmi_session_factory(host, profile), DcomWmiSession)

    def test_winrm_hash_uses_explicit_hash_without_password_or_secret_repr(self):
        from remoteops.remoting.session import PsrpSession
        profile = CredentialProfile("hash", "EXAMPLE\\user", method=AuthMethod.NTLM_HASH)
        session = PsrpSession(Host("host", "192.0.2.10"), profile)
        with patch("pypsrp.wsman.WSMan") as wsman, patch("pypsrp.powershell.RunspacePool"), patch.object(session, "execute", return_value=([], "")):
            session.connect(ResolvedCredential(profile.username, "a" * 32))
            options = wsman.call_args.kwargs
            self.assertIsInstance(options["username"], spnego.NTLMHash)
            self.assertIsNone(options["password"])
            self.assertEqual(options["auth"], "ntlm")
            self.assertNotIn("a" * 32, repr(wsman.call_args))
            session.disconnect()
