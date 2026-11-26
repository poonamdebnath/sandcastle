from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .models import IssueCandidate, IssueDependency

BLOCKED_BY_LINE_RE = re.compile(r"^Blocked by:\s*(?P<issues>#\d+(?:,\s*#\d+)*)\s*$", re.MULTILINE)
DEPENDENCY_STATE_ERROR = (
    "unable to confirm whether predecessor issues are merged to main"
)
ISSUE_DEPENDENCY_FRAGMENT = """
number
title
state
closedByPullRequestsReferences(first: 20, includeClosedPrs: true) {
  nodes {
    number
    state
    merged
    mergedAt
    baseRefName
  }
}
timelineItems(first: 20, itemTypes: [CROSS_REFERENCED_EVENT, CONNECTED_EVENT]) {
  nodes {
    __typename
    ... on CrossReferencedEvent {
      source {
        __typename
        ... on PullRequest {
          number
          state
          merged
          mergedAt
          baseRefName
        }
      }
      willCloseTarget
    }
    ... on ConnectedEvent {
      source {
        __typename
        ... on PullRequest {
          number
          state
          merged
          mergedAt
          baseRefName
        }
      }
    }
  }
}
"""
ISSUE_QUERY_FIELDS = f"""
number
title
state
body
url
labels(first: 20) {{
  nodes {{
    name
  }}
}}
assignees(first: 20) {{
  nodes {{
    login
  }}
}}
issueDependenciesSummary {{
  blockedBy
  totalBlockedBy
}}
blockedBy(first: 100) {{
  totalCount
  nodes {{
    {ISSUE_DEPENDENCY_FRAGMENT}
  }}
}}
"""

_GH_SUBPROCESS_KWARGS = {
    "capture_output": True,
    "text": True,
    "encoding": "utf-8",
    "errors": "replace",
}


def list_open_issues(repo_root: Path, limit: int = 100) -> list[IssueCandidate]:
    owner, repo = _get_repo_owner_and_name(repo_root)
    payload = _run_graphql(
        repo_root,
        """
        query($owner: String!, $repo: String!, $limit: Int!) {
          repository(owner: $owner, name: $repo) {
            issues(
              first: $limit
              states: OPEN
              orderBy: { field: CREATED_AT, direction: ASC }
            ) {
              nodes {
                """
        + ISSUE_QUERY_FIELDS
        + """
              }
            }
          }
        }
        """,
        {
            "owner": owner,
            "repo": repo,
            "limit": limit,
        },
    )
    nodes = payload["data"]["repository"]["issues"]["nodes"]
    return [
        _hydrate_issue_candidate(repo_root, node, default_branch="main")
        for node in nodes
        if isinstance(node, dict)
    ]


def get_issue(repo_root: Path, issue_number: int) -> IssueCandidate:
    owner, repo = _get_repo_owner_and_name(repo_root)
    payload = _run_graphql(
        repo_root,
        """
        query($owner: String!, $repo: String!, $number: Int!) {
          repository(owner: $owner, name: $repo) {
            issue(number: $number) {
              """
        + ISSUE_QUERY_FIELDS
        + """
            }
          }
        }
        """,
        {
            "owner": owner,
            "repo": repo,
            "number": issue_number,
        },
    )
    issue = payload["data"]["repository"]["issue"]
    if not isinstance(issue, dict):
        raise RuntimeError(f"issue #{issue_number} was not returned by GitHub")
    return _hydrate_issue_candidate(repo_root, issue, default_branch="main")


def get_current_login(repo_root: Path) -> str:
    command = ["gh", "api", "user", "--jq", ".login"]
    result = subprocess.run(command, cwd=repo_root, check=True, **_GH_SUBPROCESS_KWARGS)
    return result.stdout.strip()


def ensure_issue_assignee(
    repo_root: Path,
    *,
    issue_number: int,
    assignee_username: str | None,
    current_assignees: tuple[str, ...],
    overwrite_existing_assignees: bool,
) -> str:
    assignee = _require_assignee_username(
        assignee_username,
        scope=f"issue #{issue_number}",
    )
    existing_assignees = tuple(
        login for login in current_assignees if isinstance(login, str) and login
    )
    if assignee in existing_assignees and not overwrite_existing_assignees:
        return "already-assigned"

    other_assignees = tuple(
        login for login in existing_assignees if login != assignee
    )
    if other_assignees and not overwrite_existing_assignees:
        return "preserved-existing-assignee"

    if overwrite_existing_assignees:
        for login in other_assignees:
            _edit_issue_assignee(
                repo_root,
                issue_number=issue_number,
                assignee_username=login,
                action="remove",
            )

    if assignee not in existing_assignees:
        _edit_issue_assignee(
            repo_root,
            issue_number=issue_number,
            assignee_username=assignee,
            action="add",
        )

    if other_assignees:
        return "reassigned"
    return "assigned"


def ensure_pull_request_assignee(
    repo_root: Path,
    *,
    pr_number: int,
    assignee_username: str | None,
    current_assignees: tuple[str, ...] = (),
) -> str:
    assignee = _require_assignee_username(
        assignee_username,
        scope=f"pull request #{pr_number}",
    )
    existing_assignees = tuple(
        login for login in current_assignees if isinstance(login, str) and login
    )
    if assignee in existing_assignees:
        return "already-assigned"

    command = ["gh", "pr", "edit", str(pr_number), "--add-assignee", assignee]
    result = subprocess.run(command, cwd=repo_root, check=False, **_GH_SUBPROCESS_KWARGS)
    if result.returncode != 0:
        raise RuntimeError(
            f"failed to assign pull request #{pr_number} to '{assignee}': "
            f"{_command_failure(result)}"
        )
    return "assigned"


def comment_on_issue(repo_root: Path, issue_number: int, body: str) -> None:
    command = ["gh", "issue", "comment", str(issue_number), "--body", body]
    subprocess.run(command, cwd=repo_root, check=True, **_GH_SUBPROCESS_KWARGS)


def close_issue(repo_root: Path, issue_number: int, body: str) -> None:
    command = ["gh", "issue", "close", str(issue_number), "--comment", body]
    subprocess.run(command, cwd=repo_root, check=True, **_GH_SUBPROCESS_KWARGS)


def create_pr(
    repo_root: Path,
    *,
    base_branch: str,
    head_branch: str,
    title: str,
    body: str,
    draft: bool,
) -> tuple[int, str]:
    command = ["gh", "pr", "create"]
    if draft:
        command.append("--draft")
    command.extend(
        [
            "--base",
            base_branch,
            "--head",
            head_branch,
            "--title",
            title,
            "--body",
            body,
        ]
    )
    result = subprocess.run(command, cwd=repo_root, check=True, **_GH_SUBPROCESS_KWARGS)
    pr_url = result.stdout.strip()
    pr_number, confirmed_url = find_pr_for_head(repo_root, head_branch)
    return pr_number, confirmed_url or pr_url


def find_pr_for_head(
    repo_root: Path, head_branch: str
) -> tuple[int | None, str | None]:
    command = [
        "gh",
        "pr",
        "list",
        "--head",
        head_branch,
        "--state",
        "all",
        "--limit",
        "1",
        "--json",
        "number,url",
    ]
    result = subprocess.run(command, cwd=repo_root, check=True, **_GH_SUBPROCESS_KWARGS)
    payload = json.loads(result.stdout)
    if not payload:
        return None, None
    item = payload[0]
    return int(item["number"]), str(item["url"])


def get_pr(repo_root: Path, pr_number: int) -> dict[str, object]:
    command = [
        "gh",
        "pr",
        "view",
        str(pr_number),
        "--json",
        "number,url,state,isDraft,mergedAt,headRefName,headRefOid,baseRefName,reviewDecision,mergeStateStatus,mergeable,assignees",
    ]
    result = subprocess.run(command, cwd=repo_root, check=True, **_GH_SUBPROCESS_KWARGS)
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected PR payload for #{pr_number}")
    return payload


def mark_pr_ready(repo_root: Path, pr_number: int) -> str | None:
    command = ["gh", "pr", "ready", str(pr_number)]
    result = subprocess.run(command, cwd=repo_root, check=False, **_GH_SUBPROCESS_KWARGS)
    if result.returncode == 0:
        return None
    return _command_failure(result)


def merge_pr(
    repo_root: Path,
    pr_number: int,
    *,
    merge_method: str,
    head_sha: str,
) -> str | None:
    command = [
        "gh",
        "pr",
        "merge",
        str(pr_number),
        "--match-head-commit",
        head_sha,
    ]
    if merge_method == "merge":
        command.append("--merge")
    elif merge_method == "rebase":
        command.append("--rebase")
    elif merge_method == "squash":
        command.append("--squash")
    else:
        raise RuntimeError(f"unsupported merge method: {merge_method}")

    result = subprocess.run(command, cwd=repo_root, check=False, **_GH_SUBPROCESS_KWARGS)
    if result.returncode == 0:
        return None
    return _command_failure(result)


def get_pr_check_rollup(repo_root: Path, pr_number: int) -> list[dict[str, object]]:
    command = [
        "gh",
        "pr",
        "view",
        str(pr_number),
        "--json",
        "statusCheckRollup",
    ]
    result = subprocess.run(command, cwd=repo_root, check=True, **_GH_SUBPROCESS_KWARGS)
    payload = json.loads(result.stdout)
    rollup = payload.get("statusCheckRollup", [])
    if isinstance(rollup, list):
        return rollup
    return []


def get_branch_head_sha(repo_root: Path, branch_name: str) -> str:
    owner, repo = _get_repo_owner_and_name(repo_root)
    command = [
        "gh",
        "api",
        f"repos/{owner}/{repo}/commits/{branch_name}",
        "--jq",
        ".sha",
    ]
    result = subprocess.run(command, cwd=repo_root, check=True, **_GH_SUBPROCESS_KWARGS)
    return result.stdout.strip()


def get_commit_check_rollup(repo_root: Path, ref: str) -> list[dict[str, object]]:
    owner, repo = _get_repo_owner_and_name(repo_root)
    check_runs_command = [
        "gh",
        "api",
        f"repos/{owner}/{repo}/commits/{ref}/check-runs",
        "-H",
        "Accept: application/vnd.github+json",
    ]
    check_runs_result = subprocess.run(
        check_runs_command,
        cwd=repo_root,
        check=True,
        **_GH_SUBPROCESS_KWARGS,
    )
    check_runs_payload = json.loads(check_runs_result.stdout)
    check_runs = check_runs_payload.get("check_runs", [])

    commit_status_command = [
        "gh",
        "api",
        f"repos/{owner}/{repo}/commits/{ref}/status",
        "-H",
        "Accept: application/vnd.github+json",
    ]
    commit_status_result = subprocess.run(
        commit_status_command,
        cwd=repo_root,
        check=True,
        **_GH_SUBPROCESS_KWARGS,
    )
    commit_status_payload = json.loads(commit_status_result.stdout)
    commit_statuses = commit_status_payload.get("statuses", [])

    rollup: list[dict[str, object]] = []
    if isinstance(check_runs, list):
        for item in check_runs:
            if not isinstance(item, dict):
                continue
            rollup.append(
                {
                    "__typename": "CheckRun",
                    "name": item.get("name"),
                    "status": item.get("status"),
                    "conclusion": item.get("conclusion"),
                    "workflowName": item.get("name"),
                    "detailsUrl": item.get("details_url") or item.get("html_url"),
                    "startedAt": item.get("started_at"),
                    "completedAt": item.get("completed_at"),
                }
            )

    if isinstance(commit_statuses, list):
        for item in commit_statuses:
            if not isinstance(item, dict):
                continue
            state = str(item.get("state", "") or "").strip().lower()
            if state == "pending":
                status = "PENDING"
                conclusion = ""
                completed_at = ""
            elif state in {"success", "failure", "error"}:
                status = "COMPLETED"
                conclusion = "SUCCESS" if state == "success" else "FAILURE"
                completed_at = item.get("updated_at")
            else:
                status = str(item.get("state", "") or "")
                conclusion = str(item.get("state", "") or "")
                completed_at = item.get("updated_at")
            rollup.append(
                {
                    "__typename": "StatusContext",
                    "context": item.get("context"),
                    "status": status,
                    "conclusion": conclusion,
                    "workflowName": "",
                    "detailsUrl": item.get("target_url"),
                    "startedAt": item.get("created_at"),
                    "completedAt": completed_at,
                }
            )

    return rollup


def get_run_failed_log(repo_root: Path, details_url: str) -> str | None:
    match = re.search(
        r"/actions/runs/(?P<run_id>\d+)(?:/job/(?P<job_id>\d+))?", details_url
    )
    if match is None:
        return None

    command = ["gh", "run", "view", match.group("run_id")]
    job_id = match.group("job_id")
    if job_id:
        command.extend(["--job", job_id])
    command.append("--log-failed")
    result = subprocess.run(command, cwd=repo_root, check=False, **_GH_SUBPROCESS_KWARGS)
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    return output or None


def _get_repo_owner_and_name(repo_root: Path) -> tuple[str, str]:
    command = [
        "gh",
        "repo",
        "view",
        "--json",
        "nameWithOwner",
        "--jq",
        ".nameWithOwner",
    ]
    result = subprocess.run(command, cwd=repo_root, check=True, **_GH_SUBPROCESS_KWARGS)
    value = result.stdout.strip()
    owner, _, repo = value.partition("/")
    if not owner or not repo:
        raise RuntimeError(f"unexpected repository identity: {value!r}")
    return owner, repo


def _run_graphql(
    repo_root: Path, query: str, variables: dict[str, str | int]
) -> dict[str, object]:
    command = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        flag = "-F" if isinstance(value, int) else "-f"
        command.extend([flag, f"{key}={value}"])
    result = subprocess.run(command, cwd=repo_root, check=True, **_GH_SUBPROCESS_KWARGS)
    return json.loads(result.stdout)


def _hydrate_issue_candidate(
    repo_root: Path,
    payload: dict[str, object],
    *,
    default_branch: str,
) -> IssueCandidate:
    fallback_numbers = _parse_blocked_by_line(str(payload.get("body", "") or ""))
    if fallback_numbers:
        payload = {
            **payload,
            "_fallbackDependencyMap": _load_fallback_dependency_map(
                repo_root,
                fallback_numbers,
            ),
        }
    return _parse_graphql_issue(payload, default_branch=default_branch)


def _parse_graphql_issue(
    payload: dict[str, object], *, default_branch: str
) -> IssueCandidate:
    body = str(payload.get("body", "") or "")
    labels = tuple(
        str(item.get("name"))
        for item in _connection_nodes(payload.get("labels"))
        if isinstance(item, dict) and item.get("name") is not None
    )
    assignees = tuple(
        str(item.get("login"))
        for item in _connection_nodes(payload.get("assignees"))
        if isinstance(item, dict) and item.get("login") is not None
    )

    blocked_by_payload = payload.get("blockedBy")
    blocked_by_nodes = [
        item
        for item in _connection_nodes(blocked_by_payload)
        if isinstance(item, dict)
    ]
    native_dependencies = tuple(
        _parse_issue_dependency(item, default_branch=default_branch)
        for item in blocked_by_nodes
    )
    dependency_source = "none"
    dependency_error: str | None = None
    blocked_by = native_dependencies
    total_blockers = _optional_int(
        _optional_mapping(payload.get("issueDependenciesSummary")).get("totalBlockedBy")
    )
    native_total_count = _optional_int(
        _optional_mapping(blocked_by_payload).get("totalCount")
    )
    if blocked_by:
        dependency_source = "native"
    elif total_blockers and total_blockers > 0:
        dependency_source = "native"
        dependency_error = DEPENDENCY_STATE_ERROR
    else:
        fallback_numbers = _parse_blocked_by_line(body)
        if fallback_numbers:
            dependency_source = "body"
            blocked_by = _load_fallback_dependencies(
                payload,
                fallback_numbers,
                default_branch=default_branch,
            )
            if any(dependency.merged is None for dependency in blocked_by):
                dependency_error = DEPENDENCY_STATE_ERROR

    if (
        dependency_source == "native"
        and native_total_count is not None
        and native_total_count > len(blocked_by)
    ):
        dependency_error = DEPENDENCY_STATE_ERROR

    return IssueCandidate(
        number=int(payload["number"]),
        title=str(payload["title"]),
        state=str(payload["state"]),
        labels=labels,
        assignees=assignees,
        url=str(payload["url"]) if payload.get("url") is not None else None,
        body=body,
        dependency_source=dependency_source,
        blocked_by=blocked_by,
        dependency_error=dependency_error,
    )


def _load_fallback_dependencies(
    payload: dict[str, object],
    issue_numbers: tuple[int, ...],
    *,
    default_branch: str,
) -> tuple[IssueDependency, ...]:
    dependency_map = _optional_mapping(payload.get("_fallbackDependencyMap"))
    dependencies = []
    for issue_number in issue_numbers:
        raw_dependency = dependency_map.get(str(issue_number))
        if not isinstance(raw_dependency, dict):
            dependencies.append(
                IssueDependency(number=issue_number, merged=None)
            )
            continue
        dependencies.append(
            _parse_issue_dependency(raw_dependency, default_branch=default_branch)
        )
    return tuple(dependencies)


def _load_fallback_dependency_map(
    repo_root: Path,
    issue_numbers: tuple[int, ...],
) -> dict[str, dict[str, object]]:
    if not issue_numbers:
        return {}
    owner, repo = _get_repo_owner_and_name(repo_root)
    issue_fragments = []
    for issue_number in sorted(set(issue_numbers)):
        issue_fragments.append(
            f"""
            issue_{issue_number}: issue(number: {issue_number}) {{
              {ISSUE_DEPENDENCY_FRAGMENT}
            }}
            """
        )
    payload = _run_graphql(
        repo_root,
        """
        query($owner: String!, $repo: String!) {
          repository(owner: $owner, name: $repo) {
            """
        + "\n".join(issue_fragments)
        + """
          }
        }
        """,
        {
            "owner": owner,
            "repo": repo,
        },
    )
    repository = _optional_mapping(_optional_mapping(payload.get("data")).get("repository"))
    dependency_map: dict[str, dict[str, object]] = {}
    for issue_number in issue_numbers:
        raw_dependency = repository.get(f"issue_{issue_number}")
        if isinstance(raw_dependency, dict):
            dependency_map[str(issue_number)] = raw_dependency
    return dependency_map


def _parse_issue_dependency(
    payload: dict[str, object], *, default_branch: str
) -> IssueDependency:
    state = _optional_str(payload.get("state"))
    merged = _has_merged_pr_to_branch(payload, default_branch=default_branch)
    return IssueDependency(
        number=int(payload["number"]),
        title=_optional_str(payload.get("title")),
        state=state,
        merged=merged,
    )


def _has_merged_pr_to_branch(
    payload: dict[str, object], *, default_branch: str
) -> bool:
    for pr in _iter_linked_pull_requests(payload):
        if (
            _optional_bool(pr.get("merged")) is True
            and _optional_str(pr.get("baseRefName")) == default_branch
        ):
            return True
    return False


def _iter_linked_pull_requests(
    payload: dict[str, object],
) -> list[dict[str, object]]:
    pull_requests: list[dict[str, object]] = []
    for item in _connection_nodes(payload.get("closedByPullRequestsReferences")):
        if isinstance(item, dict):
            pull_requests.append(item)

    for item in _connection_nodes(payload.get("timelineItems")):
        if not isinstance(item, dict):
            continue
        source = _optional_mapping(item.get("source"))
        if _optional_str(source.get("__typename")) != "PullRequest":
            continue
        pull_requests.append(source)
    return pull_requests


def _connection_nodes(value: object) -> list[object]:
    if isinstance(value, dict):
        nodes = value.get("nodes")
        if isinstance(nodes, list):
            return nodes
    if isinstance(value, list):
        return value
    return []


def _optional_mapping(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    return {}


def _optional_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    return None


def _optional_str(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _parse_blocked_by_line(body: str) -> tuple[int, ...]:
    match = BLOCKED_BY_LINE_RE.search(body)
    if match is None:
        return ()
    return tuple(
        int(token.strip()[1:])
        for token in match.group("issues").split(",")
    )


def _command_failure(result: subprocess.CompletedProcess[str]) -> str:
    stderr = result.stderr.strip()
    if stderr:
        return stderr
    stdout = result.stdout.strip()
    if stdout:
        return stdout
    return f"gh command exited with code {result.returncode}"


def _require_assignee_username(
    assignee_username: str | None,
    *,
    scope: str,
) -> str:
    assignee = (assignee_username or "").strip()
    if assignee:
        return assignee
    raise RuntimeError(
        f"GitHub assignee username is required before Sandcastle can assign {scope}."
    )


def _edit_issue_assignee(
    repo_root: Path,
    *,
    issue_number: int,
    assignee_username: str,
    action: str,
) -> None:
    if action == "add":
        flag = "--add-assignee"
        verb = "assign"
    elif action == "remove":
        flag = "--remove-assignee"
        verb = "remove"
    else:
        raise RuntimeError(f"unsupported issue assignee action: {action}")

    command = ["gh", "issue", "edit", str(issue_number), flag, assignee_username]
    result = subprocess.run(command, cwd=repo_root, check=False, **_GH_SUBPROCESS_KWARGS)
    if result.returncode != 0:
        raise RuntimeError(
            f"failed to {verb} issue #{issue_number} assignee '{assignee_username}': "
            f"{_command_failure(result)}"
        )
