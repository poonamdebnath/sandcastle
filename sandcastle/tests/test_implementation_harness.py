from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sandcastle.runner.implementation_harness import (
    DEV_AGENT_COMMAND_ENV_VAR,
    _build_dev_agent_env,
    _render_instructions,
    _resolve_dev_agent_command,
)


class ImplementationHarnessTests(unittest.TestCase):
    def test_default_command_uses_codex_with_tdd_skill(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            command = _resolve_dev_agent_command(
                issue_number=273,
                branch="sandcastle/issue-273-example",
                issue_title="Example issue",
                worktree_path="C:/worktree",
                context_path="C:/worktree/context.json",
                issue_url="https://example.test/issues/273",
                implementation_mode="initial",
                findings_path="",
                revision_path="",
                brief_path="C:/worktree/brief.json",
                instructions_path="C:/worktree/instructions.md",
            )

        self.assertEqual(
            command[:6],
            [
                "codex",
                "--ask-for-approval",
                "never",
                "exec",
                "--sandbox",
                "workspace-write",
            ],
        )
        self.assertIn("$tdd", command[-1])
        self.assertIn("repo-local", command[-1])
        self.assertIn("public seam under test", command[-1])

    def test_default_command_includes_agent_model_when_available(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            command = _resolve_dev_agent_command(
                issue_number=273,
                branch="sandcastle/issue-273-example",
                issue_title="Example issue",
                worktree_path="C:/worktree",
                context_path="C:/worktree/context.json",
                issue_url="https://example.test/issues/273",
                implementation_mode="initial",
                findings_path="",
                revision_path="",
                brief_path="C:/worktree/brief.json",
                instructions_path="C:/worktree/instructions.md",
                agent_model="gpt-5.4-1",
                agent_profile="codex-default",
                agent_reasoning_effort="medium",
            )

        self.assertIn("--model", command)
        self.assertIn("gpt-5.4-1", command)

    def test_default_command_includes_reasoning_override_when_available(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            command = _resolve_dev_agent_command(
                issue_number=273,
                branch="sandcastle/issue-273-example",
                issue_title="Example issue",
                worktree_path="C:/worktree",
                context_path="C:/worktree/context.json",
                issue_url="https://example.test/issues/273",
                implementation_mode="initial",
                findings_path="",
                revision_path="",
                brief_path="C:/worktree/brief.json",
                instructions_path="C:/worktree/instructions.md",
                agent_model="gpt-5.3-codex",
                agent_profile="coding-default",
                agent_reasoning_effort="xhigh",
            )

        self.assertIn("--config", command)
        self.assertIn('model_reasoning_effort="xhigh"', command)

    def test_explicit_command_override_is_still_honored(self) -> None:
        with patch.dict(
            os.environ,
            {
                DEV_AGENT_COMMAND_ENV_VAR: '["python","-m","custom_dev_agent","--brief","{brief_path}"]'
            },
            clear=True,
        ):
            command = _resolve_dev_agent_command(
                issue_number=273,
                branch="sandcastle/issue-273-example",
                issue_title="Example issue",
                worktree_path="C:/worktree",
                context_path="C:/worktree/context.json",
                issue_url="https://example.test/issues/273",
                implementation_mode="initial",
                findings_path="",
                revision_path="",
                brief_path="C:/worktree/brief.json",
                instructions_path="C:/worktree/instructions.md",
            )

        self.assertEqual(
            command,
            ["python", "-m", "custom_dev_agent", "--brief", "C:/worktree/brief.json"],
        )

    def test_rendered_instructions_require_tdd_loop(self) -> None:
        instructions = _render_instructions(
            issue_number=273,
            issue_title="Example issue",
            branch="sandcastle/issue-273-example",
            worktree_path="C:/worktree",
            issue_url="https://example.test/issues/273",
            context_path="C:/worktree/context.json",
            implementation_mode="initial",
            findings_path="",
            revision_path="",
            brief={
                "goal": ["Support TDD by default"],
                "done_when": ["Sandcastle coding uses Matt's skill"],
                "findings": [],
                "next_actions": [],
            },
        )

        self.assertIn("Use the repo-local Matt `$tdd` skill", instructions)
        self.assertIn("repo-local", instructions)
        self.assertIn("strict red-green loop", instructions)
        self.assertIn("Do not create commits yourself", instructions)

    def test_build_dev_agent_env_exports_agent_profile_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "sandcastle" / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "agent-profiles.json").write_text(
                (
                    "{"
                    '"profiles":{"coding-default":{"model":"gpt-5.3-codex","reasoning_effort":"xhigh"},'
                    '"review-default":{"model":"gpt-5.4-1","reasoning_effort":"xhigh"},'
                    '"conflict-default":{"model":"gpt-5.4-1","reasoning_effort":"medium"}},'
                    '"roles":{"development-agent":{"profile":"coding-default"},'
                    '"review-agent":{"profile":"review-default"},'
                    '"conflict-resolution-agent":{"profile":"conflict-default"}}'
                    "}"
                ),
                encoding="utf-8",
            )

            env = _build_dev_agent_env(
                repo_root=root,
                issue_number=273,
                branch="sandcastle/issue-273-example",
                issue_title="Example issue",
                worktree_path="C:/worktree",
                context_path="C:/worktree/context.json",
                issue_url="https://example.test/issues/273",
                implementation_mode="initial",
                findings_path="",
                revision_path="",
                brief_path="C:/worktree/brief.json",
                instructions_path="C:/worktree/instructions.md",
            )

        self.assertEqual(env["SANDCASTLE_AGENT_ROLE"], "development-agent")
        self.assertEqual(env["SANDCASTLE_AGENT_PROFILE"], "coding-default")
        self.assertEqual(env["SANDCASTLE_AGENT_MODEL"], "gpt-5.3-codex")
        self.assertEqual(env["SANDCASTLE_AGENT_REASONING_EFFORT"], "xhigh")


if __name__ == "__main__":
    unittest.main()
