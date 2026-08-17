"""Market-data providers: yfinance, TEFAS, and the synthetic cash proxy.

Each is a thin adapter over an external source, and each names the manual path
in its failure message. TEFAS in particular is an undocumented endpoint with no
stability guarantee -- see :class:`TefasProvider`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from portfolio_tracker.ingest.providers.base import (
    InstrumentRef,
    PriceObservation,
    ProviderUnavailable,
)


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

    def fetch(self, instrument: InstrumentRef, start: date, end: date) -> list[PriceObservation]:
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
                auto_adjust=False,
                progress=False,
                threads=False,
            )
        except Exception as exc:
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
    """Turkish investment funds.

    TEFAS publishes an undocumented JSON endpoint with no versioning, no
    stability guarantee and no announcement when it changes. As of 2026-08-15 it
    returns HTTP 404 for every known path while the site itself serves 200,
    which is exactly the failure this design anticipated.

    It is kept because the endpoint may well return, and because a working
    request is a one-line change. But the manual provider is the supported path
    for these funds, not the contingency -- so this raises with instructions
    rather than pretending a fund has no prices.
    """

    name = "tefas"
    url = "https://www.tefas.gov.tr/api/DB/BindHistoryInfo"

    def __init__(self, fetcher: Callable[[str, bytes], str] | None = None) -> None:
        self._fetch = fetcher or self._post

    def supports(self, instrument: InstrumentRef) -> bool:
        return instrument.price_source == "tefas"

    @staticmethod
    def _post(url: str, payload: bytes) -> str:
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://www.tefas.gov.tr/TarihselVeriler.aspx",
                "User-Agent": "Mozilla/5.0",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            body: bytes = response.read()
            return body.decode("utf-8")

    @staticmethod
    def parse(text: str, import_key: str) -> list[PriceObservation]:
        """Read the ``{"data": [{"TARIH": ..., "FIYAT": ...}]}`` shape.

        ``TARIH`` is milliseconds since the epoch. A fault payload is detected
        explicitly, because it arrives with HTTP 200 in some deployments.
        """
        try:
            payload: dict[str, Any] = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderUnavailable(
                f"{import_key}: TEFAS returned non-JSON; the endpoint has changed. "
                f"Use data/prices_manual/{import_key}.csv instead."
            ) from exc

        if "fault" in payload or "data" not in payload:
            raise ProviderUnavailable(
                f"{import_key}: TEFAS returned no data ({payload.get('fault', payload)}). "
                f"Use data/prices_manual/{import_key}.csv instead."
            )

        observations: list[PriceObservation] = []
        for row in payload["data"]:
            raw_price, raw_date = row.get("FIYAT"), row.get("TARIH")
            if raw_price in (None, "") or raw_date in (None, ""):
                continue
            try:
                price = Decimal(str(raw_price))
                on = date.fromtimestamp(int(raw_date) / 1000)
            except (InvalidOperation, ValueError, OSError) as exc:
                raise ProviderUnavailable(f"{import_key}: unparseable TEFAS row {row!r}") from exc
            # NAV only: no distribution-adjusted series is published, so
            # total_return_close is left None rather than being faked from NAV.
            observations.append(PriceObservation(on, price))
        return observations

    def fetch(self, instrument: InstrumentRef, start: date, end: date) -> list[PriceObservation]:
        code = instrument.price_symbol or instrument.import_key
        payload = urllib.parse.urlencode(
            {
                "fontip": "YAT",
                "fonkod": code,
                "bastarih": start.strftime("%d.%m.%Y"),
                "bittarih": end.strftime("%d.%m.%Y"),
            }
        ).encode()
        try:
            text = self._fetch(self.url, payload)
        except urllib.error.HTTPError as exc:
            raise ProviderUnavailable(
                f"{instrument.import_key}: TEFAS returned HTTP {exc.code}. This endpoint is "
                f"undocumented and is currently unavailable. Supply prices in "
                f"data/prices_manual/{instrument.import_key}.csv instead."
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderUnavailable(
                f"{instrument.import_key}: could not reach TEFAS ({exc.reason}). "
                f"Use data/prices_manual/{instrument.import_key}.csv instead."
            ) from exc
        return self.parse(text, instrument.import_key)


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

    def fetch(self, instrument: InstrumentRef, start: date, end: date) -> list[PriceObservation]:
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

    def fetch(self, instrument: InstrumentRef, start: date, end: date) -> list[PriceObservation]:
        from portfolio_tracker.core.calendars import grid_days

        one = Decimal(1)
        return [
            PriceObservation(day, one, one)
            for day in grid_days(start, end, instrument.trading_calendar)
        ]
