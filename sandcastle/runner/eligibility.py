from __future__ import annotations

from dataclasses import replace

from .models import EligibilityDecision, IssueCandidate, IssuePolicy


def evaluate_issue(issue: IssueCandidate, policy: IssuePolicy) -> EligibilityDecision:
    reasons: list[str] = []
    labels = set(issue.labels)
    hold_label = policy.pull_order.hold_label.strip()
    blocking_issue_numbers = tuple(
        sorted(
            dependency.number
            for dependency in issue.blocked_by
            if dependency.merged is not True
        )
    )

    if policy.require_open_state and issue.state.upper() != "OPEN":
        reasons.append(f"state is {issue.state!r}, expected 'OPEN'")

    missing_required = sorted(set(policy.required_labels) - labels)
    if missing_required:
        reasons.append(f"missing required labels: {', '.join(missing_required)}")

    matched_forbidden = sorted(set(policy.forbidden_labels) & labels)
    if matched_forbidden:
        reasons.append(f"has forbidden labels: {', '.join(matched_forbidden)}")

    if hold_label and hold_label in labels:
        reasons.append(f"has hold label: {hold_label}")

    if policy.require_unassigned and issue.assignees:
        reasons.append("issue is already assigned")

    if blocking_issue_numbers:
        numbers = ", ".join(f"#{number}" for number in blocking_issue_numbers)
        if issue.dependency_error:
            reasons.append(
                "waiting for predecessor issues to merge: "
                f"{numbers} ({issue.dependency_error})"
            )
        else:
            reasons.append(f"waiting for predecessor issues to merge: {numbers}")

    return EligibilityDecision(
        eligible=not reasons,
        reasons=tuple(reasons),
        dependency_source=issue.dependency_source,
        blocking_issue_numbers=blocking_issue_numbers,
        dependency_error=issue.dependency_error,
    )


def is_held_only_by_label_policy(issue: IssueCandidate, policy: IssuePolicy) -> bool:
    hold_label = policy.pull_order.hold_label.strip()
    if not hold_label or hold_label not in set(issue.labels):
        return False

    labels_without_hold = tuple(label for label in issue.labels if label != hold_label)
    decision_without_hold = evaluate_issue(
        replace(issue, labels=labels_without_hold),
        policy,
    )
    return decision_without_hold.eligible
