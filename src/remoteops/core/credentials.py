from __future__ import annotations

from dataclasses import dataclass

from remoteops.core.models import CredentialProfile


SERVICE_NAME = "RemoteOps"


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    username: str
    secret: str | None


class CredentialStore:
    """Credential lookup backed by the OS keychain when keyring is installed."""

    def __init__(self) -> None:
        try:
            import keyring  # type: ignore[import-not-found]
        except ImportError:
            keyring = None
        self._keyring = keyring

    def get_secret(self, profile: CredentialProfile) -> str | None:
        if self._keyring is None or profile.prompt_for_secret:
            return None
        return self._keyring.get_password(SERVICE_NAME, profile.id)

    def set_secret(self, profile: CredentialProfile, secret: str) -> None:
        if self._keyring is None:
            raise RuntimeError("keyring is not installed; cannot store credential secrets")
        self._keyring.set_password(SERVICE_NAME, profile.id, secret)

    def resolve(self, profile: CredentialProfile) -> ResolvedCredential:
        return ResolvedCredential(username=profile.username, secret=self.get_secret(profile))

