#!/usr/bin/env python3
"""Rebuild the journal and engineer workbooks from the encrypted timesheets.

This runs in the private data repo (see data-repo-template/.github/workflows/):

    python .website/tools/build_outputs.py --data-dir . --push

It reads every timesheet issue, then rewrites output/ and timesheets/ from scratch,
so the files always match the latest saves. Like everything in the data repo, they're
encrypted (see etjm_vault.py): one file per month in each folder, with a meaningless name.
To try it locally without GitHub, give it a JSON file of issues instead:

    python tools/build_outputs.py --data-dir path/to/data --issues-file issues.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from etjm_accounts import Keys, load_state_data  # noqa: E402
from etjm_core import TimesheetError, collect_timesheets, parse_master_key, parse_timesheet, user_tag  # noqa: E402
from etjm_github import commit_and_push, list_issues  # noqa: E402
from etjm_outputs import build_all  # noqa: E402
from etjm_vault import JOBS, OUTPUT, TIMESHEETS, USERS, Vault, pack_month_files  # noqa: E402

MANAGED = [OUTPUT, TIMESHEETS]


def load_jobs_index(vault: Vault) -> dict:
    data = vault.read_json(JOBS) or {}
    return {job["no"]: job for job in data.get("jobs", [])}


def load_mirror(vault: Vault, names: dict, closed: set[str]) -> tuple[dict, list[str]]:
    """The verified copies kept in timesheets/ (only the Actions write there)."""
    mirror, warnings = {}, []
    for month, (rel, data) in vault.months(TIMESHEETS).items():
        if month in closed:
            continue
        for saved in data.get("timesheets", []):
            try:
                username = saved["username"]
                timesheet = parse_timesheet({**saved, "employee": names.get(username, saved.get("employee"))})
            except (TimesheetError, KeyError, TypeError) as exc:
                warnings.append(f"A timesheet in {rel} couldn't be read: {exc}")
                continue
            timesheet.username = username
            timesheet.issue = saved.get("issue")
            mirror[(username, month)] = timesheet
    return mirror, warnings


def write_tree(vault: Vault, files: dict[str, bytes], closed: set[str] = frozenset()) -> None:
    """Replace output/ and timesheets/ with `files` (one encrypted file per month and folder),
    except closed months, whose files are left exactly as they are."""
    wanted = {vault.month_path(folder, month): value for (folder, month), value in pack_month_files(files).items()}
    for folder in MANAGED:
        base = vault.root / folder
        if not base.exists():
            continue
        kept = {rel for month, (rel, _) in vault.months(folder).items() if month in closed}
        for child in base.iterdir():
            rel = f"{folder}/{child.name}"
            if rel not in wanted and rel not in kept:
                child.unlink()
        if not any(base.iterdir()):
            base.rmdir()
    for rel, value in wanted.items():
        vault.write_json(rel, value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", default=".", help="checkout of the data repo")
    parser.add_argument("--push", action="store_true", help="commit and push the result")
    parser.add_argument("--issues-file", help="read issues from this JSON file instead of GitHub")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"), help="owner/name of the data repo")
    args = parser.parse_args()
    data_dir = Path(args.data_dir).resolve()

    def fetch_issues() -> list[dict]:
        if args.issues_file:
            return json.loads(Path(args.issues_file).read_text(encoding="utf-8"))
        token = os.environ.get("GITHUB_TOKEN")
        if not token or not args.repo:
            sys.exit("GITHUB_TOKEN and GITHUB_REPOSITORY (or --repo) are needed to read the issues.")
        return list_issues(args.repo, token)

    try:
        keys = Keys(parse_master_key(os.environ.get("MASTER_KEY", "")))
    except ValueError as exc:
        sys.exit(f"The MASTER_KEY secret is missing or wrong: {exc}")
    vault = Vault(data_dir, keys.master)

    def regenerate():
        settings = vault.settings()
        closed = set(settings["closedMonths"])
        names = {e["username"]: e["name"] for e in settings["employees"]}
        state, _ = load_state_data(vault.read_json(USERS))
        keys_by_tag = {
            user_tag(keys.master, username): {"username": username, "name": names[username],
                                              "dataKeys": keys.data_keys(username, record)}
            for username, record in state["users"].items() if username in names
        }
        mirror, mirror_warnings = load_mirror(vault, names, closed)
        timesheets, warnings = collect_timesheets(fetch_issues(), keys_by_tag, mirror, closed)
        warnings = mirror_warnings + warnings
        files, checks = build_all(timesheets, settings, load_jobs_index(vault))
        write_tree(vault, files, closed)
        return timesheets, warnings, checks

    if args.push:
        pushed, (timesheets, warnings, checks) = commit_and_push(
            data_dir, MANAGED, "Update journal and engineer workbooks", regenerate)
    else:
        pushed, (timesheets, warnings, checks) = False, regenerate()

    # Only counts here: Actions logs are easier to see than the encrypted files, so they name nobody.
    lines = [f"Read {len(timesheets)} timesheet(s)."]
    for month, month_checks in checks.items():
        count = sum(1 for t in timesheets if t.month_key == month)
        lines.append(f"- {month}: {count} engineer(s), {len(month_checks)} check(s) (see the Admin page)")
    lines += [f"- Warning: {w}" for w in warnings]
    lines.append("Pushed the updated files." if pushed else "No files changed." if args.push else "Files written (not pushed).")
    print("\n".join(lines))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("### Journal and workbooks\n\n" + "\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
