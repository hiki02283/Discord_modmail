"""Logging setup.

Two destinations: a readable console stream and a rotating file. A redaction
filter runs on every record so a bot token cannot reach a log file or the
terminal, even if it is accidentally formatted into a message.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)-24s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_FILE_NAME = "modmail.log"
_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 3

# Discord bot tokens are three base64url-ish segments separated by dots, and
# the first segment is a snowflake. Matching loosely on purpose: a false
# positive is harmless, a leaked token is not.
_TOKEN_PATTERN = re.compile(r"\b\d{15,25}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{20,}\b")

_REDACTED = "[REDACTED]"


class RedactingFilter(logging.Filter):
    """Replace anything resembling a Discord token in log output."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and _TOKEN_PATTERN.search(record.msg):
            record.msg = _TOKEN_PATTERN.sub(_REDACTED, record.msg)

        if record.args:
            record.args = _redact_args(record.args)

        if record.exc_text:
            record.exc_text = _TOKEN_PATTERN.sub(_REDACTED, record.exc_text)

        return True


def _redact_args(args: object) -> object:
    """Redact token-shaped strings inside logging arguments."""
    if isinstance(args, dict):
        return {key: _redact_args(value) for key, value in args.items()}
    if isinstance(args, tuple):
        return tuple(_redact_args(item) for item in args)
    if isinstance(args, str):
        return _TOKEN_PATTERN.sub(_REDACTED, args)
    return args


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> Path:
    """Configure root logging and return the log file path.

    Safe to call more than once: existing handlers are replaced rather than
    stacked, which matters for tests and for restarts in the same process.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    redactor = RedactingFilter()
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(formatter)
    console.addFilter(redactor)
    root.addHandler(console)

    if log_dir is None:
        log_dir = Path("logs")
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / _FILE_NAME

    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redactor)
    root.addHandler(file_handler)

    # discord.py is chatty at DEBUG; keep its own logger at the requested level.
    logging.getLogger("discord").setLevel(getattr(logging, level.upper(), logging.INFO))
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    return log_path
