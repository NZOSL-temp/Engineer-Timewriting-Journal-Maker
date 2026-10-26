"""Sign-ins: who can sign in, their keys and passwords.

The private data repo keeps users.json (version 2), written only by the Actions:

    {"v": 2, "epoch": 3, "users": {"alex.smith": {
        "userKey":  sealed with the state key        random; unlocks the person's profile
        "dataKeys": [sealed, ...]                     encrypt their timesheets (newest last)
        "password": sealed with the password key      only while they use an issued password
        "wrap":     {"salt", "iterations", "key"}     the user key locked with their password
        "own": false, "seq": 0                        set once they choose their own password
        "reset": true                                 written by the Admin page to ask for a reset
        "recover": {...}                              an emailed password-reset link (see below)
    }}}

Every secret in it is encrypted under keys derived from MASTER_KEY, and the whole file is
encrypted again in the repo (etjm_vault.py), so it doesn't even show who has a sign-in. The
website's public users.json holds, per person, only the wrap and a profile encrypted with
their user key (see profile_for).

Forgotten passwords: the sign-in page's "Forgot your password?" asks the reset helper (see
relay/) to open a "Password reset request" issue naming a hash of the username. If that
person has an email address, start_recovery() makes a *new* user key and timesheet key for
them and emails a link holding a random secret. The secret unlocks only a copy of the new
user key, published next to their sign-in. With it, the reset page sets a new password
(an ETJMR1 request), and the new keys replace the old ones, which signs out anyone who had
the old password. Until then the old password keeps working, so nobody can lock someone out
by asking for resets. A link works once, for RECOVER_MINUTES.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

from etjm_core import (
    MIN_OWN_PASSWORD,
    PASSWORD_TITLE_RE,
    PASSWORD_V1,
    PBKDF2_ITERATIONS,
    RESET_REQUEST_V1,
    RESET_TITLE,
    b64,
    bundle_key,
    cipher_block,
    generate_password,
    password_store_key,
    recharge_rate,
    recover_key,
    seal,
    seal_profile,
    state_key,
    store_key,
    unb64,
    unb64url,
    unseal,
    unseal_profile,
    user_id,
    user_tag,
    wrap_user_key,
)

RECOVER_MINUTES = 60  # how long a password-reset link works
RESEND_MINUTES = 15  # at most one reset email per person this often

ABOUT = ("Sign-ins for the timewriting website, kept by the Actions. Every secret here is encrypted "
         "with keys derived from the MASTER_KEY secret. Use the website's Admin page to give someone "
         "a new password; don't edit this file by hand.")


def new_state() -> dict:
    return {"v": 2, "about": ABOUT, "epoch": 1, "users": {}}


def load_state(path: Path) -> tuple[dict, bool]:
    """A plain users.json file (the data repo's copy is encrypted: see load_state_data)."""
    return load_state_data(json.loads(path.read_text(encoding="utf-8")) if path.exists() else None)


def load_state_data(data) -> tuple[dict, bool]:
    """The sign-ins, and whether they had to be started again (old or missing file)."""
    if data is None:
        return new_state(), True
    if not isinstance(data, dict) or data.get("v") != 2:
        return new_state(), True  # version 1 kept passwords in plain text: everyone gets new ones
    data["about"] = ABOUT
    return data, False


class Keys:
    """Opens the sealed secrets in the state with keys derived from MASTER_KEY."""

    def __init__(self, master: bytes):
        self.master = master
        self.state = state_key(master)
        self.passwords = password_store_key(master)

    def user_key(self, username: str, record: dict) -> bytes:
        return unseal(self.state, record["userKey"], f"state|userKey|{username}")

    def data_keys(self, username: str, record: dict) -> list[bytes]:
        return [unseal(self.state, token, f"state|dataKey|{username}|{i}") for i, token in enumerate(record["dataKeys"])]

    def password(self, username: str, record: dict) -> str | None:
        token = record.get("password")
        return unseal(self.passwords, token, f"password|{username}").decode("utf-8") if token else None

    def seal_user_key(self, username: str, key: bytes) -> str:
        return seal(self.state, key, f"state|userKey|{username}")

    def seal_data_key(self, username: str, index: int, key: bytes) -> str:
        return seal(self.state, key, f"state|dataKey|{username}|{index}")

    def seal_password(self, username: str, password: str) -> str:
        return seal(self.passwords, password.encode("utf-8"), f"password|{username}")

    def recover_user_key(self, username: str, recover: dict) -> bytes:
        return unseal(self.state, recover["userKey"], f"state|recoverUserKey|{username}|{recover['id']}")

    def recover_data_key(self, username: str, recover: dict) -> bytes:
        return unseal(self.state, recover["dataKey"], f"state|recoverDataKey|{username}|{recover['id']}")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(when: dt.datetime) -> str:
    return when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(text: str) -> dt.datetime:
    return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def _recovery_open(record: dict, now: dt.datetime) -> bool:
    recover = record.get("recover")
    try:
        return bool(recover) and _parse_iso(recover["expires"]) > now
    except (KeyError, TypeError, ValueError):
        return False


def _issue_password(keys: Keys, username: str, record: dict, admin: bool) -> None:
    """A new random password (longer for admins), and a new user key so old sign-ins stop working."""
    password = generate_password(4 if admin else 3)
    user_key = os.urandom(32)
    record["userKey"] = keys.seal_user_key(username, user_key)
    record["password"] = keys.seal_password(username, password)
    record["wrap"] = wrap_user_key(user_key, password, username)
    record["own"] = False
    record.pop("reset", None)
    record.pop("recover", None)  # an emailed reset link stops working too


def _add_data_key(keys: Keys, username: str, record: dict) -> None:
    record.setdefault("dataKeys", [])
    record["dataKeys"].append(keys.seal_data_key(username, len(record["dataKeys"]), os.urandom(32)))


def sync_people(state: dict, keys: Keys, people: list[dict]) -> dict:
    """Give new people a sign-in, reset those the Admin page asked to, and remove leavers.

    Removing or resetting anyone moves the epoch on, which changes the job-list key, so a
    sign-in that's been taken away can't read the job list (or the save token) any more.
    """
    users = state["users"]
    report = {"created": [], "reset": [], "removed": []}
    wanted = {p["username"]: p for p in people}
    for username, person in wanted.items():
        record = users.get(username)
        if record is None:
            record = users[username] = {"dataKeys": [], "seq": 0}
            _add_data_key(keys, username, record)
            _issue_password(keys, username, record, person["admin"])
            report["created"].append(username)
        elif record.get("reset"):
            _add_data_key(keys, username, record)
            _issue_password(keys, username, record, person["admin"])
            report["reset"].append(username)
    for username in sorted(set(users) - set(wanted)):
        del users[username]
        report["removed"].append(username)
    if report["reset"] or report["removed"]:
        state["epoch"] = int(state.get("epoch", 1)) + 1
    return report


def _valid_wrap(wrap) -> bool:
    try:
        ok = (int(wrap["iterations"]) >= PBKDF2_ITERATIONS
              and len(unb64(wrap["salt"])) == 16
              and re.fullmatch(r"[A-Za-z0-9_-]{16}\.[A-Za-z0-9_-]{40,}", wrap["key"]) is not None)
        if ok:
            unb64url(wrap["key"].split(".")[1])
        return ok
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def _finish_recovery(state: dict, keys: Keys, username: str, record: dict, token: str, now: dt.datetime) -> bool:
    """An ETJMR1 request from the reset page: sealed with the new user key from the emailed
    link, so only someone with that link could have made it."""
    recover = record.get("recover")
    if not _recovery_open(record, now):
        return False
    try:
        new_user_key = keys.recover_user_key(username, recover)
        request = json.loads(unseal(new_user_key, token, f"etjm-recover-pw|{username}|{recover['id']}"))
        if request.get("id") != recover["id"] or not _valid_wrap(request.get("wrap")):
            return False
        data_key = keys.recover_data_key(username, recover)
    except (ValueError, KeyError, TypeError, AttributeError):
        return False
    wrap = request["wrap"]
    record["userKey"] = keys.seal_user_key(username, new_user_key)
    record.setdefault("dataKeys", [])
    record["dataKeys"].append(keys.seal_data_key(username, len(record["dataKeys"]), data_key))
    record["wrap"] = {"salt": wrap["salt"], "iterations": int(wrap["iterations"]), "key": wrap["key"]}
    record["own"] = True
    record.pop("password", None)
    record.pop("recover", None)
    # New keys for this person: the old password's key, and the job-list key it unlocked, stop working.
    state["epoch"] = int(state.get("epoch", 1)) + 1
    return True


def apply_password_changes(state: dict, keys: Keys, issues: list[dict], now: dt.datetime | None = None) -> tuple[list[int], list[str]]:
    """Apply "Password change" requests. A change made while signed in (ETJMP1) is sealed with
    the person's user key, and one made from an emailed reset link (ETJMR1) with the link's
    new user key, so only they could have made it. Returns the issue numbers to close, and
    notes for the log (which name nobody)."""
    now = now or utc_now()
    tags = {user_tag(keys.master, u): u for u in state["users"]}
    done, notes = [], []
    for issue in sorted(issues, key=lambda i: i.get("number") or 0):
        match = PASSWORD_TITLE_RE.match((issue.get("title") or "").strip())
        if not match or "pull_request" in issue or issue.get("state") != "open":
            continue
        number = issue.get("number")
        done.append(number)
        username = tags.get(match.group(1))
        token = cipher_block(issue.get("body") or "", PASSWORD_V1) or ""
        if not username or not token.startswith(("ETJMP1.", "ETJMR1.")):
            notes.append(f"Password change #{number} isn't for anyone who can sign in; ignored.")
            continue
        record = state["users"][username]
        if token.startswith("ETJMR1."):
            ok = _finish_recovery(state, keys, username, record, token[len("ETJMR1."):], now)
            notes.append(f"Password reset #{number} applied." if ok
                         else f"Password reset #{number} couldn't be verified, or its link had expired or been used; ignored.")
            continue
        try:
            request = json.loads(unseal(keys.user_key(username, record), token[len("ETJMP1."):], f"etjm-pw|{username}"))
            seq, wrap = int(request["seq"]), request["wrap"]
            ok = seq > int(record.get("seq", 0)) and _valid_wrap(wrap)
        except (ValueError, KeyError, TypeError):
            ok = False
        if ok:
            record["wrap"] = {"salt": wrap["salt"], "iterations": int(wrap["iterations"]), "key": wrap["key"]}
            record["own"] = True
            record["seq"] = seq
            record.pop("password", None)
            notes.append(f"Password change #{number} applied.")
        else:
            notes.append(f"Password change #{number} couldn't be verified or was out of date; ignored.")
    return done, notes


def start_recovery(keys: Keys, username: str, record: dict, now: dt.datetime) -> tuple[str, bytes]:
    """A new password-reset link for someone. Returns its id and secret (which goes only in
    the email). Any earlier link stops working."""
    rid = os.urandom(8).hex()
    secret = os.urandom(32)
    new_user_key, new_data_key = os.urandom(32), os.urandom(32)
    record["recover"] = {
        "id": rid,
        "userKey": seal(keys.state, new_user_key, f"state|recoverUserKey|{username}|{rid}"),
        "dataKey": seal(keys.state, new_data_key, f"state|recoverDataKey|{username}|{rid}"),
        "wrap": seal(recover_key(secret), new_user_key, f"etjm-recover|{username}|{rid}"),
        "sent": _iso(now),
        "expires": _iso(now + dt.timedelta(minutes=RECOVER_MINUTES)),
    }
    return rid, secret


def handle_reset_requests(state: dict, keys: Keys, issues: list[dict], everyone: list[dict],
                          now: dt.datetime | None = None, can_send: bool = True) -> tuple[list[int], list[dict], list[str]]:
    """Answer "Password reset request" issues, which name a hash of a username. Someone with
    an email address gets a new reset link, at most every RESEND_MINUTES; the answer to the
    person asking is the same whatever happens, so it gives nothing away. Returns the issue
    numbers to close, the emails to send, and notes for the log (which name nobody)."""
    now = now or utc_now()
    by_uid = {user_id(u): u for u in state["users"]}
    known = {p["username"]: p for p in everyone}
    done, outbox, notes = [], [], []
    for issue in sorted(issues, key=lambda i: i.get("number") or 0):
        if ((issue.get("title") or "").strip() != RESET_TITLE or "pull_request" in issue
                or issue.get("state") != "open" or RESET_REQUEST_V1 not in (issue.get("body") or "")):
            continue
        number = issue.get("number")
        done.append(number)
        username = by_uid.get(cipher_block(issue.get("body") or "", RESET_REQUEST_V1) or "")
        person = known.get(username or "")
        if not person or not person.get("email"):
            notes.append(f"Password reset request #{number}: not a sign-in with an email address; ignored.")
            continue
        if not can_send:
            notes.append(f"Password reset request #{number}: reset emails aren't set up (RELAY_URL and RELAY_SECRET); ignored.")
            continue
        record = state["users"][username]
        recover = record.get("recover")
        if _recovery_open(record, now) and now - _parse_iso(recover["sent"]) < dt.timedelta(minutes=RESEND_MINUTES):
            notes.append(f"Password reset request #{number}: a link was sent in the last {RESEND_MINUTES} minutes; ignored.")
            continue
        rid, secret = start_recovery(keys, username, record, now)
        outbox = [m for m in outbox if m["username"] != username]
        outbox.append({"username": username, "name": person["name"], "email": person["email"], "id": rid,
                       "secret": secret, "sent": int(now.timestamp()), "expires": record["recover"]["expires"]})
        notes.append(f"Password reset request #{number}: reset link made.")
    return done, outbox, notes


def expire_recoveries(state: dict, now: dt.datetime | None = None) -> int:
    """Remove reset links that have run out. Returns how many."""
    now = now or utc_now()
    expired = 0
    for record in state["users"].values():
        if record.get("recover") and not _recovery_open(record, now):
            record.pop("recover")
            expired += 1
    return expired


def profile_for(person: dict, settings: dict, keys: Keys, epoch: int, record: dict, admin_token: str | None) -> dict:
    """What only this person's sign-in unlocks."""
    username = person["username"]
    profile = {
        "v": 3,
        "name": person["name"],
        "username": username,
        "timesheets": person["timesheets"],
        "admin": person["admin"],
        "email": person.get("email"),
        "rate": None,
        "epoch": epoch,
        "bundleKey": b64(bundle_key(keys.master, epoch)),
        "tag": user_tag(keys.master, username),
    }
    if person["timesheets"]:
        rate = recharge_rate(settings, person["name"])
        profile["rate"] = None if rate is None else (int(rate) if rate == rate.to_integral_value() else float(rate))
        profile["dataKeys"] = [b64(k) for k in keys.data_keys(username, record)]
    if person["admin"]:
        profile["passwordKey"] = b64(keys.passwords)
        profile["storeKey"] = b64(store_key(keys.master))
        if admin_token:
            profile["adminToken"] = admin_token
    return profile


def recovery_profile(profile: dict, recover: dict) -> dict:
    """What an emailed reset link unlocks: just enough to send the new password (the job-list
    key, for the save token, and the person's tag). Nothing else until the reset is done."""
    return {"v": 3, "username": profile["username"], "name": profile["name"], "tag": profile["tag"],
            "epoch": profile["epoch"], "bundleKey": profile["bundleKey"], "recover": recover["id"]}


def _keep_or_seal(old_token, key: bytes, value: dict, aad: str) -> str:
    """The old sealed copy if it still holds the same thing (so unchanged files stay unchanged)."""
    from etjm_core import profile_bytes

    try:
        if isinstance(old_token, str) and json.loads(unseal(key, old_token, aad).decode("utf-8")) == value:
            return old_token
    except (ValueError, TypeError):
        pass
    return seal(key, profile_bytes(value), aad)


def public_users(state: dict, keys: Keys, profiles: dict, existing: dict, extra: dict | None = None,
                 now: dt.datetime | None = None) -> dict:
    """The website's users.json. Unchanged entries are kept byte for byte. `extra` adds
    top-level settings for the sign-in page (such as where "Forgot your password?" goes)."""
    now = now or utc_now()
    out = {}
    for username, record in sorted(state["users"].items()):
        if username not in profiles:
            continue
        uid = user_id(username)
        user_key = keys.user_key(username, record)
        old = existing.get(uid) or {}
        entry = {"salt": record["wrap"]["salt"], "iterations": record["wrap"]["iterations"], "key": record["wrap"]["key"]}
        try:
            same = isinstance(old.get("profile"), str) and unseal_profile(user_key, old["profile"], username) == profiles[username]
        except (KeyError, TypeError, ValueError):
            same = False
        entry["profile"] = old["profile"] if same else seal_profile(user_key, profiles[username], username)
        recover = record.get("recover")
        if recover and _recovery_open(record, now):
            rid = recover["id"]
            old_recover = old.get("recover") if isinstance(old.get("recover"), dict) and old["recover"].get("id") == rid else {}
            entry["recover"] = {
                "id": rid,
                "expires": recover["expires"],
                "key": recover["wrap"],
                "profile": _keep_or_seal(old_recover.get("profile"), keys.recover_user_key(username, recover),
                                         recovery_profile(profiles[username], recover), f"etjm-recover-profile|{username}|{rid}"),
            }
        out[uid] = entry
    return {"v": 3, **(extra or {}), "users": out}


def check_own_password(password: str, username: str) -> str | None:
    """The website applies the same rules; kept here so the tests pin them down."""
    from etjm_core import normalize_password

    plain = normalize_password(password)
    if len(plain) < MIN_OWN_PASSWORD:
        return f"Use at least {MIN_OWN_PASSWORD} characters."
    if len(set(plain)) < 5:
        return "Use a mix of different characters."
    if username.replace(".", "").upper() in plain:
        return "Don't include your username."
    return None
