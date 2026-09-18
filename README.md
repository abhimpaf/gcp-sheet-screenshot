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

### Scheduling

The project timezone **must** be `Asia/Kolkata`. Project Settings → tick
"Show appsscript.json manifest file" → set `"timeZone": "Asia/Kolkata"`.
`setupDailyMasterTrigger()` refuses to run otherwise: the master trigger's
`atHour(0)` is interpreted in the project timezone, so a mismatch can push the
nightly scheduling run past 08:04 IST and silently lose the morning hours.

Then run `setupDailyMasterTrigger()` once from the editor and accept the
authorization prompt. It wipes every existing trigger, creates one daily
trigger for `scheduleExactHourlyRuns`, and books today's remaining runs.

Each night that master trigger books 15 one-shot triggers at `:04` past 08:00
through 22:00 IST. One-shot `.at()` triggers fire close to the requested
minute; the daily `atHour(0)` one fires anywhere in the 00:00–01:00 window,
which does not matter because it only schedules.

Trigger budget: 15 hourly + 1 master + at most 1 resume = 17, against the
Apps Script cap of 20 per script per user.

`scheduleExactHourlyRuns()` is safe to run by hand at any time — it books only
the hours still ahead, so it doubles as the repair if a night is missed.

Triggers run as whoever created them, so the reports are sent from that
account and count against its MailApp quota.

### Continuation

Apps Script kills an execution at 6 minutes. A run stops fetching at
`MAX_RUNTIME_MS`, emails what it has, records the delivered batch keys, and
schedules `resumeHourlyScreenshots` to pick up the rest ~45s later, for up to
`MAX_ROUNDS` rounds per hour.

Only the delivered keys are stored, never the table data — the next round
rebuilds the payloads from the sheet. The pending state pins the hour, so a
round that crosses the hour boundary still reads the right column block.

Recipients with batches in more than one round get one email per round, with
the subject suffixed `(part N)`.

### Diagnostics

`testOneScreenshot()` sends a single table and logs `sheetReadMs` and `fetchMs`
separately, so you can tell whether the time is going into Sheets reads or into
the render service.

`showSchedule()` prints the project timezone, which hours are booked, which
have completed and which were missed.

`resetToday()` clears the day's completion markers and any stuck resume
trigger, keeping the booking record intact.

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
