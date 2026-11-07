import os
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from remoteops.core.credentials import CredentialStore, ResolvedCredential
from remoteops.core.database import Repository
from remoteops.core.models import AuthMethod, CredentialProfile, Host


class SavedCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repository = Repository(Path(self.temp.name) / "test.sqlite3")
        self.store = CredentialStore(self.repository)
        self.store._keyring = Mock()
        self.store._keyring.get_password.return_value = None
        self.profile = CredentialProfile("Lab", "LAB\\user", method=AuthMethod.NTLM, prompt_for_secret=False)
        self.secret = "fixture-password-not-for-a-real-host"

    def test_database_secret_is_encrypted_and_survives_new_store(self):
        self.store.save_profile(self.profile, self.secret)
        data = self.repository.get_encrypted_secret(self.profile.id)
        self.assertNotIn(self.secret.encode(), data)
        self.assertNotIn(self.secret.encode(), self.repository.db_path.read_bytes())
        self.assertNotIn(self.secret, repr(ResolvedCredential("LAB\\user", self.secret)))
        reopened = CredentialStore(Repository(self.repository.db_path))
        reopened._keyring = None
        self.assertEqual(reopened.resolve(self.profile).secret, self.secret)
        self.store._keyring.get_password.assert_not_called()

    def test_edit_preserves_id_creation_and_password_unless_replaced(self):
        self.store.save_profile(self.profile, self.secret)
        original = self.repository.get_encrypted_secret(self.profile.id)
        edited = deepcopy(self.profile)
        edited.name = "Renamed"
        edited.username = "LAB\\newuser"
        self.store.save_profile(edited)
        persisted = self.repository.get_credential_profile(self.profile.id)
        self.assertEqual(persisted.name, "Renamed")
        self.assertEqual(persisted.created_at, self.profile.created_at)
        self.assertEqual(self.repository.get_encrypted_secret(self.profile.id), original)
        self.assertEqual(self.store.resolve(persisted).secret, self.secret)
        self.store.save_profile(edited, "replacement-fixture")
        self.assertEqual(self.store.resolve(edited).secret, "replacement-fixture")

    def test_forget_password_removes_database_secret_and_requires_new_value(self):
        self.store.save_profile(self.profile, self.secret)
        prompt = deepcopy(self.profile)
        prompt.prompt_for_secret = True
        self.store.save_profile(prompt)
        self.assertIsNone(self.repository.get_encrypted_secret(prompt.id))
        self.assertIsNone(self.store.resolve(prompt).secret)
        with self.assertRaisesRegex(ValueError, "Enter a password"):
            self.store.save_profile(self.profile)
        self.store._keyring.get_password.assert_not_called()

    def test_saved_secret_is_used_if_prompt_flag_is_stale(self):
        self.store.save_profile(self.profile, self.secret)
        stale = deepcopy(self.profile)
        stale.prompt_for_secret = True
        self.repository.save_credential_profile(stale)
        self.assertEqual(self.store.resolve(stale).secret, self.secret)

    def test_failed_encryption_does_not_commit_profile_changes(self):
        self.store.save_profile(self.profile, self.secret)
        edited = deepcopy(self.profile)
        edited.name = "Should not persist"
        with patch.object(self.store, "_encrypt", side_effect=RuntimeError("Unavailable")):
            with self.assertRaises(RuntimeError):
                self.store.save_profile(edited, "new-value")
        self.assertEqual(self.repository.get_credential_profile(self.profile.id).name, "Lab")
        self.assertEqual(self.store.resolve(self.profile).secret, self.secret)

    def test_database_failure_rolls_back_profile_and_secret_together(self):
        self.store.save_profile(self.profile, self.secret)
        with self.repository.connect() as conn:
            conn.execute("create trigger reject_secret before update on credential_secrets begin select raise(ABORT, 'fixture'); end")
        edited = deepcopy(self.profile)
        edited.name = "Not committed"
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_profile(edited, "new-value")
        self.assertEqual(self.repository.get_credential_profile(self.profile.id).name, "Lab")
        self.assertEqual(self.store.resolve(self.profile).secret, self.secret)

    def test_delete_clears_assignments_and_only_selected_credential(self):
        self.store.save_profile(self.profile, self.secret)
        host = self.repository.save_host(Host("host", "192.0.2.1", auth_profile_id=self.profile.id))
        other = self.repository.save_credential_profile(CredentialProfile("Other", "other"))
        self.repository.delete_credential_profile(self.profile.id)
        self.assertIsNone(self.repository.get_credential_profile(self.profile.id))
        self.assertIsNone(self.repository.get_encrypted_secret(self.profile.id))
        self.assertIsNone(self.repository.get_host(host.id).auth_profile_id)
        self.assertIsNotNone(self.repository.get_credential_profile(other.id))

    def test_tamper_and_profile_swap_fail_without_keychain_fallback(self):
        self.store.save_profile(self.profile, self.secret)
        original = self.repository.get_encrypted_secret(self.profile.id)
        other = CredentialProfile("Other", "other", prompt_for_secret=False)
        self.repository.save_credential_profile(other, encrypted_secret=original)
        with self.assertRaisesRegex(RuntimeError, "could not be decrypted"):
            self.store.resolve(other)
        self.repository.save_credential_profile(self.profile, encrypted_secret=b"invalid ciphertext")
        with self.assertRaisesRegex(RuntimeError, "could not be decrypted"):
            self.store.resolve(self.profile)
        self.store._keyring.get_password.assert_not_called()

    def test_missing_key_is_not_silently_replaced(self):
        self.store.save_profile(self.profile, self.secret)
        backup = self.store.key_path.with_suffix(".backup")
        self.store.key_path.rename(backup)
        with self.assertRaises(RuntimeError):
            self.store.resolve(self.profile)
        with self.assertRaisesRegex(RuntimeError, "key is missing"):
            self.store.save_profile(self.profile, "new-value")
        self.assertFalse(self.store.key_path.exists())
        backup.rename(self.store.key_path)
        self.assertEqual(self.store.resolve(self.profile).secret, self.secret)

    def test_legacy_keychain_migrates_only_after_successful_lookup(self):
        self.repository.save_credential_profile(self.profile)
        self.store._keyring.get_password.return_value = self.secret
        self.assertEqual(self.store.resolve(self.profile).secret, self.secret)
        self.assertIsNotNone(self.repository.get_encrypted_secret(self.profile.id))
        self.store._keyring.get_password.side_effect = RuntimeError("keychain offline")
        self.assertEqual(self.store.resolve(self.profile).secret, self.secret)

    @unittest.skipIf(os.name == "nt", "POSIX permissions; Windows key uses DPAPI instead")
    def test_posix_key_permissions_are_private_and_checked(self):
        self.store.save_profile(self.profile, self.secret)
        self.assertEqual(self.store.key_path.stat().st_mode & 0o777, 0o600)
        self.store.key_path.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, "permissions 0600"):
            self.store.resolve(self.profile)
