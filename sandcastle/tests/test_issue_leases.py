from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sandcastle.runner.issue_leases import (
    IssueLeaseContext,
    IssueLeaseUnavailable,
    acquire_issue_lease,
    active_issue_lease,
    issue_lease_guard,
    refresh_issue_lease,
    release_issue_lease,
)
from sandcastle.runner.models import SandcastlePaths, SchedulerPolicy


def _paths(root: Path) -> SandcastlePaths:
    state_dir = root / "state"
    return SandcastlePaths(
        root=root / "sandcastle",
        config_dir=root / "sandcastle" / "config",
        templates_dir=root / "sandcastle" / "templates",
        state_dir=state_dir,
        leases_dir=state_dir / "leases",
        issues_dir=state_dir / "issues",
        contexts_dir=state_dir / "contexts",
        ci_dir=state_dir / "ci",
        verification_dir=state_dir / "verification",
        implementation_dir=state_dir / "implementation",
        conflict_dir=state_dir / "conflict",
        review_dir=state_dir / "review",
        findings_dir=state_dir / "findings",
        revision_dir=state_dir / "revision",
    )


def _parallel_scheduler() -> SchedulerPolicy:
    return SchedulerPolicy(
        mode="parallel-backlog-fill",
        max_active_issues=5,
        max_waiting_for_human=5,
        lease_timeout_seconds=60,
        lease_refresh_interval_seconds=5,
        lease_lock_timeout_seconds=5,
    )


class IssueLeaseTests(unittest.TestCase):
    def test_acquire_and_release_issue_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _paths(Path(temp_dir))
            scheduler = _parallel_scheduler()
            lease = acquire_issue_lease(
                paths,
                scheduler,
                IssueLeaseContext(
                    issue_number=218,
                    operation="advance:implementing",
                    status="implementing",
                    branch="sandcastle/issue-218",
                    worktree_path="C:/worktree",
                    pr_number=10,
                ),
                owner_id="worker-a",
            )

            self.assertEqual(lease["owner_id"], "worker-a")
            self.assertIsNotNone(active_issue_lease(paths, 218))
            self.assertTrue(
                release_issue_lease(paths, scheduler, 218, owner_id="worker-a")
            )
            self.assertIsNone(active_issue_lease(paths, 218))

    def test_parallel_worker_cannot_take_live_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _paths(Path(temp_dir))
            scheduler = _parallel_scheduler()
            context = IssueLeaseContext(
                issue_number=218,
                operation="advance:implementing",
                status="implementing",
                branch="sandcastle/issue-218",
                worktree_path="C:/worktree",
                pr_number=None,
            )
            acquire_issue_lease(paths, scheduler, context, owner_id="worker-a")

            with self.assertRaises(IssueLeaseUnavailable):
                acquire_issue_lease(paths, scheduler, context, owner_id="worker-b")

    def test_stale_lease_can_be_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _paths(Path(temp_dir))
            scheduler = _parallel_scheduler()
            context = IssueLeaseContext(
                issue_number=218,
                operation="advance:implementing",
                status="implementing",
                branch="sandcastle/issue-218",
                worktree_path="C:/worktree",
                pr_number=None,
            )
            acquire_issue_lease(paths, scheduler, context, owner_id="worker-a")
            lease_path = paths.leases_dir / "218.json"
            payload = active_issue_lease(paths, 218)
            assert payload is not None
            payload["expires_at"] = "2000-01-01T00:00:00+00:00"
            lease_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

            recovered = acquire_issue_lease(
                paths,
                scheduler,
                context,
                owner_id="worker-b",
            )
            self.assertEqual(recovered["owner_id"], "worker-b")

    def test_refresh_keeps_same_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _paths(Path(temp_dir))
            scheduler = _parallel_scheduler()
            context = IssueLeaseContext(
                issue_number=218,
                operation="advance:verify",
                status="pr-open",
                branch="sandcastle/issue-218",
                worktree_path="C:/worktree",
                pr_number=42,
            )
            with patch(
                "sandcastle.runner.issue_leases.utc_now_iso",
                side_effect=[
                    "2026-10-02T12:24:32+00:00",
                    "2026-10-02T12:24:32+00:00",
                    "2026-10-02T12:24:33+00:00",
                    "2026-10-02T12:24:33+00:00",
                ],
            ):
                original = acquire_issue_lease(
                    paths, scheduler, context, owner_id="worker-a"
                )
                refreshed = refresh_issue_lease(
                    paths, scheduler, context, owner_id="worker-a"
                )

            self.assertEqual(refreshed["owner_id"], "worker-a")
            self.assertEqual(refreshed["acquired_at"], original["acquired_at"])
            self.assertNotEqual(refreshed["refreshed_at"], original["refreshed_at"])

    def test_serial_mode_does_not_create_lease_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _paths(Path(temp_dir))
            scheduler = SchedulerPolicy(
                mode="serial-merge-first",
                max_active_issues=5,
                max_waiting_for_human=5,
                lease_timeout_seconds=60,
                lease_refresh_interval_seconds=5,
                lease_lock_timeout_seconds=5,
            )
            with issue_lease_guard(
                paths,
                scheduler,
                issue_number=218,
                operation="advance:implementing",
                status="implementing",
            ):
                pass

            self.assertFalse(paths.leases_dir.exists())

    def test_guard_releases_lease_on_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _paths(Path(temp_dir))
            scheduler = _parallel_scheduler()
            with patch(
                "sandcastle.runner.issue_leases.current_worker_id",
                return_value="worker-a",
            ):
                with issue_lease_guard(
                    paths,
                    scheduler,
                    issue_number=218,
                    operation="advance:implementing",
                    status="implementing",
                    branch="sandcastle/issue-218",
                ):
                    self.assertEqual(
                        active_issue_lease(paths, 218)["owner_id"], "worker-a"
                    )

            self.assertIsNone(active_issue_lease(paths, 218))


if __name__ == "__main__":
    unittest.main()
