import io
import logging
import os
import traceback
import uuid

from flask import Flask, g, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

import log_setup
from log_setup import Timer, log

log_setup.configure()
logger = logging.getLogger("sheet-screenshot")

BOOT_ID = uuid.uuid4().hex[:8]

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
)


def safe_get(arr, i, j, default):
    try:
        return arr[i][j] if arr[i][j] else default
    except Exception:
        return default


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
# HTML GENERATOR WITH CUSTOM STYLING
# -------------------------------
def generate_html_from_formatted(payload):
    blocks = payload.get("data", payload)

    if not isinstance(blocks, dict):
        raise ValueError("expected an object of blocks, got %s" % type(blocks).__name__)

    html = """
    <html>
    <head>
    <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;700&display=swap" rel="stylesheet">
    <style>
    body {
        margin: 0;
        padding: 0;
        background: #ffffff;
        display: inline-block;
        font-family: 'IBM Plex Mono', 'Cascadia Mono', Consolas, monospace;
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

    rendered_blocks = 0

    for block_name, block in blocks.items():
        if not isinstance(block, dict):
            log(
                logger,
                logging.WARNING,
                "skipping block %r: expected object, got %s"
                % (block_name, type(block).__name__),
                request_id=getattr(g, "request_id", None),
                block=block_name,
            )
            continue

        values = block.get("values", [])
        backgrounds = block.get("backgrounds", [])

        if not values:
            log(
                logger,
                logging.WARNING,
                "skipping block %r: no values" % block_name,
                request_id=getattr(g, "request_id", None),
                block=block_name,
            )
            continue

        log(
            logger,
            logging.DEBUG,
            "rendering block %r" % block_name,
            request_id=getattr(g, "request_id", None),
            block=block_name,
            rows=len(values),
            background_rows=len(backgrounds),
        )

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

        html += "<table>"
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
        html += "</table>"
        rendered_blocks += 1

    if rendered_blocks == 0:
        raise ValueError(
            "payload contained no renderable block (keys seen: %s)"
            % list(blocks.keys())[:20]
        )

    html += "</body></html>"
    return html


# -------------------------------
# SCREENSHOT FUNCTION
# -------------------------------
def take_screenshot(html):
    request_id = getattr(g, "request_id", None)
    with sync_playwright() as p:
        with Timer() as t:
            browser = p.chromium.launch()
        log(logger, logging.INFO, "chromium launched", request_id=request_id, duration_ms=t.ms)

        context = browser.new_context(device_scale_factor=2)
        page = context.new_page()

        page.on("pageerror", lambda e: log(
            logger, logging.WARNING, "page error: %s" % e, request_id=request_id,
        ))
        page.on("console", lambda m: log(
            logger, logging.DEBUG, "console[%s]: %s" % (m.type, m.text), request_id=request_id,
        ))

        try:
            with Timer() as t:
                page.set_content(html, wait_until="networkidle")
                page.evaluate("document.fonts.ready")
            log(logger, logging.INFO, "content rendered", request_id=request_id, duration_ms=t.ms)

            table = page.locator("table").first
            if table.count() == 0:
                raise ValueError("no <table> element rendered - nothing to screenshot")

            with Timer() as t:
                image_bytes = table.screenshot(type="png")
            log(
                logger, logging.INFO, "screenshot captured",
                request_id=request_id, duration_ms=t.ms, image_bytes=len(image_bytes),
            )
        finally:
            browser.close()
    return image_bytes


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
        "playwright_import_error": PLAYWRIGHT_IMPORT_ERROR,
    }
    if not healthy:
        logger.error("healthz reporting degraded: %s", PLAYWRIGHT_IMPORT_ERROR)
    return jsonify(body), (200 if healthy else 503)


@app.route("/generate", methods=["POST"])
def generate():
    request_id = g.request_id

    if sync_playwright is None:
        log(
            logger, logging.ERROR, "rejecting /generate: playwright unavailable",
            request_id=request_id,
        )
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

    try:
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
            return jsonify({"error": "No JSON received", "request_id": request_id}), 400

        blocks = payload.get("data", payload) if isinstance(payload, dict) else None
        log(
            logger, logging.INFO, "payload parsed",
            request_id=request_id,
            body_bytes=len(raw),
            block_count=len(blocks) if isinstance(blocks, dict) else None,
            block_names=list(blocks.keys())[:20] if isinstance(blocks, dict) else None,
        )

        with Timer() as t:
            html = generate_html_from_formatted(payload)
        log(
            logger, logging.INFO, "html generated",
            request_id=request_id, duration_ms=t.ms, html_bytes=len(html),
        )

        image_bytes = take_screenshot(html)

        response = send_file(
            io.BytesIO(image_bytes),
            mimetype="image/png",
            download_name="screenshot.png"
        )
        response.headers["X-Request-Id"] = request_id
        return response

    except Exception as e:
        logger.exception("generate failed (request_id=%s)", request_id)
        return (
            jsonify(
                {
                    "error": type(e).__name__,
                    "message": str(e),
                    "request_id": request_id,
                }
            ),
            500,
        )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
