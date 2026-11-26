from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sandcastle.runner.conflict_resolution import (
    _render_instructions,
    _resolve_conflict_agent_command,
    resolve_pr_conflicts,
)


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


class ConflictResolutionTests(unittest.TestCase):
    def test_default_command_uses_codex_with_repo_local_conflict_skill(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            command = _resolve_conflict_agent_command(
                issue_number=323,
                pr_number=71,
                branch="sandcastle/issue-323-conflict-agent",
                worktree_path="C:/worktree",
                issue_title="Bundled conflict fallback",
                issue_url="https://example.test/issues/323",
                context_path="C:/worktree/context.json",
                base_ref="origin/main",
                conflict_brief_path="C:/worktree/conflict-brief.json",
                conflict_result_path="C:/worktree/conflict-result.json",
                conflict_instructions_path="C:/worktree/conflict-instructions.md",
                agent_profile="conflict-default",
                agent_model="gpt-5.4-1",
                agent_reasoning_effort="medium",
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
        self.assertIn("--model", command)
        self.assertIn("gpt-5.4-1", command)
        self.assertIn("$resolving-merge-conflicts", command[-1])
        self.assertIn("status set to resolved or blocked", command[-1])

    def test_resolve_pr_conflicts_uses_bundled_codex_fallback_when_override_missing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_agent_profiles(root)
            conflict_dir = root / "state" / "conflicts"
            conflict_dir.mkdir(parents=True, exist_ok=True)

            def _run_conflict_agent(*args, **kwargs):
                env = kwargs["env"]
                Path(env["SANDCASTLE_CONFLICT_RESULT_PATH"]).write_text(
                    json.dumps({"status": "resolved"}),
                    encoding="utf-8",
                )
                return Mock(returncode=0, stdout="resolved", stderr="")

            with patch.dict(os.environ, {}, clear=True):
                with patch(
                    "sandcastle.runner.conflict_resolution.merge_base_into_worktree",
                    return_value=(1, "", "automatic merge failed"),
                ):
                    with patch(
                        "sandcastle.runner.conflict_resolution.list_unmerged_files",
                        side_effect=[
                            ["sandcastle/runner/main.py"],
                            [],
                        ],
                    ):
                        with patch(
                            "sandcastle.runner.conflict_resolution.current_head_sha",
                            side_effect=["pre-merge-sha", "post-merge-sha"],
                        ):
                            with patch(
                                "sandcastle.runner.conflict_resolution.stage_all_changes"
                            ):
                                with patch(
                                    "sandcastle.runner.conflict_resolution.is_merge_in_progress",
                                    return_value=False,
                                ):
                                    with patch(
                                        "sandcastle.runner.conflict_resolution.subprocess.run",
                                        side_effect=_run_conflict_agent,
                                    ) as run:
                                        result_path = resolve_pr_conflicts(
                                            conflict_dir=conflict_dir,
                                            issue_number=323,
                                            pr_number=71,
                                            branch="sandcastle/issue-323-conflict-agent",
                                            worktree_path=str(root),
                                            issue_title="Bundled conflict fallback",
                                            issue_url="https://example.test/issues/323",
                                            context_path="C:/worktree/context.json",
                                            base_ref="origin/main",
                                        )

            payload = json.loads(result_path.read_text(encoding="utf-8"))
            command = run.call_args.args[0]
            env = run.call_args.kwargs["env"]

        self.assertEqual(command[0], "codex")
        self.assertIn("--model", command)
        self.assertIn("gpt-5.4-1", command)
        self.assertIn("$resolving-merge-conflicts", command[-1])
        self.assertEqual(env["SANDCASTLE_AGENT_ROLE"], "conflict-resolution-agent")
        self.assertEqual(env["SANDCASTLE_AGENT_PROFILE"], "conflict-default")
        self.assertEqual(payload["status"], "resolved")
        self.assertEqual(payload["resolution_kind"], "agent-resolved")
        self.assertEqual(payload["post_merge_head_sha"], "post-merge-sha")

    def test_rendered_instructions_require_repo_local_skill(self) -> None:
        instructions = _render_instructions(
            issue_number=273,
            pr_number=17,
            branch="sandcastle/issue-273-example",
            worktree_path="C:/worktree",
            issue_title="Example issue",
            issue_url="https://example.test/issues/273",
            context_path="C:/worktree/context.json",
            base_ref="origin/main",
            conflicted_files=["sandcastle/runner/main.py"],
        )

        self.assertIn(
            "Use the repo-local Matt `resolving-merge-conflicts` skill",
            instructions,
        )
        self.assertIn("Resolve only safe, mechanical merge conflicts", instructions)


if __name__ == "__main__":
    unittest.main()
