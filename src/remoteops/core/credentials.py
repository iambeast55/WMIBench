from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from remoteops.core.models import CredentialProfile
from remoteops.core.authentication import secret_kind, validate_secret


SERVICE_NAME = "RemoteOps"


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    username: str
    secret: str | None = field(repr=False)


class CredentialStore:
    """Encrypted SQLite secrets; legacy keychain entries migrate on successful lookup."""

    def __init__(self, repository=None) -> None:
        self.repository = repository
        self.key_path = Path(repository.db_path).with_suffix(".key") if repository else None
        try:
            import keyring  # type: ignore[import-not-found]
        except ImportError:
            keyring = None
        self._keyring = keyring

    def _cipher(self, *, create=False):
        path = self.key_path
        if path is None:
            raise RuntimeError("A database is required to save credentials.")
        try:
            if create and not path.exists():
                if self.repository.has_saved_secrets():
                    raise RuntimeError("The local credential key is missing. Restore it before saving passwords; existing encrypted credentials have not been changed.")
                key = Fernet.generate_key()
                if os.name == "nt":
                    import win32crypt
                    key = win32crypt.CryptProtectData(key, "RemoteOps credentials", None, None, None, 1)
                # Exclusive creation never replaces a key used by another app instance.
                try:
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    pass
                else:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(key)
                        stream.flush()
                        os.fsync(stream.fileno())
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError("Credential key must be a regular private file.")
            if os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
                raise RuntimeError("Credential key must belong to this user with permissions 0600.")
            key = path.read_bytes()
            if os.name == "nt":
                import win32crypt
                key = win32crypt.CryptUnprotectData(key, None, None, None, 1)[1]
            return Fernet(key)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("Cannot unlock saved credentials. Restore the original local key and database for this user account.") from exc

    def _encrypt(self, profile, secret):
        # Bind ciphertext to its profile so swapping database rows cannot swap passwords.
        payload = json.dumps({"profile": profile.id, "secret": secret}).encode("utf-8")
        return self._cipher(create=True).encrypt(payload)

    def get_secret(self, profile: CredentialProfile) -> str | None:
        # Use an encrypted value when one exists, even if an older profile has
        # a stale prompt flag. Profiles with no saved value still prompt.
        if profile.prompt_for_secret and (
            self.repository is None or self.repository.get_encrypted_secret(profile.id) is None
        ):
            return None
        data = self.repository.get_encrypted_secret(profile.id) if self.repository else None
        if data is not None:
            try:
                payload = json.loads(self._cipher().decrypt(data))
                if payload["profile"] != profile.id or not isinstance(payload["secret"], str):
                    raise ValueError("Credential identity mismatch")
                return payload["secret"]
            except (InvalidToken, ValueError, KeyError, TypeError) as exc:
                raise RuntimeError("Saved credential could not be decrypted. Edit it and replace the password.") from exc
        if self._keyring is None:
            return None
        try:
            secret = self._keyring.get_password(SERVICE_NAME, profile.id)
        except Exception as exc:
            raise RuntimeError("The legacy keychain is unavailable. Edit this credential and save its password in the database.") from exc
        if secret and self.repository:
            self.repository.save_credential_profile(profile, encrypted_secret=self._encrypt(profile, secret))
        return secret

    def set_secret(self, profile: CredentialProfile, secret: str) -> None:
        secret = validate_secret(profile.method, secret)
        if self.repository:
            self.repository.save_credential_profile(profile, encrypted_secret=self._encrypt(profile, secret))
        elif self._keyring is not None:
            self._keyring.set_password(SERVICE_NAME, profile.id, secret)
        else:
            raise RuntimeError("A database is required to save credentials.")

    def save_profile(self, profile: CredentialProfile, secret: str | None = None):
        if self.repository is None:
            raise RuntimeError("A database is required to save credentials.")
        if profile.prompt_for_secret:
            return self.repository.save_credential_profile(profile, encrypted_secret=None)
        if secret:
            secret = validate_secret(profile.method, secret)
            return self.repository.save_credential_profile(profile, encrypted_secret=self._encrypt(profile, secret))
        old = self.repository.get_credential_profile(profile.id)
        if old is not None and secret_kind(old.method) != secret_kind(profile.method):
            raise ValueError("Enter a new secret when changing the credential type.")
        if self.repository.get_encrypted_secret(profile.id) is None:
            old = self.repository.get_credential_profile(profile.id)
            if old is not None and not old.prompt_for_secret and self.get_secret(old):
                return self.repository.save_credential_profile(profile)
            raise ValueError("Enter a password to save, or turn off Save password.")
        return self.repository.save_credential_profile(profile)

    def resolve(self, profile: CredentialProfile) -> ResolvedCredential:
        return ResolvedCredential(username=profile.username, secret=self.get_secret(profile))

