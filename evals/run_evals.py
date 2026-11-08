"""Run the extraction evals and write a markdown + JSON report.

  make evals            live: calls Claude, overwrites evals/recordings/, needs ANTHROPIC_API_KEY
  make evals-recorded   replays evals/recordings/ (CI); exits 1 if any wrong value was accepted

Usage: python -m evals.run_evals --mode live|recorded [--label baseline]
"""

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from evals.metrics import EvalReport, FieldScore, evaluate
from procure.costs import estimate_cost_usd
from procure.extract import build_request, build_result, call_model, duplicate_result
from procure.ingest import Document, load_inbox
from procure.models import ExtractionResult, GoldenSet, PurchaseRequest, Usage
from procure.normalise import check_quote
from procure.recordings import (
    Recording,
    StaleRecordingError,
    load_recording,
    prompt_fingerprint,
    save_recording,
)

ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "data" / "inbox"
LABELS_PATH = ROOT / "golden" / "labels.json"
PR_PATH = ROOT / "data" / "pr" / "PR-0001.json"
RECORDINGS = ROOT / "evals" / "recordings"
RESULTS = ROOT / "evals" / "results"


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal .env reader (KEY=value lines). Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text("utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#") and value.strip():
            os.environ.setdefault(key.strip(), value.strip())


# --- Running ----------------------------------------------------------------------------------


class RunMeta:
    def __init__(self) -> None:
        self.models: set[str] = set()
        self.usage = Usage()
        self.cost_usd: Decimal | None = Decimal("0")
        self.duration_s = 0.0
        self.llm_calls = 0

    def add(self, model: str, usage: Usage, duration_s: float) -> None:
        self.models.add(model)
        self.usage = Usage(
            input_tokens=self.usage.input_tokens + usage.input_tokens,
            output_tokens=self.usage.output_tokens + usage.output_tokens,
        )
        cost = estimate_cost_usd(model, usage)
        self.cost_usd = None if cost is None or self.cost_usd is None else self.cost_usd + cost
        self.duration_s += duration_s
        self.llm_calls += 1


def run_live(
    docs: list[Document], pr: PurchaseRequest, model: str, recordings: Path
) -> tuple[dict[str, ExtractionResult], RunMeta]:
    import anthropic  # only needed for live runs

    client = anthropic.Anthropic()
    results: dict[str, ExtractionResult] = {}
    meta = RunMeta()
    for doc in docs:
        if doc.duplicate_of is not None:
            results[doc.name] = duplicate_result(doc)
            continue
        start = time.perf_counter()
        raw, usage, stop_reason = call_model(client, build_request(doc, pr, model))
        duration = time.perf_counter() - start
        save_recording(
            recordings,
            Recording(
                doc_name=doc.name,
                doc_hash=doc.doc_hash,
                prompt_fingerprint=prompt_fingerprint(),
                model=model,
                stop_reason=stop_reason,
                usage=usage,
                duration_s=round(duration, 2),
                tool_input=raw,
            ),
        )
        meta.add(model, usage, duration)
        results[doc.name] = build_result(doc, raw, usage)
        print(f"  {doc.name}: {usage.input_tokens} in / {usage.output_tokens} out, {duration:.1f}s")
    return results, meta


def run_recorded(
    docs: list[Document], recordings: Path
) -> tuple[dict[str, ExtractionResult], RunMeta]:
    """Raises FileNotFoundError / StaleRecordingError if a recording is missing or outdated."""
    results: dict[str, ExtractionResult] = {}
    meta = RunMeta()
    for doc in docs:
        if doc.duplicate_of is not None:
            results[doc.name] = duplicate_result(doc)
            continue
        rec = load_recording(recordings, doc)
        meta.add(rec.model, rec.usage, rec.duration_s)
        results[doc.name] = build_result(doc, rec.tool_input, rec.usage)
    return results, meta


def apply_checks(
    results: dict[str, ExtractionResult], labels: GoldenSet
) -> dict[str, ExtractionResult]:
    """Expiry and total checks, with `today` fixed by the labels so results are reproducible."""
    out = {}
    for name, r in results.items():
        if r.quote is not None:
            r = r.model_copy(update={"quote": check_quote(r.quote, labels.today)})
        out[name] = r
    return out


# --- Reporting --------------------------------------------------------------------------------


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{x:.0%}"


def _field_row(name: str, s: FieldScore) -> str:
    cells = [name, f"{s.correct}/{s.total}", _pct(s.accuracy), s.wrong_accepted, s.wrongly_flagged]
    return "| " + " | ".join(str(c) for c in cells) + " |"


def to_markdown(report: EvalReport, meta: RunMeta, label: str, mode: str) -> str:
    cost = "unknown model price" if meta.cost_usd is None else f"${meta.cost_usd:.4f}"
    lines = [
        f"# Extraction eval: {label}",
        "",
        f"- Mode: {mode} · model: {', '.join(sorted(meta.models)) or '–'} · "
        f"prompt fingerprint: `{prompt_fingerprint()}`",
        f"- Documents: {report.docs_total} · LLM calls: {meta.llm_calls} · "
        f"tokens: {meta.usage.input_tokens} in / {meta.usage.output_tokens} out · "
        f"est. cost: {cost} · LLM time: {meta.duration_s:.1f}s",
        "",
        "## Fields",
        "",
        "Correct = right value, or correctly flagged as not stated. "
        "**Wrong accepted** = a wrong value with status ok (the dangerous error).",
        "",
        "| Field | Correct | Accuracy | Wrong accepted | Wrongly flagged |",
        "|---|---|---|---|---|",
        *(_field_row(name, s) for name, s in report.fields.items()),
        _field_row("**All fields**", report.overall),
        "",
        "## Flags",
        "",
        "| Flag | TP | FP | FN | Precision | Recall |",
        "|---|---|---|---|---|---|",
        *(
            f"| {name} | {s.tp} | {s.fp} | {s.fn} | {_pct(s.precision)} | {_pct(s.recall)} |"
            for name, s in report.flags.items()
        ),
        "",
        f"## Documents: {report.docs_correct}/{report.docs_total} with the right outcome",
        "",
        "## Failures",
        "",
    ]
    if not report.failures:
        lines.append("None.")
    else:
        lines += [
            "| Doc | Field | Outcome | Expected | Got | Reason |",
            "|---|---|---|---|---|---|",
        ]
        for f in report.failures:
            lines.append(
                f"| {f.doc} | {f.field} | {f.outcome} | {f.expected} | {f.got} | {f.reason or ''} |"
            )
    return "\n".join(lines) + "\n"


def to_json(report: EvalReport, meta: RunMeta, label: str, mode: str) -> str:
    data = {
        "label": label,
        "mode": mode,
        "models": sorted(meta.models),
        "prompt_fingerprint": prompt_fingerprint(),
        "usage": meta.usage.model_dump(),
        "cost_usd": None if meta.cost_usd is None else str(meta.cost_usd),
        "duration_s": round(meta.duration_s, 2),
        "fields": {n: asdict(s) | {"accuracy": s.accuracy} for n, s in report.fields.items()},
        "overall": asdict(report.overall) | {"accuracy": report.overall.accuracy},
        "flags": {
            n: asdict(s) | {"precision": s.precision, "recall": s.recall}
            for n, s in report.flags.items()
        },
        "docs_correct": report.docs_correct,
        "docs_total": report.docs_total,
        "failures": [asdict(f) for f in report.failures],
    }
    return json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n"


# --- CLI --------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=["live", "recorded"], required=True)
    parser.add_argument("--label", default=None, help="name for the saved report, e.g. baseline")
    parser.add_argument("--recordings", type=Path, default=RECORDINGS)
    parser.add_argument("--results", type=Path, default=RESULTS)
    args = parser.parse_args(argv)

    labels = GoldenSet.model_validate_json(LABELS_PATH.read_text("utf-8"))
    pr = PurchaseRequest.model_validate_json(PR_PATH.read_text("utf-8"))
    docs = load_inbox(INBOX)

    if args.mode == "live":
        load_dotenv()
        if not os.environ.get("ANTHROPIC_API_KEY") or not os.environ.get("ANTHROPIC_MODEL"):
            print("Live evals need ANTHROPIC_API_KEY and ANTHROPIC_MODEL (see .env.example).")
            return 2
        print(f"Running live evals on {len(docs)} documents with {os.environ['ANTHROPIC_MODEL']}")
        results, meta = run_live(docs, pr, os.environ["ANTHROPIC_MODEL"], args.recordings)
    else:
        if not any(args.recordings.glob("*.json")):
            print(
                f"SKIPPED: no recordings in {args.recordings}. "
                "Run `make evals` with an API key first."
            )
            return 0
        try:
            results, meta = run_recorded(docs, args.recordings)
        except (FileNotFoundError, StaleRecordingError) as exc:
            print(f"Recordings unusable: {exc}. Re-record with `make evals`.")
            return 2

    report = evaluate(apply_checks(results, labels), labels)
    label = args.label or args.mode
    markdown = to_markdown(report, meta, label, args.mode)
    print(markdown)

    if args.label:  # only labelled runs are saved, so CI does not litter evals/results/
        stem = f"{datetime.now():%Y-%m-%d}-{args.label}"
        args.results.mkdir(parents=True, exist_ok=True)
        (args.results / f"{stem}.md").write_text(markdown, encoding="utf-8")
        (args.results / f"{stem}.json").write_text(
            to_json(report, meta, label, args.mode), encoding="utf-8"
        )
        print(f"Saved evals/results/{stem}.md and .json")

    wrong = report.overall.wrong_accepted
    if args.mode == "recorded" and wrong > 0:
        print(f"GATE FAILED: {wrong} wrong value(s) accepted.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
