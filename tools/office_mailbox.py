#!/usr/bin/env python3
"""The office's Outlook.com mailbox, for "Forgot your password?" (see etjm_mailbox.py and
mailbox/README.md). Run by the data repo's Actions:

    python .website/tools/office_mailbox.py --data-dir . check --push
        reads the mailbox for password-reset requests and passes each on as a "Password reset
        request" issue, which the next step (publish_website.py) answers; also keeps the
        mailbox's sign-in fresh. Needs MASTER_KEY, GITHUB_TOKEN (issues: write) and
        GITHUB_REPOSITORY. Does nothing if no mailbox is connected.

    python .website/tools/office_mailbox.py --data-dir . connect --client-id ID --push
        signs the mailbox in (the "Connect the office mailbox" workflow): it shows a code to
        type at microsoft.com/devicelogin, and waits.

    python .website/tools/office_mailbox.py --data-dir . disconnect --push
        forgets the mailbox; "Forgot your password?" then says to ask the office.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from etjm_accounts import Keys  # noqa: E402
from etjm_core import parse_master_key  # noqa: E402
from etjm_github import api, commit_and_push  # noqa: E402
from etjm_mail import MailboxError  # noqa: E402
from etjm_mailbox import apply_changes, check, connect, load_mailbox, masked, save_mailbox, utc_now  # noqa: E402
from etjm_vault import MAILBOX, Vault  # noqa: E402


def say(text: str) -> None:
    print(text, flush=True)  # the sign-in code has to show in the log straight away


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", default=".", help="checkout of the data repo")
    parser.add_argument("--push", action="store_true", help="commit and push mailbox.json")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="read the mailbox for reset requests")
    connecting = commands.add_parser("connect", help="sign the mailbox in")
    connecting.add_argument("--client-id", required=True, help="the app registration's Application (client) ID")
    commands.add_parser("disconnect", help="forget the mailbox")
    args = parser.parse_args()
    try:
        keys = Keys(parse_master_key(os.environ.get("MASTER_KEY", "")))
    except ValueError as exc:
        sys.exit(f"The MASTER_KEY secret is missing or wrong: {exc}")
    data_dir = Path(args.data_dir).resolve()
    vault = Vault(data_dir, keys.master)

    def store(message: str, change) -> None:
        if args.push:
            commit_and_push(data_dir, [MAILBOX], message, change)
        else:
            change()

    if args.command == "check":
        repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
        if load_mailbox(vault, keys) and not (repo and token):
            sys.exit("GITHUB_TOKEN and GITHUB_REPOSITORY are needed to pass requests on.")
        now = utc_now()
        changes, notes = check(vault, keys, lambda issue: api("POST", f"/repos/{repo}/issues", token, issue), now)
        for note in notes:
            say(note)
        if changes:
            def change():
                box = load_mailbox(vault, keys)  # as it is now on GitHub
                if box:
                    save_mailbox(vault, keys, apply_changes(box, changes, now))
            store("Office mailbox: note password-reset requests, keep its sign-in fresh", change)
        return 0

    if args.command == "connect":
        try:
            box = connect(args.client_id, say=say)
        except (ValueError, MailboxError) as exc:
            sys.exit(str(exc))
        store("Connect the office mailbox (for password resets)", lambda: save_mailbox(vault, keys, box))
        say(f"Connected. Password-reset emails will come from {masked(box['account'])}, and requests sent there are read "
            "each time the “Update website and journals” workflow runs.")
        return 0

    store("Disconnect the office mailbox", lambda: (data_dir / MAILBOX).unlink(missing_ok=True))
    say("Disconnected. “Forgot your password?” now says to ask the office.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
