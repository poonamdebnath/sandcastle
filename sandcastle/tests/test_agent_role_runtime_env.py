from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sandcastle.runner.conflict_resolution import _build_conflict_agent_env
from sandcastle.runner.review_loop import _build_review_agent_env


def _write_agent_profiles(root: Path) -> None:
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


class AgentRoleRuntimeEnvTests(unittest.TestCase):
    def test_review_agent_env_uses_review_role_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_agent_profiles(root)

            env = _build_review_agent_env(
                repo_root=root,
                issue_number=300,
                branch="sandcastle/issue-300-agent-profiles",
                worktree_path="C:/worktree",
                issue_title="Role-specific models",
                pr_number=77,
                issue_url="https://example.test/issues/300",
                context_path="C:/worktree/context.json",
                implementation_path="C:/worktree/implementation.json",
                verification_status="passed",
                review_brief_path="C:/worktree/review-brief.json",
                review_result_path="C:/worktree/review-result.json",
                review_instructions_path="C:/worktree/review-instructions.md",
            )

        self.assertEqual(env["SANDCASTLE_AGENT_ROLE"], "review-agent")
        self.assertEqual(env["SANDCASTLE_AGENT_PROFILE"], "review-default")
        self.assertEqual(env["SANDCASTLE_AGENT_MODEL"], "gpt-5.4-1")
        self.assertEqual(env["SANDCASTLE_AGENT_REASONING_EFFORT"], "xhigh")

    def test_conflict_agent_env_uses_conflict_role_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_agent_profiles(root)

            env = _build_conflict_agent_env(
                repo_root=root,
                issue_number=300,
                pr_number=77,
                branch="sandcastle/issue-300-agent-profiles",
                worktree_path="C:/worktree",
                issue_title="Role-specific models",
                issue_url="https://example.test/issues/300",
                context_path="C:/worktree/context.json",
                base_ref="origin/main",
                conflict_brief_path="C:/worktree/conflict-brief.json",
                conflict_result_path="C:/worktree/conflict-result.json",
                conflict_instructions_path="C:/worktree/conflict-instructions.md",
            )

        self.assertEqual(env["SANDCASTLE_AGENT_ROLE"], "conflict-resolution-agent")
        self.assertEqual(env["SANDCASTLE_AGENT_PROFILE"], "conflict-default")
        self.assertEqual(env["SANDCASTLE_AGENT_MODEL"], "gpt-5.4-1")
        self.assertEqual(env["SANDCASTLE_AGENT_REASONING_EFFORT"], "medium")


if __name__ == "__main__":
    unittest.main()
