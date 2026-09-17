# gcp_sheet_screenshot_project

Renders Google Sheets data as PNG tables so reports can be emailed to people
who cannot be given access to the spreadsheet itself.

`gcode.js` is the Apps Script side (paste it into the bound script project).
`main.py` is the Cloud Run service it calls.

## Endpoints

| Route             | Method | Purpose                                                  |
| ----------------- | ------ | -------------------------------------------------------- |
| `/`               | GET    | Liveness string with the boot id                          |
| `/healthz`        | GET    | Readiness; reports the Playwright import error if degraded |
| `/generate-batch` | POST   | Many tables, one browser. What `gcode.js` uses             |
| `/generate`       | POST   | Single table, returns a PNG. Kept for the older caller     |

`/generate-batch` takes `{"tables": {"<key>": {"values": [...], "backgrounds": [...]}}}`
and returns `{"images": {"<key>": "<base64 png>"}, "errors": {...}, "request_id": "..."}`.

Row 0 of `values` is the title (rendered with `colspan`), row 1 is the header,
the rest are data rows. Only `values` and `backgrounds` are read — anything
else in the payload is ignored.

A table that fails to render is reported in `errors` while the rest of the
batch still comes back, so one bad table never loses the others.

## Environment variables

| Variable                  | Default | Meaning                                                      |
| ------------------------- | ------- | ------------------------------------------------------------ |
| `API_KEY`                 | unset   | When set, `/generate*` require a matching `X-Api-Key` header. Unset leaves the service open |
| `MAX_CONCURRENT_RENDERS`  | `2`     | Concurrent Chromium renders. Must not exceed gunicorn `--threads` |
| `MAX_TABLES_PER_REQUEST`  | `50`    | Tables accepted in one batch call                             |
| `LOG_LEVEL`               | `INFO`  | Set `DEBUG` to also log browser console output                |

## Deploying

Chromium needs roughly 500MB resident per concurrent render, and the image
keeps one browser per worker thread. The defaults below match `--threads 2` in
the Dockerfile — raising one without the other will get the container
OOM-killed mid-request.

```
gcloud run deploy sheet-screenshot-1 \
  --source . \
  --region <REGION> \
  --memory 2Gi \
  --cpu 2 \
  --concurrency 2 \
  --timeout 300 \
  --set-env-vars API_KEY=<same value as the GCP_API_KEY script property>
```

Then confirm the service came up:

```
curl -s https://<service-url>/healthz
```

## Apps Script setup

Script properties:

- `GCP_URL` — the service URL. Either the base URL or the old `/generate` form
  works; `batchUrl_()` normalises it.
- `GCP_API_KEY` — must match `API_KEY` on the revision. Leave empty if auth is off.

Run `setupDailyMasterTrigger()` once. It creates a midnight trigger that
schedules that day's runs at `:04` past each hour from 08:00 to 22:00 IST.

`testOneScreenshot()` sends a single table to the service and logs the result
without emailing anyone — use it to check connectivity after a deploy.

All hour arithmetic goes through `istInstant_()`, which builds instants from
UTC with a fixed +05:30 offset. It does not depend on the Apps Script project
timezone, so the trigger time and the column mapping cannot drift apart.

## Logs

Everything is written to stdout as single-line JSON, which Cloud Logging parses
into real fields. Each request carries a `request_id`, returned to Apps Script
in the `X-Request-Id` header, so a failure logged there can be traced directly:

```
gcloud logging read \
  'resource.type=cloud_run_revision AND jsonPayload.request_id="<id>"' \
  --limit 50 --format json
```
