from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sandcastle.runner.main import (
    _handle_auto_step,
    _handle_implement,
    _handle_run_loop,
)
from sandcastle.runner.models import (
    BacklogSelection,
    BlockOnPolicy,
    BranchPolicy,
    CleanupPolicy,
    CompletionPolicy,
    GateCommand,
    GatesConfig,
    GithubPolicy,
    HandoffPolicy,
    ImplementationPolicy,
    IssuePolicy,
    IssueStatus,
    PullOrderPolicy,
    RepoConfig,
    ReviewPolicy,
    SandcastlePaths,
    SchedulerPolicy,
    TestsPolicy,
    WorktreePolicy,
    ClaimPolicy,
)
from sandcastle.runner.state_store import try_load_issue_status, write_issue_status


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


def _repo_config() -> RepoConfig:
    return RepoConfig(
        repo="example-maintainer/example-project",
        remote="origin",
        default_branch="main",
        base_ref="origin/main",
        branch_prefix="sandcastle",
        branch_name_pattern="sandcastle/issue-{issue_number}-{slug}",
        worktree=WorktreePolicy(
            enabled=True,
            root=".sandcastle-worktrees",
            path_pattern=".sandcastle-worktrees/issue-{issue_number}",
        ),
        scheduler=SchedulerPolicy(
            mode="serial-merge-first",
            max_active_issues=3,
            max_waiting_for_human=3,
            lease_timeout_seconds=1800,
            lease_refresh_interval_seconds=60,
            lease_lock_timeout_seconds=30,
        ),
        implementation=ImplementationPolicy(
            enabled=True,
            command=("python", "-m", "sandcastle.runner.implementation_harness"),
        ),
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
            auto_ready_pr_after_checks=True,
            auto_merge_prs=True,
            merge_method="merge",
            assignee_username="example-maintainer",
        ),
    )


def _gates_config() -> GatesConfig:
    return GatesConfig(
        must_pass_before_pr=True,
        rerun_after_review_fixes=True,
        rerun_after_conflict_resolution=True,
        verification_order=("format", "tests", "review"),
        format=GateCommand(enabled=True, command=("python", "-m", "ruff", "check")),
        tests=TestsPolicy(
            targeted_first=True,
            default_command=("python", "-m", "pytest"),
            escalate_to_full_suite_when=("integration",),
        ),
        review=ReviewPolicy(
            required=True,
            must_be_clean_before_ready_for_human=True,
            max_revision_loops=3,
        ),
        branch_policy=BranchPolicy(
            one_issue_one_branch_until_merge=True,
            new_branch_from_fresh_main_after_merge=True,
        ),
        block_on=BlockOnPolicy(
            format_failure=True,
            test_failure=True,
            unresolved_review_findings=True,
            unsafe_conflict_resolution=True,
        ),
    )


def _last_json_object(raw: str) -> dict[str, object]:
    return _json_objects(raw)[-1]


def _json_objects(raw: str) -> list[dict[str, object]]:
    decoder = json.JSONDecoder()
    cursor = 0
    payloads: list[dict[str, object]] = []
    raw = raw.strip()
    while cursor < len(raw):
        while cursor < len(raw) and raw[cursor].isspace():
            cursor += 1
        if cursor >= len(raw):
            break
        payload, cursor = decoder.raw_decode(raw, cursor)
        if isinstance(payload, dict):
            payloads.append(payload)
    if not payloads:
        raise AssertionError("expected at least one JSON object in command output")
    return payloads


class CleanupPolicyTests(unittest.TestCase):
    def test_implement_auto_cleans_failed_startup_without_commits_or_pr(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            repo_config = _repo_config()
            worktree_path = root / ".sandcastle-worktrees" / "issue-285"
            worktree_path.mkdir(parents=True)
            context_path = paths.contexts_dir / "285.json"
            context_path.parent.mkdir(parents=True, exist_ok=True)
            context_path.write_text('{"title":"Issue 285"}\n', encoding="utf-8")
            lease_path = paths.leases_dir / "285.json"
            lease_path.parent.mkdir(parents=True, exist_ok=True)
            lease_path.write_text('{"issue_number":285}\n', encoding="utf-8")
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=285,
                    status="branch-created",
                    branch="sandcastle/issue-285-cleanup",
                    worktree_path=str(worktree_path),
                    extra={
                        "issue_title": "Cleanup startup failure",
                        "context_loaded": True,
                        "context_path": str(context_path),
                    },
                ),
            )
            implementation_path = paths.implementation_dir / "285.json"
            implementation_path.parent.mkdir(parents=True, exist_ok=True)
            implementation_path.write_text(
                json.dumps({"status": "failed", "returncode": 1}) + "\n",
                encoding="utf-8",
            )
            output = io.StringIO()

            with patch(
                "sandcastle.runner.main.run_implementation_command",
                return_value=implementation_path,
            ):
                with patch(
                    "sandcastle.runner.main.branch_ahead_count", return_value=0
                ):
                    with patch(
                        "sandcastle.runner.main.remove_worktree"
                    ) as remove_worktree:
                        with patch(
                            "sandcastle.runner.main.branch_exists", return_value=True
                        ):
                            with patch(
                                "sandcastle.runner.main.delete_local_branch"
                            ) as delete_local_branch:
                                with patch(
                                    "sandcastle.runner.main.remote_branch_exists",
                                    return_value=True,
                                ):
                                    with patch(
                                        "sandcastle.runner.main.delete_remote_branch"
                                    ) as delete_remote_branch:
                                        with redirect_stdout(output):
                                            exit_code = _handle_implement(
                                                root, paths, repo_config, _gates_config(), 285
                                            )

            self.assertEqual(exit_code, 0)
            self.assertIsNone(try_load_issue_status(paths, 285))
            self.assertFalse(context_path.exists())
            self.assertFalse(implementation_path.exists())
            self.assertFalse(lease_path.exists())
            remove_worktree.assert_called_once_with(root, str(worktree_path))
            delete_local_branch.assert_called_once_with(
                root, "sandcastle/issue-285-cleanup"
            )
            delete_remote_branch.assert_called_once_with(
                root, "origin", "sandcastle/issue-285-cleanup"
            )
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["status"], "failed-startup-cleaned")
            self.assertEqual(payload["issue_number"], 285)

    def test_implement_preserves_blocked_issue_when_branch_has_commits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            repo_config = _repo_config()
            worktree_path = root / ".sandcastle-worktrees" / "issue-285"
            worktree_path.mkdir(parents=True)
            context_path = paths.contexts_dir / "285.json"
            context_path.parent.mkdir(parents=True, exist_ok=True)
            context_path.write_text('{"title":"Issue 285"}\n', encoding="utf-8")
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=285,
                    status="branch-created",
                    branch="sandcastle/issue-285-cleanup",
                    worktree_path=str(worktree_path),
                    extra={
                        "issue_title": "Cleanup startup failure",
                        "context_loaded": True,
                        "context_path": str(context_path),
                    },
                ),
            )
            implementation_path = paths.implementation_dir / "285.json"
            implementation_path.parent.mkdir(parents=True, exist_ok=True)
            implementation_path.write_text(
                json.dumps({"status": "failed", "returncode": 1}) + "\n",
                encoding="utf-8",
            )

            with patch(
                "sandcastle.runner.main.run_implementation_command",
                return_value=implementation_path,
            ):
                with patch("sandcastle.runner.main.branch_ahead_count", return_value=1):
                    with patch("sandcastle.runner.main.branch_exists", return_value=True):
                        with patch(
                            "sandcastle.runner.main.remove_worktree"
                        ) as remove_worktree:
                            exit_code = _handle_implement(
                                root, paths, repo_config, _gates_config(), 285
                            )

            self.assertEqual(exit_code, 1)
            persisted = try_load_issue_status(paths, 285)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(persisted.status, "blocked")
            self.assertEqual(persisted.branch, "sandcastle/issue-285-cleanup")
            self.assertEqual(persisted.worktree_path, str(worktree_path))
            self.assertEqual(persisted.extra["implementation_path"], str(implementation_path))
            remove_worktree.assert_not_called()

    def test_auto_step_cleans_legacy_blocked_failed_startup_issue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            repo_config = _repo_config()
            issue_policy = _issue_policy()
            gates_config = _gates_config()
            worktree_path = root / ".sandcastle-worktrees" / "issue-285"
            worktree_path.mkdir(parents=True)
            implementation_path = paths.implementation_dir / "285.json"
            implementation_path.parent.mkdir(parents=True, exist_ok=True)
            implementation_path.write_text(
                json.dumps({"status": "failed", "returncode": 1}) + "\n",
                encoding="utf-8",
            )
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=285,
                    status="blocked",
                    branch="sandcastle/issue-285-cleanup",
                    worktree_path=str(worktree_path),
                    extra={
                        "issue_title": "Cleanup startup failure",
                        "implementation_status": "failed",
                        "implementation_path": str(implementation_path),
                        "error": "codex exec: unknown flag --ask-for-approval",
                    },
                ),
            )

            with patch("sandcastle.runner.main.branch_ahead_count", return_value=0):
                with patch("sandcastle.runner.main.remove_worktree") as remove_worktree:
                    with patch("sandcastle.runner.main.branch_exists", return_value=True):
                        with patch(
                            "sandcastle.runner.main.delete_local_branch"
                        ) as delete_local_branch:
                            with patch(
                                "sandcastle.runner.main.remote_branch_exists",
                                return_value=False,
                            ):
                                exit_code = _handle_auto_step(
                                    root,
                                    paths,
                                    issue_policy,
                                    repo_config,
                                    gates_config,
                                )

            self.assertEqual(exit_code, 0)
            self.assertIsNone(try_load_issue_status(paths, 285))
            self.assertFalse(implementation_path.exists())
            remove_worktree.assert_called_once_with(root, str(worktree_path))
            delete_local_branch.assert_called_once_with(
                root, "sandcastle/issue-285-cleanup"
            )

    def test_run_loop_waits_after_blocked_issue_that_cannot_be_auto_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            repo_config = _repo_config()
            issue_policy = _issue_policy()
            gates_config = _gates_config()
            worktree_path = root / ".sandcastle-worktrees" / "issue-285"
            worktree_path.mkdir(parents=True)
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=285,
                    status="blocked",
                    branch="sandcastle/issue-285-cleanup",
                    worktree_path=str(worktree_path),
                    extra={
                        "issue_title": "Blocked after real work",
                        "implementation_status": "failed",
                    },
                ),
            )
            output = io.StringIO()

            with patch("sandcastle.runner.main.branch_ahead_count", return_value=2):
                with patch("sandcastle.runner.main.branch_exists", return_value=True):
                    with patch(
                        "sandcastle.runner.main._select_backlog_issue",
                        return_value=BacklogSelection(selected_issue=None),
                    ):
                        with redirect_stdout(output):
                            exit_code = _handle_run_loop(
                                root,
                                paths,
                                issue_policy,
                                repo_config,
                                gates_config,
                                poll_interval_seconds=1,
                                max_idle_polls=1,
                            )

            self.assertEqual(exit_code, 0)
            payloads = _json_objects(output.getvalue())
            self.assertEqual(payloads[-1]["status"], "stopped")
            self.assertEqual(payloads[-1]["blocked_issue_count"], 1)
            self.assertIn(
                "blocked issue",
                str(payloads[-2]["stop_reason"]),
            )


if __name__ == "__main__":
    unittest.main()
