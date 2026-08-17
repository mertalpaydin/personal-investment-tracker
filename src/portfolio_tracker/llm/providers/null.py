"""The provider used when narration is off, or no key has been set.

A real object rather than `None`, so callers never branch on nullability and
the "no key" path exercises exactly the same code as the "network failed"
path. There is one way narration can be absent, not two.
"""

from __future__ import annotations

from portfolio_tracker.llm.adapter import Completion, LLMUnavailable, Message


class NullProvider:
    name = "none"
    model = "none"

    def __init__(self, reason: str = "narration is switched off") -> None:
        self._reason = reason

    def available(self) -> bool:
        return False

    def complete(
        self,
        messages: list[Message],
        *,
        max_output_tokens: int = 700,
        temperature: float = 0.2,
        timeout_s: float = 30.0,
    ) -> Completion:
        raise LLMUnavailable(self._reason)
