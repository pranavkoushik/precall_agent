"""FastAPI app — Vercel serverless entry point.

All routes live under /api/* and are dispatched by FastAPI.
Static frontend is served by Vercel from the public/ directory at the project root.
"""
from __future__ import annotations

import re
import time
from datetime import date as _date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.auth import (
    build_google_auth_url,
    clear_session_cookie,
    exchange_google_callback,
    get_session_user,
    require_session_user,
    set_session_cookie,
)
from app.briefing import run_briefing
from app.config import settings
from app.services import (
    GeminiRateLimited,
    GmailScopeMissing,
    build_slack_message,
    fetch_partner_calls,
    parse_mojo_csv,
    render_deck,
    render_email_html,
    send_gmail_html,
    send_to_slack,
    refresh_user_calendar_credentials,
    summarise_performance,
)
from app import storage
from app.publishers import PUBLISHERS

app = FastAPI(title="Pre Call Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(GeminiRateLimited)
async def _gemini_rate_limit_handler(request: Request, exc: GeminiRateLimited):
    """Return a clean 429 with Retry-After so the UI can show 'try again in Xs' instead of a stack trace."""
    return JSONResponse(
        status_code=429,
        content={
            "error": "rate_limited",
            "detail": str(exc),
            "retry_after_seconds": exc.retry_after_seconds,
        },
        headers={"Retry-After": str(exc.retry_after_seconds)},
    )

# When running locally with `uvicorn api.index:app`, serve the frontend too.
# On Vercel this is handled by the rewrites in vercel.json.
PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"
if PUBLIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(PUBLIC_DIR)), name="static")


# ─── Models ───────────────────────────────────────────────────────────────────
class CallIn(BaseModel):
    partner: str | None = None
    time: str | None = None
    title: str | None = None
    attendees: list[str] | None = None


class RunBriefingIn(BaseModel):
    partner: str
    call: CallIn | None = None


class NoteIn(BaseModel):
    kind: str = "note"
    title: str | None = None
    body: str


class EditsIn(BaseModel):
    edits: dict[str, Any] | None = None


class SlackIn(BaseModel):
    channel: str | None = None


# ─── Health ───────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {
        "ok": True,
        "gemini": bool(settings.gemini_api_key),
        "calendar_oauth": bool(settings.google_oauth_client_id and settings.google_oauth_client_secret),
        "slack": bool(settings.slack_webhook_url),
        "sheets": bool(settings.google_sheet_id and settings.google_credentials_json),
        "app_secret": bool(settings.app_secret_key or settings.google_oauth_client_secret),
    }


# ─── Calendar ─────────────────────────────────────────────────────────────────
@app.get("/api/auth/google")
def auth_google(request: Request):
    return RedirectResponse(build_google_auth_url(request))


@app.get("/api/auth/google/callback")
def auth_google_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        raise HTTPException(400, f"Google OAuth error: {error}")
    if not code or not state:
        raise HTTPException(400, "Missing OAuth code/state")
    user = exchange_google_callback(request, code, state)
    response = RedirectResponse("/")
    set_session_cookie(response, user, request)
    return response


@app.get("/api/auth/me")
def auth_me(request: Request):
    user = get_session_user(request)
    return {"authenticated": bool(user), "user": user}


@app.post("/api/auth/logout")
def auth_logout():
    response = JSONResponse({"ok": True})
    clear_session_cookie(response)
    return response


# In-memory cache for /api/calendar/today. Keyed by (email, date), TTL 5 min.
# Avoids burning a Gemini call on every page reload — calendars rarely change minute-to-minute,
# and the Reload button bypasses with ?refresh=true.
_CALENDAR_CACHE: dict[tuple[str, str], tuple[float, list]] = {}
_CALENDAR_TTL_SECONDS = 300


@app.get("/api/calendar/today")
def calendar_today(request: Request, date: str | None = None, refresh: bool = False):
    user = require_session_user(request)
    d = date or _date.today().isoformat()
    cache_key = (user["email"], d)
    now = time.time()

    if not refresh:
        cached = _CALENDAR_CACHE.get(cache_key)
        if cached and (now - cached[0]) < _CALENDAR_TTL_SECONDS:
            cached_at, calls = cached
            return {
                "date": d, "user": user, "calls": calls,
                "cached": True, "cached_age_seconds": int(now - cached_at),
            }

    oauth_user = storage.get_oauth_user(user["email"])
    if not oauth_user or not oauth_user.get("token"):
        raise HTTPException(401, "Sign in with Google to read your calendar")
    credentials, refreshed_token = refresh_user_calendar_credentials(oauth_user["token"])
    if refreshed_token != oauth_user["token"]:
        storage.upsert_oauth_user(
            email=oauth_user["email"],
            name=oauth_user.get("name") or user["name"],
            picture=oauth_user.get("picture") or user.get("picture") or "",
            token=refreshed_token,
        )
    calls = fetch_partner_calls(d, calendar_credentials=credentials)
    _CALENDAR_CACHE[cache_key] = (now, calls)
    # Drop any sufficiently-stale entries while we're here so the dict can't grow without bound.
    for k in [k for k, (ts, _) in _CALENDAR_CACHE.items() if now - ts > _CALENDAR_TTL_SECONDS * 4]:
        _CALENDAR_CACHE.pop(k, None)
    return {"date": d, "user": user, "calls": calls, "cached": False, "cached_age_seconds": 0}


# ─── Briefings ────────────────────────────────────────────────────────────────
@app.post("/api/briefing/run")
async def briefing_run(payload: RunBriefingIn, request: Request):
    if not payload.partner:
        raise HTTPException(400, "partner required")
    call = payload.call.model_dump(exclude_none=True) if payload.call else {}
    user = get_session_user(request)
    result = await run_briefing(partner=payload.partner, call=call, user=user)
    storage.audit(user, "created", "briefing", result.get("id"), {"partner": payload.partner})
    return result


@app.get("/api/briefings")
def briefings_all(limit: int = 200):
    """List every briefing in the sheet, newest first. Used by the Past briefings tab."""
    return {"briefings": storage.list_all_briefings(limit=limit)}


@app.get("/api/publishers")
def publishers_grouped():
    """Return the curated publisher list grouped by tier. Used by the manual
    'Brief any publisher' dropdown on the dashboard."""
    grouped: dict[str, list[dict]] = {"P0": [], "P1": [], "P2": []}
    for p in PUBLISHERS:
        tier = p.get("tier", "P2")
        grouped.setdefault(tier, []).append({
            "name": p["name"],
            "aliases": p.get("aliases") or [],
            "domains": p.get("domains") or [],
        })
    for tier in grouped:
        grouped[tier].sort(key=lambda x: x["name"].lower())
    return grouped


@app.get("/api/briefing/{briefing_id}")
def briefing_get(briefing_id: int):
    b = storage.get_briefing(briefing_id)
    if not b:
        raise HTTPException(404, "not found")
    return b


@app.patch("/api/briefing/{briefing_id}")
def briefing_patch(briefing_id: int, body: dict[str, Any], request: Request):
    b = storage.get_briefing(briefing_id)
    if not b:
        raise HTTPException(404, "not found")
    merged = {**(b.get("edits") or {}), **(body or {})}
    storage.update_briefing_edits(briefing_id, merged)
    storage.audit(get_session_user(request), "edited", "briefing", briefing_id, {"keys": list((body or {}).keys())})
    return {"id": briefing_id, "edits": merged}


@app.post("/api/briefing/{briefing_id}/approve")
def briefing_approve(briefing_id: int, request: Request, payload: EditsIn | None = None):
    b = storage.get_briefing(briefing_id)
    if not b:
        raise HTTPException(404, "not found")
    storage.approve_briefing(briefing_id, (payload.edits if payload else None))
    storage.audit(get_session_user(request), "approved", "briefing", briefing_id)
    return {"id": briefing_id, "status": "approved"}


@app.post("/api/briefing/{briefing_id}/clone")
def briefing_clone(briefing_id: int, request: Request, payload: EditsIn | None = None):
    """Create a NEW DRAFT briefing record from this one's content + the user's
    latest edits, baked in as the new "output". The original is untouched.
    The new briefing stays in "draft" so the rep can keep editing and approve
    explicitly when ready."""
    b = storage.get_briefing(briefing_id)
    if not b:
        raise HTTPException(404, "not found")
    edits = (payload.edits if payload else None) or {}
    base_output = b.get("output") or {}
    # Merge edits at the top level (same shape the renderer uses for "effective" view).
    merged_output = {**base_output, **edits}
    inputs = {
        "partner": b["partner"],
        "call": b.get("call") or {},
        "cloned_from_briefing_id": b["id"],
        "generated_by": (get_session_user(request) or {"email": "anonymous"}),
    }
    new_id = storage.insert_briefing(b["partner"], b.get("call"), inputs, merged_output)
    storage.audit(
        get_session_user(request),
        "cloned",
        "briefing",
        new_id,
        {"cloned_from": b["id"]},
    )
    return {"id": new_id, "status": "draft", "cloned_from": b["id"]}


@app.post("/api/briefing/{briefing_id}/slack")
def briefing_slack(briefing_id: int, request: Request, payload: SlackIn | None = None):
    b = storage.get_briefing(briefing_id)
    if not b:
        raise HTTPException(404, "not found")
    if b["status"] != "approved":
        raise HTTPException(400, "briefing must be approved before sending")

    brief = storage.effective_brief(b)
    text = build_slack_message(brief, b["partner"], b["call"])

    if not settings.slack_webhook_url:
        return {"sent": False, "preview": text}

    try:
        send_to_slack(text, (payload.channel if payload else None))
    except Exception as e:
        raise HTTPException(502, f"Slack send failed: {e}")
    storage.mark_briefing_sent(briefing_id)
    storage.audit(get_session_user(request), "sent_slack", "briefing", briefing_id,
                  {"channel": (payload.channel if payload else None) or settings.slack_default_channel})
    return {"sent": True, "preview": text}


@app.get("/api/briefing/{briefing_id}/preview")
def briefing_preview(briefing_id: int):
    b = storage.get_briefing(briefing_id)
    if not b:
        raise HTTPException(404, "not found")
    brief = storage.effective_brief(b)
    return {"preview": build_slack_message(brief, b["partner"], b["call"])}


@app.get("/api/briefing/{briefing_id}/deck", response_class=HTMLResponse)
def briefing_deck(briefing_id: int):
    b = storage.get_briefing(briefing_id)
    if not b:
        raise HTTPException(404, "not found")
    return render_deck(partner=b["partner"], call=b["call"], brief=storage.effective_brief(b))


@app.get("/api/briefing/{briefing_id}/pdf")
async def briefing_pdf(briefing_id: int, request: Request):
    """Render the deck as a downloadable PDF — A4 landscape, zero margins,
    background graphics enabled, clickable source links preserved.

    Requires Playwright (`pip install playwright && playwright install chromium`).
    Local-dev workflow only — Vercel's serverless Python runtime can't host the
    Chromium binary out-of-the-box (use the print-to-PDF browser fallback there)."""
    b = storage.get_briefing(briefing_id)
    if not b:
        raise HTTPException(404, "not found")

    try:
        from playwright.async_api import async_playwright  # type: ignore[import-not-found]
    except ImportError:
        raise HTTPException(
            501,
            "PDF export needs Playwright. Install with: pip install playwright && playwright install chromium. "
            "As a workaround, open the deck in your browser and use Print → Save as PDF.",
        )

    html = render_deck(partner=b["partner"], call=b["call"], brief=storage.effective_brief(b))

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content(html, wait_until="domcontentloaded")
                # Wait for Google Fonts (Poppins / Lato / DM Mono) to actually load before snapshotting.
                await page.evaluate("document.fonts.ready")
                pdf_bytes = await page.pdf(
                    format="A4",
                    landscape=True,
                    print_background=True,
                    margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
                    prefer_css_page_size=False,
                )
            finally:
                await browser.close()
    except Exception as e:
        raise HTTPException(500, f"PDF render failed: {e}")

    storage.audit(get_session_user(request), "downloaded_pdf", "briefing", briefing_id)

    # "{Publisher} {DD-MM-YYYY}.pdf" — strip only filename-unsafe characters
    # (Windows blocks < > : " / \ | ? *) so the partner name stays readable.
    safe_partner = re.sub(r'[<>:"/\\|?*]+', "", b["partner"]).strip() or "partner"
    filename = f'{safe_partner} {_date.today().strftime("%d-%m-%Y")}.pdf'
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/briefing/{briefing_id}/email")
def briefing_email(briefing_id: int, request: Request):
    """Send the full brief as an HTML email to the logged-in user via the Gmail API.
    Uses their stored OAuth token (must include the gmail.send scope, granted at
    sign-in). No mailto, no truncation — full content rendered as styled HTML."""
    user = require_session_user(request)
    b = storage.get_briefing(briefing_id)
    if not b:
        raise HTTPException(404, "not found")
    if b["status"] != "approved":
        raise HTTPException(400, "briefing must be approved before emailing")

    oauth_record = storage.get_oauth_user(user["email"])
    if not oauth_record or not oauth_record.get("token"):
        raise HTTPException(401, "Sign in with Google first to enable email sending.")

    brief = storage.effective_brief(b)
    subject = f"Pre-call brief: {b['partner']}"
    html = render_email_html(brief, b["partner"], b["call"])

    try:
        message_id = send_gmail_html(
            to=user["email"],
            subject=subject,
            html=html,
            oauth_token=oauth_record["token"],
            sender=user["email"],
        )
    except GmailScopeMissing as e:
        raise HTTPException(403, str(e))
    except Exception as e:
        raise HTTPException(502, f"Gmail send failed: {e}")

    storage.audit(user, "sent_email", "briefing", briefing_id, {"to": user["email"], "message_id": message_id})
    return {"sent": True, "to": user["email"], "message_id": message_id}


@app.get("/api/audit")
def audit_list(target_type: str | None = None, target_id: str | None = None, limit: int = 200):
    return {"audit": storage.list_audit(target_type=target_type, target_id=target_id, limit=limit)}


# ─── Notes ────────────────────────────────────────────────────────────────────
@app.post("/api/notes/{partner}")
def notes_create(partner: str, payload: NoteIn, request: Request):
    kind = (payload.kind or "note").lower()
    if kind not in {"note", "qbr", "contract"}:
        raise HTTPException(400, "kind must be note|qbr|contract")
    if not (payload.body or "").strip():
        raise HTTPException(400, "body required")
    saved = storage.insert_note(partner, kind, payload.title, payload.body.strip())
    storage.audit(get_session_user(request), "created", "note", saved.get("id"),
                  {"partner": partner, "kind": kind})
    return saved


@app.get("/api/notes/{partner}")
def notes_list(partner: str):
    return {"partner": partner, "notes": storage.list_notes(partner)}


@app.delete("/api/notes/{note_id}")
def notes_delete(note_id: int, request: Request):
    storage.delete_note(note_id)
    storage.audit(get_session_user(request), "deleted", "note", note_id)
    return {"ok": True}


# ─── Performance ──────────────────────────────────────────────────────────────
@app.post("/api/performance/upload")
async def performance_upload(request: Request, partner: str = Form(...), file: UploadFile = File(...)):
    if not partner.strip():
        raise HTTPException(400, "partner required")
    raw = await file.read()
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(413, "file too large (max 20MB)")
    rows, currency = parse_mojo_csv(raw)
    summary = summarise_performance(rows, currency=currency)
    saved = storage.insert_perf(partner, file.filename or "upload.csv", summary)
    storage.audit(get_session_user(request), "uploaded", "performance", saved.get("id"),
                  {"partner": partner, "filename": file.filename, "rows": len(rows), "currency": currency})
    return {
        "id": saved["id"],
        "partner": partner,
        "summary": summary,
        "row_count": len(rows),
        "currency": currency,
    }


@app.get("/api/performance/{partner}")
def performance_list(partner: str):
    return {"partner": partner, "uploads": storage.list_perf(partner)}


@app.delete("/api/performance/{perf_id}")
def performance_delete(perf_id: int, request: Request):
    ok = storage.delete_perf(perf_id)
    storage.audit(get_session_user(request), "deleted", "performance", perf_id)
    return {"ok": ok}


# ─── Session reset (clear all uploads) ────────────────────────────────────────
@app.post("/api/uploads/clear")
def uploads_clear():
    """Wipe all performance + transcript uploads. Called by the frontend on every
    page load so each session starts with no stale uploads — matches the rep's
    workflow of re-uploading fresh data per call."""
    return storage.clear_all_uploads()


# ─── Transcripts (uploaded by the rep) ────────────────────────────────────────
@app.post("/api/transcripts/{partner}")
async def transcripts_upload(
    partner: str,
    request: Request,
    file: UploadFile = File(...),
    call_date: str | None = Form(None),
    call_title: str | None = Form(None),
):
    if not partner.strip():
        raise HTTPException(400, "partner required")
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(413, "transcript too large (max 5MB)")
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            content = raw.decode("latin-1")
        except UnicodeDecodeError:
            raise HTTPException(400, "transcript must be a text file (UTF-8 or Latin-1)")
    if not content.strip():
        raise HTTPException(400, "transcript file is empty")
    saved = storage.insert_transcript(
        partner=partner,
        filename=file.filename or "transcript.txt",
        content=content,
        call_date=call_date,
        call_title=call_title,
    )
    storage.audit(get_session_user(request), "uploaded", "transcript", saved.get("id"),
                  {"partner": partner, "filename": file.filename, "truncated": saved.get("truncated")})
    return saved


@app.get("/api/transcripts/{partner}")
def transcripts_list(partner: str):
    return {"partner": partner, "transcripts": storage.list_transcripts(partner)}


@app.delete("/api/transcripts/{transcript_id}")
def transcripts_delete(transcript_id: int, request: Request):
    ok = storage.delete_transcript(transcript_id)
    storage.audit(get_session_user(request), "deleted", "transcript", transcript_id)
    return {"ok": ok}


# ─── Static frontend (local dev only) ─────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def serve_index():
    idx = PUBLIC_DIR / "index.html"
    if idx.exists():
        return FileResponse(str(idx), media_type="text/html")
    return HTMLResponse("<h1>Pre Call Agent</h1><p>Frontend not found.</p>", status_code=200)


@app.get("/{filename}")
def serve_static(filename: str):
    """Serve top-level files in public/ (styles.css, app.js, etc.) during local dev.
    On Vercel, static files are served directly without hitting this handler."""
    target = PUBLIC_DIR / filename
    if target.is_file():
        return FileResponse(str(target))
    raise HTTPException(404, "not found")
