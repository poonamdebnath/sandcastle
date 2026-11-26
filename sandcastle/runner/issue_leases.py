from __future__ import annotations

import json
import os
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import SandcastlePaths, SchedulerPolicy
from .state_store import read_json_file, utc_now_iso, write_json_file

WORKER_ID_ENV_VAR = "SANDCASTLE_WORKER_ID"
LOCK_POLL_INTERVAL_SECONDS = 0.1


class IssueLeaseUnavailable(RuntimeError):
    def __init__(self, payload: dict[str, object]):
        super().__init__(str(payload.get("reason", "issue lease is unavailable")))
        self.payload = payload


@dataclass(frozen=True)
class IssueLeaseContext:
    issue_number: int
    operation: str
    status: str | None
    branch: str | None
    worktree_path: str | None
    pr_number: int | None


class IssueLeaseGuard:
    def __init__(
        self,
        *,
        paths: SandcastlePaths,
        scheduler: SchedulerPolicy,
        lease_context: IssueLeaseContext,
    ) -> None:
        self._paths = paths
        self._scheduler = scheduler
        self._lease_context = lease_context
        self._owner_id = current_worker_id()
        self.payload: dict[str, object] | None = None
        self._stop_event = threading.Event()
        self._refresh_thread: threading.Thread | None = None
        self._enabled = leases_enabled(scheduler)

    def __enter__(self) -> IssueLeaseGuard:
        if not self._enabled:
            return self

        self.payload = acquire_issue_lease(
            self._paths,
            self._scheduler,
            self._lease_context,
            owner_id=self._owner_id,
        )
        self._start_refresh_thread()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._enabled:
            return

        self._stop_event.set()
        if self._refresh_thread is not None:
            self._refresh_thread.join(timeout=1.0)
        release_issue_lease(
            self._paths,
            self._scheduler,
            self._lease_context.issue_number,
            owner_id=self._owner_id,
        )

    def _start_refresh_thread(self) -> None:
        refresh_interval = self._scheduler.lease_refresh_interval_seconds
        if refresh_interval <= 0:
            return
        if refresh_interval >= self._scheduler.lease_timeout_seconds:
            return

        self._refresh_thread = threading.Thread(
            target=self._refresh_loop,
            name=f"sandcastle-lease-{self._lease_context.issue_number}",
            daemon=True,
        )
        self._refresh_thread.start()

    def _refresh_loop(self) -> None:
        while not self._stop_event.wait(self._scheduler.lease_refresh_interval_seconds):
            try:
                refresh_issue_lease(
                    self._paths,
                    self._scheduler,
                    self._lease_context,
                    owner_id=self._owner_id,
                )
            except RuntimeError:
                return


@contextmanager
def issue_lease_guard(
    paths: SandcastlePaths,
    scheduler: SchedulerPolicy,
    *,
    issue_number: int,
    operation: str,
    status: str | None = None,
    branch: str | None = None,
    worktree_path: str | None = None,
    pr_number: int | None = None,
):
    guard = IssueLeaseGuard(
        paths=paths,
        scheduler=scheduler,
        lease_context=IssueLeaseContext(
            issue_number=issue_number,
            operation=operation,
            status=status,
            branch=branch,
            worktree_path=worktree_path,
            pr_number=pr_number,
        ),
    )
    with guard:
        yield guard


def leases_enabled(scheduler: SchedulerPolicy) -> bool:
    return scheduler.mode not in {"serial-merge-first", "serial-active-slot"}


def current_worker_id() -> str:
    explicit = os.environ.get(WORKER_ID_ENV_VAR, "").strip()
    if explicit:
        return explicit
    host = os.environ.get("COMPUTERNAME", "").strip() or socket.gethostname()
    return f"{host}:{os.getpid()}"


def acquire_issue_lease(
    paths: SandcastlePaths,
    scheduler: SchedulerPolicy,
    lease_context: IssueLeaseContext,
    *,
    owner_id: str,
) -> dict[str, object]:
    if not leases_enabled(scheduler):
        return {}

    with _control_lock(
        paths.leases_dir,
        lease_context.issue_number,
        owner_id,
        scheduler.lease_lock_timeout_seconds,
    ):
        lease_path = _lease_path(paths.leases_dir, lease_context.issue_number)
        existing = _read_optional_json(lease_path)
        if (
            existing is not None
            and str(existing.get("owner_id", "")) != owner_id
            and not _lease_is_stale(existing)
        ):
            raise IssueLeaseUnavailable(
                {
                    "issue_number": lease_context.issue_number,
                    "operation": lease_context.operation,
                    "reason": (
                        f"issue {lease_context.issue_number} is currently leased by "
                        f"{existing.get('owner_id')} until {existing.get('expires_at')}"
                    ),
                    "lease": existing,
                }
            )
        payload = _lease_payload(
            lease_context,
            owner_id=owner_id,
            scheduler_mode=scheduler.mode,
            lease_timeout_seconds=scheduler.lease_timeout_seconds,
            previous_acquired_at=_optional_payload_str(
                existing.get("acquired_at") if existing is not None else None
            ),
        )
        write_json_file(lease_path, payload)
        return payload


def refresh_issue_lease(
    paths: SandcastlePaths,
    scheduler: SchedulerPolicy,
    lease_context: IssueLeaseContext,
    *,
    owner_id: str,
) -> dict[str, object]:
    if not leases_enabled(scheduler):
        return {}

    with _control_lock(
        paths.leases_dir,
        lease_context.issue_number,
        owner_id,
        scheduler.lease_lock_timeout_seconds,
    ):
        lease_path = _lease_path(paths.leases_dir, lease_context.issue_number)
        existing = _read_optional_json(lease_path)
        if existing is None:
            raise RuntimeError("issue lease disappeared before refresh")
        if str(existing.get("owner_id", "")) != owner_id:
            raise RuntimeError("issue lease is no longer owned by this worker")

        payload = _lease_payload(
            lease_context,
            owner_id=owner_id,
            scheduler_mode=scheduler.mode,
            lease_timeout_seconds=scheduler.lease_timeout_seconds,
            previous_acquired_at=_optional_payload_str(existing.get("acquired_at")),
        )
        write_json_file(lease_path, payload)
        return payload


def release_issue_lease(
    paths: SandcastlePaths,
    scheduler: SchedulerPolicy,
    issue_number: int,
    *,
    owner_id: str,
) -> bool:
    if not leases_enabled(scheduler):
        return False

    with _control_lock(
        paths.leases_dir,
        issue_number,
        owner_id,
        scheduler.lease_lock_timeout_seconds,
    ):
        lease_path = _lease_path(paths.leases_dir, issue_number)
        existing = _read_optional_json(lease_path)
        if existing is None:
            return False
        if str(existing.get("owner_id", "")) != owner_id:
            return False
        lease_path.unlink(missing_ok=True)
        return True


def active_issue_lease(
    paths: SandcastlePaths, issue_number: int
) -> dict[str, object] | None:
    return _read_optional_json(_lease_path(paths.leases_dir, issue_number))


def _lease_payload(
    lease_context: IssueLeaseContext,
    *,
    owner_id: str,
    scheduler_mode: str,
    lease_timeout_seconds: int,
    previous_acquired_at: str | None,
) -> dict[str, object]:
    refreshed_at = utc_now_iso()
    expires_at = (
        datetime.now(timezone.utc).replace(microsecond=0).timestamp()
        + lease_timeout_seconds
    )
    return {
        "issue_number": lease_context.issue_number,
        "owner_id": owner_id,
        "scheduler_mode": scheduler_mode,
        "operation": lease_context.operation,
        "status": lease_context.status,
        "branch": lease_context.branch,
        "worktree_path": lease_context.worktree_path,
        "pr_number": lease_context.pr_number,
        "acquired_at": previous_acquired_at or refreshed_at,
        "refreshed_at": refreshed_at,
        "expires_at": datetime.fromtimestamp(
            expires_at, tz=timezone.utc
        ).isoformat(),
    }


@contextmanager
def _control_lock(
    leases_dir: Path, issue_number: int, owner_id: str, lock_timeout_seconds: int
):
    leases_dir.mkdir(parents=True, exist_ok=True)
    lock_path = leases_dir / f"{issue_number}.lock"
    deadline = time.monotonic() + max(1, lock_timeout_seconds)
    while True:
        try:
            handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(handle, "w", encoding="utf-8") as file_handle:
                json.dump(
                    {
                        "issue_number": issue_number,
                        "owner_id": owner_id,
                        "acquired_at": utc_now_iso(),
                    },
                    file_handle,
                    indent=2,
                )
                file_handle.write("\n")
            break
        except FileExistsError:
            lock_payload = _read_optional_json(lock_path)
            if lock_payload is not None and _lease_is_stale(
                lock_payload, stale_after_seconds=lock_timeout_seconds
            ):
                lock_path.unlink(missing_ok=True)
                continue
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"timed out waiting for issue lease control lock for issue {issue_number}"
                )
            time.sleep(LOCK_POLL_INTERVAL_SECONDS)

    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def _lease_path(leases_dir: Path, issue_number: int) -> Path:
    return leases_dir / f"{issue_number}.json"


def _read_optional_json(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    return read_json_file(path)


def _lease_is_stale(
    payload: dict[str, object], *, stale_after_seconds: int | None = None
) -> bool:
    if stale_after_seconds is not None:
        refreshed_at = _parse_iso_timestamp(
            _optional_payload_str(payload.get("acquired_at"))
        )
        if refreshed_at is None:
            return True
        return (
            datetime.now(timezone.utc) - refreshed_at
        ).total_seconds() > stale_after_seconds

    expires_at = _parse_iso_timestamp(_optional_payload_str(payload.get("expires_at")))
    if expires_at is None:
        return True
    return datetime.now(timezone.utc) >= expires_at


def _parse_iso_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _optional_payload_str(value: object) -> str | None:
    return str(value) if value is not None else None
