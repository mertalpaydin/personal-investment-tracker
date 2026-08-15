"""``pt accounts`` -- creating deposit accounts and importing their statements.

Interest rates are where a hundredfold error hides in plain sight. ``0.415``
and ``41.5`` are both plausible readings of "41.5%", and a term deposit stored
at 0.415% still produces a number, still accrues, and still balances -- it is
simply a hundred times too small, and nothing about the output says so. Two
tests here exist only for that: one that a rate typed either way means the same
thing, and one that importing a statement over an account does not re-divide a
rate the parser already converted.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from portfolio_tracker.cli import app
from portfolio_tracker.db.connection import connect

runner = CliRunner()
D = Decimal

INTEREST_STATEMENT = (
    "date,added capital,interest gain,tax,net\n"
    "01/08/2026,0,43.06,11.36,31.70\n"
    "01/07/2026,0,41.61,10.97,30.64\n"
    "01/04/2026,20186.08,0,0,0\n"
)

TERM_STATEMENT = (
    "start date,capital,interest rate,return date,total return,tax,net return\n"
    "14/08/2026,176177,41.5,15/09/2026,182586.95,1121.74,181465.21\n"
)


def fresh(tmp_path: Path) -> Path:
    target = tmp_path / "p.db"
    runner.invoke(app, ["init-db", "--db", str(target)])
    return target


def rate_of(db: Path, account_id: int) -> Decimal:
    conn = connect(db, read_only=True)
    try:
        return D(
            str(
                conn.execute(
                    "SELECT interest_rate FROM account WHERE id = ?", (account_id,)
                ).fetchone()[0]
            )
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Creating an account
# ---------------------------------------------------------------------------


def test_a_deposit_account_can_be_created_without_writing_sql(tmp_path: Path) -> None:
    """The gap this command closes: no CSV anywhere mentions a deposit account."""
    db = fresh(tmp_path)
    result = runner.invoke(
        app,
        [
            "accounts",
            "add",
            "--name",
            "Tagesgeld",
            "--kind",
            "TAGESGELD",
            "--rate",
            "2.5",
            "--first-accrual",
            "2026-04-01",
            "--db",
            str(db),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "created account 2" in result.output
    assert rate_of(db, 2) == D("0.025")


def test_a_rate_means_the_same_typed_either_way(tmp_path: Path) -> None:
    """2.5 and 0.025 are both "two and a half percent" to a person typing fast.

    Reading one as the other is off by a hundred and compounds silently for a
    year before the balance looks wrong enough to question.
    """
    db = fresh(tmp_path)
    for value in ("2.5", "0.025"):
        runner.invoke(
            app,
            [
                "accounts",
                "add",
                "--name",
                f"A{value}",
                "--kind",
                "TAGESGELD",
                "--rate",
                value,
                "--first-accrual",
                "2026-04-01",
                "--db",
                str(db),
            ],
        )
    assert rate_of(db, 2) == rate_of(db, 3) == D("0.025")


def test_a_rate_without_a_value_date_is_refused_in_words(tmp_path: Path) -> None:
    """The schema forbids it; the CHECK text names a column, not the problem."""
    db = fresh(tmp_path)
    result = runner.invoke(
        app,
        ["accounts", "add", "--name", "X", "--kind", "TAGESGELD", "--rate", "2.5", "--db", str(db)],
    )
    assert result.exit_code == 1
    assert "--first-accrual" in result.output


def test_an_unknown_kind_is_refused(tmp_path: Path) -> None:
    db = fresh(tmp_path)
    result = runner.invoke(
        app, ["accounts", "add", "--name", "X", "--kind", "SPARBUCH", "--db", str(db)]
    )
    assert result.exit_code == 1
    assert "kind" in result.output


def test_creating_a_deposit_account_gives_it_a_cash_instrument(tmp_path: Path) -> None:
    """A balance is modelled as a position, so FX revaluation needs no special case."""
    db = fresh(tmp_path)
    runner.invoke(
        app,
        [
            "accounts",
            "add",
            "--name",
            "TRY savings",
            "--kind",
            "FESTGELD",
            "--currency",
            "TRY",
            "--db",
            str(db),
        ],
    )
    conn = connect(db, read_only=True)
    try:
        row = conn.execute(
            "SELECT i.currency, i.is_cash_proxy FROM instrument i "
            "JOIN account a ON a.cash_instrument_id = i.id WHERE a.id = 2"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row["is_cash_proxy"] == 1
    assert row["currency"] == "TRY"


def test_a_withholding_rate_is_recorded(tmp_path: Path) -> None:
    """Turkish stopaj is taken at source; accruing gross drifts high forever."""
    db = fresh(tmp_path)
    runner.invoke(
        app,
        [
            "accounts",
            "add",
            "--name",
            "TR",
            "--kind",
            "FESTGELD",
            "--currency",
            "TRY",
            "--withholding",
            "17.5",
            "--db",
            str(db),
        ],
    )
    listing = runner.invoke(app, ["accounts", "list", "--db", str(db)]).output
    assert "17.500% withheld at source" in listing


# ---------------------------------------------------------------------------
# Importing a statement
# ---------------------------------------------------------------------------


def _deposit_account(tmp_path: Path, **extra: str) -> Path:
    db = fresh(tmp_path)
    args = ["accounts", "add", "--name", "Deposit", "--kind", "TAGESGELD", "--db", str(db)]
    for key, value in extra.items():
        args += [f"--{key.replace('_', '-')}", value]
    runner.invoke(app, args)
    return db


def test_a_statement_books_capital_and_interest(tmp_path: Path) -> None:
    db = _deposit_account(tmp_path)
    path = tmp_path / "stmt.csv"
    path.write_text(INTEREST_STATEMENT, encoding="utf-8")
    result = runner.invoke(
        app, ["accounts", "import", str(path), "--account", "2", "--db", str(db)]
    )
    assert result.exit_code == 0, result.output
    assert "1 capital movement(s) and 2 interest payment(s)" in result.output


def test_interest_is_booked_as_income_not_as_saving(tmp_path: Path) -> None:
    """The single most important column value in this file.

    ``flow_class = 'EXTERNAL_IN'`` would make unit accounting issue units for
    the interest, cancelling exactly the return it represents -- and the tool
    would report the owner as having saved his own interest.
    """
    db = _deposit_account(tmp_path)
    path = tmp_path / "stmt.csv"
    path.write_text(INTEREST_STATEMENT, encoding="utf-8")
    runner.invoke(app, ["accounts", "import", str(path), "--account", "2", "--db", str(db)])

    conn = connect(db, read_only=True)
    try:
        rows = conn.execute(
            "SELECT type, flow_class, tax_withheld_local FROM transaction_ ORDER BY type, id"
        ).fetchall()
    finally:
        conn.close()
    kinds = {(r["type"], r["flow_class"]) for r in rows}
    assert ("INTEREST", "INCOME") in kinds
    assert ("DEPOSIT", "EXTERNAL_IN") in kinds
    assert any(r["tax_withheld_local"] > D(0) for r in rows if r["type"] == "INTEREST")


def test_importing_the_same_statement_twice_books_nothing_new(tmp_path: Path) -> None:
    db = _deposit_account(tmp_path)
    path = tmp_path / "stmt.csv"
    path.write_text(INTEREST_STATEMENT, encoding="utf-8")
    runner.invoke(app, ["accounts", "import", str(path), "--account", "2", "--db", str(db)])
    again = runner.invoke(app, ["accounts", "import", str(path), "--account", "2", "--db", str(db)])
    assert "0 capital movement(s) and 0 interest payment(s); 3 already present" in again.output


def test_a_dry_run_writes_nothing(tmp_path: Path) -> None:
    db = _deposit_account(tmp_path)
    path = tmp_path / "stmt.csv"
    path.write_text(INTEREST_STATEMENT, encoding="utf-8")
    result = runner.invoke(
        app, ["accounts", "import", str(path), "--account", "2", "--dry-run", "--db", str(db)]
    )
    assert "would book" in result.output
    conn = connect(db, read_only=True)
    try:
        assert conn.execute("SELECT count(*) FROM transaction_").fetchone()[0] == 0
    finally:
        conn.close()


def test_a_statement_cannot_be_booked_against_a_depot(tmp_path: Path) -> None:
    """Interest on a brokerage account would misstate both balances."""
    db = _deposit_account(tmp_path)
    path = tmp_path / "stmt.csv"
    path.write_text(INTEREST_STATEMENT, encoding="utf-8")
    result = runner.invoke(
        app, ["accounts", "import", str(path), "--account", "1", "--db", str(db)]
    )
    assert result.exit_code == 1
    assert "not a deposit account" in result.output


def test_a_term_statement_sets_the_rate_it_states_not_a_hundredth_of_it(
    tmp_path: Path,
) -> None:
    """The parser already converts "41.5" to 0.415. Converting twice gave 0.415%.

    A 41.5% Turkish term deposit accruing like a current account still produces
    a plausible-looking number, which is exactly why it needs a test.
    """
    db = fresh(tmp_path)
    runner.invoke(
        app,
        [
            "accounts",
            "add",
            "--name",
            "TR term",
            "--kind",
            "FESTGELD",
            "--currency",
            "TRY",
            "--rollover",
            "PAYOUT",
            "--db",
            str(db),
        ],
    )
    path = tmp_path / "term.csv"
    path.write_text(TERM_STATEMENT, encoding="utf-8")
    result = runner.invoke(
        app, ["accounts", "import", str(path), "--account", "2", "--db", str(db)]
    )
    assert result.exit_code == 0, result.output
    assert rate_of(db, 2) == D("0.415")
    assert "rate set to 41.50%" in result.output


def test_a_term_statement_is_checked_against_its_own_arithmetic(tmp_path: Path) -> None:
    db = fresh(tmp_path)
    runner.invoke(
        app,
        [
            "accounts",
            "add",
            "--name",
            "TR term",
            "--kind",
            "FESTGELD",
            "--currency",
            "TRY",
            "--db",
            str(db),
        ],
    )
    path = tmp_path / "term.csv"
    path.write_text(TERM_STATEMENT, encoding="utf-8")
    result = runner.invoke(
        app, ["accounts", "import", str(path), "--account", "2", "--db", str(db)]
    )
    assert "interest verified" in result.output
    assert "stopaj" in result.output


# ---------------------------------------------------------------------------
# Accrual
# ---------------------------------------------------------------------------


def test_accrue_reports_what_is_earned_but_unpaid(tmp_path: Path) -> None:
    """A real amount the balance does not show and no statement reports."""
    db = _deposit_account(tmp_path, rate="2.5", first_accrual="2026-04-01")
    path = tmp_path / "stmt.csv"
    path.write_text(INTEREST_STATEMENT, encoding="utf-8")
    runner.invoke(app, ["accounts", "import", str(path), "--account", "2", "--db", str(db)])
    result = runner.invoke(
        app, ["accounts", "accrue", "--account", "2", "--end", "2026-08-15", "--db", str(db)]
    )
    assert result.exit_code == 0, result.output
    assert "earned and not yet paid" in result.output


def test_accrual_without_a_value_date_refuses(tmp_path: Path) -> None:
    db = _deposit_account(tmp_path)
    result = runner.invoke(
        app, ["accounts", "accrue", "--account", "2", "--end", "2026-08-15", "--db", str(db)]
    )
    assert result.exit_code == 1
    assert "first-accrual" in result.output
