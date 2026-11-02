#!/usr/bin/env python3
"""Browser check of the website with made-up people and a fake GitHub, end to end.

Runs the real pages in Chromium (Playwright) against a temporary encrypted data repo; the
"Action" (publish_website.py and build_outputs.py) runs whenever something is saved, as on
GitHub. Covers loading a month from a spreadsheet and the office's "Allow write access".

    pip install playwright && playwright install chromium
    python tests/browser_check.py
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "tools"
sys.path.insert(0, str(TOOLS))

import openpyxl  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from etjm_accounts import Keys  # noqa: E402
from etjm_vault import Vault  # noqa: E402

PORT = 8769
SITE_URL = f"http://127.0.0.1:{PORT}/"
MASTER = bytes(range(9, 41))
SHOTS = Path(tempfile.gettempdir()) / "etjm-browser-shots"
SHOTS.mkdir(exist_ok=True)

tmp = Path(tempfile.mkdtemp(prefix="etjm-browser-"))
DATA, SITE_REPO = tmp / "data", tmp / "site-repo"
shutil.copytree(REPO / "site", SITE_REPO / "site")
SITE = SITE_REPO / "site"
vault = Vault(DATA, MASTER)
vault.write_json("settings.json", {
    "employees": [{"name": "Ann Example", "rechargeRate": 160}, {"name": "Ben Sample", "rechargeRate": 215}],
    "admins": [{"name": "Dee Office"}], "closedMonths": [],
})
report = openpyxl.Workbook()
report.active.append(["Project No.", "Job Description", "Job Location", "Owner", "Project Task No.", "Status"])
for no, desc, task in (("JOB009683", "Wharfline log yard review", "REVEX"), ("JOB000010", "Tank repaint", "REVEX"),
                       ("JOB000010", "Tank repaint", "CAPEX"), ("JOB008298", "Compliance studies", "REVEX")):
    report.active.append([no, desc, "DU", 80, task, "Open"])
buf = io.BytesIO(); report.save(buf); raw = buf.getvalue()
vault.write_json("input/report.json", {"name": "Job Task Overview - test.xlsx", "sha256": hashlib.sha256(raw).hexdigest(),
                                       "uploaded": "2026-10-01", "xlsx": base64.b64encode(raw).decode()})

lock = threading.Lock()
issues: list[dict] = []
dirty, busy = threading.Event(), threading.Event()
errors: list[str] = []
passed: list[str] = []
ENV = {**os.environ, "MASTER_KEY": base64.b64encode(MASTER).decode(), "SAVE_TOKEN": "test-save-token", "ADMIN_TOKEN": "test-admin-token"}
ENV.pop("GITHUB_TOKEN", None)
ISSUES_FILE = tmp / "issues.json"


def check(name, ok, detail=""):
    (passed if ok else errors).append(name)
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))


def action():
    with lock:
        ISSUES_FILE.write_text(json.dumps(issues))
    for script, args in (("publish_website.py", ["--site-dir", str(SITE_REPO), "--data-repo", "example/data"]), ("build_outputs.py", [])):
        out = subprocess.run([sys.executable, str(TOOLS / script), "--data-dir", str(DATA), "--issues-file", str(ISSUES_FILE), *args],
                             env=ENV, capture_output=True, text=True)
        if out.returncode:
            errors.append(f"{script} failed: {out.stderr[-500:]}")


def simulator():
    while True:
        dirty.wait(); busy.set(); dirty.clear()
        try:
            action()
        finally:
            busy.clear()


def wait_for_action():
    deadline = time.time() + 90
    time.sleep(1)
    while (dirty.is_set() or busy.is_set()) and time.time() < deadline:
        time.sleep(0.3)


def blob_sha(b): return hashlib.sha1(b"blob %d\0" % len(b) + b).hexdigest()


def github(route, request):
    url = urllib.parse.urlparse(request.url); path = urllib.parse.unquote(url.path); method = request.method
    m = re.match(r"/repos/[^/]+/[^/]+/contents/(.*)", path)
    if m:
        rel = m.group(1); target = DATA / rel
        if method == "GET":
            if target.is_dir():
                return route.fulfill(json=[{"name": c.name, "path": f"{rel}/{c.name}", "type": "dir" if c.is_dir() else "file"} for c in sorted(target.iterdir())])
            if not target.exists():
                return route.fulfill(status=404, json={"message": "Not Found"})
            data = target.read_bytes()
            if "raw" in request.headers.get("accept", ""):
                return route.fulfill(body=data, content_type="application/octet-stream")
            return route.fulfill(json={"sha": blob_sha(data), "content": base64.b64encode(data).decode(), "encoding": "base64"})
        if method == "PUT":
            body = json.loads(request.post_data); data = base64.b64decode(body["content"])
            target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data); dirty.set()
            return route.fulfill(json={"commit": {"sha": f"c{time.time_ns()}"}, "content": {"sha": blob_sha(data)}})
    if "/actions/runs" in path:
        return route.fulfill(json={"workflow_runs": [{"path": ".github/workflows/update.yml", "status": "completed", "conclusion": "success"}]})
    if path.startswith("/search/issues"):
        with lock:
            items = json.loads(json.dumps(issues))
        return route.fulfill(json={"total_count": len(items), "items": items[::-1]})
    m = re.match(r"/repos/[^/]+/[^/]+/issues/(\d+)$", path)
    if m:
        with lock:
            issue = next((i for i in issues if i["number"] == int(m.group(1))), None)
            if issue and method == "PATCH":
                issue.update({k: v for k, v in json.loads(request.post_data).items() if k in ("title", "body", "state")})
                issue["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()); dirty.set()
            snapshot = json.loads(json.dumps(issue)) if issue else None
        return route.fulfill(json=snapshot) if snapshot else route.fulfill(status=404, json={"message": "Not Found"})
    if re.match(r"/repos/[^/]+/[^/]+/issues$", path) and method == "POST":
        body = json.loads(request.post_data)
        with lock:
            issue = {"number": len(issues) + 1, "title": body["title"], "body": body["body"], "state": "open",
                     "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            issues.append(issue)
        dirty.set()
        return route.fulfill(status=201, json=issue)
    errors.append(f"unmocked {method} {path}")
    return route.fulfill(status=404, json={"message": "Not Found"})


def issued(username):
    return Keys(MASTER).password(username, vault.read_json("users.json")["users"][username])


def saved_month(username, month):
    """The verified copy the journals use (timesheets/), as {job/task or label: {day: hours}}."""
    found = vault.months("timesheets").get(month)
    sheet = next((t for t in (found[1]["timesheets"] if found else []) if t.get("username") == username), None)
    if not sheet:
        return None
    out = {f"{j['job']}/{task}": hours for j in sheet["jobs"] for task, hours in j["hours"].items()}
    out.update({o["label"]: o["hours"] for o in sheet["other"]})
    return out


def own_layout(path, month_first, rows, name="Ann Example", total_row=True):
    """A spreadsheet laid out like the engineers' own: NAME/MONTH, a header row with dates,
    other time on N/A rows, jobs (some only in the description), a Manually selected column."""
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "My hours"
    ws.append(["NAME", name]); ws.append(["MONTH", month_first]); ws.append([])
    days = [month_first.replace(day=d) for d in range(1, 31)]
    ws.append(["Job Number", "Location", "Owner", "Description", "Capex/Revex", "Manually selected", "Total Hours", *days])
    for row in rows:
        ws.append(row)
    if total_row:
        ws.append(["", "", "", "TOTAL HOURS"])
        ws.append(["JOB009683", "", "", "summary row below the totals", "REVEX", "", "", 99])
    ws2 = wb.create_sheet("Notes"); ws2.append(["not this sheet"])
    wb.save(path)


handler = type("Q", (http.server.SimpleHTTPRequestHandler,), {"log_message": lambda *a: None})
site_server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), lambda *a, **k: handler(*a, directory=str(SITE), **k))
threading.Thread(target=site_server.serve_forever, daemon=True).start()
action()
threading.Thread(target=simulator, daemon=True).start()


def new_page(browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=True)
    ctx.route(re.compile(r"https://api\.github\.com/.*"), github)
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(f"page error: {e}"))
    return ctx, page


def sign_in(page, username, lands):
    page.goto(SITE_URL + "index.html")
    page.fill("#username", username); page.fill("#password", issued(username)); page.click("#submit")
    page.wait_for_url(re.compile(lands), timeout=30000)


def grid(page):
    """What the grid shows: {row label: {day: hours}} for rows with hours."""
    return page.evaluate("""() => {
      const out = {};
      for (const tr of document.querySelectorAll('#grid-wrap tbody tr')) {
        const cells = [...tr.querySelectorAll('input.cell')].filter((c) => c.value.trim());
        if (!cells.length) continue;
        const label = (tr.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
        out[label] = Object.fromEntries(cells.map((c) => [c.dataset.day, Number(c.value)]));
      }
      return out;
    }""")


def wait_ready(page):
    page.wait_for_selector("#grid-wrap:not([hidden])", timeout=30000)
    page.wait_for_timeout(1500)


def load_file(page, path):
    with page.expect_file_chooser() as chooser:
        page.click("#sheet-btn")
    chooser.value.set_files(str(path))
    page.wait_for_timeout(800)


import datetime as dt  # noqa: E402

with sync_playwright() as p:
    browser = p.chromium.launch()

    # ---- An engineer loads their own spreadsheet
    ctx, page = new_page(browser)
    sign_in(page, "ann.example", r"home\.html")
    page.goto(SITE_URL + "timesheet.html?month=2026-11"); wait_ready(page)
    nov = dt.datetime(2026, 11, 1)
    first = tmp / "ann-november.xlsx"
    own_layout(first, nov, [
        ["N/A", "N/A", "N/A", "General Time", "", "", 0, 2, 3],
        ["N/A", "N/A", "N/A", "General Time", "", "", 0, 1],          # a second General Time row: added together
        ["N/A", "N/A", "N/A", "Annual Leave"],                       # no hours: left out
        ["CENTRAL"],                                                 # a heading
        ["JOB009683", "DU", "80", "Wharfline log yard review", "REVEX", "", 0, 4, None, 2.5],
        ["", "", "", "J0010-DU-80 Tank repaint", "#VALUE!", "CAPEX", 0, None, 1, None, None, 6],
        ["J8298-CE-80", "", "", "Compliance studies", "", "", 0],    # a job with no hours: kept
    ])
    load_file(page, first)
    shown = grid(page)
    check("sheet: loads straight in when the month is empty", page.locator("#confirm").evaluate("d => !d.open"))
    check("sheet: jobs, CAPEX/REVEX (incl. Manually selected and a job number in the description)",
          any("JOB009683" in k and v == {"1": 4, "3": 2.5} for k, v in shown.items())
          and any("JOB000010" in k and v == {"2": 1, "5": 6} for k, v in shown.items()), json.dumps(shown))
    check("sheet: other time added up, empty rows and headings left out",
          any("General Time" in k and v == {"1": 3, "2": 3} for k, v in shown.items())
          and not any("Annual Leave" in k or "CENTRAL" in k for k in shown), json.dumps(shown))
    check("sheet: rows below the totals ignored", not any(v.get("4") == 99 or v.get("1") == 103 for v in shown.values()), json.dumps(shown))
    check("sheet: a job with no hours is kept", page.locator("#grid-wrap tbody tr", has_text="JOB008298").count() == 1)
    check("sheet: says what it loaded and to save", "Loaded ann-november.xlsx" in page.locator("#notices").inner_text()
          and "Unsaved" in page.locator("#save-status").inner_text())
    page.click("#save-btn")
    page.locator("#save-status").filter(has_text=re.compile("^Saved")).wait_for(timeout=30000)
    wait_for_action()
    check("sheet: saved like typed hours", saved_month("ann.example", "2026-11") == {
        "JOB009683/REVEX": {"1": 4, "3": 2.5}, "JOB000010/CAPEX": {"2": 1, "5": 6}, "General Time": {"1": 3, "2": 3}},
          json.dumps(saved_month("ann.example", "2026-11")))

    # The website's own workbook for that month, to load back later
    dctx, dpage = new_page(browser)
    sign_in(dpage, "dee.office", r"admin\.html")
    dpage.wait_for_selector("table.journals tbody tr", timeout=30000)
    jrow = dpage.locator("table.journals tbody tr", has_text="November 2026")
    jrow.locator("details.workbooks summary").click()
    with dpage.expect_download() as d:
        jrow.get_by_role("button", name="Ann Example").click()
    workbook = tmp / "website-workbook.xlsx"
    shutil.copy(d.value.path(), workbook)
    dctx.close()

    # Replacing: asks first; Cancel keeps it, Replace empties what the new sheet leaves empty
    second = tmp / "ann-november-2.xlsx"
    own_layout(second, nov, [["JOB009683", "DU", "80", "Wharfline log yard review", "REVEX", "", 0, None, 7]], total_row=False)
    load_file(page, second)
    check("replace: asks first", page.locator("#confirm").evaluate("d => d.open") and "Replace November 2026" in page.locator("#confirm").inner_text())
    page.click("#confirm-no"); page.wait_for_timeout(300)
    check("replace: Cancel keeps the month", grid(page) == shown)
    load_file(page, second); page.click("#confirm-yes"); page.wait_for_timeout(500)
    after = grid(page)
    check("replace: everything else goes, days left empty become empty",
          list(after.values()) == [{"2": 7}] and page.locator("#grid-wrap tbody tr", has_text="JOB000010").count() == 0, json.dumps(after))
    page.click("#save-btn")
    page.locator("#save-status").filter(has_text=re.compile("^Saved")).wait_for(timeout=30000)
    wait_for_action()

    wrong = tmp / "ann-september.xlsx"
    own_layout(wrong, dt.datetime(2026, 9, 1), [["JOB009683", "", "", "x", "REVEX", "", 0, 1]])
    load_file(page, wrong)
    check("wrong month: refused, with why", "is for September 2026, not November 2026" in page.locator("#toast").inner_text()
          and grid(page) == after)
    page.screenshot(path=str(SHOTS / "sheet-loaded.png"), full_page=True)
    ctx.close()

    # ---- The office: read-only until "Allow write access"
    ctx, page = new_page(browser)
    sign_in(page, "dee.office", r"admin\.html")
    page.goto(SITE_URL + "timesheet.html?month=2026-11&as=ann.example"); wait_ready(page)
    check("office: read-only, no spreadsheet button", page.locator("#sheet-btn").is_hidden() and page.locator("#save-btn").is_hidden()
          and page.locator("#grid-wrap input.cell").first.is_disabled())
    page.get_by_role("button", name="Allow write access").click()
    check("office: asks before allowing it", page.locator("#confirm").evaluate("d => d.open") and "Allow write access to Ann Example" in page.locator("#confirm").inner_text())
    page.click("#confirm-yes"); page.wait_for_timeout(500)
    check("office: can edit after allowing", page.locator("#sheet-btn").is_visible() and page.locator("#save-btn").is_visible()
          and not page.locator("#grid-wrap input.cell").first.is_disabled() and "Editing Ann Example" in page.locator("#notices").inner_text())
    load_file(page, workbook)
    page.click("#confirm-yes"); page.wait_for_timeout(500)
    check("workbook: the website's own workbook loads back the same", grid(page) == shown, json.dumps(grid(page)))
    cell = page.locator("#grid-wrap tbody tr", has_text="JOB009683").locator("input.cell[data-day='9']")
    cell.fill("1.5"); cell.press("Tab")
    page.click("#save-btn")
    page.locator("#save-status").filter(has_text=re.compile("^Saved")).wait_for(timeout=30000)
    wait_for_action()
    check("office: saved as Ann's copy, accepted by the Action", saved_month("ann.example", "2026-11") == {
        "JOB009683/REVEX": {"1": 4, "3": 2.5, "9": 1.5}, "JOB000010/CAPEX": {"2": 1, "5": 6}, "General Time": {"1": 3, "2": 3}},
          json.dumps(saved_month("ann.example", "2026-11")))
    page.get_by_role("button", name="Stop editing").click(); page.wait_for_timeout(500)
    check("office: Stop editing is read-only again", page.locator("#sheet-btn").is_hidden() and page.locator("#grid-wrap input.cell").first.is_disabled())
    page.screenshot(path=str(SHOTS / "office-writing.png"), full_page=True)
    ctx.close()

    ctx, page = new_page(browser)
    sign_in(page, "ann.example", r"home\.html")
    page.goto(SITE_URL + "timesheet.html?month=2026-11"); wait_ready(page)
    check("engineer: sees the office's change", any("JOB009683" in k and v == {"1": 4, "3": 2.5, "9": 1.5} for k, v in grid(page).items()), json.dumps(grid(page)))
    ctx.close()

    # A non-admin can't get write access to someone else's
    ctx, page = new_page(browser)
    sign_in(page, "ben.sample", r"home\.html")
    page.goto(SITE_URL + "timesheet.html?month=2026-11&as=ann.example"); page.wait_for_timeout(2500)
    check("engineer: ?as= for someone else goes to their own page", "as=" not in page.url)
    ctx.close()
    browser.close()

site_server.shutdown()
shutil.rmtree(tmp, ignore_errors=True)
print(f"{len(passed)} passed, {len(errors)} problem(s)")
for e in errors:
    print("PROBLEM:", e)
sys.exit(1 if errors else 0)
