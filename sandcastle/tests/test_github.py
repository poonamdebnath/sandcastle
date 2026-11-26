from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sandcastle.runner.github import (
    ensure_issue_assignee,
    ensure_pull_request_assignee,
    get_issue,
    list_open_issues,
)


def _completed(stdout: str) -> Mock:
    return Mock(returncode=0, stdout=stdout, stderr="")


def _failed(stderr: str) -> Mock:
    return Mock(returncode=1, stdout="", stderr=stderr)


class GithubIssueDependencyTests(unittest.TestCase):
    def test_list_open_issues_requests_utf8_output_from_gh(self) -> None:
        issues_payload = {
            "data": {
                "repository": {
                    "issues": {
                        "nodes": [
                            {
                                "number": 280,
                                "title": "Dependency-aware pickup ™",
                                "state": "OPEN",
                                "body": "",
                                "url": "https://example.test/issues/280",
                                "labels": {"nodes": []},
                                "assignees": {"nodes": []},
                                "issueDependenciesSummary": {"blockedBy": 0, "totalBlockedBy": 0},
                                "blockedBy": {"totalCount": 0, "nodes": []},
                            }
                        ]
                    }
                }
            }
        }

        def fake_run(*args, **kwargs):
            self.assertEqual(kwargs.get("encoding"), "utf-8")
            self.assertEqual(kwargs.get("errors"), "replace")
            command = args[0]
            if command[:3] == ["gh", "repo", "view"]:
                return _completed("example-maintainer/example-project\n")
            if command[:3] == ["gh", "api", "graphql"]:
                return _completed(json.dumps(issues_payload))
            raise AssertionError(f"unexpected command: {command}")

        with patch("sandcastle.runner.github.subprocess.run", side_effect=fake_run):
            issues = list_open_issues(Path("C:/repo"))

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].title, "Dependency-aware pickup ™")

    def test_get_issue_uses_native_dependencies_and_merged_pr_signal(self) -> None:
        issue_payload = {
            "data": {
                "repository": {
                    "issue": {
                        "number": 280,
                        "title": "Dependency-aware pickup",
                        "state": "OPEN",
                        "body": "Part of #278",
                        "url": "https://example.test/issues/280",
                        "labels": {"nodes": [{"name": "ready-for-agent"}, {"name": "type:dev"}]},
                        "assignees": {"nodes": []},
                        "issueDependenciesSummary": {"blockedBy": 1, "totalBlockedBy": 2},
                        "blockedBy": {
                            "totalCount": 2,
                            "nodes": [
                                {
                                    "number": 278,
                                    "title": "Earlier task",
                                    "state": "OPEN",
                                    "closedByPullRequestsReferences": {"nodes": []},
                                    "timelineItems": {"nodes": []},
                                },
                                {
                                    "number": 279,
                                    "title": "Merged task",
                                    "state": "OPEN",
                                    "closedByPullRequestsReferences": {
                                        "nodes": [
                                            {
                                                "number": 501,
                                                "state": "MERGED",
                                                "merged": True,
                                                "mergedAt": "2026-10-26T12:00:00Z",
                                                "baseRefName": "main",
                                            }
                                        ]
                                    },
                                    "timelineItems": {"nodes": []},
                                },
                            ],
                        },
                    }
                }
            }
        }

        with patch(
            "sandcastle.runner.github.subprocess.run",
            side_effect=[
                _completed("example-maintainer/example-project\n"),
                _completed(json.dumps(issue_payload)),
            ],
        ):
            issue = get_issue(Path("C:/repo"), 280)

        self.assertEqual(issue.dependency_source, "native")
        self.assertEqual(tuple(dependency.number for dependency in issue.blocked_by), (278, 279))
        self.assertEqual(tuple(dependency.merged for dependency in issue.blocked_by), (False, True))
        self.assertIsNone(issue.dependency_error)


class GithubAssignmentTests(unittest.TestCase):
    def test_issue_assignment_requires_configured_username(self) -> None:
        with self.assertRaisesRegex(
            RuntimeError,
            "GitHub assignee username is required",
        ):
            ensure_issue_assignee(
                Path("C:/repo"),
                issue_number=299,
                assignee_username=None,
                current_assignees=(),
                overwrite_existing_assignees=False,
            )

    def test_issue_assignment_adds_configured_user_when_unassigned(self) -> None:
        with patch(
            "sandcastle.runner.github.subprocess.run",
            return_value=_completed(""),
        ) as run:
            status = ensure_issue_assignee(
                Path("C:/repo"),
                issue_number=299,
                assignee_username="example-maintainer",
                current_assignees=(),
                overwrite_existing_assignees=False,
            )

        self.assertEqual(status, "assigned")
        command = run.call_args.args[0]
        self.assertEqual(
            command,
            ["gh", "issue", "edit", "299", "--add-assignee", "example-maintainer"],
        )

    def test_issue_assignment_preserves_existing_assignment_without_overwrite(self) -> None:
        with patch("sandcastle.runner.github.subprocess.run") as run:
            status = ensure_issue_assignee(
                Path("C:/repo"),
                issue_number=299,
                assignee_username="example-maintainer",
                current_assignees=("octocat",),
                overwrite_existing_assignees=False,
            )

        self.assertEqual(status, "preserved-existing-assignee")
        run.assert_not_called()

    def test_issue_assignment_replaces_existing_assignment_when_overwrite_allowed(self) -> None:
        with patch(
            "sandcastle.runner.github.subprocess.run",
            side_effect=[_completed(""), _completed("")],
        ) as run:
            status = ensure_issue_assignee(
                Path("C:/repo"),
                issue_number=299,
                assignee_username="example-maintainer",
                current_assignees=("octocat",),
                overwrite_existing_assignees=True,
            )

        self.assertEqual(status, "reassigned")
        self.assertEqual(
            [item.args[0] for item in run.call_args_list],
            [
                ["gh", "issue", "edit", "299", "--remove-assignee", "octocat"],
                ["gh", "issue", "edit", "299", "--add-assignee", "example-maintainer"],
            ],
        )

    def test_issue_assignment_surfaces_invalid_username_error(self) -> None:
        with patch(
            "sandcastle.runner.github.subprocess.run",
            return_value=_failed("could not resolve to a user"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "failed to assign issue #299 assignee 'example-maintainer'",
            ):
                ensure_issue_assignee(
                    Path("C:/repo"),
                    issue_number=299,
                    assignee_username="example-maintainer",
                    current_assignees=(),
                    overwrite_existing_assignees=False,
                )

    def test_pull_request_assignment_surfaces_api_failure(self) -> None:
        with patch(
            "sandcastle.runner.github.subprocess.run",
            return_value=_failed("GraphQL: Resource not accessible by integration"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "failed to assign pull request #310 to 'example-maintainer'",
            ):
                ensure_pull_request_assignee(
                    Path("C:/repo"),
                    pr_number=310,
                    assignee_username="example-maintainer",
                )

    def test_pull_request_assignment_is_noop_when_already_assigned(self) -> None:
        with patch("sandcastle.runner.github.subprocess.run") as run:
            status = ensure_pull_request_assignee(
                Path("C:/repo"),
                pr_number=310,
                assignee_username="example-maintainer",
                current_assignees=("octocat", "example-maintainer"),
            )

        self.assertEqual(status, "already-assigned")
        run.assert_not_called()

    def test_get_issue_falls_back_to_body_blocked_by_line(self) -> None:
        issue_payload = {
            "data": {
                "repository": {
                    "issue": {
                        "number": 280,
                        "title": "Dependency-aware pickup",
                        "state": "OPEN",
                        "body": "Blocked by: #278, #279\n\nPart of #278",
                        "url": "https://example.test/issues/280",
                        "labels": {"nodes": [{"name": "ready-for-agent"}, {"name": "type:dev"}]},
                        "assignees": {"nodes": []},
                        "issueDependenciesSummary": {"blockedBy": 0, "totalBlockedBy": 0},
                        "blockedBy": {"totalCount": 0, "nodes": []},
                    },
                    "issue_278": {
                        "number": 278,
                        "title": "Earlier task",
                        "state": "OPEN",
                        "closedByPullRequestsReferences": {"nodes": []},
                        "timelineItems": {"nodes": []},
                    },
                    "issue_279": {
                        "number": 279,
                        "title": "Merged task",
                        "state": "OPEN",
                        "closedByPullRequestsReferences": {"nodes": []},
                        "timelineItems": {
                            "nodes": [
                                {
                                    "__typename": "CrossReferencedEvent",
                                    "source": {
                                        "__typename": "PullRequest",
                                        "number": 502,
                                        "state": "MERGED",
                                        "merged": True,
                                        "mergedAt": "2026-10-26T12:05:00Z",
                                        "baseRefName": "main",
                                    },
                                    "willCloseTarget": False,
                                }
                            ]
                        },
                    },
                }
            }
        }

        with patch(
            "sandcastle.runner.github.subprocess.run",
            side_effect=[
                _completed("example-maintainer/example-project\n"),
                _completed(json.dumps({"data": {"repository": {"issue": issue_payload["data"]["repository"]["issue"]}}})),
                _completed("example-maintainer/example-project\n"),
                _completed(json.dumps({"data": {"repository": {
                    "issue_278": issue_payload["data"]["repository"]["issue_278"],
                    "issue_279": issue_payload["data"]["repository"]["issue_279"],
                }}})),
            ],
        ):
            issue = get_issue(Path("C:/repo"), 280)

        self.assertEqual(issue.dependency_source, "body")
        self.assertEqual(tuple(dependency.number for dependency in issue.blocked_by), (278, 279))
        self.assertEqual(tuple(dependency.merged for dependency in issue.blocked_by), (False, True))
        self.assertIsNone(issue.dependency_error)


if __name__ == "__main__":
    unittest.main()
