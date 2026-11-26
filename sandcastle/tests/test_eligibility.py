from __future__ import annotations

import unittest

from sandcastle.runner.eligibility import evaluate_issue
from sandcastle.runner.models import (
    ClaimPolicy,
    CompletionPolicy,
    HandoffPolicy,
    IssueDependency,
    IssueCandidate,
    IssuePolicy,
    PullOrderPolicy,
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


class EligibilityTests(unittest.TestCase):
    def test_hold_label_makes_issue_ineligible(self) -> None:
        decision = evaluate_issue(
            IssueCandidate(
                number=269,
                title="Held issue",
                state="OPEN",
                labels=("ready-for-agent", "type:dev", "sandcastle:hold"),
                assignees=(),
                url="https://example.test/issues/269",
            ),
            _issue_policy(),
        )

        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reasons, ("has hold label: sandcastle:hold",))

    def test_unmerged_predecessor_makes_issue_ineligible(self) -> None:
        decision = evaluate_issue(
            IssueCandidate(
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
                        title="Parent task",
                        state="OPEN",
                        merged=False,
                    ),
                ),
            ),
            _issue_policy(),
        )

        self.assertFalse(decision.eligible)
        self.assertEqual(
            decision.reasons,
            ("waiting for predecessor issues to merge: #278",),
        )
        self.assertEqual(decision.blocking_issue_numbers, (278,))
        self.assertEqual(decision.dependency_source, "native")

    def test_dependency_state_error_blocks_issue(self) -> None:
        decision = evaluate_issue(
            IssueCandidate(
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
                        title="Parent task",
                        state=None,
                        merged=None,
                    ),
                ),
                dependency_error="unable to confirm whether predecessor issues are merged to main",
            ),
            _issue_policy(),
        )

        self.assertFalse(decision.eligible)
        self.assertEqual(
            decision.reasons,
            (
                "waiting for predecessor issues to merge: #278 "
                "(unable to confirm whether predecessor issues are merged to main)",
            ),
        )
        self.assertEqual(decision.blocking_issue_numbers, (278,))


if __name__ == "__main__":
    unittest.main()
