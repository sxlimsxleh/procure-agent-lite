"""Structured JSON logs (design rule 6): one line per event, always with a run_id.

Named logging.py per the project layout. Inside this file `import logging` still means the
standard library, because Python 3 imports are absolute.

    with log_step("extract", run_id) as extra:
        ...
        extra["input_tokens"] = 1234
    -> {"ts": "...", "event": "step", "step": "extract", "run_id": "...", "duration_ms": 812,
        "status": "ok", "input_tokens": 1234}
"""

import json
import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

LOGGER_NAME = "procure"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
            **getattr(record, "fields", {}),
        }
        return json.dumps(data, ensure_ascii=False, default=str)


class _CurrentStderrHandler(logging.StreamHandler):
    """Looks up sys.stderr at every write, so a redirected stderr (tests, tools) is respected."""

    @property  # type: ignore[override]
    def stream(self):  # noqa: ANN201
        return sys.stderr

    @stream.setter
    def stream(self, value: object) -> None:
        pass


def get_logger() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:  # configure once
        handler = _CurrentStderrHandler()  # stderr: stdout stays free for human-readable output
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def log_event(event: str, **fields: Any) -> None:
    get_logger().info(event, extra={"fields": fields})


@contextmanager
def log_step(step: str, run_id: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Time a pipeline step. The yielded dict collects extra fields (tokens, cost, counts)."""
    extra: dict[str, Any] = dict(fields)
    start = time.perf_counter()
    status = "ok"
    try:
        yield extra
    except Exception as exc:
        status = "error"
        extra["error"] = repr(exc)
        raise
    finally:
        duration_ms = round((time.perf_counter() - start) * 1000)
        log_event("step", step=step, run_id=run_id, duration_ms=duration_ms, status=status, **extra)
