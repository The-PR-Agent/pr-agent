import os

os.environ["AUTO_CAST_FOR_DYNACONF"] = "false"
import json
import logging
import sys
from enum import Enum
from string import Formatter

from loguru import logger


class LoggingFormat(str, Enum):
    CONSOLE = "CONSOLE"
    JSON = "JSON"


def json_format(record: dict) -> str:
    return record["message"]


def analytics_filter(record: dict) -> bool:
    return record.get("extra", {}).get("analytics", False)


def inv_analytics_filter(record: dict) -> bool:
    return not record.get("extra", {}).get("analytics", False)


def setup_logger(level: str = "INFO", fmt: LoggingFormat = LoggingFormat.CONSOLE):
    level: int = logging.getLevelName(level.upper())
    if type(level) is not int:
        level = logging.INFO

    if fmt == LoggingFormat.JSON and os.getenv("LOG_SANE", "0").lower() == "0":  # better debugging github_app
        logger.remove(None)
        logger.add(
            sys.stdout,
            filter=inv_analytics_filter,
            level=level,
            format="{message}",
            colorize=False,
            serialize=True,
        )
    elif fmt == LoggingFormat.CONSOLE: # does not print the 'extra' fields
        logger.remove(None)
        logger.add(sys.stdout, level=level, colorize=True, filter=inv_analytics_filter)

    # Imported lazily so `from pr_agent.log import get_logger` does not pull
    # in config_loader while this package is still initializing. config_loader
    # loads settings via custom_merge_loader, which itself imports get_logger.
    from pr_agent.config_loader import get_settings

    log_folder = get_settings().get("CONFIG.ANALYTICS_FOLDER", "")
    if log_folder:
        pid = os.getpid()
        log_file = os.path.join(log_folder, f"pr-agent.{pid}.log")
        logger.add(
            log_file,
            filter=analytics_filter,
            level=level,
            format="{message}",
            colorize=False,
            serialize=True,
        )

    return logger


def _is_template_for(message, payload: dict) -> bool:
    """Whether the message reads as a loguru template these keywords are meant to fill.

    An already-interpolated message carries braces from data, whose field names do not match
    the payload; a real template names every field it uses.
    """
    try:
        fields = [name for _, name, _, _ in Formatter().parse(str(message)) if name]
    except ValueError:
        return False  # unbalanced braces: never a template
    if not fields:
        return False
    return all(name.split(".")[0].split("[")[0] in payload for name in fields)


class StructuredLogger:
    """loguru proxy that attaches a keyword payload without re-formatting the message.

    loguru formats the message with `str.format(*args, **kwargs)` as soon as a keyword
    argument is passed. The codebase passes `artifact=` (and friends) to carry structured
    data, so a message that already interpolated an exception - every PyGithub error renders
    the provider's JSON - used to raise `KeyError` from inside the `except` block reporting
    it. Binding the payload instead puts it in `record["extra"]`, which is where the JSON
    sink already reads it from, and leaves the message untouched.

    Explicit templating keeps working: positional arguments are passed straight through, and
    a message whose fields are all named in the payload is still formatted.
    """

    def __init__(self, wrapped):
        self._logger = wrapped

    def __getattr__(self, name):
        return getattr(self._logger, name)

    def _emit(self, method: str, message, args: tuple, payload: dict):
        target = self._logger.opt(depth=2)  # report the caller, not this proxy
        if payload and not args and not _is_template_for(message, payload):
            return getattr(target.bind(**payload), method)(message)
        return getattr(target, method)(message, *args, **payload)

    def trace(self, message, *args, **kwargs):
        return self._emit("trace", message, args, kwargs)

    def debug(self, message, *args, **kwargs):
        return self._emit("debug", message, args, kwargs)

    def info(self, message, *args, **kwargs):
        return self._emit("info", message, args, kwargs)

    def success(self, message, *args, **kwargs):
        return self._emit("success", message, args, kwargs)

    def warning(self, message, *args, **kwargs):
        return self._emit("warning", message, args, kwargs)

    def error(self, message, *args, **kwargs):
        return self._emit("error", message, args, kwargs)

    def critical(self, message, *args, **kwargs):
        return self._emit("critical", message, args, kwargs)

    def exception(self, message, *args, **kwargs):
        return self._emit("exception", message, args, kwargs)

    def log(self, level, message, *args, **kwargs):
        target = self._logger.opt(depth=2)
        if kwargs and not args and not _is_template_for(message, kwargs):
            return target.bind(**kwargs).log(level, message)
        return target.log(level, message, *args, **kwargs)


_structured_logger = StructuredLogger(logger)


def get_logger(*args, **kwargs):
    return _structured_logger
