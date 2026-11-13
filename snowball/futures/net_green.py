"""Future Trader net-green close gate (Scout idea, Tb approved 2026-10-01 6:25 AM CT).

Every FT close path (session_day close, momentum TP / stall / EOD, merged stock stall
exits, research, legacy strategy / fade, HALT flatten and daily-loss-kill flatten) closes a
lot ONLY when its net P&L after ROUND-TRIP fees is above FUTURES_NET_GREEN_BUFFER_USD
(default $0.50). Otherwise the lot is held — a stricter never-sell-red. Contract rolls
(reason ``contract_roll``) keep their existing exception and are not gated here.

Fees are the REAL Coinbase CFM fees seen on our 25 recorded live fills: a flat $0.12 per
contract per side, 0% of notional (FUTURES_FEE_PER_CONTRACT / FUTURES_FEE_PCT_PER_SIDE).
Entry fee = the fee actually recorded on the lot's opening fill when > 0, else estimated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ROLL_REASON_BASE = "contract_roll"


def is_roll_reason(reason: str) -> bool:
    return str(reason or "").split(":", 1)[0] == ROLL_REASON_BASE


def estimate_side_fee_usd(
    price: float, contracts: float, *, contract_size: float, fee_pct_per_side: float, fee_per_contract: float
) -> float:
    notional = abs(float(price)) * float(contract_size) * float(contracts)
    return notional * max(0.0, float(fee_pct_per_side)) + float(contracts) * max(0.0, float(fee_per_contract))


@dataclass
class NetGreen:
    ok: bool
    reason: str
    exit_px: float | None
    basis: float
    gross_usd: float | None
    entry_fee_usd: float
    exit_fee_usd: float
    net_usd: float | None
    buffer_usd: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "why": self.reason, "exit_px": self.exit_px, "basis": self.basis,
            "gross_usd": None if self.gross_usd is None else round(self.gross_usd, 4),
            "entry_fee_usd": round(self.entry_fee_usd, 4), "exit_fee_usd": round(self.exit_fee_usd, 4),
            "net_usd": None if self.net_usd is None else round(self.net_usd, 4),
            "buffer_usd": self.buffer_usd,
        }


def net_green_check(
    *,
    side: str,
    basis: float,
    qty: float,
    exit_px: float | None,
    contracts: float,
    contract_size: float,
    fee_pct_per_side: float,
    fee_per_contract: float,
    buffer_usd: float,
    entry_fee_usd: float | None = None,
) -> NetGreen:
    """Net P&L of closing ``qty`` (underlying units) at ``exit_px`` vs ``basis`` after round-trip fees."""
    buf = float(buffer_usd)
    kw = dict(contract_size=contract_size, fee_pct_per_side=fee_pct_per_side, fee_per_contract=fee_per_contract)
    ent_fee = (
        float(entry_fee_usd)
        if entry_fee_usd is not None and float(entry_fee_usd) > 0
        else estimate_side_fee_usd(basis, contracts, **kw)
    )
    if exit_px is None or float(exit_px) <= 0 or basis <= 0:
        return NetGreen(False, "no_price", None, float(basis), None, ent_fee, 0.0, None, buf)
    ex_fee = estimate_side_fee_usd(float(exit_px), contracts, **kw)
    if str(side).lower() == "short":
        gross = (float(basis) - float(exit_px)) * float(qty)
    else:
        gross = (float(exit_px) - float(basis)) * float(qty)
    net = gross - ent_fee - ex_fee
    ok = net > buf
    return NetGreen(ok, "net_green" if ok else "net_below_buffer", float(exit_px), float(basis),
                    gross, ent_fee, ex_fee, net, buf)


def conservative_exit_px(side: str, bid: float | None, ask: float | None, fallback: float | None) -> float | None:
    """Worst touch for the close: long sells into the bid, short buys at the ask."""
    px = ask if str(side).lower() == "short" else bid
    if px is None or float(px) <= 0:
        px = fallback
    return None if px is None or float(px) <= 0 else float(px)
