"""Generate the live QA campaign map from GitHub Issues (read-only).

Run from the repository root with: python tools/qa/map.py
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = "Yatin-Code/openseek-telegram"
EPICS = ((40, "Epic G"), (41, "Epic H"))
HARNESS = 42
OUTPUT = Path(__file__).resolve().parents[2] / "docs" / "QA-MAP.md"


def api(path: str, *, paginate: bool = False):
    command = ["gh", "api"] + (["--paginate", "--slurp"] if paginate else []) + [path]
    proc = subprocess.run(command, capture_output=True, text=True)
    if proc.returncode:
        raise RuntimeError(f"gh api {path} failed: {proc.stderr.strip()[:500]}")
    try:
        result = json.loads(proc.stdout)
        return [item for page in result for item in page] if paginate else result
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON from gh api {path}: {exc}") from exc


def markdown(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def main() -> None:
    issue_map = {}
    for epic, name in EPICS:
        children = api(f"repos/{REPO}/issues/{epic}/sub_issues", paginate=True)
        for child in children:
            issue_map[child["number"]] = (name, child)
    issue_map[HARNESS] = ("Epic H + harness", api(f"repos/{REPO}/issues/{HARNESS}"))
    issues = list(issue_map.values())

    def latest_comment(issue):
        comments = api(f"repos/{REPO}/issues/{issue['number']}/comments?per_page=100", paginate=True)
        if not comments:
            return None
        latest = max(comments, key=lambda row: (row["created_at"], int(row["id"])))
        return latest.get("html_url")

    with ThreadPoolExecutor(max_workers=8) as pool:
        latest_urls = list(pool.map(latest_comment, (issue for _, issue in issues)))
    rows = []
    for (surface, issue), latest in zip(issues, latest_urls):
        number = issue["number"]
        labels = [label["name"] for label in issue.get("labels", [])]
        qa = ", ".join(label for label in labels if label.startswith("qa:")) or "not recorded"
        blockers = issue.get("issue_dependencies_summary", {}).get("blocked_by", 0) or 0
        rows.append((surface, number, issue["title"], issue["state"], qa,
                     latest, blockers, issue["html_url"]))

    bugs = api(f"repos/{REPO}/issues?state=open&labels=bug&per_page=100", paginate=True)
    bug_rows = [(bug["number"], bug["title"], bug["html_url"])
                for bug in bugs if "pull_request" not in bug]
    generated = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M IST")
    lines = [
        "# QA campaign map", "", f"Generated: {generated}", "",
        "Regenerate from the repository root: `python tools/qa/map.py`.", "",
        "## Tickets", "",
        "| Ticket | Surface | State | QA status | Latest comment | Open blockers |",
        "|---|---|---|---|---|---:|",
    ]
    for surface, number, title, state, qa, latest, blockers, url in sorted(rows, key=lambda row: row[1]):
        comment_link = f"[latest comment]({latest})" if latest else "—"
        lines.append(f"| [#{number} {markdown(title)}]({url}) | {surface} | {state} | {qa} | {comment_link} | {blockers} |")
    lines.extend(["", "## Open bug followups", ""])
    if bug_rows:
        lines.extend(f"- [#{number} {markdown(title)}]({url})" for number, title, url in sorted(bug_rows))
    else:
        lines.append("None.")
    lines.extend([
        "", "## Release gate", "",
        "The affected slice must be green. Campaign acceptance requires both agent and maintainer sign-off; this map does not manufacture signatures.",
        "", "## Ordering note", "",
        "G9 (#51) has a known close-order cycle with campaign-end teardown #74: #74 requires both epics closed, while G9 is not complete until the teardown steps are resolved. Track the ordering in the current issue comments.",
        "",
    ])
    content = "\n".join(lines)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=OUTPUT.parent,
                                         prefix=".QA-MAP.", delete=False) as temp:
            temp_name = temp.name
            temp.write(content)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_name, OUTPUT)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, KeyError, TypeError) as exc:
        raise SystemExit(f"QA map generation failed: {exc}") from exc
