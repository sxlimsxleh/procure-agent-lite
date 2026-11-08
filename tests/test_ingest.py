from pathlib import Path

from procure.ingest import collapse_ws, load_document, load_inbox, normalise_text

INBOX = Path(__file__).resolve().parent.parent / "data" / "inbox"


def test_loads_all_supported_files() -> None:
    docs = load_inbox(INBOX)
    assert len(docs) == 13
    assert all(d.text for d in docs)


def test_forwarded_duplicate_is_detected_by_attachment_hash() -> None:
    docs = {d.name: d for d in load_inbox(INBOX)}
    first, second = docs["q08_fwd.eml"], docs["q09_fwd_again.eml"]
    assert first.doc_hash == second.doc_hash
    assert first.duplicate_of is None
    assert second.duplicate_of == "q08_fwd.eml"
    assert first.text != second.text  # different subject/sender/body
    assert [d.name for d in docs.values() if d.duplicate_of] == ["q09_fwd_again.eml"]


def test_quote_id_derives_from_hash() -> None:
    doc = load_document(INBOX / "q05_baltic_bearings.txt")
    assert doc.quote_id == f"Q-{doc.doc_hash[:8]}"


def test_pdf_text_includes_page_two() -> None:
    text = load_document(INBOX / "q01_mueller_waelzlager.pdf").text
    assert "Listenpreis: 4,85 €" in text
    assert "ab 500 Stk.: 4,46 €" in text  # the volume discount on page 2


def test_eml_includes_body_and_attachment() -> None:
    text = load_document(INBOX / "q02_alpentech.eml").text
    assert "anbei unser Angebot" in text
    assert "[Attachment: AlpenTech_Angebot_4471.pdf]" in text
    assert "Gesamtbetrag: 2.795,00 € (brutto)" in text


def test_body_only_eml() -> None:
    text = load_document(INBOX / "q10_bohemia_body_only.eml").text
    assert "Unit price: EUR 4.55 (net, excl. VAT)" in text
    assert "[Attachment" not in text


def test_xlsx_rows_are_pipe_joined() -> None:
    text = load_document(INBOX / "q11_huber_pricelist.xlsx").text
    assert "6205-2RS | Rillenkugellager 6205-2RS | 10 | 4.39 | 100" in text.splitlines()


def test_normalise_text() -> None:
    assert normalise_text("  a  b \t c \n\n\n d  ") == "a b c\nd"


def test_collapse_ws_joins_lines() -> None:
    assert collapse_ws("Gesamt\n  netto:\t2.310,00 €") == "Gesamt netto: 2.310,00 €"
