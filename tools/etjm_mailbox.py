"""The office's Outlook.com mailbox, for "Forgot your password?".

Someone who has forgotten their password emails the office: the sign-in page opens a
ready-made email to the mailbox, naming their username. Each run of the data repo's Action
reads the mailbox (check), turns each request into a "Password reset request" issue naming a
hash of the username, and etjm_accounts answers those issues as it always has: if that
person has an email address, a new reset link goes to that address, sent from this same
mailbox (etjm_mail.send_outlook). The link only ever goes to the address the office has for
the person, so asking for someone else's reset gets the asker nothing, and the answer is the
same whether or not the username exists.

The mailbox is connected once (connect, from the "Connect the office mailbox" workflow), by
signing in to Microsoft with a code. Its sign-in (a refresh token) is kept in the data repo's
mailbox.json, sealed with a key from MASTER_KEY that admins' sign-ins don't carry, so only
the Actions can use it. It's renewed every RENEW_DAYS. If Microsoft stops accepting it, the
website stops offering resets by email and the Action's log says to connect it again.
"""

from __future__ import annotations

import datetime as dt
import re

import etjm_mail
from etjm_core import RESET_REQUEST_V1, RESET_TITLE, USERNAME_RE, normalize_username, seal, unseal, user_id
from etjm_vault import MAILBOX

RENEW_DAYS = 7  # how often the stored sign-in is swapped for a fresh one (Microsoft's last 90 days)
LOOK_BACK_DAYS = 3  # how far back each check reads
KEEP_HANDLED_DAYS = 10  # how long a request email is remembered as answered
MAX_REQUESTS_PER_RUN = 10
CLIENT_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_AAD = "state|mailbox|refreshToken"


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _iso(when: dt.datetime) -> str:
    return when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(text) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def masked(address: str) -> str:
    """"office.admin@example.com" -> "of…@example.com", for the Action's log."""
    local, _, domain = (address or "").partition("@")
    return f"{local[:2]}…@{domain}" if domain else "an unknown address"


# ------------------------------------------------------------- the stored sign-in

def load_mailbox(vault, keys) -> dict | None:
    """The connected mailbox, or None: {"account", "clientId", "refreshToken", "renewed",
    "lost" (when Microsoft stopped accepting the sign-in), "handled" ({email id: received})}."""
    try:
        data = vault.read_json(MAILBOX)
        if not isinstance(data, dict) or not data.get("refreshToken"):
            return None
        box = dict(data)
        box["refreshToken"] = unseal(keys.state, data["refreshToken"], _AAD).decode("utf-8")
    except ValueError:
        return None  # encrypted with another MASTER_KEY, or changed: connect the mailbox again
    box["handled"] = dict(box.get("handled") or {})
    return box


def save_mailbox(vault, keys, box: dict) -> None:
    vault.write_json(MAILBOX, {**box, "refreshToken": seal(keys.state, box["refreshToken"].encode("utf-8"), _AAD)})


def offers_resets(box: dict | None) -> bool:
    """Whether the website should offer "Forgot your password?" by email to the office."""
    return bool(box and box.get("account") and not box.get("lost"))


def apply_changes(box: dict, changes: dict, now: dt.datetime | None = None) -> dict:
    """The stored mailbox with `changes` (from check) made, forgetting old answered emails."""
    now = now or utc_now()
    box = {**box, "handled": {**(box.get("handled") or {}), **changes.get("handled", {})}}
    for key in ("refreshToken", "renewed"):
        if changes.get(key):
            box[key] = changes[key]
    if "lost" in changes:
        if changes["lost"]:
            box["lost"] = changes["lost"]
        else:
            box.pop("lost", None)
    cutoff = now - dt.timedelta(days=KEEP_HANDLED_DAYS)
    box["handled"] = {mid: when for mid, when in box["handled"].items() if (_parse(when) or now) >= cutoff}
    return box


# ---------------------------------------------------------- reading requests

def requested_username(subject: str, preview: str) -> str | None:
    """The username in a reset-request email, or None if it isn't one. The sign-in page writes
    "Engineer Timewriting password reset: alex.smith" as the subject and "Username: alex.smith"
    in the text; either is enough, in case one gets changed."""
    subject, preview = subject or "", preview or ""
    if not re.search(r"password\s*reset", f"{subject} {preview}", re.I):
        return None
    for pattern, text in ((r"password\s*reset\s*[:\-–—]\s*([A-Za-z0-9][A-Za-z0-9._-]{0,39})", subject),
                          (r"user\s*name\s*[:\-–—]?\s*([A-Za-z0-9][A-Za-z0-9._-]{0,39})", preview)):
        match = re.search(pattern, text, re.I)
        if match:
            username = normalize_username(match.group(1)).rstrip(".")
            if USERNAME_RE.match(username):
                return username
    return None


def request_issue(username: str) -> dict:
    """The "Password reset request" issue for a username, in the reset helper's format."""
    return {"title": RESET_TITLE,
            "body": f"{RESET_REQUEST_V1}\nA password-reset request emailed to the office.\n\n```etjm\n{user_id(username)}\n```\n"}


def check(vault, keys, open_issue, now: dt.datetime | None = None, http=etjm_mail) -> tuple[dict, list[str]]:
    """Read the mailbox for reset requests and pass each on with open_issue(issue). Returns the
    changes to store (see apply_changes) and lines for the log, which name nobody."""
    now = now or utc_now()
    box = load_mailbox(vault, keys)
    if not box:
        return {}, ["The office mailbox isn't connected, so password-reset requests by email aren't read (see mailbox/README.md)."]
    try:
        tokens = http.refresh(box["clientId"], box["refreshToken"])
    except etjm_mail.SignInLost as exc:
        return ({} if box.get("lost") else {"lost": _iso(now)}), [
            f"::warning::The office mailbox's sign-in has stopped working ({exc}). Run “Connect the office mailbox” in the "
            "data repo's Actions to connect it again. Until then, “Forgot your password?” tells people to ask the office."]
    except etjm_mail.MailboxError as exc:
        return {}, [f"::warning::Couldn't check the office mailbox this time ({exc}). It's tried again on the next run."]
    changes = {"lost": None} if box.get("lost") else {}
    renewed = _parse(box.get("renewed"))
    if tokens.get("refresh_token") and (not renewed or now - renewed >= dt.timedelta(days=RENEW_DAYS)):
        changes.update(refreshToken=tokens["refresh_token"], renewed=_iso(now))
    try:
        messages = http.recent_messages(tokens["access_token"], now - dt.timedelta(days=LOOK_BACK_DAYS))
    except etjm_mail.MailboxError as exc:
        return changes, [f"::warning::Couldn't read the office mailbox this time ({exc}). It's tried again on the next run."]
    asked: dict[str, list] = {}  # username -> [(email id, received)]
    for message in sorted(messages, key=lambda m: m.get("receivedDateTime") or ""):
        mid = message.get("id")
        if not mid or mid in box["handled"]:
            continue
        username = requested_username(message.get("subject"), message.get("bodyPreview"))
        if username:
            asked.setdefault(username, []).append((mid, message.get("receivedDateTime") or _iso(now)))
    handled = {}
    for username in list(asked)[:MAX_REQUESTS_PER_RUN]:
        open_issue(request_issue(username))
        handled.update(dict(asked[username]))
    if handled:
        changes["handled"] = handled
    notes = [f"Office mailbox: {len(handled)} new password-reset request email(s)."]
    if len(asked) > MAX_REQUESTS_PER_RUN:
        notes.append(f"::warning::More than {MAX_REQUESTS_PER_RUN} people asked for a reset at once; the rest are answered on the next run.")
    return changes, notes


# --------------------------------------------------------------- connecting

def connect(client_id: str, say=print, http=etjm_mail, now: dt.datetime | None = None) -> dict:
    """Sign the mailbox in (someone types a code at microsoft.com/devicelogin). Returns the
    mailbox to store with save_mailbox."""
    client_id = (client_id or "").strip().lower()
    if not CLIENT_ID_RE.match(client_id):
        raise ValueError("That isn't an Application (client) ID: it looks like 1a2b3c4d-1a2b-1a2b-1a2b-1a2b3c4d5e6f.")
    started = http.start_device_sign_in(client_id)
    say(started.get("message") or f"Open {started.get('verification_uri')} and enter the code {started.get('user_code')}.")
    say("Sign in there with the office's Outlook.com account (the one reset emails should come from), and accept. "
        "This waits until you have.")
    tokens = http.finish_device_sign_in(client_id, started)
    account = http.mailbox_address(tokens["access_token"])
    if "@" not in account:
        raise etjm_mail.MailboxError("Microsoft didn't say which mailbox signed in.")
    return {"v": 1, "account": account, "clientId": client_id, "refreshToken": tokens["refresh_token"],
            "renewed": _iso(now or utc_now()), "handled": {}}
