"""Gemini, over its REST API, with no SDK.

``google-generativeai`` would pull grpc, protobuf and google-auth into a
project whose entire runtime dependency list is four packages -- for the sake
of one HTTP POST with a JSON body. ``ingest/fx.py`` already calls the ECB with
``urllib.request`` and an injectable fetcher so tests never touch the network;
this follows that pattern exactly.

The key travels in the ``x-goog-api-key`` header, never in the query string.
Query strings end up in proxy logs, in browser history, and -- the one that
actually bites -- in the text of exceptions, which this project writes to a
table it later shows on a page.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from portfolio_tracker.llm.adapter import Completion, LLMRefused, LLMUnavailable, Message

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MODELS_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"

#: Statuses worth trying again. 429 is quota or rate, 5xx is theirs.
RETRY_STATUSES = (429, 500, 502, 503, 504)

#: Waits between attempts, in seconds. Two retries: a free tier that is out of
#: quota will still be out of quota in twenty seconds, and a page waiting on a
#: third backoff has already failed as far as the reader is concerned.
RETRY_BACKOFF_S = (2.0, 8.0)

#: Google's free-tier limits are per project and change without notice, so
#: nothing here hardcodes one. The caller enforces its own cap; this only
#: reacts to what the API actually says.

#: (status, body) -> the transport, injectable so tests never make a request.
Fetcher = Callable[[str, bytes, dict[str, str], float], tuple[int, bytes]]


def _post(url: str, body: bytes, headers: dict[str, str], timeout_s: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LLMUnavailable(f"could not reach the model: {exc}") from exc


class GeminiProvider:
    """The only class in this repository that knows Gemini exists."""

    name = "gemini"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gemini-3.6-flash",
        fetcher: Fetcher = _post,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._key = api_key
        self.model = model
        self._fetch = fetcher
        self._sleep = sleep

    def available(self) -> bool:
        return bool(self._key)

    # -- the call ----------------------------------------------------------

    def complete(
        self,
        messages: list[Message],
        *,
        max_output_tokens: int = 700,
        temperature: float = 0.2,
        timeout_s: float = 30.0,
    ) -> Completion:
        if not self._key:
            raise LLMUnavailable("no API key is set")

        system = "\n\n".join(m.content for m in messages if m.role == "system")
        user = "\n\n".join(m.content for m in messages if m.role != "system")

        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_output_tokens,
                "responseMimeType": "text/plain",
            },
        }
        if system:
            # A proper system instruction rather than smuggling the rules into
            # the user turn, where they read as something to be weighed against
            # the data instead of as constraints on it.
            payload["systemInstruction"] = {"parts": [{"text": system}]}

        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self._key,
            "Accept": "application/json",
        }
        url = ENDPOINT.format(model=self.model)

        started = time.monotonic()
        status, raw = self._attempt(url, body, headers, timeout_s)
        latency_ms = int((time.monotonic() - started) * 1000)
        return self._read(status, raw, latency_ms)

    def _attempt(
        self, url: str, body: bytes, headers: dict[str, str], timeout_s: float
    ) -> tuple[int, bytes]:
        status, raw = self._fetch(url, body, headers, timeout_s)
        for wait in RETRY_BACKOFF_S:
            if status not in RETRY_STATUSES:
                break
            # Jittered, so two clients that failed together do not retry
            # together. Nothing here is concurrent today; the habit is cheap.
            self._sleep(wait + random.uniform(0, 0.5))
            status, raw = self._fetch(url, body, headers, timeout_s)
        return status, raw

    def _read(self, status: int, raw: bytes, latency_ms: int) -> Completion:
        if status != 200:
            raise self._error_for(status, raw)

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise LLMRefused(f"the model's reply was not JSON: {exc}") from exc

        candidates = parsed.get("candidates") or []
        if not candidates:
            # A prompt-level block: nothing was generated at all.
            reason = (parsed.get("promptFeedback") or {}).get("blockReason")
            raise LLMRefused(f"the model declined to answer ({reason or 'no reason given'})")

        first = candidates[0]
        finish = first.get("finishReason")
        parts = (first.get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts).strip()

        if finish == "SAFETY" or not text:
            raise LLMRefused(f"the model declined to answer ({finish or 'empty reply'})")

        usage = parsed.get("usageMetadata") or {}
        return Completion(
            text=text,
            provider=self.name,
            model=self.model,
            input_tokens=usage.get("promptTokenCount"),
            output_tokens=usage.get("candidatesTokenCount"),
            finish_reason=finish,
            http_status=status,
            latency_ms=latency_ms,
        )

    def _error_for(self, status: int, raw: bytes) -> Exception:
        detail = _message_from(raw)
        if status in (400, 401, 403):
            # A rejected key or a wrong model name. Retrying forever would burn
            # nothing but would also never start working, so the caller is told
            # to stop and fix something.
            hint = (
                " The model name may be wrong -- check it against the model list."
                if status == 400 and "model" in detail.lower()
                else " Check the API key."
            )
            return LLMUnavailable(f"the model refused the request ({status}): {detail}.{hint}")
        if status == 404:
            return LLMUnavailable(
                f"no such model: {self.model!r}. Pick one from the model list in Settings."
            )
        if status == 429:
            return LLMUnavailable(
                f"the free tier's limit has been reached: {detail}. It resets on Google's schedule."
            )
        return LLMUnavailable(f"the model service returned {status}: {detail}")

    # -- discovery ---------------------------------------------------------

    def models(self, *, timeout_s: float = 15.0) -> list[str]:
        """Model names this key may actually use.

        Worth an endpoint of its own: model identifiers change, and a settings
        page that lets somebody type one is a settings page that lets them
        type one that does not exist. Asking is better than guessing.
        """
        if not self._key:
            raise LLMUnavailable("no API key is set")
        request = urllib.request.Request(
            MODELS_ENDPOINT, headers={"x-goog-api-key": self._key}, method="GET"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise LLMUnavailable(
                f"could not list models ({exc.code}): {_message_from(exc.read())}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise LLMUnavailable(f"could not list models: {exc}") from exc

        return sorted(
            str(m.get("name", "")).removeprefix("models/")
            for m in parsed.get("models", [])
            if "generateContent" in (m.get("supportedGenerationMethods") or [])
        )


def _message_from(raw: bytes) -> str:
    """The API's own explanation, or the raw body if it did not give one."""
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace")[:200]
    error = parsed.get("error")
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])[:300]
    return str(parsed)[:200]
