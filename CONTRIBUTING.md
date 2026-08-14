# Contributing

This is a personal project, but the working rules are written down because one of
them is load-bearing for privacy.

## The one rule that matters

**Never run `git add .` or `git add -A` in this repository.**

Stage explicit paths, always:

```bash
git add src/portfolio_tracker/core/fx.py tests/test_fx.py
```

This repository holds a real investment portfolio in `data/` while being published
publicly. `.gitignore` denies `data/` by default and `scripts/check_no_pii.py` runs
as a pre-commit hook and again in CI, so a blanket add would almost certainly be
caught. "Almost certainly" is not the standard a financial record deserves, and the
guard cannot know about a file type nobody anticipated.

If you genuinely need to commit something the ignore rules block, use `git add -f`
and say why in the commit message. The friction is deliberate.

## Setup

```bash
uv sync
pre-commit install
```

`pre-commit install` is not optional. Without it the PII guard only runs in CI,
which means a leak is caught after it has already been pushed — and a secret in a
pushed commit is a secret in the repository forever, whether or not you delete it
in a later commit.

## Data

| Path | Status |
|---|---|
| `data/open.csv`, `data/closed.csv` | Real exports. Never committed. |
| `data/example/*` | Synthetic, generated, committed. Safe. |
| `config.yaml`, `seeds/*.yaml`, `.env` | Real. Never committed. `*.example.*` versions are. |
| `reports/` | Import reports quote real holdings. Never committed. |

Run the project against the synthetic corpus by default:

```bash
uv run pt import investing --open data/example/open.csv --closed data/example/closed.csv
```

### Regenerating the example data

```bash
uv run python scripts/make_example_data.py --seed 20260814 --out data/example
uv run python scripts/make_example_data.py --check    # CI runs this
```

The generator is deterministic: the same seed produces byte-identical output. CI
fails if the committed files differ from a fresh generation by a single byte, which
makes the corpus tamper-evident and stops hand-tweaking from drifting it away from
`truth.json`.

**Do not hand-edit anything under `data/example/`.** Those files encode a UTF-8 BOM,
CRLF line endings, a trailing U+00A0 on every `Name` cell, and `K`-abbreviated
quantities. Every one of those is a real property of the upstream export that the
parser must handle. An editor that strips trailing whitespace or normalises line
endings will leave the tests passing while they test nothing. `.gitattributes` marks
these files `-text` and `.editorconfig` exempts them, but neither can stop a
deliberate save.

## Tests

```bash
uv run pytest -q                 # all
uv run pytest tests/test_parse.py -q
uv run ruff check . && uv run ruff format --check .
uv run mypy src
```

Money and quantities are `Decimal`, never `float`. A project whose acceptance
criterion is stated in cents cannot use binary floating point for money. `mypy` is
strict over `core/` and `importers/` specifically because `Decimal`-vs-`float`
confusion is the bug class that matters here.

## Commit style

Explain *why*, not *what* — the diff already says what. Findings about the source
data belong in `docs/adr/`, not buried in a commit body.
