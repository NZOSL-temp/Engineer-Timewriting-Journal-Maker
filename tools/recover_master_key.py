#!/usr/bin/env python3
"""Lost MASTER_KEY? Re-encrypt everything for a new one, using an office admin's sign-in.

If the MASTER_KEY secret was replaced or lost and nobody has the old value, nothing in the
data repo opens with the new one, and the Action fails. But an admin's profile holds the
file key and the password-store key, and each issued password opens that person's user key
(and their profile, with their timesheet keys). So this rebuilds everything for the new key
and keeps everyone's password:

    pbpaste | ADMIN_PASSWORD='…' python tools/recover_master_key.py \\
        --data-dir ../Engineer-Timewriting-Data --site-dir . --admin alex.smith --renames renames.json

It reads the new MASTER_KEY from standard input (so it's never typed or shown) and writes the
re-encrypted files into the data repo checkout. Timesheet issue titles carry a tag made from
the key, so it writes the old and new tags to --renames. Then: commit and push the data repo,
rename the issues, and run "Update website and journals" (which must have the same new key).

Someone who chose their own password can't be recovered this way, because nobody knows it.
They get a new issued password (shown on the Admin page), as with an admin reset; their
journals are unaffected. If the new key is already in use, it does nothing.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from etjm_accounts import Keys, _add_data_key, _issue_password  # noqa: E402
from etjm_core import (  # noqa: E402
    normalize_username,
    parse_master_key,
    parse_settings,
    people,
    unseal,
    unseal_profile,
    unwrap_user_key,
    user_id,
    user_tag,
)
from etjm_vault import JOBS, OUTPUT, REPORT, SETTINGS, TIMESHEETS, USERS, Vault  # noqa: E402


def recover(data_dir: Path, public_users: dict, admin: str, admin_password: str, new: bytes) -> dict:
    """Re-encrypt the data repo for `new`. Returns {"kept": [...], "reset": [...], "renames": {old_tag: new_tag}}."""
    entry = public_users.get(user_id(admin))
    if not entry:
        raise ValueError(f"{admin} isn't in the website's sign-in file.")
    try:
        admin_key = unwrap_user_key(entry, admin_password, admin)
    except ValueError as exc:
        raise ValueError("That admin password isn't right.") from exc
    profile = unseal_profile(admin_key, entry["profile"], admin)
    if not profile.get("storeKey") or not profile.get("passwordKey"):
        raise ValueError(f"{admin} isn't an admin, so their sign-in can't open the files.")
    old_vault = Vault.from_store_key(data_dir, base64.b64decode(profile["storeKey"]))
    old_passwords = base64.b64decode(profile["passwordKey"])
    new_vault, new_keys = Vault(data_dir, new), Keys(new)

    state = old_vault.read_json(USERS)
    settings = parse_settings(old_vault.read_json(SETTINGS))
    is_admin = {p["username"]: p["admin"] for p in people(settings)}
    result = {"kept": [], "reset": [], "renames": {}}
    for username, record in state["users"].items():
        issued = unseal(old_passwords, record["password"], f"password|{username}").decode("utf-8") if record.get("password") else None
        user_key = admin_key if username == admin else None
        if user_key is None and issued:
            try:
                user_key = unwrap_user_key(record["wrap"], issued, username)
            except ValueError:
                user_key = None
        own_profile = None
        public_entry = public_users.get(user_id(username)) or {}
        if user_key and public_entry.get("profile"):
            try:
                own_profile = unseal_profile(user_key, public_entry["profile"], username)
            except ValueError:
                own_profile = None
        record.pop("recover", None)  # an emailed link made with the old key can't be honoured
        if user_key is None or own_profile is None:
            # Nobody knows this person's key: a fresh sign-in, as an admin reset gives.
            record["dataKeys"] = []
            _add_data_key(new_keys, username, record)
            _issue_password(new_keys, username, record, is_admin.get(username, False))
            result["reset"].append(username)
            continue
        record["userKey"] = new_keys.seal_user_key(username, user_key)
        data_keys = [base64.b64decode(k) for k in own_profile.get("dataKeys") or []]
        record["dataKeys"] = [new_keys.seal_data_key(username, i, k) for i, k in enumerate(data_keys)]
        if not data_keys:  # someone without timesheets: their unused key is replaced
            _add_data_key(new_keys, username, record)
        if issued:
            record["password"] = new_keys.seal_password(username, issued)
        if own_profile.get("tag"):
            result["renames"][own_profile["tag"]] = user_tag(new, username)
        result["kept"].append(username)

    for rel in (SETTINGS, REPORT, JOBS):
        data = old_vault.read(rel)
        if data is not None:
            new_vault.write(rel, data)
    new_vault.write_json(USERS, state)
    for folder in (TIMESHEETS, OUTPUT):
        for month, (rel, data) in old_vault.months(folder).items():
            (data_dir / rel).unlink()
            new_vault.write_json(new_vault.month_path(folder, month), data)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", required=True, help="checkout of the private data repo")
    parser.add_argument("--site-dir", required=True, help="checkout of the website repo (for site/data/users.json)")
    parser.add_argument("--admin", required=True, help="username of an office admin")
    parser.add_argument("--renames", required=True, help="where to write the old and new issue tags (JSON)")
    args = parser.parse_args()
    data_dir = Path(args.data_dir).resolve()
    try:
        new = parse_master_key(sys.stdin.read().strip())
    except ValueError as exc:
        sys.exit(f"The new MASTER_KEY (from standard input) isn't right: {exc}")
    password = os.environ.get("ADMIN_PASSWORD") or ""
    try:
        if Vault(data_dir, new).read(USERS) is not None:
            print("The files already use this key. Nothing to do.")
            return 0
    except ValueError:
        pass
    public = json.loads((Path(args.site_dir) / "site" / "data" / "users.json").read_text(encoding="utf-8"))["users"]
    try:
        result = recover(data_dir, public, normalize_username(args.admin), password, new)
    except ValueError as exc:
        sys.exit(str(exc))
    Path(args.renames).write_text(json.dumps(result["renames"], indent=1) + "\n", encoding="utf-8")
    print(f"Re-encrypted everything for the new key. Passwords kept for {len(result['kept'])} "
          f"{'person' if len(result['kept']) == 1 else 'people'}" + (f"; new issued password for: {', '.join(result['reset'])}" if result["reset"] else "") + ".")
    print("Now commit and push the data repo, rename the issues, and run Update website and journals.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
