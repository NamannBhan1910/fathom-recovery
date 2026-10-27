#!/usr/bin/env python3
"""Create this private GitHub repo under the intended personal account and push it.

Run once from this repository in an interactive terminal. The token is read with
getpass, used only for the API calls and child Git process, and never persisted.
"""
from __future__ import annotations

import base64
import getpass
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path


OWNER = "NamannBhan1910"
NAME = "fathom-recovery"
TAG = "v1.2.0"
API = "https://api.github.com"
REPO_DIR = Path(__file__).resolve().parent


def find_git() -> str:
    candidates = [
        shutil.which("git"),
        Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/git/cmd/git.exe",
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/cmd/git.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise RuntimeError("Git was not found. Install Git for Windows or set Git on PATH, then retry.")


def git(*args: str, token: str | None = None) -> str:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    basic = None
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {basic}"
    result = subprocess.run([find_git(), *args], cwd=REPO_DIR, env=env,
                            capture_output=True, text=True, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        if token:
            detail = detail.replace(token, "***").replace(basic, "***")
        raise RuntimeError(f"git {args[0]} failed: {detail}")
    return result.stdout.strip()


def api(path: str, token: str, data: dict | None = None) -> dict:
    payload = json.dumps(data).encode() if data is not None else None
    request = urllib.request.Request(
        API + path, data=payload,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub API {path} returned HTTP {exc.code}. Check token permissions and account access.") from None


def main() -> None:
    if git("status", "--porcelain"):
        raise RuntimeError("Commit local changes before publishing")
    git("rev-parse", "--verify", "main")
    git("rev-parse", "--verify", TAG)
    token = getpass.getpass("GitHub token (masked; not saved): ").strip()
    if not token:
        raise RuntimeError("A GitHub token is required")
    identity = api("/user", token)
    login = identity.get("login", "")
    if login.lower() != OWNER.lower():
        raise RuntimeError(f"Signed in as {login!r}; expected {OWNER!r}. No repository was created.")

    path = f"/repos/{OWNER}/{NAME}"
    try:
        repository = api(path, token)
        print("Using the existing private repository after verifying its owner and visibility.")
    except RuntimeError as exc:
        if "HTTP 404" not in str(exc):
            raise
        repository = api("/user/repos", token, {
            "name": NAME,
            "description": "Shared Fathom panel recovery helper",
            "private": True,
            "auto_init": False,
        })
        print("Created the private repository.")
    if not repository.get("private") or repository.get("owner", {}).get("login", "").lower() != OWNER.lower():
        raise RuntimeError("Repository owner or visibility did not match; stopping before push")

    remote = repository["clone_url"]
    try:
        configured = git("remote", "get-url", "origin")
    except RuntimeError:
        git("remote", "add", "origin", remote)
    else:
        if configured != remote:
            raise RuntimeError("origin points to a different URL; verify it before publishing")
    git("push", "origin", "main", TAG, token=token)
    print(f"Published: {repository['html_url']} (private, main and {TAG})")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
