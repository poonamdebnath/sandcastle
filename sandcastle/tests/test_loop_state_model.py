from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from sandcastle.runner.main import (
    _count_active_work,
    _count_waiting_work,
    _handle_probe_ci,
    _handle_process_issue,
    _handle_process_next,
    _handle_run_loop,
    _handle_status,
    _handle_sync_pr,
    _operator_state,
    _scheduler_snapshot,
)
from sandcastle.runner.models import (
    BacklogSelection,
    ClaimPolicy,
    CleanupPolicy,
    CompletionPolicy,
    GithubPolicy,
    HandoffPolicy,
    ImplementationPolicy,
    IssueCandidate,
    IssuePolicy,
    IssueStatus,
    PullOrderPolicy,
    RepoConfig,
    SandcastlePaths,
    SchedulerPolicy,
    WorktreePolicy,
)
from sandcastle.runner.state_store import try_load_issue_status, write_issue_status, write_json_file


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


def _issue_policy() -> IssuePolicy:
    return IssuePolicy(
        required_labels=("ready-for-agent", "type:dev"),
        forbidden_labels=("needs-info", "ready-for-human", "wontfix", "type:infra"),
        require_open_state=True,
        require_unassigned=False,
        allow_batches=False,
        claim=ClaimPolicy(
            assign_to_me=True,
            add_comment=True,
            comment_template="issue-comment-claimed.md",
        ),
        pull_order=PullOrderPolicy(
            strategy="oldest-first",
            max_issues_per_run=1,
            hold_label="sandcastle:hold",
            priority_high_label="sandcastle:priority-high",
            priority_low_label="sandcastle:priority-low",
        ),
        handoff=HandoffPolicy(
            comment_template="issue-comment-blocked.md",
            write_local_note=True,
            local_note_template="handoff-note.md",
        ),
        completion=CompletionPolicy(comment_template="issue-comment-complete.md"),
    )


def _scheduler() -> SchedulerPolicy:
    return SchedulerPolicy(
        mode="parallel-backlog-fill",
        max_active_issues=1,
        max_waiting_for_human=3,
        lease_timeout_seconds=60,
        lease_refresh_interval_seconds=5,
        lease_lock_timeout_seconds=5,
    )


def _repo_config() -> RepoConfig:
    return RepoConfig(
        repo="example-maintainer/example-project",
        remote="origin",
        default_branch="main",
        base_ref="origin/main",
        branch_prefix="sandcastle/issue-",
        branch_name_pattern="sandcastle/issue-{issue_number}",
        worktree=WorktreePolicy(
            enabled=True,
            root=".sandcastle-worktrees",
            path_pattern="issue-{issue_number}",
        ),
        scheduler=_scheduler(),
        implementation=ImplementationPolicy(enabled=True, command=("python", "-m", "sandcastle")),
        cleanup=CleanupPolicy(
            close_issue_after_merge=True,
            remove_worktree_after_merge=True,
            delete_local_branch_after_merge=True,
            delete_remote_branch_after_merge=True,
            remove_worktree_after_failed_startup=True,
            delete_local_branch_after_failed_startup=True,
            delete_remote_branch_after_failed_startup=True,
            delete_runtime_state_after_failed_startup=True,
            remove_worktree_after_block=False,
            delete_local_branch_after_block=False,
        ),
        github=GithubPolicy(
            issues_enabled=True,
            pull_requests_enabled=True,
            draft_pr_by_default=True,
            auto_ready_pr_after_checks=False,
            auto_merge_prs=False,
            merge_method="squash",
            assignee_username="example-maintainer",
        ),
    )


def _open_pr_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "number": 17,
        "url": "https://example.test/pr/17",
        "state": "OPEN",
        "isDraft": False,
        "mergedAt": None,
        "headRefName": "sandcastle/issue-290",
        "headRefOid": "abc123",
        "baseRefName": "main",
        "reviewDecision": None,
        "mergeStateStatus": "CLEAN",
        "mergeable": "MERGEABLE",
    }
    payload.update(overrides)
    return payload


class LoopStateProjectionTests(unittest.TestCase):
    def test_scheduler_snapshot_separates_active_and_waiting_work(self) -> None:
        active = IssueStatus(issue_number=1, status="implementing")
        waiting_human = IssueStatus(
            issue_number=2,
            status="ready-for-human",
            pr_number=12,
        )
        waiting_ci = IssueStatus(
            issue_number=3,
            status="pr-open",
            pr_number=13,
            extra={"ci_status": "pending", "findings_status": "clean"},
        )
        no_op = IssueStatus(issue_number=4, status="no-op")
        backlog = BacklogSelection(selected_issue=None)

        snapshot = _scheduler_snapshot(
            [active, waiting_human, waiting_ci, no_op],
            backlog,
            _scheduler(),
        )

        self.assertEqual(_operator_state(active), "active")
        self.assertEqual(_operator_state(waiting_human), "waiting")
        self.assertEqual(_operator_state(waiting_ci), "waiting")
        self.assertEqual(_operator_state(no_op), "done")
        self.assertEqual(
            _count_active_work([active, waiting_human, waiting_ci, no_op]), 1
        )
        self.assertEqual(
            _count_waiting_work([active, waiting_human, waiting_ci, no_op]), 2
        )
        self.assertEqual(snapshot["active_issue_count"], 1)
        self.assertEqual(snapshot["waiting_issue_count"], 2)
        self.assertEqual(
            snapshot["operator_state_counts"],
            {"active": 1, "waiting": 2, "blocked": 0, "done": 0},
        )

    def test_status_command_reports_operator_state(self) -> None:
        output = io.StringIO()
        issue = IssueCandidate(
            number=290,
            title="Loop states",
            state="OPEN",
            labels=("ready-for-agent", "type:dev"),
            assignees=(),
            url="https://example.test/issues/290",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _paths(Path(temp_dir))
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=290,
                    status="pr-open",
                    pr_number=17,
                    extra={"ci_status": "pending", "findings_status": "clean"},
                ),
            )

            with patch("sandcastle.runner.main.get_issue", return_value=issue):
                with redirect_stdout(output):
                    exit_code = _handle_status(
                        Path(temp_dir),
                        paths,
                        _issue_policy(),
                        290,
                    )

        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["local_state"]["status"], "pr-open")
        self.assertEqual(payload["local_state"]["operator_state"], "waiting")


class WaitingWakeupTests(unittest.TestCase):
    def _write_waiting_issue(
        self,
        paths: SandcastlePaths,
        status: str = "ready-for-human",
        *,
        extra: dict[str, object] | None = None,
    ) -> None:
        issue_extra: dict[str, object] = {
            "issue_title": "Loop states",
            "ci_status": "passed",
            "findings_status": "clean",
        }
        if extra:
            issue_extra.update(extra)
        write_issue_status(
            paths,
            IssueStatus(
                issue_number=290,
                status=status,
                branch="sandcastle/issue-290",
                worktree_path="C:/worktrees/issue-290",
                pr_number=17,
                extra=issue_extra,
            ),
        )

    def test_sync_pr_reactivates_waiting_issue_for_review_findings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            self._write_waiting_issue(paths)

            with patch(
                "sandcastle.runner.main.snapshot_pr_checks",
                side_effect=lambda **kwargs: write_json_file(
                    kwargs["ci_dir"] / f"{kwargs['issue_number']}.json",
                    {
                        "issue_number": kwargs["issue_number"],
                        "pr_number": kwargs["pr_number"],
                        "status": "passed",
                        "checks": [],
                        "failed_checks": [],
                    },
                ),
            ):
                with patch(
                    "sandcastle.runner.main.collect_findings",
                    side_effect=lambda **kwargs: write_json_file(
                        kwargs["findings_dir"] / f"{kwargs['issue_number']}.json",
                        {
                            "issue_number": kwargs["issue_number"],
                            "pr_number": kwargs["pr_number"],
                            "status": "findings",
                            "findings": [{"summary": "new review comment"}],
                        },
                    ),
                ):
                    with patch(
                        "sandcastle.runner.main.get_pr",
                        return_value=_open_pr_payload(),
                    ):
                        with patch(
                            "sandcastle.runner.main.get_pr_check_rollup",
                            return_value=[],
                        ):
                            exit_code = _handle_sync_pr(
                                root,
                                paths,
                                _repo_config(),
                                290,
                            )
            persisted = try_load_issue_status(paths, 290)

        self.assertEqual(exit_code, 0)
        assert persisted is not None
        self.assertEqual(persisted.status, "review-fix-loop")
        self.assertEqual(_operator_state(persisted), "active")

    def test_sync_pr_reactivates_waiting_issue_for_ci_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            self._write_waiting_issue(paths, status="waiting-human-review")

            with patch(
                "sandcastle.runner.main.snapshot_pr_checks",
                side_effect=lambda **kwargs: write_json_file(
                    kwargs["ci_dir"] / f"{kwargs['issue_number']}.json",
                    {
                        "issue_number": kwargs["issue_number"],
                        "pr_number": kwargs["pr_number"],
                        "status": "failed",
                        "checks": [],
                        "failed_checks": [{"name": "test"}],
                    },
                ),
            ):
                with patch(
                    "sandcastle.runner.main.collect_findings",
                    side_effect=lambda **kwargs: write_json_file(
                        kwargs["findings_dir"] / f"{kwargs['issue_number']}.json",
                        {
                            "issue_number": kwargs["issue_number"],
                            "pr_number": kwargs["pr_number"],
                            "status": "clean",
                            "findings": [],
                        },
                    ),
                ):
                    with patch(
                        "sandcastle.runner.main.get_pr",
                        return_value=_open_pr_payload(),
                    ):
                        with patch(
                            "sandcastle.runner.main.get_pr_check_rollup",
                            return_value=[],
                        ):
                            exit_code = _handle_sync_pr(
                                root,
                                paths,
                                _repo_config(),
                                290,
                            )
            persisted = try_load_issue_status(paths, 290)

        self.assertEqual(exit_code, 0)
        assert persisted is not None
        self.assertEqual(persisted.status, "review-fix-loop")
        self.assertEqual(_operator_state(persisted), "active")

    def test_sync_pr_reactivates_waiting_issue_for_stale_branch_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            self._write_waiting_issue(paths)

            with patch(
                "sandcastle.runner.main.snapshot_pr_checks",
                side_effect=lambda **kwargs: write_json_file(
                    kwargs["ci_dir"] / f"{kwargs['issue_number']}.json",
                    {
                        "issue_number": kwargs["issue_number"],
                        "pr_number": kwargs["pr_number"],
                        "status": "pending",
                        "checks": [],
                        "failed_checks": [],
                    },
                ),
            ):
                with patch(
                    "sandcastle.runner.main.collect_findings",
                    side_effect=lambda **kwargs: write_json_file(
                        kwargs["findings_dir"] / f"{kwargs['issue_number']}.json",
                        {
                            "issue_number": kwargs["issue_number"],
                            "pr_number": kwargs["pr_number"],
                            "status": "clean",
                            "findings": [],
                        },
                    ),
                ):
                    with patch(
                        "sandcastle.runner.main.get_pr",
                        return_value=_open_pr_payload(mergeStateStatus="BEHIND"),
                    ):
                        with patch(
                            "sandcastle.runner.main.get_pr_check_rollup",
                            return_value=[],
                        ):
                            exit_code = _handle_sync_pr(
                                root,
                                paths,
                                _repo_config(),
                                290,
                            )
            persisted = try_load_issue_status(paths, 290)

        self.assertEqual(exit_code, 0)
        assert persisted is not None
        self.assertEqual(persisted.status, "resolving-conflicts")
        self.assertEqual(_operator_state(persisted), "active")

    def test_sync_pr_reactivates_waiting_issue_when_gate_results_are_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            self._write_waiting_issue(
                paths,
                extra={
                    "implementation_path": "C:/state/implementation/290.json",
                    "implementation_head_sha": "old-head",
                    "verified_implementation_path": "C:/state/implementation/290.json",
                    "verified_head_sha": "old-head",
                    "reviewed_implementation_path": "C:/state/implementation/290.json",
                    "reviewed_head_sha": "old-head",
                    "findings_source_implementation_path": "C:/state/implementation/290.json",
                    "findings_source_head_sha": "old-head",
                    "pr_head_sha": "old-head",
                },
            )

            with patch(
                "sandcastle.runner.main.snapshot_pr_checks",
                side_effect=lambda **kwargs: write_json_file(
                    kwargs["ci_dir"] / f"{kwargs['issue_number']}.json",
                    {
                        "issue_number": kwargs["issue_number"],
                        "pr_number": kwargs["pr_number"],
                        "status": "passed",
                        "checks": [],
                        "failed_checks": [],
                    },
                ),
            ):
                with patch(
                    "sandcastle.runner.main.collect_findings",
                    side_effect=lambda **kwargs: write_json_file(
                        kwargs["findings_dir"] / f"{kwargs['issue_number']}.json",
                        {
                            "issue_number": kwargs["issue_number"],
                            "pr_number": kwargs["pr_number"],
                            "status": "clean",
                            "findings": [],
                        },
                    ),
                ):
                    with patch(
                        "sandcastle.runner.main.get_pr",
                        return_value=_open_pr_payload(
                            headRefOid="new-head",
                            reviewDecision="APPROVED",
                        ),
                    ):
                        with patch(
                            "sandcastle.runner.main.get_pr_check_rollup",
                            return_value=[],
                        ):
                            exit_code = _handle_sync_pr(
                                root,
                                paths,
                                _repo_config(),
                                290,
                            )
            persisted = try_load_issue_status(paths, 290)

        self.assertEqual(exit_code, 0)
        assert persisted is not None
        self.assertEqual(persisted.status, "pr-open")
        self.assertEqual(_operator_state(persisted), "active")
        self.assertTrue(persisted.extra["verification_is_stale"])
        self.assertTrue(persisted.extra["review_is_stale"])
        self.assertTrue(persisted.extra["findings_are_stale"])

    def test_sync_pr_keeps_findings_stale_until_fresh_gates_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            self._write_waiting_issue(
                paths,
                extra={
                    "implementation_path": "C:/state/implementation/290.json",
                    "implementation_head_sha": "old-head",
                    "verified_implementation_path": "C:/state/implementation/290.json",
                    "verified_head_sha": "old-head",
                    "reviewed_implementation_path": "C:/state/implementation/290.json",
                    "reviewed_head_sha": "old-head",
                    "findings_source_implementation_path": "C:/state/implementation/290.json",
                    "findings_source_head_sha": "old-head",
                    "pr_head_sha": "old-head",
                },
            )

            with patch(
                "sandcastle.runner.main.snapshot_pr_checks",
                side_effect=lambda **kwargs: write_json_file(
                    kwargs["ci_dir"] / f"{kwargs['issue_number']}.json",
                    {
                        "issue_number": kwargs["issue_number"],
                        "pr_number": kwargs["pr_number"],
                        "status": "passed",
                        "checks": [],
                        "failed_checks": [],
                    },
                ),
            ):
                with patch(
                    "sandcastle.runner.main.collect_findings",
                    side_effect=lambda **kwargs: write_json_file(
                        kwargs["findings_dir"] / f"{kwargs['issue_number']}.json",
                        {
                            "issue_number": kwargs["issue_number"],
                            "pr_number": kwargs["pr_number"],
                            "status": "clean",
                            "findings": [],
                        },
                    ),
                ):
                    with patch(
                        "sandcastle.runner.main.get_pr",
                        return_value=_open_pr_payload(
                            headRefOid="new-head",
                            reviewDecision="APPROVED",
                        ),
                    ):
                        with patch(
                            "sandcastle.runner.main.get_pr_check_rollup",
                            return_value=[],
                        ):
                            first_exit_code = _handle_sync_pr(
                                root,
                                paths,
                                _repo_config(),
                                290,
                            )
                            second_exit_code = _handle_sync_pr(
                                root,
                                paths,
                                _repo_config(),
                                290,
                            )
            persisted = try_load_issue_status(paths, 290)

        self.assertEqual(first_exit_code, 0)
        self.assertEqual(second_exit_code, 0)
        assert persisted is not None
        self.assertEqual(persisted.status, "pr-open")
        self.assertTrue(persisted.extra["verification_is_stale"])
        self.assertTrue(persisted.extra["review_is_stale"])
        self.assertTrue(persisted.extra["findings_are_stale"])
        self.assertEqual(persisted.extra["findings_source_head_sha"], "old-head")

    def test_probe_ci_reactivates_waiting_issue_when_gate_results_are_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            self._write_waiting_issue(
                paths,
                status="pr-open",
                extra={
                    "implementation_path": "C:/state/implementation/290.json",
                    "implementation_head_sha": "old-head",
                    "verified_implementation_path": "C:/state/implementation/290.json",
                    "verified_head_sha": "old-head",
                    "reviewed_implementation_path": "C:/state/implementation/290.json",
                    "reviewed_head_sha": "old-head",
                    "findings_source_implementation_path": "C:/state/implementation/290.json",
                    "findings_source_head_sha": "old-head",
                    "pr_head_sha": "old-head",
                },
            )

            with patch(
                "sandcastle.runner.main.snapshot_pr_checks",
                side_effect=lambda **kwargs: write_json_file(
                    kwargs["ci_dir"] / f"{kwargs['issue_number']}.json",
                    {
                        "issue_number": kwargs["issue_number"],
                        "pr_number": kwargs["pr_number"],
                        "status": "passed",
                        "checks": [],
                        "failed_checks": [],
                    },
                ),
            ):
                with patch(
                    "sandcastle.runner.main.collect_findings",
                    side_effect=lambda **kwargs: write_json_file(
                        kwargs["findings_dir"] / f"{kwargs['issue_number']}.json",
                        {
                            "issue_number": kwargs["issue_number"],
                            "pr_number": kwargs["pr_number"],
                            "status": "clean",
                            "findings": [],
                        },
                    ),
                ):
                    with patch(
                        "sandcastle.runner.main.get_pr",
                        return_value=_open_pr_payload(
                            headRefOid="new-head",
                            reviewDecision="APPROVED",
                        ),
                    ):
                        exit_code = _handle_probe_ci(
                            root,
                            paths,
                            _repo_config(),
                            290,
                        )
            persisted = try_load_issue_status(paths, 290)

        self.assertEqual(exit_code, 0)
        assert persisted is not None
        self.assertEqual(persisted.status, "pr-open")
        self.assertEqual(_operator_state(persisted), "active")
        self.assertTrue(persisted.extra["verification_is_stale"])
        self.assertTrue(persisted.extra["review_is_stale"])
        self.assertTrue(persisted.extra["findings_are_stale"])

    def test_sync_pr_moves_clean_approved_pr_to_merge_wait_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            self._write_waiting_issue(
                paths,
                extra={
                    "implementation_path": "C:/state/implementation/290.json",
                    "implementation_head_sha": "abc123",
                    "verified_implementation_path": "C:/state/implementation/290.json",
                    "verified_head_sha": "abc123",
                    "reviewed_implementation_path": "C:/state/implementation/290.json",
                    "reviewed_head_sha": "abc123",
                    "findings_source_implementation_path": "C:/state/implementation/290.json",
                    "findings_source_head_sha": "abc123",
                    "pr_head_sha": "abc123",
                },
            )

            with patch(
                "sandcastle.runner.main.snapshot_pr_checks",
                side_effect=lambda **kwargs: write_json_file(
                    kwargs["ci_dir"] / f"{kwargs['issue_number']}.json",
                    {
                        "issue_number": kwargs["issue_number"],
                        "pr_number": kwargs["pr_number"],
                        "status": "passed",
                        "checks": [],
                        "failed_checks": [],
                    },
                ),
            ):
                with patch(
                    "sandcastle.runner.main.collect_findings",
                    side_effect=lambda **kwargs: write_json_file(
                        kwargs["findings_dir"] / f"{kwargs['issue_number']}.json",
                        {
                            "issue_number": kwargs["issue_number"],
                            "pr_number": kwargs["pr_number"],
                            "status": "clean",
                            "findings": [],
                        },
                    ),
                ):
                    with patch(
                        "sandcastle.runner.main.get_pr",
                        return_value=_open_pr_payload(reviewDecision="APPROVED"),
                    ):
                        with patch(
                            "sandcastle.runner.main.get_pr_check_rollup",
                            return_value=[],
                        ):
                            exit_code = _handle_sync_pr(
                                root,
                                paths,
                                _repo_config(),
                                290,
                            )
            persisted = try_load_issue_status(paths, 290)

        self.assertEqual(exit_code, 0)
        assert persisted is not None
        self.assertEqual(persisted.status, "approved-awaiting-merge")
        self.assertEqual(_operator_state(persisted), "waiting")

    def test_sync_pr_persists_unsatisfied_merge_conditions(self) -> None:
        base = _repo_config()
        repo_config = RepoConfig(
            repo=base.repo,
            remote=base.remote,
            default_branch=base.default_branch,
            base_ref=base.base_ref,
            branch_prefix=base.branch_prefix,
            branch_name_pattern=base.branch_name_pattern,
            worktree=base.worktree,
            scheduler=base.scheduler,
            implementation=base.implementation,
            cleanup=base.cleanup,
            github=GithubPolicy(
                issues_enabled=base.github.issues_enabled,
                pull_requests_enabled=base.github.pull_requests_enabled,
                draft_pr_by_default=base.github.draft_pr_by_default,
                auto_ready_pr_after_checks=base.github.auto_ready_pr_after_checks,
                auto_merge_prs=True,
                merge_method=base.github.merge_method,
                assignee_username=base.github.assignee_username,
            ),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            self._write_waiting_issue(
                paths,
                extra={
                    "implementation_path": "C:/state/implementation/290.json",
                    "implementation_head_sha": "abc123",
                    "verified_implementation_path": "C:/state/implementation/290.json",
                    "verified_head_sha": "abc123",
                    "reviewed_implementation_path": "C:/state/implementation/290.json",
                    "reviewed_head_sha": "abc123",
                    "findings_source_implementation_path": "C:/state/implementation/290.json",
                    "findings_source_head_sha": "abc123",
                    "pr_head_sha": "abc123",
                },
            )

            with patch(
                "sandcastle.runner.main.snapshot_pr_checks",
                side_effect=lambda **kwargs: write_json_file(
                    kwargs["ci_dir"] / f"{kwargs['issue_number']}.json",
                    {
                        "issue_number": kwargs["issue_number"],
                        "pr_number": kwargs["pr_number"],
                        "status": "passed",
                        "checks": [],
                        "failed_checks": [],
                    },
                ),
            ):
                with patch(
                    "sandcastle.runner.main.collect_findings",
                    side_effect=lambda **kwargs: write_json_file(
                        kwargs["findings_dir"] / f"{kwargs['issue_number']}.json",
                        {
                            "issue_number": kwargs["issue_number"],
                            "pr_number": kwargs["pr_number"],
                            "status": "clean",
                            "findings": [],
                        },
                    ),
                ):
                    with patch(
                        "sandcastle.runner.main.get_pr",
                        return_value=_open_pr_payload(
                            reviewDecision="APPROVED",
                            mergeStateStatus="BLOCKED",
                        ),
                    ):
                        with patch(
                            "sandcastle.runner.main.get_pr_check_rollup",
                            return_value=[],
                        ):
                            with patch("sandcastle.runner.main.merge_pr") as merge_pr:
                                exit_code = _handle_sync_pr(
                                    root,
                                    paths,
                                    repo_config,
                                    290,
                                )
            persisted = try_load_issue_status(paths, 290)

        self.assertEqual(exit_code, 0)
        merge_pr.assert_not_called()
        assert persisted is not None
        self.assertEqual(persisted.status, "approved-awaiting-merge")
        self.assertEqual(persisted.extra["auto_merge_status"], "waiting")
        self.assertIn("merge state is blocked", str(persisted.extra["auto_merge_reason"]))


class LoopStopBehaviorTests(unittest.TestCase):
    def test_process_next_advances_single_issue_until_waiting_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=290,
                    status="claimed",
                    branch="sandcastle/issue-290",
                    worktree_path="C:/worktrees/issue-290",
                ),
            )
            output = io.StringIO()
            transitions = [
                IssueStatus(
                    issue_number=290,
                    status="implementing",
                    branch="sandcastle/issue-290",
                    worktree_path="C:/worktrees/issue-290",
                ),
                IssueStatus(
                    issue_number=290,
                    status="ready-for-human",
                    branch="sandcastle/issue-290",
                    worktree_path="C:/worktrees/issue-290",
                    pr_number=17,
                ),
            ]

            def advance(*_args, **_kwargs) -> int:
                write_issue_status(paths, transitions.pop(0))
                return 0

            with patch(
                "sandcastle.runner.main._is_failed_startup_cleanup_candidate",
                return_value=False,
            ):
                with patch(
                    "sandcastle.runner.main.issue_lease_guard",
                    side_effect=lambda *args, **kwargs: nullcontext(),
                ):
                    with patch(
                        "sandcastle.runner.main._advance_active_issue",
                        side_effect=advance,
                    ):
                        with patch(
                            "sandcastle.runner.main._select_backlog_issue",
                            return_value=BacklogSelection(selected_issue=None),
                        ):
                            with redirect_stdout(output):
                                exit_code = _handle_process_next(
                                    root,
                                    paths,
                                    _issue_policy(),
                                    _repo_config(),
                                    object(),
                                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "waiting")
        self.assertEqual(payload["chosen_issue_number"], 290)
        self.assertEqual(payload["resulting_state"]["status"], "ready-for-human")
        self.assertEqual(payload["resulting_state"]["operator_state"], "waiting")
        self.assertEqual(payload["micro_steps"], 2)

    def test_process_next_ignores_unrelated_blocked_issue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=289,
                    status="blocked",
                    branch="sandcastle/issue-289",
                    worktree_path="C:/worktrees/issue-289",
                    extra={
                        "blocked_reason": "needs human clarification",
                        "blocked_next_action": "Clarify the requirement before resuming.",
                    },
                ),
            )
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=290,
                    status="claimed",
                    branch="sandcastle/issue-290",
                    worktree_path="C:/worktrees/issue-290",
                ),
            )
            output = io.StringIO()

            def advance(*_args, **_kwargs) -> int:
                write_issue_status(
                    paths,
                    IssueStatus(
                        issue_number=290,
                        status="ready-for-human",
                        branch="sandcastle/issue-290",
                        worktree_path="C:/worktrees/issue-290",
                        pr_number=17,
                    ),
                )
                return 0

            with patch(
                "sandcastle.runner.main._is_failed_startup_cleanup_candidate",
                return_value=False,
            ):
                with patch(
                    "sandcastle.runner.main.issue_lease_guard",
                    side_effect=lambda *args, **kwargs: nullcontext(),
                ):
                    with patch(
                        "sandcastle.runner.main._advance_active_issue",
                        side_effect=advance,
                    ):
                        with patch(
                            "sandcastle.runner.main._select_backlog_issue",
                            return_value=BacklogSelection(selected_issue=None),
                        ):
                            with redirect_stdout(output):
                                exit_code = _handle_process_next(
                                    root,
                                    paths,
                                    _issue_policy(),
                                    _repo_config(),
                                    object(),
                                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "waiting")
        self.assertEqual(payload["chosen_issue_number"], 290)
        self.assertEqual(payload["resulting_state"]["status"], "ready-for-human")
        self.assertEqual(payload["blocked_issue_count"], 1)

    def test_process_issue_stops_at_waiting_boundary_when_capacity_is_full(self) -> None:
        issue = IssueCandidate(
            number=271,
            title="Targeted orchestration",
            state="OPEN",
            labels=("ready-for-agent", "type:dev"),
            assignees=(),
            url="https://example.test/issues/271",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=290,
                    status="implementing",
                    branch="sandcastle/issue-290",
                    worktree_path="C:/worktrees/issue-290",
                ),
            )
            output = io.StringIO()

            with patch("sandcastle.runner.main.get_issue", return_value=issue):
                with patch(
                    "sandcastle.runner.main._select_backlog_issue",
                    return_value=BacklogSelection(selected_issue=None),
                ):
                    with redirect_stdout(output):
                        exit_code = _handle_process_issue(
                            root,
                            paths,
                            _issue_policy(),
                            _repo_config(),
                            object(),
                            271,
                        )

        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "waiting")
        self.assertEqual(payload["chosen_issue_number"], 271)
        self.assertIsNone(payload["resulting_state"])
        self.assertEqual(payload["stop_category"], "idle-wait")
        self.assertIn("capacity", payload["stop_reason"])

    def test_run_loop_stops_for_blocked_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)

            with patch(
                "sandcastle.runner.main._process_until_boundary",
                return_value=(
                    1,
                    {
                        "status": "blocked",
                        "block_scope": "queue",
                        "chosen_issue_number": 290,
                        "stop_category": "repo-global-blocker",
                        "stop_reason": "queue is paused by a repo-global blocker that requires human intervention",
                    },
                ),
            ):
                with redirect_stdout(io.StringIO()):
                    exit_code = _handle_run_loop(
                        root,
                        paths,
                        _issue_policy(),
                        _repo_config(),
                        object(),
                        poll_interval_seconds=1,
                        max_idle_polls=1,
                    )

        self.assertEqual(exit_code, 1)

    def test_run_loop_continues_after_issue_local_blocked_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            process_calls = []

            def process_side_effect(*_args, **_kwargs):
                process_calls.append("called")
                if len(process_calls) == 1:
                    return (
                        1,
                        {
                            "status": "blocked",
                            "block_scope": "issue",
                            "chosen_issue_number": 290,
                            "stop_category": "real-blocker",
                            "stop_reason": "blocked issue requires human intervention before Sandcastle can continue",
                        },
                    )
                return (
                    0,
                    {
                        "status": "waiting",
                        "chosen_issue_number": None,
                        "stop_category": "idle-wait",
                        "stop_reason": "no eligible open issue is available for pickup",
                    },
                )

            with patch(
                "sandcastle.runner.main._process_until_boundary",
                side_effect=process_side_effect,
            ):
                with redirect_stdout(io.StringIO()):
                    exit_code = _handle_run_loop(
                        root,
                        paths,
                        _issue_policy(),
                        _repo_config(),
                        object(),
                        poll_interval_seconds=1,
                        max_idle_polls=1,
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(process_calls), 2)

    def test_run_loop_continues_after_done_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            process_calls = []

            def process_side_effect(*_args, **_kwargs):
                process_calls.append("called")
                if len(process_calls) == 1:
                    return (
                        0,
                        {
                            "status": "done",
                            "chosen_issue_number": 290,
                            "stop_category": "done-boundary",
                            "stop_reason": "issue 290 reached a done boundary",
                        },
                    )
                return (
                    0,
                    {
                        "status": "waiting",
                        "chosen_issue_number": None,
                        "stop_category": "idle-wait",
                        "stop_reason": "no eligible open issue is available for pickup",
                    },
                )

            with patch(
                "sandcastle.runner.main._process_until_boundary",
                side_effect=process_side_effect,
            ):
                with redirect_stdout(io.StringIO()):
                    exit_code = _handle_run_loop(
                        root,
                        paths,
                        _issue_policy(),
                        _repo_config(),
                        object(),
                        poll_interval_seconds=1,
                        max_idle_polls=1,
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(process_calls), 2)


if __name__ == "__main__":
    unittest.main()
