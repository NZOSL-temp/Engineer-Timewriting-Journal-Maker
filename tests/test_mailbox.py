"""Tests for "Forgot your password?" through the office's Outlook.com mailbox: reading reset
requests from it, keeping its sign-in, and sending the reset emails from it. Run with:
python -m unittest discover -s tests

Microsoft is played by a small server on this computer. Everything uses made-up names,
addresses and keys.
"""

import base64
import datetime as dt
import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import etjm_mail  # noqa: E402
from etjm_accounts import Keys  # noqa: E402
from etjm_core import RESET_REQUEST_V1, RESET_TITLE, cipher_block, unb64url, user_id  # noqa: E402
from etjm_mailbox import (  # noqa: E402
    MAX_REQUESTS_PER_RUN,
    apply_changes,
    check,
    connect,
    load_mailbox,
    offers_resets,
    requested_username,
    save_mailbox,
)
from etjm_vault import MAILBOX, Vault  # noqa: E402

MASTER = bytes(range(32))
NOW = dt.datetime(2026, 11, 3, 9, 0, tzinfo=dt.timezone.utc)
CLIENT = "1a2b3c4d-1a2b-1a2b-1a2b-1a2b3c4d5e6f"


def box(**changes):
    return {"v": 1, "account": "office@example.com", "clientId": CLIENT, "refreshToken": "refresh-1",
            "renewed": "2026-11-01T00:00:00Z", "handled": {}, **changes}


def message(mid, subject, preview="", hours_ago=1):
    return {"id": mid, "subject": subject, "bodyPreview": preview,
            "receivedDateTime": (NOW - dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")}


class FakeMicrosoft:
    """Stands in for etjm_mail's Microsoft calls in check() and connect()."""

    def __init__(self, messages=(), lost=False, down=False):
        self.messages, self.lost, self.down = list(messages), lost, down
        self.refreshed = []

    def refresh(self, client_id, refresh_token):
        if self.lost:
            raise etjm_mail.SignInLost("Microsoft no longer accepts the mailbox's sign-in (invalid_grant)")
        if self.down:
            raise etjm_mail.MailboxError("couldn't reach Microsoft")
        self.refreshed.append((client_id, refresh_token))
        return {"access_token": "access", "refresh_token": f"fresh-{len(self.refreshed)}"}

    def recent_messages(self, access_token, since):
        self.since = since
        return list(self.messages)

    def start_device_sign_in(self, client_id):
        return {"device_code": "dev", "user_code": "ABCD1234", "verification_uri": "https://microsoft.com/devicelogin",
                "message": "To sign in, use a web browser to open the page https://microsoft.com/devicelogin and enter the code ABCD1234."}

    def finish_device_sign_in(self, client_id, started):
        return {"access_token": "access", "refresh_token": "first-refresh"}

    def mailbox_address(self, access_token):
        return "office@example.com"


class RequestTests(unittest.TestCase):
    def test_the_username_comes_from_the_subject_or_the_text(self):
        self.assertEqual(requested_username("Engineer Timewriting password reset: ann.example", ""), "ann.example")
        self.assertEqual(requested_username("RE: Engineer Timewriting password reset: Ann.Example", ""), "ann.example")
        self.assertEqual(requested_username("Engineer Timewriting password reset: ann.example.", ""), "ann.example")
        self.assertEqual(requested_username("Password reset please", "Hi, can I have a link?\r\nUsername: Ben.Sample\r\nThanks"), "ben.sample")
        self.assertEqual(requested_username("Engineer Timewriting password reset", "Username ben.sample"), "ben.sample")

    def test_other_emails_are_not_requests(self):
        self.assertIsNone(requested_username("Lunch on Friday?", "Username: ann.example"))
        self.assertIsNone(requested_username("Password reset", "I forgot it"))
        self.assertIsNone(requested_username("Password reset: ../../etc", ""))
        self.assertIsNone(requested_username("", ""))

    def test_check_passes_each_person_on_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault, keys = Vault(Path(tmp), MASTER), Keys(MASTER)
            save_mailbox(vault, keys, box())
            microsoft = FakeMicrosoft([
                message("m1", "Engineer Timewriting password reset: ann.example", hours_ago=3),
                message("m2", "Newsletter", "Username: someone"),
                message("m3", "Password reset", "Username: ben.sample", hours_ago=2),
                message("m4", "Re: Engineer Timewriting password reset: ann.example", hours_ago=1),
            ])
            opened = []
            changes, notes = check(vault, keys, opened.append, NOW, http=microsoft)
            self.assertEqual([cipher_block(i["body"], RESET_REQUEST_V1) for i in opened], [user_id("ann.example"), user_id("ben.sample")])
            self.assertTrue(all(i["title"] == RESET_TITLE for i in opened))
            self.assertEqual(sorted(changes["handled"]), ["m1", "m3", "m4"])
            self.assertEqual(notes, ["Office mailbox: 3 new password-reset request email(s)."])
            self.assertEqual(microsoft.since, NOW - dt.timedelta(days=3))
            self.assertNotIn("refreshToken", changes, "renewed two days ago: no need for a new sign-in yet")
            save_mailbox(vault, keys, apply_changes(load_mailbox(vault, keys), changes, NOW))
            opened.clear()
            changes, notes = check(vault, keys, opened.append, NOW, http=microsoft)
            self.assertEqual((opened, changes), ([], {}))

    def test_at_most_ten_people_a_run_and_the_rest_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault, keys = Vault(Path(tmp), MASTER), Keys(MASTER)
            save_mailbox(vault, keys, box())
            microsoft = FakeMicrosoft([message(f"m{i}", f"Engineer Timewriting password reset: person.{i}") for i in range(12)])
            opened = []
            changes, notes = check(vault, keys, opened.append, NOW, http=microsoft)
            self.assertEqual(len(opened), MAX_REQUESTS_PER_RUN)
            self.assertEqual(len(changes["handled"]), MAX_REQUESTS_PER_RUN)
            self.assertIn("answered on the next run", notes[-1])
            save_mailbox(vault, keys, apply_changes(load_mailbox(vault, keys), changes, NOW))
            opened.clear()
            check(vault, keys, opened.append, NOW, http=microsoft)
            self.assertEqual(len(opened), 2)

    def test_not_connected(self):
        with tempfile.TemporaryDirectory() as tmp:
            changes, notes = check(Vault(Path(tmp), MASTER), Keys(MASTER), self.fail, NOW, http=FakeMicrosoft())
            self.assertEqual(changes, {})
            self.assertIn("isn't connected", notes[0])


class SignInTests(unittest.TestCase):
    def test_kept_sealed_where_admins_cannot_read_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault, keys = Vault(Path(tmp), MASTER), Keys(MASTER)
            save_mailbox(vault, keys, box(refreshToken="the-real-refresh-token"))
            self.assertEqual(load_mailbox(vault, keys)["refreshToken"], "the-real-refresh-token")
            stored = vault.read_json(MAILBOX)  # what an admin's store key opens
            self.assertNotIn("the-real-refresh-token", json.dumps(stored))
            self.assertEqual(stored["account"], "office@example.com")
            self.assertIsNone(load_mailbox(vault, Keys(bytes(range(1, 33)))), "another MASTER_KEY opens nothing")
            self.assertIsNone(load_mailbox(Vault(Path(tmp), bytes(range(1, 33))), Keys(bytes(range(1, 33)))))

    def test_renewed_weekly(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault, keys = Vault(Path(tmp), MASTER), Keys(MASTER)
            save_mailbox(vault, keys, box(renewed="2026-10-26T09:00:00Z"))
            changes, _ = check(vault, keys, lambda issue: None, NOW, http=FakeMicrosoft())
            self.assertEqual((changes["refreshToken"], changes["renewed"]), ("fresh-1", "2026-11-03T09:00:00Z"))
            saved = apply_changes(load_mailbox(vault, keys), changes, NOW)
            self.assertEqual(saved["refreshToken"], "fresh-1")

    def test_a_lost_sign_in_stops_the_offer_until_it_works_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault, keys = Vault(Path(tmp), MASTER), Keys(MASTER)
            save_mailbox(vault, keys, box())
            self.assertTrue(offers_resets(load_mailbox(vault, keys)))
            changes, notes = check(vault, keys, self.fail, NOW, http=FakeMicrosoft(lost=True))
            self.assertIn("::warning::The office mailbox's sign-in has stopped working", notes[0])
            self.assertIn("Connect the office mailbox", notes[0])
            save_mailbox(vault, keys, apply_changes(load_mailbox(vault, keys), changes, NOW))
            self.assertFalse(offers_resets(load_mailbox(vault, keys)))
            self.assertEqual(check(vault, keys, self.fail, NOW, http=FakeMicrosoft(lost=True))[0], {}, "already noted")
            changes, _ = check(vault, keys, self.fail, NOW, http=FakeMicrosoft())
            save_mailbox(vault, keys, apply_changes(load_mailbox(vault, keys), changes, NOW))
            self.assertTrue(offers_resets(load_mailbox(vault, keys)))

    def test_microsoft_down_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault, keys = Vault(Path(tmp), MASTER), Keys(MASTER)
            save_mailbox(vault, keys, box())
            changes, notes = check(vault, keys, self.fail, NOW, http=FakeMicrosoft(down=True))
            self.assertEqual(changes, {})
            self.assertIn("tried again on the next run", notes[0])

    def test_old_answered_emails_are_forgotten(self):
        kept = apply_changes(box(handled={"old": "2026-10-01T00:00:00Z", "recent": "2026-11-02T00:00:00Z"}),
                             {"handled": {"new": "2026-11-03T08:00:00Z"}}, NOW)
        self.assertEqual(sorted(kept["handled"]), ["new", "recent"])

    def test_connect(self):
        said = []
        result = connect(CLIENT.upper(), say=said.append, http=FakeMicrosoft(), now=NOW)
        self.assertEqual((result["account"], result["clientId"], result["refreshToken"]), ("office@example.com", CLIENT, "first-refresh"))
        self.assertIn("ABCD1234", said[0])
        with self.assertRaises(ValueError):
            connect("not-a-client-id", say=said.append, http=FakeMicrosoft())

    def test_the_device_code_is_waited_for(self):
        answers = iter([{"error": "authorization_pending"}, {"error": "slow_down"}, {"access_token": "a", "refresh_token": "r"}])
        slept = []
        with mock.patch.object(etjm_mail, "_login", lambda endpoint, fields: next(answers)):
            tokens = etjm_mail.finish_device_sign_in(CLIENT, {"device_code": "d", "interval": 5, "expires_in": 900},
                                                     sleep=slept.append, clock=lambda: 0)
        self.assertEqual((tokens["refresh_token"], slept), ("r", [5, 5, 10]))
        with mock.patch.object(etjm_mail, "_login", lambda endpoint, fields: {"error": "expired_token"}):
            with self.assertRaises(etjm_mail.MailboxError):
                etjm_mail.finish_device_sign_in(CLIENT, {"device_code": "d", "interval": 1}, sleep=lambda s: None, clock=lambda: 0)


class CommandLineTests(unittest.TestCase):
    """The commands exactly as the data repo's workflows run them."""

    def run_tool(self, data_dir, *args):
        env = {**os.environ, "MASTER_KEY": base64.b64encode(MASTER).decode()}
        env.pop("GITHUB_TOKEN", None)
        return subprocess.run([sys.executable, str(TOOLS / "office_mailbox.py"), "--data-dir", str(data_dir), *args],
                              env=env, capture_output=True, text=True)

    def test_the_workflow_commands(self):
        root = Path(__file__).resolve().parents[1] / "data-repo-template/.github/workflows"
        self.assertIn("office_mailbox.py --data-dir . check --push", (root / "update.yml").read_text())
        mailbox_yml = (root / "mailbox.yml").read_text()
        self.assertIn('office_mailbox.py --data-dir . connect --client-id "$CLIENT_ID" --push', mailbox_yml)
        self.assertIn("office_mailbox.py --data-dir . disconnect --push", mailbox_yml)
        with tempfile.TemporaryDirectory() as tmp:
            done = self.run_tool(tmp, "check", "--push")  # as "Update website and journals" runs it
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("isn't connected", done.stdout)
            done = self.run_tool(tmp, "connect", "--client-id", "not-an-id", "--push")  # as "Connect the office mailbox" does
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("Application (client) ID", done.stderr)
            self.assertNotIn("unrecognized arguments", done.stderr)
            save_mailbox(Vault(Path(tmp), MASTER), Keys(MASTER), box())
            done = self.run_tool(tmp, "disconnect")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertFalse((Path(tmp) / MAILBOX).exists())


class MicrosoftStub(http.server.BaseHTTPRequestHandler):
    """Microsoft's sign-in and Graph, as far as the website uses them."""

    accept = {"refresh-1"}
    messages: list = []
    sent: list = []
    calls: list = []

    def _answer(self, status, value=None):
        body = b"" if value is None else json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        MicrosoftStub.calls.append(("POST", self.path))
        if self.path == "/login/token":
            fields = dict(urllib.parse.parse_qsl(body))
            if fields.get("grant_type") == "refresh_token" and fields.get("refresh_token") in MicrosoftStub.accept:
                return self._answer(200, {"access_token": "stub-access", "refresh_token": "stub-refresh", "expires_in": 3600})
            return self._answer(400, {"error": "invalid_grant", "error_description": "AADSTS70008: expired"})
        if self.path == "/graph/me/sendMail":
            if self.headers.get("Authorization") != "Bearer stub-access":
                return self._answer(401, {"error": {"code": "InvalidAuthenticationToken"}})
            MicrosoftStub.sent.append(json.loads(body)["message"])
            return self._answer(202)
        return self._answer(404, {"error": "not found"})

    def do_GET(self):
        MicrosoftStub.calls.append(("GET", self.path))
        if self.headers.get("Authorization") != "Bearer stub-access":
            return self._answer(401, {"error": {"code": "InvalidAuthenticationToken"}})
        if self.path.startswith("/graph/me/mailFolders/inbox/messages?"):
            return self._answer(200, {"value": MicrosoftStub.messages})
        if self.path.startswith("/graph/me/mailFolders/junkemail/messages?"):
            return self._answer(200, {"value": []})
        if self.path.startswith("/graph/me?"):
            return self._answer(200, {"userPrincipalName": "office@example.com", "mail": None})
        return self._answer(404, {"error": {"code": "NotFound"}})

    def log_message(self, *args):
        pass


class EndToEndMailboxTests(unittest.TestCase):
    """The real scripts against a stand-in for Microsoft: email the office, get the link."""

    def test_from_an_emailed_request_to_a_link_sent_from_the_mailbox(self):
        import openpyxl

        from test_recovery import SETTINGS, open_link

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MicrosoftStub)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        MicrosoftStub.accept, MicrosoftStub.sent, MicrosoftStub.calls = {"refresh-1"}, [], []
        MicrosoftStub.messages = [message("m1", "Engineer Timewriting password reset: ann.example")]
        env = {"ETJM_MS_LOGIN": f"{base}/login", "ETJM_MS_GRAPH": f"{base}/graph"}
        try:
            with mock.patch.dict(os.environ, env), tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(etjm_mail.mailbox_address(etjm_mail.refresh(CLIENT, "refresh-1")["access_token"]), "office@example.com")
                with self.assertRaises(etjm_mail.SignInLost):
                    etjm_mail.refresh(CLIENT, "someone-elses-token")
                data, site = Path(tmp) / "data", Path(tmp) / "site"
                site.mkdir()
                vault, keys = Vault(data, MASTER), Keys(MASTER)
                vault.write_json("settings.json", SETTINGS)
                workbook = openpyxl.Workbook()
                workbook.active.append(["Project No.", "Job Description", "Job Location", "Owner", "Project Task No.", "Status"])
                workbook.active.append(["JOB000010", "Tank repaint", "SE", 80, "REVEX", "Open"])
                buffer = io.BytesIO()
                workbook.save(buffer)
                vault.write_json("input/report.json", {"name": "report.xlsx", "sha256": "x", "uploaded": "2026-10-01",
                                                       "xlsx": base64.b64encode(buffer.getvalue()).decode()})
                save_mailbox(vault, keys, box())
                issues_file = Path(tmp) / "issues.json"
                run_env = {**os.environ, **env, "MASTER_KEY": base64.b64encode(MASTER).decode(), "SITE_URL": "https://example.com/site/"}
                run_env.pop("GITHUB_TOKEN", None)
                run_env.pop("RELAY_URL", None)

                def publish(issue_list):
                    issues_file.write_text(json.dumps(issue_list))
                    return subprocess.run([sys.executable, str(TOOLS / "publish_website.py"), "--data-dir", str(data), "--site-dir", str(site),
                                           "--data-repo", "example/data", "--issues-file", str(issues_file)],
                                          env=run_env, capture_output=True, text=True, check=True).stdout

                log = publish([])
                users = json.loads((site / "site/data/users.json").read_text())
                self.assertEqual(users["reset"], {"email": "office@example.com"})
                self.assertNotIn("office@example.com", log)

                opened = []
                changes, notes = check(vault, keys, opened.append, NOW)  # the real Microsoft calls, to the stand-in
                self.assertEqual(notes, ["Office mailbox: 1 new password-reset request email(s)."])
                issues = [{"number": n, "state": "open", **issue} for n, issue in enumerate(opened, 1)]
                log = publish(issues)
                self.assertIn("Password-reset emails sent: 1.", log)
                self.assertNotIn("ann", log)
                (sent,) = MicrosoftStub.sent
                self.assertEqual(sent["toRecipients"], [{"emailAddress": {"address": "ann.example@example.com"}}])
                self.assertEqual(sent["subject"], "Reset your Engineer Timewriting password")
                html = sent["body"]["content"].replace("&amp;", "&")
                link = html.split('href="', 1)[1].split('"', 1)[0]
                self.assertTrue(link.startswith("https://example.com/site/reset.html#"))
                fragment = dict(urllib.parse.parse_qsl(link.split("#", 1)[1]))
                entry = json.loads((site / "site/data/users.json").read_text())["users"][user_id("ann.example")]
                new_user_key, profile = open_link(entry, "ann.example", fragment["id"], unb64url(fragment["k"]))
                self.assertEqual(profile["username"], "ann.example")

                MicrosoftStub.accept = set()  # Microsoft stops accepting the sign-in
                changes, notes = check(vault, keys, self.fail, NOW)
                self.assertIn("stopped working", notes[0])
                save_mailbox(vault, keys, apply_changes(load_mailbox(vault, keys), changes, NOW))
                publish([])
                self.assertNotIn("reset", json.loads((site / "site/data/users.json").read_text()), "no offer while it doesn't work")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
