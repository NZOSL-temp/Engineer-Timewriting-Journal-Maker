#!/usr/bin/env python3
"""Every file in the private data repo is encrypted, so a copy of the repo is unreadable.

Each file holds {"etjm": "encrypted", "v": 1, "data": "<iv>.<ciphertext>"}: AES-256-GCM
with the store key (derived from MASTER_KEY), bound to the file's path, so a file can't be
swapped for another one or changed unnoticed. Admins' sign-ins unlock the same key, which
is how the Admin page reads and writes these files. Nothing in the repo names anyone:

    settings.json            engineers, rates, admins, closed months
    users.json               sign-ins (see etjm_accounts.py)
    input/report.json        the Business Central report: {"name", "sha256", "uploaded", "xlsx"}
    generated/jobs.json      the job list read from it
    timesheets/<tag>.json    a month's verified timesheets: {"month", "timesheets": [...]}
    output/<tag>.json        a month's journal and workbooks: {"month", "files": {name: base64}}

<tag> is a meaningless name for the month (etjm_core.file_tag).

To look at or change these files by hand, with MASTER_KEY set:

    python tools/etjm_vault.py --data-dir DATA list
    python tools/etjm_vault.py --data-dir DATA show settings.json
    python tools/etjm_vault.py --data-dir DATA export output/<tag>.json some/folder
    python tools/etjm_vault.py --data-dir DATA import my-settings.json settings.json
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from etjm_core import file_tag, parse_master_key, parse_settings, seal, store_key, unseal  # noqa: E402

SETTINGS = "settings.json"
USERS = "users.json"
REPORT = "input/report.json"
JOBS = "generated/jobs.json"
TIMESHEETS = "timesheets"
OUTPUT = "output"


class NotEncrypted(ValueError):
    pass


class Vault:
    def __init__(self, root: Path, master: bytes):
        self.root = Path(root)
        self.master = master
        self.key = store_key(master)

    @classmethod
    def from_store_key(cls, root: Path, key: bytes) -> "Vault":
        """Read-only use with just the file key (as an admin's profile has it), without MASTER_KEY."""
        vault = cls.__new__(cls)
        vault.root, vault.master, vault.key = Path(root), None, key
        return vault

    def _aad(self, rel: str) -> str:
        return f"etjm-file|{rel}"

    def read(self, rel: str) -> bytes | None:
        path = self.root / rel
        if not path.exists():
            return None
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            envelope = None
        if not isinstance(envelope, dict) or envelope.get("etjm") != "encrypted" or not isinstance(envelope.get("data"), str):
            raise NotEncrypted(f"{rel} isn't encrypted. Files in the data repo must be added with tools/etjm_vault.py or the Admin page.")
        try:
            return unseal(self.key, envelope["data"], self._aad(rel))
        except ValueError as exc:
            raise ValueError(f"{rel} couldn't be decrypted: it was changed, moved, or encrypted with another MASTER_KEY.") from exc

    def write(self, rel: str, data: bytes) -> bool:
        """Encrypt and save; leaves the file alone if its contents are the same already."""
        try:
            if self.read(rel) == data:
                return False
        except ValueError:
            pass
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        envelope = {"etjm": "encrypted", "v": 1, "data": seal(self.key, data, self._aad(rel))}
        path.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
        return True

    def read_json(self, rel: str, default=None):
        data = self.read(rel)
        return default if data is None else json.loads(data.decode("utf-8"))

    def write_json(self, rel: str, value) -> bool:
        return self.write(rel, (json.dumps(value, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))

    def settings(self) -> dict:
        data = self.read_json(SETTINGS)
        if data is None:
            raise ValueError("settings.json is missing from the data repo.")
        return parse_settings(data)

    def month_path(self, folder: str, month: str) -> str:
        return f"{folder}/{file_tag(self.master, f'{folder}|{month}')}.json"

    def months(self, folder: str) -> dict[str, tuple[str, dict]]:
        """Each month's file in a folder: {month: (path, contents)}."""
        found = {}
        for path in sorted((self.root / folder).glob("*.json")):
            rel = f"{folder}/{path.name}"
            data = self.read_json(rel)
            found[data["month"]] = (rel, data)
        return found


def pack_month_files(files: dict[str, bytes]) -> dict[str, dict]:
    """Turn {"output/2026-09/x.csv": bytes, "timesheets/2026-09/ann.json": bytes} into one
    record per folder and month: {("output", "2026-09"): {...}}."""
    packed: dict[tuple[str, str], dict] = {}
    for rel, content in sorted(files.items()):
        folder, month, name = rel.split("/", 2)
        if folder == OUTPUT:
            entry = packed.setdefault((folder, month), {"month": month, "files": {}})
            entry["files"][name] = base64.b64encode(content).decode("ascii")
        elif folder == TIMESHEETS:
            entry = packed.setdefault((folder, month), {"month": month, "timesheets": []})
            entry["timesheets"].append(json.loads(content.decode("utf-8")))
        else:
            raise ValueError(f"Unexpected output file {rel}")
    return packed


def _describe(vault: Vault, rel: str) -> str:
    fixed = {SETTINGS: "settings", USERS: "sign-ins", REPORT: "Business Central report", JOBS: "job list"}
    if rel in fixed:
        return fixed[rel]
    data = vault.read_json(rel)
    if rel.startswith(f"{OUTPUT}/"):
        return f"{data['month']} journal and workbooks ({len(data['files'])} files)"
    if rel.startswith(f"{TIMESHEETS}/"):
        return f"{data['month']} verified timesheets ({len(data['timesheets'])})"
    return "?"


def main() -> int:
    parser = argparse.ArgumentParser(description="Look at or change the encrypted files in the data repo (needs MASTER_KEY).")
    parser.add_argument("--data-dir", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="every encrypted file and what it holds")
    show = sub.add_parser("show", help="print a JSON file")
    show.add_argument("path")
    export = sub.add_parser("export", help="decrypt a file into a folder (journals and workbooks become their own files)")
    export.add_argument("path")
    export.add_argument("dest")
    imp = sub.add_parser("import", help="encrypt a plain JSON (or .xlsx report) file into the repo")
    imp.add_argument("source")
    imp.add_argument("path")
    args = parser.parse_args()
    try:
        vault = Vault(Path(args.data_dir), parse_master_key(os.environ.get("MASTER_KEY", "")))
    except ValueError as exc:
        sys.exit(f"MASTER_KEY is missing or wrong: {exc}")

    if args.command == "list":
        for path in sorted(p for p in vault.root.rglob("*.json") if ".git" not in p.parts and ".github" not in p.parts):
            rel = path.relative_to(vault.root).as_posix()
            try:
                print(f"{rel:45} {_describe(vault, rel)}")
            except (NotEncrypted, ValueError, KeyError) as exc:
                print(f"{rel:45} ! {exc}")
    elif args.command == "show":
        print(json.dumps(vault.read_json(args.path), indent=2, ensure_ascii=False))
    elif args.command == "export":
        dest = Path(args.dest)
        dest.mkdir(parents=True, exist_ok=True)
        data = vault.read_json(args.path)
        if args.path == REPORT:
            (dest / data["name"]).write_bytes(base64.b64decode(data["xlsx"]))
        elif "files" in data:
            for name, content in data["files"].items():
                (dest / name).write_bytes(base64.b64decode(content))
        else:
            (dest / Path(args.path).name).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Written to {dest}")
    elif args.command == "import":
        source = Path(args.source)
        if args.path == REPORT:
            import datetime as dt
            import hashlib

            raw = source.read_bytes()
            value = {"name": source.name, "sha256": hashlib.sha256(raw).hexdigest(),
                     "uploaded": dt.date.today().isoformat(), "xlsx": base64.b64encode(raw).decode("ascii")}
        else:
            value = json.loads(source.read_text(encoding="utf-8"))
            if args.path == SETTINGS:
                parse_settings(value)  # check it before saving
        vault.write_json(args.path, value)
        print(f"Encrypted into {args.path}. Commit and push it to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
