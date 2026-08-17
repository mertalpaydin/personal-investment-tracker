"""Turning a computed answer into sentences, or not, without ever failing.

:func:`narrate` does not raise. Every path -- no key, narration off, quota
gone, network down, a refusal, a guardrail trip -- returns a
:class:`Narration` whose ``text`` is ``None`` and whose ``reason`` says which.
Endpoints therefore add ``narrative`` to a body they have already finished
computing, and no endpoint's correctness depends on a third party answering.

That is tested rather than asserted: `tests/test_llm.py` runs each task with a
provider that always raises and checks the deterministic block is byte-identical
to the one produced with a working provider.

The guardrail check after the fact is a deny-list, and a deny-list is not a
safety mechanism. It is here because it fails *closed* and can be tested: a
sentence that predicts a price is dropped, the run is recorded as REFUSED, and
the offending text is kept in `llm_run` so it can be read. Its real job is
catching a prompt that has drifted, not defeating a determined model.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from portfolio_tracker.llm import keys, runs
from portfolio_tracker.llm.adapter import LLMProvider, LLMRefused, LLMUnavailable, Message

PROMPTS = Path(__file__).parent / "prompts"

TASKS = ("daily_brief", "peer_scan", "tax_explainer", "harvest_plan")

#: Output that breaks a guardrail badly enough to drop the whole answer.
#: Narrow on purpose: a pattern that also matches honest prose would suppress
#: good narration and teach nobody anything.
FORBIDDEN: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(will|should|expect\w*|likely)\b[^.]{0,40}"
            r"\b(rise|fall|reach|outperform|drop)\b",
            re.I,
        ),
        "predicted a price movement",
    ),
    (
        re.compile(
            r"\b(recommend|advise|you should)\b[^.]{0,30}\b(buy|sell|switch|move|invest)\b",
            re.I,
        ),
        "recommended a trade",
    ),
    (re.compile(r"\bprice target\b|\bforecasts?\b|\bprojected return\b", re.I), "forecast"),
)


@dataclass(frozen=True)
class Narration:
    """Prose, or the reason there is none."""

    text: str | None = None
    #: OK | CACHED | UNAVAILABLE | REFUSED | ERROR | CAPPED
    status: str = "UNAVAILABLE"
    reason: str | None = None
    model: str | None = None
    run_id: int | None = None

    @property
    def ok(self) -> bool:
        return self.text is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "narrative": self.text,
            "narrative_status": self.status,
            "narrative_reason": self.reason,
            "narrative_model": self.model,
        }


@lru_cache(maxsize=8)
def prompt(name: str) -> str:
    path = PROMPTS / f"{name}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def check_output(text: str) -> str | None:
    """The guardrail that was broken, or None."""
    for pattern, what in FORBIDDEN:
        if pattern.search(text):
            return what
    return None


def strip_amounts(payload: Any) -> Any:
    """Remove absolute euro figures, keeping percentages and differences.

    For somebody who would rather not send their net worth to a third party.
    The peer comparison and most of a brief read perfectly well on percentages
    alone, so this is a real option rather than a token one.
    """
    if isinstance(payload, dict):
        return {
            key: strip_amounts(value)
            for key, value in payload.items()
            if not (key.endswith("_eur") or key.endswith("_eur_value"))
        }
    if isinstance(payload, list):
        return [strip_amounts(item) for item in payload]
    return payload


def narrate(
    conn: sqlite3.Connection,
    *,
    task: str,
    payload: dict[str, Any],
    subject: str | None = None,
    provider: LLMProvider | None = None,
    max_sentences: int = 6,
) -> Narration:
    """Ask for prose about ``payload``. Never raises.

    ``conn`` is used to read settings and to write the run log. Nothing here
    reads portfolio data: whatever the model is told is entirely in ``payload``,
    which the caller computed.
    """
    if task not in TASKS:
        return Narration(status="ERROR", reason=f"unknown task {task!r}")

    chosen = provider if provider is not None else keys.provider_for(conn)
    if not keys.send_amounts(conn):
        payload = strip_amounts(payload)

    body = dict(payload)
    body.setdefault("task", task)
    digest = runs.fingerprint(body)
    as_json = keys.redact(json.dumps(body, sort_keys=True, default=str), keys.get_key(conn))

    # -- a fresh answer for identical inputs already exists ----------------
    hit = runs.cached(conn, task=task, input_sha256=digest)
    if hit is not None and hit["output_text"]:
        return Narration(
            text=str(hit["output_text"]),
            status="CACHED",
            reason="unchanged since this was last written",
            model=str(hit["model"]),
            run_id=int(hit["id"]),
        )

    def log(status: str, **fields: Any) -> int:
        return runs.record(
            conn,
            task=task,
            subject=subject,
            provider=getattr(chosen, "name", "none"),
            model=getattr(chosen, "model", "none"),
            status=status,
            input_sha256=digest,
            input_json=as_json,
            **fields,
        )

    if not chosen.available():
        # Not logged: "no key is set" is a configuration state, not an event,
        # and recording one per page load would bury the real runs.
        try:
            chosen.complete([])
        except LLMUnavailable as exc:
            return Narration(status="UNAVAILABLE", reason=str(exc))
        except Exception as exc:
            return Narration(status="ERROR", reason=str(exc))
        return Narration(status="UNAVAILABLE", reason="narration is not configured")

    used = runs.usage(conn, daily_cap=keys.daily_cap(conn))
    if used.capped:
        run_id = log("CAPPED", error=f"{used.calls_today} of {used.daily_cap} calls used today")
        return Narration(
            status="CAPPED",
            reason=(
                f"Today's limit of {used.daily_cap} narrations has been used. "
                "The figures above are unaffected."
            ),
            run_id=run_id,
        )
    if used.too_soon:
        return Narration(
            status="UNAVAILABLE",
            reason="Another narration was requested a moment ago; try again shortly.",
        )

    messages = [
        Message("system", prompt("system")),
        Message(
            "user",
            f"{prompt(task).format(max_sentences=max_sentences)}\n\n"
            f"```json\n{json.dumps(body, indent=2, sort_keys=True, default=str)}\n```",
        ),
    ]

    try:
        completion = chosen.complete(messages, max_output_tokens=700)
    except LLMUnavailable as exc:
        run_id = log("UNAVAILABLE", error=keys.redact(str(exc), keys.get_key(conn)))
        return Narration(status="UNAVAILABLE", reason=str(exc), run_id=run_id)
    except LLMRefused as exc:
        run_id = log("REFUSED", error=keys.redact(str(exc), keys.get_key(conn)))
        return Narration(status="REFUSED", reason=str(exc), run_id=run_id)
    except Exception as exc:
        run_id = log("ERROR", error=keys.redact(f"{type(exc).__name__}: {exc}", keys.get_key(conn)))
        return Narration(status="ERROR", reason=str(exc), run_id=run_id)

    broken = check_output(completion.text)
    if broken is not None:
        # Recorded with the text, so the drift is readable rather than lost.
        run_id = log(
            "REFUSED",
            output_text=completion.text,
            error=f"guardrail: {broken}",
            latency_ms=completion.latency_ms,
            http_status=completion.http_status,
        )
        return Narration(
            status="REFUSED",
            reason=f"The model {broken}, which this tool does not publish.",
            model=completion.model,
            run_id=run_id,
        )

    run_id = log(
        "OK",
        output_text=completion.text,
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
        latency_ms=completion.latency_ms,
        http_status=completion.http_status,
    )
    return Narration(
        text=completion.text,
        status="OK",
        model=completion.model,
        run_id=run_id,
    )
