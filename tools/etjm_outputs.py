"""Build the files the office uses from the saved timesheets.

For each month this makes:

* Timewriting Journal - Sep 2026.csv / .xlsx  (the journal uploaded to Business Central)
* Engineer timewriting - <name> - September 2026.xlsx  (one per engineer, old layout)
* README.md  (a summary with checks, shown by GitHub when you open the folder)

Everything is a pure function of the timesheets and settings, and the xlsx files are
written with fixed timestamps, so rebuilding unchanged data gives identical files.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import io
import json
import urllib.parse
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from etjm_core import (
    MONTH_ABBR,
    MONTH_NAMES,
    Timesheet,
    days_in_month,
    fmt_number,
    month_end,
    month_title,
    month_tokens,
    name_key,
    recharge_rate,
    safe_file_part,
    slugify,
)

JOURNAL_HEADER = ["Employee - ID", "Date", "GST inclusive ", "GST", "Excl", "GL", "Project",
                  "Description", "Capex/Revex", "No. of hours", "Recharge Rate", "Document number"]
YELLOW_COLUMNS = {"Project", "Capex/Revex", "No. of hours"}
ACCOUNTING = '_-* #,##0.00_-;\\-* #,##0.00_-;_-* "-"??_-;_-@_-'
HOURS_FORMAT = '0.##;-0.##;""'
CENT = Decimal("0.01")
EXCEL_EPOCH = dt.date(1899, 12, 30)


@dataclass
class JournalLine:
    employee: str
    date: dt.date
    amount: Decimal                 # "GST inclusive" and "Excl" (GST is always 0)
    description: str
    document: str
    project: str = ""
    task: str = ""
    hours: Decimal | None = None
    rate: Decimal | None = None
    gl: str = ""
    balancing: bool = False


def _format(template: str, what: str, **tokens) -> str:
    try:
        return template.format(**tokens)
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(f"settings.json: journal.{what} has an unknown placeholder ({exc}).") from exc


def build_journal(timesheets: list[Timesheet], settings: dict, year: int, month: int):
    """One line per job and Capex/Revex with hours, then a balancing line per engineer."""
    cfg = settings["journal"]
    date = month_end(year, month)
    tokens = month_tokens(settings, year, month)
    document = _format(cfg["documentNumber"], "documentNumber", employee="", **tokens)
    lines: list[JournalLine] = []
    warnings: list[str] = []
    for ts in sorted(timesheets, key=lambda t: name_key(t.employee)):
        worked = [line for line in ts.jobs if line.total > 0]
        if not worked:
            continue
        rate = recharge_rate(settings, ts.employee)
        if rate is None:
            warnings.append(f"{ts.employee} has no rechargeRate in settings.json, so their lines are $0.")
            rate = Decimal(0)
        description = _format(cfg["description"], "description", employee=ts.employee, **tokens)
        total = Decimal(0)
        for line in worked:
            amount = (rate * line.total).quantize(CENT, ROUND_HALF_UP)
            total += amount
            lines.append(JournalLine(ts.employee, date, amount, description, document,
                                     project=line.job, task=line.task, hours=line.total, rate=rate))
        lines.append(JournalLine(ts.employee, date, -total, description, document,
                                 gl=str(cfg["balancingGlAccount"]), balancing=True))
    return lines, warnings


def money(value: Decimal) -> str:
    value = Decimal(value).quantize(CENT, ROUND_HALF_UP)
    return f"{(value if value != 0 else Decimal('0.00')):.2f}"


def journal_csv(lines: list[JournalLine], settings: dict) -> bytes:
    cfg = settings["journal"]
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(JOURNAL_HEADER)
    for line in lines:
        writer.writerow([
            line.employee,
            line.date.strftime(cfg["dateFormat"]),
            money(line.amount),
            money(Decimal(0)),
            money(line.amount),
            line.gl,
            line.project,
            line.description,
            line.task,
            "" if line.hours is None else fmt_number(line.hours),
            "" if line.rate is None else fmt_number(line.rate),
            line.document,
        ])
    data = buffer.getvalue().encode("utf-8")
    return b"\xef\xbb\xbf" + data if cfg.get("csvUtf8Bom", True) else data


# ------------------------------------------------------------------ workbooks

def _workbook(created: dt.datetime, title: str):
    import xlsxwriter

    buffer = io.BytesIO()
    workbook = xlsxwriter.Workbook(buffer, {
        "in_memory": True,
        "strings_to_formulas": False,
        "strings_to_numbers": False,
        "strings_to_urls": False,
    })
    workbook.set_properties({"title": title, "author": "Engineer Timewriting Journal Maker",
                             "created": created})
    return workbook, buffer


def _serial(date: dt.date) -> int:
    return (date - EXCEL_EPOCH).days


def _gl_value(gl: str):
    return int(gl) if gl.isdigit() else gl


def journal_xlsx(lines: list[JournalLine], settings: dict, year: int, month: int,
                 created: dt.datetime) -> bytes:
    """The journal laid out like "Timewriting Journal - Sep 2026.xlsx", with live formulas."""
    workbook, buffer = _workbook(created, f"Timewriting Journal - {MONTH_ABBR[month - 1]} {year}")
    f = workbook.add_format
    head = {"bold": True, "border": 1, "valign": "vcenter", "text_wrap": True}
    fmt = {
        "head": f(head),
        "head_yellow": f({**head, "bg_color": "#FFFF00"}),
        "date": f({"num_format": "d/mm/yyyy"}),
        "money": f({"num_format": ACCOUNTING}),
        "hours_yellow": f({"num_format": ACCOUNTING, "bg_color": "#FFFF00"}),
        "yellow": f({"bg_color": "#FFFF00"}),
        "input_date": f({"num_format": "d/mm/yyyy", "bg_color": "#FFFF00", "bold": True}),
        "bold": f({"bold": True}),
    }
    date_value = _serial(month_end(year, month))

    sheet = workbook.add_worksheet("Input")
    sheet.write_string(0, 0, "Date", fmt["bold"])
    sheet.write_number(0, 1, date_value, fmt["input_date"])
    sheet.set_column(0, 0, 8)
    sheet.set_column(1, 1, 12)

    sheet = workbook.add_worksheet("Journal")
    sheet.activate()
    sheet.set_first_sheet()
    for col, name in enumerate(JOURNAL_HEADER):
        sheet.write_string(0, col, name, fmt["head_yellow" if name in YELLOW_COLUMNS else "head"])
    for col, width in enumerate([18, 11, 14, 9, 14, 7, 12, 34, 12, 12, 13, 22]):
        sheet.set_column(col, col, width)
    sheet.freeze_panes(1, 0)

    first = 2  # Excel row number of the current engineer's first line
    for index, line in enumerate(lines):
        r = index + 1        # 0-based row for xlsxwriter
        x = r + 1            # Excel row number
        sheet.write_string(r, 0, line.employee)
        sheet.write_formula(r, 1, "=Input!$B$1", fmt["date"], date_value)
        if line.balancing:
            sheet.write_formula(r, 2, f"=-SUM(C{first}:C{x - 1})", fmt["money"], float(line.amount))
            sheet.write_formula(r, 3, f"=-SUM(D{first}:D{x - 1})", fmt["money"], 0)
            sheet.write_formula(r, 4, f"=-SUM(E{first}:E{x - 1})", fmt["money"], float(line.amount))
            gl = _gl_value(line.gl)
            (sheet.write_number if isinstance(gl, int) else sheet.write_string)(r, 5, gl)
            sheet.write_blank(r, 6, None, fmt["yellow"])
            sheet.write_blank(r, 8, None, fmt["yellow"])
            sheet.write_blank(r, 9, None, fmt["yellow"])
            first = x + 1
        else:
            sheet.write_formula(r, 2, f"=K{x}*J{x}", fmt["money"], float(line.amount))
            sheet.write_number(r, 3, 0, fmt["money"])
            sheet.write_formula(r, 4, f"=C{x}", fmt["money"], float(line.amount))
            sheet.write_string(r, 6, line.project, fmt["yellow"])
            sheet.write_string(r, 8, line.task, fmt["yellow"])
            sheet.write_number(r, 9, float(line.hours), fmt["hours_yellow"])
            sheet.write_number(r, 10, float(line.rate))
        sheet.write_string(r, 7, line.description)
        sheet.write_string(r, 11, line.document)

    workbook.close()
    return buffer.getvalue()


def engineer_xlsx(ts: Timesheet, jobs_index: dict, created: dt.datetime) -> bytes:
    """One engineer's month, laid out like the old "Timewriting 1-For Engineer" sheet."""
    year, month = ts.year, ts.month
    days = days_in_month(year, month)
    workbook, buffer = _workbook(created, f"Engineer timewriting - {ts.employee} - {month_title(year, month)}")
    f = workbook.add_format
    border = {"border": 1, "border_color": "#BFBFBF"}
    weekend = {"bg_color": "#EDEDED"}
    fmt = {
        "label": f({"bold": True}),
        "name": f({"bold": True, "font_size": 12}),
        "month": f({"bold": True, "num_format": "mmmm yyyy", "align": "left"}),
        "head": f({"bold": True, "bg_color": "#D9E1F2", "text_wrap": True, "valign": "vcenter", **border}),
        "head_day": f({"bold": True, "bg_color": "#D9E1F2", "num_format": "ddd d", "align": "center", **border}),
        "head_weekend": f({"bold": True, "bg_color": "#C9D3E6", "num_format": "ddd d", "align": "center", **border}),
        "text": f(border),
        "check": f({"font_color": "#C00000", **border}),
        "total": f({"bold": True, "num_format": HOURS_FORMAT, "align": "center", **border}),
        "hours": f({"num_format": HOURS_FORMAT, "align": "center", **border}),
        "hours_weekend": f({"num_format": HOURS_FORMAT, "align": "center", **weekend, **border}),
        "daily": f({"bold": True, "num_format": "0.##", "align": "center", "bg_color": "#F2F2F2", **border}),
        "daily_label": f({"bold": True, "bg_color": "#F2F2F2", **border}),
        "section": f({"bold": True, "italic": True, "font_color": "#595959"}),
    }
    sheet = workbook.add_worksheet("Timewriting 1-For Engineer")

    first_day_col = 7  # column H, as in the old sheet
    last_day_col = first_day_col + days - 1
    weekend_days = {d for d in range(1, days + 1) if dt.date(year, month, d).weekday() >= 5}

    sheet.write_string(0, 0, "NAME", fmt["label"])
    sheet.write_string(0, 1, ts.employee, fmt["name"])
    sheet.write_string(1, 0, "MONTH", fmt["label"])
    sheet.write_number(1, 1, _serial(dt.date(year, month, 1)), fmt["month"])

    headers = ["Job Number", "Location", "Owner", "Description", "Capex/Revex", "Check", "Total Hours"]
    for col, name in enumerate(headers):
        sheet.write_string(4, col, name, fmt["head"])
    for day in range(1, days + 1):
        style = fmt["head_weekend" if day in weekend_days else "head_day"]
        sheet.write_number(4, first_day_col + day - 1, _serial(dt.date(year, month, day)), style)

    rows = []  # (cells A..F, hours dict)
    for line in ts.other:
        rows.append((["N/A", "N/A", "N/A", line.label, "", ""], line.hours))
    for line in ts.jobs:  # including jobs with no hours, as in the engineers' own spreadsheet
        info = jobs_index.get(line.job)
        check = ""
        if jobs_index:
            if info is None:
                check = "Not an open job"
            elif line.total > 0 and line.task not in info["tasks"]:
                check = f"{line.task} not open"
        location = line.location or (info or {}).get("loc", "")
        owner = line.owner or (info or {}).get("owner", "")
        description = line.description or (info or {}).get("desc", "")
        task = line.task or "/".join((info or {}).get("tasks") or [])  # no hours: what the job is open for
        rows.append(([line.job, location, owner, description, task, check], line.hours))

    top = 5  # first data row (0-based), Excel row 6
    for i, (cells, hours) in enumerate(rows):
        r = top + i
        for col, value in enumerate(cells):
            sheet.write_string(r, col, value, fmt["check" if col == 5 and value else "text"])
        total = sum(hours.values(), Decimal(0))
        sheet.write_formula(r, 6, f"=SUM({_col(first_day_col)}{r + 1}:{_col(last_day_col)}{r + 1})",
                            fmt["total"], float(total))
        for day in range(1, days + 1):
            style = fmt["hours_weekend" if day in weekend_days else "hours"]
            value = hours.get(day)
            if value:
                sheet.write_number(r, first_day_col + day - 1, float(value), style)
            else:
                sheet.write_blank(r, first_day_col + day - 1, None, style)

    totals_row = top + len(rows)
    day_totals = ts.day_totals()
    grand = sum(day_totals.values(), Decimal(0))
    sheet.write_string(totals_row, 3, "Daily hours", fmt["daily_label"])
    for col in (0, 1, 2, 4, 5):
        sheet.write_blank(totals_row, col, None, fmt["daily_label"])
    sheet.write_string(3, 3, "Daily hours", fmt["label"])
    first_excel, last_excel = top + 1, totals_row  # Excel rows of the first and last data rows

    def total_cell(row, col, value):
        letter = _col(col)
        if rows:
            sheet.write_formula(row, col, f"=SUM({letter}{first_excel}:{letter}{last_excel})", fmt["daily"], value)
        else:
            sheet.write_number(row, col, value, fmt["daily"])
        sheet.write_formula(3, col, f"={letter}{totals_row + 1}", fmt["daily"], value)

    total_cell(totals_row, 6, float(grand))
    for day in range(1, days + 1):
        total_cell(totals_row, first_day_col + day - 1, float(day_totals.get(day, Decimal(0))))

    sheet.set_column(0, 0, 12)
    sheet.set_column(1, 2, 9)
    sheet.set_column(3, 3, 48)
    sheet.set_column(4, 4, 11)
    sheet.set_column(5, 5, 15)
    sheet.set_column(6, 6, 8)
    sheet.set_column(first_day_col, last_day_col, 6.5)
    sheet.set_row(4, 30)
    sheet.freeze_panes(5, first_day_col)
    workbook.close()
    return buffer.getvalue()


def _col(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


# ----------------------------------------------------------------- the month

def job_checks(ts: Timesheet, jobs_index: dict) -> list[str]:
    checks = []
    if jobs_index:
        for line in ts.jobs:
            if line.total <= 0:
                continue
            info = jobs_index.get(line.job)
            hours = fmt_number(line.total)
            if info is None:
                checks.append(f"{ts.employee}: {line.job} {line.task} ({hours} h) is not in the current list of open jobs.")
            elif line.task not in info["tasks"]:
                open_for = " and ".join(info["tasks"])
                checks.append(f"{ts.employee}: {line.job} is open for {open_for}, not {line.task} ({hours} h).")
    for day, hours in sorted(ts.day_totals().items()):
        if hours > 24:
            when = dt.date(ts.year, ts.month, day).strftime("%a %d %b")
            checks.append(f"{ts.employee}: {fmt_number(hours)} hours entered on {when}.")
    return checks


def _local_time(iso: str, tz_name: str) -> str:
    if not iso:
        return ""
    try:
        moment = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    try:
        from zoneinfo import ZoneInfo
        moment = moment.astimezone(ZoneInfo(tz_name))
    except Exception:  # noqa: BLE001 - fall back to the time as saved
        pass
    return f"{moment.day} {MONTH_ABBR[moment.month - 1]} {moment.year}, {moment:%H:%M}"


def _created(timesheets: list[Timesheet]) -> dt.datetime:
    stamps = [t.updated_at or t.saved_at for t in timesheets if t.updated_at or t.saved_at]
    for stamp in sorted(stamps, reverse=True):
        try:
            moment = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            return moment.astimezone(dt.timezone.utc).replace(tzinfo=None, microsecond=0)
        except ValueError:
            continue
    return dt.datetime(2000, 1, 1)


def _link(name: str) -> str:
    return f"[{name}](<{urllib.parse.quote(name)}>)"


def summary_markdown(timesheets, lines, checks, settings, year, month, files) -> str:
    totals = {}
    for line in lines:
        if not line.balancing:
            totals[name_key(line.employee)] = totals.get(name_key(line.employee), Decimal(0)) + line.amount
    out = [f"# Timewriting – {month_title(year, month)}", "",
           "Built automatically from the timesheets saved on the website. "
           "Don't edit these files by hand: they are rebuilt every time someone saves.", ""]
    out += ["| Engineer | Job hours | Other hours | Journal amount | Last saved | Issue |",
            "|---|--:|--:|--:|---|---|"]
    for ts in sorted(timesheets, key=lambda t: name_key(t.employee)):
        amount = totals.get(name_key(ts.employee), Decimal(0))
        out.append(f"| {ts.employee} | {fmt_number(ts.job_total)} | {fmt_number(ts.other_total)} | "
                   f"${amount:,.2f} | {_local_time(ts.saved_at, settings.get('timeZone', 'UTC'))} | "
                   f"#{ts.issue if ts.issue is not None else ''} |")
    out += ["", "## Files", ""]
    for name in files:
        out.append(f"- {_link(name)}")
    out += ["", "## Checks", ""]
    out += [f"- {c}" for c in checks] if checks else ["No problems found."]
    return "\n".join(out) + "\n"


# The Admin page can download each engineer workbook without the jobs that have no hours.
WITH_HOURS_ONLY = " (only jobs with hours)"


def build_month(timesheets: list[Timesheet], settings: dict, jobs_index: dict,
                year: int, month: int) -> tuple[dict[str, bytes], list[str]]:
    key = f"{year:04d}-{month:02d}"
    folder = f"output/{key}"
    created = _created(timesheets)
    lines, warnings = build_journal(timesheets, settings, year, month)
    checks = list(warnings)
    staff = {name_key(e["name"]) for e in settings["employees"]}
    for ts in sorted(timesheets, key=lambda t: name_key(t.employee)):
        if staff and name_key(ts.employee) not in staff:
            checks.append(f"{ts.employee} isn't in settings.json, so they can't sign in and have no recharge rate.")
        checks.extend(job_checks(ts, jobs_index))

    stem = f"Timewriting Journal - {MONTH_ABBR[month - 1]} {year}"
    names = [f"{stem}.csv", f"{stem}.xlsx"]
    files = {
        f"{folder}/{stem}.csv": journal_csv(lines, settings),
        f"{folder}/{stem}.xlsx": journal_xlsx(lines, settings, year, month, created),
    }
    for ts in sorted(timesheets, key=lambda t: name_key(t.employee)):
        name = f"Engineer timewriting - {safe_file_part(ts.employee)} - {month_title(year, month)}.xlsx"
        names.append(name)
        files[f"{folder}/{name}"] = engineer_xlsx(ts, jobs_index, _created([ts]))
        if any(not line.total for line in ts.jobs):
            worked = dataclasses.replace(ts, jobs=[line for line in ts.jobs if line.total])
            files[f"{folder}/{name[:-len('.xlsx')]}{WITH_HOURS_ONLY}.xlsx"] = engineer_xlsx(worked, jobs_index, _created([ts]))
        files[f"timesheets/{key}/{ts.username or slugify(ts.employee)}.json"] = (
            json.dumps(ts.to_json(), indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    files[f"{folder}/README.md"] = summary_markdown(
        timesheets, lines, checks, settings, year, month, names).encode("utf-8")
    return files, checks


def build_all(timesheets: list[Timesheet], settings: dict, jobs_index: dict):
    """Every output file for the open months, keyed by its path in the data repo.

    Closed months (settings "closedMonths") are skipped: their files are left exactly as
    they were when the office closed them, whatever has changed since.
    """
    closed = set(settings.get("closedMonths", []))
    by_month: dict[tuple[int, int], list[Timesheet]] = {}
    files: dict[str, bytes] = {}
    checks: dict[str, list[str]] = {}
    for ts in timesheets:
        if ts.month_key in closed:
            continue
        if ts.deleted:
            # Deleted by its engineer: no journal lines or workbook, but the verified copies
            # remember it, so an older save can't bring it back.
            files[f"timesheets/{ts.month_key}/{ts.username or slugify(ts.employee)}.json"] = (
                json.dumps(ts.to_json(), indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            continue
        by_month.setdefault((ts.year, ts.month), []).append(ts)
    for (year, month), group in sorted(by_month.items()):
        month_files, month_checks = build_month(group, settings, jobs_index, year, month)
        files.update(month_files)
        checks[f"{year:04d}-{month:02d}"] = month_checks
    return files, checks
