"""Small GitHub helpers for the Actions: the REST API (issues) and committing with retries."""

from __future__ import annotations

import json
import random
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


def api(method: str, path: str, token: str, payload=None):
    url = path if path.startswith("https://") else API + path
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    for attempt in range(4):
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "engineer-timewriting-journal-maker",
            **({"Content-Type": "application/json"} if data else {}),
        })
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
                return (json.loads(body) if body else None), response.headers
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or attempt == 3:
                raise
        except urllib.error.URLError:
            if attempt == 3:
                raise
        time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def _next_link(header: str | None) -> str | None:
    for part in (header or "").split(","):
        match = re.search(r'<([^>]+)>;\s*rel="next"', part)
        if match:
            return match.group(1)
    return None


def list_issues(repo: str, token: str) -> list[dict]:
    """Every issue in the repo, open and closed (pull requests are filtered out later)."""
    issues: list[dict] = []
    url = f"/repos/{repo}/issues?state=all&per_page=100&sort=created&direction=asc"
    while url:
        page, headers = api("GET", url, token)
        issues.extend(page)
        url = _next_link(headers.get("Link"))
    return issues


# ------------------------------------------------------------------------ git

def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True)
    if check and result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def commit_and_push(repo: Path, paths: list[str], message: str, regenerate,
                    branch: str = "main", attempts: int = 6):
    """Reset to the remote branch, regenerate files, commit them and push.

    If someone else pushed in the meantime the push is rejected; then start again
    from their commit, so the files are always rebuilt from the latest data.
    Returns (pushed, whatever regenerate() returned last time).
    """
    if not git(repo, "config", "user.email", check=False).stdout.strip():
        git(repo, "config", "user.name", BOT_NAME)
        git(repo, "config", "user.email", BOT_EMAIL)
    result = None
    for attempt in range(attempts):
        git(repo, "fetch", "--quiet", "origin", branch)
        git(repo, "reset", "--quiet", "--hard", f"origin/{branch}")
        result = regenerate()
        # A path that is neither on disk nor tracked (e.g. no timesheets yet) would make git add fail.
        present = [p for p in paths if (Path(repo) / p).exists() or git(repo, "ls-files", "--", p).stdout.strip()]
        if present:
            git(repo, "add", "--all", "--", *present)
        if git(repo, "diff", "--cached", "--quiet", check=False).returncode == 0:
            return False, result
        git(repo, "commit", "--quiet", "-m", message)
        push = git(repo, "push", "--quiet", "origin", f"HEAD:{branch}", check=False)
        if push.returncode == 0:
            return True, result
        print(f"Push was rejected (attempt {attempt + 1}); retrying from the latest commit.")
        time.sleep(2 ** attempt + random.random() * 2)
    raise RuntimeError(f"Couldn't push to {branch} after {attempts} attempts.")
