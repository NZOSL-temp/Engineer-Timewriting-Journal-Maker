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
import datetime as dt
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
    sheet = next((t for t in (found[1]["timesheets"] if found else []) if t.get("username") == username and not t.get("deleted")), None)
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


def raw_xlsx(path, rows_xml, shared, *, prefix="", date1904=False, q='"', shared_name="sharedStrings.xml", first_sheet="Hours",
             styles=None, recalc=False):
    """An .xlsx written by hand, the way other programs write them: optional element prefixes
    (<x:row>), single-quoted attributes, the 1904 date system, a differently named shared-strings
    part. Its second sheet is a decoy timesheet, so reading the first sheet is what's tested."""
    import zipfile
    x = f"{prefix}:" if prefix else ""
    ns = f'xmlns{":" + prefix if prefix else ""}="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pr = f"<{x}workbookPr date1904={q}1{q}/>" if date1904 else ""
    calc = f"<{x}calcPr calcId={q}124519{q} fullCalcOnLoad={q}1{q}/>" if recalc else ""
    workbook = (f'<?xml version="1.0"?><{x}workbook {ns} xmlns:r="{rel}">{pr}<{x}sheets>'
                f"<{x}sheet name={q}{first_sheet}{q} sheetId={q}1{q} r:id={q}rId1{q}/>"
                f"<{x}sheet name={q}Decoy{q} sheetId={q}2{q} r:id={q}rId2{q}/></{x}sheets>{calc}</{x}workbook>")
    rels = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="rId2" Type="{rel}/worksheet" Target="/xl/worksheets/sheet2.xml"/>'
            f'<Relationship Id="rId3" Type="{rel}/sharedStrings" Target="{shared_name}"/>'
            f'<Relationship Id="rId4" Type="{rel}/styles" Target="styles.xml"/></Relationships>')
    root = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    sheet = lambda body: f'<?xml version="1.0"?><{x}worksheet {ns}><{x}sheetData>{body}</{x}sheetData></{x}worksheet>'
    decoy = f'<{x}row r="1"><{x}c r="A1" t="inlineStr"><{x}is><{x}t>Job Number</{x}t></{x}is></{x}c></{x}row>'
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("_rels/.rels", root)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet(rows_xml))
        z.writestr("xl/worksheets/sheet2.xml", sheet(decoy))
        z.writestr(f"xl/{shared_name}", f"<{x}sst {ns}>{''.join(shared)}</{x}sst>")
        if styles:
            z.writestr("xl/styles.xml", f'<?xml version="1.0"?><{x}styleSheet {ns}>{styles}</{x}styleSheet>')


def serial(day, date1904=False):
    import datetime as _dt
    return (_dt.date(2026, 11, day) - (_dt.date(1904, 1, 1) if date1904 else _dt.date(1899, 12, 30))).days


def read_in_page(page, path, month="2026-11"):
    """Run the website's reader on a file: {data, notes, name} or {error}."""
    return page.evaluate("""async ([b64, month]) => {
      const { readFirstSheet, timesheetFromSheet } = await import('./assets/xlsx.js');
      try {
        const sheet = await readFirstSheet(Uint8Array.from(atob(b64), (c) => c.charCodeAt(0)));
        const jobs = new Map([['JOB009683', { no: 'JOB009683', tasks: ['REVEX'], loc: 'DU', owner: '80', desc: 'Wharfline log yard review' }],
                              ['JOB000010', { no: 'JOB000010', tasks: ['CAPEX', 'REVEX'], loc: 'DU', owner: '80', desc: 'Tank repaint' }]]);
        return timesheetFromSheet(sheet, month, { jobs, otherTime: ['General Time', 'Training'] });
      } catch (e) { return { error: e.message }; }
    }""", [base64.b64encode(Path(path).read_bytes()).decode(), month])


def asking(page):
    """Whether the page's question box is open (it's only made the first time it's needed)."""
    box = page.locator("#confirm")
    return box.count() > 0 and box.evaluate("d => d.open")


def months_from_now(n):
    """"YYYY-MM" n months after this month (New timesheet offers this month and the next two)."""
    today = dt.date.today()
    y, m = divmod(today.year * 12 + today.month - 1 + n, 12)
    return f"{y}-{m + 1:02d}"


def label_of(month):
    return dt.date(int(month[:4]), int(month[5:]), 1).strftime("%B %Y")


def first_of(month):
    return dt.datetime(int(month[:4]), int(month[5:]), 1)


def new_timesheet(page, month=None, *, source=None, sheet=None):
    """On My timesheets: New timesheet, then Start with a copy of `source`, a spreadsheet, or empty."""
    page.click("#new-sheet"); page.wait_for_timeout(300)
    if sheet:
        with page.expect_file_chooser() as chooser:
            page.locator("#new-panel").get_by_role("button", name="Choose file…").click()
        chooser.value.set_files(str(sheet)); page.wait_for_timeout(1200)
    if month:
        page.select_option("#new-month", month)
    if source:
        page.check("input[name='start-with'][value='reuse']"); page.select_option("#new-source", source)
    elif not sheet:
        page.check("input[name='start-with'][value='empty']")


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



with sync_playwright() as p:
    browser = p.chromium.launch()

    # ---- The reader on spreadsheets written the ways other programs write them
    ctx, page = new_page(browser)
    page.goto(SITE_URL + "index.html")
    hours = lambda result, key: next((j["hours"].get(key[1], {}) for j in result.get("data", {}).get("jobs", []) if j["job"] == key[0]), None) \
        if isinstance(key, tuple) else next((o["hours"] for o in result.get("data", {}).get("other", []) if o["label"] == key), None)
    # Prefixed elements, cells and rows without positions, an empty self-closing row between rows,
    # an empty shared string, rich text and phonetic runs, text hours ("7,5", "7.5h", "AL"),
    # TRUE in a day cell, an other-time row starting with "Total", and rows below TOTAL HOURS.
    sh = ["<x:si/>", "<x:si><x:t>Job Number</x:t></x:si>", "<x:si><x:t>Description</x:t></x:si>",
          "<x:si><x:t>Capex/Revex</x:t></x:si>", "<x:si><x:t>N/A</x:t></x:si>",
          "<x:si><x:r><x:t>General </x:t></x:r><x:r><x:t>Time</x:t></x:r><x:rPh><x:t>ignored</x:t></x:rPh></x:si>",
          "<x:si><x:t>CAPEX</x:t></x:si>", "<x:si><x:t>Total Safety Training</x:t></x:si>", "<x:si><x:t>TOTAL HOURS</x:t></x:si>"]
    c = lambda v, t=None: f"<x:c{' t=' + chr(34) + t + chr(34) if t else ''}><x:v>{v}</x:v></x:c>"
    istr = lambda v: f'<x:c t="inlineStr"><x:is><x:t>{v}</x:t></x:is></x:c>'
    body = (f'<x:row r="1">{istr("NAME")}{istr("Ann Example")}</x:row>'
            '<x:row r="2" spans="1:6" ht="20" customHeight="1"/>'
            f'<x:row r="3">{c(1, "s")}{c(2, "s")}{c(3, "s")}{c(serial(1))}{c(serial(2))}{c(serial(3))}</x:row>'
            f'<x:row>{c(4, "s")}{c(5, "s")}<x:c/>{c(2)}{istr("7,5")}{istr("AL")}</x:row>'
            '<x:row spans="1:6"/>'
            f'<x:row>{istr("J0010-DU-80")}{istr("Tank repaint")}{c(6, "s")}{istr("7.5h")}{c(1, "b")}{c(1)}</x:row>'
            f'<x:row>{c(4, "s")}{c(7, "s")}<x:c/>{c(1)}</x:row>'
            f'<x:row>{c(0, "s")}{c(8, "s")}</x:row>'
            f'<x:row>{istr("JOB009683")}{istr("below the totals")}<x:c/>{c(99)}</x:row>')
    raw_xlsx(tmp / "odd.xlsx", body, sh, prefix="x", q="'", shared_name="SharedStrings.xml")
    got = read_in_page(page, tmp / "odd.xlsx")
    check("reader: prefixed tags, cells without positions, empty rows, odd shared strings",
          hours(got, ("JOB000010", "CAPEX")) == {"1": 7.5, "3": 1} and hours(got, "General Time") == {"1": 2, "2": 7.5}, json.dumps(got)[:400])
    check("reader: an other-time row starting with Total is kept; rows below TOTAL HOURS aren't",
          hours(got, "Total Safety Training") == {"1": 1} and hours(got, ("JOB009683", "REVEX")) is None, json.dumps(got)[:400])
    check("reader: text that isn't hours is listed, TRUE isn't an hour", any("“AL”" in n and "General Time, 3 Nov" in n for n in got.get("notes", [])),
          json.dumps(got.get("notes")))
    check("reader: the NAME row", got.get("name") == "Ann Example")
    # The 1904 date system, "Project No.", a job in the description when the job cell says N/A,
    # one open task (no guess needed)
    body = (f'<row r="1"><c r="A1" t="inlineStr"><is><t>Project No.</t></is></c><c r="B1" t="inlineStr"><is><t>Description</t></is></c>'
            f'<c r="D1"><v>{serial(1, True)}</v></c><c r="E1"><v>{serial(2, True)}</v></c></row>'
            '<row r="2"><c r="A2" t="inlineStr"><is><t>N/A</t></is></c><c r="B2" t="inlineStr"><is><t>J9683-DU-80 Wharfline</t></is></c>'
            '<c r="E2"><v>3.25</v></c></row>')
    raw_xlsx(tmp / "mac.xlsx", body, [], date1904=True)
    got = read_in_page(page, tmp / "mac.xlsx")
    check("reader: 1904 dates, Project No., job in the description", hours(got, ("JOB009683", "REVEX")) == {"2": 3.25}
          and not any("CAPEX or REVEX" in n for n in got.get("notes", [])), json.dumps(got)[:400])
    # Dates typed as text (day first) and an ISO date cell
    body = ('<row r="1"><c r="A1" t="inlineStr"><is><t>Job Number</t></is></c><c r="B1" t="inlineStr"><is><t>Description</t></is></c>'
            '<c r="C1" t="inlineStr"><is><t>1/11/2026</t></is></c><c r="D1" t="d"><v>2026-11-02T00:00:00</v></c></row>'
            '<row r="2"><c r="A2" t="inlineStr"><is><t>JOB000010</t></is></c><c r="C2"><v>2</v></c><c r="D2"><v>4</v></c></row>')
    raw_xlsx(tmp / "textdates.xlsx", body, [])
    got = read_in_page(page, tmp / "textdates.xlsx")
    check("reader: text and ISO dates; both tasks open so REVEX is guessed and said",
          hours(got, ("JOB000010", "REVEX")) == {"1": 2, "2": 4} and any("CAPEX or REVEX" in n for n in got.get("notes", [])), json.dumps(got)[:400])
    # Only the first sheet counts
    raw_xlsx(tmp / "firstnot.xlsx", '<row r="1"><c r="A1" t="inlineStr"><is><t>Some notes</t></is></c></row>', [], first_sheet="Notes")
    got = read_in_page(page, tmp / "firstnot.xlsx")
    check("reader: only the first sheet is read", "first sheet (“Notes”)" in (got.get("error") or ""), json.dumps(got))
    # Hours shown as times (a built-in and a custom [h]:mm style, and "7:30" typed as text), a row
    # with hours but no job number, a row whose own total doesn't match its days, and a formula
    # total a program never worked out (no false alarm).
    styles = ('<numFmts count="1"><numFmt numFmtId="170" formatCode="[h]:mm"/></numFmts>'
              '<cellXfs count="3"><xf numFmtId="0"/><xf numFmtId="20" applyNumberFormat="1"/><xf numFmtId="170" applyNumberFormat="1"/></cellXfs>')
    t = lambda ref, v: f'<c r="{ref}" t="inlineStr"><is><t>{v}</t></is></c>'
    body = (f'<row r="1">{t("A1", "Job Number")}{t("B1", "Description")}{t("C1", "Total Hours")}'
            f'<c r="D1"><v>{serial(1)}</v></c><c r="E1"><v>{serial(2)}</v></c><c r="F1"><v>{serial(3)}</v></c></row>'
            f'<row r="2">{t("A2", "JOB009683")}<c r="C2"><f>SUM(D2:F2)</f><v>16</v></c>'
            f'<c r="D2" s="1"><v>0.3125</v></c><c r="E2" s="2"><v>0.25</v></c>{t("F2", "2:30")}</row>'
            f'<row r="3">{t("A3", "DU")}{t("B3", "Somewhere")}<c r="D3"><v>3</v></c></row>'
            f'<row r="4">{t("A4", "N/A")}{t("B4", "Training")}<c r="C4"><v>10</v></c><c r="D4"><v>4</v></c><c r="E4"><v>5</v></c></row>')
    raw_xlsx(tmp / "times.xlsx", body, [], styles=styles)
    got = read_in_page(page, tmp / "times.xlsx")
    notes = " ".join(got.get("notes", []))
    check("reader: hours shown as times (7:30 is 7.5 h), built-in, [h]:mm and typed",
          hours(got, ("JOB009683", "REVEX")) == {"1": 7.5, "2": 6, "3": 2.5}, json.dumps(got)[:400])
    check("reader: a row with hours but no job number is reported, not lost", "row 3 (“DU – Somewhere”), 3 h" in notes, notes)
    check("reader: a row whose total doesn't match its days is reported", "row 4 (“Training”): its total is 10 h, but its days in November add up to 9 h" in notes
          and "row 2" not in notes.split("match the days read")[-1].split(".")[0], notes)
    body = (f'<row r="1">{t("A1", "Job Number")}{t("B1", "Total Hours")}<c r="C1"><v>{serial(1)}</v></c></row>'
            f'<row r="2">{t("A2", "JOB009683")}<c r="B2"><f>SUM(C2)</f><v>0</v></c><c r="C2"><v>5</v></c></row>')
    raw_xlsx(tmp / "unworked-total.xlsx", body, [], recalc=True)
    got = read_in_page(page, tmp / "unworked-total.xlsx")
    check("reader: a never-worked-out formula total isn't a false alarm", hours(got, ("JOB009683", "REVEX")) == {"1": 5}
          and not any("match the days" in n for n in got.get("notes", [])), json.dumps(got)[:300])
    body = (f'<row r="1">{t("A1", "Job Number")}<c r="B1"><v>{serial(1)}</v></c></row>'
            f'<row r="2">{t("A2", "JOB009683")}<c r="B2"><f>4+1</f></c></row>')
    raw_xlsx(tmp / "unworked.xlsx", body, [], recalc=True)
    check("reader: hours that are formulas never worked out: asks to save it in Excel first",
          "Open it in Excel, save it" in (read_in_page(page, tmp / "unworked.xlsx").get("error") or ""))
    (tmp / "not.xlsx").write_bytes(b"%PDF-1.4 not a spreadsheet")
    check("reader: a file that isn't a spreadsheet", "isn’t an Excel" in (read_in_page(page, tmp / "not.xlsx").get("error") or ""))
    ctx.close()

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
    check("sheet: loads straight in when the month is empty", not asking(page))
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
    check("sheet: the loaded message goes once saved", "Loaded ann-november.xlsx" not in page.locator("#notices").inner_text())
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
    second = tmp / "ann-november-2.xlsm"  # a macro workbook is fine too
    own_layout(second, nov, [["JOB009683", "DU", "80", "Wharfline log yard review", "REVEX", "", 0, None, 7]], total_row=False)
    load_file(page, second)
    check("replace: asks first", asking(page) and "Replace November 2026" in page.locator("#confirm").inner_text())
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

    # New timesheet › My spreadsheet: the month comes from the file, and it opens filled in
    nxt = months_from_now(1)
    mine = tmp / "ann-next-month.xlsx"
    own_layout(mine, first_of(nxt), [["JOB009683", "DU", "80", "Wharfline log yard review", "REVEX", "", 0, 3, None, 1.5],
                                      ["N/A", "N/A", "N/A", "Training", "", "", 0, None, 2]], total_row=False)
    page.goto(SITE_URL + "home.html"); page.wait_for_selector("#new-sheet:not([hidden])", timeout=30000); page.wait_for_timeout(1500)
    new_timesheet(page, sheet=wrong)
    status = page.locator(".new-file-status")
    check("new from sheet: a sheet for a month that can't be started says so", "is for September 2026" in status.inner_text()
          and page.locator("input[name='start-with'][value='sheet']").is_checked(), status.inner_text())
    page.get_by_role("button", name="Start timesheet").click(); page.wait_for_timeout(500)
    check("new from sheet: ...and Start explains rather than opening", "home.html" in page.url and "is for September 2026, not" in status.inner_text(), status.inner_text())
    page.locator("#new-panel").get_by_role("button", name="Cancel").click()
    new_timesheet(page, sheet=mine)
    page.locator("#new-panel").screenshot(path=str(SHOTS / "new-panel.png"))
    page.set_viewport_size({"width": 390, "height": 844}); page.wait_for_timeout(300)
    check("phone: the New timesheet panel fits", page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
    page.locator("#new-panel").screenshot(path=str(SHOTS / "new-panel-phone.png"))
    page.set_viewport_size({"width": 1280, "height": 900}); page.wait_for_timeout(200)
    check("new from sheet: the month is picked from the file", page.locator("#new-month").input_value() == nxt
          and f"for {label_of(nxt)}" in status.inner_text(), f"{page.locator('#new-month').input_value()} {status.inner_text()}")
    page.get_by_role("button", name="Start timesheet").click()
    page.wait_for_url(re.compile(rf"timesheet\.html\?month={nxt}"), timeout=30000); wait_ready(page)
    filled = grid(page)
    check("new from sheet: opens filled in, ready to save", any("JOB009683" in k and v == {"1": 3, "3": 1.5} for k, v in filled.items())
          and any("Training" in k and v == {"2": 2} for k, v in filled.items()) and "Loaded ann-next-month.xlsx" in page.locator("#notices").inner_text()
          and "Unsaved" in page.locator("#save-status").inner_text(), json.dumps(filled))
    page.click("#save-btn")
    page.locator("#save-status").filter(has_text=re.compile("^Saved")).wait_for(timeout=30000)
    wait_for_action()
    check("new from sheet: saved", saved_month("ann.example", nxt) == {"JOB009683/REVEX": {"1": 3, "3": 1.5}, "Training": {"2": 2}},
          json.dumps(saved_month("ann.example", nxt)))
    page.set_viewport_size({"width": 390, "height": 844}); page.wait_for_timeout(400)
    check("phone: the save bar fits (Load from spreadsheet included)", page.evaluate(
        "() => { const bar = document.querySelector('#savebar'); return bar.scrollWidth <= bar.clientWidth + 1 && document.documentElement.scrollWidth <= innerWidth; }")
          and page.locator("#sheet-btn").is_visible())
    page.screenshot(path=str(SHOTS / "sheet-phone.png"))
    ctx.close()

    # ---- The office: read-only until "Allow write access"
    ctx, page = new_page(browser)
    sign_in(page, "dee.office", r"admin\.html")
    page.goto(SITE_URL + "timesheet.html?month=2026-11&as=ann.example"); wait_ready(page)
    check("office: read-only, no spreadsheet button", page.locator("#sheet-btn").is_hidden() and page.locator("#save-btn").is_hidden()
          and page.locator("#grid-wrap input.cell").first.is_disabled())
    notice = page.locator("#notices")
    check("office: Allow write access and Back to Admin offered", notice.get_by_role("button", name="Allow write access").is_visible()
          and notice.get_by_role("button", name="Back to Admin").is_visible())
    page.get_by_role("button", name="Allow write access").click()
    check("office: asks before allowing it", asking(page) and "Allow write access to Ann Example" in page.locator("#confirm").inner_text())
    page.click("#confirm-yes"); page.wait_for_timeout(500)
    check("office: can edit after allowing", page.locator("#sheet-btn").is_visible() and page.locator("#save-btn").is_visible()
          and not page.locator("#grid-wrap input.cell").first.is_disabled() and "Editing Ann Example" in page.locator("#notices").inner_text()
          and notice.get_by_role("button", name="Stop editing").is_visible() and notice.get_by_role("button", name="Back to Admin").is_visible())
    load_file(page, workbook)
    page.click("#confirm-yes"); page.wait_for_timeout(500)
    check("workbook: the website's own workbook loads back the same", grid(page) == shown, json.dumps(grid(page)))
    cell = page.locator("#grid-wrap tbody tr", has_text="JOB009683").locator("input.cell[data-day='9']")
    cell.fill("1.5"); cell.press("Tab")
    page.click("#next-month"); page.wait_for_timeout(300)
    check("office: leaving a month with unsaved changes asks", asking(page)
          and "Leave November 2026 without saving" in page.locator("#confirm").inner_text())
    page.click("#confirm-no"); page.wait_for_timeout(300)
    check("office: ...and Cancel stays, changes kept", "November 2026" in page.locator("#month-label").inner_text() and cell.input_value() == "1.5")
    page.click("#save-btn")
    page.locator("#save-status").filter(has_text=re.compile("^Saved")).wait_for(timeout=30000)
    wait_for_action()
    check("office: saved as Ann's copy, accepted by the Action", saved_month("ann.example", "2026-11") == {
        "JOB009683/REVEX": {"1": 4, "3": 2.5, "9": 1.5}, "JOB000010/CAPEX": {"2": 1, "5": 6}, "General Time": {"1": 3, "2": 3}},
          json.dumps(saved_month("ann.example", "2026-11")))
    page.get_by_role("button", name="Stop editing").click(); page.wait_for_timeout(500)
    check("office: Stop editing is read-only again", page.locator("#sheet-btn").is_hidden() and page.locator("#grid-wrap input.cell").first.is_disabled())
    page.screenshot(path=str(SHOTS / "office-writing.png"), full_page=True)
    notice.get_by_role("button", name="Back to Admin").click()
    page.wait_for_url(re.compile(r"admin\.html"), timeout=15000)
    check("office: Back to Admin goes back", page.url.endswith("admin.html"))
    ctx.close()

    # ---- The office on Ann's My timesheets: everything she can do, once allowed
    ctx, page = new_page(browser)
    sign_in(page, "dee.office", r"admin\.html")
    page.goto(SITE_URL + "home.html?as=ann.example"); page.wait_for_selector("#sheets table", timeout=30000)
    notice = page.locator("#notices")
    check("office home: read-only at first", page.locator("#new-sheet").is_hidden()
          and page.locator("#sheets").get_by_role("button", name=re.compile("^Delete")).count() == 0
          and notice.get_by_role("button", name="Allow write access").is_visible() and notice.get_by_role("link", name="Back to Admin").is_visible())
    notice.get_by_role("button", name="Allow write access").click(); page.wait_for_timeout(300)
    check("office home: asks first, saying what it allows", asking(page)
          and "start new ones" in page.locator("#confirm").inner_text())
    page.click("#confirm-yes"); page.wait_for_timeout(500)
    check("office home: New timesheet, Open and Delete once allowed", page.locator("#new-sheet").is_visible()
          and page.locator("#sheets").get_by_role("button", name="Delete November 2026").is_visible()
          and "Editing Ann Example" in notice.inner_text() and notice.get_by_role("button", name="Stop editing").is_visible())
    later = months_from_now(2)
    new_timesheet(page, later, source="2026-11")
    page.get_by_role("button", name="Start timesheet").click()
    page.wait_for_url(re.compile(rf"timesheet\.html\?month={later}"), timeout=30000); wait_ready(page)
    check("office: write access carries to the month (no second question)", not asking(page)
          and "Editing Ann Example" in page.locator("#notices").inner_text() and page.locator("#save-btn").is_visible())
    check("office: the job template was copied from November", page.locator("#grid-wrap tbody tr", has_text="JOB000010").count() >= 1
          and page.locator("#grid-wrap tbody tr", has_text="JOB008298").count() == 1 and not grid(page), json.dumps(grid(page)))
    cell = page.locator("#grid-wrap tbody tr", has_text="JOB009683").locator("input.cell[data-day='4']")
    cell.fill("6"); cell.press("Tab")
    page.click("#save-btn")
    page.locator("#save-status").filter(has_text=re.compile("^Saved")).wait_for(timeout=30000)
    check("office: the new month is saved as Ann's (straight away, before the Action)",
          any(i["title"].startswith(f"Timesheet {later} ") for i in issues))
    page.goto(SITE_URL + "home.html?as=ann.example"); page.wait_for_selector("#sheets table", timeout=30000)
    row = page.locator("#sheets tbody tr", has_text=label_of(later))
    check("office home: the new month is listed straight away, write access still on", row.count() == 1 and page.locator("#new-sheet").is_visible())
    wait_for_action()
    check("office: accepted by the Action as Ann's (with the template's other-time row)",
          saved_month("ann.example", later) == {"JOB009683/REVEX": {"4": 6}, "General Time": {}}, json.dumps(saved_month("ann.example", later)))
    row.get_by_role("button", name=f"Delete {label_of(later)}").click()
    page.locator("tr.confirm-row").get_by_role("button", name=f"Delete {label_of(later)}").click()
    page.locator("#toast", has_text="Deleted").wait_for(timeout=30000)
    wait_for_action()
    check("office home: deletes a month as Ann", saved_month("ann.example", later) is None and row.count() == 0)
    theirs = tmp / "ann-later.xlsx"
    own_layout(theirs, first_of(later), [["J0010-DU-80", "", "", "Tank repaint", "CAPEX", "", 0, None, None, 2]], total_row=False)
    new_timesheet(page, sheet=theirs)
    check("office home: Their spreadsheet, month picked from it", page.locator("#new-month").input_value() == later
          and "Their spreadsheet" in page.locator("#new-panel").inner_text())
    page.get_by_role("button", name="Start timesheet").click()
    page.wait_for_url(re.compile(rf"timesheet\.html\?month={later}"), timeout=30000); wait_ready(page)
    page.click("#save-btn")
    page.locator("#save-status").filter(has_text=re.compile("^Saved")).wait_for(timeout=30000)
    wait_for_action()
    check("office: a month started from their spreadsheet is saved as Ann's", saved_month("ann.example", later) == {"JOB000010/CAPEX": {"3": 2}},
          json.dumps(saved_month("ann.example", later)))
    page.goto(SITE_URL + "home.html?as=ann.example"); page.wait_for_selector("#sheets table", timeout=30000)
    page.locator("#notices").get_by_role("button", name="Stop editing").click(); page.wait_for_timeout(300)
    check("office home: Stop editing is read-only again", page.locator("#new-sheet").is_hidden()
          and page.locator("#sheets").get_by_role("button", name=re.compile("^Delete")).count() == 0)
    page.goto(SITE_URL + f"timesheet.html?month={later}&as=ann.example"); wait_ready(page)
    check("office: ...on her months too", page.locator("#save-btn").is_hidden() and page.locator("#grid-wrap input.cell").first.is_disabled())
    page.screenshot(path=str(SHOTS / "office-home.png"), full_page=True)
    ctx.close()

    # The office doesn't silently replace a save of Ann's it hasn't seen
    octx, opage = new_page(browser)
    sign_in(opage, "dee.office", r"admin\.html")
    opage.goto(SITE_URL + f"timesheet.html?month={nxt}&as=ann.example"); wait_ready(opage)
    opage.locator("#notices").get_by_role("button", name="Allow write access").click(); opage.click("#confirm-yes"); opage.wait_for_timeout(400)
    actx, apage = new_page(browser)
    sign_in(apage, "ann.example", r"home\.html")
    apage.goto(SITE_URL + f"timesheet.html?month={nxt}"); wait_ready(apage)
    acell = apage.locator("#grid-wrap tbody tr", has_text="JOB009683").locator("input.cell[data-day='10']")
    acell.fill("2"); acell.press("Tab"); apage.click("#save-btn")
    apage.locator("#save-status").filter(has_text=re.compile("^Saved")).wait_for(timeout=30000)
    actx.close()
    ocell = lambda day: opage.locator("#grid-wrap tbody tr", has_text="JOB009683").locator(f"input.cell[data-day='{day}']")
    ocell(11).fill("1"); ocell(11).press("Tab"); opage.click("#save-btn"); opage.wait_for_timeout(800)
    check("clash: the office is asked before replacing a newer save of Ann's", asking(opage)
          and "Ann saved" in opage.locator("#confirm").inner_text() and "Save over it" in opage.locator("#confirm").inner_text())
    opage.click("#confirm-no"); opage.wait_for_timeout(400)
    check("clash: Cancel saves nothing and offers her save", "Unsaved" in opage.locator("#save-status").inner_text()
          and opage.locator("#notices").get_by_role("button", name="Show Ann’s save").is_visible())
    opage.locator("#notices").get_by_role("button", name="Show Ann’s save").click(); opage.wait_for_timeout(400)
    check("clash: her save is shown", ocell(10).input_value() == "2" and ocell(11).input_value() == "")
    ocell(12).fill("1"); ocell(12).press("Tab"); opage.click("#save-btn")
    opage.locator("#save-status").filter(has_text=re.compile("^Saved")).wait_for(timeout=30000)
    check("clash: then saving on top of it doesn't ask again", not asking(opage))
    wait_for_action()
    check("clash: both changes kept", saved_month("ann.example", nxt) == {"JOB009683/REVEX": {"1": 3, "3": 1.5, "10": 2, "12": 1}, "Training": {"2": 2}},
          json.dumps(saved_month("ann.example", nxt)))
    octx.close()

    ctx, page = new_page(browser)
    sign_in(page, "ann.example", r"home\.html")
    page.wait_for_selector("#sheets table", timeout=30000); page.wait_for_timeout(1500)
    check("engineer: sees the month the office started", page.locator("#sheets tbody tr", has_text=label_of(later)).count() == 1)
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
