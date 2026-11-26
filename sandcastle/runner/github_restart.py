from __future__ import annotations

import argparse
import json
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "classify":
        return _handle_classify(
            event_name=args.event_name,
            event_path=Path(args.event_path),
            output_path=Path(args.output_path),
            github_output=Path(args.github_output) if args.github_output else None,
        )
    if args.command == "consume":
        return _handle_consume(
            event_name=args.event_name,
            event_path=Path(args.event_path),
            output_path=Path(args.output_path),
        )

    parser.error(f"unknown command: {args.command}")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sandcastle-github-restart")
    subparsers = parser.add_subparsers(dest="command", required=True)

    classify = subparsers.add_parser(
        "classify", help="Normalize a GitHub event into a Sandcastle wakeup decision."
    )
    classify.add_argument("--event-name", required=True)
    classify.add_argument("--event-path", required=True)
    classify.add_argument("--output-path", required=True)
    classify.add_argument("--github-output")

    consume = subparsers.add_parser(
        "consume", help="Normalize a controller-entry GitHub event for logging."
    )
    consume.add_argument("--event-name", required=True)
    consume.add_argument("--event-path", required=True)
    consume.add_argument("--output-path", required=True)
    return parser


def _handle_classify(
    *,
    event_name: str,
    event_path: Path,
    output_path: Path,
    github_output: Path | None,
) -> int:
    payload = _read_json(event_path)
    decision = _classify_event(event_name, payload)
    output_path.write_text(json.dumps(decision, indent=2), encoding="utf-8")
    if github_output is not None:
        _append_output(
            github_output,
            "should_dispatch",
            "true" if decision["should_dispatch"] else "false",
        )
        _append_output(github_output, "wake_reason", str(decision["wake_reason"] or ""))
        _append_output(
            github_output,
            "client_payload_json",
            json.dumps(decision["client_payload"], separators=(",", ":")),
        )
    print(json.dumps(decision, indent=2))
    return 0


def _handle_consume(*, event_name: str, event_path: Path, output_path: Path) -> int:
    payload = _read_json(event_path)
    if event_name == "repository_dispatch":
        client_payload = payload.get("client_payload", {})
    else:
        client_payload = _classify_event(event_name, payload)["client_payload"]

    summary = {
        "event_name": event_name,
        "wake_reason": client_payload.get("wake_reason"),
        "trigger_event": client_payload.get("trigger_event", event_name),
        "trigger_action": client_payload.get("trigger_action"),
        "pull_request_number": client_payload.get("pull_request_number"),
        "issue_number": client_payload.get("issue_number"),
        "head_branch": client_payload.get("head_branch"),
        "base_branch": client_payload.get("base_branch"),
        "repository": client_payload.get("repository"),
        "note": (
            "GitHub wakeup reached the Sandcastle controller entrypoint. "
            "The controller can now run a bounded Sandcastle loop on the configured unattended backend."
        ),
    }
    output_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


def _classify_event(event_name: str, payload: dict[str, object]) -> dict[str, object]:
    action = str(payload.get("action", "") or "")
    repository = payload.get("repository", {})
    if not isinstance(repository, dict):
        repository = {}

    pull_request = payload.get("pull_request", {})
    if not isinstance(pull_request, dict):
        pull_request = {}

    issue = payload.get("issue", {})
    if not isinstance(issue, dict):
        issue = {}

    review = payload.get("review", {})
    if not isinstance(review, dict):
        review = {}

    workflow_run = payload.get("workflow_run", {})
    if not isinstance(workflow_run, dict):
        workflow_run = {}

    wake_reason: str | None = None
    if event_name == "workflow_dispatch":
        wake_reason = "manual-dispatch"
    elif event_name == "schedule":
        wake_reason = "scheduled-poll"
    elif event_name == "workflow_run":
        workflow_name = str(workflow_run.get("name", "") or "")
        workflow_event = str(workflow_run.get("event", "") or "")
        workflow_conclusion = str(workflow_run.get("conclusion", "") or "").lower()
        if action == "completed" and workflow_name == "CI" and workflow_event == "pull_request":
            wake_reason = f"workflow-run-ci-{workflow_conclusion or 'completed'}"
    elif event_name == "pull_request_review":
        review_state = str(review.get("state", "") or "").upper()
        if action == "submitted" and review_state in {
            "APPROVED",
            "CHANGES_REQUESTED",
            "COMMENTED",
        }:
            wake_reason = f"pull-request-review-{review_state.lower()}"
    elif event_name == "pull_request_review_comment":
        if action == "created":
            wake_reason = "pull-request-review-comment-created"
    elif event_name == "issue_comment":
        if action == "created" and isinstance(issue.get("pull_request"), dict):
            wake_reason = "pull-request-conversation-comment-created"
    elif event_name == "pull_request":
        if action == "closed" and bool(pull_request.get("merged")):
            wake_reason = "pull-request-merged"
        elif action in {"reopened", "ready_for_review", "review_requested"}:
            wake_reason = f"pull-request-{action.replace('_', '-')}"

    # repository_dispatch allows at most 10 top-level client_payload fields.
    client_payload = {
        "wake_reason": wake_reason,
        "trigger_event": event_name,
        "trigger_action": action or None,
        "repository": repository.get("full_name")
        or _nested_str(workflow_run, "repository", "full_name"),
        "pull_request_number": _optional_int(pull_request.get("number")) or _workflow_run_pull_request_number(workflow_run),
        "issue_number": _optional_int(issue.get("number")),
        "head_branch": _nested_str(pull_request, "head", "ref") or _optional_str(workflow_run.get("head_branch")),
        "base_branch": _nested_str(pull_request, "base", "ref") or _workflow_run_base_branch(workflow_run),
        "merged": bool(pull_request.get("merged")),
        "sender": _nested_str(payload, "sender", "login"),
    }
    return {
        "should_dispatch": wake_reason is not None,
        "wake_reason": wake_reason,
        "client_payload": client_payload,
    }


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"expected JSON object in {path}")
    return payload


def _append_output(path: Path, name: str, value: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{name}<<__SANDCASTLE__\n")
        handle.write(value)
        handle.write("\n__SANDCASTLE__\n")


def _nested_str(payload: dict[str, object], *keys: str) -> str | None:
    current: object = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if current is None:
        return None
    return str(current)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _workflow_run_pull_request_number(workflow_run: dict[str, object]) -> int | None:
    pull_requests = workflow_run.get("pull_requests", [])
    if not isinstance(pull_requests, list) or not pull_requests:
        return None
    first = pull_requests[0]
    if not isinstance(first, dict):
        return None
    return _optional_int(first.get("number"))


def _workflow_run_base_branch(workflow_run: dict[str, object]) -> str | None:
    pull_requests = workflow_run.get("pull_requests", [])
    if not isinstance(pull_requests, list) or not pull_requests:
        return None
    first = pull_requests[0]
    if not isinstance(first, dict):
        return None
    base = first.get("base")
    if not isinstance(base, dict):
        return None
    return _optional_str(base.get("ref"))


if __name__ == "__main__":
    raise SystemExit(main())
