"""Recorded LLM responses, so evals, CI and the demo can run without an API key.

A recording stores exactly what the model sent to record_quote for one document, plus usage.
Replaying a recording runs it through the same grounding/flag code as a live call.

Staleness: each recording stores a fingerprint of everything that shapes the model's answer
(system prompt, tool schema, effort) and the document hash. If either changed since recording,
replaying would report numbers for a prompt we no longer use, so it is refused.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from procure.extract import DEFAULT_EFFORT, RECORD_QUOTE_TOOL, SYSTEM_PROMPT
from procure.ingest import Document
from procure.models import Usage


def prompt_fingerprint() -> str:
    material = {"system": SYSTEM_PROMPT, "tool": RECORD_QUOTE_TOOL, "effort": DEFAULT_EFFORT}
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]


class Recording(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_name: str
    doc_hash: str
    prompt_fingerprint: str
    model: str
    stop_reason: str
    usage: Usage
    duration_s: float
    tool_input: dict[str, Any] | None  # None = the model did not call record_quote


class StaleRecordingError(Exception):
    pass


def recording_path(directory: Path, doc_name: str) -> Path:
    return directory / f"{doc_name}.json"


def save_recording(directory: Path, rec: Recording) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    text = json.dumps(rec.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
    recording_path(directory, rec.doc_name).write_text(text + "\n", encoding="utf-8")


def load_recording(directory: Path, doc: Document) -> Recording:
    path = recording_path(directory, doc.name)
    if not path.exists():
        raise FileNotFoundError(f"no recording for {doc.name}")
    rec = Recording.model_validate_json(path.read_text("utf-8"))
    if rec.prompt_fingerprint != prompt_fingerprint():
        raise StaleRecordingError(f"{doc.name}: recorded with a different prompt/schema")
    if rec.doc_hash != doc.doc_hash:
        raise StaleRecordingError(f"{doc.name}: document changed since recording")
    return rec
