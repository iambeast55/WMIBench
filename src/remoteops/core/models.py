from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


def new_id() -> str:
    return str(uuid4())


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


class Transport(StrEnum):
    HTTP = "http"
    HTTPS = "https"


class ConnectionMode(StrEnum):
    WINRM = "winrm"
    WMI = "wmi_dcom"


class AuthMethod(StrEnum):
    KERBEROS = "kerberos"
    KERBEROS_RC4 = "kerberos_rc4"
    KERBEROS_AES = "kerberos_aes"
    NTLM = "ntlm"
    NTLM_HASH = "ntlm_hash"
    BASIC = "basic"


class SessionState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTING = "disconnecting"
    RUNNING = "running"
    ERROR = "error"


@dataclass(slots=True)
class Host:
    name: str
    address: str
    id: str = field(default_factory=new_id)
    port: int = 5985
    transport: Transport = Transport.HTTP
    auth_profile_id: str | None = None
    group_name: str | None = None
    tags: list[str] = field(default_factory=list)
    notes: str = ""
    connection_mode: ConnectionMode = ConnectionMode.WINRM
    wmi_namespace: str = "root/cimv2"
    keepalive_seconds: int = 60
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    kerberos_hostname: str = ""

    @property
    def endpoint(self) -> str:
        if self.connection_mode is ConnectionMode.WMI:
            return f"wmi://{self.address}/{self.wmi_namespace}"
        return f"{self.transport.value}://{self.address}:{self.port}/wsman"


@dataclass(slots=True)
class CredentialProfile:
    name: str
    username: str
    method: AuthMethod = AuthMethod.KERBEROS
    id: str = field(default_factory=new_id)
    prompt_for_secret: bool = True
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    domain: str = ""
    domain_controller: str = ""
    dns_server: str = ""


@dataclass(slots=True)
class CommandTemplate:
    name: str
    script: str = ""
    id: str = field(default_factory=new_id)
    alias: str = ""
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    wmi: dict[str, Any] | None = None
    category: str = ""
    favorite: bool = False
    plugin: Any = None
    module_path: str = ""


@dataclass(slots=True)
class CommandResult:
    host_id: str
    host_name: str
    command: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    started_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None
    command_line: str = ""
    connection_mode: str = "winrm"
    records: list[dict[str, Any]] = field(default_factory=list)
    history_id: int | None = None

    @property
    def succeeded(self) -> bool:
        return self.exit_code in (0, None) and not self.stderr

    @property
    def duration_seconds(self) -> float | None:
        if self.completed_at is None:
            return None
        return (self.completed_at - self.started_at).total_seconds()
