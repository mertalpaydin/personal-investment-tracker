"""The narration layer, and the promise that the tool works without it.

The load-bearing test is
``test_the_figures_are_identical_with_and_without_a_model``. Everything else
here guards a way narration could quietly become something other than
narration: inventing figures, predicting prices, leaking a key, or spending a
free tier's quota on a page refresh.

No test in this file touches the network. The provider is injectable
throughout, exactly as `ingest/fx.py` made the ECB injectable.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_tracker.api.app import app as api_app
from portfolio_tracker.api.app import get_db, get_writable_db
from portfolio_tracker.core import env, settings
from portfolio_tracker.db import migrate as M
from portfolio_tracker.db.connection import connect
from portfolio_tracker.llm import keys, runs, tasks
from portfolio_tracker.llm.adapter import Completion, LLMRefused, LLMUnavailable, Message
from portfolio_tracker.llm.providers.gemini import GeminiProvider
from portfolio_tracker.llm.providers.null import NullProvider

VALID_KEY = "AIza" + "b" * 35


@pytest.fixture(autouse=True)
def no_ambient_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate from a real `.env` and real environment variables.

    Without this, these tests pass or fail depending on whether the machine
    running them happens to have a key configured -- which is the definition
    of a flaky test, and would have hidden a genuine bug the day somebody
    removed their key.
    """
    monkeypatch.delenv(keys.ENV_KEY, raising=False)
    monkeypatch.delenv(keys.ENV_MODEL, raising=False)
    monkeypatch.setattr(env, "DEFAULT_ENV", tmp_path / "absent.env")
    env.reload()
    yield
    env.reload()


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:", same_thread=False)
    M.migrate(conn)
    conn.execute("INSERT INTO person (id, name) VALUES (1, 'Owner')")
    conn.execute(
        "INSERT INTO account (id, person_id, name, kind, currency) "
        "VALUES (1, 1, 'Depot', 'DEPOT', 'EUR')"
    )
    conn.commit()
    return conn


@pytest.fixture
def client(db: sqlite3.Connection) -> Iterator[TestClient]:
    api_app.dependency_overrides[get_db] = lambda: db
    api_app.dependency_overrides[get_writable_db] = lambda: db
    with TestClient(api_app) as test_client:
        yield test_client
    api_app.dependency_overrides.clear()


class Fake:
    """A provider that answers with whatever it was given."""

    name = "fake"
    model = "fake-1"

    def __init__(self, text: str = "The portfolio is worth EUR 100.") -> None:
        self.text = text
        self.calls: list[list[Message]] = []

    def available(self) -> bool:
        return True

    def complete(self, messages: list[Message], **_: Any) -> Completion:
        self.calls.append(messages)
        return Completion(text=self.text, provider=self.name, model=self.model, latency_ms=5)


class Broken:
    """A provider that is always unreachable."""

    name = "broken"
    model = "broken-1"

    def available(self) -> bool:
        return True

    def complete(self, messages: list[Message], **_: Any) -> Completion:
        raise LLMUnavailable("the network is down")


PAYLOAD = {"as_of": "2026-06-30", "portfolio": {"total_value_eur": 100.0}}


# ---------------------------------------------------------------------------
# Never raising
# ---------------------------------------------------------------------------


def test_narration_never_raises_however_the_provider_fails(db: sqlite3.Connection) -> None:
    result = tasks.narrate(db, task="daily_brief", payload=PAYLOAD, provider=Broken())
    assert result.text is None
    assert result.status == "UNAVAILABLE"
    assert "network" in (result.reason or "")


def test_no_key_is_a_state_not_an_error(db: sqlite3.Connection) -> None:
    result = tasks.narrate(db, task="daily_brief", payload=PAYLOAD, provider=NullProvider())
    assert result.text is None
    assert result.status == "UNAVAILABLE"
    # Nothing logged: a configuration state recorded on every page load would
    # bury the runs that actually happened.
    assert db.execute("SELECT count(*) c FROM llm_run").fetchone()["c"] == 0


def test_an_unexpected_provider_failure_is_still_contained(db: sqlite3.Connection) -> None:
    class Exploding:
        name = "boom"
        model = "boom-1"

        def available(self) -> bool:
            return True

        def complete(self, messages: list[Message], **_: Any) -> Completion:
            raise ZeroDivisionError("something nobody anticipated")

    result = tasks.narrate(db, task="daily_brief", payload=PAYLOAD, provider=Exploding())
    assert result.status == "ERROR"
    assert result.text is None


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "The fund will likely rise further next quarter.",
        "I recommend you sell the semiconductor ETF.",
        "Its price target is EUR 200.",
        "The forecast for next year is positive.",
    ],
)
def test_output_that_predicts_or_advises_is_dropped(db: sqlite3.Connection, text: str) -> None:
    """Fails closed: no narrative rather than a forbidden one."""
    result = tasks.narrate(db, task="daily_brief", payload=PAYLOAD, provider=Fake(text))
    assert result.text is None
    assert result.status == "REFUSED"

    row = db.execute("SELECT status, output_text FROM llm_run").fetchone()
    # Kept, so drift is readable rather than lost.
    assert row["status"] == "REFUSED"
    assert row["output_text"] == text


@pytest.mark.parametrize(
    "text",
    [
        "The portfolio is worth EUR 176,648 and rose 1.2% since the previous valuation.",
        "IWDA returned 7 percentage points more over three years, at similar volatility.",
        "Prices are three days old, so every figure above rests on stale data.",
        "Advance lump-sum tax charge (Vorabpauschale) of EUR 363 falls due on 2 January.",
    ],
)
def test_ordinary_narration_is_not_caught(db: sqlite3.Connection, text: str) -> None:
    """A deny-list that suppresses good prose teaches nobody anything."""
    result = tasks.narrate(db, task="daily_brief", payload=PAYLOAD, provider=Fake(text))
    assert result.text == text
    assert result.status == "OK"


def test_the_rules_are_sent_as_a_system_instruction(db: sqlite3.Connection) -> None:
    provider = Fake()
    tasks.narrate(db, task="daily_brief", payload=PAYLOAD, provider=provider)
    system = "\n".join(m.content for m in provider.calls[0] if m.role == "system")
    assert "Use ONLY the numbers in the supplied JSON" in system
    assert "Never predict" in system
    assert "Never recommend" in system


def test_the_payload_is_the_only_data_the_model_sees(db: sqlite3.Connection) -> None:
    provider = Fake()
    tasks.narrate(db, task="daily_brief", payload=PAYLOAD, provider=provider)
    user = "\n".join(m.content for m in provider.calls[0] if m.role != "system")
    assert "176648" not in user
    assert json.dumps(PAYLOAD["portfolio"], indent=2, sort_keys=True) in user or "100.0" in user


# ---------------------------------------------------------------------------
# Cost control
# ---------------------------------------------------------------------------


def test_an_unchanged_payload_is_answered_from_the_cache(db: sqlite3.Connection) -> None:
    """Without this a free tier is exhausted by refreshing the overview."""
    provider = Fake()
    first = tasks.narrate(db, task="daily_brief", payload=PAYLOAD, provider=provider)
    second = tasks.narrate(db, task="daily_brief", payload=PAYLOAD, provider=provider)

    assert first.status == "OK"
    assert second.status == "CACHED"
    assert second.text == first.text
    assert len(provider.calls) == 1, "the second answer must not have cost a call"


def _earlier_today(conn: sqlite3.Connection) -> None:
    """Move existing runs out of the double-click window, but keep them today.

    `usage` counts calls since midnight, so backdating to another day would
    silently reset the daily cap rather than testing it.
    """
    conn.execute("UPDATE llm_run SET created_at = ?", (f"{date.today().isoformat()}T00:00:01Z",))
    conn.commit()


def test_changed_figures_are_narrated_afresh(db: sqlite3.Connection) -> None:
    provider = Fake()
    tasks.narrate(db, task="daily_brief", payload=PAYLOAD, provider=provider)
    # The cache is keyed on the payload, and this one differs -- but the
    # timestamps have to be moved out of the double-click window first, or
    # that guard is what the test would be measuring.
    _earlier_today(db)
    tasks.narrate(
        db,
        task="daily_brief",
        payload={**PAYLOAD, "as_of": "2026-07-01"},
        provider=provider,
    )
    assert len(provider.calls) == 2


def test_two_requests_in_the_same_instant_only_cost_one_call(db: sqlite3.Connection) -> None:
    """A double click is one intention, not two."""
    provider = Fake()
    tasks.narrate(db, task="daily_brief", payload=PAYLOAD, provider=provider)
    blocked = tasks.narrate(
        db, task="daily_brief", payload={**PAYLOAD, "as_of": "2026-07-09"}, provider=provider
    )
    assert blocked.status == "UNAVAILABLE"
    assert len(provider.calls) == 1


def test_the_daily_cap_stops_calls_and_says_so(db: sqlite3.Connection) -> None:
    settings.set_value(db, keys.CAP_SETTING, 1)
    provider = Fake()
    tasks.narrate(db, task="daily_brief", payload=PAYLOAD, provider=provider)
    _earlier_today(db)
    blocked = tasks.narrate(
        db, task="daily_brief", payload={**PAYLOAD, "as_of": "2026-07-02"}, provider=provider
    )
    assert blocked.status == "CAPPED"
    assert "limit" in (blocked.reason or "").lower()
    assert len(provider.calls) == 1


def test_amounts_can_be_withheld_while_percentages_are_sent(db: sqlite3.Connection) -> None:
    settings.set_value(db, keys.SEND_AMOUNTS_SETTING, False)
    provider = Fake()
    tasks.narrate(
        db,
        task="daily_brief",
        payload={"as_of": "2026-06-30", "total_value_eur": 176648.0, "twr": 0.47},
        provider=provider,
    )
    user = "\n".join(m.content for m in provider.calls[0] if m.role != "system")
    assert "176648" not in user
    assert "0.47" in user


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["short", "", "   ", "https://aistudio.google.com/apikey", "a b c"])
def test_an_obviously_wrong_key_is_refused_before_any_request(
    db: sqlite3.Connection, bad: str
) -> None:
    """The mistakes a person actually makes: a truncated paste, a URL, spaces.

    Deliberately not a format check. Google has shipped at least two key
    formats -- `AIza…` of 39 characters and `AQ.…` of 53 -- and a rule that
    knows today's rejects tomorrow's for a reason the person pasting it cannot
    do anything about. Whether a key works is the API's judgement.
    """
    with pytest.raises(keys.KeyError_, match="does not look like an API key"):
        keys.set_key(db, bad)


@pytest.mark.parametrize(
    "good",
    [
        "AIza" + "b" * 35,  # the older format
        "AQ." + "Ab8RN" * 10,  # the newer one
    ],
)
def test_both_known_key_formats_are_accepted(db: sqlite3.Connection, good: str) -> None:
    keys.set_key(db, good)
    assert keys.get_key(db) == good


def test_a_key_of_either_format_is_scrubbed_from_text() -> None:
    for key in ("AIza" + "b" * 35, "AQ." + "Ab8RN" * 10):
        assert key not in keys.redact(f"boom: {key} failed")


def test_a_key_is_never_returned_only_hinted(db: sqlite3.Connection, client: TestClient) -> None:
    client.put("/api/settings/llm", json={"api_key": VALID_KEY, "enabled": True})
    body = client.get("/api/settings/llm").json()
    assert body["configured"] is True
    assert VALID_KEY not in json.dumps(body)
    assert body["hint"].startswith("AIza")


def test_a_key_is_scrubbed_out_of_recorded_text() -> None:
    leaked = f"request failed with key={VALID_KEY} and that is that"
    assert VALID_KEY not in keys.redact(leaked)
    assert "redacted" in keys.redact(leaked)


def test_deleting_the_key_also_switches_narration_off(
    db: sqlite3.Connection, client: TestClient
) -> None:
    client.put("/api/settings/llm", json={"api_key": VALID_KEY, "enabled": True})
    client.delete("/api/settings/llm/key")
    body = client.get("/api/settings/llm").json()
    assert body["configured"] is False
    assert body["enabled"] is False


def test_narration_ships_switched_off(db: sqlite3.Connection) -> None:
    """Sending figures to a third party is opted into, never discovered."""
    assert keys.enabled(db) is False
    assert isinstance(keys.provider_for(db), NullProvider)


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------


def _fetcher(status: int, body: dict[str, Any]):
    captured: dict[str, Any] = {}

    def fetch(url: str, data: bytes, headers: dict[str, str], timeout: float):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json.loads(data.decode("utf-8"))
        return status, json.dumps(body).encode("utf-8")

    return fetch, captured


def test_the_key_travels_in_a_header_never_the_url() -> None:
    """A query string ends up in proxy logs and in exception text."""
    fetch, captured = _fetcher(
        200, {"candidates": [{"content": {"parts": [{"text": "fine"}]}, "finishReason": "STOP"}]}
    )
    GeminiProvider(VALID_KEY, fetcher=fetch).complete([Message("user", "hello")])
    assert captured["headers"]["x-goog-api-key"] == VALID_KEY
    assert VALID_KEY not in captured["url"]


def test_the_system_prompt_is_sent_as_a_system_instruction() -> None:
    fetch, captured = _fetcher(
        200, {"candidates": [{"content": {"parts": [{"text": "fine"}]}, "finishReason": "STOP"}]}
    )
    GeminiProvider(VALID_KEY, fetcher=fetch).complete(
        [Message("system", "the rules"), Message("user", "the data")]
    )
    assert captured["body"]["systemInstruction"]["parts"][0]["text"] == "the rules"
    assert captured["body"]["contents"][0]["parts"][0]["text"] == "the data"


def test_a_safety_block_is_a_refusal_not_an_outage() -> None:
    fetch, _ = _fetcher(200, {"candidates": [{"finishReason": "SAFETY", "content": {}}]})
    with pytest.raises(LLMRefused):
        GeminiProvider(VALID_KEY, fetcher=fetch).complete([Message("user", "x")])


def test_a_rate_limit_is_retried_then_reported_plainly() -> None:
    attempts: list[int] = []

    def fetch(url: str, data: bytes, headers: dict[str, str], timeout: float):
        attempts.append(1)
        return 429, b'{"error": {"message": "quota exceeded"}}'

    with pytest.raises(LLMUnavailable, match="limit has been reached"):
        GeminiProvider(VALID_KEY, fetcher=fetch, sleep=lambda _: None).complete(
            [Message("user", "x")]
        )
    assert len(attempts) == 3, "one attempt plus two retries"


def test_a_bad_key_is_not_retried() -> None:
    """Retrying a rejected key forever would never start working."""
    attempts: list[int] = []

    def fetch(url: str, data: bytes, headers: dict[str, str], timeout: float):
        attempts.append(1)
        return 403, b'{"error": {"message": "API key not valid"}}'

    with pytest.raises(LLMUnavailable, match="Check the API key"):
        GeminiProvider(VALID_KEY, fetcher=fetch, sleep=lambda _: None).complete(
            [Message("user", "x")]
        )
    assert len(attempts) == 1


def test_an_unknown_model_says_which_one() -> None:
    fetch, _ = _fetcher(404, {"error": {"message": "not found"}})
    with pytest.raises(LLMUnavailable, match="no such model"):
        GeminiProvider(VALID_KEY, model="gemini-nope", fetcher=fetch).complete(
            [Message("user", "x")]
        )


# ---------------------------------------------------------------------------
# The promise
# ---------------------------------------------------------------------------


def test_the_figures_are_identical_with_and_without_a_model(
    db: sqlite3.Connection, client: TestClient
) -> None:
    """The whole point: narration is an addition, never a dependency.

    Every figure on a narrated response must be byte-identical to the same
    response with narration off. If this ever fails, some endpoint has started
    computing something inside the narration path.
    """
    db.execute(
        "INSERT INTO portfolio_daily (date, person_id, market_value_eur, total_value_eur, "
        "cost_basis_eur, units_outstanding, unit_price, cash_eur, deposits_eur, "
        "realized_gain_eur, valuation_basis_eur, income_cash_eur, income_received_eur, "
        "completeness, drawdown_pct) VALUES ('2026-06-30', 1, '100', '100', '90', '1', "
        "'110', '0', '0', '0', '100', '0', '0', 'FULL', '0')"
    )
    db.commit()

    plain = client.get("/api/brief").json()
    client.put("/api/settings/llm", json={"api_key": VALID_KEY, "enabled": True})
    narrated = client.get("/api/brief", params={"narrate": "true"}).json()

    ignore = {"narrative", "narrative_status", "narrative_reason", "narrative_model"}
    assert {k: v for k, v in plain.items() if k not in ignore} == {
        k: v for k, v in narrated.items() if k not in ignore
    }
    # No network in tests, so the model is unreachable -- and the figures
    # arrive anyway, which is exactly the property under test.
    assert narrated["narrative"] is None


def test_the_run_log_records_what_was_sent(db: sqlite3.Connection) -> None:
    """What makes "it only narrates our figures" checkable rather than claimed."""
    tasks.narrate(db, task="daily_brief", payload=PAYLOAD, provider=Fake("all fine"))
    row = db.execute("SELECT input_json, output_text, status FROM llm_run").fetchone()
    assert row["status"] == "OK"
    assert json.loads(row["input_json"])["portfolio"]["total_value_eur"] == 100.0
    assert row["output_text"] == "all fine"


def test_an_unknown_task_is_refused_rather_than_sent(db: sqlite3.Connection) -> None:
    provider = Fake()
    result = tasks.narrate(db, task="not_a_task", payload=PAYLOAD, provider=provider)
    assert result.status == "ERROR"
    assert provider.calls == []


def test_the_usage_view_counts_only_successful_calls(db: sqlite3.Connection) -> None:
    tasks.narrate(db, task="daily_brief", payload=PAYLOAD, provider=Fake("fine"))
    _earlier_today(db)
    tasks.narrate(
        db, task="daily_brief", payload={**PAYLOAD, "as_of": "2026-07-03"}, provider=Broken()
    )
    used = runs.usage(db, daily_cap=50)
    assert used.calls_today == 1
