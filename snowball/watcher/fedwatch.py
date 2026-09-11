"""CME FedWatch research for The Watcher (never places orders).

Probabilities come from the sanctioned ``cme-fedwatch`` package (same path Fed
Desk uses). The official CME FedWatch Tool URL is recorded for citation in
PDF/research — we do not scrape brittle HTML when the package is authoritative.
"""

from __future__ import annotations

import logging
from typing import Any

from snowball.fed.probs import summarize_fedwatch_payload
from snowball.models import utcnow
from snowball.watcher.feeds import CME_FEDWATCH_TOOL_URL
from snowball.watcher.store import ResearchEvent, WatcherStore, event_fingerprint

log = logging.getLogger("snowball.watcher")

SOURCE = "cme_fedwatch"


def fetch_fedwatch_probabilities() -> dict[str, Any]:
    """Authoritative probabilities via cme-fedwatch package."""
    from cme_fedwatch import get_probabilities

    return get_probabilities()


def research_event_from_summary(
    summarized: dict[str, Any], *, fetched_at=None
) -> ResearchEvent:
    """Build a watcher ResearchEvent with source attribution for PDF/research."""
    now = fetched_at or utcnow()
    next_date = summarized.get("next_meeting_date") or "unknown"
    p_hold = summarized.get("p_hold")
    p_hike = summarized.get("p_hike")
    p_cut = summarized.get("p_cut")
    title = (
        f"CME FedWatch next meeting {next_date}: "
        f"hold={_pct(p_hold)} hike={_pct(p_hike)} cut={_pct(p_cut)}"
    )
    raw = dict(summarized)
    raw["source_attribution"] = {
        "name": "CME FedWatch Tool",
        "url": CME_FEDWATCH_TOOL_URL,
        "provider": "cme-fedwatch",
        "note": "Probabilities via sanctioned package; HTML page not scraped for orders",
    }
    # Fingerprint by meeting + rounded probs so unchanged polls dedupe; updates insert.
    fp_key = (
        f"{next_date}|hold={_pct(p_hold)}|hike={_pct(p_hike)}|cut={_pct(p_cut)}"
    )
    return ResearchEvent(
        source=SOURCE,
        kind="fedwatch_probs",
        title=title,
        url=CME_FEDWATCH_TOOL_URL,
        published_at=now,
        country="united states",
        importance=3,
        tags=["fedwatch", "fomc", "rates", "cme"],
        raw_json=raw,
        fetched_at=now,
        fingerprint=event_fingerprint(SOURCE, CME_FEDWATCH_TOOL_URL, fp_key, None),
    )


def _pct(v: object) -> str:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "n/a"
    # summarize_fedwatch may store 0-1 or 0-100; display sensibly
    if x <= 1.0:
        x *= 100.0
    return f"{x:.1f}%"


def poll_fedwatch_into_store(store: WatcherStore) -> int:
    """Fetch FedWatch probs and upsert a research event. Returns 1 if new, else 0.

    Research-only: never places orders.
    """
    try:
        payload = fetch_fedwatch_probabilities()
        summarized = summarize_fedwatch_payload(payload)
        ev = research_event_from_summary(summarized)
        inserted = store.upsert(ev)
        log.info(
            "The Watcher FedWatch logged",
            extra={
                "data": {
                    "source": SOURCE,
                    "url": CME_FEDWATCH_TOOL_URL,
                    "next_meeting": summarized.get("next_meeting_date"),
                    "p_hold": summarized.get("p_hold"),
                    "p_hike": summarized.get("p_hike"),
                    "p_cut": summarized.get("p_cut"),
                    "inserted": bool(inserted),
                }
            },
        )
        return 1 if inserted else 0
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "The Watcher FedWatch poll failed",
            extra={"data": {"error": str(exc), "url": CME_FEDWATCH_TOOL_URL}},
        )
        return 0
