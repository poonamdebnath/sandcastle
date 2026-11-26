from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from sandcastle.runner.main import _handle_collect_findings, _handle_implement
from sandcastle.runner.models import (
    BlockOnPolicy,
    BranchPolicy,
    CleanupPolicy,
    CompletionPolicy,
    GateCommand,
    GatesConfig,
    GithubPolicy,
    HandoffPolicy,
    ImplementationPolicy,
    IssueStatus,
    RepoConfig,
    ReviewPolicy,
    SandcastlePaths,
    SchedulerPolicy,
    TestsPolicy,
    WorktreePolicy,
)
from sandcastle.runner.state_store import try_load_issue_status, write_json_file, write_issue_status


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


def _write_no_op_template(paths: SandcastlePaths) -> None:
    template_path = paths.templates_dir / "issue-comment-no-op.md"
    template_path.parent.mkdir(parents=True, exist_ok=True)
    template_path.write_text(
        "No-op for #{{issue_number}}.\n"
        "Branch: {{branch}}\n"
        "Worktree: {{worktree_path}}\n"
        "Cleanup:\n"
        "{{cleanup_summary}}\n",
        encoding="utf-8",
    )


class ImplementationLoopTests(unittest.TestCase):
    def test_zero_change_success_becomes_terminal_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            worktree_path = root / ".sandcastle-worktrees" / "issue-270"
            worktree_path.mkdir(parents=True)
            _write_no_op_template(paths)
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=270,
                    status="branch-created",
                    branch="sandcastle/issue-270",
                    worktree_path=str(worktree_path),
                    extra={"issue_title": "Issue 270"},
                ),
            )
            implementation_path = write_json_file(
                paths.implementation_dir / "270.json",
                {
                    "status": "succeeded",
                    "returncode": 0,
                    "pre_command_head_sha": "abc",
                    "post_command_head_sha": "abc",
                    "changed_files": [".sandcastle-agent/issue-270/implementation-brief.json"],
                    "diff_summary": "",
                },
            )

            with patch(
                "sandcastle.runner.main.run_implementation_command",
                return_value=implementation_path,
            ):
                with patch("sandcastle.runner.main.comment_on_issue") as comment_on_issue:
                    with patch(
                        "sandcastle.runner.main.branch_exists", return_value=True
                    ):
                        with patch(
                            "sandcastle.runner.main.branch_ahead_count", return_value=0
                        ):
                            with patch(
                                "sandcastle.runner.main.list_worktree_changed_files",
                                return_value=[
                                    ".sandcastle-agent/issue-270/implementation-brief.json"
                                ],
                            ):
                                with patch(
                                    "sandcastle.runner.main.remove_worktree"
                                ) as remove_worktree:
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
                                                exit_code = _handle_implement(
                                                    root,
                                                    paths,
                                                    _repo_config(),
                                                    _gates_config(),
                                                    270,
                                                )
            persisted = try_load_issue_status(paths, 270)

        self.assertEqual(exit_code, 0)
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted.status, "no-op")
        self.assertIsNone(persisted.branch)
        self.assertIsNone(persisted.worktree_path)
        self.assertEqual(persisted.extra["implementation_status"], "no-op")
        self.assertEqual(
            persisted.extra["no_op_reason"],
            "implementation command exited 0 but produced no meaningful code changes",
        )
        comment_on_issue.assert_called_once()
        remove_worktree.assert_called_once_with(root, str(worktree_path))
        delete_local_branch.assert_called_once_with(root, "sandcastle/issue-270")
        delete_remote_branch.assert_called_once_with(
            root, "origin", "sandcastle/issue-270"
        )

    def test_zero_change_success_preserves_local_state_when_worktree_has_real_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            worktree_path = root / ".sandcastle-worktrees" / "issue-271"
            worktree_path.mkdir(parents=True)
            _write_no_op_template(paths)
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=271,
                    status="branch-created",
                    branch="sandcastle/issue-271",
                    worktree_path=str(worktree_path),
                    extra={"issue_title": "Issue 271"},
                ),
            )
            implementation_path = write_json_file(
                paths.implementation_dir / "271.json",
                {
                    "status": "succeeded",
                    "returncode": 0,
                    "pre_command_head_sha": "abc",
                    "post_command_head_sha": "abc",
                    "changed_files": [
                        ".sandcastle-agent/issue-271/implementation-brief.json"
                    ],
                    "diff_summary": "",
                },
            )

            with patch(
                "sandcastle.runner.main.run_implementation_command",
                return_value=implementation_path,
            ):
                with patch("sandcastle.runner.main.comment_on_issue"):
                    with patch(
                        "sandcastle.runner.main.branch_exists", return_value=True
                    ):
                        with patch(
                            "sandcastle.runner.main.branch_ahead_count", return_value=0
                        ):
                            with patch(
                                "sandcastle.runner.main.list_worktree_changed_files",
                                return_value=["notes.txt"],
                            ):
                                with patch(
                                    "sandcastle.runner.main.remove_worktree"
                                ) as remove_worktree:
                                    with patch(
                                        "sandcastle.runner.main.delete_local_branch"
                                    ) as delete_local_branch:
                                        with patch(
                                            "sandcastle.runner.main.delete_remote_branch"
                                        ) as delete_remote_branch:
                                            exit_code = _handle_implement(
                                                root,
                                                paths,
                                                _repo_config(),
                                                _gates_config(),
                                                271,
                                            )
            persisted = try_load_issue_status(paths, 271)

        self.assertEqual(exit_code, 0)
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted.status, "no-op")
        self.assertEqual(persisted.branch, "sandcastle/issue-271")
        self.assertEqual(persisted.worktree_path, str(worktree_path))
        self.assertIn(
            "still has non-runtime local changes",
            "\n".join(persisted.extra["cleanup_actions"]),
        )
        remove_worktree.assert_not_called()
        delete_local_branch.assert_not_called()
        delete_remote_branch.assert_not_called()

    def test_successful_review_fix_commit_is_owned_and_pushed_by_sandcastle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            worktree_path = root / ".sandcastle-worktrees" / "issue-270"
            worktree_path.mkdir(parents=True)
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=270,
                    status="review-fix-loop",
                    branch="sandcastle/issue-270",
                    worktree_path=str(worktree_path),
                    pr_number=31,
                    extra={
                        "issue_title": "Issue 270",
                        "findings_path": str(paths.findings_dir / "270.json"),
                        "review_fix_attempt_count": 1,
                    },
                ),
            )
            implementation_path = write_json_file(
                paths.implementation_dir / "270.json",
                {
                    "status": "succeeded",
                    "returncode": 0,
                    "pre_command_head_sha": "abc",
                    "post_command_head_sha": "abc",
                    "changed_files": ["sandcastle/runner/main.py"],
                    "diff_summary": " sandcastle/runner/main.py | 12 ++++++++++--",
                },
            )

            with patch(
                "sandcastle.runner.main.run_implementation_command",
                return_value=implementation_path,
            ):
                with patch(
                    "sandcastle.runner.main.list_worktree_changed_files",
                    return_value=["sandcastle/runner/main.py", ".sandcastle-agent/issue-270/implementation-brief.json"],
                ):
                    with patch(
                        "sandcastle.runner.main.stage_paths"
                    ) as stage_paths:
                        with patch(
                            "sandcastle.runner.main.commit_staged_changes"
                        ) as commit_changes:
                            with patch(
                                "sandcastle.runner.main.current_head_sha",
                                return_value="def",
                            ):
                                with patch(
                                    "sandcastle.runner.main.push_branch"
                                ) as push_branch:
                                    exit_code = _handle_implement(
                                        root, paths, _repo_config(), _gates_config(), 270
                                    )
            persisted = try_load_issue_status(paths, 270)

        self.assertEqual(exit_code, 0)
        stage_paths.assert_called_once_with(
            str(worktree_path), ["sandcastle/runner/main.py"]
        )
        commit_changes.assert_called_once()
        push_branch.assert_called_once_with(root, "origin", "sandcastle/issue-270")
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted.status, "implementing")
        self.assertEqual(
            persisted.extra["implementation_changed_files"],
            ["sandcastle/runner/main.py"],
        )
        self.assertEqual(persisted.extra["implementation_head_sha"], "def")
        self.assertTrue(persisted.extra["implementation_runner_committed"])
        self.assertTrue(persisted.extra["implementation_runner_pushed"])
        self.assertEqual(persisted.extra["review_fix_attempt_count"], 2)

    def test_review_fix_retry_cap_blocks_before_new_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            worktree_path = root / ".sandcastle-worktrees" / "issue-270"
            worktree_path.mkdir(parents=True)
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=270,
                    status="review-fix-loop",
                    branch="sandcastle/issue-270",
                    worktree_path=str(worktree_path),
                    extra={
                        "issue_title": "Issue 270",
                        "findings_path": str(paths.findings_dir / "270.json"),
                        "review_fix_attempt_count": 3,
                    },
                ),
            )

            with patch(
                "sandcastle.runner.main.run_implementation_command"
            ) as run_command:
                exit_code = _handle_implement(
                    root, paths, _repo_config(), _gates_config(), 270
                )
            persisted = try_load_issue_status(paths, 270)

        self.assertEqual(exit_code, 1)
        run_command.assert_not_called()
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted.status, "blocked")
        self.assertIn("retry cap", str(persisted.extra["error"]))

    def test_repeated_findings_block_early(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=270,
                    status="implementing",
                    branch="sandcastle/issue-270",
                    worktree_path=str(root / ".sandcastle-worktrees" / "issue-270"),
                    extra={
                        "implementation_path": "C:/old-impl.json",
                        "findings_fingerprint": json.dumps(
                            [
                                {
                                    "details": "same failure",
                                    "kind": "check_failure",
                                    "path": "",
                                    "source": "ci",
                                    "summary": "CI still fails.",
                                }
                            ],
                            sort_keys=True,
                        ),
                        "findings_source_implementation_path": "C:/older-impl.json",
                    },
                ),
            )
            findings_path = write_json_file(
                paths.findings_dir / "270.json",
                {
                    "issue_number": 270,
                    "status": "findings",
                    "findings": [
                        {
                            "source": "ci",
                            "kind": "check_failure",
                            "summary": "CI still fails.",
                            "details": "same failure",
                        }
                    ],
                },
            )

            output = io.StringIO()
            with patch(
                "sandcastle.runner.main.collect_findings",
                return_value=findings_path,
            ):
                with redirect_stdout(output):
                    exit_code = _handle_collect_findings(paths, _repo_config(), 270)
            persisted = try_load_issue_status(paths, 270)

        self.assertEqual(exit_code, 0)
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted.status, "blocked")
        self.assertIn("repeated unchanged", str(persisted.extra["error"]))


if __name__ == "__main__":
    unittest.main()
