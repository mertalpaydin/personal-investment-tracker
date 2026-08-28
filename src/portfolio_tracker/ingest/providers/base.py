"""The price-provider contract.

Every provider is interchangeable, and one of them -- ``manual`` -- is a
first-class path rather than a fallback. The others depend on third-party
endpoints that can and do disappear; a provider that breaks must degrade to
"put the file here yourself", never to a crash and never to a silently empty
result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol, runtime_checkable


class ProviderUnavailable(RuntimeError):
    """The source could not be reached, or has changed shape.

    Carries a remedy rather than just a cause: every raise site names the
    manual path, because that is what the user can actually act on.
    """


class RateLimited(ProviderUnavailable):
    """The source is refusing us for asking too often.

    Its own subclass because it needs the opposite response to every other
    failure. A bad ticker should be recorded and the run continued; being
    rate-limited means the next request is *more* likely to fail than the
    last, and continuing turns a pause into a block. So the caller waits,
    retries a few times, and then stops the whole run -- keeping everything
    already stored, and resuming from there next time.
    """


@dataclass(frozen=True)
class PriceObservation:
    on: date
    close: Decimal
    #: Dividend-adjusted close. Used for performance, never for tax or NAV.
    #: ``None`` where the source publishes only a NAV, which must not be
    #: silently backfilled from ``close`` by the provider -- the distinction
    #: matters and belongs to the caller.
    total_return_close: Decimal | None = None
    volume: Decimal | None = None


@dataclass(frozen=True)
class InstrumentRef:
    """What a provider needs to know. Deliberately not the ORM row."""

    instrument_id: int
    import_key: str
    currency: str
    price_source: str
    price_symbol: str | None
    trading_calendar: str
    #: OWNED / WATCH / UNIVERSE. Only what is owned is valued daily; the rest
    #: exists to be compared against, and can be sampled less often.
    status: str = "OWNED"


@runtime_checkable
class PriceProvider(Protocol):
    name: str

    def supports(self, instrument: InstrumentRef) -> bool: ...

    def fetch(
        self, instrument: InstrumentRef, start: date, end: date, *, weekly: bool = False
    ) -> list[PriceObservation]: ...

    """Prices for the window.

    ``weekly`` asks for one observation a week instead of one a day. Used for
    instruments that are only ever compared against, never valued: a daily
    series for two hundred of them is a great deal of somebody else's
    bandwidth to answer a question about three-year returns.
    """
