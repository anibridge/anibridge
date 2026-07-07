"""API endpoints for accessing historical log files."""

import logging
import re
from pathlib import Path
from typing import Annotated

import msgspec
from litestar.exceptions.http_exceptions import HTTPException
from litestar.handlers.http_handlers.decorators import get
from litestar.params import PathParameter, QueryParameter
from litestar.router import Router

from anibridge.app.config.settings import get_config
from anibridge.app.exceptions import InvalidLogFileNameError, LogFileNotFoundError
from anibridge.app.logging import APP_LOGGER_NAME, get_logger

__all__ = ["router"]

LOG_DIR: Path | None = None


class LogFileModel(msgspec.Struct):
    name: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Log file base name.",
            examples=["anibridge.INFO.log"],
        ),
    ]
    size: Annotated[
        int,
        msgspec.Meta(
            ge=0,
            description="Log file size in bytes.",
            examples=[8192],
        ),
    ]
    mtime: Annotated[
        int,
        msgspec.Meta(
            ge=0,
            description="Log file modification time in epoch milliseconds.",
            examples=[1715179200000],
        ),
    ]
    current: Annotated[
        bool,
        msgspec.Meta(
            description="Whether this file is the currently active log target.",
            examples=[True],
        ),
    ]


class LogEntryModel(msgspec.Struct):
    level: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Parsed log level for the line.",
            examples=["INFO"],
        ),
    ]
    message: Annotated[
        str,
        msgspec.Meta(
            min_length=1,
            description="Rendered log message.",
            examples=["Scheduler initialized successfully"],
        ),
    ]
    timestamp: (
        Annotated[
            str,
            msgspec.Meta(
                description="Parsed timestamp from the log line when available.",
                examples=["2026-01-01 00:00:00"],
            ),
        ]
        | None
    ) = None


def _get_log_dir() -> Path:
    if LOG_DIR is not None:
        return LOG_DIR.resolve()
    return (get_config().data_path / "logs").resolve()


def _is_log_filename(name: str) -> bool:
    lower_name = name.lower()
    return lower_name.startswith("anibridge.") and ".log" in lower_name


LINE_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - "
    r"(?P<level>[A-Z]+) - (?P<logger>[^ ]+?)"
    r"(?: - (?P<source>[^ ]+:\d+))? - (?P<message>.*)$"
)


def _list_log_files() -> list[Path]:
    log_dir = _get_log_dir()
    if not log_dir.exists():
        return []
    # Include the active log file and rotated backups.
    files = {
        path.name: path
        for path in log_dir.iterdir()
        if path.is_file() and _is_log_filename(path.name)
    }

    return sorted(files.values(), key=lambda p: p.stat().st_mtime, reverse=True)


@get(path="/files", sync_to_thread=True)
def list_log_files() -> list[LogFileModel]:
    """Return metadata about available log files."""
    files = _list_log_files()
    res: list[LogFileModel] = []

    # Determine current effective log level to identify active file.
    root_logger = get_logger(APP_LOGGER_NAME)
    current_level_name = logging.getLevelName(root_logger.getEffectiveLevel())
    active_basename = f"anibridge.{current_level_name}.log"

    for f in files:
        st = f.stat()
        res.append(
            LogFileModel(
                name=f.name,
                size=st.st_size,
                mtime=int(st.st_mtime * 1000),
                # Active file must exactly match the lowercase logger filename
                current=f.name == active_basename,
            )
        )

    return res


def _safe_resolve(name: str) -> Path:
    """Resolve a user-supplied file name safely within LOG_DIR."""
    if "/" in name or ".." in name:
        raise InvalidLogFileNameError("Invalid log file name")

    log_dir = _get_log_dir()
    target = (log_dir / name).resolve()

    try:
        target.relative_to(log_dir)
    except ValueError as exc:
        raise InvalidLogFileNameError("Invalid log file name") from exc

    if not _is_log_filename(target.name):
        raise InvalidLogFileNameError("Invalid log file name")

    if not target.exists() or not target.is_file():
        raise LogFileNotFoundError("Log file not found")

    return target


def _tail_lines(path: Path, max_lines: int) -> list[str]:
    """Return up to the last max_lines of the file efficiently."""
    if max_lines == 0:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return [ln.rstrip("\n\r") for ln in fh]

    chunk_size = 8192

    with path.open("rb") as fh:
        fh.seek(0, 2)
        file_size = fh.tell()
        if file_size <= 0:
            return []

        pos = file_size
        blocks: list[bytes] = []
        newline_count = 0

        # Read backwards until we have enough separators to reconstruct
        # the final max_lines entries.
        while pos > 0 and newline_count <= max_lines:
            read_size = min(chunk_size, pos)
            pos -= read_size
            fh.seek(pos)
            block = fh.read(read_size)
            blocks.append(block)
            newline_count += block.count(b"\n")

    data = b"".join(reversed(blocks))
    tail_bytes = data.splitlines()[-max_lines:]
    return [line.decode("utf-8", errors="replace") for line in tail_bytes]


@get(path="/file/{name:str}", sync_to_thread=True)
def get_log_file(
    name: Annotated[str, PathParameter()],
    lines: Annotated[int, QueryParameter(ge=0, le=5000)] = 500,
) -> list[LogEntryModel]:
    """Return the last N lines of a log file parsed into JSON entries."""
    if lines < 0 or lines > 5000:
        raise HTTPException(status_code=400, detail="lines must be between 0 and 5000")

    path = _safe_resolve(name)
    raw_lines = _tail_lines(path, lines)
    res: list[LogEntryModel] = []

    for ln in raw_lines:
        ln = ln.rstrip("\n\r")
        m = LINE_RE.match(ln)
        if m:
            gd = m.groupdict()
            res.append(
                LogEntryModel(
                    timestamp=gd["timestamp"], level=gd["level"], message=gd["message"]
                )
            )
        else:
            res.append(LogEntryModel(timestamp=None, level="INFO", message=ln))

    return res


router = Router(path="/logs", route_handlers=[list_log_files, get_log_file])
