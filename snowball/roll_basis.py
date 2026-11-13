"""Attach carried contract-roll cost basis to Position objects (no heavy imports)."""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger("snowball.futures.roll")


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def attach_roll_basis(conn: sqlite3.Connection, positions: list) -> list:
    """Set ``Position.roll_basis`` from the lane DB (no-op without the table)."""
    if not positions:
        return positions
    try:
        if not _has_table(conn, "roll_basis"):
            return positions
        ids = [int(p.id) for p in positions]
        q = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT position_id, basis_price FROM roll_basis WHERE position_id IN ({q})", ids
        ).fetchall()
        m = {int(r[0]): float(r[1]) for r in rows}
        for p in positions:
            if int(p.id) in m:
                p.roll_basis = m[int(p.id)]
    except sqlite3.Error:
        log.exception("roll basis lookup failed")
    return positions


