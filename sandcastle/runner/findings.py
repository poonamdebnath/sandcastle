from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .state_store import utc_now_iso, write_json_file


def collect_findings(
    *,
    repo_root: Path,
    repo_name: str,
    findings_dir: Path,
    issue_number: int,
    pr_number: int | None,
    verification_payload: dict[str, object] | None,
    ci_payload: dict[str, object] | None = None,
) -> Path:
    findings: list[dict[str, object]] = []

    if verification_payload is not None:
        findings.extend(_verification_findings(verification_payload))

    if ci_payload is not None:
        findings.extend(_ci_findings(ci_payload))

    pr_discussion = None
    review_comments = None
    if pr_number is not None:
        pr_discussion = _fetch_pr_discussion(repo_root, pr_number)
        review_comments = _fetch_pr_review_comments(repo_root, repo_name, pr_number)
        findings.extend(_discussion_findings(pr_discussion, review_comments))

    payload = {
        "issue_number": issue_number,
        "pr_number": pr_number,
        "status": "findings" if findings else "clean",
        "findings": findings,
        "ci": ci_payload,
        "pr_discussion": pr_discussion,
        "review_comments": review_comments,
        "written_at_utc": utc_now_iso(),
    }
    return write_json_file(findings_dir / f"{issue_number}.json", payload)


def _verification_findings(
    verification_payload: dict[str, object],
) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    if verification_payload.get("status") == "passed":
        return findings

    steps = verification_payload.get("steps", [])
    if not isinstance(steps, list):
        steps = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        returncode = int(step.get("returncode", 0))
        if returncode == 0:
            continue
        findings.append(
            {
                "source": "verification",
                "severity": "high",
                "kind": step.get("name", "verification"),
                "summary": f"Verification step '{step.get('name', 'unknown')}' failed.",
                "details": (step.get("stderr") or step.get("stdout") or "").strip()[
                    :4000
                ],
            }
        )
    return findings


def _ci_findings(ci_payload: dict[str, object]) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    status = str(ci_payload.get("status", ""))
    if status == "passed":
        return findings

    if status == "timed_out":
        findings.append(
            {
                "source": "ci",
                "severity": "high",
                "kind": "timeout",
                "summary": "PR CI did not complete before the Sandcastle timeout.",
                "details": "CI polling timed out before all required checks completed.",
            }
        )
        return findings

    failed_checks = ci_payload.get("failed_checks", [])
    if not isinstance(failed_checks, list):
        failed_checks = []
    for check in failed_checks:
        if not isinstance(check, dict):
            continue
        name = str(check.get("name", "check"))
        workflow_name = str(check.get("workflow_name", "")).strip()
        prefix = f"{workflow_name} / " if workflow_name else ""
        findings.append(
            {
                "source": "ci",
                "severity": "high",
                "kind": "check_failure",
                "summary": f"CI check '{prefix}{name}' failed.",
                "details": str(
                    check.get("log_excerpt", "") or check.get("details_url", "")
                )[:4000],
            }
        )
    return findings


def _discussion_findings(
    pr_discussion: dict[str, object] | None,
    review_comments: list[dict[str, object]] | None,
) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []

    if isinstance(pr_discussion, dict):
        for review in pr_discussion.get("reviews", []):
            state = str(review.get("state", "")).upper()
            if state not in {"CHANGES_REQUESTED", "COMMENTED"}:
                continue
            body = str(review.get("body", "")).strip()
            if not body:
                continue
            findings.append(
                {
                    "source": "pr_review",
                    "severity": "medium" if state == "COMMENTED" else "high",
                    "kind": state.lower(),
                    "summary": f"PR review is {state.lower().replace('_', ' ')}.",
                    "details": body[:4000],
                    "author": review.get("author"),
                }
            )

        for comment in pr_discussion.get("comments", []):
            body = str(comment.get("body", "")).strip()
            if not body:
                continue
            findings.append(
                {
                    "source": "pr_comment",
                    "severity": "medium",
                    "kind": "comment",
                    "summary": "PR conversation comment may require action.",
                    "details": body[:4000],
                    "author": comment.get("author"),
                }
            )

    if isinstance(review_comments, list):
        for comment in review_comments:
            body = str(comment.get("body", "")).strip()
            if not body:
                continue
            findings.append(
                {
                    "source": "pr_review_comment",
                    "severity": "medium",
                    "kind": "review_comment",
                    "summary": "PR review comment may require action.",
                    "details": body[:4000],
                    "author": comment.get("user", {}).get("login")
                    if isinstance(comment.get("user"), dict)
                    else None,
                    "path": comment.get("path"),
                }
            )

    return findings


def _fetch_pr_discussion(repo_root: Path, pr_number: int) -> dict[str, object]:
    command = [
        "gh",
        "pr",
        "view",
        str(pr_number),
        "--json",
        "comments,reviews",
    ]
    result = subprocess.run(
        command,
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    comments = []
    for item in payload.get("comments", []):
        if not isinstance(item, dict):
            continue
        author = item.get("author")
        comments.append(
            {
                "author": author.get("login") if isinstance(author, dict) else None,
                "body": item.get("body", ""),
                "createdAt": item.get("createdAt"),
            }
        )
    reviews = []
    for item in payload.get("reviews", []):
        if not isinstance(item, dict):
            continue
        author = item.get("author")
        reviews.append(
            {
                "author": author.get("login") if isinstance(author, dict) else None,
                "body": item.get("body", ""),
                "state": item.get("state"),
                "submittedAt": item.get("submittedAt"),
            }
        )
    return {"comments": comments, "reviews": reviews}


def _fetch_pr_review_comments(
    repo_root: Path, repo_name: str, pr_number: int
) -> list[dict[str, object]]:
    command = [
        "gh",
        "api",
        f"repos/{repo_name}/pulls/{pr_number}/comments",
    ]
    result = subprocess.run(
        command,
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    if isinstance(payload, list):
        return payload
    return []
