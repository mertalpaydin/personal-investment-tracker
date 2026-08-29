"""The front end's two structural guarantees, mechanised.

Both exist because the failure they prevent is invisible on the machine the
code was written on:

``test_nothing_under_static_references_a_remote_host``
    A CDN link works perfectly for whoever added it and shows an unstyled page
    with no charts to whoever opens the app offline. The app reads a database
    that is already on the laptop; it has no business needing the network to
    draw it.

``test_no_javascript_file_uses_innerhtml``
    Instrument names arrive from a broker's CSV export. Nobody audits them, and
    one containing markup would execute the moment it were concatenated into
    innerHTML. The page this replaced did exactly that in its sale-pricing
    script, so the rule is enforced rather than remembered.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "portfolio_tracker" / "api" / "static"
VENDOR = STATIC / "vendor"
PROVENANCE = VENDOR / "PROVENANCE.md"

#: Anything that would make the browser open a connection off this origin.
REMOTE = re.compile(r"""(?:https?:)?//(?!\s)[\w.-]+\.[a-z]{2,}""", re.IGNORECASE)

#: Rows of the checksum table in PROVENANCE.md: | `file` | bytes | `sha256` |
_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*([\d,]+)\s*\|\s*`([0-9a-f]{64})`\s*\|", re.MULTILINE)


def _text_files(suffixes: tuple[str, ...], *, include_vendor: bool = False) -> list[Path]:
    return sorted(
        p
        for p in STATIC.rglob("*")
        if p.is_file() and p.suffix in suffixes and (include_vendor or VENDOR not in p.parents)
    )


# ---------------------------------------------------------------------------
# The assets are actually there
# ---------------------------------------------------------------------------


def test_the_shell_and_its_stylesheet_exist() -> None:
    assert (STATIC / "index.html").is_file()
    assert (STATIC / "app.css").is_file()
    assert (STATIC / "js" / "main.js").is_file()


def test_every_view_the_router_names_exists() -> None:
    """A nav entry pointing at a module that was never written is a blank page.

    main.js maps route patterns to dynamic imports, and a dynamic import only
    fails at click time -- which is to say, in front of the user.
    """
    main = (STATIC / "js" / "main.js").read_text(encoding="utf-8")
    imported = set(re.findall(r"""import\(["'](\./[\w./-]+\.js)["']\)""", main))
    assert imported, "main.js declares no views; the route table must have moved"
    missing = [ref for ref in imported if not (STATIC / "js" / ref[2:]).is_file()]
    assert not missing, f"main.js imports modules that do not exist: {sorted(missing)}"


def test_every_static_module_imports_only_files_that_exist() -> None:
    broken: list[str] = []
    for path in _text_files((".js",)):
        for ref in re.findall(r"""from\s+["'](\.[\w./-]+\.js)["']""", path.read_text("utf-8")):
            if not (path.parent / ref).resolve().is_file():
                broken.append(f"{path.relative_to(STATIC)} -> {ref}")
    assert not broken, f"broken relative imports: {broken}"


# ---------------------------------------------------------------------------
# Offline
# ---------------------------------------------------------------------------


def test_nothing_under_static_references_a_remote_host() -> None:
    offenders: list[str] = []
    for path in _text_files((".html", ".css", ".js")):
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for found in REMOTE.findall(line):
                # SVG and XML namespace URLs are identifiers, not fetches: the
                # browser never resolves them.
                if "w3.org" in found or "schemas.microsoft.com" in found:
                    continue
                offenders.append(f"{path.relative_to(STATIC)}:{line_no} {found}")
    assert not offenders, (
        "static assets must be self-contained so the app works offline; found: " + str(offenders)
    )


def test_the_shell_loads_the_vendored_chart_library_not_a_cdn() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "/static/vendor/uPlot.iife.js" in html
    assert "/static/vendor/uPlot.min.css" in html


# ---------------------------------------------------------------------------
# XSS
# ---------------------------------------------------------------------------


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """Lines with comments removed, so prose about a rule is not a breach of it.

    Crude on purpose: a `//` inside a string literal would be mistaken for a
    comment. There are no URLs in this codebase's JavaScript -- a separate test
    enforces that -- so the one case where it would matter cannot arise.
    """
    out: list[tuple[int, str]] = []
    in_block = False
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw
        if in_block:
            end = line.find("*/")
            if end == -1:
                continue
            line, in_block = line[end + 2 :], False
        while "/*" in line:
            start = line.find("/*")
            end = line.find("*/", start + 2)
            if end == -1:
                line, in_block = line[:start], True
                break
            line = line[:start] + line[end + 2 :]
        line = line.split("//", 1)[0]
        if line.strip():
            out.append((n, line))
    return out


@pytest.mark.parametrize(
    "banned", ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"]
)
def test_no_javascript_file_writes_markup_from_a_string(banned: str) -> None:
    offenders = [
        f"{p.relative_to(STATIC)}:{n}"
        for p in _text_files((".js",))
        for n, line in _code_lines(p)
        if banned in line
    ]
    assert not offenders, (
        f"{banned} builds DOM from a string, and instrument names come from an "
        f"unaudited CSV; use dom.js el()/text instead. Found at: {offenders}"
    )


# ---------------------------------------------------------------------------
# Vendored bytes
# ---------------------------------------------------------------------------


def test_provenance_records_every_vendored_file() -> None:
    recorded = {name for name, _, _ in _ROW.findall(PROVENANCE.read_text(encoding="utf-8"))}
    present = {p.name for p in VENDOR.iterdir() if p.is_file() and p.name != "PROVENANCE.md"}
    assert present == recorded, (
        "every vendored file needs a recorded checksum, and every recorded "
        f"checksum needs a file. present={sorted(present)} recorded={sorted(recorded)}"
    )


def test_vendored_files_match_their_recorded_checksums() -> None:
    """A vendored library is only trustworthy if its bytes are pinned.

    This also catches the quieter failure: a pre-commit hook reformatting the
    file. `.pre-commit-config.yaml` excludes this directory precisely so that
    cannot happen, and this test is what notices if the exclude is ever lost.
    """
    rows = _ROW.findall(PROVENANCE.read_text(encoding="utf-8"))
    assert rows, "PROVENANCE.md has no checksum table"
    for name, size, digest in rows:
        blob = (VENDOR / name).read_bytes()
        assert len(blob) == int(size.replace(",", "")), f"{name} changed size"
        assert hashlib.sha256(blob).hexdigest() == digest, f"{name} does not match its checksum"


def test_the_vendored_library_is_small_enough_to_commit() -> None:
    """Mirrors the `check-added-large-files --maxkb=256` pre-commit hook.

    Kept here too so the limit is visible to anyone swapping the library, not
    just to whoever happens to run the hook.
    """
    for path in VENDOR.iterdir():
        if path.is_file():
            assert path.stat().st_size <= 256 * 1024, f"{path.name} exceeds the 256 KB limit"


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_modules_must_be_revalidated_before_reuse() -> None:
    """A stale module is a broken page, not a stale stylesheet.

    Starlette sends ETag and Last-Modified but no Cache-Control, which leaves a
    browser free to apply heuristic caching and serve yesterday's copy without
    asking. On a page assembled from a dozen ES modules that surfaces as one
    file calling a function that only exists in a newer one -- and it works
    perfectly for whoever shipped it, which is what makes it worth a test.
    """
    from fastapi.testclient import TestClient

    from portfolio_tracker.api.app import app

    with TestClient(app) as client:
        for path in ("/static/js/main.js", "/static/js/fmt.js", "/static/app.css"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert "no-store" in response.headers.get("cache-control", ""), path

        # Still cheap when the browser does ask: the ETag turns a repeat
        # request into a 304 with no body.
        first = client.get("/static/js/fmt.js")
        again = client.get("/static/js/fmt.js", headers={"If-None-Match": first.headers["etag"]})
        assert again.status_code == 304


def test_the_vendored_library_is_cached_hard() -> None:
    """Its name carries its version and its bytes are checksummed.

    The one file here large enough for caching to be worth anything, and the
    one that cannot silently change underneath a page.
    """
    from fastapi.testclient import TestClient

    from portfolio_tracker.api.app import app

    with TestClient(app) as client:
        cached = client.get("/static/vendor/uPlot.iife.js").headers.get("cache-control", "")
    assert "immutable" in cached
