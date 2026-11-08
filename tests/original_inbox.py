"""The milestone 1 inbox (q01-q13) as its own Paths, for tests of the approval flow.

Those tests replay recorded extractions; they should not depend on which documents were added
to the dataset later (or whether those already have recordings).
"""

import shutil
import tempfile
from pathlib import Path

from procure.pipeline import DEFAULT_PATHS, Paths

_DIR = tempfile.TemporaryDirectory(prefix="original-inbox-")  # removed when the process exits
for _doc in sorted(DEFAULT_PATHS.inbox.iterdir()):
    if _doc.name < "q14":
        shutil.copy(_doc, Path(_DIR.name) / _doc.name)

ORIGINAL_PATHS = Paths(inbox=Path(_DIR.name))
