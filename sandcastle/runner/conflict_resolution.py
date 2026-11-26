from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .agent_profiles import build_agent_profile_env, load_agent_profile
from .state_store import read_json_file, utc_now_iso, write_json_file
from .worktree import (
    abort_merge,
    commit_merge_resolution,
    current_head_sha,
    is_merge_in_progress,
    list_unmerged_files,
    merge_base_into_worktree,
    stage_all_changes,
)

CONFLICT_AGENT_COMMAND_ENV_VAR = "SANDCASTLE_CONFLICT_AGENT_COMMAND_JSON"


def resolve_pr_conflicts(
    *,
    conflict_dir: Path,
    issue_number: int,
    pr_number: int | None,
    branch: str,
    worktree_path: str,
    issue_title: str,
    issue_url: str | None,
    context_path: str | None,
    base_ref: str,
) -> Path:
    started_at = utc_now_iso()
    pre_merge_head_sha = current_head_sha(worktree_path)
    payload: dict[str, object] = {
        "issue_number": issue_number,
        "pr_number": pr_number,
        "branch": branch,
        "worktree_path": worktree_path,
        "issue_title": issue_title,
        "issue_url": issue_url,
        "context_path": context_path,
        "base_ref": base_ref,
        "pre_merge_head_sha": pre_merge_head_sha,
        "started_at_utc": started_at,
    }

    returncode, stdout, stderr = merge_base_into_worktree(worktree_path, base_ref)
    payload["merge_returncode"] = returncode
    payload["merge_stdout"] = stdout
    payload["merge_stderr"] = stderr

    if returncode == 0:
        post_merge_head_sha = current_head_sha(worktree_path)
        payload.update(
            {
                "status": "resolved",
                "resolution_kind": (
                    "no-op" if post_merge_head_sha == pre_merge_head_sha else "clean-merge"
                ),
                "conflicted_files": [],
                "post_merge_head_sha": post_merge_head_sha,
                "completed_at_utc": utc_now_iso(),
            }
        )
        return write_json_file(conflict_dir / f"{issue_number}.json", payload)

    conflicted_files = list_unmerged_files(worktree_path)
    payload["conflicted_files"] = conflicted_files
    if not conflicted_files:
        payload.update(
            {
                "status": "failed",
                "blocked_reason": (
                    stderr.strip() or stdout.strip() or "git merge failed without conflict markers"
                ),
                "completed_at_utc": utc_now_iso(),
            }
        )
        return write_json_file(conflict_dir / f"{issue_number}.json", payload)

    repo_root = Path(worktree_path)
    agent_profile = load_agent_profile("conflict-resolution-agent", repo_root)
    support_dir = Path(worktree_path) / ".sandcastle-agent" / f"issue-{issue_number}"
    support_dir.mkdir(parents=True, exist_ok=True)
    brief_path = support_dir / "conflict-brief.json"
    brief_path.write_text(
        json.dumps(
            {
                "issue_number": issue_number,
                "pr_number": pr_number,
                "branch": branch,
                "worktree_path": worktree_path,
                "issue_title": issue_title,
                "issue_url": issue_url,
                "context_path": context_path,
                "base_ref": base_ref,
                "pre_merge_head_sha": pre_merge_head_sha,
                "conflicted_files": conflicted_files,
                "merge_stdout": stdout,
                "merge_stderr": stderr,
                "agent_profile": {
                    "role": agent_profile.role_name,
                    "profile": agent_profile.profile_name,
                    "model": agent_profile.model,
                    "reasoning_effort": agent_profile.reasoning_effort,
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    result_path = support_dir / "conflict-result.json"
    instructions_path = support_dir / "conflict-instructions.md"
    instructions_path.write_text(
        _render_instructions(
            issue_number=issue_number,
            pr_number=pr_number,
            branch=branch,
            worktree_path=worktree_path,
            issue_title=issue_title,
            issue_url=issue_url or "",
            context_path=context_path or "",
            base_ref=base_ref,
            conflicted_files=conflicted_files,
        ),
        encoding="utf-8",
    )

    command = _resolve_conflict_agent_command(
        issue_number=issue_number,
        pr_number=pr_number,
        branch=branch,
        worktree_path=worktree_path,
        issue_title=issue_title,
        issue_url=issue_url or "",
        context_path=context_path or "",
        base_ref=base_ref,
        conflict_brief_path=str(brief_path),
        conflict_result_path=str(result_path),
        conflict_instructions_path=str(instructions_path),
        agent_role=agent_profile.role_name,
        agent_profile=agent_profile.profile_name,
        agent_model=agent_profile.model,
        agent_reasoning_effort=agent_profile.reasoning_effort,
    )

    env = _build_conflict_agent_env(
        repo_root=repo_root,
        issue_number=issue_number,
        pr_number=pr_number,
        branch=branch,
        worktree_path=worktree_path,
        issue_title=issue_title,
        issue_url=issue_url or "",
        context_path=context_path or "",
        base_ref=base_ref,
        conflict_brief_path=str(brief_path),
        conflict_result_path=str(result_path),
        conflict_instructions_path=str(instructions_path),
    )
    result = subprocess.run(
        command,
        cwd=Path(worktree_path),
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    agent_result = _load_optional_json(result_path)
    payload.update(
        {
            "brief_path": str(brief_path),
            "result_path": str(result_path),
            "instructions_path": str(instructions_path),
            "conflict_agent_command": command,
            "conflict_agent_returncode": result.returncode,
            "conflict_agent_stdout": result.stdout,
            "conflict_agent_stderr": result.stderr,
            "agent_result": agent_result,
        }
    )

    if agent_result.get("status") == "blocked":
        blocked_reason = str(
            agent_result.get("reason") or "conflict agent reported the conflict is unsafe"
        )
        _abort_merge_if_needed(worktree_path)
        payload.update(
            {
                "status": "blocked",
                "resolution_kind": "manual-handoff",
                "blocked_reason": blocked_reason,
                "completed_at_utc": utc_now_iso(),
            }
        )
        return write_json_file(conflict_dir / f"{issue_number}.json", payload)

    try:
        stage_all_changes(worktree_path)
        remaining_conflicts = list_unmerged_files(worktree_path)
        payload["remaining_conflicted_files"] = remaining_conflicts
        if remaining_conflicts:
            _abort_merge_if_needed(worktree_path)
            payload.update(
                {
                    "status": "blocked",
                    "resolution_kind": "manual-handoff",
                    "blocked_reason": (
                        "conflict agent did not fully resolve all conflicted files: "
                        + ", ".join(remaining_conflicts)
                    ),
                    "completed_at_utc": utc_now_iso(),
                }
            )
            return write_json_file(conflict_dir / f"{issue_number}.json", payload)
        if is_merge_in_progress(worktree_path):
            commit_merge_resolution(worktree_path)
    except RuntimeError as error:
        _abort_merge_if_needed(worktree_path)
        payload.update(
            {
                "status": "blocked",
                "resolution_kind": "manual-handoff",
                "blocked_reason": str(error),
                "completed_at_utc": utc_now_iso(),
            }
        )
        return write_json_file(conflict_dir / f"{issue_number}.json", payload)

    payload.update(
        {
            "status": "resolved",
            "resolution_kind": "agent-resolved",
            "post_merge_head_sha": current_head_sha(worktree_path),
            "completed_at_utc": utc_now_iso(),
        }
    )
    return write_json_file(conflict_dir / f"{issue_number}.json", payload)


def _resolve_conflict_agent_command(**values: str | int) -> list[str]:
    raw_command = os.environ.get(CONFLICT_AGENT_COMMAND_ENV_VAR, "").strip()
    if not raw_command:
        return _default_conflict_agent_command(**values)

    payload = json.loads(raw_command)
    if not isinstance(payload, list) or not payload:
        raise RuntimeError(
            _missing_command_message(
                f"{CONFLICT_AGENT_COMMAND_ENV_VAR} must be a non-empty JSON array of command parts."
            )
        )

    substitutions = {key: str(value) for key, value in values.items()}
    resolved: list[str] = []
    for item in payload:
        if not isinstance(item, str):
            raise RuntimeError(
                _missing_command_message(
                    f"{CONFLICT_AGENT_COMMAND_ENV_VAR} entries must all be strings."
                )
            )
        resolved.append(item.format(**substitutions))
    return resolved


def _default_conflict_agent_command(**values: str | int) -> list[str]:
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
    command.append(_build_default_codex_conflict_prompt(**values))
    return command


def _build_default_codex_conflict_prompt(**values: str | int) -> str:
    issue_number = str(values.get("issue_number", ""))
    pr_number = str(values.get("pr_number", ""))
    issue_title = str(values.get("issue_title", ""))
    branch = str(values.get("branch", ""))
    worktree_path = str(values.get("worktree_path", ""))
    issue_url = str(values.get("issue_url", ""))
    context_path = str(values.get("context_path", ""))
    base_ref = str(values.get("base_ref", "origin/main"))
    conflict_brief_path = str(values.get("conflict_brief_path", ""))
    conflict_result_path = str(values.get("conflict_result_path", ""))
    conflict_instructions_path = str(values.get("conflict_instructions_path", ""))

    return (
        f"Resolve merge conflicts for Sandcastle issue #{issue_number}: {issue_title}. "
        "Use the repo-local Matt $resolving-merge-conflicts skill for this conflict-resolution pass. "
        f"Use Sandcastle agent profile {values.get('agent_profile', '')} with reasoning policy {values.get('agent_reasoning_effort', '')}. "
        f"Work only in the existing issue branch {branch} at {worktree_path}. "
        f"PR number: {pr_number or '(not available)'}. "
        f"Issue URL: {issue_url or '(not available)'}. "
        f"Read issue context from {context_path or '(not available)'}. "
        f"Read the conflict brief at {conflict_brief_path} and detailed instructions at {conflict_instructions_path}. "
        f"Resolve the merge against base ref {base_ref}. "
        f"Write JSON to {conflict_result_path} with status set to resolved or blocked; if blocked, include a short reason. "
        "Resolve only safe, mechanical merge conflicts, do not widen scope, and leave the worktree in a clean merge-resolved state if successful."
    )


def _load_optional_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    return read_json_file(path)


def _build_conflict_agent_env(*, repo_root: Path, **values: str | int | None) -> dict[str, str]:
    env = dict(os.environ)
    env.update(build_agent_profile_env("conflict-resolution-agent", repo_root))
    for key, value in values.items():
        env[f"SANDCASTLE_{key.upper()}"] = "" if value is None else str(value)
    return env


def _abort_merge_if_needed(worktree_path: str) -> None:
    if is_merge_in_progress(worktree_path):
        abort_merge(worktree_path)


def _render_instructions(
    *,
    issue_number: int,
    pr_number: int,
    branch: str,
    worktree_path: str,
    issue_title: str,
    issue_url: str,
    context_path: str,
    base_ref: str,
    conflicted_files: list[str],
) -> str:
    lines = [
        f"# Sandcastle Conflict Resolution Instructions for Issue {issue_number}",
        "",
        f"- issue title: {issue_title}",
        f"- PR number: {pr_number if pr_number is not None else '(not available)'}",
        f"- branch: {branch}",
        f"- worktree path: {worktree_path}",
        f"- issue url: {issue_url or '(not available)'}",
        f"- context path: {context_path or '(not available)'}",
        f"- base ref: {base_ref}",
        "",
        "## Conflicted Files",
    ]
    lines.extend(f"- {path}" for path in conflicted_files)
    lines.extend(
        [
            "",
            "## Execution Rules",
            "- Use the repo-local Matt `resolving-merge-conflicts` skill for this conflict-resolution pass.",
            "- Resolve only safe, mechanical merge conflicts on this existing issue branch.",
            "- Do not widen scope beyond conflict resolution.",
            "- If the conflict is semantic, ambiguous, or unsafe, write a blocked result instead of guessing.",
            "- Leave the worktree in a clean merge-resolved state if you succeed.",
            "",
            "## Inputs",
            "- The conflict brief is at `SANDCASTLE_CONFLICT_BRIEF_PATH`.",
            "- The result file path is `SANDCASTLE_CONFLICT_RESULT_PATH`.",
            "- Additional environment variables beginning with `SANDCASTLE_` carry the same context.",
            "",
            "## Result Contract",
            "- Write optional JSON to `SANDCASTLE_CONFLICT_RESULT_PATH` with `status` set to `resolved` or `blocked`.",
            "- If blocked, include a short `reason` field explaining why a human must take over.",
        ]
    )
    return "\n".join(lines) + "\n"


def _missing_command_message(reason: str) -> str:
    return (
        f"{reason}\n"
        f"Set {CONFLICT_AGENT_COMMAND_ENV_VAR} to a JSON array command to override the bundled conflict-resolution agent.\n"
        "Example:\n"
        '["python", "-m", "my_conflict_agent", "--brief", "{conflict_brief_path}", "--result", "{conflict_result_path}"]'
    )
