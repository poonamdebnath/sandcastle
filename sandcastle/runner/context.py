from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .state_store import utc_now_iso, write_json_file


def fetch_issue_context(repo_root: Path, issue_number: int) -> dict[str, object]:
    command = [
        "gh",
        "issue",
        "view",
        str(issue_number),
        "--json",
        "number,title,body,comments,labels,assignees,url",
    ]
    result = subprocess.run(
        command,
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    comments = []
    for item in payload.get("comments", []):
        if not isinstance(item, dict):
            continue
        author = item.get("author")
        comments.append(
            {
                "author": author.get("login") if isinstance(author, dict) else None,
                "body": item.get("body", ""),
                "createdAt": item.get("createdAt"),
                "url": item.get("url"),
            }
        )

    return {
        "issue_number": payload["number"],
        "title": payload["title"],
        "url": payload.get("url"),
        "body": payload.get("body", ""),
        "labels": [label["name"] for label in payload.get("labels", []) if isinstance(label, dict) and "name" in label],
        "assignees": [user["login"] for user in payload.get("assignees", []) if isinstance(user, dict) and "login" in user],
        "comments": comments,
        "loaded_at_utc": utc_now_iso(),
    }


def write_issue_context(contexts_dir: Path, issue_number: int, payload: dict[str, object]) -> Path:
    return write_json_file(contexts_dir / f"{issue_number}.json", payload)
