"""Market-data providers: yfinance, TEFAS, and the synthetic cash proxy.

Each is a thin adapter over an external source, and each names the manual path
in its failure message. TEFAS in particular is an undocumented endpoint with no
stability guarantee -- see :class:`TefasProvider`.
"""

from __future__ import annotations

import http.client
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from portfolio_tracker.ingest.onvista_cache import OnvistaNotation
from portfolio_tracker.ingest.providers.base import (
    InstrumentRef,
    PriceObservation,
    ProviderUnavailable,
    RateLimited,
)

logger = logging.getLogger(__name__)


class _NotationCache(Protocol):
    """What `OnvistaProvider` needs from a notation cache.

    A structural type rather than importing a concrete class: the provider
    stays database-free, and any object with these two methods -- backed by
    a table, a dict in a test, or nothing at all -- works.
    """

    def get(self, instrument_id: int) -> OnvistaNotation | None: ...

    def put(self, instrument_id: int, notation: OnvistaNotation) -> None: ...


@dataclass(frozen=True)
class InstrumentDescription:
    """What a provider can say about a symbol that is not yet in the database.

    Every field except ``symbol`` is optional, because every one of them comes
    from a source that may simply not answer. The UI shows what came back and
    lets the person correct it before anything is stored -- which is the right
    shape anyway: they know what they typed and the feed is guessing.
    """

    symbol: str
    name: str | None = None
    currency: str | None = None
    exchange: str | None = None
    quote_type: str | None = None
    last_close: Decimal | None = None
    last_date: date | None = None
    observations: int = 0


class YFinanceProvider:
    """Exchange-listed instruments.

    ``auto_adjust=False`` is mandatory. With the default the library overwrites
    ``Close`` with the adjusted series, and the raw close is then unrecoverable
    -- but tax and NAV need the raw price while performance needs the adjusted
    one, so collapsing them silently corrupts one or the other.
    """

    name = "yfinance"

    def supports(self, instrument: InstrumentRef) -> bool:
        return instrument.price_source == "yfinance"

    def describe(self, symbol: str, *, probe_days: int = 14) -> InstrumentDescription:
        """What a symbol appears to be, before anything is written.

        Deliberately a *validation* step and not an autocomplete. yfinance has
        no supported symbol search: the endpoint behind one is undocumented,
        unversioned and aggressively rate-limited -- the same failure mode
        `TefasProvider` already records. Promising a search box would mean
        promising something that breaks without notice. Typing a ticker and
        pressing Check is honest about what can actually be relied on.

        The currency it returns is load-bearing rather than decorative: a
        database trigger aborts any price whose currency differs from its
        instrument's, so guessing it wrong makes every later fetch fail with
        an error nobody could act on.
        """
        ticker = symbol.strip()
        if not ticker:
            raise ProviderUnavailable("Enter a ticker, for example IWDA.DE.")
        try:
            import yfinance
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise ProviderUnavailable(
                "yfinance is not installed; run `uv sync --extra ingest`"
            ) from exc

        handle = yfinance.Ticker(ticker)

        # `fast_info` is lazy: converting it to a dict, or reading a key an
        # unknown symbol has no answer for, raises from inside the library.
        # Each field is therefore fetched on its own and a failure means
        # "not available" rather than taking the whole lookup down.
        def field(name: str) -> Any:
            try:
                return getattr(handle.fast_info, name, None)
            except Exception:
                return None

        currency = field("currency")
        last = field("last_price")
        exchange = field("exchange")

        # The name needs the heavy `.info` call, which scrapes and is the first
        # thing to break -- so it is attempted, never depended on. A blank name
        # is a field the person fills in, not a failed lookup.
        name: str | None = None
        quote_type: str | None = None
        try:
            info: dict[str, Any] = handle.info or {}
            name = info.get("shortName") or info.get("longName") or None
            quote_type = info.get("quoteType") or None
            currency = currency or info.get("currency")
            exchange = exchange or info.get("exchange")
        except Exception:
            pass

        end = date.today()
        start = end - timedelta(days=probe_days)
        observations: list[PriceObservation] = []
        try:
            observations = self.fetch(
                InstrumentRef(
                    instrument_id=0,
                    import_key=ticker,
                    currency=str(currency or "EUR"),
                    price_source="yfinance",
                    price_symbol=ticker,
                    trading_calendar="XETRA",
                ),
                start,
                end,
            )
        except ProviderUnavailable:
            observations = []

        if not observations and last is None:
            raise ProviderUnavailable(
                f"{ticker!r} returned no prices. Check the ticker -- Xetra listings "
                "usually end in '.DE', and Yahoo is the naming this uses."
            )

        return InstrumentDescription(
            symbol=ticker,
            name=str(name) if name else None,
            currency=str(currency) if currency else None,
            exchange=str(exchange) if exchange else None,
            quote_type=str(quote_type) if quote_type else None,
            last_close=observations[-1].close if observations else None,
            last_date=observations[-1].on if observations else None,
            observations=len(observations),
        )

    def fetch(
        self, instrument: InstrumentRef, start: date, end: date, *, weekly: bool = False
    ) -> list[PriceObservation]:
        symbol = instrument.price_symbol
        if not symbol:
            raise ProviderUnavailable(
                f"{instrument.import_key}: no price_symbol set. Add one in seeds/instruments.yaml "
                f"(for Xetra this is usually '{instrument.import_key}.DE')."
            )
        try:
            import yfinance
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise ProviderUnavailable(
                "yfinance is not installed; run `uv sync --extra ingest`, "
                "or supply prices manually in data/prices_manual/"
            ) from exc

        try:
            frame = yfinance.download(
                symbol,
                start=start.isoformat(),
                end=end.isoformat(),
                interval="1wk" if weekly else "1d",
                auto_adjust=False,
                progress=False,
                threads=False,
            )
        except Exception as exc:
            # Being throttled needs the opposite response to a bad ticker, so
            # it is raised as its own type. yfinance surfaces this several
            # ways depending on version -- a named exception, an HTTP 429, or
            # prose -- and none of them is stable enough to match on alone.
            if _looks_rate_limited(exc):
                raise RateLimited(
                    f"{instrument.import_key}: Yahoo is rate-limiting this client "
                    f"({exc}). Already-fetched prices are kept; run the update again "
                    "in a few minutes and it will carry on from here."
                ) from exc
            raise ProviderUnavailable(
                f"{instrument.import_key}: yfinance request failed ({exc}). "
                f"Fall back to data/prices_manual/{instrument.import_key}.csv"
            ) from exc

        if frame is None or frame.empty:
            raise ProviderUnavailable(
                f"{instrument.import_key}: yfinance returned nothing for {symbol!r}. "
                "Check the symbol, or supply the file manually."
            )

        # A single-symbol download still comes back column-multi-indexed.
        if hasattr(frame.columns, "nlevels") and frame.columns.nlevels > 1:
            frame = frame.droplevel(1, axis=1)

        if "Close" not in frame.columns:
            raise ProviderUnavailable(
                f"{instrument.import_key}: yfinance response has no Close column "
                f"(got {list(frame.columns)}); the library's shape may have changed"
            )

        observations: list[PriceObservation] = []
        for stamp, row in frame.iterrows():
            close = row.get("Close")
            if close is None or close != close:  # NaN: market shut, or not yet settled
                continue
            adjusted = row.get("Adj Close")
            observations.append(
                PriceObservation(
                    on=stamp.date(),
                    close=Decimal(str(round(float(close), 6))),
                    total_return_close=(
                        Decimal(str(round(float(adjusted), 6)))
                        if adjusted is not None and adjusted == adjusted
                        else None
                    ),
                )
            )
        return observations


class TefasProvider:
    """Turkish investment funds, from TEFAS itself.

    TEFAS is the official register: every fund's daily price is reported there
    by its founder through Takasbank. The site was rebuilt in 2026 and the old
    ``/api/DB/BindHistoryInfo`` form endpoint was removed. The new site's own
    fund page posts ``{"fonKodu", "dil", "periyod"}`` as JSON to
    ``/api/funds/fonFiyatBilgiGetir`` and receives dated daily prices; that is
    the request made here, found by recording what the page itself sends.

    The HTML pages sit behind an F5 bot challenge; this JSON endpoint did not,
    as of 2026-09-27. It is still undocumented, so a changed response raises
    with the manual path as the remedy rather than returning nothing.

    ``periyod`` is a look-back in months from the latest price, not a date
    range: 1, 12 and 60 were verified. The smallest one that reaches ``start``
    is requested and the result is trimmed to ``[start, end]``.
    """

    name = "tefas"
    url = "https://www.tefas.gov.tr/api/funds/fonFiyatBilgiGetir"
    _PERIODS: tuple[tuple[int, int], ...] = ((1, 28), (12, 360), (60, 5 * 365))

    def __init__(
        self,
        fetcher: Callable[[str, bytes], str] | None = None,
        *,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._fetch = fetcher or self._post
        self._today = today

    def supports(self, instrument: InstrumentRef) -> bool:
        return instrument.price_source == "tefas"

    @staticmethod
    def _post(url: str, payload: bytes) -> str:
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Origin": "https://www.tefas.gov.tr",
                "Referer": "https://www.tefas.gov.tr/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
                ),
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            body: bytes = response.read()
            return body.decode("utf-8")

    @classmethod
    def _period_for(cls, start: date, today: date) -> int:
        span = (today - start).days
        for months, days in cls._PERIODS:
            if span <= days:
                return months
        return cls._PERIODS[-1][0]

    @staticmethod
    def parse(text: str, import_key: str) -> list[PriceObservation]:
        """Read ``{"errorMessage": null, "resultList": [{"tarih", "fiyat"}]}``.

        ``tarih`` is an ISO date. An ``errorMessage`` arrives with HTTP 200
        ("Sistem Hatasi!!", "system error", for a request the service does not understand), so
        it is checked explicitly.
        """
        try:
            payload: dict[str, Any] = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderUnavailable(
                f"{import_key}: TEFAS returned non-JSON; the endpoint has changed. "
                f"Use data/prices_manual/{import_key}.csv instead."
            ) from exc
        if payload.get("errorMessage") or not isinstance(payload.get("resultList"), list):
            raise ProviderUnavailable(
                f"{import_key}: TEFAS returned no prices "
                f"({payload.get('errorMessage') or 'no resultList'}). "
                f"Use data/prices_manual/{import_key}.csv instead."
            )
        observations: list[PriceObservation] = []
        for row in payload["resultList"]:
            raw_price, raw_date = row.get("fiyat"), row.get("tarih")
            if raw_price in (None, "") or raw_date in (None, ""):
                continue
            try:
                price = Decimal(str(raw_price))
                on = date.fromisoformat(str(raw_date)[:10])
            except (InvalidOperation, ValueError) as exc:
                raise ProviderUnavailable(f"{import_key}: unparseable TEFAS row {row!r}") from exc
            # NAV only: no distribution-adjusted series is published, so
            # total_return_close is left None rather than being faked from NAV.
            observations.append(PriceObservation(on, price))
        return observations

    def fetch(
        self, instrument: InstrumentRef, start: date, end: date, *, weekly: bool = False
    ) -> list[PriceObservation]:
        code = instrument.price_symbol or instrument.import_key
        payload = json.dumps(
            {"fonKodu": code, "dil": "TR", "periyod": self._period_for(start, self._today())}
        ).encode()
        try:
            text = self._fetch(self.url, payload)
        except urllib.error.HTTPError as exc:
            raise ProviderUnavailable(
                f"{instrument.import_key}: TEFAS returned HTTP {exc.code}. "
                f"Use data/prices_manual/{instrument.import_key}.csv instead."
            ) from exc
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            reason = getattr(exc, "reason", str(exc))
            raise ProviderUnavailable(
                f"{instrument.import_key}: could not reach TEFAS ({reason}). "
                f"Use data/prices_manual/{instrument.import_key}.csv instead."
            ) from exc
        observations = [o for o in self.parse(text, instrument.import_key) if start <= o.on <= end]
        return _weekly_sample(observations) if weekly else observations


class FintablesProvider:
    """Turkish investment funds, via a public republisher of TEFAS NAVs.

    TEFAS's own endpoint sits behind a bot-protection challenge, which this
    project does not attempt to defeat. Fintables serves the same figures over
    a plain unauthenticated JSON endpoint; cross-checked against the broker
    export, both funds agreed to within 0.02%.

    **It returns only the latest observation.** The payload carries a
    ``prev_price`` too, but not the date that price belongs to -- and inferring
    it would mean guessing around Turkish public holidays, which move against
    the Gregorian calendar. A price stored against the wrong date is worse than
    a missing one, so only the dated figure is kept.

    The practical consequence: this keeps a series current once it exists, but
    it cannot backfill. History has to be loaded once from a manual file.
    """

    name = "fintables"
    url_template = "https://api.fintables.com/funds/{code}/price/"

    def __init__(self, fetcher: Callable[[str], str] | None = None) -> None:
        self._fetch = fetcher or self._get

    def supports(self, instrument: InstrumentRef) -> bool:
        return instrument.price_source == "fintables"

    @staticmethod
    def _get(url: str) -> str:
        request = urllib.request.Request(
            url, headers={"User-Agent": "portfolio-tracker/0.1", "Accept": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            body: bytes = response.read()
            return body.decode("utf-8")

    @staticmethod
    def parse(text: str, import_key: str) -> list[PriceObservation]:
        try:
            payload: dict[str, Any] = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderUnavailable(
                f"{import_key}: fintables returned non-JSON; the endpoint may have changed. "
                f"Use data/prices_manual/{import_key}.csv instead."
            ) from exc

        day, price = payload.get("day"), payload.get("price")
        if not day or price in (None, ""):
            raise ProviderUnavailable(
                f"{import_key}: fintables returned no dated price ({payload}). "
                f"Use data/prices_manual/{import_key}.csv instead."
            )
        try:
            value = Decimal(str(price))
        except InvalidOperation as exc:
            raise ProviderUnavailable(
                f"{import_key}: unparseable fintables price {price!r}"
            ) from exc
        if value <= 0:
            raise ProviderUnavailable(f"{import_key}: non-positive fintables price {price!r}")

        # NAV only, as with TEFAS: no distribution-adjusted series is published,
        # so total_return_close stays None rather than being faked from NAV.
        return [PriceObservation(date.fromisoformat(day), value)]

    def fetch(
        self, instrument: InstrumentRef, start: date, end: date, *, weekly: bool = False
    ) -> list[PriceObservation]:
        code = instrument.price_symbol or instrument.import_key
        try:
            text = self._fetch(self.url_template.format(code=code))
        except urllib.error.HTTPError as exc:
            raise ProviderUnavailable(
                f"{instrument.import_key}: fintables returned HTTP {exc.code} for {code!r}. "
                f"Use data/prices_manual/{instrument.import_key}.csv instead."
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderUnavailable(
                f"{instrument.import_key}: could not reach fintables ({exc.reason}). "
                f"Use data/prices_manual/{instrument.import_key}.csv instead."
            ) from exc
        return [o for o in self.parse(text, instrument.import_key) if start <= o.on <= end]


#: Substrings that mean "you are asking too often" rather than "no such
#: ticker". Matched case-insensitively against the exception and its type.
_RATE_LIMIT_MARKERS = (
    "ratelimit",
    "rate limit",
    "rate-limit",
    "too many requests",
    "429",
)


def _looks_rate_limited(exc: BaseException) -> bool:
    """Whether a yfinance failure is throttling rather than a bad symbol.

    Deliberately a substring match, and deliberately admitted as such. There
    is no stable exception type across yfinance versions, and the cost of the
    two mistakes is very different: treating throttling as a normal error
    hammers a source that is already refusing us, while treating an odd error
    as throttling only costs a wait.
    """
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


class OnvistaProvider:
    """German-listed ETFs and stocks, via onvista's public JSON API.

    Built because Yahoo Finance is unreliable for Xetra ETFs: it dropped 110
    Xetra symbols in September 2026, publishes ETF daily bars a day late, and
    -- worst -- its history for roughly half of the .DE ETFs in this database
    turned out to be USD figures stored against EUR instruments (a constant
    ~14.4% gap; some series 100x off; some a different fund entirely).
    onvista publishes correct Xetra EUR closes with same-day availability.

    Three calls make up one resolution: a search matches the instrument to
    onvista's ``entityType``/``entityValue``, a snapshot lists the markets it
    quotes on (Xetra preferred, then Tradegate, gettex, Stuttgart) and their
    currency, and the EOD history call then serves the actual series. The
    first two are cached (by whatever ``notation_cache`` is handed in, keyed
    on ``instrument.instrument_id``) so a repeat fetch skips straight to
    history -- the provider itself stays database-free, like the others.
    """

    name = "onvista"

    _SEARCH_URL = "https://api.onvista.de/api/v1/instruments/query"
    _SNAPSHOT_URL = "https://api.onvista.de/api/v1/{kind}s/{entity_id}/snapshot"
    _HISTORY_URL = "https://api.onvista.de/api/v1/instruments/{entity_type}/{entity_id}/eod_history"

    #: Notation preference, in order: Xetra, then Tradegate, then gettex.
    #: Stuttgart has no stable ``codeExchange`` seen so far, so it is matched
    #: by market name instead, as the last resort before giving up.
    _EXCHANGE_PREFERENCE: tuple[str, ...] = ("GER", "BSX", "TRO")

    def __init__(
        self,
        fetcher: Callable[[str], str] | None = None,
        notation_cache: _NotationCache | None = None,
    ) -> None:
        self._fetch = fetcher or self._get
        self._cache = notation_cache

    def supports(self, instrument: InstrumentRef) -> bool:
        return instrument.price_source == "onvista"

    @staticmethod
    def _get(url: str) -> str:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            body: bytes = response.read()
            return body.decode("utf-8")

    # -- search ------------------------------------------------------------

    @staticmethod
    def parse_search(text: str, query: str, isin: str | None) -> tuple[str, str]:
        """Return ``(entity_type, entity_id)`` for the best match.

        Matched on ISIN when the instrument has one, else the first FUND or
        STOCK hit -- which is how owned ETFs with no ISIN on file (EGV2,
        FLXD, IS0E, LYBK, NQSE, SEC0) are resolved, by their bare symbol.
        """
        try:
            payload: dict[str, Any] = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderUnavailable(
                f"{query}: onvista search returned non-JSON; the endpoint may have changed"
            ) from exc
        candidates = payload.get("list") or []
        match: dict[str, Any] | None = None
        if isin:
            match = next((c for c in candidates if c.get("isin") == isin), None)
        if match is None:
            match = next((c for c in candidates if c.get("entityType") in ("FUND", "STOCK")), None)
        if match is None:
            raise ProviderUnavailable(
                f"{query}: onvista search found no FUND/STOCK match "
                f"(isin={isin!r}, {len(candidates)} candidate(s))"
            )
        entity_type = str(match["entityType"]).lower()
        entity_id = str(match["entityValue"])
        return entity_type, entity_id

    def _search(self, instrument: InstrumentRef) -> tuple[str, str]:
        query = instrument.isin or (instrument.price_symbol or instrument.import_key).removesuffix(
            ".DE"
        )
        url = f"{self._SEARCH_URL}?{urllib.parse.urlencode({'searchValue': query, 'limit': 5})}"
        try:
            text = self._fetch(url)
        except urllib.error.HTTPError as exc:
            raise ProviderUnavailable(
                f"{instrument.import_key}: onvista search returned HTTP {exc.code}. "
                f"Use data/prices_manual/{instrument.import_key}.csv instead."
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderUnavailable(
                f"{instrument.import_key}: could not reach onvista ({exc.reason}). "
                f"Use data/prices_manual/{instrument.import_key}.csv instead."
            ) from exc
        return self.parse_search(text, query, instrument.isin)

    # -- snapshot ------------------------------------------------------------

    @staticmethod
    def parse_snapshot(text: str, import_key: str, want_currency: str) -> OnvistaNotation:
        """Choose a notation from ``quoteList.list`` and check its currency.

        The currency check is load-bearing, not decorative: a stored price in
        the wrong currency silently misvalues the whole position, so a
        mismatch is refused here rather than passed downstream.
        """
        try:
            payload: dict[str, Any] = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderUnavailable(
                f"{import_key}: onvista snapshot returned non-JSON; the endpoint may have changed"
            ) from exc
        quotes = ((payload.get("quoteList") or {}).get("list")) or []
        if not quotes:
            raise ProviderUnavailable(f"{import_key}: onvista snapshot has no quotes")

        chosen: dict[str, Any] | None = None
        for code in OnvistaProvider._EXCHANGE_PREFERENCE:
            chosen = next(
                (q for q in quotes if (q.get("market") or {}).get("codeExchange") == code), None
            )
            if chosen is not None:
                break
        if chosen is None:
            chosen = next(
                (
                    q
                    for q in quotes
                    if "stuttgart" in str((q.get("market") or {}).get("name") or "").lower()
                ),
                None,
            )
        if chosen is None:
            raise ProviderUnavailable(
                f"{import_key}: onvista has no Xetra/Tradegate/gettex/Stuttgart notation "
                f"among {[(q.get('market') or {}).get('codeExchange') for q in quotes]}"
            )

        market = chosen.get("market") or {}
        id_notation = market.get("idNotation")
        if id_notation is None:
            raise ProviderUnavailable(f"{import_key}: onvista notation has no idNotation")
        currency = (
            chosen.get("isoCurrency") or chosen.get("currency") or chosen.get("currencySymbol")
        )
        if currency != want_currency:
            raise ProviderUnavailable(
                f"{import_key}: onvista notation currency {currency!r} does not match "
                f"instrument currency {want_currency!r}"
            )
        inst = payload.get("instrument") or {}
        resolved_symbol = str(inst["symbol"]) if inst.get("symbol") else None
        market_label = str(market.get("name") or market.get("codeExchange") or "unknown")
        return OnvistaNotation(
            entity_type="",  # filled in by the caller, which already knows it
            entity_id="",
            id_notation=str(id_notation),
            market=market_label,
            currency=str(currency),
            resolved_symbol=resolved_symbol,
        )

    def _resolve(self, instrument: InstrumentRef) -> OnvistaNotation:
        notation: OnvistaNotation | None = None
        if self._cache is not None:
            notation = self._cache.get(instrument.instrument_id)

        if notation is None:
            entity_type, entity_id = self._search(instrument)
            url = self._SNAPSHOT_URL.format(kind=entity_type, entity_id=entity_id)
            try:
                text = self._fetch(url)
            except urllib.error.HTTPError as exc:
                raise ProviderUnavailable(
                    f"{instrument.import_key}: onvista snapshot returned HTTP {exc.code}. "
                    f"Use data/prices_manual/{instrument.import_key}.csv instead."
                ) from exc
            except urllib.error.URLError as exc:
                raise ProviderUnavailable(
                    f"{instrument.import_key}: could not reach onvista ({exc.reason}). "
                    f"Use data/prices_manual/{instrument.import_key}.csv instead."
                ) from exc
            partial = self.parse_snapshot(text, instrument.import_key, instrument.currency)
            notation = OnvistaNotation(
                entity_type=entity_type,
                entity_id=entity_id,
                id_notation=partial.id_notation,
                market=partial.market,
                currency=partial.currency,
                resolved_symbol=partial.resolved_symbol,
            )
            if self._cache is not None:
                self._cache.put(instrument.instrument_id, notation)

        if notation.resolved_symbol:
            expected = (
                (instrument.price_symbol or instrument.import_key)
                .removesuffix(".DE")
                .strip()
                .upper()
            )
            resolved = notation.resolved_symbol.strip().upper()
            if expected and resolved != expected:
                logger.warning(
                    "Onvista ticker mismatch for %s: resolved symbol %r "
                    "does not match expected ticker %r",
                    instrument.import_key,
                    notation.resolved_symbol,
                    expected,
                )
        return notation

    # -- history ------------------------------------------------------------

    #: What each ``range`` value spans, verified live. The endpoint's window is
    #: ``[startDate, startDate + range]``; ``range`` alone returns only the
    #: most recent month whatever value is passed. That is why history is
    #: paged from ``start`` forward rather than requested in one call.
    _RANGE_DAYS: tuple[tuple[str, int], ...] = (("M1", 31), ("Y1", 365), ("Y5", 5 * 365))

    @classmethod
    def _range_for(cls, start: date, end: date) -> str:
        """Smallest onvista ``range`` value that covers ``[start, end]``."""
        span_days = (end - start).days
        for name, days in cls._RANGE_DAYS:
            if span_days <= days:
                return name
        return cls._RANGE_DAYS[-1][0]

    @classmethod
    def _range_days(cls, name: str) -> int:
        return dict(cls._RANGE_DAYS)[name]

    @staticmethod
    def parse_history(text: str, import_key: str) -> list[PriceObservation]:
        try:
            payload: dict[str, Any] = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderUnavailable(
                f"{import_key}: onvista eod_history returned non-JSON; the endpoint may "
                "have changed"
            ) from exc
        times = payload.get("datetimeLast") or []
        closes = payload.get("last") or []
        volumes = payload.get("volume") or []
        if not times or not closes:
            raise ProviderUnavailable(f"{import_key}: onvista eod_history returned no rows")

        observations: list[PriceObservation] = []
        for i, ts in enumerate(times):
            if i >= len(closes) or closes[i] is None:
                continue
            try:
                on = datetime.fromtimestamp(int(ts), tz=UTC).date()
                close = Decimal(str(closes[i]))
            except (InvalidOperation, ValueError, OSError, OverflowError) as exc:
                raise ProviderUnavailable(
                    f"{import_key}: unparseable onvista history row at index {i}"
                ) from exc
            volume = (
                Decimal(str(volumes[i])) if i < len(volumes) and volumes[i] is not None else None
            )
            # onvista publishes no adjusted close, so total_return_close stays
            # None rather than being faked from the raw close -- as with TEFAS
            # and fintables.
            observations.append(PriceObservation(on, close, None, volume))
        return observations

    def fetch(
        self, instrument: InstrumentRef, start: date, end: date, *, weekly: bool = False
    ) -> list[PriceObservation]:
        notation = self._resolve(instrument)
        # Verified live: this endpoint wants the entity type upper-cased
        # ("FUND"/"STOCK"), unlike the snapshot endpoint's lower-cased path
        # segment ("funds"/"stocks") -- passing it through as stored fails
        # every request with HTTP 400 "Entity type must not be null".
        base = self._HISTORY_URL.format(
            entity_type=notation.entity_type.upper(), entity_id=notation.entity_id
        )
        # The window is [startDate, startDate + range], so a five-year
        # history that should end today would otherwise stop a few days
        # short. Page forward from `start` until `end` is inside a window.
        observations: list[PriceObservation] = []
        cursor = start
        while cursor <= end:
            range_param = self._range_for(cursor, end)
            query = urllib.parse.urlencode(
                {
                    "idNotation": notation.id_notation,
                    "range": range_param,
                    "startDate": cursor.isoformat(),
                }
            )
            try:
                text = self._fetch(f"{base}?{query}")
            except urllib.error.HTTPError as exc:
                raise ProviderUnavailable(
                    f"{instrument.import_key}: onvista eod_history returned HTTP {exc.code}. "
                    f"Use data/prices_manual/{instrument.import_key}.csv instead."
                ) from exc
            except urllib.error.URLError as exc:
                raise ProviderUnavailable(
                    f"{instrument.import_key}: could not reach onvista ({exc.reason}). "
                    f"Use data/prices_manual/{instrument.import_key}.csv instead."
                ) from exc
            observations.extend(self.parse_history(text, instrument.import_key))
            cursor = cursor + timedelta(days=self._range_days(range_param) + 1)
        seen: set[date] = set()
        unique = []
        for o in sorted(observations, key=lambda o: o.on):
            if start <= o.on <= end and o.on not in seen:
                seen.add(o.on)
                unique.append(o)
        return _weekly_sample(unique) if weekly else unique


def _weekly_sample(observations: list[PriceObservation]) -> list[PriceObservation]:
    """One observation per ISO week: the last trading day of each week.

    Used for instruments only ever compared against, never valued -- a daily
    series for two hundred of them is a great deal of somebody else's
    bandwidth to answer a question about three-year returns.
    """
    by_week: dict[tuple[int, int], PriceObservation] = {}
    for obs in sorted(observations, key=lambda o: o.on):
        by_week[obs.on.isocalendar()[:2]] = obs
    return [by_week[key] for key in sorted(by_week)]


class SyntheticProvider:
    """Cash proxies.

    A deposit balance is carried as a position priced at exactly 1 unit of its
    own currency, so it flows through the same daily revaluation as everything
    else. The FX leg then does the real work, which is the whole point: the
    euro value of a lira deposit moves every day even though its "price" never
    does.
    """

    name = "synthetic"

    def supports(self, instrument: InstrumentRef) -> bool:
        return instrument.price_source == "synthetic"

    def fetch(
        self, instrument: InstrumentRef, start: date, end: date, *, weekly: bool = False
    ) -> list[PriceObservation]:
        from portfolio_tracker.core.calendars import grid_days

        one = Decimal(1)
        return [
            PriceObservation(day, one, one)
            for day in grid_days(start, end, instrument.trading_calendar)
        ]
