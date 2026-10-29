"""Build and publish the encrypted static portfolio snapshot to GitHub Pages.

Run this file whenever you want to push your latest local portfolio state to
GitHub Pages without running ``run.py``:

    uv run python publish.py

What it does:
1. Runs the same database migration and derived-layer check as ``run.py``.
2. Reads ``PT_ACCESS_TOKEN`` from ``.env`` (or prompts for one if not set).
3. Pre-computes every read-only API endpoint and encrypts the bundle with
   **AES-256-GCM** (600,000 PBKDF2-SHA256 iterations) into ``dist/``.
4. Encrypts a clean SQLite backup into ``dist/vault.enc`` so the scheduled
   GitHub Action can refresh daily market prices and FX rates in the cloud
   without ever storing plaintext financial data on GitHub.
5. Pushes ``dist/`` to the ``gh-pages`` branch of your ``origin`` remote.
"""

from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

DB_PATH = REPO_ROOT / "data" / "portfolio.db"
DIST_DIR = REPO_ROOT / "dist"
ENV_FILE = REPO_ROOT / ".env"
TOKEN_ENV_VAR = "PT_ACCESS_TOKEN"


def _resolve_token(*, interactive: bool = True) -> str:
    """Read PT_ACCESS_TOKEN from os.environ or .env, or prompt if interactive."""
    from portfolio_tracker.core import env

    token = (os.environ.get(TOKEN_ENV_VAR) or env.get(TOKEN_ENV_VAR) or "").strip()
    if token:
        return token

    if not interactive or not sys.stdin.isatty():
        raise RuntimeError(
            f"{TOKEN_ENV_VAR} is not set. Add `{TOKEN_ENV_VAR}=your-secret-token` "
            "to your `.env` file or environment variables."
        )

    print(
        f"\nNo {TOKEN_ENV_VAR} found in `.env`.\n"
        "This token encrypts your snapshot (AES-256-GCM) before it leaves your machine\n"
        "and is what you will type on your phone/browser to unlock GitHub Pages.\n"
    )
    while True:
        entered = getpass.getpass("Choose an access token (at least 6 characters): ").strip()
        if len(entered) < 6:
            print("Token must be at least 6 characters long.")
            continue
        confirm = getpass.getpass("Confirm access token: ").strip()
        if entered != confirm:
            print("Tokens did not match. Please try again.")
            continue
        break

    existing = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    sep = "" if (not existing or existing.endswith("\n")) else "\n"
    ENV_FILE.write_text(f"{existing}{sep}{TOKEN_ENV_VAR}={entered}\n", encoding="utf-8")
    print(f"Saved {TOKEN_ENV_VAR} to {ENV_FILE.name} (gitignored).\n")
    return entered


_SSH_GITHUB_PREFIX = "git" + "@github.com:"


def _remote_url() -> str:
    out = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    url = out.stdout.strip()
    # GitHub Pages ignores pushes authenticated with a repository SSH Deploy Key.
    # Converting SSH github.com:owner/repo.git to https://github.com/owner/repo.git
    # uses Git Credential Manager's personal user token so GitHub triggers a Pages build.
    if url.startswith(_SSH_GITHUB_PREFIX) and not os.environ.get("GITHUB_ACTIONS"):
        path = url.removeprefix(_SSH_GITHUB_PREFIX)
        return f"https://github.com/{path}"
    return url


def _git_config_value(key: str, default: str) -> str:
    out = subprocess.run(
        ["git", "config", key],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    val = out.stdout.strip()
    return val if out.returncode == 0 and val else default


def _push_gh_pages(dist_dir: Path, branch: str = "gh-pages") -> None:
    """Commit the contents of ``dist_dir`` to ``gh-pages`` and push."""
    from datetime import UTC, datetime

    remote = _remote_url()
    # GitHub Pages only triggers a new deployment when the commit author email
    # matches a verified email on the pusher's GitHub account (or github-actions[bot]).
    user_name = _git_config_value("user.name", "github-actions[bot]")
    user_email = _git_config_value(
        "user.email", "41898282+github-actions[bot]@users.noreply.github.com"
    )
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    with tempfile.TemporaryDirectory() as tmp:
        stage_dir = Path(tmp) / "site"
        import shutil

        shutil.copytree(dist_dir, stage_dir)

        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=stage_dir, check=True)

        git("init", "-b", branch)
        git("config", "user.name", user_name)
        git("config", "user.email", user_email)
        git("add", "-A")
        git("commit", "-m", f"Update encrypted portfolio snapshot ({stamp})")
        git("push", "--force", remote, f"HEAD:refs/heads/{branch}")

    _trigger_pages_build(remote, branch=branch)


def _github_repo_slug(remote: str) -> str | None:
    cleaned = remote.strip().removesuffix(".git")
    for prefix in ("https://github.com/", _SSH_GITHUB_PREFIX):
        if cleaned.startswith(prefix):
            slug = cleaned.removeprefix(prefix).strip("/")
            if "/" in slug:
                return slug
    return None


def _github_https_token() -> str | None:
    env_tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if env_tok:
        return env_tok.strip()
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        if line.startswith("password="):
            return line.removeprefix("password=").strip()
    return None


def _trigger_pages_build(remote: str, branch: str = "gh-pages") -> None:
    """Explicitly request a GitHub Pages deployment for ``branch`` via REST API."""
    import json
    import urllib.error
    import urllib.request

    slug = _github_repo_slug(remote)
    token = _github_https_token()
    if not slug or not token:
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "portfolio-tracker-publisher",
        "Content-Type": "application/json",
    }
    base_url = f"https://api.github.com/repos/{slug}/pages"
    try:
        cfg_body = json.dumps(
            {"build_type": "legacy", "source": {"branch": branch, "path": "/"}}
        ).encode("utf-8")
        req_cfg = urllib.request.Request(base_url, data=cfg_body, headers=headers, method="PUT")
        with urllib.request.urlopen(req_cfg, timeout=10):
            pass
    except (urllib.error.URLError, OSError):
        pass

    try:
        req_build = urllib.request.Request(
            f"{base_url}/builds", data=b"{}", headers=headers, method="POST"
        )
        with urllib.request.urlopen(req_build, timeout=10):
            print("Triggered GitHub Pages build deployment.", flush=True)
    except (urllib.error.URLError, OSError):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=DB_PATH,
        help="Path to SQLite portfolio database (default: data/portfolio.db).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DIST_DIR,
        help="Output directory for static site (default: dist/).",
    )
    parser.add_argument(
        "--build-only",
        action="store_true",
        help="Build dist/ locally without pushing to the gh-pages branch.",
    )
    parser.add_argument(
        "--no-vault",
        action="store_true",
        help="Do not include vault.enc (encrypted SQLite DB for CI updates) in dist/.",
    )
    parser.add_argument(
        "--decrypt-vault",
        type=Path,
        default=None,
        help="Decrypt a vault.enc file back into --db (used by GitHub Actions).",
    )
    args = parser.parse_args(argv)

    from portfolio_tracker.core import crypto
    from portfolio_tracker.export_static import build_static_site

    try:
        token = _resolve_token(interactive=not args.build_only and args.decrypt_vault is None)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.decrypt_vault is not None:
        if not args.decrypt_vault.exists():
            print(f"Vault file not found: {args.decrypt_vault}", file=sys.stderr)
            return 1
        db_bytes = crypto.decrypt_bytes(args.decrypt_vault.read_bytes(), token)
        args.db.parent.mkdir(parents=True, exist_ok=True)
        args.db.write_bytes(db_bytes)
        print(f"Decrypted {args.decrypt_vault} -> {args.db} ({len(db_bytes):,} bytes)")
        return 0

    if not args.db.exists():
        print(f"No database found at {args.db}.", file=sys.stderr)
        return 1

    if args.db.resolve() == DB_PATH.resolve():
        import run as runner

        if (problem := runner._prepare()) is not None:
            print(problem, file=sys.stderr)
            return 1

    print("Building encrypted static snapshot (AES-256-GCM)...", flush=True)
    snap_path = build_static_site(
        db_path=args.db,
        out_dir=args.out,
        token=token,
        include_encrypted_db=not args.no_vault,
    )
    size_kb = snap_path.stat().st_size / 1024
    print(f"Created {snap_path.relative_to(REPO_ROOT)} ({size_kb:.1f} KB)")

    if args.build_only:
        print(f"Static site ready in {args.out}")
        return 0

    print("Pushing encrypted site to origin/gh-pages...", flush=True)
    try:
        _push_gh_pages(args.out)
    except subprocess.CalledProcessError as exc:
        print(f"Git push failed (exit {exc.returncode}).", file=sys.stderr)
        return 1

    print("\nPublished to `gh-pages` branch!")
    print("Open your GitHub Pages URL and unlock with your PT_ACCESS_TOKEN.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
