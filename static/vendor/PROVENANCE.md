# Vendored third-party assets

Committed rather than fetched, so the page works with no network and no build
step. A CDN reference is the failure mode where the app works on the machine it
was written on and shows blank charts on the machine it was written for.

`tests/test_static_assets.py` re-checks every hash below, and separately asserts
that no file under `static/` references a remote host. Replacing a file without
updating its hash here fails the suite.

## uPlot 1.6.32

- Upstream: <https://github.com/leeoniya/uPlot>
- Fetched from: `https://cdn.jsdelivr.net/npm/uplot@1.6.32/dist/`
- Licence: MIT (see `LICENSE` in this directory)

| File | Bytes | SHA-256 |
|---|---:|---|
| `uPlot.iife.js` | 150232 | `1b71fc5e6b5b572922ed9941ed21d067207c8e5ecac0d35de66fd65d9686e791` |
| `uPlot.min.css` | 1857 | `df630c6a8d6f8eeaff264b50f73ce5b114f646ffd9a0bb74f049b0a00135fa04` |
| `LICENSE` | 1078 | `8f989229699b4fe2f1a0432d0e9edc338a8a911e250e2d1b01ecd770a5f5b1bd` |

CRLF was normalised to LF on the way in, so the hashes are stable regardless of
git's autocrlf setting. `.gitattributes` pins this directory to `-text` to keep
it that way.

### Why this library

Almost every chart here is a time series over daily data — portfolio value,
unit price, drawdown, one holding's price, an FX rate. uPlot is a time-series
library specifically, renders several years of daily points without complaint,
ships as an IIFE that needs no bundler, and fits under this repo's
`check-added-large-files --maxkb=256` limit *unminified*, which matters more
than it sounds: the PII guard scans `.js` line by line, and a 47 KB single line
is both unreadable and a false-positive risk.

The non-minified build is committed deliberately. It is inspectable, which is
the same reason this project has no build step at all.

### What it is deliberately not used for

uPlot draws no pie, donut or treemap, and none is wanted. Allocation,
concentration and return attribution render as labelled CSS bars, which are
easier to read than a donut at the twenty-odd rows this portfolio produces, and
which need no library at all.
