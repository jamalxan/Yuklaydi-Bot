from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from bot.config import settings


class JsonFormatter(logging.Formatter):
    """One valid JSON object per line.

    The old printf-style template produced broken JSON as soon as a message
    contained a quote or a newline (exception texts, tracebacks). Here every
    value goes through json.dumps; tracebacks land in an "exc" field and
    structured data passed as ``extra={"fields": {...}}`` is merged in.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            for k, v in fields.items():
                payload.setdefault(k, v)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exc"] = record.exc_text
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)

    # Quiet noisy libs
    for name in ("aiogram", "aiohttp", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)


logger = logging.getLogger("yuklaydi")
