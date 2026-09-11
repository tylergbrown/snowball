"""Best-effort PTR text parser for pdftotext -layout output.

Electronic filings (DocID like 20035143) put owner + asset fragment + P/S/E +
dates on the first line; ticker and [ST]/[OP] often wrap onto the next line,
and the amount range may split across those lines. If ticker is missing, the
asset string is still stored.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

_DATEPAIR = re.compile(
    r"(?P<tx>S\s*\(\s*partial\s*\)|[PSE])\s+"
    r"(?P<tdate>\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(?P<ndate>\d{1,2}/\d{1,2}/\d{4})"
)
_TICKER_CODE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,6})\)\s*\[([A-Z]{2})\]")
_CODE_ONLY = re.compile(r"\[([A-Z]{2})\]")
_TICKER_ONLY = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,6})\)")
_NAME = re.compile(r"Name:\s*(.+)", re.I)
_DISTRICT = re.compile(r"State/District:\s*([A-Z]{2}\d{0,2})", re.I)
_FILING_ID = re.compile(r"Filing ID #\s*(\d+)", re.I)
_SIGNED = re.compile(
    r"(?:Digitally\s+Signed|Electronically\s+Signed|Signature(?:\s+Date)?)\s*:\s*(.+)",
    re.I,
)
_SIGNED_DATE = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})")
_DESC = re.compile(r"^\s*(?:D\s*:|Description\s*:)\s*(.*)$", re.I)
_FS = re.compile(r"^\s*F\s+S\s*:", re.I)
_DOLLAR = re.compile(r"\$[\d,]+")


def _clean(text: str) -> str:
    text = text.replace("\x00", " ").replace("\f", "\n")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def _norm_tx(raw: str) -> tuple[str, bool]:
    token = re.sub(r"\s+", " ", (raw or "").strip()).upper()
    partial = "PARTIAL" in token
    if token.startswith("P"):
        return "P", partial
    if token.startswith("S"):
        return "S", partial
    if token.startswith("E"):
        return "E", partial
    return token[:8], partial


def _pull_asset(text: str) -> tuple[str, str | None, str | None]:
    ticker: str | None = None
    code: str | None = None
    m = _TICKER_CODE.search(text)
    if m:
        ticker, code = m.group(1), m.group(2)
        text = text[: m.start()] + " " + text[m.end() :]
    else:
        m2 = _CODE_ONLY.search(text)
        if m2:
            code = m2.group(1)
            text = text[: m2.start()] + " " + text[m2.end() :]
        m3 = _TICKER_ONLY.search(text)
        if m3:
            ticker = m3.group(1)
            text = text[: m3.start()] + " " + text[m3.end() :]
    asset = re.sub(r"\s+", " ", text).strip(" -")
    return asset, ticker, code


def _is_stop(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    low = s.lower()
    if low.startswith("digitally signed") or low.startswith("electronically signed"):
        return True
    if low.startswith("i certify") or "i certify that" in low:
        return True
    if "for the complete list of asset type" in low:
        return True
    if s.startswith("Clerk of the House") or "Filing ID #" in s:
        return True
    if s.startswith("Name:") or s.startswith("Status:") or s.startswith("State/District:"):
        return True
    if re.match(r"ID\s+Owner\b", s):
        return True
    return False


def _parse_start(line: str) -> dict[str, Any] | None:
    m = _DATEPAIR.search(line)
    if not m:
        return None
    left = line[: m.start()].strip()
    if not left or left.lower().startswith("id owner"):
        return None
    owner = ""
    body = left
    om = re.match(r"^(SP|JT|DC)\b\s*(.*)$", left)
    if om:
        owner = om.group(1)
        body = om.group(2).strip()
    else:
        # Member's own line, no owner code. Reject tiny leftovers / form labels.
        if len(left) < 3 or re.fullmatch(r"[A-Z]", left):
            return None
    tx, partial = _norm_tx(m.group("tx"))
    return {
        "owner": owner or "self",
        "body": body,
        "tx_type": tx,
        "partial": partial,
        "tx_date": m.group("tdate"),
        "notification_date": m.group("ndate"),
        "amount_tail": line[m.end() :].strip(),
    }


def _amount_range(parts: list[str]) -> str:
    blob = re.sub(r"\s+", " ", " ".join(p.strip() for p in parts if p and p.strip())).strip()
    over = re.search(r"Over\s+\$[\d,]+", blob, re.I)
    if over and "-" not in blob.split("Over")[-1]:
        return re.sub(r"\s+", " ", over.group(0))
    ranged = re.search(r"\$[\d,]+\s*-\s*\$[\d,]+", blob)
    if ranged:
        return re.sub(r"\s+", " ", ranged.group(0))
    nums = _DOLLAR.findall(blob)
    if len(nums) >= 2:
        return f"{nums[0]} - {nums[1]}"
    if nums:
        return nums[0]
    return blob


def _header_fields(text: str) -> dict[str, str | None]:
    member = None
    nm = _NAME.search(text)
    if nm:
        member = re.sub(r"\s+", " ", nm.group(1)).strip() or None
    district = None
    dm = _DISTRICT.search(text)
    if dm:
        district = dm.group(1).upper()
    filing_id = None
    fm = _FILING_ID.search(text)
    if fm:
        filing_id = fm.group(1)
    signature_date = None
    sm = _SIGNED.search(text)
    if sm:
        dm2 = _SIGNED_DATE.search(sm.group(1))
        if dm2:
            signature_date = dm2.group(1)
    return {
        "member": member,
        "district": district,
        "filing_id": filing_id,
        "signature_date": signature_date,
    }


def row_hash(
    doc_id: str,
    owner: str,
    asset_name: str,
    ticker: str | None,
    asset_code: str | None,
    tx_type: str,
    tx_date: str,
    notification_date: str,
    amount_range: str,
) -> str:
    key = "|".join(
        [
            doc_id or "",
            owner or "",
            re.sub(r"\s+", " ", (asset_name or "").strip().lower()),
            (ticker or "").upper(),
            (asset_code or "").upper(),
            tx_type or "",
            tx_date or "",
            notification_date or "",
            re.sub(r"\s+", " ", amount_range or ""),
        ]
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def parse_ptr_text(text: str, *, doc_id: str = "", pdf_url: str = "") -> dict[str, Any]:
    """Return filing header fields plus transaction rows. Empty text => no rows."""
    cleaned = _clean(text or "")
    header = _header_fields(cleaned)
    if not cleaned.strip():
        return {**header, "transactions": [], "parse_status": "scanned_skip"}

    lines = cleaned.split("\n")
    rows: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        start = _parse_start(lines[i])
        if start is None:
            i += 1
            continue
        block_lines = [lines[i]]
        asset_bits = [start["body"]]
        amount_parts = [start["amount_tail"]]
        desc_bits: list[str] = []
        ticker: str | None = None
        code: str | None = None
        body_asset, t0, c0 = _pull_asset(start["body"])
        asset_bits = [body_asset]
        ticker = t0 or ticker
        code = c0 or code
        j = i + 1
        while j < len(lines):
            line = lines[j]
            if _parse_start(line) is not None or _is_stop(line):
                break
            if not line.strip():
                j += 1
                continue
            dm = _DESC.match(line)
            if dm:
                desc_bits.append(dm.group(1).strip())
                j += 1
                continue
            if _FS.match(line):
                j += 1
                continue
            stripped = line.strip()
            amounts = _DOLLAR.findall(stripped)
            rest = stripped
            if amounts:
                amount_parts.extend(amounts)
                rest = _DOLLAR.sub(" ", stripped)
                rest = re.sub(r"\s+", " ", rest).strip()
            if rest:
                piece, t1, c1 = _pull_asset(rest)
                if t1:
                    ticker = ticker or t1
                if c1:
                    code = code or c1
                if piece:
                    asset_bits.append(piece)
            block_lines.append(line)
            j += 1

        asset_name = re.sub(r"\s+", " ", " ".join(b for b in asset_bits if b)).strip()
        amount_range = _amount_range(amount_parts)
        description = re.sub(r"\s+", " ", " ".join(desc_bits)).strip()
        snippet = "\n".join(block_lines).strip()
        if len(snippet) > 800:
            snippet = snippet[:800]
        rows.append(
            {
                "owner": start["owner"],
                "asset_name": asset_name,
                "ticker": ticker,
                "asset_code": code,
                "tx_type": start["tx_type"],
                "partial": bool(start["partial"]),
                "tx_date": start["tx_date"],
                "notification_date": start["notification_date"],
                "amount_range": amount_range,
                "description": description,
                "signature_date": header.get("signature_date"),
                "member": header.get("member"),
                "district": header.get("district"),
                "doc_id": doc_id or header.get("filing_id") or "",
                "pdf_url": pdf_url,
                "snippet": snippet,
            }
        )
        i = j if j > i else i + 1

    status = "parsed" if rows else "parsed_no_rows"
    return {**header, "transactions": rows, "parse_status": status}
