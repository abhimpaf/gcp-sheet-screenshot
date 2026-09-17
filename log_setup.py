"""Structured logging for Cloud Run / Cloud Logging.

Cloud Logging picks up single-line JSON on stdout and promotes the well known
fields (severity, message, logging.googleapis.com/trace) into the log entry, so
plain prints become searchable entries with the right log level.
"""
import json
import logging
import os
import sys
import time

PROJECT_ID = (
    os.environ.get("GOOGLE_CLOUD_PROJECT")
    or os.environ.get("GCP_PROJECT")
    or os.environ.get("GCLOUD_PROJECT")
    or ""
)

# Populated per request by the Flask before_request hook.
_TRACE = {}

_LEVEL_TO_SEVERITY = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARNING",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRITICAL",
}


def set_trace(trace_id, span_id=None):
    _TRACE["trace"] = trace_id
    _TRACE["span"] = span_id


def clear_trace():
    _TRACE.clear()


class CloudLoggingFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "severity": _LEVEL_TO_SEVERITY.get(record.levelno, "DEFAULT"),
            "message": record.getMessage(),
            "logger": record.name,
            "sourceLocation": {
                "file": record.pathname,
                "line": record.lineno,
                "function": record.funcName,
            },
        }

        if record.exc_info:
            # Keep the traceback inside `message`: Cloud Error Reporting only
            # groups an entry when the stack trace is part of the message.
            entry["message"] = "%s\n%s" % (
                entry["message"],
                self.formatException(record.exc_info),
            )

        extra = getattr(record, "json_fields", None)
        if extra:
            entry.update(extra)

        trace_id = _TRACE.get("trace")
        if trace_id and PROJECT_ID:
            entry["logging.googleapis.com/trace"] = "projects/%s/traces/%s" % (
                PROJECT_ID,
                trace_id,
            )
        elif trace_id:
            entry["trace_id"] = trace_id
        if _TRACE.get("span"):
            entry["logging.googleapis.com/spanId"] = _TRACE["span"]

        return json.dumps(entry, default=str)


def configure(level=None):
    """Send every log record to stdout as structured JSON."""
    level = level or os.environ.get("LOG_LEVEL", "INFO").upper()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(CloudLoggingFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # Gunicorn installs its own handlers on these; drop them so we do not get
    # every line twice, once structured and once plain.
    for name in ("gunicorn.error", "gunicorn.access", "werkzeug"):
        gl = logging.getLogger(name)
        gl.handlers = []
        gl.propagate = True

    logging.captureWarnings(True)
    return root


def log(logger, level, message, **fields):
    """Log `message` with arbitrary structured fields attached."""
    logger.log(level, message, extra={"json_fields": fields})


class Timer:
    """Context manager measuring wall time in milliseconds."""

    def __enter__(self):
        self._start = time.perf_counter()
        self.ms = 0.0
        return self

    def __exit__(self, *exc):
        self.ms = round((time.perf_counter() - self._start) * 1000, 1)
        return False
