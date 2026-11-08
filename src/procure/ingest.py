"""Load inbox documents as normalised text and detect duplicates by content hash.

The hash is taken over the *quote content*, not the file: an email that forwards a PDF is hashed
by the PDF's bytes, so the same quote forwarded twice (different subject, sender, body) is caught.
"""

import hashlib
import io
import re
import unicodedata
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path

import pdfplumber
from openpyxl import load_workbook

from procure.models import StrictModel

SUPPORTED_SUFFIXES = {".pdf", ".txt", ".eml", ".xlsx"}


class Document(StrictModel):
    name: str  # filename in the inbox
    doc_hash: str  # sha256 hex of the quote content
    text: str  # normalised text: what the LLM reads and what grounding checks against
    duplicate_of: str | None = None  # name of the first document with the same hash

    @property
    def quote_id(self) -> str:
        return f"Q-{self.doc_hash[:8]}"


def normalise_text(text: str) -> str:
    """NFKC (e.g. non-breaking space -> space), collapse runs of spaces, drop blank lines."""
    text = unicodedata.normalize("NFKC", text)
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def collapse_ws(text: str) -> str:
    """All whitespace (including line breaks) to single spaces. Used for span comparisons, so a
    source_text that crosses a line break in the PDF still matches."""
    return " ".join(text.split())


def pdf_text(data: bytes) -> str:
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def xlsx_text(data: bytes) -> str:
    """One line per row, non-empty cells joined by ' | '."""
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    rows = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            rows.append(" | ".join(str(cell) for cell in row if cell is not None))
    return "\n".join(rows)


def eml_text_and_payload(data: bytes) -> tuple[str, bytes]:
    msg = BytesParser(policy=policy.default).parsebytes(data)
    assert isinstance(msg, EmailMessage)
    body_part = msg.get_body(preferencelist=("plain",))
    body = body_part.get_content() if body_part is not None else ""
    texts = [f"Subject: {msg['Subject']}\nFrom: {msg['From']}\n\n{body}"]
    pdfs: list[bytes] = []
    for part in msg.iter_attachments():
        if part.get_content_type() == "application/pdf":
            pdf = part.get_content()
            pdfs.append(pdf)
            texts.append(f"[Attachment: {part.get_filename()}]\n{pdf_text(pdf)}")
    text = "\n\n".join(texts)
    # Attachments carry the quote -> hash them. Body-only emails: hash the body, not the headers.
    payload = b"".join(pdfs) if pdfs else normalise_text(body).encode("utf-8")
    return text, payload


def read_document(path: Path) -> tuple[str, bytes]:
    """Return (raw text, payload bytes to hash)."""
    data = path.read_bytes()
    match path.suffix:
        case ".pdf":
            return pdf_text(data), data
        case ".txt":
            return data.decode("utf-8"), data
        case ".xlsx":
            return xlsx_text(data), data
        case ".eml":
            return eml_text_and_payload(data)
    raise ValueError(f"unsupported file type: {path.name}")


def load_document(path: Path) -> Document:
    text, payload = read_document(path)
    return Document(
        name=path.name, doc_hash=hashlib.sha256(payload).hexdigest(), text=normalise_text(text)
    )


def load_inbox(inbox: Path) -> list[Document]:
    """Load all supported files, sorted by name. Later copies of a hash get duplicate_of set."""
    first_by_hash: dict[str, str] = {}
    docs = []
    for path in sorted(inbox.iterdir()):
        if path.suffix not in SUPPORTED_SUFFIXES:
            continue
        doc = load_document(path)
        if doc.doc_hash in first_by_hash:
            doc = doc.model_copy(update={"duplicate_of": first_by_hash[doc.doc_hash]})
        else:
            first_by_hash[doc.doc_hash] = doc.name
        docs.append(doc)
    return docs
