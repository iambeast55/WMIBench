"""Bounded, one-shot read-only batches. Cancellation never interrupts an RPC call."""
from __future__ import annotations

import csv
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
from dataclasses import dataclass, field
from threading import Lock

from remoteops.commands.templates import PreparedCommand
from remoteops.core.credentials import ResolvedCredential
from remoteops.core.models import CommandResult, CredentialProfile, Host, SessionState, utc_now
from remoteops.remoting.output import display_value


@dataclass
class BatchTarget:
    host: Host
    command: PreparedCommand | None
    profile: CredentialProfile | None = None
    credential: ResolvedCredential | None = field(default=None, repr=False)
    allow_connect: bool = False
    skip_reason: str = ""


@dataclass(frozen=True)
class BatchUpdate:
    host_id: str
    status: str
    result: CommandResult | None = None
    detail: str = ""
    session_state: SessionState | None = None


class BatchRunner:
    def __init__(self, manager, targets, concurrency=4):
        if type(concurrency) is not int or not 1 <= concurrency <= 12:
            raise ValueError("Concurrency must be between 1 and 12")
        if not targets or len({t.host.id for t in targets}) != len(targets):
            raise ValueError("Select distinct hosts for the batch")
        self.manager = manager
        self.targets = deepcopy(list(targets))
        self.concurrency = concurrency
        self._lock = Lock()
        self._cancelled = False
        self._started = False

    def cancel(self):
        with self._lock:
            self._cancelled = True

    def run(self, notify):
        with self._lock:
            if self._started:
                raise RuntimeError("A batch cannot be replayed; create a new batch")
            self._started = True
        updates = {}

        def publish(update):
            updates[update.host_id] = update
            notify(update)

        queued = deque()
        try:
            for target in self.targets:
                command = target.command
                reason = target.skip_reason
                if not reason and (command is None or command.help_text
                                   or target.host.connection_mode.value not in command.read_only_modes):
                    reason = "This command is not verified read-only for this backend."
                if reason:
                    publish(BatchUpdate(target.host.id, "Skipped", detail=reason))
                else:
                    queued.append(target)
                    publish(BatchUpdate(target.host.id, "Queued"))

            with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="remoteops-batch") as pool:
                active = {}
                while queued or active:
                    while queued and len(active) < self.concurrency:
                        with self._lock:
                            if self._cancelled:
                                break
                            target = queued.popleft()
                            future = pool.submit(self._run_one, target)
                            active[future] = target
                        publish(BatchUpdate(target.host.id, "Running"))
                    with self._lock:
                        cancelled = self._cancelled
                    if cancelled:
                        while queued:
                            publish(BatchUpdate(queued.popleft().host.id, "Skipped", detail="Cancelled before dispatch."))
                    if active:
                        done, _ = wait(active, timeout=0.1, return_when=FIRST_COMPLETED)
                        for future in done:
                            active.pop(future)
                            publish(future.result())
            return [updates[t.host.id] for t in self.targets]
        finally:
            for target in self.targets:
                target.credential = None

    def _run_one(self, target):
        started = utc_now()
        try:
            result = self.manager.run_prepared_read_only(
                target.host, target.command, target.profile, target.credential,
                allow_connect=target.allow_connect,
            )
        except Exception as exc:
            result = CommandResult(
                target.host.id, target.host.name, target.command.script,
                command_line=target.command.command_line, connection_mode=target.host.connection_mode.value,
                stderr=f"{type(exc).__name__}: {exc}", exit_code=1,
                started_at=started, completed_at=utc_now(),
            )
        return BatchUpdate(target.host.id, "Succeeded" if result.succeeded else "Failed",
                           result, result.stderr, self.manager.session_state(target.host, target.profile))


def batch_records(hosts, updates):
    """Namespaced provider columns cannot overwrite host identity or outcome."""
    for host in hosts:
        update = updates.get(host.id)
        if update is None:
            continue
        result = update.result
        identity = {"Host": host.name, "Backend": host.connection_mode.value, "Status": update.status}
        metadata = {"Address": host.address, "Host ID": host.id,
                    "Seconds": result.duration_seconds if result else None, "Error": update.detail}
        if result and result.records:
            for row in result.records:
                yield {**identity, **{f"Result.{key}": value for key, value in row.items()}, **metadata}
        else:
            yield {**identity, "Output": result.stdout if result else "", **metadata}


def export_batch_csv(path, hosts, updates):
    columns = list(dict.fromkeys(key for row in batch_records(hosts, updates) for key in row))

    def cell(value):
        text = display_value(value)
        return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text

    with open(path, "w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow([cell(key) for key in columns])
        for row in batch_records(hosts, updates):
            writer.writerow([cell(row.get(key)) for key in columns])
