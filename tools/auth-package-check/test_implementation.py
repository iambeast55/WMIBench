"""Runs inside the frozen executable through --plugin-cli test."""
import unittest


class AuthenticationPackageTests(unittest.TestCase):
    def test_crypto_backends_and_ticket_credentials(self):
        from impacket.krb5 import crypto
        from minikerberos.common.creds import KerberosCredential
        from minikerberos.protocol.encryption import _enctype_table, Key
        from minikerberos.protocol.constants import EncryptionType
        from remoteops.remoting.kerberos import KerberosContext
        from remoteops.remoting.kerberos_rpc import KerberosRpcConnection
        from remoteops.remoting.kerberos_http import MappedAdapter
        from remoteops.core.authentication import DomainConnection
        self.assertTrue(KerberosContext and KerberosRpcConnection and MappedAdapter)
        self.assertEqual(DomainConnection("example.com", "192.0.2.1").target("192.0.2.2", "server"),
                         ("server.example.com", "192.0.2.2"))
        for enctype, size in ((17, 16), (18, 32), (23, 16)):
            key = crypto.Key(enctype, bytes(range(size)))
            data = b"offline encryption fixture"
            ciphertext = crypto._enctype_table[enctype].encrypt(key, 11, data, None)
            self.assertEqual(_enctype_table[enctype].decrypt(Key(enctype, key.contents), 11, ciphertext), data)
        credential = KerberosCredential()
        credential.username = "fixture"
        credential.domain = "EXAMPLE.COM"
        credential.password = "offline fixture password"
        self.assertEqual(len(credential.get_key_for_enctype(EncryptionType.ARCFOUR_HMAC_MD5)), 16)
