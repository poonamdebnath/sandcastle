from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sandcastle.runner.github_backend import (
    _handle_validate,
    _handle_prepare_host,
    _backend_blockers,
    _command_blockers,
    _role_policy_blockers,
    _validate_agent_command,
)


class GithubBackendValidationTests(unittest.TestCase):
    def test_missing_dev_agent_command_uses_bundled_codex_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "sandcastle" / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            self._write_required_tdd_inventory(root)
            (config_dir / "agent-profiles.json").write_text(
                json.dumps(
                    {
                        "profiles": {
                            "coding-default": {
                                "model": "gpt-5.3-codex",
                                "reasoning_effort": "xhigh",
                            },
                            "review-default": {
                                "model": "gpt-5.4-1",
                                "reasoning_effort": "xhigh",
                            },
                            "conflict-default": {
                                "model": "gpt-5.4-1",
                                "reasoning_effort": "medium",
                            }
                        },
                        "roles": {
                            "development-agent": {"profile": "coding-default"},
                            "review-agent": {"profile": "review-default"},
                            "conflict-resolution-agent": {"profile": "conflict-default"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                with patch(
                    "sandcastle.runner.github_backend.shutil.which",
                    return_value=r"C:\Users\codex\codex.exe",
                ):
                    with patch(
                        "sandcastle.runner.github_backend.subprocess.run",
                        return_value=Mock(returncode=0, stdout="", stderr=""),
                    ) as probe:
                        result = _validate_agent_command(
                            name="development-agent",
                            env_var="SANDCASTLE_DEV_AGENT_COMMAND_JSON",
                            required_for=("autonomous_issue_execution",),
                            repo_root=root,
                        )

        self.assertFalse(result["configured"])
        self.assertTrue(result["ready"])
        self.assertEqual(result["fallback"], "bundled-codex-tdd")
        self.assertEqual(result["resolved_executable"], r"C:\Users\codex\codex.exe")
        self.assertIn("--config", result["command"])
        self.assertIn('model_reasoning_effort="xhigh"', result["command"])
        self.assertIn("--model", result["command"])
        self.assertIn("gpt-5.3-codex", result["command"])
        self.assertEqual(
            result["readiness_probe"]["probe_command"],
            [
                r"C:\Users\codex\codex.exe",
                "--ask-for-approval",
                "never",
                "exec",
                "--sandbox",
                "workspace-write",
                "--config",
                'model_reasoning_effort="xhigh"',
                "--model",
                "gpt-5.3-codex",
                "--help",
            ],
        )
        self.assertEqual(
            result["profile_probe"]["probe_command"],
            [
                r"C:\Users\codex\codex.exe",
                "exec",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--color",
                "never",
                "--ephemeral",
                "--config",
                'model_reasoning_effort="xhigh"',
                "--model",
                "gpt-5.3-codex",
                "Reply with exactly OK.",
            ],
        )
        self.assertIn("$tdd", str(result["command"][-1]))
        self.assertEqual(probe.call_count, 2)

    def test_missing_dev_agent_command_is_not_ready_when_bundled_probe_fails(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with patch(
                "sandcastle.runner.github_backend.shutil.which",
                return_value=r"C:\Users\codex\codex.exe",
            ):
                with patch(
                    "sandcastle.runner.github_backend.subprocess.run",
                    return_value=Mock(
                        returncode=2,
                        stdout="",
                        stderr="error: unexpected argument '--ask-for-approval'",
                    ),
                ):
                    result = _validate_agent_command(
                        name="development-agent",
                        env_var="SANDCASTLE_DEV_AGENT_COMMAND_JSON",
                        required_for=("autonomous_issue_execution",),
                    )

        self.assertFalse(result["configured"])
        self.assertFalse(result["ready"])
        self.assertIn("bundled Codex fallback probe failed", str(result["reason"]))
        self.assertEqual(result["readiness_probe"]["probe_returncode"], 2)

    def test_missing_dev_agent_command_is_not_ready_when_role_profile_probe_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "sandcastle" / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            self._write_required_tdd_inventory(root)
            (config_dir / "agent-profiles.json").write_text(
                json.dumps(
                    {
                        "profiles": {
                            "coding-default": {
                                "model": "definitely-not-a-real-model",
                                "reasoning_effort": "xhigh",
                            },
                            "review-default": {
                                "model": "gpt-5.4-1",
                                "reasoning_effort": "xhigh",
                            },
                            "conflict-default": {
                                "model": "gpt-5.4-1",
                                "reasoning_effort": "medium",
                            }
                        },
                        "roles": {
                            "development-agent": {"profile": "coding-default"},
                            "review-agent": {"profile": "review-default"},
                            "conflict-resolution-agent": {"profile": "conflict-default"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                with patch(
                    "sandcastle.runner.github_backend.shutil.which",
                    return_value=r"C:\Users\codex\codex.exe",
                ):
                    with patch(
                        "sandcastle.runner.github_backend.subprocess.run",
                        return_value=Mock(returncode=0, stdout="", stderr=""),
                    ):
                        with patch(
                            "sandcastle.runner.github_backend.probe_agent_role_runtime",
                            return_value={
                                "ready": False,
                                "reason": "Codex runtime rejected role 'development-agent'",
                                "probe_command": ["codex", "exec"],
                            },
                        ):
                            result = _validate_agent_command(
                                name="development-agent",
                                env_var="SANDCASTLE_DEV_AGENT_COMMAND_JSON",
                                required_for=("autonomous_issue_execution",),
                                repo_root=root,
                            )

        self.assertFalse(result["configured"])
        self.assertFalse(result["ready"])
        self.assertEqual(
            result["reason"],
            "Codex runtime rejected role 'development-agent'",
        )
        self.assertFalse(result["profile_probe"]["ready"])

    def test_missing_review_agent_command_uses_bundled_codex_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "sandcastle" / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            self._write_required_code_review_manifest(root)
            (config_dir / "agent-profiles.json").write_text(
                json.dumps(
                    {
                        "profiles": {
                            "coding-default": {
                                "model": "gpt-5.3-codex",
                                "reasoning_effort": "xhigh",
                            },
                            "review-default": {
                                "model": "gpt-5.4-1",
                                "reasoning_effort": "xhigh",
                            },
                            "conflict-default": {
                                "model": "gpt-5.4-1",
                                "reasoning_effort": "medium",
                            },
                        },
                        "roles": {
                            "development-agent": {"profile": "coding-default"},
                            "review-agent": {"profile": "review-default"},
                            "conflict-resolution-agent": {"profile": "conflict-default"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                with patch(
                    "sandcastle.runner.github_backend.shutil.which",
                    return_value=r"C:\Users\codex\codex.exe",
                ):
                    with patch(
                        "sandcastle.runner.github_backend.subprocess.run",
                        return_value=Mock(returncode=0, stdout="", stderr=""),
                    ) as probe:
                        result = _validate_agent_command(
                            name="review-agent",
                            env_var="SANDCASTLE_REVIEW_AGENT_COMMAND_JSON",
                            required_for=("autonomous_issue_execution",),
                            repo_root=root,
                        )

        self.assertFalse(result["configured"])
        self.assertTrue(result["ready"])
        self.assertEqual(result["fallback"], "bundled-codex-code-review")
        self.assertEqual(result["resolved_executable"], r"C:\Users\codex\codex.exe")
        self.assertIn("--config", result["command"])
        self.assertIn('model_reasoning_effort="xhigh"', result["command"])
        self.assertIn("--model", result["command"])
        self.assertIn("gpt-5.4-1", result["command"])
        self.assertIn("$code-review", str(result["command"][-1]))
        self.assertIn("merge-base against origin/main", str(result["command"][-1]))
        self.assertEqual(probe.call_count, 2)

    def test_missing_conflict_agent_command_uses_bundled_codex_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "sandcastle" / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            self._write_required_conflict_manifest(root)
            (config_dir / "agent-profiles.json").write_text(
                json.dumps(
                    {
                        "profiles": {
                            "coding-default": {
                                "model": "gpt-5.3-codex",
                                "reasoning_effort": "xhigh",
                            },
                            "review-default": {
                                "model": "gpt-5.4-1",
                                "reasoning_effort": "xhigh",
                            },
                            "conflict-default": {
                                "model": "gpt-5.4-1",
                                "reasoning_effort": "medium",
                            },
                        },
                        "roles": {
                            "development-agent": {"profile": "coding-default"},
                            "review-agent": {"profile": "review-default"},
                            "conflict-resolution-agent": {"profile": "conflict-default"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                with patch(
                    "sandcastle.runner.github_backend.shutil.which",
                    return_value=r"C:\Users\codex\codex.exe",
                ):
                    with patch(
                        "sandcastle.runner.github_backend.subprocess.run",
                        return_value=Mock(returncode=0, stdout="", stderr=""),
                    ) as probe:
                        result = _validate_agent_command(
                            name="conflict-resolution-agent",
                            env_var="SANDCASTLE_CONFLICT_AGENT_COMMAND_JSON",
                            required_for=("full_autonomous_loop",),
                            repo_root=root,
                        )

        self.assertFalse(result["configured"])
        self.assertTrue(result["ready"])
        self.assertEqual(
            result["fallback"],
            "bundled-codex-resolving-merge-conflicts",
        )
        self.assertEqual(result["resolved_executable"], r"C:\Users\codex\codex.exe")
        self.assertIn("--config", result["command"])
        self.assertIn('model_reasoning_effort="medium"', result["command"])
        self.assertIn("--model", result["command"])
        self.assertIn("gpt-5.4-1", result["command"])
        self.assertIn("$resolving-merge-conflicts", str(result["command"][-1]))
        self.assertEqual(probe.call_count, 2)

    def test_invalid_json_agent_command_is_rejected(self) -> None:
        with patch.dict(
            os.environ,
            {"SANDCASTLE_DEV_AGENT_COMMAND_JSON": "[not-json"},
            clear=True,
        ):
            result = _validate_agent_command(
                name="development-agent",
                env_var="SANDCASTLE_DEV_AGENT_COMMAND_JSON",
                required_for=("autonomous_issue_execution",),
            )

        self.assertTrue(result["configured"])
        self.assertFalse(result["valid"])
        self.assertIn("must be a JSON array of command parts", str(result["reason"]))

    def test_non_string_agent_command_part_is_rejected(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SANDCASTLE_REVIEW_AGENT_COMMAND_JSON": json.dumps(
                    ["python", 7, "--brief", "{review_brief_path}"]
                )
            },
            clear=True,
        ):
            result = _validate_agent_command(
                name="review-agent",
                env_var="SANDCASTLE_REVIEW_AGENT_COMMAND_JSON",
                required_for=("autonomous_issue_execution",),
            )

        self.assertTrue(result["configured"])
        self.assertFalse(result["valid"])
        self.assertEqual(
            result["reason"],
            "SANDCASTLE_REVIEW_AGENT_COMMAND_JSON entries must all be strings.",
        )

    def test_missing_executable_is_not_ready(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SANDCASTLE_CONFLICT_AGENT_COMMAND_JSON": json.dumps(
                    ["missing-tool", "--brief", "{conflict_brief_path}"]
                )
            },
            clear=True,
        ):
            with patch("sandcastle.runner.github_backend.shutil.which", return_value=None):
                result = _validate_agent_command(
                    name="conflict-resolution-agent",
                    env_var="SANDCASTLE_CONFLICT_AGENT_COMMAND_JSON",
                    required_for=("full_autonomous_loop",),
                )

        self.assertTrue(result["valid"])
        self.assertFalse(result["ready"])
        self.assertEqual(result["executable"], "missing-tool")
        self.assertIn("is not available", str(result["reason"]))

    def test_available_executable_is_ready(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SANDCASTLE_DEV_AGENT_COMMAND_JSON": json.dumps(
                    ["python", "-m", "my_dev_agent"]
                )
            },
            clear=True,
        ):
            with patch(
                "sandcastle.runner.github_backend.shutil.which",
                return_value=r"C:\Python\python.exe",
            ):
                result = _validate_agent_command(
                    name="development-agent",
                    env_var="SANDCASTLE_DEV_AGENT_COMMAND_JSON",
                    required_for=("autonomous_issue_execution",),
                )

        self.assertTrue(result["valid"])
        self.assertTrue(result["ready"])
        self.assertEqual(result["resolved_executable"], r"C:\Python\python.exe")

    def test_configured_dev_agent_command_does_not_require_repo_local_tdd_inventory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "sandcastle" / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "agent-profiles.json").write_text(
                json.dumps(
                    {
                        "profiles": {
                            "coding-default": {
                                "model": "gpt-5.3-codex",
                                "reasoning_effort": "xhigh",
                            },
                        },
                        "roles": {
                            "development-agent": {"profile": "coding-default"},
                        },
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "SANDCASTLE_DEV_AGENT_COMMAND_JSON": json.dumps(
                        ["python", "-m", "my_dev_agent"]
                    )
                },
                clear=True,
            ):
                with patch(
                    "sandcastle.runner.github_backend.shutil.which",
                    return_value=r"C:\Python\python.exe",
                ):
                    with patch(
                        "sandcastle.runner.github_backend.probe_agent_role_runtime",
                        return_value={"ready": True, "reason": None},
                    ):
                        result = _validate_agent_command(
                            name="development-agent",
                            env_var="SANDCASTLE_DEV_AGENT_COMMAND_JSON",
                            required_for=("autonomous_issue_execution",),
                            repo_root=root,
                        )

        self.assertTrue(result["valid"])
        self.assertTrue(result["ready"])
        self.assertIsNone(result["required_vendored_skills"])

    def test_configured_conflict_agent_command_requires_repo_local_skill_inventory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "sandcastle" / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "agent-profiles.json").write_text(
                json.dumps(
                    {
                        "profiles": {
                            "conflict-default": {
                                "model": "gpt-5.4-1",
                                "reasoning_effort": "medium",
                            },
                        },
                        "roles": {
                            "conflict-resolution-agent": {
                                "profile": "conflict-default"
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "SANDCASTLE_CONFLICT_AGENT_COMMAND_JSON": json.dumps(
                        ["python", "-m", "my_conflict_agent"]
                    )
                },
                clear=True,
            ):
                with patch(
                    "sandcastle.runner.github_backend.shutil.which",
                    return_value=r"C:\Python\python.exe",
                ):
                    with patch(
                        "sandcastle.runner.github_backend.probe_agent_role_runtime",
                        return_value={"ready": True, "reason": None},
                    ):
                        result = _validate_agent_command(
                            name="conflict-resolution-agent",
                            env_var="SANDCASTLE_CONFLICT_AGENT_COMMAND_JSON",
                            required_for=("full_autonomous_loop",),
                            repo_root=root,
                        )

        self.assertTrue(result["valid"])
        self.assertFalse(result["ready"])
        self.assertIn("resolving-merge-conflicts", str(result["reason"]))

    def test_configured_review_agent_command_is_not_ready_when_role_profile_probe_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "sandcastle" / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            self._write_required_code_review_manifest(root)
            (config_dir / "agent-profiles.json").write_text(
                json.dumps(
                    {
                        "profiles": {
                            "coding-default": {
                                "model": "gpt-5.3-codex",
                                "reasoning_effort": "xhigh",
                            },
                            "review-default": {
                                "model": "definitely-not-a-real-model",
                                "reasoning_effort": "xhigh",
                            },
                            "conflict-default": {
                                "model": "gpt-5.4-1",
                                "reasoning_effort": "medium",
                            }
                        },
                        "roles": {
                            "development-agent": {"profile": "coding-default"},
                            "review-agent": {"profile": "review-default"},
                            "conflict-resolution-agent": {"profile": "conflict-default"},
                        },
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "SANDCASTLE_REVIEW_AGENT_COMMAND_JSON": json.dumps(
                        ["python", "-m", "my_review_agent"]
                    )
                },
                clear=True,
            ):
                with patch(
                    "sandcastle.runner.github_backend.shutil.which",
                    return_value=r"C:\Python\python.exe",
                ):
                    with patch(
                        "sandcastle.runner.github_backend.probe_agent_role_runtime",
                        return_value={
                            "ready": False,
                            "reason": "Codex runtime rejected role 'review-agent'",
                            "probe_command": ["codex", "exec"],
                        },
                    ):
                        result = _validate_agent_command(
                            name="review-agent",
                            env_var="SANDCASTLE_REVIEW_AGENT_COMMAND_JSON",
                            required_for=("autonomous_issue_execution",),
                            repo_root=root,
                        )

        self.assertTrue(result["configured"])
        self.assertTrue(result["valid"])
        self.assertFalse(result["ready"])
        self.assertEqual(
            result["reason"],
            "Codex runtime rejected role 'review-agent'",
        )
        self.assertFalse(result["profile_probe"]["ready"])

    def test_relative_executable_path_is_resolved_as_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            executable = root / "tools" / "agent.cmd"
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_text("echo agent\n", encoding="utf-8")
            previous_cwd = Path.cwd()
            os.chdir(root)
            try:
                with patch.dict(
                    os.environ,
                    {
                        "SANDCASTLE_DEV_AGENT_COMMAND_JSON": json.dumps(
                            [r".\tools\agent.cmd", "--brief", "{brief_path}"]
                        )
                    },
                    clear=True,
                ):
                    result = _validate_agent_command(
                        name="development-agent",
                        env_var="SANDCASTLE_DEV_AGENT_COMMAND_JSON",
                        required_for=("autonomous_issue_execution",),
                    )
            finally:
                os.chdir(previous_cwd)

        self.assertTrue(result["ready"])
        self.assertTrue(str(result["resolved_executable"]).endswith("tools\\agent.cmd"))

    def test_blocker_helpers_report_only_missing_dependencies(self) -> None:
        backend = _backend_blockers(
            state_root_exists=False,
            tool_results=[
                {"tool": "git", "available": True},
                {"tool": "dotnet", "available": False},
            ],
        )
        commands = _command_blockers(
            [
                {
                    "name": "development-agent",
                    "required_for": ["autonomous_issue_execution"],
                    "ready": False,
                    "reason": "missing",
                },
                {
                    "name": "conflict-resolution-agent",
                    "required_for": ["full_autonomous_loop"],
                    "ready": False,
                    "reason": "missing",
                },
            ],
            "autonomous_issue_execution",
        )

        self.assertEqual(
            backend,
            [
                "resolved Sandcastle state root does not exist",
                "required tool 'dotnet' is not available",
            ],
        )
        self.assertEqual(commands, ["development-agent: missing"])

    def test_role_policy_blockers_only_report_required_missing_roles(self) -> None:
        blockers = _role_policy_blockers(
            [
                {"name": "development-agent", "ready": False, "reason": "missing profile"},
                {"name": "review-agent", "ready": True, "reason": None},
                {"name": "conflict-resolution-agent", "ready": False, "reason": "missing profile"},
            ],
            ("development-agent", "review-agent"),
        )

        self.assertEqual(blockers, ["development-agent: missing profile"])

    def test_validate_surfaces_required_vendored_skill_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "sandcastle" / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "agent-profiles.json").write_text(
                json.dumps(
                    {
                        "profiles": {
                            "coding-default": {
                                "model": "gpt-5.3-codex",
                                "reasoning_effort": "xhigh",
                            },
                            "review-default": {
                                "model": "gpt-5.4-1",
                                "reasoning_effort": "xhigh",
                            },
                            "conflict-default": {
                                "model": "gpt-5.4-1",
                                "reasoning_effort": "medium",
                            },
                        },
                        "roles": {
                            "development-agent": {"profile": "coding-default"},
                            "review-agent": {"profile": "review-default"},
                            "conflict-resolution-agent": {"profile": "conflict-default"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            (config_dir / "controller-backend.json").write_text(
                json.dumps(
                    {
                        "backend": {
                            "kind": "github-actions",
                            "runner_labels": ["self-hosted"],
                            "required_tools": [],
                            "required_repo_variable": "SANDCASTLE_ENABLED",
                            "state_root_env_var": "SANDCASTLE_STATE_ROOT",
                            "default_state_root": ".sandcastle-state",
                            "credentials": [],
                            "notes": [],
                        }
                    }
                ),
                encoding="utf-8",
            )
            state_root = root / ".sandcastle-state"
            state_root.mkdir(parents=True, exist_ok=True)
            output_path = root / "validation.json"

            with patch.dict(
                os.environ,
                {
                    "SANDCASTLE_STATE_ROOT": str(state_root),
                    "SANDCASTLE_REVIEW_AGENT_COMMAND_JSON": json.dumps(
                        ["python", "-m", "review_agent"]
                    ),
                },
                clear=True,
            ):
                with patch(
                    "sandcastle.runner.github_backend.shutil.which",
                    return_value=r"C:\tools\tool.exe",
                ):
                    with patch(
                        "sandcastle.runner.github_backend.subprocess.run",
                        return_value=Mock(returncode=0, stdout="", stderr=""),
                    ):
                        with patch(
                            "sandcastle.runner.github_backend.validate_agent_role_policies",
                            return_value=[
                                {"name": "development-agent", "ready": True, "reason": None},
                                {"name": "review-agent", "ready": True, "reason": None},
                                {
                                    "name": "conflict-resolution-agent",
                                    "ready": True,
                                    "reason": None,
                                },
                            ],
                        ):
                            exit_code = _handle_validate(
                                config_path=config_dir / "controller-backend.json",
                                repo_root=root,
                                output_path=output_path,
                            )

            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["ready_for_autonomous_issue_execution"])
        self.assertIn(
            "development-agent: required vendored skill 'tdd'",
            payload["autonomous_issue_execution_blockers"][0],
        )

    def test_validate_surfaces_required_review_vendored_skill_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "sandcastle" / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            self._write_required_tdd_inventory(root)
            (config_dir / "agent-profiles.json").write_text(
                json.dumps(
                    {
                        "profiles": {
                            "coding-default": {
                                "model": "gpt-5.3-codex",
                                "reasoning_effort": "xhigh",
                            },
                            "review-default": {
                                "model": "gpt-5.4-1",
                                "reasoning_effort": "xhigh",
                            },
                            "conflict-default": {
                                "model": "gpt-5.4-1",
                                "reasoning_effort": "medium",
                            },
                        },
                        "roles": {
                            "development-agent": {"profile": "coding-default"},
                            "review-agent": {"profile": "review-default"},
                            "conflict-resolution-agent": {"profile": "conflict-default"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            (config_dir / "controller-backend.json").write_text(
                json.dumps(
                    {
                        "backend": {
                            "kind": "github-actions",
                            "runner_labels": ["self-hosted"],
                            "required_tools": [],
                            "required_repo_variable": "SANDCASTLE_ENABLED",
                            "state_root_env_var": "SANDCASTLE_STATE_ROOT",
                            "default_state_root": ".sandcastle-state",
                            "credentials": [],
                            "notes": [],
                        }
                    }
                ),
                encoding="utf-8",
            )
            state_root = root / ".sandcastle-state"
            state_root.mkdir(parents=True, exist_ok=True)
            output_path = root / "validation.json"

            with patch.dict(
                os.environ,
                {
                    "SANDCASTLE_STATE_ROOT": str(state_root),
                },
                clear=True,
            ):
                with patch(
                    "sandcastle.runner.github_backend.shutil.which",
                    return_value=r"C:\tools\tool.exe",
                ):
                    with patch(
                        "sandcastle.runner.github_backend.subprocess.run",
                        return_value=Mock(returncode=0, stdout="", stderr=""),
                    ):
                        with patch(
                            "sandcastle.runner.github_backend.validate_agent_role_policies",
                            return_value=[
                                {"name": "development-agent", "ready": True, "reason": None},
                                {"name": "review-agent", "ready": True, "reason": None},
                                {
                                    "name": "conflict-resolution-agent",
                                    "ready": True,
                                    "reason": None,
                                },
                            ],
                        ):
                            exit_code = _handle_validate(
                                config_path=config_dir / "controller-backend.json",
                                repo_root=root,
                                output_path=output_path,
                            )

            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["ready_for_autonomous_issue_execution"])
        self.assertTrue(
            any(
                "review-agent: required vendored skill 'code-review'"
                in blocker
                for blocker in payload["autonomous_issue_execution_blockers"]
            )
        )

    def test_validate_surfaces_required_conflict_skill_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "sandcastle" / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            self._write_required_tdd_inventory(root)
            (config_dir / "agent-profiles.json").write_text(
                json.dumps(
                    {
                        "profiles": {
                            "coding-default": {
                                "model": "gpt-5.3-codex",
                                "reasoning_effort": "xhigh",
                            },
                            "review-default": {
                                "model": "gpt-5.4-1",
                                "reasoning_effort": "xhigh",
                            },
                            "conflict-default": {
                                "model": "gpt-5.4-1",
                                "reasoning_effort": "medium",
                            },
                        },
                        "roles": {
                            "development-agent": {"profile": "coding-default"},
                            "review-agent": {"profile": "review-default"},
                            "conflict-resolution-agent": {"profile": "conflict-default"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            (config_dir / "controller-backend.json").write_text(
                json.dumps(
                    {
                        "backend": {
                            "kind": "github-actions",
                            "runner_labels": ["self-hosted"],
                            "required_tools": [],
                            "required_repo_variable": "SANDCASTLE_ENABLED",
                            "state_root_env_var": "SANDCASTLE_STATE_ROOT",
                            "default_state_root": ".sandcastle-state",
                            "credentials": [],
                            "notes": [],
                        }
                    }
                ),
                encoding="utf-8",
            )
            state_root = root / ".sandcastle-state"
            state_root.mkdir(parents=True, exist_ok=True)
            output_path = root / "validation.json"

            with patch.dict(
                os.environ,
                {
                    "SANDCASTLE_STATE_ROOT": str(state_root),
                    "SANDCASTLE_REVIEW_AGENT_COMMAND_JSON": json.dumps(
                        ["python", "-m", "review_agent"]
                    ),
                    "SANDCASTLE_CONFLICT_AGENT_COMMAND_JSON": json.dumps(
                        ["python", "-m", "conflict_agent"]
                    ),
                },
                clear=True,
            ):
                with patch(
                    "sandcastle.runner.github_backend.shutil.which",
                    return_value=r"C:\tools\tool.exe",
                ):
                    with patch(
                        "sandcastle.runner.github_backend.subprocess.run",
                        return_value=Mock(returncode=0, stdout="", stderr=""),
                    ):
                        with patch(
                            "sandcastle.runner.github_backend.validate_agent_role_policies",
                            return_value=[
                                {"name": "development-agent", "ready": True, "reason": None},
                                {"name": "review-agent", "ready": True, "reason": None},
                                {
                                    "name": "conflict-resolution-agent",
                                    "ready": True,
                                    "reason": None,
                                },
                            ],
                        ):
                            exit_code = _handle_validate(
                                config_path=config_dir / "controller-backend.json",
                                repo_root=root,
                                output_path=output_path,
                            )

            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["ready_for_full_autonomous_loop"])
        self.assertIn(
            "conflict-resolution-agent: required vendored skill 'resolving-merge-conflicts'",
            payload["full_autonomous_loop_blockers"][0],
        )

    def test_validate_requires_matching_online_runner_for_self_hosted_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "sandcastle" / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "controller-backend.json").write_text(
                json.dumps(
                    {
                        "backend": {
                            "kind": "github-actions-self-hosted",
                            "runner_labels": ["self-hosted", "sandcastle"],
                            "required_tools": [],
                            "required_repo_variable": "SANDCASTLE_EXECUTION_ENABLED",
                            "state_root_env_var": "SANDCASTLE_STATE_ROOT",
                            "default_state_root": "git-common-dir/sandcastle-state",
                            "credentials": [],
                            "notes": [],
                        }
                    }
                ),
                encoding="utf-8",
            )
            state_root = root / "sandcastle-state"
            state_root.mkdir(parents=True, exist_ok=True)
            output_path = root / "validation.json"

            responses = iter(
                [
                    Mock(returncode=0, stdout="", stderr=""),
                    Mock(
                        returncode=0,
                        stdout=json.dumps({"nameWithOwner": "octo-org/octo-repo"}),
                        stderr="",
                    ),
                    Mock(
                        returncode=0,
                        stdout=json.dumps(
                            {
                                "permissions": {
                                    "admin": True,
                                    "maintain": False,
                                    "push": True,
                                    "triage": True,
                                    "pull": True,
                                }
                            }
                        ),
                        stderr="",
                    ),
                    Mock(
                        returncode=0,
                        stdout=json.dumps(
                            {
                                "total_count": 1,
                                "runners": [
                                    {
                                        "name": "vm-dep-dev-sand",
                                        "status": "online",
                                        "labels": [
                                            {"name": "self-hosted", "type": "read-only"},
                                            {"name": "avd", "type": "custom"},
                                        ],
                                    }
                                ],
                            }
                        ),
                        stderr="",
                    ),
                ]
            )

            with patch.dict(
                os.environ,
                {"SANDCASTLE_STATE_ROOT": str(state_root)},
                clear=True,
            ):
                with patch(
                    "sandcastle.runner.github_backend._validate_agent_command",
                    side_effect=lambda **kwargs: {
                        "name": kwargs["name"],
                        "required_for": list(kwargs["required_for"]),
                        "ready": True,
                        "reason": None,
                    },
                ):
                    with patch(
                        "sandcastle.runner.github_backend.validate_agent_role_policies",
                        return_value=[
                            {"name": "development-agent", "ready": True, "reason": None},
                            {"name": "review-agent", "ready": True, "reason": None},
                            {
                                "name": "conflict-resolution-agent",
                                "ready": True,
                                "reason": None,
                            },
                        ],
                    ):
                        with patch(
                            "sandcastle.runner.github_backend.subprocess.run",
                            side_effect=lambda *args, **kwargs: next(responses),
                        ):
                            exit_code = _handle_validate(
                                config_path=config_dir / "controller-backend.json",
                                repo_root=root,
                                output_path=output_path,
                            )

            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["ready_for_backend_wakeups"])
        self.assertIn(
            "no repository self-hosted runner is registered with labels",
            payload["backend_blockers"][0],
        )

    def test_validate_requires_admin_visibility_for_host_side_self_hosted_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "sandcastle" / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "controller-backend.json").write_text(
                json.dumps(
                    {
                        "backend": {
                            "kind": "github-actions-self-hosted",
                            "runner_labels": ["self-hosted", "sandcastle"],
                            "required_tools": [],
                            "required_repo_variable": "SANDCASTLE_EXECUTION_ENABLED",
                            "state_root_env_var": "SANDCASTLE_STATE_ROOT",
                            "default_state_root": "git-common-dir/sandcastle-state",
                            "credentials": [],
                            "notes": [],
                        }
                    }
                ),
                encoding="utf-8",
            )
            state_root = root / "sandcastle-state"
            state_root.mkdir(parents=True, exist_ok=True)
            output_path = root / "validation.json"

            responses = iter(
                [
                    Mock(returncode=0, stdout="", stderr=""),
                    Mock(
                        returncode=0,
                        stdout=json.dumps({"nameWithOwner": "octo-org/octo-repo"}),
                        stderr="",
                    ),
                    Mock(
                        returncode=0,
                        stdout=json.dumps(
                            {
                                "permissions": {
                                    "admin": False,
                                    "maintain": False,
                                    "push": True,
                                    "triage": True,
                                    "pull": True,
                                }
                            }
                        ),
                        stderr="",
                    ),
                ]
            )

            with patch.dict(
                os.environ,
                {"SANDCASTLE_STATE_ROOT": str(state_root)},
                clear=True,
            ):
                with patch(
                    "sandcastle.runner.github_backend._validate_agent_command",
                    side_effect=lambda **kwargs: {
                        "name": kwargs["name"],
                        "required_for": list(kwargs["required_for"]),
                        "ready": True,
                        "reason": None,
                    },
                ):
                    with patch(
                        "sandcastle.runner.github_backend.validate_agent_role_policies",
                        return_value=[
                            {"name": "development-agent", "ready": True, "reason": None},
                            {"name": "review-agent", "ready": True, "reason": None},
                            {
                                "name": "conflict-resolution-agent",
                                "ready": True,
                                "reason": None,
                            },
                        ],
                    ):
                        with patch(
                            "sandcastle.runner.github_backend.subprocess.run",
                            side_effect=lambda *args, **kwargs: next(responses),
                        ):
                            exit_code = _handle_validate(
                                config_path=config_dir / "controller-backend.json",
                                repo_root=root,
                                output_path=output_path,
                            )

            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["ready_for_backend_wakeups"])
        self.assertIn(
            "lacks repository Actions admin access",
            payload["backend_blockers"][0],
        )

    def test_validate_skips_runner_inventory_check_inside_github_actions_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "sandcastle" / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "controller-backend.json").write_text(
                json.dumps(
                    {
                        "backend": {
                            "kind": "github-actions-self-hosted",
                            "runner_labels": ["self-hosted", "sandcastle"],
                            "required_tools": [],
                            "required_repo_variable": "SANDCASTLE_EXECUTION_ENABLED",
                            "state_root_env_var": "SANDCASTLE_STATE_ROOT",
                            "default_state_root": "git-common-dir/sandcastle-state",
                            "credentials": [],
                            "notes": [],
                        }
                    }
                ),
                encoding="utf-8",
            )
            state_root = root / "sandcastle-state"
            state_root.mkdir(parents=True, exist_ok=True)
            output_path = root / "validation.json"

            with patch.dict(
                os.environ,
                {
                    "SANDCASTLE_STATE_ROOT": str(state_root),
                    "GITHUB_ACTIONS": "true",
                },
                clear=True,
            ):
                with patch(
                    "sandcastle.runner.github_backend._validate_agent_command",
                    side_effect=lambda **kwargs: {
                        "name": kwargs["name"],
                        "required_for": list(kwargs["required_for"]),
                        "ready": True,
                        "reason": None,
                    },
                ):
                    with patch(
                        "sandcastle.runner.github_backend.validate_agent_role_policies",
                        return_value=[
                            {"name": "development-agent", "ready": True, "reason": None},
                            {"name": "review-agent", "ready": True, "reason": None},
                            {
                                "name": "conflict-resolution-agent",
                                "ready": True,
                                "reason": None,
                            },
                        ],
                    ):
                        exit_code = _handle_validate(
                            config_path=config_dir / "controller-backend.json",
                            repo_root=root,
                            output_path=output_path,
                        )

            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ready_for_backend_wakeups"])
        self.assertEqual(payload["runner_match"]["mode"], "workflow-job")

    def test_prepare_host_sets_repo_variables_and_reports_runner_admin_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "sandcastle" / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            output_path = root / "host-prep.json"
            state_root = root / "sandcastle-state"
            (config_dir / "controller-backend.json").write_text(
                json.dumps(
                    {
                        "backend": {
                            "kind": "github-actions-self-hosted",
                            "runner_labels": ["self-hosted", "sandcastle"],
                            "required_tools": ["git", "gh", "python", "pwsh", "dotnet"],
                            "required_repo_variable": "SANDCASTLE_EXECUTION_ENABLED",
                            "state_root_env_var": "SANDCASTLE_STATE_ROOT",
                            "default_state_root": "git-common-dir/sandcastle-state",
                            "credentials": [],
                            "notes": [],
                        }
                    }
                ),
                encoding="utf-8",
            )

            responses = iter(
                [
                    Mock(returncode=0, stdout="", stderr=""),
                    Mock(returncode=0, stdout="", stderr=""),
                    Mock(returncode=0, stdout="", stderr=""),
                    Mock(
                        returncode=0,
                        stdout=json.dumps(
                            {
                                "permissions": {
                                    "admin": True,
                                    "maintain": False,
                                    "push": True,
                                    "triage": True,
                                    "pull": True,
                                }
                            }
                        ),
                        stderr="",
                    ),
                    Mock(returncode=0, stdout="[]", stderr=""),
                ]
            )

            with patch(
                "sandcastle.runner.github_backend.subprocess.run",
                side_effect=lambda *args, **kwargs: next(responses),
            ):
                exit_code = _handle_prepare_host(
                    config_path=config_dir / "controller-backend.json",
                    output_path=output_path,
                    state_root=state_root,
                    repo="octo-org/octo-repo",
                    execution_enabled=False,
                )

            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["state_root_exists"])
        self.assertTrue(payload["gh_auth"]["ready"])
        self.assertTrue(payload["runner_registration"]["ready"])
        self.assertEqual(
            payload["repo_variables"],
            [
                {
                    "name": "SANDCASTLE_STATE_ROOT",
                    "value": str(state_root),
                    "set": True,
                    "reason": None,
                },
                {
                    "name": "SANDCASTLE_EXECUTION_ENABLED",
                    "value": "false",
                    "set": True,
                    "reason": None,
                },
            ],
        )

    def test_prepare_host_reports_non_admin_runner_registration_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "sandcastle" / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            output_path = root / "host-prep.json"
            state_root = root / "sandcastle-state"
            (config_dir / "controller-backend.json").write_text(
                json.dumps(
                    {
                        "backend": {
                            "kind": "github-actions-self-hosted",
                            "runner_labels": ["self-hosted", "sandcastle"],
                            "required_tools": ["git", "gh", "python", "pwsh", "dotnet"],
                            "required_repo_variable": "SANDCASTLE_EXECUTION_ENABLED",
                            "state_root_env_var": "SANDCASTLE_STATE_ROOT",
                            "default_state_root": "git-common-dir/sandcastle-state",
                            "credentials": [],
                            "notes": [],
                        }
                    }
                ),
                encoding="utf-8",
            )

            responses = iter(
                [
                    Mock(returncode=0, stdout="", stderr=""),
                    Mock(returncode=0, stdout="", stderr=""),
                    Mock(returncode=0, stdout="", stderr=""),
                    Mock(
                        returncode=0,
                        stdout=json.dumps(
                            {
                                "permissions": {
                                    "admin": False,
                                    "maintain": False,
                                    "push": True,
                                    "triage": True,
                                    "pull": True,
                                }
                            }
                        ),
                        stderr="",
                    ),
                ]
            )

            with patch(
                "sandcastle.runner.github_backend.subprocess.run",
                side_effect=lambda *args, **kwargs: next(responses),
            ):
                exit_code = _handle_prepare_host(
                    config_path=config_dir / "controller-backend.json",
                    output_path=output_path,
                    state_root=state_root,
                    repo="octo-org/octo-repo",
                    execution_enabled=False,
                )

            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["runner_registration"]["ready"])
        self.assertIn("does not have Actions admin permission", payload["runner_registration"]["reason"])
        self.assertIn("Use a repository admin identity", payload["next_steps"][0])

    @staticmethod
    def _write_required_tdd_inventory(root: Path) -> None:
        skill_dir = root / ".agents" / "skills" / "tdd"
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_contents = "repo-local tdd\n"
        skill_path = skill_dir / "SKILL.md"
        skill_path.write_text(skill_contents, encoding="utf-8")
        inventory_path = root / "sandcastle" / "config" / "vendored-skills.json"
        inventory_path.parent.mkdir(parents=True, exist_ok=True)
        inventory_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "skills": {
                        "tdd": {
                            "runtime_required": True,
                            "upstream": {
                                "repository": "https://github.com/mattpocock/skills",
                                "path": "skills/engineering/tdd",
                                "revision": "pinned-revision",
                            },
                            "files": {
                                "SKILL.md": {
                                    "sha256": hashlib.sha256(skill_path.read_bytes()).hexdigest()
                                }
                            },
                        }
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_required_code_review_manifest(root: Path) -> None:
        skill_dir = root / ".agents" / "skills" / "code-review"
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_contents = "repo-local code-review\n"
        skill_path = skill_dir / "SKILL.md"
        skill_path.write_text(skill_contents, encoding="utf-8")
        manifest_path = root / "sandcastle" / "config" / "vendored-skills.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "skills": {
                        "code-review": {
                            "runtime_required": True,
                            "upstream": {
                                "repository": "https://github.com/mattpocock/skills",
                                "path": "skills/engineering/code-review",
                                "revision": "pinned-revision",
                            },
                            "files": {
                                "SKILL.md": {
                                    "sha256": hashlib.sha256(skill_path.read_bytes()).hexdigest()
                                }
                            },
                        }
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_required_conflict_manifest(root: Path) -> None:
        skill_dir = root / ".agents" / "skills" / "resolving-merge-conflicts"
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_contents = "repo-local resolving-merge-conflicts\n"
        skill_path = skill_dir / "SKILL.md"
        skill_path.write_text(skill_contents, encoding="utf-8")
        manifest_path = root / "sandcastle" / "config" / "vendored-skills.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "skills": {
                        "resolving-merge-conflicts": {
                            "runtime_required": True,
                            "upstream": {
                                "repository": "https://github.com/mattpocock/skills",
                                "path": "skills/engineering/resolving-merge-conflicts",
                                "revision": "pinned-revision",
                            },
                            "files": {
                                "SKILL.md": {
                                    "sha256": hashlib.sha256(skill_path.read_bytes()).hexdigest()
                                }
                            },
                        }
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
