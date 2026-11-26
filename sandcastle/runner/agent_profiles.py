from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess

from .state_store import read_json_file

AGENT_PROFILE_CONFIG_RELATIVE_PATH = Path("sandcastle") / "config" / "agent-profiles.json"
REQUIRED_AGENT_ROLES = (
    "development-agent",
    "review-agent",
    "conflict-resolution-agent",
)
SUPPORTED_REASONING_EFFORTS = ("low", "medium", "high", "xhigh")
CODEX_RUNTIME_PROBE_PROMPT = "Reply with exactly OK."


@dataclass(frozen=True)
class AgentProfile:
    name: str
    model: str
    reasoning_effort: str


@dataclass(frozen=True)
class AgentRoleProfile:
    role_name: str
    profile_name: str
    model: str
    reasoning_effort: str


def resolve_agent_profile_config_path(repo_root: Path | str) -> Path:
    return Path(repo_root) / AGENT_PROFILE_CONFIG_RELATIVE_PATH


def _load_agent_profile_payload(
    repo_root: Path | str,
) -> tuple[Path, dict[str, object], dict[str, object]]:
    config_path = resolve_agent_profile_config_path(repo_root)
    payload = read_json_file(config_path)

    profiles_payload = payload.get("profiles")
    if not isinstance(profiles_payload, dict) or not profiles_payload:
        raise RuntimeError(
            f"agent profile config {config_path} must contain a non-empty 'profiles' object."
        )

    roles_payload = payload.get("roles")
    if not isinstance(roles_payload, dict) or not roles_payload:
        raise RuntimeError(
            f"agent profile config {config_path} must contain a non-empty 'roles' object."
        )

    return config_path, profiles_payload, roles_payload


def configured_agent_roles(repo_root: Path | str) -> tuple[str, ...]:
    _, _, roles_payload = _load_agent_profile_payload(repo_root)
    return tuple(str(role_name) for role_name in roles_payload.keys())


def load_agent_profile(role_name: str, repo_root: Path | str) -> AgentRoleProfile:
    config_path, profiles_payload, roles_payload = _load_agent_profile_payload(repo_root)

    role_payload = roles_payload.get(role_name)
    if not isinstance(role_payload, dict):
        raise RuntimeError(
            f"agent profile config {config_path} is missing role '{role_name}'."
        )

    profile_name = str(role_payload.get("profile", "")).strip()
    if not profile_name:
        raise RuntimeError(
            f"agent profile config {config_path} role '{role_name}' must declare a non-empty profile."
        )

    profile_payload = profiles_payload.get(profile_name)
    if not isinstance(profile_payload, dict):
        raise RuntimeError(
            f"agent profile config {config_path} role '{role_name}' references unknown profile '{profile_name}'."
        )

    model = str(profile_payload.get("model", "")).strip()
    if not model:
        raise RuntimeError(
            f"agent profile config {config_path} profile '{profile_name}' must declare a non-empty model."
        )

    reasoning_effort = str(profile_payload.get("reasoning_effort", "")).strip()
    if not reasoning_effort:
        raise RuntimeError(
            f"agent profile config {config_path} profile '{profile_name}' must declare a non-empty reasoning_effort."
        )
    if reasoning_effort not in SUPPORTED_REASONING_EFFORTS:
        supported_values = ", ".join(SUPPORTED_REASONING_EFFORTS)
        raise RuntimeError(
            f"agent profile config {config_path} profile '{profile_name}' declares unsupported reasoning_effort "
            f"'{reasoning_effort}'. Supported values: {supported_values}."
        )

    return AgentRoleProfile(
        role_name=role_name,
        profile_name=profile_name,
        model=model,
        reasoning_effort=reasoning_effort,
    )


def build_agent_profile_env(role_name: str, repo_root: Path | str) -> dict[str, str]:
    profile = load_agent_profile(role_name, repo_root)
    return {
        "SANDCASTLE_AGENT_ROLE": profile.role_name,
        "SANDCASTLE_AGENT_PROFILE": profile.profile_name,
        "SANDCASTLE_AGENT_MODEL": profile.model,
        "SANDCASTLE_AGENT_REASONING_EFFORT": profile.reasoning_effort,
    }


def probe_agent_role_runtime(
    role_name: str,
    repo_root: Path | str,
) -> dict[str, object]:
    profile = load_agent_profile(role_name, repo_root)
    executable = shutil.which("codex")
    probe_command = _build_codex_runtime_probe_command(
        executable=executable or "codex",
        profile=profile,
    )
    if executable is None:
        return {
            "ready": False,
            "reason": "Codex CLI is not available on PATH for agent profile runtime validation.",
            "probe_command": probe_command,
        }

    try:
        result = subprocess.run(
            probe_command,
            cwd=Path(repo_root),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        return {
            "ready": False,
            "reason": f"Codex runtime probe failed: {error}",
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
        reason = (
            f"Codex runtime rejected role '{role_name}' profile '{profile.profile_name}' "
            f"(model '{profile.model}', reasoning_effort '{profile.reasoning_effort}'): {message}"
        )
    else:
        reason = (
            f"Codex runtime rejected role '{role_name}' profile '{profile.profile_name}' "
            f"(model '{profile.model}', reasoning_effort '{profile.reasoning_effort}') "
            f"with exit code {result.returncode}."
        )
    return {
        "ready": False,
        "reason": reason,
        "probe_command": probe_command,
        "probe_returncode": result.returncode,
    }


def _build_codex_runtime_probe_command(
    *,
    executable: str,
    profile: AgentRoleProfile,
) -> list[str]:
    return [
        executable,
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--ephemeral",
        "--config",
        f'model_reasoning_effort="{profile.reasoning_effort}"',
        "--model",
        profile.model,
        CODEX_RUNTIME_PROBE_PROMPT,
    ]


def validate_agent_role_policies(
    repo_root: Path | str,
    required_roles: tuple[str, ...] = REQUIRED_AGENT_ROLES,
    probe_runtime: bool = False,
) -> list[dict[str, object]]:
    config_path = resolve_agent_profile_config_path(repo_root)
    results: list[dict[str, object]] = []
    for role_name in required_roles:
        try:
            profile = load_agent_profile(role_name, repo_root)
        except Exception as error:
            results.append(
                {
                    "name": role_name,
                    "ready": False,
                    "reason": str(error),
                    "config_path": str(config_path),
                }
            )
            continue

        results.append(
            {
                "name": role_name,
                "ready": True,
                "reason": None,
                "config_path": str(config_path),
                "profile": profile.profile_name,
                "model": profile.model,
                "reasoning_effort": profile.reasoning_effort,
            }
        )
        if not probe_runtime:
            continue

        runtime_probe = probe_agent_role_runtime(role_name, repo_root)
        if runtime_probe["ready"]:
            results[-1]["runtime_probe"] = runtime_probe
            continue

        results[-1]["ready"] = False
        results[-1]["reason"] = runtime_probe["reason"]
        results[-1]["runtime_probe"] = runtime_probe
    return results
