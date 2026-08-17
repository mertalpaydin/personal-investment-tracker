"""The seam between this tool and any language model.

Everything above this file works in terms of :class:`Message` and
:class:`Completion` and knows nothing about who answers. Everything that knows
a provider exists lives under ``providers/``. That is not architectural
politeness -- it is what makes the guarantee below enforceable:

**Every figure this tool reports is computed here, in Python, from stored
data. A model only ever writes sentences about numbers it was handed.**

If a provider could reach the database, or if the narration path could compute
anything, that guarantee would rest on discipline. Instead the provider is
handed two strings and returns one, and `llm/tasks.py` records the exact JSON
it was given so any sentence can be checked against its inputs afterwards.

Deliberately mirrors ``ingest/providers/base.py``: same Protocol shape, same
single exception carrying a remedy. A reader who has understood how a price
provider fails already understands how this one does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class LLMUnavailable(RuntimeError):
    """No key, no network, quota exhausted, or narration switched off.

    Never a failure of the endpoint that wanted narration: the deterministic
    answer is already computed by the time this can be raised.
    """


class LLMRefused(RuntimeError):
    """The model answered, and the answer was not usable.

    A safety block, an empty candidate, or output that broke a guardrail.
    Distinguished from :class:`LLMUnavailable` because "it would not say" and
    "it could not be asked" are different things to tell a reader.
    """


@dataclass(frozen=True)
class Message:
    """One turn. ``role`` is 'system' or 'user'; there is no conversation."""

    role: str
    content: str


@dataclass(frozen=True)
class Completion:
    text: str
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason: str | None = None
    http_status: int | None = None
    latency_ms: int = 0


@runtime_checkable
class LLMProvider(Protocol):
    """What a provider has to offer. Two methods, no state worth sharing."""

    name: str
    model: str

    def available(self) -> bool:
        """Whether a call is worth attempting at all."""
        ...

    def complete(
        self,
        messages: list[Message],
        *,
        max_output_tokens: int = 700,
        temperature: float = 0.2,
        timeout_s: float = 30.0,
    ) -> Completion:
        """Answer, or raise LLMUnavailable / LLMRefused. Never returns None."""
        ...
