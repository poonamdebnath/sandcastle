from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .agent_profiles import build_agent_profile_env, load_agent_profile
from .state_store import utc_now_iso, write_json_file
from .worktree import merge_base_sha

REVIEW_AGENT_COMMAND_ENV_VAR = "SANDCASTLE_REVIEW_AGENT_COMMAND_JSON"
DEFAULT_REVIEW_BASE_REF = "origin/main"


def write_review_result(
    review_dir: Path,
    *,
    issue_number: int,
    branch: str,
    verification_status: str | None,
    implementation_path: str | None,
    worktree_path: str | None = None,
    pr_number: int | None = None,
    issue_title: str | None = None,
    context_path: str | None = None,
    issue_url: str | None = None,
    base_ref: str = DEFAULT_REVIEW_BASE_REF,
) -> Path:
    if not worktree_path:
        raise RuntimeError("review phase requires a worktree path")

    repo_root = Path(worktree_path)
    agent_profile = load_agent_profile("review-agent", repo_root)
    review_fixed_point = resolve_review_fixed_point(
        worktree_path,
        base_ref=base_ref,
    )
    support_dir = Path(worktree_path) / ".sandcastle-agent" / f"issue-{issue_number}"
    support_dir.mkdir(parents=True, exist_ok=True)
    review_brief_path = support_dir / "review-brief.json"
    review_result_path = support_dir / "review-result.json"
    review_instructions_path = support_dir / "review-instructions.md"

    brief = {
        "issue_number": issue_number,
        "issue_title": issue_title,
        "branch": branch,
        "worktree_path": worktree_path,
        "pr_number": pr_number,
        "issue_url": issue_url,
        "context_path": context_path,
        "implementation_path": implementation_path,
        "verification_status": verification_status,
        "review_base_ref": base_ref,
        "review_fixed_point": review_fixed_point,
        "review_result_path": str(review_result_path),
        "agent_profile": {
            "role": agent_profile.role_name,
            "profile": agent_profile.profile_name,
            "model": agent_profile.model,
            "reasoning_effort": agent_profile.reasoning_effort,
        },
    }
    review_brief_path.write_text(json.dumps(brief, indent=2) + "\n", encoding="utf-8")
    review_instructions_path.write_text(
        _render_review_instructions(
            issue_number=issue_number,
            issue_title=issue_title,
            branch=branch,
            worktree_path=worktree_path,
            pr_number=pr_number,
            issue_url=issue_url,
            context_path=context_path,
            implementation_path=implementation_path,
            verification_status=verification_status,
            review_base_ref=base_ref,
            review_fixed_point=review_fixed_point,
            review_result_path=str(review_result_path),
        ),
        encoding="utf-8",
    )

    command = _resolve_review_agent_command(
        issue_number=issue_number,
        branch=branch,
        worktree_path=worktree_path,
        issue_title=issue_title or "",
        pr_number=pr_number or "",
        issue_url=issue_url or "",
        context_path=context_path or "",
        implementation_path=implementation_path or "",
        verification_status=verification_status or "",
        review_base_ref=base_ref,
        review_fixed_point=review_fixed_point,
        review_brief_path=str(review_brief_path),
        review_result_path=str(review_result_path),
        review_instructions_path=str(review_instructions_path),
        agent_role=agent_profile.role_name,
        agent_profile=agent_profile.profile_name,
        agent_model=agent_profile.model,
        agent_reasoning_effort=agent_profile.reasoning_effort,
    )
    env = _build_review_agent_env(
        repo_root=repo_root,
        issue_number=issue_number,
        branch=branch,
        worktree_path=worktree_path,
        issue_title=issue_title or "",
        pr_number=pr_number or "",
        issue_url=issue_url or "",
        context_path=context_path or "",
        implementation_path=implementation_path or "",
        verification_status=verification_status or "",
        review_base_ref=base_ref,
        review_fixed_point=review_fixed_point,
        review_brief_path=str(review_brief_path),
        review_result_path=str(review_result_path),
        review_instructions_path=str(review_instructions_path),
    )
    started_at = utc_now_iso()
    result = subprocess.run(
        command,
        cwd=Path(worktree_path),
        check=False,
        env=env,
        capture_output=True,
        text=True,
    )
    payload = _load_review_payload(
        review_result_path=review_result_path,
        issue_number=issue_number,
        branch=branch,
        implementation_path=implementation_path,
        verification_status=verification_status,
        command=command,
        returncode=int(result.returncode),
        stdout=result.stdout,
        stderr=result.stderr,
        started_at_utc=started_at,
        review_base_ref=base_ref,
        review_fixed_point=review_fixed_point,
    )
    return write_json_file(review_dir / f"{issue_number}.json", payload)


def _resolve_review_agent_command(**values: str | int) -> list[str]:
    raw_command = os.environ.get(REVIEW_AGENT_COMMAND_ENV_VAR, "").strip()
    if not raw_command:
        return _default_review_agent_command(**values)

    try:
        payload = json.loads(raw_command)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            _missing_command_message(
                f"{REVIEW_AGENT_COMMAND_ENV_VAR} must be a JSON array of command parts: {error}"
            )
        ) from error

    if not isinstance(payload, list) or not payload:
        raise RuntimeError(
            _missing_command_message(
                f"{REVIEW_AGENT_COMMAND_ENV_VAR} must be a non-empty JSON array of command parts."
            )
        )

    substitutions = {key: str(value) for key, value in values.items()}
    resolved: list[str] = []
    for item in payload:
        if not isinstance(item, str):
            raise RuntimeError(
                _missing_command_message(
                    f"{REVIEW_AGENT_COMMAND_ENV_VAR} entries must all be strings."
                )
            )
        resolved.append(item.format(**substitutions))
    return resolved


def _build_review_agent_env(*, repo_root: Path, **values: str | int) -> dict[str, str]:
    env = dict(os.environ)
    env.update(build_agent_profile_env("review-agent", repo_root))
    for key, value in values.items():
        env[f"SANDCASTLE_{key.upper()}"] = str(value)
    return env


def _load_review_payload(
    *,
    review_result_path: Path,
    issue_number: int,
    branch: str,
    implementation_path: str | None,
    verification_status: str | None,
    command: list[str],
    returncode: int,
    stdout: str,
    stderr: str,
    started_at_utc: str,
    review_base_ref: str,
    review_fixed_point: str,
) -> dict[str, object]:
    if returncode != 0:
        return {
            "issue_number": issue_number,
            "branch": branch,
            "implementation_path": implementation_path,
            "verification_status": verification_status,
            "status": "failed",
            "findings": [],
            "command": command,
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
            "review_base_ref": review_base_ref,
            "review_fixed_point": review_fixed_point,
            "written_at_utc": utc_now_iso(),
            "started_at_utc": started_at_utc,
            "completed_at_utc": utc_now_iso(),
        }

    if not review_result_path.exists():
        raise RuntimeError(
            "review agent command succeeded but did not write the required review result file"
        )

    payload = json.loads(review_result_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("review result file must contain a JSON object")

    findings = payload.get("findings", [])
    if not isinstance(findings, list):
        raise RuntimeError("review result file 'findings' must be a list")

    status = str(payload.get("status", "")).strip().lower()
    if status not in {"clean", "findings"}:
        raise RuntimeError("review result file 'status' must be 'clean' or 'findings'")
    if status == "clean" and findings:
        raise RuntimeError("review result file cannot report 'clean' with non-empty findings")
    if status == "findings" and not findings:
        raise RuntimeError("review result file cannot report 'findings' with an empty findings list")

    return {
        "issue_number": issue_number,
        "branch": branch,
        "implementation_path": implementation_path,
        "verification_status": verification_status,
        "status": status,
        "findings": findings,
        "command": command,
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "review_base_ref": review_base_ref,
        "review_fixed_point": review_fixed_point,
        "written_at_utc": utc_now_iso(),
        "started_at_utc": started_at_utc,
        "completed_at_utc": utc_now_iso(),
        "review_result_path": str(review_result_path),
        "published_review": payload.get("published_review"),
    }


def _render_review_instructions(
    *,
    issue_number: int,
    issue_title: str | None,
    branch: str,
    worktree_path: str,
    pr_number: int | None,
    issue_url: str | None,
    context_path: str | None,
    implementation_path: str | None,
    verification_status: str | None,
    review_base_ref: str,
    review_fixed_point: str,
    review_result_path: str,
) -> str:
    lines = [
        f"# Sandcastle Review Agent Instructions for Issue {issue_number}",
        "",
        f"- issue title: {issue_title or '(not available)'}",
        f"- branch: {branch}",
        f"- worktree path: {worktree_path}",
        f"- PR number: {pr_number if pr_number is not None else '(not available)'}",
        f"- issue url: {issue_url or '(not available)'}",
        f"- context path: {context_path or '(not available)'}",
        f"- implementation artifact path: {implementation_path or '(not available)'}",
        f"- verification status: {verification_status or '(not available)'}",
        f"- review base ref: {review_base_ref}",
        f"- review fixed point: {review_fixed_point}",
        "",
        "## Review Rules",
        "- Review independently of the development agent.",
        "- Do not modify repository code directly.",
        "- Publish PR comments or PR review output if your external review agent is configured to do so.",
        "- Write the normalized review result JSON to the path given by `SANDCASTLE_REVIEW_RESULT_PATH`.",
        "- When using the bundled fallback, run the repo-local Matt `$code-review` skill against the supplied fixed point.",
        "",
        "## Required Result Format",
        "- `status`: `clean` or `findings`",
        "- `findings`: array of normalized findings; must be empty when status is `clean`",
        "- optional `published_review`: metadata about what the review agent posted back to GitHub",
        "",
        "## Output Path",
        f"- {review_result_path}",
    ]
    return "\n".join(lines) + "\n"


def resolve_review_fixed_point(worktree_path: str, *, base_ref: str) -> str:
    return merge_base_sha(worktree_path, "HEAD", base_ref)


def _default_review_agent_command(**values: str | int) -> list[str]:
    command = [
        "codex",
        "--ask-for-approval",
        "never",
        "exec",
        "--sandbox",
        "workspace-write",
    ]
    reasoning_effort = str(values.get("agent_reasoning_effort", "")).strip()
    if reasoning_effort:
        command.extend(["--config", f'model_reasoning_effort="{reasoning_effort}"'])
    model = str(values.get("agent_model", "")).strip()
    if model:
        command.extend(["--model", model])
    command.append(_build_default_codex_review_prompt(**values))
    return command


def _build_default_codex_review_prompt(**values: str | int) -> str:
    issue_number = str(values.get("issue_number", ""))
    issue_title = str(values.get("issue_title", ""))
    worktree_path = str(values.get("worktree_path", ""))
    branch = str(values.get("branch", ""))
    pr_number = str(values.get("pr_number", ""))
    issue_url = str(values.get("issue_url", ""))
    context_path = str(values.get("context_path", ""))
    implementation_path = str(values.get("implementation_path", ""))
    verification_status = str(values.get("verification_status", ""))
    review_brief_path = str(values.get("review_brief_path", ""))
    review_result_path = str(values.get("review_result_path", ""))
    review_instructions_path = str(values.get("review_instructions_path", ""))
    review_base_ref = str(values.get("review_base_ref", DEFAULT_REVIEW_BASE_REF))
    review_fixed_point = str(values.get("review_fixed_point", ""))

    return (
        f"Review Sandcastle issue #{issue_number}: {issue_title}. "
        "Use the repo-local Matt $code-review skill for this review pass. "
        f"Use fixed point {review_fixed_point} for the review diff; this is the merge-base against {review_base_ref}. "
        f"Review the existing issue branch {branch} in worktree {worktree_path}. "
        f"PR number: {pr_number or '(not available)'}. "
        f"Issue URL: {issue_url or '(not available)'}. "
        f"Read issue context from {context_path or '(not available)'}. "
        f"Read the implementation artifact from {implementation_path or '(not available)'}. "
        f"Verification status is {verification_status or '(not available)'}. "
        f"Read the review brief at {review_brief_path} and detailed instructions at {review_instructions_path}. "
        f"Write normalized JSON to {review_result_path} with status set to clean or findings. "
        "If you report findings, include a non-empty findings array with concise, actionable review findings. "
        "Do not modify repository files except the required review result JSON."
    )


def _missing_command_message(reason: str) -> str:
    return (
        f"{reason}\n"
        f"Set {REVIEW_AGENT_COMMAND_ENV_VAR} to a JSON array command for the external review agent.\n"
        "Example:\n"
        '["python", "-m", "my_review_agent", "--brief", "{review_brief_path}", "--result", "{review_result_path}"]'
    )
