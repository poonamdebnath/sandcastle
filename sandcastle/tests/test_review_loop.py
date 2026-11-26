from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sandcastle.runner.review_loop import write_review_result


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


class ReviewLoopTests(unittest.TestCase):
    def test_write_review_result_uses_bundled_codex_code_review_with_fixed_point(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_agent_profiles(root)
            worktree_path = root
            review_dir = root / "state" / "review"
            review_dir.mkdir(parents=True, exist_ok=True)

            def _run_review_agent(*args, **kwargs):
                env = kwargs["env"]
                Path(env["SANDCASTLE_REVIEW_RESULT_PATH"]).write_text(
                    json.dumps({"status": "clean", "findings": []}),
                    encoding="utf-8",
                )
                return Mock(returncode=0, stdout="review ok", stderr="")

            with patch.dict(os.environ, {}, clear=True):
                with patch(
                    "sandcastle.runner.review_loop.resolve_review_fixed_point",
                    return_value="abc123fixedpoint",
                ):
                    with patch(
                        "sandcastle.runner.review_loop.subprocess.run",
                        side_effect=_run_review_agent,
                    ) as run:
                        review_path = write_review_result(
                            review_dir,
                            issue_number=316,
                            branch="sandcastle/issue-316-review-default",
                            verification_status="passed",
                            implementation_path="C:/worktree/implementation.json",
                            worktree_path=str(worktree_path),
                            pr_number=44,
                            issue_title="Default review path",
                            context_path="C:/worktree/context.json",
                            issue_url="https://example.test/issues/316",
                            base_ref="origin/main",
                        )

            payload = json.loads(review_path.read_text(encoding="utf-8"))
            support_dir = worktree_path / ".sandcastle-agent" / "issue-316"
            brief = json.loads(
                (support_dir / "review-brief.json").read_text(encoding="utf-8")
            )
            instructions = (support_dir / "review-instructions.md").read_text(
                encoding="utf-8"
            )
            command = run.call_args.args[0]
            env = run.call_args.kwargs["env"]

        self.assertEqual(command[0], "codex")
        self.assertIn("--model", command)
        self.assertIn("gpt-5.4-1", command)
        self.assertIn("$code-review", command[-1])
        self.assertIn("abc123fixedpoint", command[-1])
        self.assertIn("merge-base against origin/main", command[-1])
        self.assertEqual(env["SANDCASTLE_REVIEW_BASE_REF"], "origin/main")
        self.assertEqual(env["SANDCASTLE_REVIEW_FIXED_POINT"], "abc123fixedpoint")
        self.assertEqual(brief["review_base_ref"], "origin/main")
        self.assertEqual(brief["review_fixed_point"], "abc123fixedpoint")
        self.assertIn("review base ref: origin/main", instructions)
        self.assertIn("review fixed point: abc123fixedpoint", instructions)
        self.assertEqual(payload["status"], "clean")
        self.assertEqual(payload["review_base_ref"], "origin/main")
        self.assertEqual(payload["review_fixed_point"], "abc123fixedpoint")


if __name__ == "__main__":
    unittest.main()
