"""Persistence for resolved onvista instrument/notation lookups.

``OnvistaProvider`` itself stays database-free, like every other provider --
it is handed a small cache object instead of a connection. Search and
snapshot are the two calls that changed (an ISIN or symbol has to be matched
to onvista's ``entityType``/``entityValue``, and a notation chosen among
whatever markets the instrument quotes on), and both are expensive relative
to the EOD history call that follows. Resolving them once and caching the
result is what makes a daily fetch cheap after the first run.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class OnvistaNotation:
    entity_type: str
    entity_id: str
    id_notation: str
    market: str
    currency: str


class NotationCache:
    """Reads and writes ``onvista_notation``, keyed by instrument id."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, instrument_id: int) -> OnvistaNotation | None:
        row = self._conn.execute(
            "SELECT entity_type, entity_id, id_notation, market, currency "
            "FROM onvista_notation WHERE instrument_id = ?",
            (instrument_id,),
        ).fetchone()
        if row is None:
            return None
        return OnvistaNotation(
            entity_type=row["entity_type"],
            entity_id=row["entity_id"],
            id_notation=row["id_notation"],
            market=row["market"],
            currency=row["currency"],
        )

    def put(self, instrument_id: int, notation: OnvistaNotation) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._conn.execute(
            """
            INSERT INTO onvista_notation
                (instrument_id, entity_type, entity_id, id_notation, market, currency, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (instrument_id) DO UPDATE SET
                entity_type = excluded.entity_type,
                entity_id   = excluded.entity_id,
                id_notation = excluded.id_notation,
                market      = excluded.market,
                currency    = excluded.currency,
                resolved_at = excluded.resolved_at
            """,
            (
                instrument_id,
                notation.entity_type,
                notation.entity_id,
                notation.id_notation,
                notation.market,
                notation.currency,
                now,
            ),
        )
        self._conn.commit()
