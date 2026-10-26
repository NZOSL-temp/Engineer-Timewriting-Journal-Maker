#!/usr/bin/env python3
"""Change MASTER_KEY: re-encrypt everything with a new one, keeping everyone's password.

Run by the data repo's Action while the NEW_MASTER_KEY secret is set (see update.yml):

    OLD_MASTER_KEY=… NEW_MASTER_KEY=… python .website/tools/rotate_master_key.py --data-dir . --push

Every key in the system comes from MASTER_KEY, except each person's user key and timesheet
keys, which are random. So this re-seals those with the new key, re-encrypts every file in
the data repo (giving the month files their new meaningless names), and renames the
timesheet issues, whose titles carry a tag made from the key. Passwords unlock the user
keys directly, so they keep working, and the next step of the Action republishes the
website with the new key. It's safe to run again: once the files open with the new key it
only renames any issues still carrying old tags.

Afterwards, put the same new key in MASTER_KEY and delete NEW_MASTER_KEY.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from etjm_accounts import Keys, load_state_data  # noqa: E402
from etjm_core import PASSWORD_TITLE_RE, TITLE_RE, parse_master_key, seal, user_tag  # noqa: E402
from etjm_github import api, commit_and_push, list_issues  # noqa: E402
from etjm_vault import JOBS, OUTPUT, REPORT, SETTINGS, TIMESHEETS, USERS, Vault  # noqa: E402

MANAGED = [SETTINGS, USERS, REPORT, JOBS, TIMESHEETS, OUTPUT]


def already_rotated(data_dir: Path, new: bytes) -> bool:
    try:
        return Vault(data_dir, new).read(USERS) is not None
    except ValueError:
        return False


def rotate_files(data_dir: Path, old: bytes, new: bytes) -> int:
    """Re-encrypt every file in the data repo with the new key. Returns how many."""
    old_vault, new_vault = Vault(data_dir, old), Vault(data_dir, new)
    old_keys, new_keys = Keys(old), Keys(new)
    count = 0
    for rel in (SETTINGS, REPORT, JOBS):
        data = old_vault.read(rel)
        if data is not None:
            new_vault.write(rel, data)
            count += 1
    state, _ = load_state_data(old_vault.read_json(USERS))
    for username, record in state["users"].items():
        record["userKey"] = new_keys.seal_user_key(username, old_keys.user_key(username, record))
        record["dataKeys"] = [new_keys.seal_data_key(username, i, key)
                              for i, key in enumerate(old_keys.data_keys(username, record))]
        if record.get("password"):
            record["password"] = new_keys.seal_password(username, old_keys.password(username, record))
        recover = record.get("recover")
        if recover:
            rid = recover["id"]
            recover["userKey"] = seal(new_keys.state, old_keys.recover_user_key(username, recover), f"state|recoverUserKey|{username}|{rid}")
            recover["dataKey"] = seal(new_keys.state, old_keys.recover_data_key(username, recover), f"state|recoverDataKey|{username}|{rid}")
    new_vault.write_json(USERS, state)
    count += 1
    for folder in (TIMESHEETS, OUTPUT):
        for month, (rel, data) in old_vault.months(folder).items():
            (data_dir / rel).unlink()
            new_vault.write_json(new_vault.month_path(folder, month), data)
            count += 1
    return count


def retitles(issues: list[dict], usernames, old: bytes, new: bytes) -> list[tuple[int, str]]:
    """Timesheet and password-change issues still titled with an old tag, and their new titles."""
    tags = {user_tag(old, u): user_tag(new, u) for u in usernames}
    out = []
    for issue in issues:
        title = (issue.get("title") or "").strip()
        match = TITLE_RE.match(title) or PASSWORD_TITLE_RE.match(title)
        if not match or "pull_request" in issue:
            continue
        old_tag = match.group(match.lastindex)
        if old_tag in tags and tags[old_tag] != old_tag:
            out.append((issue["number"], title[: -len(old_tag)] + tags[old_tag]))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", default=".")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--push", action="store_true", help="commit, push and rename the issues")
    args = parser.parse_args()
    data_dir = Path(args.data_dir).resolve()
    try:
        old = parse_master_key(os.environ.get("OLD_MASTER_KEY", ""))
        new = parse_master_key(os.environ.get("NEW_MASTER_KEY", ""))
    except ValueError as exc:
        sys.exit(f"OLD_MASTER_KEY (the MASTER_KEY secret) and NEW_MASTER_KEY must both be set: {exc}")
    if old == new:
        print("MASTER_KEY already is the new key. Delete the NEW_MASTER_KEY secret.")
        return 0

    if already_rotated(data_dir, new):
        print("The files already use the new key.")
    else:
        try:
            Vault(data_dir, old).read(USERS)
        except ValueError:
            sys.exit("The files open with neither MASTER_KEY nor NEW_MASTER_KEY. Check both secrets.")
        result = {}

        def regenerate():
            if not already_rotated(data_dir, new):
                result["count"] = rotate_files(data_dir, old, new)

        if args.push:
            commit_and_push(data_dir, MANAGED, "Re-encrypt everything with the new MASTER_KEY", regenerate)
        else:
            regenerate()
        print(f"Re-encrypted {result.get('count', 0)} file(s) with the new key.")

    state, _ = load_state_data(Vault(data_dir, new).read_json(USERS))
    token = os.environ.get("GITHUB_TOKEN")
    if args.push and token and args.repo:
        renames = retitles(list_issues(args.repo, token), state["users"], old, new)
        for number, title in renames:
            api("PATCH", f"/repos/{args.repo}/issues/{number}", token, {"title": title})
        print(f"Renamed {len(renames)} issue(s) for the new key.")
    print("Next: put the new key in the MASTER_KEY secret too, then delete NEW_MASTER_KEY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
