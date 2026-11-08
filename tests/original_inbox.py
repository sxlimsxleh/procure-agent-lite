"""The milestone 1 inbox (q01-q13) as its own Paths, for tests of the approval flow.

Those tests should not depend on which documents were added to the dataset later, nor on the
live recordings (which go stale whenever the prompt changes). So the recordings here are built
from the golden labels: a perfect model, under the current prompt fingerprint.
"""

import shutil
import tempfile
from pathlib import Path

from evals.recording import raw_from_golden
from procure.ingest import load_inbox
from procure.models import GoldenSet, Usage
from procure.pipeline import DEFAULT_PATHS, ROOT, Paths
from procure.recordings import Recording, prompt_fingerprint, save_recording

_DIR = tempfile.TemporaryDirectory(prefix="original-inbox-")  # removed when the process exits
_REC = Path(_DIR.name) / "recordings"
_INBOX = Path(_DIR.name) / "inbox"
_INBOX.mkdir()
for _doc in sorted(DEFAULT_PATHS.inbox.iterdir()):
    if _doc.name < "q14":
        shutil.copy(_doc, _INBOX / _doc.name)

_LABELS = GoldenSet.model_validate_json((ROOT / "golden" / "labels.json").read_text("utf-8"))
for _d in load_inbox(_INBOX):
    if _d.duplicate_of is None:
        save_recording(
            _REC,
            Recording(
                doc_name=_d.name,
                doc_hash=_d.doc_hash,
                prompt_fingerprint=prompt_fingerprint(),
                model="claude-opus-5-5",
                stop_reason="tool_use",
                usage=Usage(input_tokens=1500, output_tokens=400),
                duration_s=1.0,
                tool_input=raw_from_golden(_LABELS.docs[_d.name].quote),
            ),
        )

ORIGINAL_PATHS = Paths(inbox=_INBOX, recordings=_REC)
