from __future__ import annotations

from pathlib import Path

from .state_store import utc_now_iso, write_json_file


def write_revision_plan(
    revision_dir: Path,
    *,
    issue_number: int,
    branch: str,
    findings_payload: dict[str, object],
) -> Path:
    findings = findings_payload.get("findings", [])
    next_actions: list[str] = []
    if isinstance(findings, list):
        for finding in findings[:5]:
            if not isinstance(finding, dict):
                continue
            summary = str(finding.get("summary", "Address finding"))
            next_actions.append(summary)

    payload = {
        "issue_number": issue_number,
        "branch": branch,
        "findings_status": findings_payload.get("status"),
        "finding_count": len(findings) if isinstance(findings, list) else 0,
        "next_actions": next_actions,
        "written_at_utc": utc_now_iso(),
    }
    return write_json_file(revision_dir / f"{issue_number}.json", payload)
