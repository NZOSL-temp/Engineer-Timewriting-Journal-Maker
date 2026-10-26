"""Tests for forgotten passwords (reset links by email), email addresses in settings, and
changing MASTER_KEY. Run with:  python -m unittest discover -s tests

Everything here uses made-up names, addresses and keys.
"""

import base64
import datetime as dt
import hashlib
import hmac
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

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from etjm_accounts import (  # noqa: E402
    RECOVER_MINUTES,
    RESEND_MINUTES,
    Keys,
    apply_password_changes,
    expire_recoveries,
    handle_reset_requests,
    new_state,
    profile_for,
    public_users,
    sync_people,
)
from etjm_core import (  # noqa: E402
    PASSWORD_V1,
    RESET_REQUEST_V1,
    RESET_TITLE,
    bundle_key,
    clean_email,
    decrypt_bundle,
    load_settings,
    parse_settings,
    people,
    recover_key,
    seal,
    unb64url,
    unseal,
    unseal_profile,
    unwrap_user_key,
    user_id,
    user_tag,
    wrap_user_key,
)

MASTER = bytes(range(32))
NOW = dt.datetime(2026, 10, 20, 9, 0, tzinfo=dt.timezone.utc)
SETTINGS = {
    "employees": [{"name": "Ann Example", "rechargeRate": 160, "email": "Ann.Example@Example.com"},
                  {"name": "Ben Sample", "rechargeRate": 215}],
    "admins": [{"name": "Dee Office", "email": "dee@example.com"}],
}


def setup_world():
    keys = Keys(MASTER)
    settings = parse_settings(SETTINGS)
    everyone = people(settings)
    state = new_state()
    sync_people(state, keys, everyone)
    return keys, settings, everyone, state


def reset_request(number, username):
    return {"number": number, "state": "open", "title": RESET_TITLE,
            "body": f"{RESET_REQUEST_V1}\nA password-reset request from the sign-in page.\n\n```etjm\n{user_id(username)}\n```\n"}


def publish(state, keys, settings, everyone, existing=None, now=NOW):
    epoch = int(state["epoch"])
    profiles = {p["username"]: profile_for(p, settings, keys, epoch, state["users"][p["username"]], "admin-token") for p in everyone}
    return public_users(state, keys, profiles, existing or {}, {"reset": {"url": "https://example.com/relay"}}, now)["users"]


def open_link(entry, username, rid, secret):
    """What the reset page does with a link: returns the new user key and the profile it opens."""
    recover = entry["recover"]
    new_user_key = unseal(recover_key(secret), recover["key"], f"etjm-recover|{username}|{rid}")
    profile = json.loads(unseal(new_user_key, recover["profile"], f"etjm-recover-profile|{username}|{rid}"))
    return new_user_key, profile


def reset_issue(number, new_user_key, username, rid, password, tag, sealed_id=None):
    """What the reset page sends: an ETJMR1 request sealed with the link's new user key."""
    wrap = wrap_user_key(new_user_key, password, username)
    token = seal(new_user_key, json.dumps({"id": sealed_id or rid, "wrap": wrap}).encode(), f"etjm-recover-pw|{username}|{rid}")
    return {"number": number, "state": "open", "title": f"Password change · {tag}",
            "body": f"{PASSWORD_V1}\nA password reset, encrypted.\n\n```etjm\nETJMR1.{token}\n```\n"}


class EmailSettingsTests(unittest.TestCase):
    def test_emails_are_checked_and_tidied(self):
        settings = parse_settings(SETTINGS)
        self.assertEqual(settings["employees"][0]["email"], "ann.example@example.com")
        self.assertNotIn("email", settings["employees"][1])
        self.assertEqual({p["username"]: p["email"] for p in people(settings)},
                         {"ann.example": "ann.example@example.com", "ben.sample": None, "dee.office": "dee@example.com"})
        for bad in ["not an email", "a@b", "two@one.com,three@x.com", "<a@b.com>", 7]:
            with self.assertRaises(ValueError):
                parse_settings({"employees": [{"name": "Ann Example", "email": bad}]})
        self.assertIsNone(clean_email("  "))

    def test_an_admin_who_is_also_an_engineer_uses_the_engineer_email(self):
        settings = parse_settings({"employees": [{"name": "Ann Example", "email": "ann@example.com"}],
                                   "admins": [{"name": "Ann Example", "email": "ann.office@example.com"}]})
        self.assertEqual(people(settings)[0]["email"], "ann@example.com")

    def test_profiles_carry_only_your_own_email(self):
        keys, settings, everyone, state = setup_world()
        users = publish(state, keys, settings, everyone)
        for person in everyone:
            username = person["username"]
            profile = unseal_profile(keys.user_key(username, state["users"][username]), users[user_id(username)]["profile"], username)
            self.assertEqual(profile["email"], person["email"])
        self.assertNotIn("example.com", json.dumps(users))  # nobody's address is readable in the public file


class ResetRequestTests(unittest.TestCase):
    def test_a_request_makes_a_link_that_opens_only_a_new_key(self):
        keys, settings, everyone, state = setup_world()
        old_user_key = keys.user_key("ann.example", state["users"]["ann.example"])
        done, outbox, notes = handle_reset_requests(state, keys, [reset_request(7, "ann.example")], everyone, NOW)
        self.assertEqual(done, [7])
        self.assertEqual([(m["username"], m["email"]) for m in outbox], [("ann.example", "ann.example@example.com")])
        self.assertFalse(any("ann" in n for n in notes))  # the log names nobody
        mail = outbox[0]
        entry = publish(state, keys, settings, everyone)[user_id("ann.example")]
        new_user_key, profile = open_link(entry, "ann.example", mail["id"], mail["secret"])
        self.assertNotEqual(new_user_key, old_user_key)
        self.assertEqual(set(profile), {"v", "username", "name", "tag", "epoch", "bundleKey", "recover"})
        self.assertEqual(profile["tag"], user_tag(MASTER, "ann.example"))
        self.assertEqual(base64.b64decode(profile["bundleKey"]), bundle_key(MASTER, state["epoch"]))
        with self.assertRaises(ValueError):  # a wrong secret opens nothing
            open_link(entry, "ann.example", mail["id"], os.urandom(32))
        # The old password still works until the reset is finished.
        password = keys.password("ann.example", state["users"]["ann.example"])
        self.assertEqual(unwrap_user_key(entry, password, "ann.example"), old_user_key)

    def test_unknown_people_and_people_without_email_get_nothing(self):
        keys, settings, everyone, state = setup_world()
        issues = [reset_request(1, "ben.sample"), reset_request(2, "nobody.here"),
                  {"number": 3, "state": "open", "title": RESET_TITLE, "body": f"{RESET_REQUEST_V1}\n```etjm\nnot-a-hash\n```\n"}]
        done, outbox, _ = handle_reset_requests(state, keys, issues, everyone, NOW)
        self.assertEqual(done, [1, 2, 3])
        self.assertEqual(outbox, [])
        self.assertFalse(any("recover" in r for r in state["users"].values()))

    def test_nothing_is_sent_when_emails_are_not_set_up(self):
        keys, settings, everyone, state = setup_world()
        done, outbox, notes = handle_reset_requests(state, keys, [reset_request(1, "ann.example")], everyone, NOW, can_send=False)
        self.assertEqual((done, outbox), ([1], []))
        self.assertIn("aren't set up", notes[0])

    def test_one_email_per_quarter_hour_and_a_new_link_replaces_the_old(self):
        keys, settings, everyone, state = setup_world()
        _, first, _ = handle_reset_requests(state, keys, [reset_request(1, "ann.example")], everyone, NOW)
        _, again, notes = handle_reset_requests(state, keys, [reset_request(2, "ann.example")], everyone, NOW + dt.timedelta(minutes=5))
        self.assertEqual(again, [])
        self.assertIn(f"last {RESEND_MINUTES} minutes", notes[0])
        later = NOW + dt.timedelta(minutes=RESEND_MINUTES + 1)
        _, second, _ = handle_reset_requests(state, keys, [reset_request(3, "ann.example")], everyone, later)
        self.assertEqual(len(second), 1)
        self.assertNotEqual(second[0]["id"], first[0]["id"])
        entry = publish(state, keys, settings, everyone, now=later)[user_id("ann.example")]
        self.assertEqual(entry["recover"]["id"], second[0]["id"])
        with self.assertRaises(ValueError):  # the first link no longer opens anything
            open_link(entry, "ann.example", first[0]["id"], first[0]["secret"])

    def test_two_requests_in_one_run_send_one_email(self):
        keys, settings, everyone, state = setup_world()
        done, outbox, _ = handle_reset_requests(state, keys, [reset_request(1, "ann.example"), reset_request(2, "ann.example")], everyone, NOW)
        self.assertEqual((done, len(outbox)), ([1, 2], 1))

    def test_links_run_out(self):
        keys, settings, everyone, state = setup_world()
        handle_reset_requests(state, keys, [reset_request(1, "ann.example")], everyone, NOW)
        self.assertEqual(expire_recoveries(state, NOW + dt.timedelta(minutes=RECOVER_MINUTES - 1)), 0)
        self.assertIn("recover", publish(state, keys, settings, everyone)[user_id("ann.example")])
        self.assertEqual(expire_recoveries(state, NOW + dt.timedelta(minutes=RECOVER_MINUTES + 1)), 1)
        self.assertNotIn("recover", publish(state, keys, settings, everyone)[user_id("ann.example")])

    def test_an_admin_reset_cancels_a_link(self):
        keys, settings, everyone, state = setup_world()
        handle_reset_requests(state, keys, [reset_request(1, "ann.example")], everyone, NOW)
        state["users"]["ann.example"]["reset"] = True
        sync_people(state, keys, everyone)
        self.assertNotIn("recover", state["users"]["ann.example"])


class FinishResetTests(unittest.TestCase):
    def start(self):
        keys, settings, everyone, state = setup_world()
        _, outbox, _ = handle_reset_requests(state, keys, [reset_request(1, "ann.example")], everyone, NOW)
        mail = outbox[0]
        entry = publish(state, keys, settings, everyone)[user_id("ann.example")]
        new_user_key, profile = open_link(entry, "ann.example", mail["id"], mail["secret"])
        return keys, settings, everyone, state, mail, new_user_key, profile

    def test_the_new_password_and_keys_replace_the_old(self):
        keys, settings, everyone, state, mail, new_user_key, profile = self.start()
        record = state["users"]["ann.example"]
        old_password = keys.password("ann.example", record)
        old_data_keys = keys.data_keys("ann.example", record)
        epoch = state["epoch"]
        done, notes = apply_password_changes(state, keys, [reset_issue(2, new_user_key, "ann.example", mail["id"], "a brand new password", profile["tag"])],
                                             NOW + dt.timedelta(minutes=10))
        self.assertEqual((done, notes), ([2], ["Password reset #2 applied."]))
        self.assertEqual(keys.user_key("ann.example", record), new_user_key)
        self.assertEqual(len(keys.data_keys("ann.example", record)), len(old_data_keys) + 1)
        self.assertEqual(keys.data_keys("ann.example", record)[:-1], old_data_keys)
        self.assertTrue(record["own"])
        self.assertNotIn("password", record)  # the office never knows the new one
        self.assertNotIn("recover", record)
        self.assertEqual(state["epoch"], epoch + 1)  # the old job-list key stops working
        entry = publish(state, keys, settings, everyone, now=NOW + dt.timedelta(minutes=11))[user_id("ann.example")]
        self.assertEqual(unwrap_user_key(entry, "a brand new password", "ann.example"), new_user_key)
        with self.assertRaises(ValueError):
            unwrap_user_key(entry, old_password, "ann.example")
        self.assertEqual(unseal_profile(new_user_key, entry["profile"], "ann.example")["epoch"], epoch + 1)
        self.assertNotIn("recover", entry)

    def test_used_expired_wrong_or_forged_requests_are_ignored(self):
        keys, settings, everyone, state, mail, new_user_key, profile = self.start()
        rid, tag = mail["id"], profile["tag"]
        late = NOW + dt.timedelta(minutes=RECOVER_MINUTES + 1)
        cases = [
            ("expired", reset_issue(2, new_user_key, "ann.example", rid, "a brand new password", tag), late),
            ("wrong id inside", reset_issue(3, new_user_key, "ann.example", rid, "a brand new password", tag, sealed_id="0" * 16), NOW),
            ("someone else's key", reset_issue(4, os.urandom(32), "ann.example", rid, "a brand new password", tag), NOW),
            ("old user key", reset_issue(5, keys.user_key("ann.example", state["users"]["ann.example"]), "ann.example", rid, "x" * 14, tag), NOW),
        ]
        for name, request, when in cases:
            before = json.dumps(state, sort_keys=True)
            done, notes = apply_password_changes(state, keys, [request], when)
            self.assertEqual(done, [request["number"]], name)
            self.assertIn("ignored", notes[0], name)
            self.assertEqual(json.dumps(state, sort_keys=True), before, name)
        good = reset_issue(6, new_user_key, "ann.example", rid, "a brand new password", tag)
        self.assertEqual(apply_password_changes(state, keys, [good], NOW)[1], ["Password reset #6 applied."])
        replay = dict(good, number=7)
        self.assertIn("ignored", apply_password_changes(state, keys, [replay], NOW)[1][0])

    def test_saves_with_the_old_timesheet_key_are_refused_afterwards(self):
        from test_etjm import ts_issue

        from etjm_core import collect_timesheets, parse_timesheet  # noqa: F401

        keys, settings, everyone, state, mail, new_user_key, profile = self.start()
        old_key_issue = ts_issue(9, keys, state, "ann.example", {"v": 1, "employee": "Ann Example", "month": "2026-10", "savedAt": "x",
                                                                 "jobs": [], "other": []})
        apply_password_changes(state, keys, [reset_issue(2, new_user_key, "ann.example", mail["id"], "a brand new password", profile["tag"])], NOW)
        by_tag = {user_tag(MASTER, u): {"username": u, "name": u, "dataKeys": keys.data_keys(u, r)} for u, r in state["users"].items()}
        _, warnings = collect_timesheets([old_key_issue], by_tag)
        self.assertIn("old key", warnings[0])


class RelayStub(http.server.BaseHTTPRequestHandler):
    """Stands in for the reset helper: checks the signature like relay/Code.gs, and keeps the emails."""
    secret = "test-relay-secret"
    sent: list = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        fields = [str(body.get(k, "")) for k in ("ts", "to", "subject", "text", "html")]
        expected = hmac.new(self.secret.encode(), "\n".join(fields).encode(), hashlib.sha256).hexdigest()
        ok = body.get("action") == "send" and hmac.compare_digest(expected, body.get("sig", ""))
        if ok:
            RelayStub.sent.append(body)
        answer = json.dumps({"ok": ok} if ok else {"ok": False, "error": "bad signature"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, *args):
        pass


class EndToEndResetTests(unittest.TestCase):
    """The real scripts, as the Action runs them: ask, get the email, use the link, sign in."""

    def test_forgotten_password_from_request_to_sign_in(self):
        import openpyxl

        from etjm_mail import send
        from etjm_vault import Vault

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RelayStub)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        relay = f"http://127.0.0.1:{server.server_port}/exec"
        RelayStub.sent = []
        try:
            with self.assertRaises(RuntimeError):  # unsigned or wrongly signed sends are refused
                send(relay, "not-the-secret", "x@example.com", "s", "t", "h")
            with tempfile.TemporaryDirectory() as tmp:
                data, site = Path(tmp) / "data", Path(tmp) / "site"
                site.mkdir()
                vault = Vault(data, MASTER)
                vault.write_json("settings.json", SETTINGS)
                workbook = openpyxl.Workbook()
                workbook.active.append(["Project No.", "Job Description", "Job Location", "Owner", "Project Task No.", "Status"])
                workbook.active.append(["JOB000010", "Tank repaint", "SE", 80, "REVEX", "Open"])
                buffer = io.BytesIO()
                workbook.save(buffer)
                vault.write_json("input/report.json", {"name": "report.xlsx", "sha256": "x", "uploaded": "2026-10-01",
                                                       "xlsx": base64.b64encode(buffer.getvalue()).decode()})
                env = {**os.environ, "MASTER_KEY": base64.b64encode(MASTER).decode(), "RELAY_URL": relay,
                       "RELAY_SECRET": RelayStub.secret, "SITE_URL": "https://example.com/site/"}
                env.pop("GITHUB_TOKEN", None)
                issues = Path(tmp) / "issues.json"

                def run(issue_list):
                    issues.write_text(json.dumps(issue_list))
                    return subprocess.run([sys.executable, str(TOOLS / "publish_website.py"), "--data-dir", str(data), "--site-dir", str(site),
                                           "--data-repo", "example/data", "--issues-file", str(issues)],
                                          env=env, capture_output=True, text=True, check=True).stdout

                run([])
                users = json.loads((site / "site/data/users.json").read_text())
                self.assertEqual(users["reset"], {"url": relay})
                entry = users["users"][user_id("ann.example")]
                old_password = Keys(MASTER).password("ann.example", vault.read_json("users.json")["users"]["ann.example"])

                log = run([reset_request(1, "ann.example"), reset_request(2, "ben.sample")])
                self.assertIn("Password-reset emails sent: 1.", log)
                self.assertNotIn("example.com", log)
                self.assertNotIn("ann", log)
                (mail,) = RelayStub.sent
                self.assertEqual(mail["to"], "ann.example@example.com")
                link = next(word for word in mail["text"].split() if word.startswith("https://example.com/site/reset.html#"))
                self.assertIn(link, mail["html"].replace("&amp;", "&"))
                fragment = dict(urllib.parse.parse_qsl(link.split("#", 1)[1]))
                self.assertEqual(fragment["u"], "ann.example")

                users = json.loads((site / "site/data/users.json").read_text())
                entry = users["users"][user_id("ann.example")]
                new_user_key, profile = open_link(entry, "ann.example", fragment["id"], unb64url(fragment["k"]))
                bundle = decrypt_bundle(json.loads((site / "site/data/bundle.enc.json").read_text()), base64.b64decode(profile["bundleKey"]))
                self.assertEqual(bundle["dataRepo"], "example/data")

                log = run([reset_issue(3, new_user_key, "ann.example", fragment["id"], "my own new password", profile["tag"])])
                self.assertIn("Password reset #3 applied.", log)
                users = json.loads((site / "site/data/users.json").read_text())
                entry = users["users"][user_id("ann.example")]
                self.assertEqual(unwrap_user_key(entry, "my own new password", "ann.example"), new_user_key)
                with self.assertRaises(ValueError):
                    unwrap_user_key(entry, old_password, "ann.example")
                self.assertNotIn("recover", entry)
                self.assertEqual(len(RelayStub.sent), 1)
        finally:
            server.shutdown()
            server.server_close()


class MasterKeyChangeTests(unittest.TestCase):
    def test_everything_moves_to_the_new_key_and_passwords_keep_working(self):
        from build_outputs import write_tree
        from rotate_master_key import already_rotated, retitles, rotate_files
        from test_etjm import collected, settings as journal_settings, sheet

        from etjm_outputs import build_all
        from etjm_vault import Vault

        old, new = MASTER, bytes(range(1, 33))
        keys, settings, everyone, state = setup_world()
        handle_reset_requests(state, keys, [reset_request(1, "ann.example")], everyone, NOW)
        passwords = {u: keys.password(u, r) for u, r in state["users"].items()}
        wraps = {u: dict(r["wrap"]) for u, r in state["users"].items()}
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            vault = Vault(data, old)
            vault.write_json("settings.json", SETTINGS)
            vault.write_json("users.json", state)
            vault.write_json("generated/jobs.json", {"jobs": []})
            timesheets, _ = collected([sheet(month="2026-09", jobs=[{"job": "JOB000010", "hours": {"REVEX": {"1": 2}}}])])
            files, _ = build_all(timesheets, journal_settings(), {})
            write_tree(vault, files)
            months_before = {f: sorted(vault.months(f)) for f in ("output", "timesheets")}
            self.assertFalse(already_rotated(data, new))

            self.assertGreater(rotate_files(data, old, new), 4)

            self.assertTrue(already_rotated(data, new))
            fresh, stale = Vault(data, new), Vault(data, old)
            for rel in ("settings.json", "users.json", "generated/jobs.json"):
                self.assertIsNotNone(fresh.read(rel))
                with self.assertRaises(ValueError):
                    stale.read(rel)
            self.assertEqual({f: sorted(fresh.months(f)) for f in ("output", "timesheets")}, months_before)
            self.assertEqual(fresh.read_json("settings.json")["employees"][0]["email"], "Ann.Example@Example.com")
            moved = fresh.read_json("users.json")
            new_keys = Keys(new)
            for username, record in moved["users"].items():
                self.assertEqual(record["wrap"], wraps[username])  # so every password still opens the same user key
                user_key = unwrap_user_key(record["wrap"], passwords[username], username)
                self.assertEqual(new_keys.user_key(username, record), user_key)
                self.assertEqual(new_keys.data_keys(username, record), keys.data_keys(username, state["users"][username]))
                self.assertEqual(new_keys.password(username, record), passwords[username])
            recover = moved["users"]["ann.example"]["recover"]
            self.assertEqual(new_keys.recover_user_key("ann.example", recover), keys.recover_user_key("ann.example", state["users"]["ann.example"]["recover"]))

            issues = [{"number": 4, "title": f"Timesheet 2026-09 · {user_tag(old, 'ann.example')}"},
                      {"number": 5, "title": f"Password change · {user_tag(old, 'ben.sample')}"},
                      {"number": 6, "title": f"Timesheet 2026-09 · {user_tag(new, 'ann.example')}"},
                      {"number": 7, "title": "Password reset request"}]
            self.assertEqual(retitles(issues, moved["users"], old, new),
                             [(4, f"Timesheet 2026-09 · {user_tag(new, 'ann.example')}"), (5, f"Password change · {user_tag(new, 'ben.sample')}")])



class LostMasterKeyTests(unittest.TestCase):
    """MASTER_KEY replaced with nobody knowing the old one: an admin's sign-in rebuilds it all."""

    def test_an_admin_sign_in_re_encrypts_everything_for_a_new_key(self):
        from build_outputs import write_tree
        from recover_master_key import recover
        from test_etjm import collected, settings as journal_settings, sheet

        from etjm_outputs import build_all
        from etjm_vault import Vault

        old, new = MASTER, bytes(range(2, 34))
        keys, settings, everyone, state = setup_world()
        ben_key = keys.user_key("ben.sample", state["users"]["ben.sample"])  # Ben chooses his own password
        token = seal(ben_key, json.dumps({"seq": 5, "wrap": wrap_user_key(ben_key, "bens own secret words", "ben.sample")}).encode(), "etjm-pw|ben.sample")
        apply_password_changes(state, keys, [{"number": 1, "state": "open", "title": f"Password change · {user_tag(old, 'ben.sample')}",
                                              "body": f"{PASSWORD_V1}\n```etjm\nETJMP1.{token}\n```\n"}], NOW)
        self.assertTrue(state["users"]["ben.sample"]["own"])
        handle_reset_requests(state, keys, [reset_request(2, "ann.example")], everyone, NOW)
        passwords = {u: keys.password(u, r) for u, r in state["users"].items() if r.get("password")}
        user_keys = {u: keys.user_key(u, r) for u, r in state["users"].items()}
        data_keys = {u: keys.data_keys(u, r) for u, r in state["users"].items()}
        wraps = {u: dict(r["wrap"]) for u, r in state["users"].items()}
        public = publish(state, keys, settings, everyone)
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            vault = Vault(data, old)
            vault.write_json("settings.json", SETTINGS)
            vault.write_json("users.json", state)
            vault.write_json("generated/jobs.json", {"jobs": []})
            timesheets, _ = collected([sheet(month="2026-09", jobs=[{"job": "JOB000010", "hours": {"REVEX": {"1": 2}}}])])
            write_tree(vault, build_all(timesheets, journal_settings(), {})[0])
            months = {f: sorted(vault.months(f)) for f in ("output", "timesheets")}

            with self.assertRaises(ValueError):
                recover(data, public, "dee.office", "not the password", new)
            with self.assertRaises(ValueError):  # an engineer's sign-in can't open the files
                recover(data, public, "ann.example", passwords["ann.example"], new)

            result = recover(data, public, "dee.office", passwords["dee.office"], new)

            self.assertEqual((sorted(result["kept"]), result["reset"]), (["ann.example", "dee.office"], ["ben.sample"]))
            self.assertEqual(result["renames"], {user_tag(old, u): user_tag(new, u) for u in ("ann.example", "dee.office")})
            fresh = Vault(data, new)
            self.assertEqual(fresh.read_json("settings.json"), SETTINGS)
            self.assertEqual({f: sorted(fresh.months(f)) for f in ("output", "timesheets")}, months)
            moved, new_keys = fresh.read_json("users.json"), Keys(new)
            for username in ("ann.example", "dee.office"):
                record = moved["users"][username]
                self.assertEqual(record["wrap"], wraps[username])  # the same password opens the same key
                self.assertEqual(new_keys.user_key(username, record), user_keys[username])
                self.assertEqual(new_keys.password(username, record), passwords[username])
                self.assertNotIn("recover", record)
            self.assertEqual(new_keys.data_keys("ann.example", moved["users"]["ann.example"]), data_keys["ann.example"])
            ben = moved["users"]["ben.sample"]  # nobody knew Ben's password: a fresh issued one
            self.assertFalse(ben["own"])
            self.assertEqual(unwrap_user_key(ben["wrap"], new_keys.password("ben.sample", ben), "ben.sample"), new_keys.user_key("ben.sample", ben))
            self.assertEqual(len(new_keys.data_keys("ben.sample", ben)), 1)


if __name__ == "__main__":
    unittest.main()
