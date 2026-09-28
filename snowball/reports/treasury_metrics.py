"""Bitcoin treasury metrics for the daily trade PDF."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from snowball.treasury.ledger import (
    TreasurySnapshot,
    default_treasury_db,
    snapshot,
)


def treasury_snapshot_for_pdf(
    data_dir: Path,
    *,
    now: datetime | None = None,
    spot_now: float | None = None,
    write_mark: bool = True,
) -> TreasurySnapshot:
    """Load/create ledger under *data_dir* and return PDF snapshot."""
    db = default_treasury_db(data_dir)
    return snapshot(db, now=now, spot_now=spot_now, write_mark=write_mark)


__all__ = [
    "TreasurySnapshot",
    "default_treasury_db",
    "treasury_snapshot_for_pdf",
]
