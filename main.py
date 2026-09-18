import base64
import hmac
import io
import logging
import os
import threading
import traceback
import uuid

from flask import Flask, g, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

import log_setup
from log_setup import Timer, log

log_setup.configure()
logger = logging.getLogger("sheet-screenshot")

BOOT_ID = uuid.uuid4().hex[:8]

# Shared secret. When unset the service stays open, so turning auth on is a
# deliberate act (set API_KEY on the revision) and cannot lock out a caller
# that has not been updated yet.
API_KEY = os.environ.get("API_KEY", "")

# Chromium needs roughly 400-600MB resident. Keep concurrent renders bounded so
# a burst of parallel calls cannot OOM-kill the container mid-request.
MAX_CONCURRENT_RENDERS = int(os.environ.get("MAX_CONCURRENT_RENDERS", "2"))
MAX_TABLES_PER_REQUEST = int(os.environ.get("MAX_TABLES_PER_REQUEST", "50"))

_render_slots = threading.Semaphore(MAX_CONCURRENT_RENDERS)

# Playwright pulls in a lot at import time and needs the browser bundle present
# in the image. If that is broken we still want a live `app` object so gunicorn
# boots and the failure shows up as a logged 503 instead of a crash loop that
# only says "App failed to load".
PLAYWRIGHT_IMPORT_ERROR = None
try:
    with Timer() as _t:
        from playwright.sync_api import sync_playwright
    log(logger, logging.INFO, "playwright imported", boot_id=BOOT_ID, duration_ms=_t.ms)
except Exception:
    PLAYWRIGHT_IMPORT_ERROR = traceback.format_exc()
    sync_playwright = None
    logger.exception("playwright import failed - /generate will return 503")

app = Flask(__name__)

# gunicorn is invoked as `main:app`, but Cloud Run buildpacks and some
# functions-framework entrypoints look for `main:application`. Expose both so a
# change of entrypoint can never produce "Failed to find attribute".
application = app

log(
    logger,
    logging.INFO,
    "module loaded",
    boot_id=BOOT_ID,
    pid=os.getpid(),
    playwright_ok=PLAYWRIGHT_IMPORT_ERROR is None,
    auth_required=bool(API_KEY),
    max_concurrent_renders=MAX_CONCURRENT_RENDERS,
)

if not API_KEY:
    log(
        logger,
        logging.WARNING,
        "API_KEY is not set - /generate and /generate-batch are unauthenticated",
        boot_id=BOOT_ID,
    )


def safe_get(arr, i, j, default):
    try:
        return arr[i][j] if arr[i][j] else default
    except Exception:
        return default


# -------------------------------
# BROWSER POOL
# -------------------------------
# Playwright's sync objects are bound to the thread that created them, so a
# single shared browser cannot be driven from another worker thread. One
# browser per thread, launched once and reused for every later request, turns a
# ~1s cold Chromium launch per screenshot into a one-off cost per thread.
_local = threading.local()


def _browser():
    existing = getattr(_local, "browser", None)
    if existing is not None and existing.is_connected():
        return existing

    if existing is not None:
        log(logger, logging.WARNING, "browser disconnected - relaunching",
            thread=threading.current_thread().name)
        _shutdown_local_browser()

    with Timer() as t:
        _local.pw = sync_playwright().start()
        _local.browser = _local.pw.chromium.launch(
            args=[
                # Cloud Run gives containers a small /dev/shm; without this
                # Chromium can crash part-way through a render.
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-sandbox",
            ]
        )
    log(
        logger, logging.INFO, "chromium launched",
        thread=threading.current_thread().name, duration_ms=t.ms,
    )
    return _local.browser


def _shutdown_local_browser():
    for attr in ("browser", "pw"):
        obj = getattr(_local, attr, None)
        if obj is None:
            continue
        try:
            obj.close() if attr == "browser" else obj.stop()
        except Exception:
            logger.exception("failed to tear down %s", attr)
        setattr(_local, attr, None)


# -------------------------------
# AUTH
# -------------------------------
def _reject_if_unauthorized():
    """Returns a response tuple when the caller is not allowed, else None."""
    if not API_KEY:
        return None

    provided = request.headers.get("X-Api-Key", "")
    if hmac.compare_digest(provided, API_KEY):
        return None

    log(
        logger, logging.WARNING, "rejected unauthenticated request",
        request_id=getattr(g, "request_id", None),
        path=request.path,
        key_present=bool(provided),
        remote_ip=request.headers.get("X-Forwarded-For", request.remote_addr),
    )
    return (
        jsonify(
            {
                "error": "Unauthorized",
                "message": "missing or invalid X-Api-Key",
                "request_id": getattr(g, "request_id", None),
            }
        ),
        401,
    )


# -------------------------------
# REQUEST LOGGING
# -------------------------------
@app.before_request
def _start_request():
    g.request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12]
    g.timer = Timer().__enter__()

    # Cloud Run forwards "TRACE_ID/SPAN_ID;o=1"; splitting it lets Cloud Logging
    # thread our entries onto the same request as the platform's own.
    header = request.headers.get("X-Cloud-Trace-Context", "")
    if header:
        trace_id = header.split("/")[0]
        span_id = header.split("/")[1].split(";")[0] if "/" in header else None
        log_setup.set_trace(trace_id, span_id)

    log(
        logger,
        logging.INFO,
        "request start %s %s" % (request.method, request.path),
        request_id=g.request_id,
        method=request.method,
        path=request.path,
        content_length=request.content_length,
        content_type=request.content_type,
        user_agent=request.headers.get("User-Agent"),
        remote_ip=request.headers.get("X-Forwarded-For", request.remote_addr),
    )


@app.after_request
def _end_request(response):
    timer = getattr(g, "timer", None)
    if timer:
        timer.__exit__()
    log(
        logger,
        logging.INFO if response.status_code < 400 else logging.WARNING,
        "request end %s %s -> %s" % (request.method, request.path, response.status_code),
        request_id=getattr(g, "request_id", None),
        status=response.status_code,
        duration_ms=timer.ms if timer else None,
    )
    return response


@app.teardown_request
def _clear_trace(exc):
    log_setup.clear_trace()


@app.errorhandler(Exception)
def _handle_unexpected(exc):
    """Catch anything a route did not, so nothing fails silently."""
    request_id = getattr(g, "request_id", None)

    if isinstance(exc, HTTPException):
        # 404 / 405 / 413 are client mistakes, not crashes - log the fact, not a
        # traceback, so real failures stay easy to spot.
        log(
            logger,
            logging.WARNING,
            "http error %s on %s %s" % (exc.code, request.method, request.path),
            request_id=request_id,
            status=exc.code,
            reason=exc.description,
        )
        return (
            jsonify(
                {"error": exc.name, "message": exc.description, "request_id": request_id}
            ),
            exc.code,
        )

    logger.exception(
        "unhandled exception on %s %s (request_id=%s)",
        request.method,
        request.path,
        request_id,
    )
    return (
        jsonify(
            {
                "error": type(exc).__name__,
                "message": str(exc),
                "request_id": request_id,
            }
        ),
        500,
    )


# -------------------------------
# HTML GENERATION
# -------------------------------
# No external stylesheet: the font is installed in the image instead, so a
# render never waits on fonts.googleapis.com (one network round trip per
# screenshot, and a hang if egress is slow).
HTML_HEAD = """<html>
<head>
<meta charset="utf-8">
<style>
body {
    margin: 0;
    padding: 0;
    background: #ffffff;
    display: inline-block;
    font-family: 'IBM Plex Mono', 'DejaVu Sans Mono', 'Cascadia Mono', Consolas, monospace;
}
table {
    border-collapse: collapse;
    margin: 0;
    border-spacing: 0;
    font-size: 12px;
}
td {
    border: 1px solid #cccccc;
    padding: 6px 12px;
    white-space: nowrap;
    width: max-content;
}
</style>
</head>
<body>
"""

HTML_TAIL = "</body></html>"


def page_html(body):
    return HTML_HEAD + body + HTML_TAIL


def build_table_html(block):
    """Render one {values, backgrounds} block as a single <table>."""
    if not isinstance(block, dict):
        raise ValueError("expected an object, got %s" % type(block).__name__)

    values = block.get("values", [])
    backgrounds = block.get("backgrounds", [])

    if not values:
        raise ValueError("block has no values")

    # Dynamically find the indexes to ensure styling doesn't break
    batch_idx = 0 # Fallback to index 0
    name_idx = 1 # Fallback to index 1
    divider_idx = 3 # Fallback to index 3

    if len(values) > 1:
        headers_lower = [str(h).strip().lower() for h in values[1]]

        try:
            batch_idx = headers_lower.index("batch")
        except ValueError:
            pass

        try:
            name_idx = headers_lower.index("name")
        except ValueError:
            pass

        try:
            divider_idx = headers_lower.index("total calls")
        except ValueError:
            try:
                divider_idx = headers_lower.index("valid calls")
            except ValueError:
                pass

    html = "<table>"
    max_cols = max((len(row) for row in values), default=1)

    for i in range(len(values)):
        html += "<tr>"

        for j in range(len(values[i])):
            if i == 0 and j > 0:
                continue

            val = values[i][j]
            if val == "" or val is None:
                val = "&nbsp;"

            bg = "#ffffff"
            color = "#000000"
            weight = "normal"
            align = "center"

            # 1. Title Row
            if i == 0:
                bg = "#ffffff"
                weight = "bold"
                align = "center"

            # 2. Header Row (Light Purple)
            elif i == 1:
                bg = "#e9d5ff"
                weight = "bold"
                align = "center"

            # 3. Data Rows
            else:
                # Highlight BOTH Batch and Name columns in a lighter yellow
                if j == batch_idx or j == name_idx:
                    bg = "#fef9c3" # Lighter shade of yellow
                    weight = "bold"
                    align = "left"
                else:
                    align = "center"
                    # Pull the red threshold highlight from Apps Script payload
                    sheet_bg = str(safe_get(backgrounds, i, j, "#ffffff"))
                    if sheet_bg.lower() not in ["#ffffff", "#fff", "white"]:
                        bg = sheet_bg

            style = f"background:{bg}; color:{color}; font-weight:{weight}; text-align:{align};"

            # Keep the divider line dynamically after Total Calls
            if j == divider_idx:
                style += " border-right: 2px solid #94a3b8;"

            if i == 0 and j == 0:
                html += f"<td colspan='{max_cols}' style='{style}'>{val}</td>"
            else:
                html += f"<td style='{style}'>{val}</td>"

        html += "</tr>"
    return html + "</table>"


# -------------------------------
# RENDERING
# -------------------------------
def render_tables(tables, request_id):
    """Screenshot each {name: block} on one reused browser.

    Returns (images, errors, timings). A table that fails to render is recorded
    in `errors` and does not lose the rest of the batch. `timings` is returned
    to the caller so per-table cost is visible without reading Cloud Logging.
    """
    images = {}
    errors = {}
    timings = {}

    with _render_slots:
        browser = _browser()
        context = browser.new_context(device_scale_factor=2)
        page = context.new_page()
        page.on("pageerror", lambda e: log(
            logger, logging.WARNING, "page error: %s" % e, request_id=request_id,
        ))
        page.on("console", lambda m: log(
            logger, logging.DEBUG, "console[%s]: %s" % (m.type, m.text),
            request_id=request_id,
        ))

        try:
            for name, block in tables.items():
                try:
                    with Timer() as t:
                        # Everything is inline, so "load" is enough - waiting for
                        # networkidle would add ~500ms of dead time per table.
                        page.set_content(page_html(build_table_html(block)),
                                         wait_until="load")
                        page.evaluate("async () => { await document.fonts.ready; }")

                        table = page.locator("table").first
                        if table.count() == 0:
                            raise ValueError("no <table> rendered")
                        images[name] = table.screenshot(type="png")

                    timings[name] = t.ms
                    log(
                        logger, logging.INFO, "rendered %r" % name,
                        request_id=request_id, table=name,
                        duration_ms=t.ms, image_bytes=len(images[name]),
                        rows=len(block.get("values", [])) if isinstance(block, dict) else None,
                    )
                except Exception as e:
                    logger.exception(
                        "render failed for %r (request_id=%s)", name, request_id
                    )
                    errors[name] = "%s: %s" % (type(e).__name__, e)
        finally:
            try:
                context.close()
            except Exception:
                logger.exception("failed to close browser context")

    return images, errors, timings


def _parse_body(request_id):
    """Parse the JSON body leniently. Returns (payload, error_response)."""
    raw = request.get_data(cache=True)
    # Apps Script's UrlFetchApp often posts without a JSON content type, so
    # parse leniently rather than letting Flask 400 on the header alone.
    payload = request.get_json(silent=True, force=True)
    if not payload:
        log(
            logger, logging.WARNING, "no JSON received",
            request_id=request_id,
            content_type=request.content_type,
            body_bytes=len(raw),
            body_preview=raw[:500].decode("utf-8", "replace"),
        )
        return None, (
            jsonify({"error": "No JSON received", "request_id": request_id}),
            400,
        )
    log(logger, logging.INFO, "payload parsed",
        request_id=request_id, body_bytes=len(raw))
    return payload, None


# -------------------------------
# ROUTES
# -------------------------------
@app.route("/", methods=["GET"])
def home():
    return "Image API is running (boot %s)" % BOOT_ID


@app.route("/healthz", methods=["GET"])
def healthz():
    """Readiness probe that also reports why the app is degraded, if it is."""
    healthy = PLAYWRIGHT_IMPORT_ERROR is None
    body = {
        "status": "ok" if healthy else "degraded",
        "boot_id": BOOT_ID,
        "pid": os.getpid(),
        "auth_required": bool(API_KEY),
        "max_concurrent_renders": MAX_CONCURRENT_RENDERS,
        "playwright_import_error": PLAYWRIGHT_IMPORT_ERROR,
    }
    if not healthy:
        logger.error("healthz reporting degraded: %s", PLAYWRIGHT_IMPORT_ERROR)
    return jsonify(body), (200 if healthy else 503)


@app.route("/generate", methods=["POST"])
def generate():
    """Single screenshot, returned as a PNG. Kept for the older caller."""
    request_id = g.request_id

    denied = _reject_if_unauthorized()
    if denied:
        return denied

    if sync_playwright is None:
        log(logger, logging.ERROR, "rejecting /generate: playwright unavailable",
            request_id=request_id)
        return (
            jsonify(
                {
                    "error": "PlaywrightUnavailable",
                    "message": "browser runtime failed to load at startup; see /healthz",
                    "request_id": request_id,
                }
            ),
            503,
        )

    payload, error = _parse_body(request_id)
    if error:
        return error

    blocks = payload.get("data", payload) if isinstance(payload, dict) else None
    if not isinstance(blocks, dict) or not blocks:
        return (
            jsonify(
                {
                    "error": "BadPayload",
                    "message": "expected an object of blocks under 'data'",
                    "request_id": request_id,
                }
            ),
            400,
        )

    images, errors, _ = render_tables(blocks, request_id)
    if not images:
        return (
            jsonify(
                {
                    "error": "RenderFailed",
                    "message": "; ".join(errors.values()) or "nothing rendered",
                    "request_id": request_id,
                }
            ),
            500,
        )

    first = next(iter(images.values()))
    response = send_file(
        io.BytesIO(first),
        mimetype="image/png",
        download_name="screenshot.png",
    )
    response.headers["X-Request-Id"] = request_id
    return response


@app.route("/generate-batch", methods=["POST"])
def generate_batch():
    """Screenshot many tables on one browser and return them base64 encoded.

    One call replaces N round trips and N Chromium launches. A table that fails
    is reported in `errors` while the rest still come back.
    """
    request_id = g.request_id

    denied = _reject_if_unauthorized()
    if denied:
        return denied

    if sync_playwright is None:
        log(logger, logging.ERROR, "rejecting /generate-batch: playwright unavailable",
            request_id=request_id)
        return (
            jsonify(
                {
                    "error": "PlaywrightUnavailable",
                    "message": "browser runtime failed to load at startup; see /healthz",
                    "request_id": request_id,
                }
            ),
            503,
        )

    payload, error = _parse_body(request_id)
    if error:
        return error

    tables = payload.get("tables") if isinstance(payload, dict) else None
    if not isinstance(tables, dict) or not tables:
        log(logger, logging.WARNING, "no tables in payload", request_id=request_id,
            keys=list(payload.keys())[:20] if isinstance(payload, dict) else None)
        return (
            jsonify(
                {
                    "error": "BadPayload",
                    "message": "expected a non-empty object under 'tables'",
                    "request_id": request_id,
                }
            ),
            400,
        )

    if len(tables) > MAX_TABLES_PER_REQUEST:
        return (
            jsonify(
                {
                    "error": "TooManyTables",
                    "message": "%d tables requested, limit is %d"
                    % (len(tables), MAX_TABLES_PER_REQUEST),
                    "request_id": request_id,
                }
            ),
            413,
        )

    log(logger, logging.INFO, "batch accepted", request_id=request_id,
        table_count=len(tables), table_names=list(tables.keys())[:50])

    with Timer() as t:
        images, errors, timings = render_tables(tables, request_id)

    log(
        logger,
        logging.INFO if not errors else logging.WARNING,
        "batch complete: %d rendered, %d failed" % (len(images), len(errors)),
        request_id=request_id,
        rendered=len(images),
        failed=len(errors),
        duration_ms=t.ms,
        ms_per_table=round(t.ms / max(len(tables), 1), 1),
    )

    response = jsonify(
        {
            "images": {
                k: base64.b64encode(v).decode("ascii") for k, v in images.items()
            },
            "errors": errors,
            "rendered": len(images),
            "failed": len(errors),
            # Per-table milliseconds, so the caller can see where the time went
            # without anyone having to open Cloud Logging.
            "timings": timings,
            "total_ms": t.ms,
            "request_id": request_id,
        }
    )
    response.headers["X-Request-Id"] = request_id
    # 200 even with partial failures: the caller reads `errors` and still gets
    # every table that did render. 500 only when nothing came back at all.
    return response, (200 if images else 500)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
