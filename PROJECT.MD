# PROJECT BRIEF — "Dispatch": single-account spreadsheet mailer

You are building a desktop application. Build the whole thing, working, tested, and
runnable with one command. Do not stop to ask questions — where this brief is silent,
choose the simplest option, write the choice in README.md under "Decisions".

## 1. Objective

A Windows/macOS/Linux desktop app that reads a contact spreadsheet, sends personalised
marketing emails from ONE SMTP account at a throttled rate, records the send date back
into the spreadsheet, and detects replies and bounces via IMAP.

Single sender account: sam@samlax.net. Single operator. Single list at a time.

## 2. Non-negotiable constraints

- DRY RUN IS THE DEFAULT. No real email may be sent until the operator both sets
  `safety.dry_run: false` in config AND clicks a confirm dialog. Developing/testing
  must never touch a real inbox.
- Exactly one process may write the spreadsheet. One coordinator, one sender worker.
  Design the sender as a worker class with an id so N workers can be added later by
  config only — but ship with N=1.
- SQLite is the source of truth during a run. The spreadsheet is imported at start and
  written back on flush/stop. Never send directly from a live spreadsheet read.
- Never send twice to the same address in one run. Never resend a row whose last state
  is `sending` or `unknown` — surface it for human review instead.
- Never crash the UI thread. All sending happens off-thread; UI receives events.

## 3. Stack

Python 3.11+. PySide6 (QMainWindow + QAbstractTableModel), openpyxl, aiosmtplib,
aiosmtpd (dev only), aiosqlite or sqlite3, Pillow, pydantic v2, keyring, python-dotenv.
Package with PyInstaller. Tests with pytest + pytest-qt.

Layout:
```
dispatch/
  __main__.py            # GUI entry
  cli.py                 # headless entry: dispatch validate|send|export
  config.py              # pydantic models + loader
  db.py                  # schema, migrations, DAO
  sheet/
    reader.py            # xlsx -> contacts, tolerant parsing
    writer.py            # atomic write-back, preserves unknown columns
  engine/
    eligibility.py       # pure functions, heavily unit-tested
    queue.py             # work queue, claim/commit
    worker.py            # sender worker: timer, caps, backoff
    state.py             # state machine enum + transitions
  mail/
    compose.py           # templating -> EmailMessage
    sender.py            # SMTP transport, one account
    monitor.py           # IMAP reply + bounce detection
  ui/
    main_window.py, preflight.py, template_editor.py, settings.py, models.py
  templates/promo.html, templates/promo.txt
  tests/  fixtures/
```

## 4. SQLite schema

```sql
CREATE TABLE campaign_runs (
  id INTEGER PRIMARY KEY, started_at TEXT, ended_at TEXT,
  quota INTEGER, sent_count INTEGER DEFAULT 0,
  failed_count INTEGER DEFAULT 0, status TEXT, stop_reason TEXT);

CREATE TABLE contacts (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL,
  email_norm TEXT NOT NULL,          -- lower(trim(email))
  first_name TEXT, last_name TEXT, company TEXT,
  last_advertised TEXT,              -- ISO YYYY-MM-DD, from sheet
  replied INTEGER DEFAULT 0,
  autoreply_only INTEGER DEFAULT 0,
  ignored INTEGER DEFAULT 0,
  sheet_row INTEGER,                 -- 1-based, for "go to row in Excel"
  extra_json TEXT);                  -- ALL other sheet columns, verbatim

CREATE TABLE sends (
  id INTEGER PRIMARY KEY,
  run_id INTEGER REFERENCES campaign_runs(id),
  contact_id INTEGER REFERENCES contacts(id),
  account_id TEXT, message_id TEXT NOT NULL,
  state TEXT NOT NULL,               -- queued|sending|sent|failed_soft|failed_hard|unknown
  attempt_at TEXT, sent_at TEXT, smtp_code TEXT, error TEXT);

CREATE TABLE suppression (
  email_norm TEXT PRIMARY KEY, reason TEXT, added_at TEXT, source TEXT);

CREATE TABLE inbound_events (
  id INTEGER PRIMARY KEY, account_id TEXT, kind TEXT,
  matched_send_id INTEGER, from_addr TEXT, subject TEXT, detected_at TEXT);
```
Indexes on contacts.email_norm, sends.message_id, sends.state.

## 5. Config (`campaign.yaml`, pydantic-validated)

```yaml
campaign:
  name: "October promo"
  sheet_path: "./leads.xlsx"
  sheet_tab: null            # null = active sheet
  date_order: "DMY"          # DMY | MDY | ISO
  quota: 200                 # sends this run
  frequency_days: 30         # eligible when gap >= this
  shuffle_order: true
  send_window:
    enabled: true
    start: "09:00"
    end: "18:00"
    days: [Mon,Tue,Wed,Thu,Fri]
    timezone: "Europe/London"

throttle:
  mode: "random"             # fixed | random
  fixed_seconds: 120
  random_seconds: [60, 260]  # mean 160s => 200 sends ~ 8h53m, fits 9h window
  backoff_seconds: [300, 900, 1800, 3600]

limits:
  daily_cap: 200             # per account, resets local midnight
  hourly_cap: 30             # keep bursts impossible

safety:
  dry_run: true
  require_typed_confirmation_over: 50
  stop_on_failure_rate: 0.10

warmup:
  enabled: true
  ramp: [30, 60, 100, 150, 200]   # per week, index = weeks since first_send
  first_send_date: null            # set automatically on first real send

sender:
  id: "sam"
  from_name: "Sam at Samlax"
  from_email: "sam@samlax.net"
  reply_to: "sam@samlax.net"
  smtp: { host: "smtp.PROVIDER", port: 587, starttls: true, timeout: 30 }
  imap: { host: "imap.PROVIDER", port: 993, ssl: true, mailbox: "INBOX" }
  secret_ref: "keyring:dispatch/sam"   # password lives in OS keyring, never here

template:
  subject: "Quick question, {FirstName|default:\"there\"}"
  html_path: "./templates/promo.html"
  text_path: "./templates/promo.txt"
  image_url: "https://samlax.net/img/hero.jpg"   # HOSTED, not embedded
  footer:
    postal_address: "Samlax, <street>, <city>, <postcode>"
    unsubscribe_url: "https://samlax.net/u/{token}"
    unsubscribe_mailto: "unsubscribe@samlax.net"
```

## 6. Spreadsheet contract

Header row required. Match headers case-insensitively, ignoring spaces/underscores.
Column order must not matter. Preserve every unlisted column and all cell formatting.

- `FirstName`, `LastName` — optional
- `Email` — required
- `LastAdvertised` — PROGRAM WRITES (ISO date). Never write earlier than existing value.
- `Replied` — program may set TRUE, never FALSE (one-way flag)
- `Ignore` — read-only to program
- `LastContacted` — read-only to program, semantically distinct from advertised
- optional `Status`, `LastError`, `Attempts` written by program if the columns exist

Parsing requirements: openpyxl with `data_only=True`; handle real datetimes, Excel
serial numbers, and text dates; trim whitespace; skip fully blank rows; record
`sheet_row` for every contact. Ambiguous `03/04/2026` under `date_order` must be
reported in pre-flight, not silently guessed.

Writing: refuse if Excel lockfile `~$<name>.xlsx` exists. Write to temp file in the
same directory, `os.replace()` onto the original. Flush on: every 10 sends, on pause,
on stop, on exit. Never flush while a read is in progress.

## 7. Eligibility (implement as pure functions + unit tests)

Evaluate in order; first match wins:
1. `Ignore` truthy → `skipped:ignored`
2. email_norm in suppression → `skipped:suppressed`
3. `Replied` truthy → `skipped:replied`
4. email invalid, or duplicate of an earlier row → `skipped:invalid` / `skipped:duplicate`
5. `LastAdvertised` empty → ELIGIBLE
6. `last_advertised + frequency_days <= today` (i.e. gap >= frequency) → ELIGIBLE
7. else → `skipped:too_soon` (include days remaining)

Truthy = y|yes|true|1|x (case-insensitive). Blank = falsy.
Invalid email = fails a sane RFC5322-lite regex OR has no dot in the domain.

## 8. Send loop

```
on Start:
  import sheet -> sqlite; run pre-flight; show summary; on confirm:
  build eligible list; if shuffle: random.shuffle
  loop while running and sent < quota and queue not empty:
      if paused: wait
      if outside send_window: sleep until window opens (show countdown)
      if daily_cap or hourly_cap reached: stop with reason
      row = queue.claim()            # atomic; sets state=sending, attempt_at=now
      sleep until next_allowed_at      # set from previous send + interval
      msg = compose(row)               # may raise PlaceholderError -> failed_hard, no send
      if dry_run: write .eml to ./outbox/ and mark sent(dry)
      else: smtp.send(msg); mark sent; last_advertised = today
      next_allowed_at = now + interval()
```
Per-message: create `Message-ID: <uuid4@samlax.net>`, store it in `sends.message_id`.

Timer: `interval()` returns uniform(min,max) in random mode. Show a live countdown.
Interval is measured send-to-send, and a failed send does NOT reset the timer.

Error handling (SMTP):
- 5xx recipient rejected → `failed_hard`, add to suppression as `hard_bounce`
- 4xx / timeout / disconnect → `failed_soft`, keep row pending, exponential backoff
- `421 4.7.28` or text "quota"/"rate limit" → suspend THIS account for 10 min,
  continue with concurrency 1 afterwards, log loudly
- auth failure (535) → stop the run immediately, tell the user to re-auth

## 9. Feasibility + warm-up checks (do not skip)

Before every run, compute `expected_seconds = quota * mean_interval` and compare to
remaining window. If it does not fit, block the run and state the arithmetic, e.g.:
"This quota needs ~8h53m but only 5h10m of your send window remains today.
Reduce quota to 116, extend the window to 21:00, or set the timer to 60-140s."
Same panel shows: today's send count, remaining daily allowance, warm-up ceiling for
this week (if enabled, the effective cap is `min(daily_cap, warmup_today)`).

# ADDENDUM — replace Section 10 with Sections 10 and 10A

## 10. Templating (engine side — unchanged)

The send engine consumes rendered artifacts only: `template.html_path`, `template.text_path`,
the `template.subject` string, and `template.image_url`. It knows nothing about the
template editor. Templates may therefore be authored three ways, all equivalent:
by hand on disk, via "Raw HTML" mode, or via the visual Block builder below.

Placeholders: any spreadsheet column by name, plus `{Email}`, `{UnsubscribeURL}`,
`{UnsubscribeToken}`, `{PostalAddress}`. Filters: `|default:"text"`, `|upper`, `|title`,
`|trim`. HTML-escape every substituted value. Strip `\r\n` from header-bound values.

The **footer is generated by the program, not the template** — a fixed block appended at
render time containing the postal address and a visible unsubscribe link, plus the
`List-Unsubscribe` / `List-Unsubscribe-Post` headers. The operator cannot remove it.
This guarantees every outbound message is compliant.

## 10A. Template Builder (new module: `dispatch/ui/template_builder.py`)

A deliberately simple, block-based editor. Not a drag-and-drop designer, not a
WYSIWYG canvas. Scope it exactly as below; do not invent extra features.

### Two template types

`template.kind: "blocks" | "raw"` in config.

- **blocks** — edited in the builder. Source of truth is `templates/<name>.json`.
  At render/save time the app generates `<name>.html` and `<name>.txt` next to it.
- **raw** — a hand-written `.html`/`.txt` pair. The builder opens it read-only with a
  banner: "Raw HTML template — switch to Blocks to edit visually." Do not attempt to
  parse arbitrary HTML back into blocks.

If `template.kind` is absent, infer it: `.json` exists → blocks, otherwise raw.
Saving either type rewrites the `html_path`/`text_path` keys in `campaign.yaml` so the
engine always finds the current artifacts.

### Block document schema (`templates/<name>.json`)

```json
{
  "version": 1,
  "name": "October promo",
  "subject": "Quick question, {FirstName|default:\"there\"}",
  "preheader": "A one-line preview text shown in the inbox",
  "max_width_px": 600,
  "blocks": [
    {"type": "heading",   "text": "A quick idea for {Company}", "level": 2},
    {"type": "paragraph", "text": "Hi {FirstName|default:\"there\"}, I help ..."},
    {"type": "image",     "url": "https://samlax.net/img/hero.jpg",
                          "alt": "Our October range", "width": 600,
                          "link": null, "align": "center"},
    {"type": "button",    "text": "See the range", "url": "https://samlax.net/october"},
    {"type": "divider"},
    {"type": "spacer",    "height": 24},
    {"type": "paragraph", "text": "— Sam, Samlax"}
  ]
}
```

Paragraph and heading text support a tiny markdown subset, converted at render time:
`**bold**`, `*italic*`, `[label](https://url)`. Nothing else. Escape all other input.

### Builder UI

Left: vertical list of blocks with ▲ ▼ delete buttons and an "Add block" menu
(heading, paragraph, image, button, divider, spacer). Selecting a block shows its
fields on the right (a heading has text + level; an image has URL, alt, width, optional
link, alignment; a button has label + URL).

Top bar: template name, **Subject** field, **Preheader** field.

Placeholder insertion: a dropdown listing every column actually present in the currently
loaded spreadsheet, plus the built-ins. Clicking inserts `{FirstName}` at the cursor of
the focused text field, so the user never types braces by hand.

Right: live preview in a `QTextBrowser` with a sample-data selector — "Sample data"
(John Smith / john@example.com / Acme Ltd) or any real row from the imported sheet.
Because QTextBrowser supports only a subset of HTML/CSS it may look slightly different
from a real inbox: add a **"Open exact render in browser"** button that writes
`%TEMP%/dispatch-preview.html` and calls `webbrowser.open()`. Label the panel honestly
("Approximate preview").

Bottom: **Send test to sam@samlax.net** — renders with the chosen sample row, sends via
the real account (never dry-run-suppressed), and reports the SMTP result. Plus **Validate**,
**Save**, **Save as…**, **Duplicate**, **Delete**.

Validation (blocks the Save and the Send-test if failing):
- Subject non-empty, ≤ 78 chars (warn over 60)
- At least one paragraph block with real words (image-only emails are a spam signal)
- No unresolved `{Placeholder}` that isn't a real column
- No rendered greeting that reads "Hi ," or "Hi there ,"
- Every URL starts with `https://`
- Image URL present, and the "Check image" button (a HEAD request) returns 200 +
  an `image/*` content type
- Warn if total generated HTML exceeds 100 KB

### Starter templates (New → choose one)

1. **Personal note** (default) — no image, plain paragraphs, signed by hand. Best
   performer for cold outreach.
2. **Promo card** — heading, hero image, three short paragraphs, one button.
3. **Announcement** — heading, image, two paragraphs, divider, signature block.

Each ships as a `.json` in `dispatch/templates/starters/` and is copied into the user's
`templates/` folder on selection.

### Email-safe HTML generation (this is where most implementations fail)

The generator must emit a single-column table-based layout with **inline styles only**:

- Outer `<table role="presentation" width="100%">` → centred inner table of
  `max_width_px`, cell padding via `<td>` not CSS margins.
- No flexbox, no grid, no `position`, no `<style>` blocks, no external stylesheets,
  no JavaScript, no web fonts, no CSS variables.
- Font stack: `-apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif`.
- Button = a table cell with `bgcolor` + padding + an `<a>` with `display:inline-block`
  and inline padding ("bulletproof button"), not a styled `<div>`.
- Every `<img>` has `alt`, explicit `width`, `border="0"`, `display:block`, and
  `style="max-width:100%;height:auto"`.
- Body text at least 14px, line-height 1.5, colour not pure black/grey-on-grey (dark
  mode), generous padding — plain, human-looking emails deliver better than designed ones.
- Plain-text version auto-derived: strip tags, convert `<a>` to `label (url)`, keep
  paragraph breaks, wrap at 72 chars. Store it in `<name>.txt`.

### CLI parity

`dispatch template new|preview|validate --config campaign.yaml` must expose the same
three operations headlessly, `preview` writing the rendered HTML + TXT to `./outbox/`.
## 11. IMAP monitor

Poll INBOX every 5 minutes (and on Start/Stop) per account; track last seen UID in DB.

- Reply: match `In-Reply-To` / `References` against `sends.message_id`. If no header
  match, fall back to matching the From address against `contacts.email_norm`.
  On match: `contacts.replied = 1`, `sends` untouched, log an `inbound_events` row.
  Send to that contact stops immediately (it is now ineligible).
- Auto-reply detection: `Auto-Submitted: auto-replied`, `X-Autoreply`,
  `Precedence: auto_reply`, or subject matching out-of-office patterns → set
  `autoreply_only = 1` and do NOT treat as a human reply. Configurable.
- Bounce: message with `Content-Type: multipart/report`, or From containing
  postmaster/mailer-daemon, or an `X-Failed-Recipients` header. Parse the
  `message/delivery-status` part: `Final-Recipient` and `Status`. `5.x.x` →
  suppression(hard_bounce) + `failed_hard`. `4.x.x` → ignore.
- Opt-out keywords in a human reply ("unsubscribe", "remove me", "stop", "opt out",
  "take me off") → create a `needs_review` entry shown in a GUI queue. Do NOT
  auto-suppress by default; the operator approves with one click.

Never mark a row replied because of a mailing-list or noreply sender. Log everything
but store only headers + a 200-char snippet, not full bodies.

## 12. GUI

Main window, 4 regions:

TOP BAR: campaign name · status pill (Idle | Dry run | Running | Paused | Backing off |
Stopped | Stop reason) · big counter "137 / 200 (68%)" · ETA · elapsed ·
PAUSE / STOP buttons · prominent amber "DRY RUN" banner when applicable.

MAIN TABLE (QAbstractTableModel, virtualised, sortable):
`# | Sheet row | Name | Email | Last advertised | Eligible | Status | Sent at | Error`
- Row highlight for the currently-sending contact.
- Status string set is fixed: pending, queued, sending, sent, skipped:<reason>,
  failed_soft, failed_hard, unknown.
- Filter chips: All / Eligible / Sent / Skipped / Failed / Replied.

RIGHT PANEL (account card): from address · "Next send in 2:41" live countdown ·
`Today 137/200 · This hour 12/30 · Warm-up ceiling 200` · pacing line
("On pace — 63 remaining, 4h12m left in window") · health (OK / Backing off until
14:32 / Auth error) · IMAP status ("checked 2 min ago · 4 replies · 2 bounces") ·
Re-check inbox button.

BOTTOM: filterable log with timestamp, level, message; export CSV; PII rule — log
email domain only, never full body text.

Dialogs: Pre-flight report (counts by reason, invalid emails, ambiguous dates, duplicate
emails, projected duration, warm-up-limited cap, feasibility verdict, and a
"re-check inbox first" button). Confirmation dialog requiring typed "SEND 200" when
quota > 50. Template editor with sample-row picker and live HTML + plain-text preview,
plus a "send test to myself" button that renders and sends to sam@samlax.net only.
Post-run report: sent/skipped/failed by reason, per-hour breakdown, "write results to
sheet" with a diff preview, export CSV.

Keyboard: Space = pause, Ctrl+Enter = start. Persist window geometry. Single-instance
lock with a clear message. Closing the window while running asks pause-or-stop.

## 13. CLI (all GUI actions must also work headless)

`dispatch validate --config campaign.yaml` → prints the pre-flight report, exits 0/1.
`dispatch send --config campaign.yaml [--dry-run] [--quota N]` → runs, prints progress.
`dispatch export --config campaign.yaml --out results.csv`
`dispatch sync-inbox --config campaign.yaml` → runs the IMAP pass once.

## 14. Security

Password from OS keyring only (first run: prompt and store). Redact secrets in logs and
tracebacks. Strip CR/LF from header-bound values. Validate `image_url` starts with
`https://`. Sanitise template file paths (no `..` escapes). Never write the password to
the spreadsheet, the DB, or the .eml files in ./outbox/.

## 15. Tests (all must pass; none may hit the network)

Use `aiosmtpd` on localhost for a fake SMTP server, and `imaplib`-compatible fixtures
for the monitor (parse saved .eml files rather than connecting).

1. Eligibility matrix: blank date, gap 29d, gap 30d, gap 45d, ignore=T, replied=Y,
   suppressed, invalid email, duplicate → expected verdict for each.
2. Quota: 500 eligible, quota 200 → exactly 200 sends against the fake server.
3. No duplicates: shuffled run over 200 rows → set(sent recipients).len == 200.
4. Crash safety: kill between `state=sending` and `sent`; restart → row is `unknown`,
   is not resent, and appears in the report as needing review.
5. Header injection: `firstName = "Bob\nBcc: evil@x.com"` → rejected at validation.
6. Empty name → renders the default, never a literal `{FirstName}`.
7. Sheet round-trip: after flush, file opens in openpyxl, unknown columns and
   formatting intact, `LastAdvertised` updated only on sent rows, and never moves
   backwards.
8. Date parsing: `03/04/2026` under DMY vs MDY vs ambiguous → correct or flagged.
9. Feasibility: quota 200, mean 180s, window 5h → run is blocked with the arithmetic.
10. Bounce fixture (Gmail-style 550 DSN) → contact suppressed, `failed_hard`.
11. Reply fixture with `In-Reply-To` → contact `replied=1`, excluded from next run.
12. Rate-limit fixture (`421 4.7.28`) → account suspended ≥10 min, run paused not failed.

## 16. Definition of done

- `pip install -e . && python -m dispatch` opens the GUI with no config and walks the
  user through: choose sheet → choose provider → enter password → pick template.
- `pytest` green, coverage of `engine/` and `sheet/` above 80%.
- README.md: install, config reference, first-run checklist, "what to do when Gmail
  says 550/421", and the Decisions section.
- A `sample_leads.xlsx` fixture with 12 rows covering every skip reason.

## 17. Out of scope — do not build

Multi-account orchestration (leave the seam), OAuth2, click/open tracking, web UI,
scheduling/daemon mode, list scraping or enrichment, attachments other than the
hosted hero image, HTML email designer.
