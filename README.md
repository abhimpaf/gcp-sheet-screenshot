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
  --concurrency 1 \
  --min-instances 2 \
  --max-instances 10 \
  --cpu-boost \
  --timeout 300 \
  --set-env-vars API_KEY=<same value as the GCP_API_KEY script property>
```

`--concurrency 1` is what makes the Apps Script side fast. It sends 8 requests
at once; with concurrency 1 those land on up to 8 instances rendering in
parallel instead of queueing two at a time on one. `--max-instances` caps the
blast radius and `--cpu-boost` keeps the resulting cold starts out of the
first wave.

Then confirm the service came up:

```
curl -s https://<service-url>/healthz
```

## Files not in this repo

Four working files are gitignored because they carry internal schema, roster
names or email addresses. They are not reconstructible from the repo, so keep
them somewhere shared:

| File | What it is |
| ---- | ---------- |
| `gcode.js` | the Apps Script side; paste into the bound script project |
| `total_ca.sql` | the ClickHouse query feeding the `total_ca` tab |
| `sheet_formulas.xlsx` | paste-ready formulas for the three report tabs, with the `total_ca` column map |
| `Sheet1.xlsx` | an export of the live spreadsheet, for reference |

## Where the data comes from

The three report tabs hold almost no data of their own. Every value is an array
formula over `total_ca`:

- `total_ca` — 8k+ static rows (Batch, Name, Time Slot, Attempts, Valid Calls,
  FEN, REN, FR), rewritten wholesale by a separate ClickHouse push app **every
  10 minutes**, which locks the spreadsheet while it writes. `A1` is its
  "Last updated" stamp; each report tab mirrors it in `A2`.
- `name-email` — a single `IMPORTRANGE` spilling the roster from another
  spreadsheet. Each report tab `QUERY`s it for its own batches.
- `fr_hourly` / `fen_ren_hourly` / `ov_eov_hourly` — ~170 array formulas
  between them, one per hour column, each aggregating `total_ca`.

**This is the critical path for the whole pipeline.** A `getDisplayValues()`
call does not return until pending recalculation has settled, so every rewrite
of `total_ca` puts a multi-minute recalculation between the trigger and the
first screenshot. That is why the script reads each sheet in one block rather
than five calls, and why the read phase has its own budget: see
[Budgets and continuation](#budgets-and-continuation).

If reports are late or empty, check the recalculation cost before looking at
anything in this repo. Two things make it far worse than it needs to be:
whole-column references (`total_ca!$B:$B` scans 1,048,576 rows, not 8,158) and
`MAP`/`LAMBDA` wrappers (which evaluate per row instead of vectorising).

## Apps Script setup

Script properties:

- `GCP_URL` — the service URL. Either the base URL or the old `/generate` form
  works; `batchUrl_()` normalises it.
- `GCP_API_KEY` — must match `API_KEY` on the revision. Leave empty if auth is off.

### Functions you run by hand

Everything else is called by a trigger. Run these from the editor's function
dropdown.

| Function | Use it when |
| -------- | ----------- |
| `triggerRestart()` | Starting the bot, or resuming after a `triggerCleanUp()`. Also the one-time setup. |
| `triggerCleanUp()` | Stopping all reports — maintenance, a broken sheet, a holiday. |
| `runTestNow()` | Checking a change before managers see it. Needs `TEST_MODE = true`. |
| `diagnose()` | An hour failed. First thing to run; sends nothing. |
| `probeRecalc()` | Finding which minutes past the hour the spreadsheet is cheap to read. |
| `showSchedule()` | Seeing what is booked, what completed and what was missed today. |
| `resetToday()` | Clearing today's completion markers so hours can be re-sent. |
| `testOneScreenshot()` | Isolating whether time goes into Sheets reads or the render service. |
| `scheduleExactHourlyRuns()` | Re-booking today's remaining hours without touching the nightly trigger. |

### Settings you change by hand

At the top of `gcode.js`:

| Constant | Default | Meaning |
| -------- | ------- | ------- |
| `TEST_MODE` | `false` | `true` routes every email to `TEST_EMAIL` alone |
| `TEST_EMAIL` | your address | Where test mail goes |
| `TEST_HOUR` | `0` | Hour `runTestNow()` builds; `0` means the current hour |
| `FIRST_HOUR` / `LAST_HOUR` | `8` / `23` | The bounds of the booked day |
| `RUN_MINUTE` | `4` | Minute past the hour each run fires |
| `lastHour` (per sheet) | `23` / `23` / `21` | The last run each sheet takes part in |

### Scheduling

The project timezone **must** be `Asia/Kolkata`. Project Settings → tick
"Show appsscript.json manifest file" → set `"timeZone": "Asia/Kolkata"`.
`triggerRestart()` refuses to run otherwise: the master trigger's `atHour(0)`
is interpreted in the project timezone, so a mismatch can push the nightly
scheduling run past 08:04 IST and silently lose the morning hours.

Then run `triggerRestart()` once from the editor and accept the authorization
prompt. (`setupDailyMasterTrigger()` is kept as an alias for it.)

Each night the master trigger books 16 one-shot triggers at `:04` past 08:00
through 23:00 IST. One-shot `.at()` triggers fire close to the requested
minute; the daily `atHour(0)` one fires anywhere in the 00:00–01:00 window,
which does not matter because it only schedules.

Trigger budget: 16 hourly + 1 master + at most 1 resume = 18, against the
Apps Script cap of 20 per script per user. `diagnose()` prints the live count
and warns from 19.

`scheduleExactHourlyRuns()` is safe to run by hand at any time — it books only
the hours still ahead, so it doubles as the repair if a night is missed.
`triggerRestart()` is the bigger hammer: it rebuilds the nightly trigger too.

Triggers run as whoever created them, so the reports are sent from that
account and count against its MailApp quota.

### How late each sheet runs

`FIRST_HOUR`/`LAST_HOUR` bound the day; each sheet then has its own `lastHour`
and is simply not read past it. An hour is named for the run, so hour 23 is the
23:04 run reporting the 22:00–23:00 timeframe.

| Sheet | Last report | `lastHour` |
| ----- | ----------- | ---------- |
| `fr_hourly` | 22:00–23:00 | 23 |
| `fen_ren_hourly` | 22:00–23:00 | 23 |
| `ov_eov_hourly` | 20:00–21:00 | 21 |

So 08:04–21:04 runs all three, and 22:04 and 23:04 run FR and F&R only.

`LAST_HOUR = 23` is the ceiling the spreadsheet allows, not an arbitrary
choice: the report tabs carry 17 hour blocks and the 23:04 run reads the last
of them (`fr_hourly` columns 53–55, `fen_ren_hourly` 69–72). Going later needs
new columns on the report tabs first — the `total_ca` query already emits the
`H23_*` columns they would read.

### Turning it off and on

| Function | What it does |
| -------- | ------------ |
| `triggerCleanUp()` | Removes **every** trigger and clears the continuation state. Nothing is sent again until a restart. |
| `triggerRestart()` | Books the nightly scheduler plus today's remaining hours, and prints the resulting schedule. |

`triggerRestart()` picks up from wherever in the day you are — run it at 15:00
and you get 16:04 onwards today, then the full `FIRST_HOUR`–`LAST_HOUR` run
from tomorrow. It clears before it books, so running it twice cannot leave
duplicates.

Completion markers survive a cleanup on purpose, so restarting later the same
day does not re-send hours that already went out. Use `resetToday()` when you
do want them re-sent.

### Test mode

`TEST_MODE = true` sends **every** email — scheduled or manual — to
`TEST_EMAIL` alone, drops the CC and prefixes the subject `[TEST]`. Nothing
reaches the routing sheet's managers.

With it on, `runTestNow()` builds the current hour on demand and mails you the
whole report. It leaves no trace: no completion marker, no pending state, no
resume trigger, so the hour's real scheduled run is neither blocked nor
pre-empted. It also ignores an existing completion marker, since re-sending an
hour to yourself is the point of it.

You get one email per recipient group rather than one combined message, which
is what makes it useful — you see exactly what each manager would receive, and
whether the routing split them correctly.

`runTestNow()` **refuses to run unless `TEST_MODE` is true.** Without that
guard it would be one click away from mailing every manager an off-schedule
report.

Failure summaries go to `TEST_EMAIL` too, so a test run cannot page anyone.

Set `TEST_HOUR` to a specific hour (8–23) to rebuild that hour instead of the
current one; `0` means "now". The per-sheet `lastHour` still applies, so
`TEST_HOUR = 22` or `23` gives you FR and F&R only — that is the real report
for those hours, not a fault.

Remember to set `TEST_MODE` back to `false`. While it is on, the scheduled runs
keep mailing you instead of the managers; `triggerRestart()` prints a warning
if you restart with it still set.

### Budgets and continuation

Apps Script kills an execution at 6 minutes, and the reports have to be in
inboxes before :10. Reading the sheets is the expensive half — far more than
the rendering — so the two phases are budgeted separately:

| Clock      | Value                                | Governs                                            |
| ---------- | ------------------------------------ | -------------------------------------------------- |
| `readStop` | `:FETCH_DEADLINE_MINUTE`, capped at `MAX_RUNTIME_MS - FETCH_RESERVE_MS` (4 min) | whether to open another sheet |
| `execStop` | `MAX_RUNTIME_MS` (5 min)             | whether to start another render wave                |

Sheets are read and rendered **one at a time, interleaved** — each sheet's
tables are already rendering while the next sheet is being read. Reading all
of them up front meant one slow sheet cost every other sheet's reports.

A sheet is only opened when the slowest read so far says it will finish before
`readStop`; checking the clock alone is not enough, because a read that starts
one second inside the budget still runs its full length and overshoots into
the kill. A sheet already read is rendered and mailed right up to `execStop` —
late beats never, and the read has been paid for either way.

Whatever is left over — undelivered batches, or sheets never opened — is
picked up by `resumeHourlyScreenshots` ~45s later, for up to `MAX_ROUNDS`
rounds per hour. The delivered keys go in the pending property; the table
payloads themselves go in `CacheService` (chunked, 100KB per value), so the
resume round does not pay the read cost a second time for sheets round 1
already read. The cache is best-effort: a miss just rebuilds from the sheet.
The pending state pins the hour, so a round that crosses the hour boundary
still reads the right column block.

Recipients with batches in more than one round get one email per round, with
the subject suffixed `(part N)`.

When a sheet is never opened at all, it cannot be counted, so
`Screenshots expected` in the failure mail covers only the sheets that were
read. The unread sheets are named on their own line.

### Diagnostics

`diagnose()` is the first thing to run on a failed hour. It sends nothing — no
screenshots, no email — and prints the trigger count against the 20-per-user
cap, today's completion markers, and how long each sheet takes to read. Run it
at `:04` on a failing hour and at `:04` on a working one; the difference in the
read timings is usually the whole answer.

`probeRecalc()` samples read latency every 15s for ~4.5 minutes alongside
`total_ca!A1`, the ClickHouse push's own stamp. Spikes are recalculations.
Use it to find which minutes past the hour are cheap, and put `RUN_MINUTE` in
one of them.

`testOneScreenshot()` sends a single table and logs `sheetReadMs` and `fetchMs`
separately, so you can tell whether the time is going into Sheets reads or into
the render service.

An hour that finishes having delivered nothing is recorded as `FAILED`, not
`completed`, so re-running it actually re-runs. `showSchedule()` shows the
delivered/expected counts per hour.

Every run now logs the read phase in the same detail as the render phase: the
lock wait, the `Email_Routing` read, each sheet's `lastRow`/`lastCol`, and each
of the four range reads individually. A round that overruns says which call
took the minutes. Watch `lastRow` in particular — one stray value far down a
sheet inflates `getLastRow()`, and every read is sized from it, so a read of a
few hundred rows silently becomes tens of thousands.

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
