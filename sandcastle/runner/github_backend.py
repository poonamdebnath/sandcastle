from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

from .agent_profiles import (
    REQUIRED_AGENT_ROLES,
    load_agent_profile,
    probe_agent_role_runtime,
    validate_agent_role_policies,
)
from .conflict_resolution import (
    CONFLICT_AGENT_COMMAND_ENV_VAR,
    _default_conflict_agent_command,
)
from .implementation_harness import DEV_AGENT_COMMAND_ENV_VAR, _default_dev_agent_command
from .review_loop import (
    DEFAULT_REVIEW_BASE_REF,
    REVIEW_AGENT_COMMAND_ENV_VAR,
    _default_review_agent_command,
)
from .state_store import STATE_ROOT_ENV_VAR, resolve_state_dir
from .vendored_skills import validate_required_vendored_skills

DEFAULT_PATH_REQUIRED_VENDORED_SKILLS_BY_ROLE: dict[str, tuple[str, ...]] = {
    "development-agent": ("tdd",),
    "review-agent": ("code-review",),
}
MANDATORY_VENDORED_SKILLS_BY_ROLE: dict[str, tuple[str, ...]] = {
    "conflict-resolution-agent": ("resolving-merge-conflicts",),
}

AGENT_COMMAND_REQUIREMENTS = (
    {
        "name": "development-agent",
        "env_var": DEV_AGENT_COMMAND_ENV_VAR,
        "required_for": (
            "autonomous_issue_execution",
            "full_autonomous_loop",
        ),
    },
    {
        "name": "review-agent",
        "env_var": REVIEW_AGENT_COMMAND_ENV_VAR,
        "required_for": (
            "autonomous_issue_execution",
            "full_autonomous_loop",
        ),
    },
    {
        "name": "conflict-resolution-agent",
        "env_var": CONFLICT_AGENT_COMMAND_ENV_VAR,
        "required_for": ("full_autonomous_loop",),
    },
)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "describe":
        return _handle_describe(
            config_path=Path(args.config_path),
            output_path=Path(args.output_path),
            github_output=Path(args.github_output) if args.github_output else None,
        )
    if args.command == "validate":
        return _handle_validate(
            config_path=Path(args.config_path),
            repo_root=Path(args.repo_root),
            output_path=Path(args.output_path),
        )
    if args.command == "prepare-host":
        return _handle_prepare_host(
            config_path=Path(args.config_path),
            output_path=Path(args.output_path),
            state_root=Path(args.state_root),
            repo=args.repo,
            execution_enabled=args.execution_enabled == "true",
        )

    parser.error(f"unknown command: {args.command}")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sandcastle-github-backend")
    subparsers = parser.add_subparsers(dest="command", required=True)

    describe = subparsers.add_parser(
        "describe", help="Describe the configured unattended execution backend."
    )
    describe.add_argument("--config-path", required=True)
    describe.add_argument("--output-path", required=True)
    describe.add_argument("--github-output")

    validate = subparsers.add_parser(
        "validate", help="Validate the configured unattended execution backend."
    )
    validate.add_argument("--config-path", required=True)
    validate.add_argument("--repo-root", required=True)
    validate.add_argument("--output-path", required=True)

    prepare_host = subparsers.add_parser(
        "prepare-host",
        help="Prepare a Windows host and repository variables for the self-hosted backend.",
    )
    prepare_host.add_argument("--config-path", required=True)
    prepare_host.add_argument("--output-path", required=True)
    prepare_host.add_argument("--state-root", required=True)
    prepare_host.add_argument("--repo")
    prepare_host.add_argument(
        "--execution-enabled",
        choices=("true", "false"),
        default="false",
        help="Set the execution repo variable to true only after the runner is registered.",
    )
    return parser


def _handle_describe(
    *, config_path: Path, output_path: Path, github_output: Path | None
) -> int:
    config = _load_backend_config(config_path)
    payload = {
        "backend_kind": config["kind"],
        "runner_labels": config["runner_labels"],
        "required_tools": config["required_tools"],
        "required_repo_variable": config["required_repo_variable"],
        "state_root_env_var": config["state_root_env_var"],
        "default_state_root": config["default_state_root"],
        "credentials": config["credentials"],
        "notes": config["notes"],
    }
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if github_output is not None:
        _append_output(github_output, "backend_kind", str(config["kind"]))
        _append_output(
            github_output,
            "runs_on_json",
            json.dumps(list(config["runner_labels"]), separators=(",", ":")),
        )
        _append_output(
            github_output,
            "required_repo_variable",
            str(config["required_repo_variable"]),
        )
        _append_output(
            github_output,
            "state_root_env_var",
            str(config["state_root_env_var"]),
        )
    print(json.dumps(payload, indent=2))
    return 0


def _handle_validate(*, config_path: Path, repo_root: Path, output_path: Path) -> int:
    config = _load_backend_config(config_path)
    configured_state_root = os.environ.get(str(config["state_root_env_var"]), "").strip()
    state_root = resolve_state_dir(repo_root)
    tool_results = [
        {
            "tool": tool,
            "available": shutil.which(tool) is not None,
        }
        for tool in config["required_tools"]
    ]
    command_results = [
        _validate_agent_command(
            name=str(requirement["name"]),
            env_var=str(requirement["env_var"]),
            required_for=tuple(str(item) for item in requirement["required_for"]),
            repo_root=repo_root,
        )
        for requirement in AGENT_COMMAND_REQUIREMENTS
    ]
    role_policy_results = validate_agent_role_policies(repo_root, probe_runtime=True)
    all_required_tools_available = all(
        bool(result["available"]) for result in tool_results
    )
    runner_match = _probe_self_hosted_runner_match(config)
    ready_for_backend_wakeups = (
        state_root.exists()
        and all_required_tools_available
        and bool(runner_match["ready"])
    )
    ready_for_autonomous_issue_execution = ready_for_backend_wakeups and all(
        bool(result["ready"])
        for result in command_results
        if "autonomous_issue_execution" in result["required_for"]
    ) and all(
        bool(result["ready"])
        for result in role_policy_results
        if result["name"] in {"development-agent", "review-agent"}
    )
    ready_for_full_autonomous_loop = ready_for_backend_wakeups and all(
        bool(result["ready"])
        for result in command_results
    ) and all(
        bool(result["ready"]) for result in role_policy_results
    )
    payload = {
        "backend_kind": config["kind"],
        "runner_labels": config["runner_labels"],
        "repo_root": str(repo_root),
        "state_root_env_var": config["state_root_env_var"],
        "configured_state_root": configured_state_root or None,
        "resolved_state_root": str(state_root),
        "state_root_exists": state_root.exists(),
        "required_tools": tool_results,
        "all_required_tools_available": all_required_tools_available,
        "runner_match": runner_match,
        "external_agent_commands": command_results,
        "agent_role_policies": role_policy_results,
        "ready_for_backend_execution": ready_for_backend_wakeups,
        "ready_for_backend_wakeups": ready_for_backend_wakeups,
        "ready_for_autonomous_issue_execution": ready_for_autonomous_issue_execution,
        "ready_for_autonomous_conflict_resolution": ready_for_full_autonomous_loop,
        "ready_for_full_autonomous_loop": ready_for_full_autonomous_loop,
        "backend_blockers": _backend_blockers(
            state_root_exists=state_root.exists(),
            tool_results=tool_results,
            runner_match=runner_match,
        ),
        "autonomous_issue_execution_blockers": _command_blockers(
            command_results, "autonomous_issue_execution"
        )
        + _role_policy_blockers(
            role_policy_results, ("development-agent", "review-agent")
        ),
        "full_autonomous_loop_blockers": _command_blockers(
            command_results, "full_autonomous_loop"
        )
        + _role_policy_blockers(role_policy_results, REQUIRED_AGENT_ROLES),
    }
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


def _probe_self_hosted_runner_match(config: dict[str, object]) -> dict[str, object]:
    if str(config["kind"]) != "github-actions-self-hosted":
        return {
            "required": False,
            "ready": True,
            "reason": None,
            "mode": "not-applicable",
        }

    if os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true":
        return {
            "required": False,
            "ready": True,
            "reason": None,
            "mode": "workflow-job",
        }

    required_labels = tuple(str(label) for label in config["runner_labels"])
    gh_auth = _probe_gh_auth()
    if not bool(gh_auth["ready"]):
        return {
            "required": True,
            "ready": False,
            "reason": (
                "host validation cannot confirm an online self-hosted runner with labels "
                f"{required_labels!r} because gh auth status is not ready."
            ),
            "mode": "host-check",
        }

    try:
        repo = _resolve_current_repo()
    except RuntimeError as error:
        return {
            "required": True,
            "ready": False,
            "reason": (
                "host validation cannot resolve the current repository for self-hosted runner "
                f"verification: {error}"
            ),
            "mode": "host-check",
        }

    permissions = _load_repo_permissions(repo, True)
    if permissions is None:
        return {
            "required": True,
            "ready": False,
            "reason": (
                "host validation cannot load repository permissions needed to inspect "
                "self-hosted runners."
            ),
            "mode": "host-check",
            "repo": repo,
        }
    if permissions.get("ready") is False:
        return {
            "required": True,
            "ready": False,
            "reason": str(
                permissions.get(
                    "reason",
                    "host validation cannot inspect repository self-hosted runners.",
                )
            ),
            "mode": "host-check",
            "repo": repo,
        }
    if not bool(permissions.get("admin")):
        return {
            "required": True,
            "ready": False,
            "reason": (
                "host validation cannot confirm an online self-hosted runner with labels "
                f"{required_labels!r} because the current GitHub identity lacks repository "
                "Actions admin access."
            ),
            "mode": "host-check",
            "repo": repo,
        }

    result = _run_command(["gh", "api", f"repos/{repo}/actions/runners"])
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        return {
            "required": True,
            "ready": False,
            "reason": message
            or "host validation could not inspect repository self-hosted runners.",
            "mode": "host-check",
            "repo": repo,
        }

    payload = json.loads(result.stdout)
    runners = payload.get("runners")
    if not isinstance(runners, list):
        return {
            "required": True,
            "ready": False,
            "reason": (
                "host validation could not read repository self-hosted runner inventory."
            ),
            "mode": "host-check",
            "repo": repo,
        }

    required = {label.lower() for label in required_labels}
    matching_runner_names: list[str] = []
    online_matching_runner_names: list[str] = []
    for runner in runners:
        if not isinstance(runner, dict):
            continue
        labels = runner.get("labels")
        if not isinstance(labels, list):
            continue
        label_names = {
            str(label.get("name", "")).strip().lower()
            for label in labels
            if isinstance(label, dict)
        }
        if not required.issubset(label_names):
            continue
        name = str(runner.get("name", "")).strip() or "(unnamed runner)"
        matching_runner_names.append(name)
        if str(runner.get("status", "")).strip().lower() == "online":
            online_matching_runner_names.append(name)

    if online_matching_runner_names:
        return {
            "required": True,
            "ready": True,
            "reason": None,
            "mode": "host-check",
            "repo": repo,
            "matching_runners": matching_runner_names,
            "online_matching_runners": online_matching_runner_names,
        }
    if matching_runner_names:
        return {
            "required": True,
            "ready": False,
            "reason": (
                "no matching self-hosted runner is online for labels "
                f"{required_labels!r}; matching registered runners: {matching_runner_names!r}."
            ),
            "mode": "host-check",
            "repo": repo,
            "matching_runners": matching_runner_names,
            "online_matching_runners": [],
        }
    return {
        "required": True,
        "ready": False,
        "reason": (
            "no repository self-hosted runner is registered with labels "
            f"{required_labels!r}."
        ),
        "mode": "host-check",
        "repo": repo,
        "matching_runners": [],
        "online_matching_runners": [],
    }


def _handle_prepare_host(
    *,
    config_path: Path,
    output_path: Path,
    state_root: Path,
    repo: str | None,
    execution_enabled: bool,
) -> int:
    config = _load_backend_config(config_path)
    resolved_repo = repo or _resolve_current_repo()
    state_root.mkdir(parents=True, exist_ok=True)
    gh_auth = _probe_gh_auth()
    repo_variables: list[dict[str, object]] = []

    for name, value in (
        (str(config["state_root_env_var"]), str(state_root)),
        (
            str(config["required_repo_variable"]),
            "true" if execution_enabled else "false",
        ),
    ):
        repo_variables.append(
            _set_repo_variable(
                repo=resolved_repo,
                name=name,
                value=value,
                gh_auth_ready=bool(gh_auth["ready"]),
            )
        )

    permissions = _load_repo_permissions(resolved_repo, bool(gh_auth["ready"]))
    runner_registration = _probe_runner_registration_access(
        resolved_repo,
        gh_auth_ready=bool(gh_auth["ready"]),
        permissions=permissions,
    )
    payload = {
        "backend_kind": config["kind"],
        "repo": resolved_repo,
        "state_root": str(state_root),
        "state_root_exists": state_root.exists(),
        "gh_auth": gh_auth,
        "repo_permissions": permissions,
        "repo_variables": repo_variables,
        "runner_registration": runner_registration,
        "next_steps": _prepare_host_next_steps(
            repo=resolved_repo,
            state_root=state_root,
            gh_auth=gh_auth,
            repo_variables=repo_variables,
            runner_registration=runner_registration,
            execution_enabled=execution_enabled,
        ),
    }
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    if not bool(gh_auth["ready"]):
        return 1
    if not all(bool(item["set"]) for item in repo_variables):
        return 1
    return 0


def _load_backend_config(config_path: Path) -> dict[str, object]:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    backend = payload.get("backend")
    if not isinstance(backend, dict):
        raise RuntimeError(f"expected 'backend' object in {config_path}")
    return backend


def _append_output(path: Path, name: str, value: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{name}<<__SANDCASTLE__\n")
        handle.write(value)
        handle.write("\n__SANDCASTLE__\n")


def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )


def _resolve_current_repo() -> str:
    result = _run_command(["gh", "repo", "view", "--json", "nameWithOwner"])
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            "failed to resolve the current GitHub repository"
            + (f": {message}" if message else "")
        )
    payload = json.loads(result.stdout)
    repo = payload.get("nameWithOwner")
    if not isinstance(repo, str) or not repo.strip():
        raise RuntimeError("gh repo view did not return nameWithOwner.")
    return repo


def _probe_gh_auth() -> dict[str, object]:
    result = _run_command(["gh", "auth", "status"])
    if result.returncode == 0:
        return {
            "ready": True,
            "reason": None,
        }
    message = (result.stderr or result.stdout).strip()
    return {
        "ready": False,
        "reason": message or "gh auth status failed.",
    }


def _set_repo_variable(
    *,
    repo: str,
    name: str,
    value: str,
    gh_auth_ready: bool,
) -> dict[str, object]:
    if not gh_auth_ready:
        return {
            "name": name,
            "value": value,
            "set": False,
            "reason": "gh auth status is not ready.",
        }

    result = _run_command(
        ["gh", "variable", "set", name, "--repo", repo, "--body", value]
    )
    if result.returncode == 0:
        return {
            "name": name,
            "value": value,
            "set": True,
            "reason": None,
        }
    message = (result.stderr or result.stdout).strip()
    return {
        "name": name,
        "value": value,
        "set": False,
        "reason": message or f"failed to set repository variable {name}.",
    }


def _load_repo_permissions(
    repo: str, gh_auth_ready: bool
) -> dict[str, object] | None:
    if not gh_auth_ready:
        return None

    result = _run_command(["gh", "api", f"repos/{repo}"])
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        return {
            "ready": False,
            "reason": message or f"failed to load repository permissions for {repo}.",
        }

    payload = json.loads(result.stdout)
    permissions = payload.get("permissions")
    if not isinstance(permissions, dict):
        return {
            "ready": False,
            "reason": f"repository {repo} did not return a permissions object.",
        }

    return {
        "ready": True,
        "admin": bool(permissions.get("admin")),
        "maintain": bool(permissions.get("maintain")),
        "push": bool(permissions.get("push")),
        "triage": bool(permissions.get("triage")),
        "pull": bool(permissions.get("pull")),
    }


def _probe_runner_registration_access(
    repo: str,
    *,
    gh_auth_ready: bool,
    permissions: dict[str, object] | None,
) -> dict[str, object]:
    if not gh_auth_ready:
        return {
            "ready": False,
            "reason": "gh auth status is not ready.",
        }

    if permissions is not None and permissions.get("ready") is False:
        return {
            "ready": False,
            "reason": str(permissions.get("reason", "failed to load repo permissions")),
        }

    if permissions is not None and not bool(permissions.get("admin")):
        return {
            "ready": False,
            "reason": (
                "GitHub CLI can access the repository but does not have Actions admin "
                "permission needed to register self-hosted runners."
            ),
        }

    result = _run_command(["gh", "api", f"repos/{repo}/actions/runners/downloads"])
    if result.returncode == 0:
        return {
            "ready": True,
            "reason": None,
        }

    message = (result.stderr or result.stdout).strip()
    return {
        "ready": False,
        "reason": message
        or "failed to verify access to self-hosted runner administration.",
    }


def _prepare_host_next_steps(
    *,
    repo: str,
    state_root: Path,
    gh_auth: dict[str, object],
    repo_variables: list[dict[str, object]],
    runner_registration: dict[str, object],
    execution_enabled: bool,
) -> list[str]:
    steps: list[str] = []
    if not bool(gh_auth["ready"]):
        steps.append("Run 'gh auth login' on the runner host, then rerun prepare-host.")
        return steps

    if not all(bool(item["set"]) for item in repo_variables):
        steps.append("Repair the failed repository variable writes, then rerun prepare-host.")
        return steps

    if not bool(runner_registration["ready"]):
        steps.append(
            "Use a repository admin identity to open Settings -> Actions -> Runners and "
            "register a Windows runner with labels 'self-hosted' and 'sandcastle'."
        )
        steps.append(
            "Keep the execution variable set to false until the runner is online and the "
            "service is installed."
        )
    elif not execution_enabled:
        steps.append(
            "The runner registration prerequisites are ready. After the runner is online, "
            "rerun prepare-host with --execution-enabled true or update the repo variable "
            "manually."
        )

    steps.append(
        "Run '$env:SANDCASTLE_STATE_ROOT=\""
        f"{state_root}"
        "\"; python -m sandcastle.runner.github_backend validate --config-path "
        "sandcastle/config/controller-backend.json --repo-root . --output-path "
        "sandcastle/state/backend-validation.json' from the runner host."
    )
    steps.append(
        f"Confirm '{state_root}' remains durable across sign-out and reboot for the runner "
        "service identity."
    )
    steps.append(
        f"After the runner is online for {repo}, manually dispatch the 'Sandcastle Controller' "
        "workflow on main and verify a bounded loop completes on the self-hosted runner."
    )
    return steps


def _validate_agent_command(
    *,
    name: str,
    env_var: str,
    required_for: tuple[str, ...],
    repo_root: Path | None = None,
) -> dict[str, object]:
    raw_value = os.environ.get(env_var, "").strip()
    profile_probe = _role_profile_probe(name=name, repo_root=repo_root)
    required_vendored_skills = _required_vendored_skill_probe(
        name=name,
        repo_root=repo_root,
        command_configured=bool(raw_value),
    )
    if not raw_value:
        agent_values: dict[str, str] = {}
        if repo_root is not None:
            try:
                profile = load_agent_profile(name, repo_root)
            except RuntimeError:
                profile = None
            if profile is not None:
                agent_values = {
                    "agent_role": profile.role_name,
                    "agent_profile": profile.profile_name,
                    "agent_model": profile.model,
                    "agent_reasoning_effort": profile.reasoning_effort,
                }
        if name == "development-agent":
            payload = _default_dev_agent_command(
                issue_number=0,
                branch="sandcastle/issue-0-placeholder",
                issue_title="placeholder issue",
                worktree_path=".",
                context_path="",
                issue_url="",
                implementation_mode="initial",
                findings_path="",
                revision_path="",
                brief_path="brief.json",
                instructions_path="instructions.md",
                **agent_values,
            )
            fallback = "bundled-codex-tdd"
        elif name == "review-agent":
            payload = _default_review_agent_command(
                issue_number=0,
                branch="sandcastle/issue-0-placeholder",
                worktree_path=".",
                issue_title="placeholder issue",
                pr_number=0,
                issue_url="",
                context_path="",
                implementation_path="",
                verification_status="",
                review_base_ref=DEFAULT_REVIEW_BASE_REF,
                review_fixed_point="HEAD",
                review_brief_path="review-brief.json",
                review_result_path="review-result.json",
                review_instructions_path="review-instructions.md",
                **agent_values,
            )
            fallback = "bundled-codex-code-review"
        elif name == "conflict-resolution-agent":
            payload = _default_conflict_agent_command(
                issue_number=0,
                pr_number=0,
                branch="sandcastle/issue-0-placeholder",
                worktree_path=".",
                issue_title="placeholder issue",
                issue_url="",
                context_path="",
                base_ref="origin/main",
                conflict_brief_path="conflict-brief.json",
                conflict_result_path="conflict-result.json",
                conflict_instructions_path="conflict-instructions.md",
                **agent_values,
            )
            fallback = "bundled-codex-resolving-merge-conflicts"
        else:
            payload = None
            fallback = None

        if payload is not None:
            executable = payload[0].strip()
            executable_available, resolved_executable = _resolve_command_executable(
                executable
            )
            readiness_probe = _probe_bundled_codex_command(
                payload,
                resolved_executable or executable,
                executable_available,
            )
            return {
                "name": name,
                "env_var": env_var,
                "required_for": list(required_for),
                "configured": False,
                "valid": True,
                "ready": bool(readiness_probe["ready"])
                and _profile_probe_ready(profile_probe)
                and _vendored_skill_probe_ready(required_vendored_skills),
                "reason": _command_reason(
                    readiness_probe["reason"],
                    profile_probe,
                    required_vendored_skills,
                ),
                "command": payload,
                "executable": executable,
                "executable_available": executable_available,
                "resolved_executable": resolved_executable,
                "readiness_probe": readiness_probe,
                "profile_probe": profile_probe,
                "required_vendored_skills": required_vendored_skills,
                "fallback": fallback,
            }
        return {
            "name": name,
            "env_var": env_var,
            "required_for": list(required_for),
            "configured": False,
            "valid": False,
            "ready": False,
            "reason": f"{env_var} is not configured.",
        }

    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError as error:
        return {
            "name": name,
            "env_var": env_var,
            "required_for": list(required_for),
            "configured": True,
            "valid": False,
            "ready": False,
            "reason": f"{env_var} must be a JSON array of command parts: {error}",
            "raw_value": raw_value,
        }

    if not isinstance(payload, list) or not payload:
        return {
            "name": name,
            "env_var": env_var,
            "required_for": list(required_for),
            "configured": True,
            "valid": False,
            "ready": False,
            "reason": f"{env_var} must be a non-empty JSON array of command parts.",
            "raw_value": raw_value,
        }

    for item in payload:
        if not isinstance(item, str):
            return {
                "name": name,
                "env_var": env_var,
                "required_for": list(required_for),
                "configured": True,
                "valid": False,
                "ready": False,
                "reason": f"{env_var} entries must all be strings.",
                "raw_value": raw_value,
            }

    executable = payload[0].strip()
    executable_available, resolved_executable = _resolve_command_executable(executable)
    return {
        "name": name,
        "env_var": env_var,
        "required_for": list(required_for),
        "configured": True,
        "valid": True,
        "ready": executable_available
        and _profile_probe_ready(profile_probe)
        and _vendored_skill_probe_ready(required_vendored_skills),
        "reason": _command_reason(
            None
            if executable_available
            else f"command executable '{executable}' is not available on PATH or as a resolvable file.",
            profile_probe,
            required_vendored_skills,
        ),
        "command": payload,
        "executable": executable,
        "executable_available": executable_available,
        "resolved_executable": resolved_executable,
        "profile_probe": profile_probe,
        "required_vendored_skills": required_vendored_skills,
    }


def _role_profile_probe(*, name: str, repo_root: Path | None) -> dict[str, object] | None:
    if repo_root is None:
        return None
    try:
        return probe_agent_role_runtime(name, repo_root)
    except RuntimeError as error:
        return {
            "ready": False,
            "reason": str(error),
        }


def _profile_probe_ready(profile_probe: dict[str, object] | None) -> bool:
    if profile_probe is None:
        return True
    return bool(profile_probe["ready"])


def _required_vendored_skill_probe(
    *, name: str, repo_root: Path | None, command_configured: bool
) -> list[dict[str, object]] | None:
    if repo_root is None:
        return None
    required_skill_names = MANDATORY_VENDORED_SKILLS_BY_ROLE.get(name, ())
    if not required_skill_names and not command_configured:
        required_skill_names = DEFAULT_PATH_REQUIRED_VENDORED_SKILLS_BY_ROLE.get(
            name, ()
        )
    if not required_skill_names:
        return None
    return validate_required_vendored_skills(repo_root, required_skill_names)


def _vendored_skill_probe_ready(
    skill_probe: list[dict[str, object]] | None,
) -> bool:
    if skill_probe is None:
        return True
    return all(bool(item["ready"]) for item in skill_probe)


def _command_reason(
    command_reason: str | None,
    profile_probe: dict[str, object] | None,
    vendored_skill_probe: list[dict[str, object]] | None,
) -> str | None:
    reasons: list[str] = []
    if profile_probe is not None and not bool(profile_probe["ready"]):
        reasons.append(str(profile_probe["reason"]))
    if vendored_skill_probe is not None:
        reasons.extend(
            str(item["reason"])
            for item in vendored_skill_probe
            if not bool(item["ready"])
            and str(item.get("reason", "")).strip()
        )
    if command_reason:
        reasons.append(command_reason)
    if not reasons:
        return None
    return "; ".join(reasons)


def _resolve_command_executable(executable: str) -> tuple[bool, str | None]:
    if not executable:
        return False, None

    direct_path = Path(executable).expanduser()
    if direct_path.is_absolute():
        return direct_path.exists(), str(direct_path) if direct_path.exists() else None

    if any(sep in executable for sep in ("/", "\\")):
        resolved = direct_path.resolve()
        return resolved.exists(), str(resolved) if resolved.exists() else None

    resolved = shutil.which(executable)
    return resolved is not None, resolved


def _probe_bundled_codex_command(
    command: list[str], executable: str, executable_available: bool
) -> dict[str, object]:
    if not executable_available:
        return {
            "ready": False,
            "reason": "bundled Codex fallback requires 'codex' on PATH.",
        }

    probe_command = [executable, *command[1:-1], "--help"]
    try:
        result = subprocess.run(
            probe_command,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        return {
            "ready": False,
            "reason": f"bundled Codex fallback probe failed: {error}",
            "probe_command": probe_command,
        }

    if result.returncode == 0:
        return {
            "ready": True,
            "reason": None,
            "probe_command": probe_command,
        }

    message = (result.stderr or result.stdout or "").strip()
    if message:
        message = " ".join(message.split())
        reason = f"bundled Codex fallback probe failed: {message}"
    else:
        reason = (
            "bundled Codex fallback probe failed: "
            f"'{Path(executable).name} {' '.join(command[1:-1])} --help' exited with code {result.returncode}."
        )

    return {
        "ready": False,
        "reason": reason,
        "probe_command": probe_command,
        "probe_returncode": result.returncode,
    }


def _backend_blockers(
    *,
    state_root_exists: bool,
    tool_results: list[dict[str, object]],
    runner_match: dict[str, object] | None = None,
) -> list[str]:
    blockers: list[str] = []
    if not state_root_exists:
        blockers.append("resolved Sandcastle state root does not exist")
    blockers.extend(
        f"required tool '{result['tool']}' is not available"
        for result in tool_results
        if not bool(result["available"])
    )
    if runner_match is not None and not bool(runner_match.get("ready")):
        blockers.append(
            str(runner_match.get("reason", "")).strip()
            or "self-hosted runner match is not ready"
        )
    return blockers


def _command_blockers(
    command_results: list[dict[str, object]], readiness_level: str
) -> list[str]:
    blockers: list[str] = []
    for result in command_results:
        if readiness_level not in result["required_for"]:
            continue
        if bool(result["ready"]):
            continue
        reason = str(result.get("reason", "")).strip() or "command is not ready"
        blockers.append(f"{result['name']}: {reason}")
    return blockers


def _role_policy_blockers(
    role_policy_results: list[dict[str, object]], required_roles: tuple[str, ...]
) -> list[str]:
    blockers: list[str] = []
    required = set(required_roles)
    for result in role_policy_results:
        if str(result.get("name")) not in required:
            continue
        if bool(result.get("ready")):
            continue
        reason = str(result.get("reason", "")).strip() or "role policy is not ready"
        blockers.append(f"{result['name']}: {reason}")
    return blockers


if __name__ == "__main__":
    raise SystemExit(main())
