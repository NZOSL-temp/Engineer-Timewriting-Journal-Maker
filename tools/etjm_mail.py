"""Password-reset emails, sent through the reset helper (relay/Code.gs, a Google Apps Script
web app owned by the office).

The helper sends only messages signed with RELAY_SECRET (HMAC-SHA256 over a timestamp and the
message), and refuses old or repeated ones, so nobody else can use it to send email.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import time
import urllib.parse
import urllib.request

from etjm_core import b64url


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
