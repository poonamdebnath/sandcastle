from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sandcastle.runner.agent_profiles import (
    build_agent_profile_env,
    configured_agent_roles,
    load_agent_profile,
    validate_agent_role_policies,
)


class AgentProfileTests(unittest.TestCase):
    def test_load_agent_profile_reads_role_specific_binding(self) -> None:
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

            profile = load_agent_profile("development-agent", root)

        self.assertEqual(profile.profile_name, "coding-default")
        self.assertEqual(profile.model, "gpt-5.3-codex")
        self.assertEqual(profile.reasoning_effort, "xhigh")

    def test_configured_agent_roles_includes_future_roles_without_code_changes(self) -> None:
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
                            "documentation-default": {
                                "model": "gpt-5.4-1",
                                "reasoning_effort": "medium",
                            },
                        },
                        "roles": {
                            "development-agent": {"profile": "coding-default"},
                            "review-agent": {"profile": "review-default"},
                            "documentation-agent": {"profile": "documentation-default"},
                        },
                    }
                ),
                encoding="utf-8",
            )

            roles = configured_agent_roles(root)
            documentation_profile = load_agent_profile("documentation-agent", root)

        self.assertEqual(
            roles,
            (
                "development-agent",
                "review-agent",
                "documentation-agent",
            ),
        )
        self.assertEqual(documentation_profile.model, "gpt-5.4-1")
        self.assertEqual(documentation_profile.reasoning_effort, "medium")

    def test_build_agent_profile_env_exports_expected_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "sandcastle" / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "agent-profiles.json").write_text(
                json.dumps(
                    {
                        "profiles": {
                            "review-default": {
                                "model": "gpt-5.4-1",
                                "reasoning_effort": "xhigh",
                            },
                            "coding-default": {
                                "model": "gpt-5.3-codex",
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

            env = build_agent_profile_env("review-agent", root)

        self.assertEqual(env["SANDCASTLE_AGENT_ROLE"], "review-agent")
        self.assertEqual(env["SANDCASTLE_AGENT_PROFILE"], "review-default")
        self.assertEqual(env["SANDCASTLE_AGENT_MODEL"], "gpt-5.4-1")
        self.assertEqual(env["SANDCASTLE_AGENT_REASONING_EFFORT"], "xhigh")

    def test_validate_agent_role_policies_reports_missing_profile_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "sandcastle" / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "agent-profiles.json").write_text(
                json.dumps(
                    {
                        "profiles": {
                            "codex-default": {
                                "model": "gpt-5.4-1",
                                "reasoning_effort": "medium",
                            }
                        },
                        "roles": {
                            "development-agent": {"profile": "codex-default"},
                            "review-agent": {"profile": "missing"},
                            "conflict-resolution-agent": {"profile": "codex-default"},
                        },
                    }
                ),
                encoding="utf-8",
            )

            results = validate_agent_role_policies(root)

        review_result = next(item for item in results if item["name"] == "review-agent")
        self.assertFalse(review_result["ready"])
        self.assertIn("references unknown profile 'missing'", str(review_result["reason"]))

    def test_validate_agent_role_policies_rejects_unknown_reasoning_effort(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "sandcastle" / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "agent-profiles.json").write_text(
                json.dumps(
                    {
                        "profiles": {
                            "review-default": {
                                "model": "gpt-5.4-1",
                                "reasoning_effort": "turbo",
                            },
                            "coding-default": {
                                "model": "gpt-5.3-codex",
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

            results = validate_agent_role_policies(root)

        review_result = next(item for item in results if item["name"] == "review-agent")
        self.assertFalse(review_result["ready"])
        self.assertIn("unsupported reasoning_effort", str(review_result["reason"]))

    def test_validate_agent_role_policies_surfaces_runtime_model_probe_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "sandcastle" / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "agent-profiles.json").write_text(
                json.dumps(
                    {
                        "profiles": {
                            "review-default": {
                                "model": "gpt-5.4-1",
                                "reasoning_effort": "xhigh",
                            },
                            "coding-default": {
                                "model": "gpt-5.3-codex",
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

            with patch(
                "sandcastle.runner.agent_profiles.shutil.which",
                return_value=r"C:\Users\codex\codex.exe",
            ):
                with patch(
                    "sandcastle.runner.agent_profiles.subprocess.run",
                    return_value=Mock(
                        returncode=1,
                        stdout="",
                        stderr="unknown model gpt-5.4-1",
                    ),
                ):
                    results = validate_agent_role_policies(root, probe_runtime=True)

        review_result = next(item for item in results if item["name"] == "review-agent")
        self.assertFalse(review_result["ready"])
        self.assertIn("Codex runtime rejected role 'review-agent'", str(review_result["reason"]))
        self.assertEqual(
            review_result["runtime_probe"]["probe_command"][0],
            r"C:\Users\codex\codex.exe",
        )


if __name__ == "__main__":
    unittest.main()
