from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

log = logging.getLogger("snowball.watcher")


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _text(el: ET.Element | None) -> str:
    if el is None or el.text is None:
        return ""
    return el.text.strip()


def _child(el: ET.Element, name: str) -> ET.Element | None:
    for child in list(el):
        if _local(child.tag) == name:
            return child
    return None


def _link(el: ET.Element) -> str:
    link_el = _child(el, "link")
    if link_el is None:
        guid = _child(el, "guid")
        return _text(guid)
    href = (link_el.get("href") or "").strip()
    if href:
        return href
    return _text(link_el)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        iso = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        log.debug("The Watcher: unparseable date %r", raw)
        return None


@dataclass(frozen=True)
class RssItem:
    title: str
    url: str
    published_at: datetime | None
    summary: str
    raw: dict[str, str]


def parse_rss(xml_text: str) -> list[RssItem]:
    """Parse RSS 2.0 or Atom. Returns [] on empty/invalid XML (caller logs)."""
    text = (xml_text or "").lstrip()
    if not text or text.startswith("<!DOCTYPE html") or text.startswith("<html"):
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        log.warning("The Watcher: RSS XML parse failed")
        return []

    root_name = _local(root.tag).lower()
    items: list[RssItem] = []
    if root_name == "rss" or root_name == "rdf":
        for el in root.iter():
            if _local(el.tag).lower() != "item":
                continue
            item = _from_rss_item(el)
            if item:
                items.append(item)
    elif root_name == "feed":
        for el in list(root):
            if _local(el.tag).lower() != "entry":
                continue
            item = _from_atom_entry(el)
            if item:
                items.append(item)
    else:
        # Some feeds wrap channel without rss root quirks
        for el in root.iter():
            if _local(el.tag).lower() == "item":
                item = _from_rss_item(el)
                if item:
                    items.append(item)
    return items


def _from_rss_item(el: ET.Element) -> RssItem | None:
    title = _text(_child(el, "title"))
    url = _link(el)
    if not title and not url:
        return None
    published = parse_datetime(
        _text(_child(el, "pubDate")) or _text(_child(el, "date")) or _text(_child(el, "updated"))
    )
    summary = _text(_child(el, "description")) or _text(_child(el, "summary"))
    return RssItem(
        title=title or url,
        url=url,
        published_at=published,
        summary=summary,
        raw={"title": title, "url": url, "summary": summary},
    )


def _from_atom_entry(el: ET.Element) -> RssItem | None:
    title = _text(_child(el, "title"))
    url = _link(el)
    if not title and not url:
        return None
    published = parse_datetime(
        _text(_child(el, "published")) or _text(_child(el, "updated"))
    )
    summary = _text(_child(el, "summary")) or _text(_child(el, "content"))
    return RssItem(
        title=title or url,
        url=url,
        published_at=published,
        summary=summary,
        raw={"title": title, "url": url, "summary": summary},
    )
