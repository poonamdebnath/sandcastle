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
    _can_pick_new_issue,
    _count_active_work,
    _handle_auto_step,
    _handle_discover,
    _handle_resume,
    _handle_run_issue,
    _handle_status,
    _select_backlog_issue,
)
from sandcastle.runner.models import (
    BacklogSelection,
    ClaimPolicy,
    CompletionPolicy,
    HandoffPolicy,
    IssueDependency,
    IssueCandidate,
    IssuePolicy,
    IssueStatus,
    PullOrderPolicy,
    RepoConfig,
    SandcastlePaths,
    SchedulerPolicy,
    WorktreePolicy,
    ImplementationPolicy,
    CleanupPolicy,
    GithubPolicy,
)
from sandcastle.runner.state_store import try_load_issue_status, write_issue_status


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
            mode="serial-active-slot",
            max_active_issues=3,
            max_waiting_for_human=3,
            lease_timeout_seconds=1800,
            lease_refresh_interval_seconds=60,
            lease_lock_timeout_seconds=30,
        ),
        implementation=ImplementationPolicy(enabled=True, command=("python",)),
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


class MainIssueSelectionTests(unittest.TestCase):
    def test_serial_active_slot_only_counts_code_mutating_states(self) -> None:
        statuses = [
            IssueStatus(issue_number=289, status="waiting-human-review"),
            IssueStatus(issue_number=290, status="review-fix-loop"),
            IssueStatus(issue_number=291, status="implementing"),
        ]

        self.assertEqual(_count_active_work(statuses), 1)
        self.assertTrue(
            _can_pick_new_issue(
                [IssueStatus(issue_number=289, status="waiting-human-review")],
                SchedulerPolicy(
                    mode="serial-active-slot",
                    max_active_issues=3,
                    max_waiting_for_human=3,
                    lease_timeout_seconds=1800,
                    lease_refresh_interval_seconds=60,
                    lease_lock_timeout_seconds=30,
                ),
            )
        )

    def test_discover_reports_hold_as_ineligible_reason(self) -> None:
        issue = IssueCandidate(
            number=269,
            title="Held issue",
            state="OPEN",
            labels=("ready-for-agent", "type:dev", "sandcastle:hold"),
            assignees=(),
            url="https://example.test/issues/269",
        )
        output = io.StringIO()

        with patch("sandcastle.runner.main.list_open_issues", return_value=[issue]):
            with redirect_stdout(output):
                exit_code = _handle_discover(
                    Path("C:/repo"),
                    _issue_policy(),
                    SimpleNamespace(base_ref="origin/main"),
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(
            payload["issues"],
            [
                {
                    "number": 269,
                    "title": "Held issue",
                    "eligible": False,
                    "reasons": ["has hold label: sandcastle:hold"],
                    "labels": ["ready-for-agent", "type:dev", "sandcastle:hold"],
                    "assignees": [],
                    "url": "https://example.test/issues/269",
                    "base_ref": "origin/main",
                    "dependencies": {
                        "source": "none",
                        "blocking_issue_numbers": [],
                        "dependency_error": None,
                    },
                    "pickup_policy": {"held": True, "priority": "normal", "priority_rank": 1},
                }
            ],
        )

    def test_discover_reports_dependency_blockers(self) -> None:
        issue = IssueCandidate(
            number=280,
            title="Dependency-aware pickup",
            state="OPEN",
            labels=("ready-for-agent", "type:dev"),
            assignees=(),
            url="https://example.test/issues/280",
            dependency_source="native",
            blocked_by=(
                IssueDependency(
                    number=278,
                    title="Earlier task",
                    state="OPEN",
                    merged=False,
                ),
                IssueDependency(
                    number=279,
                    title="Merged task",
                    state="OPEN",
                    merged=True,
                ),
            ),
        )
        output = io.StringIO()

        with patch("sandcastle.runner.main.list_open_issues", return_value=[issue]):
            with redirect_stdout(output):
                exit_code = _handle_discover(
                    Path("C:/repo"),
                    _issue_policy(),
                    SimpleNamespace(base_ref="origin/main"),
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["issues"][0]["eligible"], False)
        self.assertEqual(
            payload["issues"][0]["reasons"],
            ["waiting for predecessor issues to merge: #278"],
        )
        self.assertEqual(
            payload["issues"][0]["dependencies"],
            {
                "source": "native",
                "blocking_issue_numbers": [278],
                "dependency_error": None,
            },
        )

    def test_backlog_selection_tracks_hold_only_issues_separately(self) -> None:
        held = IssueCandidate(
            number=269,
            title="Held issue",
            state="OPEN",
            labels=("ready-for-agent", "type:dev", "sandcastle:hold"),
            assignees=(),
            url="https://example.test/issues/269",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _paths(Path(temp_dir))
            with patch("sandcastle.runner.main.list_open_issues", return_value=[held]):
                selection = _select_backlog_issue(Path(temp_dir), paths, _issue_policy())

        self.assertIsNone(selection.selected_issue)
        self.assertEqual(selection.held_issue_numbers, (269,))
        self.assertEqual(selection.reordered_issue_numbers, ())

    def test_run_issue_refuses_held_ticket_before_claiming(self) -> None:
        issue = IssueCandidate(
            number=269,
            title="Held issue",
            state="OPEN",
            labels=("ready-for-agent", "type:dev", "sandcastle:hold"),
            assignees=(),
            url="https://example.test/issues/269",
        )
        output = io.StringIO()

        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _paths(Path(temp_dir))
            with patch("sandcastle.runner.main.get_issue", return_value=issue):
                with patch("sandcastle.runner.main._claim_issue") as claim_issue:
                    with redirect_stdout(output):
                        exit_code = _handle_run_issue(
                            Path(temp_dir),
                            paths,
                            _issue_policy(),
                            SimpleNamespace(),
                            269,
                        )

        self.assertEqual(exit_code, 1)
        claim_issue.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["error"], "issue is not eligible for Sandcastle pickup")
        self.assertEqual(payload["issue_number"], 269)
        self.assertEqual(payload["reasons"], ["has hold label: sandcastle:hold"])

    def test_auto_step_records_resume_priority_before_new_backlog_pickup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=289,
                    status="review-fix-loop",
                    branch="sandcastle/issue-289",
                    worktree_path=str(root / ".sandcastle-worktrees" / "issue-289"),
                    extra={"issue_title": "Resume me"},
                ),
            )
            backlog = BacklogSelection(
                selected_issue=IssueCandidate(
                    number=290,
                    title="Fresh issue",
                    state="OPEN",
                    labels=("ready-for-agent", "type:dev"),
                    assignees=(),
                    url="https://example.test/issues/290",
                )
            )
            repo_config = RepoConfig(
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
                    mode="serial-active-slot",
                    max_active_issues=3,
                    max_waiting_for_human=3,
                    lease_timeout_seconds=1800,
                    lease_refresh_interval_seconds=60,
                    lease_lock_timeout_seconds=30,
                ),
                implementation=ImplementationPolicy(enabled=True, command=("python",)),
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
            with patch("sandcastle.runner.main._select_backlog_issue", return_value=backlog):
                with patch("sandcastle.runner.main._handle_resume_sync", return_value=None):
                    with patch("sandcastle.runner.main._handle_revise", return_value=0) as revise:
                        exit_code = _handle_auto_step(
                            root,
                            paths,
                            _issue_policy(),
                            repo_config,
                            SimpleNamespace(),
                        )

            self.assertEqual(exit_code, 0)
            revise.assert_called_once_with(paths, 289)
            persisted = try_load_issue_status(paths, 289)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(
                persisted.extra["resume_priority_reason"],
                "existing branch regained priority ahead of fresh backlog pickup",
            )
            self.assertEqual(persisted.extra["resumed_ahead_of_issue_number"], 290)

    def test_auto_step_picks_new_backlog_issue_when_another_issue_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            output = io.StringIO()
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=289,
                    status="blocked",
                    branch="sandcastle/issue-289",
                    worktree_path=str(root / ".sandcastle-worktrees" / "issue-289"),
                    extra={
                        "issue_title": "Blocked issue",
                        "blocked_reason": "needs human clarification",
                        "blocked_next_action": "Clarify the expected behavior before resuming.",
                    },
                ),
            )
            backlog = BacklogSelection(
                selected_issue=IssueCandidate(
                    number=290,
                    title="Fresh issue",
                    state="OPEN",
                    labels=("ready-for-agent", "type:dev"),
                    assignees=(),
                    url="https://example.test/issues/290",
                )
            )

            def fake_run_issue(*_args, **_kwargs) -> int:
                write_issue_status(
                    paths,
                    IssueStatus(
                        issue_number=290,
                        status="claimed",
                        branch="sandcastle/issue-290-fresh-issue",
                        worktree_path=str(root / ".sandcastle-worktrees" / "issue-290"),
                        extra={"issue_title": "Fresh issue"},
                    ),
                )
                print(
                    json.dumps(
                        {
                            "status": "claimed",
                            "issue_number": 290,
                            "operator_state": "active",
                        }
                    )
                )
                return 0

            with patch(
                "sandcastle.runner.main._is_failed_startup_cleanup_candidate",
                return_value=False,
            ):
                with patch(
                    "sandcastle.runner.main._select_backlog_issue",
                    return_value=backlog,
                ):
                    with patch(
                        "sandcastle.runner.main._handle_run_issue",
                        side_effect=fake_run_issue,
                    ):
                        with redirect_stdout(output):
                            exit_code = _handle_auto_step(
                                root,
                                paths,
                                _issue_policy(),
                                _repo_config(),
                                SimpleNamespace(),
                            )

            self.assertEqual(exit_code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["issue_number"], 290)
            self.assertEqual(payload["chosen_issue_number"], 290)
            self.assertEqual(payload["status"], "claimed")
            self.assertEqual(payload["blocked_issue_count"], 1)
            persisted = try_load_issue_status(paths, 290)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(persisted.status, "claimed")

    def test_status_reports_dependency_blockers(self) -> None:
        issue = IssueCandidate(
            number=280,
            title="Dependency-aware pickup",
            state="OPEN",
            labels=("ready-for-agent", "type:dev"),
            assignees=(),
            url="https://example.test/issues/280",
            dependency_source="body",
            blocked_by=(
                IssueDependency(
                    number=278,
                    title="Earlier task",
                    state="OPEN",
                    merged=False,
                ),
            ),
        )
        output = io.StringIO()

        with tempfile.TemporaryDirectory() as temp_dir:
            paths = _paths(Path(temp_dir))
            with patch("sandcastle.runner.main.get_issue", return_value=issue):
                with redirect_stdout(output):
                    exit_code = _handle_status(
                        Path(temp_dir),
                        paths,
                        _issue_policy(),
                        280,
                    )

        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["eligibility"]["eligible"], False)
        self.assertEqual(
            payload["eligibility"]["reasons"],
            ["waiting for predecessor issues to merge: #278"],
        )
        self.assertEqual(
            payload["eligibility"]["dependencies"],
            {
                "source": "body",
                "blocking_issue_numbers": [278],
                "dependency_error": None,
            },
        )

    def test_resume_blocks_when_pull_request_assignment_fails(self) -> None:
        issue = IssueCandidate(
            number=299,
            title="Configurable assignee",
            state="OPEN",
            labels=("ready-for-agent", "type:dev"),
            assignees=(),
            url="https://example.test/issues/299",
        )
        output = io.StringIO()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            worktree_path = root / ".sandcastle-worktrees" / "issue-299"
            worktree_path.mkdir(parents=True)
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=299,
                    status="ready-for-pr",
                    branch="sandcastle/issue-299",
                    worktree_path=str(worktree_path),
                    extra={"issue_title": issue.title},
                ),
            )
            blocked = IssueStatus(
                issue_number=299,
                status="blocked",
                branch="sandcastle/issue-299",
                worktree_path=str(worktree_path),
                pr_number=17,
                extra={"blocked_reason": "assignment failed"},
            )

            with patch("sandcastle.runner.main.get_issue", return_value=issue):
                with patch(
                    "sandcastle.runner.main.find_pr_for_head",
                    return_value=(17, "https://example.test/pr/17"),
                ):
                    with patch(
                        "sandcastle.runner.main.get_pr",
                        return_value={
                            "number": 17,
                            "url": "https://example.test/pr/17",
                            "assignees": {"nodes": []},
                        },
                    ):
                        with patch(
                            "sandcastle.runner.main.ensure_pull_request_assignee",
                            side_effect=RuntimeError("GraphQL: not assignable"),
                        ):
                            with patch(
                                "sandcastle.runner.main._block_issue",
                                return_value=blocked,
                            ) as block_issue:
                                with redirect_stdout(output):
                                    exit_code = _handle_resume(
                                        root,
                                        paths,
                                        _issue_policy(),
                                        _repo_config(),
                                        299,
                                    )

        self.assertEqual(exit_code, 1)
        block_issue.assert_called_once()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["pr_number"], 17)
        self.assertIn("not assignable", payload["error"])

    def test_resume_allows_recovering_blocked_pull_request_assignment(self) -> None:
        issue = IssueCandidate(
            number=299,
            title="Configurable assignee",
            state="OPEN",
            labels=("ready-for-agent", "type:dev"),
            assignees=(),
            url="https://example.test/issues/299",
        )
        output = io.StringIO()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            worktree_path = root / ".sandcastle-worktrees" / "issue-299"
            worktree_path.mkdir(parents=True)
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=299,
                    status="blocked",
                    branch="sandcastle/issue-299",
                    worktree_path=str(worktree_path),
                    pr_number=17,
                    extra={
                        "issue_title": issue.title,
                        "blocked_reason": "assignment failed",
                        "blocked_resume_kind": "pull-request-assignment",
                        "blocked_resume_status": "ready-for-pr",
                    },
                ),
            )

            with patch("sandcastle.runner.main.get_issue", return_value=issue):
                with patch(
                    "sandcastle.runner.main.find_pr_for_head",
                    return_value=(17, "https://example.test/pr/17"),
                ):
                    with patch(
                        "sandcastle.runner.main.get_pr",
                        return_value={
                            "number": 17,
                            "url": "https://example.test/pr/17",
                            "assignees": {"nodes": []},
                        },
                    ):
                        with patch(
                            "sandcastle.runner.main.ensure_pull_request_assignee",
                            return_value=None,
                        ) as ensure_assignment:
                            with redirect_stdout(output):
                                exit_code = _handle_resume(
                                    root,
                                    paths,
                                    _issue_policy(),
                                    _repo_config(),
                                    299,
                                )
            persisted = try_load_issue_status(paths, 299)

        self.assertEqual(exit_code, 0)
        ensure_assignment.assert_called_once()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "pr-open")
        self.assertEqual(payload["pr_number"], 17)
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted.status, "pr-open")
        self.assertEqual(persisted.pr_number, 17)

    def test_resume_passes_existing_pr_assignees_to_assignment_helper(self) -> None:
        issue = IssueCandidate(
            number=299,
            title="Configurable assignee",
            state="OPEN",
            labels=("ready-for-agent", "type:dev"),
            assignees=(),
            url="https://example.test/issues/299",
        )
        output = io.StringIO()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = _paths(root)
            worktree_path = root / ".sandcastle-worktrees" / "issue-299"
            worktree_path.mkdir(parents=True)
            write_issue_status(
                paths,
                IssueStatus(
                    issue_number=299,
                    status="ready-for-pr",
                    branch="sandcastle/issue-299",
                    worktree_path=str(worktree_path),
                    extra={"issue_title": issue.title},
                ),
            )

            with patch("sandcastle.runner.main.get_issue", return_value=issue):
                with patch(
                    "sandcastle.runner.main.find_pr_for_head",
                    return_value=(17, "https://example.test/pr/17"),
                ):
                    with patch(
                        "sandcastle.runner.main.get_pr",
                        return_value={
                            "number": 17,
                            "url": "https://example.test/pr/17",
                            "assignees": {"nodes": [{"login": "example-maintainer"}]},
                        },
                    ):
                        with patch(
                            "sandcastle.runner.main.ensure_pull_request_assignee",
                            return_value="already-assigned",
                        ) as ensure_assignment:
                            with redirect_stdout(output):
                                exit_code = _handle_resume(
                                    root,
                                    paths,
                                    _issue_policy(),
                                    _repo_config(),
                                    299,
                                )

        self.assertEqual(exit_code, 0)
        ensure_assignment.assert_called_once_with(
            root,
            pr_number=17,
            assignee_username="example-maintainer",
            current_assignees=("example-maintainer",),
        )
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "pr-open")
        self.assertEqual(payload["pr_number"], 17)


if __name__ == "__main__":
    unittest.main()
