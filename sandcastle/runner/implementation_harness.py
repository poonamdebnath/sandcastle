from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from .agent_profiles import build_agent_profile_env, load_agent_profile

DEV_AGENT_COMMAND_ENV_VAR = "SANDCASTLE_DEV_AGENT_COMMAND_JSON"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sandcastle-implementation-harness",
        description="Development-agent bridge for Sandcastle issue execution.",
    )
    parser.add_argument("--issue-number", required=True, type=int)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--issue-title", required=True)
    parser.add_argument("--worktree-path", required=True)
    parser.add_argument("--context-path", required=False, default="")
    parser.add_argument("--implementation-mode", required=False, default="initial")
    parser.add_argument("--findings-path", required=False, default="")
    parser.add_argument("--revision-path", required=False, default="")
    args = parser.parse_args(argv)

    context_payload = _load_context(args.context_path)
    findings_payload = _load_optional_json(args.findings_path)
    revision_payload = _load_optional_json(args.revision_path)
    issue_url = str(context_payload.get("url", ""))
    repo_root = Path(args.worktree_path)
    agent_profile = load_agent_profile("development-agent", repo_root)
    brief = {
        "issue_number": args.issue_number,
        "issue_title": args.issue_title,
        "branch": args.branch,
        "worktree_path": args.worktree_path,
        "issue_url": issue_url,
        "implementation_mode": args.implementation_mode,
        "findings_path": args.findings_path or None,
        "revision_path": args.revision_path or None,
        "goal": _extract_section_bullets(str(context_payload.get("body", "")), "## Goal"),
        "done_when": _extract_section_bullets(
            str(context_payload.get("body", "")), "## Done when"
        ),
        "findings": findings_payload.get("findings", [])
        if isinstance(findings_payload.get("findings"), list)
        else [],
        "next_actions": revision_payload.get("next_actions", [])
        if isinstance(revision_payload.get("next_actions"), list)
        else [],
        "comments": [
            {
                "author": item.get("author"),
                "body": item.get("body"),
            }
            for item in context_payload.get("comments", [])
            if isinstance(item, dict)
        ],
        "agent_profile": {
            "role": agent_profile.role_name,
            "profile": agent_profile.profile_name,
            "model": agent_profile.model,
            "reasoning_effort": agent_profile.reasoning_effort,
        },
    }
    support_dir = (
        Path(args.worktree_path)
        / ".sandcastle-agent"
        / f"issue-{args.issue_number}"
    )
    support_dir.mkdir(parents=True, exist_ok=True)
    brief_path = support_dir / "implementation-brief.json"
    brief_path.write_text(json.dumps(brief, indent=2) + "\n", encoding="utf-8")
    instructions_path = support_dir / "implementation-instructions.md"
    instructions_path.write_text(
        _render_instructions(
            issue_number=args.issue_number,
            issue_title=args.issue_title,
            branch=args.branch,
            worktree_path=args.worktree_path,
            issue_url=issue_url,
            context_path=args.context_path,
            implementation_mode=args.implementation_mode,
            findings_path=args.findings_path,
            revision_path=args.revision_path,
            brief=brief,
        ),
        encoding="utf-8",
    )

    command = _resolve_dev_agent_command(
        issue_number=args.issue_number,
        branch=args.branch,
        issue_title=args.issue_title,
        worktree_path=args.worktree_path,
        context_path=args.context_path,
        issue_url=issue_url,
        implementation_mode=args.implementation_mode,
        findings_path=args.findings_path,
        revision_path=args.revision_path,
        brief_path=str(brief_path),
        instructions_path=str(instructions_path),
        agent_role=agent_profile.role_name,
        agent_profile=agent_profile.profile_name,
        agent_model=agent_profile.model,
        agent_reasoning_effort=agent_profile.reasoning_effort,
    )
    env = _build_dev_agent_env(
        repo_root=repo_root,
        issue_number=args.issue_number,
        branch=args.branch,
        issue_title=args.issue_title,
        worktree_path=args.worktree_path,
        context_path=args.context_path,
        issue_url=issue_url,
        implementation_mode=args.implementation_mode,
        findings_path=args.findings_path,
        revision_path=args.revision_path,
        brief_path=str(brief_path),
        instructions_path=str(instructions_path),
    )
    result = subprocess.run(
        command,
        cwd=Path(args.worktree_path),
        check=False,
        env=env,
    )
    return int(result.returncode)


def _load_context(context_path: str) -> dict[str, object]:
    return _load_optional_json(context_path)


def _load_optional_json(path_text: str) -> dict[str, object]:
    if not path_text:
        return {}
    path = Path(path_text)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _extract_section_bullets(markdown: str, heading: str) -> list[str]:
    lines = markdown.splitlines()
    in_section = False
    bullets: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped == heading:
            in_section = True
            continue
        if in_section and stripped.startswith("## "):
            break
        if in_section and stripped.startswith("- "):
            bullets.append(stripped[2:].strip())
    return bullets


def _resolve_dev_agent_command(**values: str | int) -> list[str]:
    raw_command = os.environ.get(DEV_AGENT_COMMAND_ENV_VAR, "").strip()
    if not raw_command:
        return _default_dev_agent_command(**values)

    try:
        payload = json.loads(raw_command)
    except json.JSONDecodeError as error:
        raise SystemExit(
            _missing_command_message(
                f"{DEV_AGENT_COMMAND_ENV_VAR} must be a JSON array of command parts: {error}"
            )
        ) from error

    if not isinstance(payload, list) or not payload:
        raise SystemExit(
            _missing_command_message(
                f"{DEV_AGENT_COMMAND_ENV_VAR} must be a non-empty JSON array of command parts."
            )
        )

    substitutions = {key: str(value) for key, value in values.items()}
    resolved: list[str] = []
    for item in payload:
        if not isinstance(item, str):
            raise SystemExit(
                _missing_command_message(
                    f"{DEV_AGENT_COMMAND_ENV_VAR} entries must all be strings."
                )
            )
        resolved.append(item.format(**substitutions))
    return resolved


def _build_dev_agent_env(*, repo_root: Path, **values: str | int) -> dict[str, str]:
    env = dict(os.environ)
    env.update(build_agent_profile_env("development-agent", repo_root))
    for key, value in values.items():
        env[f"SANDCASTLE_{key.upper()}"] = str(value)
    return env


def _render_instructions(
    *,
    issue_number: int,
    issue_title: str,
    branch: str,
    worktree_path: str,
    issue_url: str,
    context_path: str,
    implementation_mode: str,
    findings_path: str,
    revision_path: str,
    brief: dict[str, object],
) -> str:
    lines = [
        f"# Sandcastle Development Agent Instructions for Issue {issue_number}",
        "",
        f"- issue title: {issue_title}",
        f"- branch: {branch}",
        f"- worktree path: {worktree_path}",
        f"- issue url: {issue_url or '(not available)'}",
        f"- context path: {context_path or '(not available)'}",
        f"- implementation mode: {implementation_mode}",
        f"- findings path: {findings_path or '(not available)'}",
        f"- revision path: {revision_path or '(not available)'}",
        "",
        "## Goals",
    ]
    goal_lines = list(brief.get("goal", []))
    if goal_lines:
        lines.extend(f"- {item}" for item in goal_lines if isinstance(item, str))
    else:
        lines.append("- No explicit goal bullets were extracted from the issue body.")
    lines.extend(["", "## Done When"])
    done_lines = list(brief.get("done_when", []))
    if done_lines:
        lines.extend(f"- {item}" for item in done_lines if isinstance(item, str))
    else:
        lines.append("- No explicit done-when bullets were extracted from the issue body.")
    findings = brief.get("findings", [])
    lines.extend(["", "## Review Findings"])
    if isinstance(findings, list) and findings:
        for finding in findings[:10]:
            if not isinstance(finding, dict):
                continue
            lines.append(f"- {finding.get('summary', 'Address review finding')}")
    else:
        lines.append("- No review-fix findings were supplied for this pass.")
    next_actions = brief.get("next_actions", [])
    agent_profile = brief.get("agent_profile", {})
    if not isinstance(agent_profile, dict):
        agent_profile = {}
    lines.extend(["", "## Revision Plan"])
    if isinstance(next_actions, list) and next_actions:
        lines.extend(f"- {item}" for item in next_actions if isinstance(item, str))
    else:
        lines.append("- No explicit revision plan was supplied for this pass.")
    lines.extend(
        [
            "",
            "## Agent Profile",
            f"- role: {agent_profile.get('role', '(not available)')}",
            f"- profile: {agent_profile.get('profile', '(not available)')}",
            f"- model: {agent_profile.get('model', '(not available)')}",
            f"- reasoning effort: {agent_profile.get('reasoning_effort', '(not available)')}",
            "",
            "## Execution Rules",
            "- Use the repo-local Matt `$tdd` skill for this coding pass.",
            "- Start by naming the public seam under test.",
            "- Work in a strict red-green loop and add tests before implementation changes.",
            "- Work only inside the provided issue worktree.",
            "- Keep the implementation scoped to this issue.",
            "- Leave changes on the existing issue branch.",
            "- Do not create commits yourself; Sandcastle owns commit and push progression.",
            "",
            "## Inputs",
            "- A machine-readable brief is available at the path given by `SANDCASTLE_BRIEF_PATH`.",
            "- Additional environment variables beginning with `SANDCASTLE_` carry the same context fields.",
            "- When this is a review-fix pass, use `SANDCASTLE_FINDINGS_PATH` and `SANDCASTLE_REVISION_PATH` as the source of truth.",
        ]
    )
    return "\n".join(lines) + "\n"


def _default_dev_agent_command(**values: str | int) -> list[str]:
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
    command.append(_build_default_codex_prompt(**values))
    return command


def _build_default_codex_prompt(**values: str | int) -> str:
    issue_number = str(values.get("issue_number", ""))
    issue_title = str(values.get("issue_title", ""))
    implementation_mode = str(values.get("implementation_mode", "initial"))
    worktree_path = str(values.get("worktree_path", ""))
    context_path = str(values.get("context_path", ""))
    findings_path = str(values.get("findings_path", ""))
    revision_path = str(values.get("revision_path", ""))
    brief_path = str(values.get("brief_path", ""))
    instructions_path = str(values.get("instructions_path", ""))

    findings_line = (
        f"Use findings from {findings_path}. " if findings_path else ""
    )
    revision_line = (
        f"Use revision plan {revision_path}. " if revision_path else ""
    )
    return (
        f"Implement Sandcastle issue #{issue_number}: {issue_title}. "
        "Use the repo-local Matt $tdd skill. Start by stating the public seam under test, "
        "then work in a strict red-green loop. "
        f"Use Sandcastle agent profile {values.get('agent_profile', '')} with reasoning policy {values.get('agent_reasoning_effort', '')}. "
        f"Work only in the existing issue worktree at {worktree_path}. "
        f"Read issue context from {context_path}. "
        f"Read the implementation brief at {brief_path} and detailed instructions at {instructions_path}. "
        f"This is the {implementation_mode} implementation pass. "
        f"{findings_line}{revision_line}"
        "Keep the change scoped to the issue, do not create commits yourself, and leave the branch ready for Sandcastle verification."
    )


def _missing_command_message(reason: str) -> str:
    return (
        f"{reason}\n"
        f"Set {DEV_AGENT_COMMAND_ENV_VAR} to a JSON array command for the external development agent.\n"
        "Example:\n"
        '["python", "-m", "my_dev_agent", "--brief", "{brief_path}", "--worktree", "{worktree_path}"]'
    )


if __name__ == "__main__":
    raise SystemExit(main())
