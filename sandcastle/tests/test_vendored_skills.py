from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from sandcastle.runner.vendored_skills import validate_required_vendored_skills


class VendoredSkillValidationTests(unittest.TestCase):
    def test_missing_required_skill_directory_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_inventory(
                root,
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
                                "SKILL.md": {"sha256": self._sha256("tdd skill\n")},
                            },
                        }
                    },
                },
            )

            results = validate_required_vendored_skills(root, ("tdd",))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "tdd")
        self.assertFalse(results[0]["ready"])
        self.assertIn("directory is missing", str(results[0]["reason"]))

    def test_missing_skill_md_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / ".agents" / "skills" / "tdd"
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "tests.md").write_text("tests\n", encoding="utf-8")
            self._write_inventory(
                root,
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
                                "tests.md": {"sha256": self._sha256("tests\n")},
                            },
                        }
                    },
                },
            )

            results = validate_required_vendored_skills(root, ("tdd",))

        self.assertFalse(results[0]["ready"])
        self.assertIn("missing SKILL.md", str(results[0]["reason"]))

    def test_missing_inventory_entry_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / ".agents" / "skills" / "tdd"
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text("tdd skill\n", encoding="utf-8")
            self._write_inventory(root, {"version": 1, "skills": {}})

            results = validate_required_vendored_skills(root, ("tdd",))

        self.assertFalse(results[0]["ready"])
        self.assertIn("inventory entry is missing", str(results[0]["reason"]))

    def test_hash_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / ".agents" / "skills" / "tdd"
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text("actual contents\n", encoding="utf-8")
            self._write_inventory(
                root,
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
                                "SKILL.md": {"sha256": self._sha256("expected contents\n")},
                            },
                        }
                    },
                },
            )

            results = validate_required_vendored_skills(root, ("tdd",))

        self.assertFalse(results[0]["ready"])
        self.assertIn("hash mismatch", str(results[0]["reason"]))

    def test_untracked_required_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / ".agents" / "skills" / "tdd"
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text("skill\n", encoding="utf-8")
            (skill_dir / "tests.md").write_text("tests\n", encoding="utf-8")
            self._write_inventory(
                root,
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
                                "SKILL.md": {"sha256": self._sha256("skill\n")},
                            },
                        }
                    },
                },
            )

            results = validate_required_vendored_skills(root, ("tdd",))

        self.assertFalse(results[0]["ready"])
        self.assertIn("untracked files", str(results[0]["reason"]))

    @staticmethod
    def _sha256(contents: str) -> str:
        return hashlib.sha256(contents.encode("utf-8")).hexdigest()

    @staticmethod
    def _write_inventory(root: Path, payload: dict[str, object]) -> None:
        inventory_path = root / "sandcastle" / "config" / "vendored-skills.json"
        inventory_path.parent.mkdir(parents=True, exist_ok=True)
        inventory_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
