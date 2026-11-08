"""Minimal document-to-text helper used ONLY to sanity-check the golden labels in milestone 1.

Milestone 2 replaces this with src/procure/ingest.py; the dataset test will then use ingest.
"""

import io
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pdfplumber
from openpyxl import load_workbook


def collapse_ws(text: str) -> str:
    return " ".join(text.split())


def pdf_text(data: bytes) -> str:
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def doc_text(path: Path) -> str:
    data = path.read_bytes()
    if path.suffix == ".pdf":
        return pdf_text(data)
    if path.suffix == ".txt":
        return data.decode("utf-8")
    if path.suffix == ".xlsx":
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        rows = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                rows.append(" | ".join(str(c) for c in row if c is not None))
        return "\n".join(rows)
    if path.suffix == ".eml":
        msg = BytesParser(policy=policy.default).parsebytes(data)
        parts = []
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                parts.append(part.get_content())
            elif part.get_content_type() == "application/pdf":
                parts.append(pdf_text(part.get_content()))
        return "\n".join(parts)
    raise ValueError(f"unsupported file type: {path}")


def attachments(path: Path) -> list[bytes]:
    msg = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    return [p.get_content() for p in msg.iter_attachments()]
