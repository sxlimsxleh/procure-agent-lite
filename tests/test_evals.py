"""Eval machinery tests. No API key: recordings are built from the golden labels."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from evals.metrics import evaluate, score_field
from evals.recording import raw_from_golden
from evals.run_evals import main
from procure.costs import estimate_cost_usd
from procure.extract import build_result
from procure.ingest import load_inbox
from procure.models import ExtractedField, GoldenSet, Usage
from procure.recordings import (
    Recording,
    StaleRecordingError,
    load_recording,
    prompt_fingerprint,
    save_recording,
)

ROOT = Path(__file__).resolve().parent.parent
LABELS = GoldenSet.model_validate_json((ROOT / "golden/labels.json").read_text("utf-8"))
DOCS = {d.name: d for d in load_inbox(ROOT / "data/inbox")}


def write_golden_recordings(directory: Path, edits: dict[str, dict] | None = None) -> None:
    """Recordings as if the model had answered exactly like the labels (plus optional edits)."""
    for name, doc in DOCS.items():
        if doc.duplicate_of is not None:
            continue
        raw = raw_from_golden(LABELS.docs[name].quote)
        raw.update((edits or {}).get(name, {}))
        save_recording(
            directory,
            Recording(
                doc_name=name,
                doc_hash=doc.doc_hash,
                prompt_fingerprint=prompt_fingerprint(),
                model="claude-opus-5-5",
                stop_reason="tool_use",
                usage=Usage(input_tokens=1000, output_tokens=200),
                duration_s=1.5,
                tool_input=raw,
            ),
        )


def run(tmp_path: Path, *extra: str) -> int:
    return main(["--mode", "recorded", "--recordings", str(tmp_path / "rec"),
                 "--results", str(tmp_path / "results"), *extra])  # fmt: skip


def test_golden_recordings_score_perfectly(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    write_golden_recordings(tmp_path / "rec")
    assert run(tmp_path, "--label", "golden") == 0
    report = json.loads((next((tmp_path / "results").glob("*-golden.json"))).read_text())
    assert report["overall"]["accuracy"] == 1.0
    assert report["overall"]["wrong_accepted"] == 0
    assert report["docs_correct"] == report["docs_total"] == 13
    assert report["failures"] == []
    for name, flag in report["flags"].items():
        assert flag["fp"] == 0 and flag["fn"] == 0, name
    assert "| **All fields** |" in capsys.readouterr().out


def test_gate_fails_on_wrong_value_accepted(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    # The model reads the page-1 list price instead of the page-2 discount, with a real span.
    edit = {"unit_price": {"value": "4.85", "source_text": "Listenpreis: 4,85 € / Stück"}}
    write_golden_recordings(tmp_path / "rec", {"q01_mueller_waelzlager.pdf": edit})
    assert run(tmp_path) == 1
    out = capsys.readouterr().out
    assert "GATE FAILED: 1 wrong value(s) accepted" in out
    assert "| q01_mueller_waelzlager.pdf | unit_price | wrong_accepted | 4.46 | 4.85 |" in out


def test_wrongly_flagged_does_not_fail_gate(tmp_path: Path) -> None:
    edit = {"delivery_date": {"value": None, "source_text": None}}
    write_golden_recordings(tmp_path / "rec", {"q03_helvetia_lager.pdf": edit})
    assert run(tmp_path) == 0


def test_newsletter_turned_into_quote_counts_as_wrong_accepted(tmp_path: Path) -> None:
    newsletter = {
        "is_quote": True, "supplier_name": "Lagerwelt", "currency": "EUR", "vat_included": True,
        "unit_price": {"value": "3.99", "source_text": "ab 3,99 € pro Stück"},
    }  # fmt: skip
    write_golden_recordings(tmp_path / "rec", {"q13_newsletter.eml": newsletter})
    assert run(tmp_path) == 1


def test_no_recordings_is_skipped_not_failed(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    assert run(tmp_path) == 0
    assert "SKIPPED" in capsys.readouterr().out


def test_missing_recording_is_an_error(tmp_path: Path) -> None:
    write_golden_recordings(tmp_path / "rec")
    (tmp_path / "rec" / "q05_baltic_bearings.txt.json").unlink()
    assert run(tmp_path) == 2


def test_stale_recording_is_rejected(tmp_path: Path) -> None:
    write_golden_recordings(tmp_path / "rec")
    path = tmp_path / "rec" / "q05_baltic_bearings.txt.json"
    data = json.loads(path.read_text())
    data["prompt_fingerprint"] = "0" * 16  # recorded with an older prompt
    path.write_text(json.dumps(data))
    with pytest.raises(StaleRecordingError):
        load_recording(tmp_path / "rec", DOCS["q05_baltic_bearings.txt"])
    assert run(tmp_path) == 2


def test_score_field_outcomes() -> None:
    golden = LABELS.docs["q05_baltic_bearings.txt"]
    raw = raw_from_golden(golden.quote)
    quote = build_result(DOCS["q05_baltic_bearings.txt"], raw).quote
    assert score_field("delivery_date", quote, golden)[0] == "correct"  # correctly flagged
    assert score_field("unit_price", quote, golden)[0] == "correct"
    assert quote is not None
    wrong = quote.model_copy(
        update={"unit_price": ExtractedField[Decimal].ok(Decimal("4.05"), "x")}
    )
    assert score_field("unit_price", wrong, golden)[0] == "wrong_accepted"
    assert score_field("unit_price", None, golden)[0] == "wrongly_flagged"


def test_missing_result_counts_as_wrong_outcome() -> None:
    report = evaluate({}, LABELS)
    assert report.docs_correct == 0
    assert report.overall.wrong_accepted == 0  # nothing accepted, so nothing dangerous


def test_cost_estimate() -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=100_000)
    assert estimate_cost_usd("claude-opus-5-5", usage) == Decimal("6")
    assert estimate_cost_usd("some-unknown-model", usage) is None
