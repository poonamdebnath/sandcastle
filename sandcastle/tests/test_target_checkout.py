from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sandcastle.runner.main import _build_parser
from sandcastle.runner.shell import repo_root_from_file


class TargetCheckoutTests(unittest.TestCase):
    def test_installed_runner_uses_current_checkout_instead_of_source_location(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir)
            with patch.dict(os.environ, {}, clear=True), patch(
                "sandcastle.runner.shell.Path.cwd", return_value=target
            ):
                self.assertEqual(repo_root_from_file(__file__), target.resolve())

    def test_environment_selects_target_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"SANDCASTLE_REPO_ROOT": temp_dir}):
                self.assertEqual(repo_root_from_file(__file__), Path(temp_dir).resolve())

    def test_cli_accepts_explicit_target_checkout(self) -> None:
        args = _build_parser().parse_args(["--repo-root", ".", "report"])
        self.assertEqual(args.repo_root, ".")
        self.assertEqual(args.command, "report")
