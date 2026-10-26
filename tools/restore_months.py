#!/usr/bin/env python3
"""Put a month's saved timesheets back on GitHub, or empty a month.

Engineers' "My timesheets" lists the months they've saved as issues in the data repo,
while the journals use the verified copies in timesheets/. If the issues are lost (say the
data repo was recreated), the journals are fine but the months vanish from My timesheets.

    --restore 2026-09   re-creates each person's timesheet issue for that month from the
                        verified copy, encrypted with their newest data key as the website
                        saves it, so it's listed (and can be copied from) again. People who
                        already have an issue for the month are skipped.
    --clear 2026-10     reopens the month and removes its verified copies and journal, so it
                        starts empty (any issues for it should be gone first).

Run it from an Action: it needs MASTER_KEY, GITHUB_TOKEN (issues: write) and
GITHUB_REPOSITORY, then commits and pushes with --push. Afterwards run "Update website
and journals".
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from etjm_accounts import Keys, load_state_data  # noqa: E402
from etjm_core import TIMESHEET_V2, encrypt_timesheet, parse_master_key, user_tag  # noqa: E402
from etjm_github import api, commit_and_push, list_issues  # noqa: E402
from etjm_vault import OUTPUT, SETTINGS, TIMESHEETS, USERS, Vault  # noqa: E402

MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def restored_issues(vault: Vault, keys: Keys, month: str, existing_titles: set[str]) -> list[dict]:
    """The issues to create for `month`: {"title", "body"} for each verified timesheet."""
    copy = vault.months(TIMESHEETS).get(month)
    if not copy:
        return []
    state, _ = load_state_data(vault.read_json(USERS))
    issues = []
    for sheet in copy[1].get("timesheets", []):
        username = sheet.get("username")
        record = state["users"].get(username)
        if not record or sheet.get("deleted"):
            continue
        title = f"Timesheet {month} · {user_tag(keys.master, username)}"
        if title in existing_titles:
            continue
        data_keys = keys.data_keys(username, record)
        data = {k: v for k, v in sheet.items() if k != "issue"}
        token = encrypt_timesheet(data, data_keys[-1], len(data_keys) - 1, username)
        issues.append({"title": title, "body": f"{TIMESHEET_V2}\nEncrypted.\n\n```etjm\n{token}\n```\n"})
    return issues


def clear_month(vault: Vault, month: str) -> None:
    """Reopen `month` and remove its verified copies and journal."""
    settings = vault.read_json(SETTINGS)
    settings["closedMonths"] = [m for m in settings.get("closedMonths", []) if m != month]
    vault.write_json(SETTINGS, settings)
    for folder in (TIMESHEETS, OUTPUT):
        found = vault.months(folder).get(month)
        if found:
            (vault.root / found[0]).unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", default=".", help="checkout of the data repo")
    parser.add_argument("--restore", action="append", default=[], help="month to put back on GitHub, like 2026-09")
    parser.add_argument("--clear", action="append", default=[], help="month to empty, like 2026-10")
    parser.add_argument("--push", action="store_true", help="commit and push the changed files")
    args = parser.parse_args()
    for month in args.restore + args.clear:
        if not MONTH_RE.match(month):
            sys.exit(f"{month} isn't a month like 2026-09.")
    keys = Keys(parse_master_key(os.environ.get("MASTER_KEY", "")))
    data_dir = Path(args.data_dir).resolve()
    vault = Vault(data_dir, keys.master)
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")

    if args.restore:
        if not repo or not token:
            sys.exit("GITHUB_TOKEN and GITHUB_REPOSITORY are needed to create the issues.")
        titles = {(i.get("title") or "").strip() for i in list_issues(repo, token) if "pull_request" not in i}
        for month in args.restore:
            created = 0
            for issue in restored_issues(vault, keys, month, titles):
                api("POST", f"/repos/{repo}/issues", token, issue)
                created += 1
            print(f"{month}: put back {created} timesheet(s).")

    if args.clear:
        def clear():
            for month in args.clear:
                clear_month(vault, month)
        if args.push:
            pushed, _ = commit_and_push(data_dir, [SETTINGS, TIMESHEETS, OUTPUT], "Empty a month", clear)
            print(f"Emptied {', '.join(args.clear)}." + (" Pushed." if pushed else " Nothing to change."))
        else:
            clear()
            print(f"Emptied {', '.join(args.clear)} (not pushed).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
