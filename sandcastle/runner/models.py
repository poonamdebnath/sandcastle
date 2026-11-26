from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ClaimPolicy:
    assign_to_me: bool
    add_comment: bool
    comment_template: str
    overwrite_existing_assignees: bool = False


@dataclass(frozen=True)
class PullOrderPolicy:
    strategy: str
    max_issues_per_run: int
    hold_label: str
    priority_high_label: str
    priority_low_label: str


@dataclass(frozen=True)
class HandoffPolicy:
    comment_template: str
    write_local_note: bool
    local_note_template: str


@dataclass(frozen=True)
class CompletionPolicy:
    comment_template: str


@dataclass(frozen=True)
class IssuePolicy:
    required_labels: tuple[str, ...]
    forbidden_labels: tuple[str, ...]
    require_open_state: bool
    require_unassigned: bool
    allow_batches: bool
    claim: ClaimPolicy
    pull_order: PullOrderPolicy
    handoff: HandoffPolicy
    completion: CompletionPolicy


@dataclass(frozen=True)
class WorktreePolicy:
    enabled: bool
    root: str
    path_pattern: str


@dataclass(frozen=True)
class CleanupPolicy:
    close_issue_after_merge: bool
    remove_worktree_after_merge: bool
    delete_local_branch_after_merge: bool
    delete_remote_branch_after_merge: bool
    remove_worktree_after_failed_startup: bool
    delete_local_branch_after_failed_startup: bool
    delete_remote_branch_after_failed_startup: bool
    delete_runtime_state_after_failed_startup: bool
    remove_worktree_after_block: bool
    delete_local_branch_after_block: bool


@dataclass(frozen=True)
class GithubPolicy:
    issues_enabled: bool
    pull_requests_enabled: bool
    draft_pr_by_default: bool
    auto_ready_pr_after_checks: bool
    auto_merge_prs: bool
    merge_method: str
    assignee_username: str | None = None


@dataclass(frozen=True)
class SchedulerPolicy:
    mode: str
    max_active_issues: int
    max_waiting_for_human: int
    lease_timeout_seconds: int
    lease_refresh_interval_seconds: int
    lease_lock_timeout_seconds: int


@dataclass(frozen=True)
class ImplementationPolicy:
    enabled: bool
    command: tuple[str, ...]


@dataclass(frozen=True)
class RepoConfig:
    repo: str
    remote: str
    default_branch: str
    base_ref: str
    branch_prefix: str
    branch_name_pattern: str
    worktree: WorktreePolicy
    scheduler: SchedulerPolicy
    implementation: ImplementationPolicy
    cleanup: CleanupPolicy
    github: GithubPolicy


@dataclass(frozen=True)
class GateCommand:
    enabled: bool
    command: tuple[str, ...]


@dataclass(frozen=True)
class TestsPolicy:
    targeted_first: bool
    default_command: tuple[str, ...]
    escalate_to_full_suite_when: tuple[str, ...]


@dataclass(frozen=True)
class ReviewPolicy:
    required: bool
    must_be_clean_before_ready_for_human: bool
    max_revision_loops: int


@dataclass(frozen=True)
class BranchPolicy:
    one_issue_one_branch_until_merge: bool
    new_branch_from_fresh_main_after_merge: bool


@dataclass(frozen=True)
class BlockOnPolicy:
    format_failure: bool
    test_failure: bool
    unresolved_review_findings: bool
    unsafe_conflict_resolution: bool


@dataclass(frozen=True)
class GatesConfig:
    must_pass_before_pr: bool
    rerun_after_review_fixes: bool
    rerun_after_conflict_resolution: bool
    verification_order: tuple[str, ...]
    format: GateCommand
    tests: TestsPolicy
    review: ReviewPolicy
    branch_policy: BranchPolicy
    block_on: BlockOnPolicy


@dataclass(frozen=True)
class IssueDependency:
    number: int
    title: str | None = None
    state: str | None = None
    merged: bool | None = None


@dataclass(frozen=True)
class IssueCandidate:
    number: int
    title: str
    state: str
    labels: tuple[str, ...]
    assignees: tuple[str, ...]
    url: str | None = None
    body: str | None = None
    dependency_source: str = "none"
    blocked_by: tuple[IssueDependency, ...] = ()
    dependency_error: str | None = None


@dataclass(frozen=True)
class BacklogSelection:
    selected_issue: IssueCandidate | None
    selected_priority: str | None = None
    held_issue_numbers: tuple[int, ...] = ()
    reordered_issue_numbers: tuple[int, ...] = ()


@dataclass(frozen=True)
class EligibilityDecision:
    eligible: bool
    reasons: tuple[str, ...] = ()
    dependency_source: str = "none"
    blocking_issue_numbers: tuple[int, ...] = ()
    dependency_error: str | None = None


@dataclass(frozen=True)
class IssueStatus:
    issue_number: int
    status: str
    branch: str | None = None
    worktree_path: str | None = None
    pr_number: int | None = None
    last_updated_utc: str | None = None
    extra: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SandcastlePaths:
    root: Path
    config_dir: Path
    templates_dir: Path
    state_dir: Path
    leases_dir: Path
    issues_dir: Path
    contexts_dir: Path
    ci_dir: Path
    verification_dir: Path
    implementation_dir: Path
    conflict_dir: Path
    review_dir: Path
    findings_dir: Path
    revision_dir: Path
