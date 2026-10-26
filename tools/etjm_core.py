"""Core logic for the Engineer Timewriting Journal Maker.

The website (site/) saves each engineer's month as a GitHub issue in the private
data repo. The Actions in that repo use this module to:

* read the "Job Task Overview" report from Business Central (read_job_report),
* encrypt the job list the website loads (encrypt_bundle), and
* read the encrypted timesheets back out of the issues (collect_timesheets).

Building the journal and workbooks lives in etjm_outputs.py, and talking to
GitHub lives in etjm_github.py. Nothing in this module touches the network.
"""

from __future__ import annotations

import base64
import binascii
import calendar
import copy
import datetime as dt
import hashlib
import json
import math
import os
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

TASKS = ("CAPEX", "REVEX")
JOB_RE = re.compile(r"^JOB\d{6}$")
MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
PBKDF2_ITERATIONS = 600_000
MAX_DAY_HOURS = Decimal(24)
MAX_TIMESHEET_JSON = 300_000

MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]
MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

DEFAULT_SETTINGS = {
    "employees": [],
    "admins": [],
    "closedMonths": [],
    "otherTime": [
        "General Time",
        "General Time - BP",
        "General Time - Hutt JV",
        "Asset Team Meeting",
        "Training",
        "Annual Leave",
        "Sick Leave",
        "Lieu Leave",
        "Public Holiday",
        "Special Leave",
        "Bereavement Leave",
        "Misc Navision coded jobs - BP less than 1 hour per day",
        "Misc Navision coded jobs - Z less than 1 hour per day",
        "Triage",
    ],
    "jobStatuses": ["Open"],
    "timeZone": "Pacific/Auckland",
    "journal": {
        "balancingGlAccount": "9011",
        "dateFormat": "%d/%m/%Y",
        "monthLabels": ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                        "Jul", "Aug", "Sept", "Oct", "Nov", "Dec"],
        "description": "{employee} Timewriting {monthLabel} {yy}",
        "documentNumber": "Engtimewrite{monthLabel} {yy}",
        "csvUtf8Bom": True,
    },
}


class TimesheetError(ValueError):
    """A saved timesheet that can't be used. The message is safe to show."""


# ---------------------------------------------------------------- text & names

_CONTROL = {"Cc", "Cf", "Cs", "Co", "Cn"}


def clean_text(value) -> str:
    """Collapse whitespace (including non-breaking spaces) and drop control characters."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = unicodedata.normalize("NFC", str(value))
    text = "".join(" " if ch.isspace() else ch for ch in text
                   if ch.isspace() or unicodedata.category(ch) not in _CONTROL)
    return re.sub(r" +", " ", text).strip()


_NAME_EXTRA = set(" .'’-")


def clean_name(value) -> str:
    """Check an engineer's name. It ends up in the journal, so keep it plain."""
    name = clean_text(value)
    if not name:
        raise TimesheetError("The name is empty.")
    if len(name) > 60:
        raise TimesheetError("The name is longer than 60 characters.")
    if not unicodedata.category(name[0]).startswith("L"):
        raise TimesheetError("The name must start with a letter.")
    for ch in name:
        if not (unicodedata.category(ch)[0] in "LM" or ch in _NAME_EXTRA):
            raise TimesheetError(
                "The name can only contain letters, spaces, apostrophes, hyphens and full stops.")
    return name


def name_key(name: str) -> str:
    return clean_text(name).casefold()


def slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug or hashlib.sha256(text.encode()).hexdigest()[:12]


def safe_file_part(text: str) -> str:
    """Make a name safe to use inside a file name, keeping it readable."""
    return re.sub(r'[\\/:*?"<>|]+', "", text).strip() or "Unnamed"


# ---------------------------------------------------------------------- months

def parse_month(value) -> tuple[int, int]:
    match = MONTH_RE.match(str(value or ""))
    if not match:
        raise TimesheetError("The month must look like 2026-09.")
    year, month = int(match.group(1)), int(match.group(2))
    if not 2000 <= year <= 2100:
        raise TimesheetError("The month is out of range.")
    return year, month


def days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def month_end(year: int, month: int) -> dt.date:
    return dt.date(year, month, days_in_month(year, month))


def month_title(year: int, month: int) -> str:
    return f"{MONTH_NAMES[month - 1]} {year}"


# -------------------------------------------------------------------- numbers

def to_hours(value) -> Decimal:
    """Hours for one day: a number from 0 to 24 with at most two decimals."""
    if isinstance(value, bool):
        raise TimesheetError("Hours must be numbers.")
    try:
        if isinstance(value, (int, float)):
            if isinstance(value, float) and not math.isfinite(value):
                raise TimesheetError("Hours must be numbers.")
            hours = Decimal(str(value))
        elif isinstance(value, str):
            hours = Decimal(value.strip())
        else:
            raise TimesheetError("Hours must be numbers.")
    except InvalidOperation as exc:
        raise TimesheetError("Hours must be numbers.") from exc
    if not hours.is_finite() or hours < 0 or hours > MAX_DAY_HOURS:
        raise TimesheetError("Hours for a day must be between 0 and 24.")
    if hours != hours.quantize(Decimal("0.01")):
        raise TimesheetError("Hours can have at most two decimal places.")
    return hours + 0  # turns -0 into 0


def fmt_number(value: Decimal) -> str:
    """160 -> '160', Decimal('4.50') -> '4.5'."""
    value = Decimal(value)
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


# ------------------------------------------------------------------- settings

def load_settings(path: Path | None) -> dict:
    """Read a plain settings.json file, filling in defaults (the data repo's copy is
    encrypted: see etjm_vault.Vault.settings)."""
    user = json.loads(Path(path).read_text(encoding="utf-8")) if path and Path(path).exists() else None
    return parse_settings(user)


def parse_settings(user) -> dict:
    """Check settings and fill in defaults."""
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    if user is not None:
        if not isinstance(user, dict):
            raise ValueError("settings.json must contain a JSON object.")
        user = copy.deepcopy(user)
        journal = user.pop("journal", None) or {}
        settings.update(user)
        settings["journal"].update(journal)
    _check_settings(settings)
    return settings


def _check_settings(settings: dict) -> None:
    settings.pop("defaultRechargeRate", None)  # no default rate: each engineer has their own
    employees = settings.get("employees")
    if not isinstance(employees, list):
        raise ValueError("settings.json: employees must be a list.")
    names, usernames = set(), set()
    for emp in employees:
        if not isinstance(emp, dict) or "name" not in emp:
            raise ValueError('settings.json: each employee needs a "name".')
        emp["name"] = clean_name(emp["name"])
        if emp.get("rechargeRate") is not None and not _is_rate(emp["rechargeRate"]):
            raise ValueError(f"settings.json: rechargeRate for {emp['name']} must be a positive number.")
        username = normalize_username(emp.get("username") or default_username(emp["name"]))
        if not USERNAME_RE.match(username):
            raise ValueError(f"settings.json: the username for {emp['name']} can only use letters, numbers, dots, dashes and underscores.")
        if username in usernames or name_key(emp["name"]) in names:
            raise ValueError(f"settings.json: {emp['name']} ({username}) is listed twice, or shares a username. Give one a different \"username\".")
        names.add(name_key(emp["name"]))
        usernames.add(username)
        emp["username"] = username
        if not isinstance(emp.get("active", True), bool):
            raise ValueError(f"settings.json: active for {emp['name']} must be true or false.")
        emp["active"] = emp.get("active", True)
        _set_email(emp)
    admins = settings.get("admins")
    if not isinstance(admins, list):
        raise ValueError("settings.json: admins must be a list.")
    engineers = {e["username"]: e for e in employees}
    for admin in admins:
        if not isinstance(admin, dict) or "name" not in admin:
            raise ValueError('settings.json: each admin needs a "name".')
        admin["name"] = clean_name(admin["name"])
        username = normalize_username(admin.get("username") or default_username(admin["name"]))
        if not USERNAME_RE.match(username):
            raise ValueError(f"settings.json: the username for {admin['name']} can only use letters, numbers, dots, dashes and underscores.")
        same = engineers.get(username)
        if same and name_key(same["name"]) != name_key(admin["name"]):
            raise ValueError(f"settings.json: the username {username} is used by two different people.")
        admin["username"] = username
        _set_email(admin)
    closed = settings.get("closedMonths")
    if not isinstance(closed, list) or not all(isinstance(m, str) and MONTH_RE.match(m) for m in closed):
        raise ValueError('settings.json: closedMonths must be a list of months like "2026-09".')
    settings["closedMonths"] = sorted(set(closed))
    other = settings.get("otherTime")
    if not isinstance(other, list) or not all(isinstance(x, str) and clean_text(x) for x in other):
        raise ValueError("settings.json: otherTime must be a list of names.")
    settings["otherTime"] = [clean_text(x) for x in other]
    statuses = settings.get("jobStatuses")
    if not isinstance(statuses, list) or not statuses:
        raise ValueError("settings.json: jobStatuses must be a non-empty list, e.g. [\"Open\"].")
    labels = settings["journal"].get("monthLabels")
    if not isinstance(labels, list) or len(labels) != 12:
        raise ValueError("settings.json: journal.monthLabels must list 12 month labels.")


EMAIL_RE = re.compile(r"^[^@\s<>\"',;:()\[\]]+@[^@\s<>\"',;:()\[\]]+\.[^@\s<>\"',;:()\[\]]+$")


def clean_email(value, who: str = "someone") -> str | None:
    """Someone's email address for password resets, tidied up (None if there isn't one)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise ValueError(f"settings.json: the email for {who} must be text.")
    email = value.strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise ValueError(f"settings.json: the email for {who} doesn't look like an email address.")
    return email


def _set_email(person: dict) -> None:
    email = clean_email(person.get("email"), person["name"])
    if email:
        person["email"] = email
    else:
        person.pop("email", None)


def _is_rate(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value > 0)


def people(settings: dict) -> list[dict]:
    """Everyone who can sign in: current engineers (who fill timesheets) and admins, merged by username.

    Engineers marked "active": false have left. They can't sign in, but stay in settings.json
    so the journals they saved keep their recharge rate.
    """
    result: dict[str, dict] = {}
    for emp in settings["employees"]:
        if not emp.get("active", True):
            continue
        result[emp["username"]] = {"name": emp["name"], "username": emp["username"], "timesheets": True, "admin": False,
                                   "email": emp.get("email")}
    for admin in settings.get("admins", []):
        person = result.setdefault(admin["username"], {
            "name": admin["name"], "username": admin["username"], "timesheets": False, "admin": False, "email": None})
        person["admin"] = True
        person["email"] = person["email"] or admin.get("email")
    return list(result.values())


def recharge_rate(settings: dict, employee: str) -> Decimal | None:
    """The engineer's own rechargeRate from settings.json (there is no default rate)."""
    key = name_key(employee)
    for emp in settings["employees"]:
        if name_key(emp["name"]) == key and emp.get("rechargeRate") is not None:
            return Decimal(str(emp["rechargeRate"]))
    return None


def month_tokens(settings: dict, year: int, month: int) -> dict:
    return {
        "monthLabel": settings["journal"]["monthLabels"][month - 1],
        "yy": f"{year % 100:02d}",
        "yyyy": str(year),
        "mon": MONTH_ABBR[month - 1],
        "month": MONTH_NAMES[month - 1],
    }


# --------------------------------------------------------------------- crypto
#
# One secret, MASTER_KEY, lives only in the data repo's GitHub Actions secrets. Every
# other key is derived from it or kept encrypted under it:
#
#   bundle key     HKDF(MASTER, "etjm/bundle/<epoch>")  encrypts the job list. The epoch
#                  goes up whenever someone is removed or reset, which locks them out.
#   user key       random, one per person, stored encrypted in the private users.json.
#                  Their password unlocks it (PBKDF2 -> AES-GCM); it unlocks their profile.
#   data keys      random, one or more per person: encrypt their timesheets end to end, so
#                  other engineers (who share the save token) can't read or forge them.
#   password key   HKDF(MASTER, "etjm/passwords"): encrypts the passwords the office
#                  issues, so admins can show them; passwords people choose aren't stored.
#
# Everything is AES-256-GCM with associated data naming what the ciphertext is and whose
# it is, so a ciphertext can't be moved from one person, month or purpose to another.

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
PASSWORD_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
MIN_OWN_PASSWORD = 12


def default_username(name: str) -> str:
    """"Alex Smith" -> "alex.smith"."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", ".", ascii_name).strip(".")


def normalize_username(username: str) -> str:
    return unicodedata.normalize("NFC", username or "").strip().lower()


def normalize_password(password: str) -> str:
    """Passwords ignore case, spaces and dashes (the website does the same)."""
    password = unicodedata.normalize("NFC", password or "").upper()
    return re.sub(r"[\s\-]+", "", password)


def generate_password(groups: int = 3) -> str:
    """Random password like K7QF-M2XR-9PTW (about 20 bits per group of four)."""
    import secrets

    return "-".join("".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(4)) for _ in range(groups))


def user_id(username: str) -> str:
    """How a username appears in the public users file, so names aren't listed there."""
    return hashlib.sha256(f"etjm-user:{normalize_username(username)}".encode("utf-8")).hexdigest()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: str) -> bytes:
    return base64.b64decode(text, validate=True)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def parse_master_key(text: str) -> bytes:
    try:
        key = base64.b64decode((text or "").strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("MASTER_KEY must be base64.") from exc
    if len(key) != 32:
        raise ValueError("MASTER_KEY must be 32 random bytes, base64-encoded.")
    return key


def derive(master: bytes, label: str) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=label.encode("utf-8")).derive(master)


def bundle_key(master: bytes, epoch: int) -> bytes:
    return derive(master, f"etjm/bundle/{epoch}")


def password_store_key(master: bytes) -> bytes:
    return derive(master, "etjm/passwords")


def state_key(master: bytes) -> bytes:
    return derive(master, "etjm/state")


def recover_key(secret: bytes) -> bytes:
    """Opens a password-reset link's copy of someone's new user key (see etjm_accounts.py)."""
    return derive(secret, "etjm/recover")


def store_key(master: bytes) -> bytes:
    """Encrypts every file in the private data repo (see etjm_vault.py)."""
    return derive(master, "etjm/store")


def file_tag(master: bytes, label: str) -> str:
    """A meaningless file name for something, e.g. a month's journals, so names say nothing."""
    import hmac

    return hmac.new(derive(master, "etjm/file-tag"), label.encode("utf-8"), hashlib.sha256).hexdigest()[:20]


def user_tag(master: bytes, username: str) -> str:
    """A stable, meaningless label for someone's issues, so titles don't name them."""
    import hmac

    key = derive(master, "etjm/tag")
    return hmac.new(key, normalize_username(username).encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def seal(key: bytes, plaintext: bytes, aad: str) -> str:
    """AES-256-GCM, written as "<iv>.<ciphertext>" in base64url."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    iv = os.urandom(12)
    return f"{b64url(iv)}.{b64url(AESGCM(key).encrypt(iv, plaintext, aad.encode('utf-8')))}"


def unseal(key: bytes, token: str, aad: str) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        iv, ct = token.split(".")
        return AESGCM(key).decrypt(unb64url(iv), unb64url(ct), aad.encode("utf-8"))
    except (InvalidTag, ValueError, binascii.Error) as exc:
        raise ValueError("Couldn't decrypt (wrong key, or the data was changed).") from exc


def password_key(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", normalize_password(password).encode("utf-8"), salt, iterations, 32)


def wrap_user_key(user_key: bytes, password: str, username: str, iterations: int = PBKDF2_ITERATIONS) -> dict:
    """Lock someone's user key with their password (this is what signing in unlocks)."""
    salt = os.urandom(16)
    pk = password_key(password, salt, iterations)
    return {"salt": b64(salt), "iterations": iterations, "key": seal(pk, user_key, f"etjm-user-key|{username}")}


def unwrap_user_key(wrap: dict, password: str, username: str) -> bytes:
    pk = password_key(password, unb64(wrap["salt"]), int(wrap["iterations"]))
    return unseal(pk, wrap["key"], f"etjm-user-key|{username}")


def profile_bytes(profile: dict) -> bytes:
    return json.dumps(profile, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def seal_profile(user_key: bytes, profile: dict, username: str) -> str:
    return seal(user_key, profile_bytes(profile), f"etjm-profile|{username}")


def unseal_profile(user_key: bytes, token: str, username: str) -> dict:
    return json.loads(unseal(user_key, token, f"etjm-profile|{username}").decode("utf-8"))


def encrypt_bundle(data: dict, key: bytes, epoch: int) -> dict:
    plaintext = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {"v": 3, "epoch": epoch, "data": seal(key, plaintext, f"etjm-bundle|{epoch}")}


def decrypt_bundle(blob: dict, key: bytes) -> dict:
    return json.loads(unseal(key, blob["data"], f"etjm-bundle|{blob['epoch']}").decode("utf-8"))


# Timesheets travel as GitHub issues, encrypted with the engineer's newest data key.
TIMESHEET_V2 = "<!-- etjm:timesheet:v2 -->"
PASSWORD_V1 = "<!-- etjm:password:v1 -->"
RESET_REQUEST_V1 = "<!-- etjm:reset-request:v1 -->"
RESET_TITLE = "Password reset request"
CIPHER_BLOCK_RE = re.compile(r"```etjm[ \t]*\r?\n([A-Za-z0-9_.\-]+)\r?\n```")
TITLE_RE = re.compile(r"^Timesheet (\d{4}-\d{2}) · ([0-9a-f]{16})$")
PASSWORD_TITLE_RE = re.compile(r"^Password change · ([0-9a-f]{16})$")


def timesheet_aad(username: str, month: str) -> str:
    return f"etjm-ts|{username}|{month}"


def encrypt_timesheet(data: dict, data_key: bytes, key_index: int, username: str) -> str:
    plaintext = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"ETJM2.{key_index}.{seal(data_key, plaintext, timesheet_aad(username, data['month']))}"


def cipher_block(body: str, marker: str) -> str | None:
    if marker not in (body or ""):
        return None
    match = CIPHER_BLOCK_RE.search(body.split(marker, 1)[1])
    return match.group(1) if match else None


# ------------------------------------------------------------------- job list

JOB_HEADERS = {
    "no": "Project No.",
    "description": "Job Description",
    "location": "Job Location",
    "owner": "Owner",
    "task": "Project Task No.",
    "status": "Status",
}


def _header_key(value) -> str:
    return clean_text(value).casefold()


def read_job_report(path: Path, statuses=("Open",)) -> tuple[list[dict], dict]:
    """Read a "Job Task Overview" export from Business Central.

    Columns are found by their header names, so the column order doesn't matter.
    Only job tasks whose Status is in `statuses` are kept, and only the job number,
    description, location, owner and CAPEX/REVEX tasks leave this function.
    """
    import openpyxl

    wanted = {key: _header_key(name) for key, name in JOB_HEADERS.items()}
    keep = {s.casefold() for s in statuses}
    stats = {"rows": 0, "kept_rows": 0, "skipped_rows": 0, "sheet": None}
    jobs: dict[str, dict] = {}

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet in workbook.worksheets:
            rows = sheet.iter_rows(values_only=True)
            columns = None
            for _, row in zip(range(25), rows):
                found = {_header_key(cell): i for i, cell in enumerate(row) if cell is not None}
                if all(name in found for name in wanted.values()):
                    columns = {key: found[name] for key, name in wanted.items()}
                    break
            if columns is None:
                continue
            stats["sheet"] = sheet.title
            for row in rows:
                if row is None or all(cell is None for cell in row):
                    continue
                stats["rows"] += 1
                get = lambda key: row[columns[key]] if columns[key] < len(row) else None  # noqa: E731
                if clean_text(get("status")).casefold() not in keep:
                    continue
                number = clean_text(get("no")).upper()
                task = clean_text(get("task")).upper()
                if not JOB_RE.match(number) or task not in TASKS:
                    stats["skipped_rows"] += 1
                    continue
                stats["kept_rows"] += 1
                job = jobs.setdefault(number, {
                    "no": number,
                    "desc": clean_text(get("description"))[:200],
                    "loc": clean_text(get("location"))[:20],
                    "owner": clean_text(get("owner"))[:20],
                    "tasks": [],
                })
                if task not in job["tasks"]:
                    job["tasks"].append(task)
            break
    finally:
        workbook.close()

    if stats["sheet"] is None:
        names = ", ".join(f'"{h}"' for h in JOB_HEADERS.values())
        raise ValueError(f"Couldn't find a header row with {names} in {Path(path).name}.")
    result = sorted(jobs.values(), key=lambda j: j["no"])
    for job in result:
        job["tasks"].sort(key=TASKS.index)
    stats["jobs"] = len(result)
    stats["capex_only"] = sum(1 for j in result if j["tasks"] == ["CAPEX"])
    stats["revex_only"] = sum(1 for j in result if j["tasks"] == ["REVEX"])
    stats["both"] = sum(1 for j in result if len(j["tasks"]) == 2)
    return result, stats


# ----------------------------------------------------------------- timesheets

@dataclass
class JobLine:
    job: str
    task: str
    location: str
    owner: str
    description: str
    hours: dict[int, Decimal]

    @property
    def total(self) -> Decimal:
        return sum(self.hours.values(), Decimal(0))


@dataclass
class OtherLine:
    label: str
    hours: dict[int, Decimal]

    @property
    def total(self) -> Decimal:
        return sum(self.hours.values(), Decimal(0))


@dataclass
class Timesheet:
    employee: str
    year: int
    month: int
    username: str = ""
    saved_at: str = ""
    jobs: list[JobLine] = field(default_factory=list)
    other: list[OtherLine] = field(default_factory=list)
    issue: int | None = None
    updated_at: str = ""
    deleted: bool = False  # the engineer deleted this month: no journal lines or workbook

    @property
    def month_key(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def job_total(self) -> Decimal:
        return sum((line.total for line in self.jobs), Decimal(0))

    @property
    def other_total(self) -> Decimal:
        return sum((line.total for line in self.other), Decimal(0))

    def day_totals(self) -> dict[int, Decimal]:
        totals: dict[int, Decimal] = {}
        for line in [*self.jobs, *self.other]:
            for day, hours in line.hours.items():
                totals[day] = totals.get(day, Decimal(0)) + hours
        return totals

    def to_json(self) -> dict:
        """The saved data, tidied up (this is what goes in timesheets/)."""
        hours = lambda h: {str(d): float(v) if v % 1 else int(v) for d, v in sorted(h.items())}  # noqa: E731
        jobs: dict[str, dict] = {}
        for line in self.jobs:
            entry = jobs.setdefault(line.job, {
                "job": line.job, "location": line.location, "owner": line.owner,
                "description": line.description, "hours": {}})
            if line.task:  # a job with no hours has no CAPEX/REVEX line
                entry["hours"][line.task] = hours(line.hours)
        return {
            "v": 1,
            "employee": self.employee,
            "username": self.username,
            "month": self.month_key,
            "savedAt": self.saved_at,
            "issue": self.issue,
            **({"deleted": True} if self.deleted else {}),
            "jobs": list(jobs.values()),
            "other": [{"label": o.label, "hours": hours(o.hours)} for o in self.other],
        }


def _day_hours(raw, days: int, what: str) -> dict[int, Decimal]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise TimesheetError(f"Hours for {what} are not in the expected format.")
    result: dict[int, Decimal] = {}
    for key, value in raw.items():
        if not re.fullmatch(r"\d{1,2}", str(key)) or not 1 <= int(key) <= days:
            raise TimesheetError(f"{what} has hours on a day that isn't in the month.")
        hours = to_hours(value)
        if hours:
            result[int(key)] = result.get(int(key), Decimal(0)) + hours
    return result


def parse_timesheet(data) -> Timesheet:
    """Check the JSON the website saved and turn it into a Timesheet."""
    if not isinstance(data, dict) or data.get("v") != 1:
        raise TimesheetError("This isn't a timesheet saved by the website (unknown format).")
    employee = clean_name(data.get("employee"))
    year, month = parse_month(data.get("month"))
    days = days_in_month(year, month)
    saved_at = clean_text(data.get("savedAt"))[:40]

    raw_jobs = data.get("jobs") or []
    raw_other = data.get("other") or []
    if not isinstance(raw_jobs, list) or not isinstance(raw_other, list):
        raise TimesheetError("The timesheet's rows are not in the expected format.")
    if len(raw_jobs) > 200 or len(raw_other) > 100:
        raise TimesheetError("The timesheet has too many rows.")

    lines: dict[tuple[str, str], JobLine] = {}
    for raw in raw_jobs:
        if not isinstance(raw, dict):
            raise TimesheetError("A job row is not in the expected format.")
        job = clean_text(raw.get("job")).upper()
        if not JOB_RE.match(job):
            raise TimesheetError("A job number is not in the JOB000000 format.")
        hours_by_task = raw.get("hours") or {}
        if not isinstance(hours_by_task, dict):
            raise TimesheetError(f"Hours for {job} are not in the expected format.")
        if not hours_by_task and not any(key[0] == job for key in lines):
            # A job with no hours (yet): kept, so it stays on the timesheet and in the workbook.
            lines[(job, "")] = JobLine(
                job=job, task="",
                location=clean_text(raw.get("location"))[:20],
                owner=clean_text(raw.get("owner"))[:20],
                description=clean_text(raw.get("description"))[:200],
                hours={})
        for task, raw_hours in hours_by_task.items():
            task = clean_text(task).upper()
            if task not in TASKS:
                raise TimesheetError(f"{job} has a Capex/Revex value that isn't CAPEX or REVEX.")
            hours = _day_hours(raw_hours, days, f"{job} {task}")
            line = lines.get((job, task))
            if line is None:
                lines[(job, task)] = JobLine(
                    job=job, task=task,
                    location=clean_text(raw.get("location"))[:20],
                    owner=clean_text(raw.get("owner"))[:20],
                    description=clean_text(raw.get("description"))[:200],
                    hours=hours)
            else:  # the same job twice: add the hours together
                for day, value in hours.items():
                    line.hours[day] = line.hours.get(day, Decimal(0)) + value
    for key in [k for k in lines if not k[1] and any(other[0] == k[0] and other[1] for other in lines)]:
        del lines[key]  # the job also has CAPEX or REVEX lines, so it needs no line of its own

    other: list[OtherLine] = []
    for raw in raw_other:
        if not isinstance(raw, dict):
            raise TimesheetError("An other-time row is not in the expected format.")
        label = clean_text(raw.get("label"))[:100]
        if not label:
            raise TimesheetError("An other-time row has no description.")
        other.append(OtherLine(label=label, hours=_day_hours(raw.get("hours"), days, label)))

    if data.get("deleted") is True:  # deleted by its engineer; kept so older copies can't bring it back
        return Timesheet(employee=employee, year=year, month=month, saved_at=saved_at, deleted=True)
    return Timesheet(employee=employee, year=year, month=month, saved_at=saved_at,
                     jobs=list(lines.values()), other=other)


def decrypt_timesheet_issue(issue: dict, keys_by_tag: dict) -> tuple[str, Timesheet]:
    """Open one timesheet issue. It must be encrypted with the newest data key of the person
    its title points to, for the month in its title; anything else is rejected."""
    match = TITLE_RE.match((issue.get("title") or "").strip())
    if not match:
        raise TimesheetError("its title isn't a timesheet title")
    month, tag = match.groups()
    person = keys_by_tag.get(tag)
    if person is None:
        raise TimesheetError("it isn't for anyone who can sign in")
    token = cipher_block(issue.get("body") or "", TIMESHEET_V2)
    parts = (token or "").split(".", 2)
    if len(parts) != 3 or parts[0] != "ETJM2" or not parts[1].isdigit():
        raise TimesheetError("it has no encrypted timesheet")
    index = int(parts[1])
    if index != len(person["dataKeys"]) - 1:
        raise TimesheetError("it uses an old key (saved before the sign-in was reset)")
    try:
        data = json.loads(unseal(person["dataKeys"][index], parts[2], timesheet_aad(person["username"], month)))
    except ValueError as exc:
        raise TimesheetError("it couldn't be decrypted, so it wasn't saved by its owner") from exc
    if len(json.dumps(data)) > MAX_TIMESHEET_JSON:
        raise TimesheetError("it is too large")
    timesheet = parse_timesheet({**data, "employee": person["name"]})
    if timesheet.month_key != month:
        raise TimesheetError("its month doesn't match its title")
    timesheet.username = person["username"]
    timesheet.issue = issue.get("number")
    timesheet.updated_at = issue.get("updated_at") or ""
    return person["username"], timesheet


def collect_timesheets(issues: list[dict], keys_by_tag: dict, mirror: dict | None = None,
                       closed=()) -> tuple[list[Timesheet], list[str]]:
    """Every timesheet: the newest verified copy for each person and month.

    `mirror` holds the copies kept in the private repo (timesheets/), which only the
    Actions can write. An issue only replaces one if it decrypts with its owner's key and
    was saved later, so editing, closing or replaying issues can't change the journals.
    Issue state is ignored on purpose (anyone with the save token could close issues).
    """
    warnings: list[str] = []
    best: dict[tuple[str, str], Timesheet] = dict(mirror or {})
    for issue in sorted(issues, key=lambda i: i.get("number") or 0):
        if "pull_request" in issue or TIMESHEET_V2 not in (issue.get("body") or ""):
            continue
        try:
            username, timesheet = decrypt_timesheet_issue(issue, keys_by_tag)
        except TimesheetError as exc:
            warnings.append(f"Issue #{issue.get('number')} was ignored: {exc}.")
            continue
        if timesheet.month_key in closed:
            continue
        key = (username, timesheet.month_key)
        current = best.get(key)
        if current is None or (timesheet.saved_at, timesheet.updated_at) > (current.saved_at, current.updated_at):
            best[key] = timesheet
    return list(best.values()), warnings
