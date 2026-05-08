# Pre Call Agent

Publisher call prep, automated. Built for Joveo Supply.

A partnerships rep walks into a call with one of Joveo's supply publishers (Indeed, LinkedIn, ZipRecruiter, Talent.com, etc.) every few hours. Each call needs the same homework: what's new about the partner this week, what was promised on the last call, how their campaigns are pacing, and what we should ask for or push back on. Doing that prep manually takes 30–45 minutes per call and gets skipped under load.

This agent does it in ~30 seconds. It pulls the day's calendar, matches each event against Joveo's curated publisher list, gathers four signals per partner (news, past-call transcripts, campaign performance, internal notes), synthesizes a deck-ready brief, and waits for a human to approve before posting to Slack or rendering as a printable deck.

Everything is reversible — drafts are editable, nothing ships without explicit approval, and the underlying data lives in a Google Sheet the rep already controls.

## What the agent does

End-to-end, for every partner call on the rep's calendar today:

1. **Read the calendar.** The rep signs in with Google OAuth. The app reads that user's primary Google Calendar via the Calendar API (cached in-memory for 5 minutes per user). Calendar matching is **hybrid** — first a pure-Python pass against the curated publisher list ([app/publishers.py](app/publishers.py)) using attendee-domain + title substring rules. Anything not matched but still external (i.e. has a non-`@joveo.com` attendee) gets escalated to Gemini for inferred matching as `tier="unlisted"`. Obviously-internal events are dropped before consulting Gemini at all — saves quota for free. Each match comes back with `match_reason` and `match_evidence` (a verbatim quote).

   There's also a **manual brief-any-publisher dropdown** on the dashboard for ad-hoc prep when nothing's on the calendar — it just opens the same workbench with the publisher you pick.

2. **Gather four signals per partner.**
   - **News** — Gemini with native Google Search grounding, prompted to prioritize product launches → pricing changes → funding/M&A → leadership → layoffs → competitive moves. Returns up to 5 items with source tier, recency, and a "why it matters for a Joveo conversation" detail line.
   - **Past-call transcripts** — reps upload up to 3 `.txt` transcripts per partner (downloaded from Avoma, Gong, Otter, or anywhere). Stored in the `transcripts` worksheet; the agent reads the most recent 3 at briefing time and synthesizes them into `last_call_summary`, `commitments`, `open_threads`, `sentiment`, and `key_quotes`. Auto-prunes the oldest when more than 3 are uploaded; truncates content over 45K chars.
   - **Performance** — Mojo "All Clients - Client Report" CSVs uploaded via the Performance tab. Per-client rows with currency auto-detected from the Revenue column ($/£/€/CHF/₹/¥). Multiple uploads per partner are supported (one per currency); all are passed to the synthesis prompt. The CSV parser tolerates leading blank rows and the trailing "Summary" section in Mojo's exports.
   - **Notes** — internal account notes / QBR snippets / contract terms manually entered by the rep through the Notes tab.

3. **Synthesize.** All four signals plus the calendar event are handed to Gemini with the [SYNTHESIZE_SYSTEM](app/prompts.py) prompt in JSON mode. Output is a strict JSON brief: account snapshot, performance read, 3–5 talking points (each tied to a real signal), 2–4 specific recommendations (each with rationale, expected impact, and risk), open threads to close, and risks not to walk in blind on.

   **Performance is shown one block per uploaded CSV** — multi-currency partners (CHF / USD / GBP files for the same publisher) get one metric grid each rather than a confusing single sum.

   **Gather steps run in parallel** via `asyncio.gather` — the four I/O tasks (news, transcripts, performance, notes) overlap, dropping briefing time from ~30s sequential to ~10–15s.

4. **Human review.** The brief lands in the workbench UI as an editable draft. The rep edits any field, approves, then ships via:
   - **📊 Open deck** — opens the live HTML deck in a new tab (browser-renderable, scrollable, can be Print→Save-as-PDF'd via the browser)
   - **📄 Download PDF** — dual-mode: tries server-side Playwright render first (highest fidelity, requires `playwright install chromium` locally), falls back to opening the deck with `?print=1` so the browser's Print → Save as PDF dialog auto-fires (works on Vercel and anywhere else without Chromium installed). Saves as `{Publisher} {DD-MM-YYYY}.pdf`.
   - **💬 Send to Slack** — formatted summary to a webhook channel
   - **📧 Email** — server-side send via the Gmail API to the logged-in user's inbox. No app-launching, no truncation: the full brief renders as a styled HTML email (single-column, ~640px wide, inline CSS — works in Gmail/Outlook/Apple Mail). Requires the `gmail.send` OAuth scope (granted at sign-in) and the **Gmail API** enabled in the Google Cloud project.
   - **✏️ Edit this briefing** — visible only when viewing an already-approved briefing. Unlocks the Approve button (relabeled **✓ Re-approve**) so subsequent edits **update the same briefing record in place**. The audit trail keeps the latest state; `approved_at` is refreshed on each re-approve.
   - **📝 Save as new draft** — clones the current briefing into a **new draft** with your edits baked into the new `output`. The original briefing is untouched. Useful for forking an approved brief without losing the gold copy. The `inputs_json` of the new record records `cloned_from_briefing_id` so lineage is traceable.
   - **👁 View original (pre-edit)** — toggle between the edited brief and the untouched first draft, useful for reviewers
   Nothing leaves the system without `status == "approved"`.

5. **Persist + audit.** Inputs (including who generated the briefing, taken from the OAuth session), output, edits, approval timestamp, and send timestamp all land in the `briefings` worksheet. Every action — create, edit, approve, re-approve, clone, send, upload, delete — also logs to the `audit_log` worksheet with actor email + name. The **Past briefings** section on the dashboard:

   - Shows briefings newest-first, **5 per page** with a "← Newer / Older →" pager.
   - Each row's content area is **clickable** — opens the briefing back into the workbench with all edits restored, so you can review/edit/approve/re-approve later without regenerating.
   - Per-row action buttons: **📊 Deck**, **📄 PDF**, **📧 Email**, plus **✏️ Edit** on approved rows (opens the briefing in the workbench for in-place edits and re-approval).

## Detailed flow

```
                Browser (public/index.html + app.js)
                                │
   On page load: POST /api/uploads/clear  →  wipe performance + transcripts (fresh-slate behaviour)
                 GET  /api/health
                 GET  /api/auth/me
                 GET  /api/calendar/today  →  cached 5 min/user
                 GET  /api/briefings       →  history list
                                │
                 User clicks a partner card → workbench opens (4 tabs)
                 User uploads CSVs / transcripts / notes (optional)
                 User clicks "Generate briefing"
                                ▼
                 POST /api/briefing/run { partner, call }
                                │
                       FastAPI (api/index.py)
                                │
   ┌────────────────────────────┼────────────────────────────┐
   ▼                            ▼                            ▼
fetch_partner_calls       run_briefing                  render_deck /
(Calendar API +           (briefing.py)                 build_slack_message
 Gemini matcher,
 5-min in-memory cache)
                                │
       ┌────────────────────────┼────────────────────────┐
       ▼            ▼                  ▼                 ▼
  _gather_news  _gather_avoma     _gather_perf      _gather_notes
  (Gemini +     (Sheets:          (Sheets:          (Sheets:
   Google        transcripts       performance       notes)
   Search)       → Gemini synth)   — all uploads
                                    inc. multi-
                                    currency)
                                │
                                ▼
                  Final synthesis (Gemini, JSON mode)
                                │
                                ▼
                    storage.insert_briefing
                  (Sheets: briefings worksheet,
                   with generated_by from session)
                                │
                                ▼
                  Edit → approve → ship (deck or Slack)
```

**Three Gemini calls fire per briefing**, all running concurrently via `asyncio.gather` (~10–15s end-to-end vs ~30s sequential):
1. News pull (Google Search grounding) — last 90 days, max 5 items
2. Transcript synthesis (JSON mode, 4000 max_tokens) — up to 3 uploaded .txt files → commitments / threads / sentiment
3. Final synthesis (JSON mode, 3500 max_tokens) — all signals → deck-ready brief

Plus a fourth Gemini call for calendar matching, but only for events that don't match the curated publisher list via the Python pre-pass — and the result is cached in-memory for 5 minutes per user-date. Most calendar fetches hit the cache and never call Gemini at all.

The default model is `gemini-2.5-flash-lite` (cheapest tier). Switch with `GEMINI_MODEL` in `.env`. Recommended for production: `gemini-2.5-flash` for the synthesis call (regular, not Lite — better talking-point quality).

### Resilience

- **Gemini 429 handling**: a custom `GeminiRateLimited` exception is raised when the API returns RESOURCE_EXHAUSTED. FastAPI returns a clean 429 with `Retry-After`, and the frontend shows a toast banner ("Gemini rate-limited. Retry in ~45s") instead of a stack trace.
- **JSON parse fallback**: if Gemini returns malformed JSON (Flash-Lite occasionally truncates), the parser tries to recover by extracting the largest `{...}` or `[...]` block from the response.
- **List coercion**: the deck and frontend tolerate Gemini emitting strings where arrays are expected — single-item arrays render fine, no character-by-character explosions.
- **Soft failures on Sheets writes**: audit-log writes use try/except so a Sheets quota hiccup never blocks the user-facing action.
- **Hybrid calendar matching**: the Python pass works even when Gemini is rate-limited. Worst case: known publishers still surface, ambiguous external events are simply omitted that session.

## Stack

- **FastAPI** (Python 3.12) — serverless on Vercel via [api/index.py](api/index.py)
- **Google Gemini** — synthesis, Google Search grounding, calendar matching
- **Google Calendar API** — per-user OAuth calendar fetch
- **Google Sheets** — persistence for notes, performance uploads, transcripts, briefings, and OAuth tokens (all under one sheet, one service account)
- **Slack incoming webhook** — final delivery (optional)
- **Static frontend** — vanilla HTML/CSS/JS in [public/](public/), no build step

## Branding

The deck is styled to match [joveo.com](https://www.joveo.com):

- **Fonts**: Poppins (display / headings) + Lato (body) + DM Mono (kickers, footers, monospace tags) — all loaded from Google Fonts at render time.
- **Colors**: deep indigo `#202058` (section headings, cover gradient, body text), accent purple `#5454BF` (bullet arrows, kickers, card borders, metric edges), neutral surfaces `#fafafe`/`#f6f6fa` for cards and tables.
- **Slide size**: A4 landscape (297mm × 210mm) — matches the live HTML deck and the downloaded PDF.
- **Page headings** are intentionally client-safe (Account Overview, Performance Summary, Recent Highlights, Previous Discussion, Discussion Points, Strategic Recommendations, Key Considerations) so the deck can be screen-shared on the call without internal-jargon awkwardness.

## Layout

```
.
├── api/
│   └── index.py            # FastAPI app — Vercel entry, all /api/* routes
├── app/
│   ├── auth.py             # Google OAuth (login, callback, signed cookies)
│   ├── briefing.py         # gather → synthesize orchestrator
│   ├── config.py           # pydantic-settings env loader
│   ├── prompts.py          # NEWS_SYSTEM, SYNTHESIZE_SYSTEM, AVOMA_SYNTH_SYSTEM, calendar_system
│   ├── publishers.py       # Curated 139-publisher list (P0/P1/P2)
│   ├── services.py         # Gemini, Calendar API, CSV parsing, currency detection,
│   │                       # transcript synthesis, Slack, deck rendering
│   └── storage.py          # Google Sheets persistence (6 worksheets, all CRUD)
├── public/
│   ├── index.html          # frontend SPA
│   ├── styles.css
│   └── app.js
├── docs/
│   ├── showcase.html       # printable showcase (Ctrl+P → Save as PDF)
│   └── make_pdf.py         # one-command PDF export (Playwright)
├── brief.py                # local CLI for one-off briefings
├── requirements.txt
├── vercel.json             # rewrites /api/* → api/index.py
├── .env.example
└── .python-version
```

## Worksheet schemas

The Sheet auto-creates these tabs on first write (don't pre-create them):

| Tab | Columns |
|---|---|
| `notes` | id, partner, kind, title, body, created_at |
| `performance` | id, partner, filename, period_start, period_end, summary_json, created_at |
| `transcripts` | id, partner, filename, call_date, call_title, content, truncated, created_at |
| `briefings` | id, partner, call_time, call_title, status, inputs_json, output_json, edits_json, approved_at, sent_at, created_at |
| `oauth_tokens` | email, name, picture, token_json, updated_at, created_at |
| `audit_log` | id, actor_email, actor_name, action, target_type, target_id, details_json, created_at |

## Setup

### 1. Install Python 3.12+

Windows: download from [python.org](https://python.org). Verify with `python --version`.

### 2. Install dependencies

```
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Mac/Linux
pip install -r requirements.txt
playwright install chromium     # one-time, ~200MB — needed for the "Download PDF" button
```

The `playwright install chromium` step is only required if you want the **📄 Download PDF** action to work; the rest of the app runs fine without it (the button returns a 501 with a helpful message if Chromium isn't installed, and you can always Print → Save as PDF from the **📊 Open deck** view as a fallback).

### 3. Create the Google Cloud project + service account

The service account is used for Sheets storage only.

1. Create a project at [console.cloud.google.com](https://console.cloud.google.com).
2. Enable the **Google Sheets API**.
3. Create a **service account**, generate a JSON key, download it.
4. Open the JSON, copy the entire contents — you'll paste this as a single line into `GOOGLE_CREDENTIALS_JSON`.

### 4. Create the Google Sheet

1. Create a new Google Sheet.
2. Copy its ID from the URL (`docs.google.com/spreadsheets/d/<THIS_ID>/edit`).
3. Share the sheet with the service account email (Editor access).
4. The five worksheets are auto-created on first write with the schemas in [app/storage.py](app/storage.py).

### 5. Create Google OAuth credentials for user calendars + Gmail send

Calendar and email access are per-user (every rep signs in with their own Google account):

1. In Google Cloud, configure the **OAuth consent screen** (Internal audience for Joveo-only access).
2. Add these scopes:
   - `openid`, `userinfo.email`, `userinfo.profile` (identity)
   - `https://www.googleapis.com/auth/calendar.readonly` (read calendar)
   - `https://www.googleapis.com/auth/gmail.send` (send the **📧 Email** brief on behalf of the rep)
3. **Credentials → Create credentials → OAuth client ID** → **Web application**.
4. Add Authorized redirect URIs:
   - Local: `http://localhost:3000/api/auth/google/callback`
   - Vercel: `https://your-app.vercel.app/api/auth/google/callback`
5. Copy the client ID and client secret into `.env`.

> **If you previously signed in before the `gmail.send` scope was added**, sign out and back in once — Google won't grant a new scope on a refresh token. Until then, the **📧 Email** button returns 403 with a clear "Re-sign-in" prompt.

### 5b. Enable the APIs you'll call

In **Google Cloud → APIs & Services → Library**, enable these (click each, hit **Enable**):

- **Google Sheets API** (storage)
- **Google Calendar API** (per-user calendar fetch)
- **Gmail API** (server-side email send)

These are per-project toggles. Without them, calls fail with 403 "API has not been used in project ... before or it is disabled" — that error always means "go enable the API in the Cloud console."

### 6. Get a Gemini API key

Create one at [aistudio.google.com/apikey](https://aistudio.google.com/apikey).

⚠️ The free tier caps `gemini-2.5-flash-lite` at **20 requests per day per project**. A single test session can exhaust it. **Enable billing on the Google Cloud project** before any real use — paid tier removes the cap and costs fractions of a cent per briefing.

### 7. Set up Slack delivery (optional)

Create an incoming webhook in your Slack workspace targeting the channel you want briefings posted to (default `#supply-partnership-product`). If you skip this step, the "Send to Slack" button returns a copyable preview instead of posting.

### 8. Configure environment

```
copy .env.example .env
```

Fill in:

| Var | Required | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | Yes | Gemini access |
| `GEMINI_MODEL` | No | Defaults to `gemini-2.5-flash-lite`. Try `gemini-2.5-flash` for higher quality |
| `GOOGLE_CREDENTIALS_JSON` | Yes | Full service-account JSON, on a single line |
| `GOOGLE_SHEET_ID` | Yes | Sheet ID from step 4 |
| `GOOGLE_OAUTH_CLIENT_ID` | Yes | OAuth web client ID for calendar login |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Yes | OAuth web client secret |
| `GOOGLE_OAUTH_REDIRECT_URI` | Yes | e.g. `http://localhost:3000/api/auth/google/callback` |
| `APP_SECRET_KEY` | Yes | Long random value for signing login cookies. Generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `APP_TIMEZONE` | No | Defaults to `Asia/Kolkata`. Used for calendar day boundaries. Requires `tzdata` on Windows. |
| `SLACK_WEBHOOK_URL` | No | If empty, "send" returns a copyable preview |
| `SLACK_DEFAULT_CHANNEL` | No | Defaults to `#supply-partnership-product` |

### 9. Run locally

```
uvicorn api.index:app --reload --port 3000
```

Open http://localhost:3000.

First time you click any partner call, you'll be prompted to OAuth-sign-in to Google. After that, your refresh token is stored in the `oauth_tokens` worksheet for 30 days.

### 10. CLI for one-off briefings

```
python brief.py "Indeed" --time 10:00 --title "Quarterly Partner Review"
python brief.py "LinkedIn Talent Solutions" --out brief.json
```

The CLI runs the same pipeline as the web app — useful for testing prompts or pre-warming briefings before a busy day. Note: the CLI does not have a signed-in user, so `generated_by` will be empty.

## Endpoints

All endpoints are defined in [api/index.py](api/index.py).

### Health & Auth

- `GET /api/health` — env var presence flags (`gemini`, `calendar_oauth`, `app_secret`, `slack`, `sheets`)
- `GET /api/auth/google` — starts Google OAuth login
- `GET /api/auth/google/callback` — OAuth callback; stores token, sets signed cookie
- `GET /api/auth/me` — returns logged-in user, if any
- `POST /api/auth/logout` — clears the login cookie

### Calendar

- `GET /api/calendar/today?date=YYYY-MM-DD&refresh=true` — today's partner calls. Cached 5 min/user; `refresh=true` bypasses cache.

### Briefings

- `POST /api/briefing/run` `{ partner, call }` — runs gather → synthesize (parallelised), persists, returns the briefing
- `GET /api/briefings?limit=200` — list every briefing ever generated, newest first. Frontend pages this 5 at a time.
- `GET /api/briefing/:id` — fetch a saved briefing (used by the past-briefings click-to-open and Edit flows)
- `PATCH /api/briefing/:id` — save edits (merged over output at render time)
- `POST /api/briefing/:id/approve` — mark approved (required before any send). Idempotent: also used for **re-approve** after editing an already-approved briefing — same record, status stays `approved`, `approved_at` refreshed, edits overwrite the previous edits.
- `POST /api/briefing/:id/clone` `{ edits? }` — creates a brand-new **draft** briefing record from this one's `output` plus `edits` merged on top, leaving the original untouched. Used by the **📝 Save as new draft** button. Returns `{ id, status: "draft", cloned_from }`.
- `POST /api/briefing/:id/slack` `{ channel? }` — post to Slack; requires `status == approved`
- `POST /api/briefing/:id/email` — send the full brief as styled HTML email to the **logged-in user's inbox** via the Gmail API. Requires `status == approved`, the `gmail.send` OAuth scope, and the Gmail API enabled in the Google Cloud project. Returns `{ sent: true, to, message_id }`. **Replaces the old `mailto:` flow** — no app launching, no truncation.
- `GET /api/briefing/:id/preview` — Slack message preview text without sending
- `GET /api/briefing/:id/deck` — live HTML deck (A4-landscape slides, currency-aware metric tables, clickable links). Append `?print=1` to auto-fire the browser's Save-as-PDF dialog (used as the Vercel-friendly PDF fallback).
- `GET /api/briefing/:id/pdf` — server-rendered PDF download via Playwright (A4 landscape, zero margins, background graphics on, source links preserved). Saved as `{Publisher} {DD-MM-YYYY}.pdf` (e.g. `Indeed 05-05-2026.pdf`). Returns 501 if Playwright/Chromium isn't installed; the frontend silently falls back to `GET /api/briefing/:id/deck?print=1`, which auto-fires the browser's Save-as-PDF dialog.
- `GET /api/publishers` — curated publisher list grouped by tier (P0/P1/P2)
- `GET /api/audit?target_type=&target_id=&limit=` — audit-log feed, optionally filtered to one entity. Tracked actions: `created`, `edited`, `approved`, `cloned`, `sent_slack`, `sent_email`, `uploaded_*`, `deleted_*`.

### Notes / Performance / Transcripts

- `GET /api/notes/:partner` · `POST /api/notes/:partner` `{ kind, title?, body }` · `DELETE /api/notes/:id`
- `POST /api/performance/upload` (multipart: `partner`, `file`) — Mojo CSV (≤20MB), returns `{ summary, currency, row_count }`
- `GET /api/performance/:partner` · `DELETE /api/performance/:id`
- `POST /api/transcripts/:partner` (multipart: `file`, `call_date?`, `call_title?`) — uploads .txt transcript
- `GET /api/transcripts/:partner` · `DELETE /api/transcripts/:id`

### Session reset

- `POST /api/uploads/clear` — wipes both performance and transcripts worksheets. Called by the frontend on every page load.

## Deploying to Vercel

```
vercel deploy
```

Then set environment variables in the Vercel dashboard (**Project → Settings → Environment Variables**). All keys from `.env.example` need to be set, with `GOOGLE_CREDENTIALS_JSON` pasted as a single-line string.

[vercel.json](vercel.json) rewrites every `/api/*` request to `api/index.py`. Static assets in [public/](public/) are served at the project root by Vercel's static handler.

**Function timeout**: gather steps now run in parallel (`asyncio.gather`), so a briefing typically lands in ~10–15s. Vercel hobby tier caps at 10s — likely still tight. **Vercel Pro** (60s) is recommended; set `maxDuration: 60` in `vercel.json` for the briefing endpoint.

**Caveat**: the in-memory calendar cache works per-instance, so behind Vercel's serverless model the same user may hit a cold instance and miss the cache. It's still useful (a single warm worker handles many requests) but not as effective as locally. To make caching truly global, switch to Vercel KV.

**PDF export on Vercel**: the **📄 Download PDF** button works on Vercel out-of-the-box. The frontend tries `/api/briefing/:id/pdf` first; if Playwright/Chromium isn't available (which is the default on Vercel's standard Python runtime), it transparently falls back to opening `/api/briefing/:id/deck?print=1` in a new tab, where an embedded `window.print()` script auto-fires the browser's Save-as-PDF dialog. The user clicks Save once and gets the same A4 landscape output. No extra deploy step needed. If you want zero-click downloads on Vercel later, swap the endpoint to a hosted PDF service (DocRaptor / PDFShift / Browserless) — replace the `async_playwright` block in `briefing_pdf` with an HTTP call.

**Update OAuth redirect URI**: in Google Cloud → OAuth client → Authorized redirect URIs, add `https://your-app.vercel.app/api/auth/google/callback`. Set `GOOGLE_OAUTH_REDIRECT_URI` to the same value in Vercel env vars.

## Audit & history

- **Past briefings** section on the dashboard lists every brief generated, newest first, paginated **5 per page** with a "← Newer / Older →" pager. Each row shows creator, status pill, account-snapshot preview, and per-row buttons: **📊 Deck**, **📄 PDF**, **📧 Email**, plus **✏️ Edit** on approved rows.
- **Click any row** to open the briefing back into the workbench with all edits restored — review, edit, approve, re-approve, or fork into a new draft without regenerating.
- **Audit log** worksheet captures every create / edit / approve / clone / send (Slack or email) / upload / delete with the actor's email + timestamp. Queryable via `GET /api/audit?target_type=briefing&target_id=42`.
- **View original toggle** on the workbench lets reviewers compare the rep's edits against the model's first draft side-by-side.

### Edit flows for approved briefings

Two ways to revise an approved briefing:

| Action | Button | Result |
|---|---|---|
| Update the same record in place | **✏️ Edit this briefing** → make changes → **✓ Re-approve** | Same `id`, status stays `approved`, edits overwrite previous edits, `approved_at` refreshed. |
| Fork into a new draft | **📝 Save as new draft** | New `id`, status `draft`, output = original output + edits merged. Original record untouched. `inputs_json` records `cloned_from_briefing_id`. |

## Excluded by design

- ATS / hiring volume signals (out of scope).
- Auto-send. Every brief is a draft until a human approves.
- Cross-user data scoping (planned). All notes / transcripts / performance uploads are currently visible to every signed-in user — fine for a single-team rollout, needs an `email` filter for multi-team use.

## Known limitations

- **Storage shared across users.** Notes, transcripts, and performance uploads are not currently scoped per-user — every signed-in user sees every other user's data. Fix in roadmap (add `email` column, filter on read).
- **Sheets is slow.** 5+ Sheets API calls per page load × 1–3s each. For higher-volume use, swap [app/storage.py](app/storage.py) for Postgres / Turso.
- **Free Gemini tier dies fast.** Each test cycle burns ~5 calls. Enable billing on the Google Cloud project before relying on it.
- **CSV parser is Mojo-specific.** Other report formats (per-publisher, per-day) won't parse.
