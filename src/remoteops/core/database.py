from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from remoteops.core.models import AuthMethod, CommandResult, ConnectionMode, CredentialProfile, Host, Transport, utc_now


def _dt(value: datetime) -> str:
    return value.isoformat()


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Repository:
    """Small SQLite repository for the desktop MVP."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                create table if not exists hosts (
                    id text primary key,
                    name text not null,
                    address text not null,
                    port integer not null,
                    transport text not null,
                    auth_profile_id text,
                    group_name text,
                    tags_json text not null,
                    notes text not null,
                    created_at text not null,
                    updated_at text not null
                );

                create table if not exists credential_profiles (
                    id text primary key,
                    name text not null,
                    username text not null,
                    method text not null,
                    prompt_for_secret integer not null,
                    created_at text not null,
                    updated_at text not null
                );

                create table if not exists command_history (
                    id integer primary key autoincrement,
                    host_id text not null,
                    host_name text not null,
                    command text not null,
                    stdout text not null,
                    stderr text not null,
                    exit_code integer,
                    started_at text not null,
                    completed_at text
                );
                """
            )

            migrations = {
                "hosts": {"connection_mode": "text not null default 'winrm'",
                          "wmi_namespace": "text not null default 'root/cimv2'",
                          "keepalive_seconds": "integer not null default 60"},
                "command_history": {"command_line": "text not null default ''",
                                    "connection_mode": "text not null default 'winrm'",
                                    "records_json": "text not null default '[]'"},
            }
            for table, columns in migrations.items():
                existing = {row["name"] for row in conn.execute(f"pragma table_info({table})")}
                for name, definition in columns.items():
                    if name not in existing:
                        conn.execute(f"alter table {table} add column {name} {definition}")
            conn.execute("create index if not exists history_host_id on command_history(host_id, id)")

    def save_host(self, host: Host) -> Host:
        host.updated_at = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                insert into hosts (
                    id, name, address, port, transport, auth_profile_id, group_name, tags_json,
                    notes, created_at, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(id) do update set
                    name = excluded.name,
                    address = excluded.address,
                    port = excluded.port,
                    transport = excluded.transport,
                    auth_profile_id = excluded.auth_profile_id,
                    group_name = excluded.group_name,
                    tags_json = excluded.tags_json,
                    notes = excluded.notes,
                    updated_at = excluded.updated_at
                """,
                (
                    host.id,
                    host.name,
                    host.address,
                    host.port,
                    host.transport.value,
                    host.auth_profile_id,
                    host.group_name,
                    json.dumps(host.tags),
                    host.notes,
                    _dt(host.created_at),
                    _dt(host.updated_at),
                ),
            )
            conn.execute(
                "update hosts set connection_mode=?, wmi_namespace=?, keepalive_seconds=? where id=?",
                (host.connection_mode.value, host.wmi_namespace, host.keepalive_seconds, host.id),
            )
        return host

    def list_hosts(self) -> list[Host]:
        with self.connect() as conn:
            rows = conn.execute("select * from hosts order by name collate nocase").fetchall()
        return [self._host_from_row(row) for row in rows]

    def get_host(self, host_id: str) -> Host | None:
        with self.connect() as conn:
            row = conn.execute("select * from hosts where id = ?", (host_id,)).fetchone()
        return self._host_from_row(row) if row else None

    def delete_host(self, host_id: str) -> None:
        with self.connect() as conn:
            conn.execute("delete from hosts where id = ?", (host_id,))

    def save_credential_profile(self, profile: CredentialProfile) -> CredentialProfile:
        profile.updated_at = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                insert into credential_profiles (
                    id, name, username, method, prompt_for_secret, created_at, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?)
                on conflict(id) do update set
                    name = excluded.name,
                    username = excluded.username,
                    method = excluded.method,
                    prompt_for_secret = excluded.prompt_for_secret,
                    updated_at = excluded.updated_at
                """,
                (
                    profile.id,
                    profile.name,
                    profile.username,
                    profile.method.value,
                    int(profile.prompt_for_secret),
                    _dt(profile.created_at),
                    _dt(profile.updated_at),
                ),
            )
        return profile

    def list_credential_profiles(self) -> list[CredentialProfile]:
        with self.connect() as conn:
            rows = conn.execute(
                "select * from credential_profiles order by name collate nocase"
            ).fetchall()
        return [self._profile_from_row(row) for row in rows]

    def get_credential_profile(self, profile_id: str | None) -> CredentialProfile | None:
        if not profile_id:
            return None
        with self.connect() as conn:
            row = conn.execute(
                "select * from credential_profiles where id = ?", (profile_id,)
            ).fetchone()
        return self._profile_from_row(row) if row else None

    def save_command_result(self, result: CommandResult) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into command_history (
                    host_id, host_name, command, stdout, stderr, exit_code, started_at, completed_at,
                    command_line, connection_mode, records_json
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.host_id,
                    result.host_name,
                    result.command,
                    result.stdout,
                    result.stderr,
                    result.exit_code,
                    _dt(result.started_at),
                    _dt(result.completed_at) if result.completed_at else None,
                    result.command_line,
                    result.connection_mode,
                    json.dumps(result.records, default=str),
                ),
            )
            result.history_id = conn.execute("select last_insert_rowid()").fetchone()[0]

    def save_command_results(self, results: Iterable[CommandResult]) -> None:
        for result in results:
            self.save_command_result(result)

    def list_command_history(
        self, limit: int = 200, *, host_id: str | None = None, search: str = "",
        outcome: str = "all", since: str = "", offset: int = 0,
    ) -> list[CommandResult]:
        clauses, params = [], []
        if host_id:
            clauses.append("host_id = ?")
            params.append(host_id)
        if search:
            clauses.append("(instr(lower(command_line || char(10) || command || char(10) || stdout || char(10) || stderr || char(10) || records_json), lower(?)) > 0)")
            params.append(search)
        if outcome == "success":
            clauses.append("coalesce(exit_code, 0) = 0 and stderr = ''")
        elif outcome == "failed":
            clauses.append("(coalesce(exit_code, 0) != 0 or stderr != '')")
        if since:
            clauses.append("started_at >= ?")
            params.append(since)
        where = " where " + " and ".join(clauses) if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                "select * from command_history" + where + " order by id desc limit ? offset ?",
                (*params, limit, offset),
            ).fetchall()
        return [
            CommandResult(
                host_id=row["host_id"],
                host_name=row["host_name"],
                command=row["command"],
                stdout=row["stdout"],
                stderr=row["stderr"],
                exit_code=row["exit_code"],
                started_at=_parse_dt(row["started_at"]),
                completed_at=_parse_dt(row["completed_at"]) if row["completed_at"] else None,
                command_line=row["command_line"],
                connection_mode=row["connection_mode"],
                records=json.loads(row["records_json"]),
                history_id=row["id"],
            )
            for row in rows
        ]

    def _host_from_row(self, row: sqlite3.Row) -> Host:
        return Host(
            id=row["id"],
            name=row["name"],
            address=row["address"],
            port=row["port"],
            transport=Transport(row["transport"]),
            auth_profile_id=row["auth_profile_id"],
            group_name=row["group_name"],
            tags=json.loads(row["tags_json"]),
            notes=row["notes"],
            connection_mode=ConnectionMode(row["connection_mode"]),
            wmi_namespace=row["wmi_namespace"],
            keepalive_seconds=row["keepalive_seconds"],
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )

    def _profile_from_row(self, row: sqlite3.Row) -> CredentialProfile:
        return CredentialProfile(
            id=row["id"],
            name=row["name"],
            username=row["username"],
            method=AuthMethod(row["method"]),
            prompt_for_secret=bool(row["prompt_for_secret"]),
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )
