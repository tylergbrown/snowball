"""Official House Clerk yearly filing index ({year}FD.zip / {year}FD.xml)."""

from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree as ET

CLERK_ORIGIN = "https://disclosures-clerk.house.gov"
INDEX_URL = CLERK_ORIGIN + "/public_disc/financial-pdfs/{year}FD.zip"
PTR_PDF_URL = CLERK_ORIGIN + "/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"

INDEX_FIELDS = (
    "Prefix",
    "Last",
    "First",
    "Suffix",
    "FilingType",
    "StateDst",
    "Year",
    "FilingDate",
    "DocID",
)


def index_url(year: int) -> str:
    return INDEX_URL.format(year=int(year))


def ptr_pdf_url(year: str | int, doc_id: str) -> str:
    return PTR_PDF_URL.format(year=str(year).strip(), doc_id=str(doc_id).strip())


def is_scanned_doc_id(doc_id: str) -> bool:
    """Paper scans are often DocIDs starting with 8 or 9. Electronic IDs look like 20035143."""
    token = str(doc_id or "").strip()
    return bool(token) and token[0] in {"8", "9"}


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _child_text(elem: ET.Element, name: str) -> str:
    for child in list(elem):
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return ""


def parse_fd_xml(xml_bytes: bytes, *, default_year: int | None = None) -> list[dict]:
    """Parse every Member row. Caller keeps FilingType=P. Handles UTF-8 BOM."""
    raw = xml_bytes
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    start = raw.find(b"<")
    if start > 0:
        raw = raw[start:]
    root = ET.fromstring(raw)
    rows: list[dict] = []
    for elem in root.iter():
        if _local(elem.tag) != "Member":
            continue
        doc_id = _child_text(elem, "DocID") or _child_text(elem, "DocId")
        if not doc_id:
            continue
        filing_type = _child_text(elem, "FilingType")
        year = _child_text(elem, "Year") or (str(default_year) if default_year else "")
        row = {
            "prefix": _child_text(elem, "Prefix"),
            "last": _child_text(elem, "Last"),
            "first": _child_text(elem, "First"),
            "suffix": _child_text(elem, "Suffix"),
            "filing_type": filing_type,
            "state_dst": _child_text(elem, "StateDst"),
            "year": year,
            "filing_date": _child_text(elem, "FilingDate"),
            "doc_id": doc_id,
            "pdf_url": ptr_pdf_url(year, doc_id) if year else "",
        }
        rows.append(row)
    return rows


def ptr_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if (r.get("filing_type") or "").strip().upper() == "P"]


def filings_from_zip(zip_bytes: bytes, year: int) -> list[dict]:
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = zf.namelist()
    want = f"{year}FD.xml"
    xml_name = next((n for n in names if n.lower() == want.lower()), None)
    if xml_name is None:
        xmls = [n for n in names if n.lower().endswith(".xml")]
        if not xmls:
            raise ValueError(f"{year}FD.zip has no XML index")
        xml_name = xmls[0]
    return parse_fd_xml(zf.read(xml_name), default_year=year)
