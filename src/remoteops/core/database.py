from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from remoteops.core.models import AuthMethod, CommandResult, ConnectionMode, CredentialProfile, Host, Transport, utc_now

_UNCHANGED = object()

def _dt(value: datetime) -> str:
    return value.isoformat()


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Repository:
    """Host inventory, credentials, and command history."""

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

                create table if not exists credential_secrets (
                    profile_id text primary key,
                    ciphertext blob not null
                );
                create table if not exists host_groups (
                    name text primary key collate nocase,
                    position integer not null default 0
                );
                """
            )

            migrations = {
                "hosts": {"connection_mode": "text not null default 'winrm'",
                          "wmi_namespace": "text not null default 'root/cimv2'",
                          "keepalive_seconds": "integer not null default 60",
                          "sort_order": "integer not null default 0",
                          "kerberos_hostname": "text not null default ''"},
                "credential_profiles": {"domain": "text not null default ''",
                                        "domain_controller": "text not null default ''",
                                        "dns_server": "text not null default ''"},
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
            conn.execute("insert or ignore into host_groups(name) select distinct group_name from hosts "
                         "where group_name is not null and trim(group_name) != ''")
            conn.execute("update hosts set group_name=null where trim(group_name)='' ")
            conn.execute("update hosts set group_name=(select name from host_groups where name=hosts.group_name collate nocase) "
                         "where group_name is not null")

    def save_host(self, host: Host) -> Host:
        host.updated_at = utc_now()
        with self.connect() as conn:
            previous = conn.execute("select group_name from hosts where id=?", (host.id,)).fetchone()
            host.group_name = self._ensure_group(conn, host.group_name) if host.group_name else None
            append = previous is None or previous["group_name"] != host.group_name
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
            if append:
                conn.execute("update hosts set sort_order=(select coalesce(max(sort_order), -1)+1 from hosts "
                             "where group_name is ? and id != ?) where id=?", (host.group_name, host.id, host.id))
            conn.execute("update hosts set kerberos_hostname=? where id=?", (host.kerberos_hostname, host.id))
        return host

    def list_hosts(self) -> list[Host]:
        with self.connect() as conn:
            rows = conn.execute("select * from hosts order by sort_order, name collate nocase").fetchall()
        return [self._host_from_row(row) for row in rows]

    def get_host(self, host_id: str) -> Host | None:
        with self.connect() as conn:
            row = conn.execute("select * from hosts where id = ?", (host_id,)).fetchone()
        return self._host_from_row(row) if row else None

    def delete_host(self, host_id: str) -> None:
        with self.connect() as conn:
            conn.execute("delete from hosts where id = ?", (host_id,))

    @staticmethod
    def _group_name(name):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80 or any(ord(c) < 32 for c in name):
            raise ValueError("Group names must contain 1-80 characters without line breaks.")
        return name.strip()

    def _ensure_group(self, conn, name):
        name = self._group_name(name)
        row = conn.execute("select name from host_groups where name=?", (name,)).fetchone()
        if row:
            return row["name"]
        conn.execute("insert into host_groups(name, position) values (?, (select coalesce(max(position),-1)+1 from host_groups))", (name,))
        return name

    def list_host_groups(self):
        with self.connect() as conn:
            return [row["name"] for row in conn.execute("select name from host_groups order by position, name collate nocase")]

    def create_host_group(self, name):
        name = self._group_name(name)
        with self.connect() as conn:
            if conn.execute("select 1 from host_groups where name=?", (name,)).fetchone():
                raise ValueError("A group with that name already exists.")
            return self._ensure_group(conn, name)

    def rename_host_group(self, old, new):
        new = self._group_name(new)
        with self.connect() as conn:
            group = conn.execute("select name from host_groups where name=?", (old,)).fetchone()
            if group is None:
                raise ValueError("Group no longer exists.")
            if old.casefold() != new.casefold() and conn.execute("select 1 from host_groups where name=?", (new,)).fetchone():
                raise ValueError("A group with that name already exists.")
            conn.execute("update host_groups set name=? where name=?", (new, old))
            conn.execute("update hosts set group_name=?, updated_at=? where group_name=? collate nocase",
                         (new, _dt(utc_now()), old))

    def delete_host_group(self, name):
        with self.connect() as conn:
            offset = conn.execute("select coalesce(max(sort_order),-1)+1 from hosts where group_name is null").fetchone()[0]
            rows = conn.execute("select id from hosts where group_name=? collate nocase order by sort_order, name collate nocase", (name,)).fetchall()
            for index, row in enumerate(rows):
                conn.execute("update hosts set group_name=null, sort_order=?, updated_at=? where id=?",
                             (offset + index, _dt(utc_now()), row["id"]))
            conn.execute("delete from host_groups where name=?", (name,))

    def save_inventory_order(self, groups):
        """Commit a complete tree snapshot, or reject it if inventory changed."""
        with self.connect() as conn:
            expected_groups = {row[0] for row in conn.execute("select name from host_groups")}
            expected_hosts = {row[0] for row in conn.execute("select id from hosts")}
            names = [name for name, _ in groups]
            ids = [host_id for _, hosts in groups for host_id in hosts]
            if (len(names) != len(set(names)) or set(names) != expected_groups | {None}
                    or len(ids) != len(set(ids)) or set(ids) != expected_hosts):
                raise ValueError("Host inventory changed. Refresh and try again.")
            for position, (name, hosts) in enumerate(groups):
                if name is not None:
                    conn.execute("update host_groups set position=? where name=?", (position, name))
                for order, host_id in enumerate(hosts):
                    conn.execute("update hosts set group_name=?, sort_order=?, updated_at=? where id=?",
                                 (name, order, _dt(utc_now()), host_id))

    def save_credential_profile(self, profile: CredentialProfile, *, encrypted_secret=_UNCHANGED) -> CredentialProfile:
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
            conn.execute("update credential_profiles set domain=?, domain_controller=?, dns_server=? where id=?",
                         (profile.domain, profile.domain_controller, profile.dns_server, profile.id))
            if encrypted_secret is None:
                conn.execute("delete from credential_secrets where profile_id = ?", (profile.id,))
            elif encrypted_secret is not _UNCHANGED:
                conn.execute(
                    "insert into credential_secrets(profile_id, ciphertext) values (?, ?) "
                    "on conflict(profile_id) do update set ciphertext=excluded.ciphertext",
                    (profile.id, encrypted_secret),
                )
        return profile

    def get_encrypted_secret(self, profile_id: str) -> bytes | None:
        with self.connect() as conn:
            row = conn.execute("select ciphertext from credential_secrets where profile_id = ?", (profile_id,)).fetchone()
        return bytes(row[0]) if row else None

    def has_saved_secrets(self) -> bool:
        with self.connect() as conn:
            return conn.execute("select 1 from credential_secrets limit 1").fetchone() is not None

    def delete_credential_profile(self, profile_id: str) -> None:
        with self.connect() as conn:
            conn.execute("update hosts set auth_profile_id = null, updated_at = ? where auth_profile_id = ?",
                         (_dt(utc_now()), profile_id))
            conn.execute("delete from credential_secrets where profile_id = ?", (profile_id,))
            conn.execute("delete from credential_profiles where id = ?", (profile_id,))

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
            kerberos_hostname=row["kerberos_hostname"],
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
            domain=row["domain"],
            domain_controller=row["domain_controller"],
            dns_server=row["dns_server"],
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )
