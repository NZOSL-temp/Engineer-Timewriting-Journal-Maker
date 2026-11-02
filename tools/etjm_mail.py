"""Password-reset emails.

They're sent from the office's Outlook.com mailbox through Microsoft Graph, once the mailbox is
connected (see etjm_mailbox.py and mailbox/README.md), or else through the older reset helper
(relay/Code.gs, a Google Apps Script web app owned by the office). The helper sends only
messages signed with RELAY_SECRET (HMAC-SHA256 over a timestamp and the message), and refuses
old or repeated ones, so nobody else can use it to send email.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import html
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from etjm_core import b64url

# ------------------------------------------------------- Microsoft (Outlook.com)
#
# The office's app registration is for personal Microsoft accounts, signed in once with the
# device-code flow (a code typed at microsoft.com/devicelogin), which gives a refresh token.

LOGIN = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = "offline_access User.Read Mail.Read Mail.Send"


def _base(variable: str, default: str) -> str:
    """Microsoft's address, which the tests can point at a stand-in on this computer (only)."""
    override = os.environ.get(variable, "")
    return override.rstrip("/") if override.startswith("http://127.0.0.1:") else default


class MailboxError(RuntimeError):
    """Microsoft refused or couldn't be reached. The message names nobody."""


class SignInLost(MailboxError):
    """The mailbox's sign-in no longer works (expired or taken back): connect it again."""


class _Refused(MailboxError):
    def __init__(self, status: int, code: str | None):
        super().__init__(f"Microsoft answered {status} ({code or 'no reason given'})")
        self.status, self.code = status, code


def _http(request: urllib.request.Request, timeout: int = 30) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8")
            return json.loads(text) if text.strip() else {}
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read().decode("utf-8") or "{}")
        except ValueError:
            error = {}
        code = error.get("error") if isinstance(error, dict) else None
        if isinstance(code, dict):  # Graph: {"error": {"code", "message"}}
            code = code.get("code")
        raise _Refused(exc.code, code if isinstance(code, str) else None) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MailboxError(f"couldn't reach Microsoft ({exc})") from None


def _login(endpoint: str, fields: dict) -> dict:
    """POST to the sign-in service. Its OAuth errors come back as {"error": code} rather than raising."""
    request = urllib.request.Request(f"{_base('ETJM_MS_LOGIN', LOGIN)}/{endpoint}", data=urllib.parse.urlencode(fields).encode("ascii"),
                                     method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        return _http(request)
    except _Refused as exc:
        if exc.status in (400, 401) and exc.code:
            return {"error": exc.code}
        raise


def _graph(method: str, path: str, token: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(f"{_base('ETJM_MS_GRAPH', GRAPH)}{path}", data=json.dumps(body).encode("utf-8") if body is not None else None,
                                     method=method, headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                                                             **({"Content-Type": "application/json"} if body is not None else {})})
    return _http(request)


def start_device_sign_in(client_id: str) -> dict:
    """Ask for a sign-in code: {"user_code", "device_code", "verification_uri", "interval", "expires_in", "message"}."""
    answer = _login("devicecode", {"client_id": client_id, "scope": SCOPES})
    if answer.get("error") or not answer.get("device_code"):
        raise MailboxError(f"Microsoft wouldn't start a sign-in ({answer.get('error') or 'no code given'}). "
                           "Check the client ID, and that the app registration is for personal Microsoft accounts "
                           "with public client flows allowed.")
    return answer


def finish_device_sign_in(client_id: str, started: dict, sleep=time.sleep, clock=time.monotonic) -> dict:
    """Wait for the code to be entered and the sign-in accepted. Returns the tokens."""
    interval = int(started.get("interval") or 5)
    deadline = clock() + int(started.get("expires_in") or 900)
    while clock() < deadline:
        sleep(interval)
        answer = _login("token", {"client_id": client_id, "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                  "device_code": started["device_code"]})
        error = answer.get("error")
        if not error and answer.get("refresh_token"):
            return answer
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        raise MailboxError({"expired_token": "The code ran out before the sign-in was finished. Run it again.",
                            "authorization_declined": "The sign-in was declined.",
                            "access_denied": "The sign-in was declined."}.get(error, f"The sign-in didn't work ({error or 'no refresh token'})."))
    raise MailboxError("The code ran out before the sign-in was finished. Run it again.")


def refresh(client_id: str, refresh_token: str) -> dict:
    """A new access token (and refresh token) for the mailbox."""
    answer = _login("token", {"client_id": client_id, "grant_type": "refresh_token", "refresh_token": refresh_token, "scope": SCOPES})
    if answer.get("error") in ("invalid_grant", "interaction_required", "invalid_client", "unauthorized_client"):
        raise SignInLost(f"Microsoft no longer accepts the mailbox's sign-in ({answer['error']})")
    if not answer.get("access_token"):
        raise MailboxError(f"Microsoft gave no access token ({answer.get('error') or 'no reason given'})")
    return answer


def mailbox_address(access_token: str) -> str:
    """The signed-in mailbox's email address."""
    me = _graph("GET", "/me?$select=mail,userPrincipalName", access_token)
    return (me.get("mail") or me.get("userPrincipalName") or "").strip().lower()


def recent_messages(access_token: str, since: dt.datetime, folders=("inbox", "junkemail"), top: int = 50) -> list[dict]:
    """Messages received since `since` in the inbox and junk folder: [{"id", "subject", "bodyPreview", "receivedDateTime"}]."""
    when = since.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = urllib.parse.urlencode({"$select": "id,subject,bodyPreview,receivedDateTime", "$top": str(top),
                                    "$filter": f"receivedDateTime ge {when}", "$orderby": "receivedDateTime desc"})
    found = []
    for folder in folders:
        found += _graph("GET", f"/me/mailFolders/{folder}/messages?{query}", access_token).get("value") or []
    return found


def send_outlook(access_token: str, to: str, subject: str, html_body: str) -> None:
    """Send one email from the mailbox (kept in its Sent Items)."""
    if not to or any(c in to for c in ",;\r\n"):
        raise ValueError("one email address at a time")
    _graph("POST", "/me/sendMail", access_token, {"message": {
        "subject": subject, "body": {"contentType": "HTML", "content": html_body},
        "toRecipients": [{"emailAddress": {"address": to}}]}, "saveToSentItems": True})

# ------------------------------------------------------------- the reset helper


def signature(secret: str, ts: str, to: str, subject: str, text: str, html_body: str) -> str:
    message = "\n".join([ts, to, subject, text, html_body])
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def send(relay_url: str, secret: str, to: str, subject: str, text: str, html_body: str, timeout: int = 30) -> None:
    """Ask the reset helper to send one email. Raises if it didn't."""
    if not to or any(c in to for c in ",;\r\n"):
        raise ValueError("one email address at a time")
    ts = str(int(time.time() * 1000))
    body = json.dumps({"action": "send", "ts": ts, "to": to, "subject": subject, "text": text, "html": html_body,
                       "sig": signature(secret, ts, to, subject, text, html_body)}).encode("utf-8")
    # text/plain keeps it a "simple" request, which is what Apps Script web apps expect.
    request = urllib.request.Request(relay_url, data=body, method="POST", headers={"Content-Type": "text/plain;charset=utf-8"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        answer = json.loads(response.read().decode("utf-8") or "{}")
    if not answer.get("ok"):
        raise RuntimeError(f"the reset helper didn't send it ({answer.get('error') or 'no reason given'})")


def reset_link(site_url: str, username: str, rid: str, secret: bytes, sent: int) -> str:
    """The link in the email. The secret is after the #, so it never reaches a web server.
    `sent` (seconds) lets the reset page wait for the website only while the link is new."""
    fragment = urllib.parse.urlencode({"u": username, "id": rid, "k": b64url(secret), "t": int(sent)})
    return f"{site_url.rstrip('/')}/reset.html#{fragment}"


def reset_email(name: str, username: str, link: str, minutes: int) -> tuple[str, str, str]:
    """Subject, plain text and HTML for a password-reset email."""
    first = name.split()[0] if name.split() else name
    subject = "Reset your Engineer Timewriting password"
    text = (f"Hi {first},\n\n"
            f"Someone asked to reset the password for your Engineer Timewriting sign-in ({username}).\n\n"
            f"To choose a new password, open this link. It works once, within the next {minutes} minutes:\n"
            f"{link}\n\n"
            "If that wasn't you, you can ignore this email: your password hasn't changed.\n\n"
            "Engineer Timewriting\n")
    e = html.escape
    html_body = (
        '<div style="font-family:Segoe UI,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.5;color:#1f2937;max-width:520px">'
        f"<p>Hi {e(first)},</p>"
        f"<p>Someone asked to reset the password for your Engineer Timewriting sign-in (<b>{e(username)}</b>).</p>"
        f'<p><a href="{e(link)}" style="display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;'
        'padding:10px 18px;border-radius:8px;font-weight:600">Choose a new password</a></p>'
        f"<p style=\"color:#6b7280;font-size:13px\">The link works once, within the next {minutes} minutes. "
        "If the button doesn't work, copy this into your browser:<br>"
        f'<span style="word-break:break-all">{e(link)}</span></p>'
        "<p>If that wasn't you, you can ignore this email: your password hasn't changed.</p>"
        '<p style="color:#6b7280">Engineer Timewriting</p></div>')
    return subject, text, html_body
