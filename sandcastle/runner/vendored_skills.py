from __future__ import annotations

import hashlib
import json
from pathlib import Path

VENDORED_SKILL_INVENTORY_RELATIVE_PATH = (
    Path("sandcastle") / "config" / "vendored-skills.json"
)


def resolve_vendored_skill_inventory_path(repo_root: Path | str) -> Path:
    return Path(repo_root) / VENDORED_SKILL_INVENTORY_RELATIVE_PATH


def validate_required_vendored_skills(
    repo_root: Path | str,
    required_skill_names: tuple[str, ...],
) -> list[dict[str, object]]:
    root = Path(repo_root)
    inventory_path = resolve_vendored_skill_inventory_path(root)
    inventory, inventory_error = _load_inventory(inventory_path)
    results: list[dict[str, object]] = []

    for skill_name in required_skill_names:
        skill_path = root / ".agents" / "skills" / skill_name
        result: dict[str, object] = {
            "name": skill_name,
            "ready": False,
            "reason": None,
            "skill_path": str(skill_path),
            "inventory_path": str(inventory_path),
        }

        if inventory_error is not None:
            result["reason"] = (
                f"required vendored skill '{skill_name}' cannot be validated because {inventory_error}"
            )
            results.append(result)
            continue

        assert inventory is not None
        skills_payload = inventory.get("skills")
        if not isinstance(skills_payload, dict):
            result["reason"] = (
                f"vendored skill inventory {inventory_path} must contain a 'skills' object."
            )
            results.append(result)
            continue

        entry = skills_payload.get(skill_name)
        if not isinstance(entry, dict):
            result["reason"] = (
                f"required vendored skill '{skill_name}' inventory entry is missing."
            )
            results.append(result)
            continue

        if not bool(entry.get("runtime_required")):
            result["reason"] = (
                f"required vendored skill '{skill_name}' must be marked runtime_required in {inventory_path}."
            )
            results.append(result)
            continue

        if not skill_path.is_dir():
            result["reason"] = (
                f"required vendored skill '{skill_name}' directory is missing at {skill_path}."
            )
            results.append(result)
            continue

        skill_md_path = skill_path / "SKILL.md"
        if not skill_md_path.is_file():
            result["reason"] = (
                f"required vendored skill '{skill_name}' is missing SKILL.md at {skill_md_path}."
            )
            results.append(result)
            continue

        files_payload = entry.get("files")
        if not isinstance(files_payload, dict) or not files_payload:
            result["reason"] = (
                f"required vendored skill '{skill_name}' must declare tracked files in {inventory_path}."
            )
            results.append(result)
            continue

        try:
            actual_files = {
                path.relative_to(skill_path).as_posix(): path
                for path in skill_path.rglob("*")
                if path.is_file()
            }
        except OSError as error:
            result["reason"] = (
                f"required vendored skill '{skill_name}' could not be read from {skill_path}: {error}."
            )
            results.append(result)
            continue

        expected_files = set(files_payload.keys())
        actual_file_names = set(actual_files.keys())

        missing_files = sorted(expected_files - actual_file_names)
        if missing_files:
            result["reason"] = (
                f"required vendored skill '{skill_name}' is missing tracked files: "
                f"{', '.join(missing_files)}."
            )
            results.append(result)
            continue

        untracked_files = sorted(actual_file_names - expected_files)
        if untracked_files:
            result["reason"] = (
                f"required vendored skill '{skill_name}' has untracked files not declared in {inventory_path}: "
                f"{', '.join(untracked_files)}."
            )
            results.append(result)
            continue

        hash_mismatch = _first_hash_mismatch(
            skill_name=skill_name,
            files_payload=files_payload,
            actual_files=actual_files,
        )
        if hash_mismatch is not None:
            result["reason"] = hash_mismatch
            results.append(result)
            continue

        result["ready"] = True
        results.append(result)

    return results


def _load_inventory(path: Path) -> tuple[dict[str, object] | None, str | None]:
    if not path.exists():
        return None, f"vendored skill inventory {path} is missing."

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        return None, f"vendored skill inventory {path} could not be read: {error}."
    except json.JSONDecodeError as error:
        return None, f"vendored skill inventory {path} is invalid JSON: {error}."

    if not isinstance(payload, dict):
        return None, f"vendored skill inventory {path} must contain a top-level object."
    return payload, None


def _first_hash_mismatch(
    *,
    skill_name: str,
    files_payload: dict[str, object],
    actual_files: dict[str, Path],
) -> str | None:
    for relative_path in sorted(files_payload.keys()):
        file_payload = files_payload[relative_path]
        if not isinstance(file_payload, dict):
            return (
                f"required vendored skill '{skill_name}' inventory entry for '{relative_path}' must be an object."
            )

        expected_hash = str(file_payload.get("sha256", "")).strip().lower()
        if not expected_hash:
            return (
                f"required vendored skill '{skill_name}' inventory entry for '{relative_path}' must declare sha256."
            )

        actual_path = actual_files[relative_path]
        try:
            actual_hash = hashlib.sha256(actual_path.read_bytes()).hexdigest()
        except OSError as error:
            return (
                f"required vendored skill '{skill_name}' could not be read from {actual_path}: {error}."
            )

        if actual_hash != expected_hash:
            return (
                f"required vendored skill '{skill_name}' tracked file '{relative_path}' has hash mismatch "
                f"(expected {expected_hash}, found {actual_hash})."
            )

    return None
