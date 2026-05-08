"""All external integrations: Gemini, Google Calendar API, Avoma, CSV parsing, Slack, deck rendering."""
from __future__ import annotations

import csv
import io
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from html import escape
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from google import genai
from google.genai import types as gtypes
from google.genai.errors import ClientError as GenAIClientError
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import logging

logger = logging.getLogger(__name__)


class GeminiRateLimited(Exception):
    """Raised when Gemini returns a 429. Carries the suggested retry delay so the
    API layer can surface a clean Retry-After header and the UI can show a sane message."""

    def __init__(self, retry_after_seconds: int = 60, raw_message: str = ""):
        self.retry_after_seconds = retry_after_seconds
        self.raw_message = raw_message
        super().__init__(
            f"Gemini quota exhausted — retry in {retry_after_seconds}s. "
            "Enable billing on the Google Cloud project or wait for the daily reset."
        )


def _retry_after_from_genai_error(err) -> int:
    """Pull the suggested retry delay out of a google.genai 429 payload, or fall back to 60."""
    try:
        details = (err.details or {}).get("error", {}).get("details", []) if hasattr(err, "details") else []
        if not details and hasattr(err, "response_json"):
            details = (err.response_json or {}).get("error", {}).get("details", [])
        for d in details:
            if d.get("@type", "").endswith("RetryInfo"):
                delay = d.get("retryDelay", "")
                m = re.search(r"\d+", delay)
                if m:
                    return int(m.group(0))
    except Exception:
        pass
    return 60


from app.config import settings
from app.prompts import (
    AVOMA_SYNTH_SYSTEM,
    NEWS_SYSTEM,
    calendar_system,
)
from app.publishers import (
    format_for_prompt as _format_publishers,
    is_obviously_internal as _is_obviously_internal,
    match_event as _match_event_python,
)

log = logging.getLogger(__name__)

MODEL = settings.gemini_model
FAST_MODEL = settings.gemini_model


# ─── Gemini ──────────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def _gemini() -> genai.Client:
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY not set")
    return genai.Client(api_key=settings.gemini_api_key)


def _parse_json(text: str) -> Any:
    cleaned = text.replace("```json", "").replace("```", "").strip()
    arr_start, arr_end = cleaned.find("["), cleaned.rfind("]")
    obj_start, obj_end = cleaned.find("{"), cleaned.rfind("}")
    if arr_start == 0 and arr_end > arr_start:
        candidate = cleaned[arr_start : arr_end + 1]
    elif obj_start >= 0 and obj_end > obj_start:
        candidate = cleaned[obj_start : obj_end + 1]
    else:
        candidate = cleaned
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse JSON from model: {e}. Raw: {text[:400]}")


def call_gemini(
    *,
    system: str,
    user: str,
    use_search: bool = False,
    json_mode: bool = False,
    max_tokens: int = 1500,
    fast: bool = False,
) -> str:
    cfg: dict[str, Any] = {
        "system_instruction": system,
        "max_output_tokens": max_tokens,
        "temperature": 0.4,
    }
    # Search grounding can't be combined with response_mime_type; pick one.
    if use_search:
        cfg["tools"] = [gtypes.Tool(google_search=gtypes.GoogleSearch())]
    elif json_mode:
        cfg["response_mime_type"] = "application/json"

    try:
        response = _gemini().models.generate_content(
            model=FAST_MODEL if fast else MODEL,
            contents=user,
            config=gtypes.GenerateContentConfig(**cfg),
        )
    except GenAIClientError as e:
        if getattr(e, "status_code", None) == 429 or "RESOURCE_EXHAUSTED" in str(e):
            raise GeminiRateLimited(_retry_after_from_genai_error(e), raw_message=str(e))
        raise
    return response.text or ""


def call_gemini_json(**opts) -> Any:
    return _parse_json(call_gemini(**opts))


# ─── Calendar (direct Google Calendar API) ────────────────────────────────────
_CAL_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


# Fixed-offset fallbacks for common timezones when the `tzdata` package isn't
# installed (Windows doesn't ship system tzdata). Guarantees we never return
# a naive datetime, which Google Calendar would reject with 400.
_TZ_FALLBACK = {
    "Asia/Kolkata": timezone(timedelta(hours=5, minutes=30), "IST"),
    "UTC": timezone.utc,
}


def _app_tz():
    name = settings.app_timezone or "Asia/Kolkata"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        fb = _TZ_FALLBACK.get(name)
        if fb is not None:
            logger.warning(
                "tzdata not available for %s; using fixed offset %s. "
                "Run `pip install tzdata` for full IANA timezone support.",
                name, fb,
            )
            return fb
        logger.warning(f"Unknown timezone {name}; falling back to UTC")
        return timezone.utc


@lru_cache(maxsize=1)
def _calendar_service():
    if not settings.google_credentials_json:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON not set")
    info = json.loads(settings.google_credentials_json)
    creds = ServiceAccountCredentials.from_service_account_info(info, scopes=_CAL_SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def credentials_from_oauth_token(token: dict) -> UserCredentials:
    expiry = token.get("expiry")
    return UserCredentials(
        token=token.get("token"),
        refresh_token=token.get("refresh_token"),
        token_uri=token.get("token_uri") or "https://oauth2.googleapis.com/token",
        client_id=settings.google_oauth_client_id,
        client_secret=settings.google_oauth_client_secret,
        scopes=token.get("scopes") or _CAL_SCOPES,
        expiry=datetime.fromisoformat(expiry) if expiry else None,
    )


def oauth_token_from_credentials(creds: UserCredentials, old_token: dict | None = None) -> dict:
    old_token = old_token or {}
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token or old_token.get("refresh_token"),
        "token_uri": creds.token_uri or old_token.get("token_uri") or "https://oauth2.googleapis.com/token",
        "scopes": list(creds.scopes or old_token.get("scopes") or _CAL_SCOPES),
        "expiry": creds.expiry.isoformat() if creds.expiry else old_token.get("expiry"),
    }


def refresh_user_calendar_credentials(token: dict) -> tuple[UserCredentials, dict]:
    creds = credentials_from_oauth_token(token)
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())
        token = oauth_token_from_credentials(creds, token)
    return creds, token


def _list_events_for_date(date_str: str, credentials: Any | None = None) -> list[dict]:
    """Pull raw events for a local calendar day, using APP_TIMEZONE day bounds."""
    cal = build("calendar", "v3", credentials=credentials, cache_discovery=False) if credentials else _calendar_service()
    tz = _app_tz()
    start_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=tz)
    end_dt = start_dt + timedelta(days=1)
    try:
        result = cal.events().list(
            calendarId="primary" if credentials else (settings.google_calendar_id or "primary"),
            timeMin=start_dt.isoformat(),
            timeMax=end_dt.isoformat(),
            timeZone=settings.app_timezone or "Asia/Kolkata",
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        ).execute()
    except HttpError as e:
        raise RuntimeError(f"Calendar API error: {e}")

    out = []
    for ev in (result.get("items") or []):
        s = ev.get("start", {})
        start_iso = s.get("dateTime") or s.get("date") or ""
        time_part = start_iso.split("T")[1][:5] if "T" in start_iso else ""
        out.append({
            "id": ev.get("id"),
            "title": ev.get("summary") or "(no title)",
            "start_iso": start_iso,
            "time": time_part,
            "attendees": [a.get("email") for a in (ev.get("attendees") or []) if a.get("email")],
            "organizer_email": (ev.get("organizer") or {}).get("email", ""),
            "description": (ev.get("description") or "")[:300],
        })
    return out


def fetch_partner_calls(date_str: str, calendar_credentials: Any | None = None) -> list[dict]:
    """Hybrid calendar matcher.

    Step 1: Pure-Python pass against the curated publisher list (title + attendee-domain
            substring matches). Catches the common case for free — no Gemini call.
    Step 2: Python heuristic skips obviously-internal events (all attendees on @joveo.com
            or no external party) so they never reach Gemini.
    Step 3: Anything left (external-looking but not in the curated list) is sent to Gemini
            to classify as `unlisted` or drop. Saves quota dramatically vs sending all events.
    """
    events = _list_events_for_date(date_str, calendar_credentials)
    if not events:
        return []

    matched: list[dict] = []
    ambiguous: list[dict] = []
    for ev in events:
        m = _match_event_python(ev)
        if m:
            matched.append(m)
            continue
        if _is_obviously_internal(ev):
            continue  # Skip without consulting Gemini.
        ambiguous.append(ev)

    if ambiguous:
        try:
            user_msg = (
                f"Today's date: {date_str}\n\n"
                f"Ambiguous calendar events ({len(ambiguous)}) that did NOT match the curated list "
                f"but appear to involve external attendees:\n{json.dumps(ambiguous, indent=2)}\n\n"
                f"Decide for each: is this an external partner call worth surfacing? "
                f"If yes, include with tier=\"unlisted\" and the company name. If no, omit it."
            )
            inferred = call_gemini_json(
                system=calendar_system(_format_publishers()),
                user=user_msg,
                json_mode=True,
                max_tokens=1500,
                fast=True,
            )
            if isinstance(inferred, list):
                matched.extend(m for m in inferred if isinstance(m, dict))
        except GeminiRateLimited:
            # Soft-fail: still return Python-matched events instead of blowing up the page.
            log.warning("Calendar matcher rate-limited; returning Python-matched events only.")
        except Exception as e:
            log.warning(f"Calendar inferred-match failed: {e}")

    matched.sort(key=lambda m: (m.get("time") or "", m.get("partner") or ""))
    return matched


# ─── Past-call transcripts (uploaded by the rep) ──────────────────────────────
# Replaces the previous Avoma API integration. Reps download .txt transcripts
# from Avoma (or any source) and upload them via /api/transcripts. We store
# them in Sheets and read the most recent ones at briefing time.
def synthesize_transcript_context(partner: str, transcripts: list[dict]) -> dict:
    if not transcripts:
        return {
            "last_call_summary": "",
            "commitments": [],
            "open_threads": [],
            "sentiment": "neutral",
            "key_quotes": [],
        }
    joined = "\n\n".join(
        f"--- TRANSCRIPT {i+1} ({t.get('title', 'Untitled')} · call date: {t.get('start', 'unknown')}) ---\n{t['text']}"
        for i, t in enumerate(transcripts)
    )[:80000]
    today = datetime.now(_app_tz()).strftime("%Y-%m-%d")
    return call_gemini_json(
        system=AVOMA_SYNTH_SYSTEM,
        user=f"Partner: {partner}\nTODAY: {today}\n\n{joined}",
        json_mode=True,
        max_tokens=4000,
    )


def get_call_history_context(partner: str) -> dict:
    """Read the rep's uploaded transcripts for this partner and synthesise them.
    Returns the same shape that the old Avoma integration returned, so the rest
    of the pipeline (briefing, deck, frontend) doesn't need to change."""
    # Local import to avoid a circular dependency at module load.
    from app import storage

    transcripts = storage.transcripts_for_synthesis(partner)
    synthesis = synthesize_transcript_context(partner, transcripts)
    return {
        "transcripts_used": len(transcripts),
        "transcript_files": [
            {"title": t.get("title"), "start": t.get("start")} for t in transcripts
        ],
        **synthesis,
    }


# Backwards-compatible alias so anything still importing get_avoma_context works.
get_avoma_context = get_call_history_context


# ─── CSV (Mojo client report) ─────────────────────────────────────────────────
# Schema we read from the "All Clients - Client Report" export. Columns are
# normalised by stripping non-alphanumerics; "(%)" or trailing "%" is mapped to
# the "_pct" suffix to disambiguate "Live spend(%)" from "Live spend".
def _normalise_header(h: str) -> str:
    raw = str(h or "").strip().lower()
    has_pct = "%" in raw
    k = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    if has_pct and not k.endswith("pct") and not k.endswith("_pct"):
        k = f"{k}_pct"
    return k


_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_CURRENCY_PREFIX_RE = re.compile(r"^\s*([^\d.,\-+\s]+)")
_CURRENCY_MAP = {
    "$": "USD", "US$": "USD",
    "€": "EUR",
    "£": "GBP",
    "Â£": "GBP",  # mojibake variant when £ was double-encoded by Excel
    "CHF": "CHF",
    "₹": "INR", "Rs": "INR", "Rs.": "INR",
    "¥": "JPY",
    "C$": "CAD",
    "A$": "AUD",
    "R$": "BRL",
}


def _to_number(v) -> float | None:
    """Pull the first numeric token out of a cell. Tolerates any currency prefix
    ("$ 116.40", "CHF 1585.42", "Â£ 0.00") and thousands separators ("1,234.56")."""
    if v is None or v == "":
        return None
    m = _NUMBER_RE.search(str(v))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _extract_currency_symbol(s: str) -> str | None:
    """Detect the currency from a cell like '$ 116.40' or 'CHF 1585.42'.
    Returns an ISO code where known, otherwise the literal symbol, otherwise None."""
    if not s:
        return None
    m = _CURRENCY_PREFIX_RE.match(s)
    if not m:
        return None
    sym = m.group(1).strip()
    return _CURRENCY_MAP.get(sym, sym or None)


def parse_mojo_csv(file_bytes: bytes) -> tuple[list[dict], str | None]:
    """Parse a Mojo client-level performance export.

    Returns (rows, currency). Currency is detected from the first non-empty
    Revenue cell (e.g. "$ 116.40" → "USD", "CHF 1585.42" → "CHF").

    The file is per-client (no time series). We stop at the trailing "Summary"
    section since Mojo appends a header + totals row there.
    """
    text = file_bytes.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    # Mojo's export sometimes includes one or more blank rows above the header.
    # Skip them and treat the first non-empty row as the header.
    raw_header: list[str] = []
    for raw in reader:
        if any(c.strip() for c in raw):
            raw_header = raw
            break
    headers = [_normalise_header(h) for h in raw_header]
    rev_idx = headers.index("revenue") if "revenue" in headers else None
    rows: list[dict] = []
    currency: str | None = None
    for raw in reader:
        if not any(c.strip() for c in raw):
            continue
        first = (raw[0] or "").strip().lower()
        if first == "summary":
            break  # Stop at the Summary section appended at the end of the export.
        r = dict(zip(headers, raw))
        client_name = (r.get("client_name") or "").strip()
        if not client_name:
            continue
        # Detect currency from the first row that has a Revenue value with a symbol.
        if currency is None and rev_idx is not None and rev_idx < len(raw):
            sym = _extract_currency_symbol(raw[rev_idx] or "")
            if sym:
                currency = sym
        rows.append({
            "client_name": client_name,
            "total_jobs": _to_number(r.get("total_jobs")),
            "revenue": _to_number(r.get("revenue")),
            "clicks": _to_number(r.get("clicks")),
            "cpc": _to_number(r.get("cpc")),
            "live_spend_pct": _to_number(r.get("live_spend_pct")),
            "live_spend": _to_number(r.get("live_spend")),
            "live_budget": _to_number(r.get("live_budget")),
            "budget_frequency": (r.get("live_budget_cap_frequency") or "").strip() or None,
            "applies": _to_number(r.get("applies")),
            "cpa": _to_number(r.get("cpa")),
            "cta_pct": _to_number(r.get("cta_pct")),
            "apply_starts": _to_number(r.get("apply_starts")),
            "cp_apply_starts": _to_number(r.get("cp_apply_starts")),
            "original_jobs": _to_number(r.get("original_jobs")),
            "expanded_jobs": _to_number(r.get("expanded_jobs")),
            "min_bid": _to_number(r.get("min_bid")),
            "max_bid": _to_number(r.get("max_bid")),
            "invalid_clicks": _to_number(r.get("invalid_clicks")),
            "invalid_applies": _to_number(r.get("invalid_applies")),
            "hires": _to_number(r.get("hires")),
            "cph": _to_number(r.get("cph")),
            "ath_pct": _to_number(r.get("ath_pct")),
        })
    return rows, currency


def _safe_div(a, b):
    return (a / b) if (a is not None and b) else None


def _sum(rows, key):
    return sum((r.get(key) or 0) for r in rows)


def summarise_performance(rows: list[dict], currency: str | None = None) -> dict:
    """Aggregate the Mojo client report. No time dimension, so no period trend.
    CPC/CPA totals are Revenue-based to match Mojo's per-row math."""
    if not rows:
        return {
            "totals": {},
            "top_clients": [],
            "active_clients": 0,
            "total_clients": 0,
            "currency": currency,
            "period_start": None,
            "period_end": None,
        }

    totals = {
        "revenue": _sum(rows, "revenue"),
        "live_spend": _sum(rows, "live_spend"),
        "live_budget": _sum(rows, "live_budget"),
        "clicks": _sum(rows, "clicks"),
        "applies": _sum(rows, "applies"),
        "apply_starts": _sum(rows, "apply_starts"),
        "hires": _sum(rows, "hires"),
        "invalid_clicks": _sum(rows, "invalid_clicks"),
        "invalid_applies": _sum(rows, "invalid_applies"),
        "total_jobs": _sum(rows, "total_jobs"),
    }
    # Per Mojo: CPC = Revenue / Clicks, CPA = Revenue / Applies.
    totals["cpc"] = _safe_div(totals["revenue"], totals["clicks"])
    totals["cpa"] = _safe_div(totals["revenue"], totals["applies"])
    totals["cta_pct"] = _safe_div(totals["applies"], totals["clicks"])
    totals["cph"] = _safe_div(totals["revenue"], totals["hires"]) if totals["hires"] else None

    active = [
        r for r in rows
        if (r.get("revenue") or 0) > 0 or (r.get("live_spend") or 0) > 0 or (r.get("clicks") or 0) > 0
    ]
    top_clients = sorted(active, key=lambda r: r.get("revenue") or 0, reverse=True)[:15]

    return {
        "totals": totals,
        "top_clients": top_clients,
        "active_clients": len(active),
        "total_clients": len(rows),
        "currency": currency,
        "period_start": None,  # Mojo client report has no date column
        "period_end": None,
    }


# ─── Slack ────────────────────────────────────────────────────────────────────
def build_slack_message(brief: dict, partner: str, call: dict | None) -> str:
    lines: list[str] = []
    time_str = (call or {}).get("time") or ""
    title = (call or {}).get("title") or ""
    lines.append(f"*📞 Call Brief — {partner}* · {time_str} today")
    if title:
        lines.append(f"_{title}_")
    lines.append("")

    snap = brief.get("account_snapshot") or {}
    if snap.get("headline"):
        lines.append("*📌 Account Overview*")
        lines.append(snap["headline"])
        if snap.get("what_matters_now"):
            lines.append(snap["what_matters_now"])
        lines.append("")

    perf = brief.get("performance_read") or {}
    if perf.get("headline"):
        lines.append("*📊 Performance Summary*")
        lines.append(perf["headline"])
        for s in perf.get("whats_working") or []:
            lines.append(f"  ✅ {s}")
        for s in perf.get("whats_not") or []:
            lines.append(f"  ⚠️ {s}")
        lines.append("")

    news = brief.get("news") or []
    if news:
        lines.append("*🗞 Recent Highlights*")
        for i, n in enumerate(news[:3], 1):
            lines.append(f"{i}. *{n.get('headline', '')}*")
            lines.append(f"   ↳ {n.get('detail', '')}")
            if n.get("source"):
                lines.append(f"   _{n['source']} · {n.get('recency', '')}_")
        lines.append("")

    tps = brief.get("talking_points") or []
    if tps:
        lines.append("*💬 Discussion Points*")
        for i, tp in enumerate(tps, 1):
            lines.append(f"{i}. {tp.get('point', '')}")
            if tp.get("rationale"):
                lines.append(f"   _why: {tp['rationale']}_")
        lines.append("")

    recs = brief.get("recommendations") or []
    if recs:
        lines.append("*🚀 Strategic Recommendations*")
        for i, r in enumerate(recs, 1):
            lines.append(f"{i}. *{r.get('title', '')}*")
            if r.get("rationale"):
                lines.append(f"   why: {r['rationale']}")
            if r.get("expected_impact"):
                lines.append(f"   impact: {r['expected_impact']}")
        lines.append("")

    closes = brief.get("open_threads_to_close") or []
    if closes:
        lines.append("*🧵 Items to Address*")
        for t in closes:
            lines.append(f"  • {t}")
        lines.append("")

    risks = brief.get("risks_to_flag") or []
    if risks:
        lines.append("*⚠️ Risks to Monitor*")
        for t in risks:
            lines.append(f"  • {t}")

    return "\n".join(lines)


def send_to_slack(text: str, channel: str | None = None) -> bool:
    if not settings.slack_webhook_url:
        raise RuntimeError("SLACK_WEBHOOK_URL not set")
    body: dict[str, Any] = {"text": text}
    if channel:
        body["channel"] = channel
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(settings.slack_webhook_url, json=body)
        resp.raise_for_status()
    return True


def _as_list(v):
    """Coerce model output that may be a string-where-array into a list.
    Flash-Lite occasionally emits "single tip" instead of ["single tip"]; iterating
    that naively would render one element per character."""
    if isinstance(v, list):
        return v
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        return [s] if s else []
    return [v]


# Approximate FX rates used purely for cross-currency revenue ranking — NOT
# for any user-facing financial number. The "Top clients across uploads" table
# can mix CHF/USD/GBP/EUR rows; sorting by raw magnitude would put CHF 1,216
# below USD 1,527 even though the CHF figure is worth more in USD. These rates
# don't need to be live: they only affect ordering by ~one or two slots, and
# ranking is robust to ±10% drift. Update the table once a year if needed.
_FX_TO_USD = {
    "USD": 1.0,
    "EUR": 1.08,
    "GBP": 1.27,
    "CHF": 1.13,
    "INR": 0.012,
    "JPY": 0.0064,
    "CAD": 0.73,
    "AUD": 0.66,
    "BRL": 0.20,
}


def _to_usd(amount, currency: str | None) -> float:
    """Convert an amount in `currency` to approximate USD for ranking. Unknown
    currencies are treated as USD (best we can do without context)."""
    if amount is None:
        return 0.0
    rate = _FX_TO_USD.get((currency or "USD").upper(), 1.0)
    try:
        return float(amount) * rate
    except (TypeError, ValueError):
        return 0.0


# ─── Email rendering & sending ────────────────────────────────────────────────
class GmailScopeMissing(Exception):
    """Raised when the user's stored OAuth token doesn't include gmail.send.
    The API layer turns this into a clear 403 telling the user to re-sign-in."""


_GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


def render_email_html(brief: dict, partner: str, call: dict | None) -> str:
    """A presentable single-column HTML email body — works in Gmail, Outlook,
    Apple Mail. Inline styles only (most clients strip <style> blocks)."""
    call = call or {}
    title = call.get("title") or ""
    time_str = call.get("time") or ""
    tier = (call.get("tier") or "").upper()

    snap = brief.get("account_snapshot") or {}
    perf = brief.get("performance_read") or {}
    news = [n for n in _as_list(brief.get("news")) if isinstance(n, dict)]
    tps = [t for t in _as_list(brief.get("talking_points")) if isinstance(t, dict)]
    recs = [r for r in _as_list(brief.get("recommendations")) if isinstance(r, dict)]
    closes = _as_list(brief.get("open_threads_to_close"))
    risks = _as_list(brief.get("risks_to_flag"))

    base = "font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; color: #1a1a24;"
    section_title = (
        "font-family: 'Segoe UI', Helvetica, Arial, sans-serif; "
        "font-size: 13px; font-weight: 700; text-transform: uppercase; "
        "letter-spacing: 0.12em; color: #5454BF; margin: 28px 0 10px; "
        "padding-bottom: 6px; border-bottom: 1px solid #e6e6ee;"
    )
    body_p = "font-size: 14px; line-height: 1.55; color: #2a2a36; margin: 0 0 10px;"
    item_p = "font-size: 13.5px; line-height: 1.5; color: #2a2a36; margin: 0 0 8px;"
    sub = "font-size: 12.5px; line-height: 1.5; color: #666; margin: 0 0 8px;"
    pill = (
        "display:inline-block; padding:3px 9px; border-radius:4px; "
        "background:#5454BF; color:#fff; font-size:10px; font-weight:700; "
        "letter-spacing:0.1em; text-transform:uppercase;"
    )

    parts: list[str] = []
    parts.append(
        f"""<div style="background:#f4f4f8; padding: 24px 0;">
          <div style="max-width:640px; margin:0 auto; background:#fff; padding:32px 36px; border-radius:8px; box-shadow:0 2px 8px rgba(15,15,30,0.06); {base}">
            <div style="font-size:11px; font-weight:700; letter-spacing:0.2em; text-transform:uppercase; color:#5454BF;">Joveo · Call Brief</div>
            <h1 style="font-size:28px; font-weight:800; letter-spacing:-0.01em; color:#202058; margin:6px 0 4px;">{escape(partner)}</h1>
            <div style="font-size:13px; color:#666; margin-bottom:14px;">
              {(escape(title) + ' · ') if title else ''}{escape(time_str) if time_str else ''}
              {f' &nbsp; <span style="{pill}">{escape(tier)}</span>' if tier else ''}
            </div>
        """
    )

    if snap.get("headline") or snap.get("what_matters_now"):
        parts.append(f'<div style="{section_title}">📌 Account Overview</div>')
        if snap.get("headline"):
            parts.append(f'<p style="{body_p} font-weight:600;">{escape(str(snap["headline"]))}</p>')
        if snap.get("what_matters_now"):
            parts.append(f'<p style="{body_p}">{escape(str(snap["what_matters_now"]))}</p>')
        what_changed = _as_list(snap.get("what_changed"))
        if what_changed:
            parts.append('<ul style="padding-left:18px; margin:8px 0 0;">')
            for w in what_changed:
                parts.append(f'<li style="{item_p}">{escape(str(w))}</li>')
            parts.append("</ul>")

    if perf.get("headline") or perf.get("whats_working") or perf.get("whats_not"):
        parts.append(f'<div style="{section_title}">📊 Performance Summary</div>')
        if perf.get("headline"):
            parts.append(f'<p style="{body_p}">{escape(str(perf["headline"]))}</p>')
        for label, items, color, marker in [
            ("Strengths", _as_list(perf.get("whats_working")), "#0c8456", "✓"),
            ("Can do Better!", _as_list(perf.get("whats_not")), "#b85450", "!"),
            ("Where to lean in", _as_list(perf.get("where_to_lean_in")), "#5454BF", "→"),
        ]:
            if not items:
                continue
            parts.append(f'<div style="font-size:11.5px; font-weight:700; text-transform:uppercase; letter-spacing:0.08em; color:{color}; margin:10px 0 4px;">{label}</div>')
            parts.append('<ul style="padding-left:18px; margin:0;">')
            for it in items:
                parts.append(f'<li style="{item_p}"><span style="color:{color}; font-weight:700; margin-right:4px;">{marker}</span>{escape(str(it))}</li>')
            parts.append("</ul>")

    if news:
        parts.append(f'<div style="{section_title}">🗞 Recent Highlights</div>')
        for i, n in enumerate(news, 1):
            headline = escape(str(n.get("headline", "")))
            detail = escape(str(n.get("detail", "")))
            source = escape(str(n.get("source", "")))
            recency = escape(str(n.get("recency", "")))
            url = n.get("source_url") or ""
            link_html = (
                f' · <a href="{escape(url)}" style="color:#5454BF;">source ↗</a>'
                if url else ""
            )
            recency_html = f" · {recency}" if recency else ""
            parts.append(
                f'<div style="margin:0 0 14px;">'
                f'<div style="font-size:14px; font-weight:600; color:#1a1a24; margin-bottom:3px;">{i}. {headline}</div>'
                f'<div style="{item_p} margin-left:18px;">{detail}</div>'
                f'<div style="{sub} margin-left:18px;">{source}{recency_html}{link_html}</div>'
                f"</div>"
            )

    if tps:
        parts.append(f'<div style="{section_title}">💬 Discussion Points</div>')
        for i, tp in enumerate(tps, 1):
            point = escape(str(tp.get("point", "")))
            rationale = escape(str(tp.get("rationale", "")))
            why_html = (
                f'<div style="{sub} margin-left:18px;"><em>why:</em> {rationale}</div>'
                if rationale else ""
            )
            parts.append(
                f'<div style="margin:0 0 12px;">'
                f'<div style="font-size:14px; font-weight:600; color:#1a1a24; margin-bottom:3px;">{i}. {point}</div>'
                f"{why_html}"
                f"</div>"
            )

    if recs:
        parts.append(f'<div style="{section_title}">🚀 Strategic Recommendations</div>')
        for i, r in enumerate(recs, 1):
            title_r = escape(str(r.get("title", "")))
            rationale = escape(str(r.get("rationale", "")))
            impact = escape(str(r.get("expected_impact", "")))
            risk = escape(str(r.get("risk", "")))
            inner = f'<div style="font-size:14px; font-weight:600; color:#1a1a24; margin-bottom:4px;">{i}. {title_r}</div>'
            if rationale:
                inner += f'<div style="{item_p} margin-left:18px;"><strong>Why:</strong> {rationale}</div>'
            if impact:
                inner += f'<div style="{item_p} margin-left:18px;"><strong>Impact:</strong> {impact}</div>'
            if risk:
                inner += f'<div style="{sub} margin-left:18px;"><strong>Risk:</strong> {risk}</div>'
            parts.append(f'<div style="margin:0 0 14px;">{inner}</div>')

    if closes:
        parts.append(f'<div style="{section_title}">🧵 Items to Address</div>')
        parts.append('<ul style="padding-left:18px; margin:0;">')
        for c in closes:
            parts.append(f'<li style="{item_p}">{escape(str(c))}</li>')
        parts.append("</ul>")

    if risks:
        parts.append(f'<div style="{section_title}">⚠️ Risks to Monitor</div>')
        parts.append('<ul style="padding-left:18px; margin:0;">')
        for r in risks:
            parts.append(f'<li style="{item_p}">{escape(str(r))}</li>')
        parts.append("</ul>")

    parts.append(
        '<div style="margin-top:32px; padding-top:14px; border-top:1px solid #e6e6ee; font-size:11px; color:#999; letter-spacing:0.04em;">'
        "Generated by Joveo Pre-Call Agent · Reply to discuss with the partnerships team."
        "</div></div></div>"
    )
    return (
        "<!doctype html><html><body style=\"margin:0;padding:0;background:#f4f4f8;\">"
        + "".join(parts)
        + "</body></html>"
    )


def send_gmail_html(*, to: str, subject: str, html: str, oauth_token: dict, sender: str) -> str:
    """Send an HTML email via the user's Gmail using their OAuth token.
    Returns the Gmail message id. Raises GmailScopeMissing if the token
    doesn't include gmail.send (user needs to re-sign-in)."""
    import base64
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    scopes = oauth_token.get("scopes") or []
    if _GMAIL_SEND_SCOPE not in scopes:
        raise GmailScopeMissing(
            "Gmail send permission was not granted. Sign out and sign back in with Google "
            "to grant the gmail.send scope, then try again."
        )

    creds = credentials_from_oauth_token(oauth_token)
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    # Plain-text fallback for clients that don't render HTML.
    plain_fallback = re.sub(r"<[^>]+>", "", html)
    plain_fallback = re.sub(r"\s+\n", "\n", plain_fallback).strip()
    msg.attach(MIMEText(plain_fallback, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return sent.get("id", "")


# ─── Deck rendering ───────────────────────────────────────────────────────────
_CURRENCY_SYMBOLS = {
    "USD": "$", "EUR": "€", "GBP": "£", "CHF": "CHF ",
    "INR": "₹", "JPY": "¥", "CAD": "C$", "AUD": "A$", "BRL": "R$",
}


def _fmt_money(n, currency: str | None = None):
    if n is None:
        return "—"
    prefix = _CURRENCY_SYMBOLS.get(currency, f"{currency} " if currency else "$")
    return f"{prefix}{round(n):,}"


def _fmt_num(n):
    if n is None:
        return "—"
    return f"{round(n):,}"


def _fmt_pct(n):
    if n is None:
        return "—"
    return f"{n*100:.1f}%"


_TIER_PILL_CLASS = {
    "P0": "tier-p0",
    "P1": "tier-p1",
    "P2": "tier-p2",
}


def render_deck(*, partner: str, call: dict | None, brief: dict) -> str:
    """Render the brief as an A4-landscape HTML deck (one slide per A4 page).

    Each slide shares a header strip (kicker + partner + tier pill) and footer
    (page number + agent attribution). The layout is fixed at 297mm × 210mm so
    print-to-PDF matches what's on screen exactly.
    """
    snap = brief.get("account_snapshot") or {}
    perf = brief.get("performance_read") or {}
    tps = [t for t in _as_list(brief.get("talking_points")) if isinstance(t, dict)]
    recs = [r for r in _as_list(brief.get("recommendations")) if isinstance(r, dict)]
    news = [n for n in _as_list(brief.get("news")) if isinstance(n, dict)]
    avoma = brief.get("avoma_summary")
    ps = brief.get("performance_summary")
    summaries = brief.get("performance_summaries") or ([{"filename": None, "summary": ps}] if ps else [])

    call = call or {}
    call_title = call.get("title") or ""
    call_time = call.get("time") or ""
    tier = (call.get("tier") or "").upper()
    tier_pill = (
        f'<span class="tier-pill {_TIER_PILL_CLASS.get(tier, "tier-unlisted")}">{escape(tier)}</span>'
        if tier else ""
    )
    today = datetime.now().strftime("%a · %b %d %Y")

    # Total slide count (cover + dynamic content). Computed up front so we can stamp page numbers.
    n_slides = 1  # cover
    n_slides += 1  # snapshot
    n_slides += 1  # performance
    if news:
        n_slides += 1
    if avoma and (avoma.get("last_call_summary") or avoma.get("commitments") or avoma.get("open_threads")):
        n_slides += 1
    n_slides += 1  # talking points
    n_slides += 1  # recommendations
    closes = _as_list(brief.get("open_threads_to_close"))
    risks = _as_list(brief.get("risks_to_flag"))
    if closes or risks:
        n_slides += 1

    page_counter = {"i": 0}

    def _page_label() -> str:
        page_counter["i"] += 1
        return f"{page_counter['i']:02d} / {n_slides:02d}"

    def slide(section_label: str, body_html: str, *, content_class: str = "") -> str:
        page = _page_label()
        return f"""<section class="slide {content_class}">
            <header class="slide-head">
              <div class="slide-head-left">
                <span class="kicker">{escape(section_label)}</span>
              </div>
              <div class="slide-head-right">
                <span class="head-partner">{escape(partner)}</span>{tier_pill}
              </div>
            </header>
            <div class="slide-body">{body_html}</div>
            <footer class="slide-foot">
              <span>Joveo · Call Brief</span>
              <span>{escape(call_title)}{(' · ' + escape(call_time)) if (call_title and call_time) else escape(call_time)}</span>
              <span class="page-num">{page}</span>
            </footer>
        </section>"""

    def ul(items) -> str:
        arr = _as_list(items)
        if not arr:
            return '<ul class="bullet"><li class="empty">—</li></ul>'
        return '<ul class="bullet">' + "".join(
            f"<li>{escape(str(s))}</li>" for s in arr
        ) + "</ul>"

    slides: list[str] = []

    # ─── Cover ────────────────────────────────────────────────────────────────
    page_counter["i"] += 1
    cover_page = f"{page_counter['i']:02d} / {n_slides:02d}"
    slides.append(f"""<section class="slide cover">
        <div class="cover-aurora"></div>
        <header class="slide-head">
          <div class="slide-head-left"><span class="kicker">Joveo · Partnerships</span></div>
          <div class="slide-head-right"><span class="kicker" style="color:#8a8aff">Call brief</span></div>
        </header>
        <div class="cover-body">
          <div class="cover-eyebrow">FOR THE CALL WITH</div>
          <h1 class="cover-title">{escape(partner)}</h1>
          <div class="cover-meta">
            {tier_pill}
            <span class="cover-meta-text">{escape(call_title)}{(' · ' + escape(call_time)) if (call_title and call_time) else escape(call_time)}</span>
          </div>
        </div>
        <footer class="slide-foot">
          <span>{today}</span>
          <span></span>
          <span class="page-num">{cover_page}</span>
        </footer>
    </section>""")

    # ─── Snapshot ─────────────────────────────────────────────────────────────
    slides.append(slide(
        "01 · Account Overview",
        f"""<h2 class="section-title">Account Overview</h2>
            <p class="lead">{escape(snap.get('headline', '—'))}</p>
            <div class="cols-2">
              <div class="takeaway-card">
                <h4>Recent Changes</h4>
                {ul(snap.get('what_changed', []))}
              </div>
              <div class="takeaway-card">
                <h4>Current Priorities</h4>
                <p class="body-prose">{escape(snap.get('what_matters_now', '—'))}</p>
              </div>
            </div>""",
    ))

    # ─── Performance ─────────────────────────────────────────────────────────
    def _summary_block(entry: dict) -> str:
        s = entry.get("summary") or {}
        if not s.get("totals"):
            return ""
        t = s["totals"]
        cur = s.get("currency")
        active = s.get("active_clients", 0)
        total = s.get("total_clients", 0)
        cur_pill = f'<span class="cur-pill">{escape(cur)}</span>' if cur else ""
        title = "Currency" if cur else (entry.get("filename") or "Performance")
        head = (
            f'<div class="perf-block-head">'
            f'<span class="perf-block-title">{escape(title)}</span>{cur_pill}'
            f'<span class="perf-block-meta">{active}/{total} active clients</span>'
            f'</div>'
        )
        metrics = f"""<div class="metric-row">
          <div class="metric"><div class="metric-label">Revenue</div><div class="metric-value">{_fmt_money(t.get('revenue'), cur)}</div></div>
          <div class="metric"><div class="metric-label">Live spend</div><div class="metric-value">{_fmt_money(t.get('live_spend'), cur)}</div></div>
          <div class="metric"><div class="metric-label">Clicks</div><div class="metric-value">{_fmt_num(t.get('clicks'))}</div></div>
          <div class="metric"><div class="metric-label">Applies</div><div class="metric-value">{_fmt_num(t.get('applies'))}</div></div>
          <div class="metric"><div class="metric-label">CPA</div><div class="metric-value">{_fmt_money(t.get('cpa'), cur)}</div></div>
          <div class="metric"><div class="metric-label">Apply rate</div><div class="metric-value">{_fmt_pct(t.get('cta_pct'))}</div></div>
        </div>"""
        top = (s.get("top_clients") or [])[:5]
        top_html = ""
        if top:
            rows_html = "".join(
                f"<tr><td class='client'>{escape(c.get('client_name', ''))}</td>"
                f"<td>{_fmt_money(c.get('revenue'), cur)}</td>"
                f"<td>{_fmt_num(c.get('clicks'))}</td>"
                f"<td>{_fmt_num(c.get('applies'))}</td>"
                f"<td>{_fmt_money(c.get('cpa'), cur)}</td></tr>"
                for c in top
            )
            top_html = (
                f'<table class="top-clients"><thead><tr>'
                f"<th>Top clients</th><th>Revenue</th><th>Clicks</th><th>Applies</th><th>CPA</th>"
                f"</tr></thead><tbody>{rows_html}</tbody></table>"
            )
        return f'<div class="perf-block">{head}{metrics}{top_html}</div>'

    def _compact_summaries(entries: list) -> str:
        """Used when 2+ CSVs are uploaded. Renders a single comparison table
        (one row per upload) plus an aggregated top-clients table — fits a
        single A4 page no matter how many uploads there are.
        """
        valid = [e for e in entries if (e.get("summary") or {}).get("totals")]
        if not valid:
            return '<p class="muted">No performance data uploaded.</p>'
        comp_rows = []
        all_clients = []
        for e in valid:
            s = e["summary"]
            t = s["totals"]
            cur = s.get("currency") or "—"
            active = s.get("active_clients", 0)
            total = s.get("total_clients", 0)
            has_cur = cur and cur != "—"
            filename = "Currency" if has_cur else (e.get("filename") or "Performance")
            cur_pill = f"<span class='cur-pill compact'>{escape(cur)}</span>" if has_cur else ""
            comp_rows.append(
                f"<tr>"
                f"<td class='file'><strong>{escape(filename)}</strong>"
                f"{cur_pill}"
                f"<span class='active-meta'>{active}/{total} active</span></td>"
                f"<td>{_fmt_money(t.get('revenue'), cur)}</td>"
                f"<td>{_fmt_money(t.get('live_spend'), cur)}</td>"
                f"<td>{_fmt_num(t.get('clicks'))}</td>"
                f"<td>{_fmt_num(t.get('applies'))}</td>"
                f"<td>{_fmt_money(t.get('cpa'), cur)}</td>"
                f"<td>{_fmt_pct(t.get('cta_pct'))}</td>"
                f"</tr>"
            )
            for c in (s.get("top_clients") or [])[:4]:
                all_clients.append({
                    "client": c.get("client_name") or "",
                    "currency": cur,
                    "revenue": c.get("revenue") or 0,
                    "clicks": c.get("clicks") or 0,
                    "applies": c.get("applies") or 0,
                    "cpa": c.get("cpa"),
                })

        compare_html = (
            f'<div class="perf-block-head">'
            f'<span class="perf-block-title">Multi-currency comparison</span>'
            f'<span class="perf-block-meta">{len(valid)} uploads</span>'
            f"</div>"
            f'<table class="compare-table"><thead><tr>'
            f"<th>File</th><th>Revenue</th><th>Live spend</th><th>Clicks</th>"
            f"<th>Applies</th><th>CPA</th><th>Apply rate</th>"
            f"</tr></thead><tbody>"
            + "".join(comp_rows)
            + "</tbody></table>"
        )

        # Sort all clients by USD-equivalent revenue so CHF/GBP/EUR rows aren't
        # unfairly demoted just because their raw number is smaller (e.g. CHF
        # 1,216 ≈ USD 1,374 outranks USD 1,527... no wait, CHF 1,216 ≈ USD 1,374
        # is actually less than USD 1,527 — but CHF 1,500 ≈ USD 1,695 outranks
        # USD 1,527, which raw-magnitude sort would get wrong). Rates are
        # approximate; they only affect ordering of adjacent rows.
        all_clients.sort(key=lambda c: _to_usd(c.get("revenue"), c.get("currency")), reverse=True)
        # 3+ uploads = vertical room is tight; show fewer rows to avoid overflow.
        top = all_clients[: (4 if len(valid) >= 3 else 6)]
        if not top:
            return compare_html

        top_rows = "".join(
            f"<tr>"
            f"<td class='client'>{escape(c['client'])}</td>"
            f"<td><span class='cur-pill compact'>{escape(c['currency'])}</span></td>"
            f"<td>{_fmt_money(c['revenue'], c['currency'])}</td>"
            f"<td>{_fmt_num(c['clicks'])}</td>"
            f"<td>{_fmt_num(c['applies'])}</td>"
            f"<td>{_fmt_money(c['cpa'], c['currency'])}</td>"
            f"</tr>"
            for c in top
        )
        top_html = (
            f'<div class="perf-block-head" style="margin-top: 4mm">'
            f'<span class="perf-block-title">Top clients across uploads</span>'
            f'<span class="perf-block-meta">by revenue</span>'
            f"</div>"
            f'<table class="top-clients"><thead><tr>'
            f"<th>Client</th><th>Cur</th><th>Revenue</th><th>Clicks</th>"
            f"<th>Applies</th><th>CPA</th>"
            f"</tr></thead><tbody>"
            + top_rows
            + "</tbody></table>"
        )
        return compare_html + top_html

    if len(summaries) >= 2:
        summary_blocks = _compact_summaries(summaries)
    else:
        summary_blocks = "".join(_summary_block(e) for e in summaries) or '<p class="muted">No performance data uploaded.</p>'
    # Tighten the whole perf slide when 3+ uploads — comparison + top-clients +
    # takeaways otherwise overflow A4 landscape (173mm body height).
    perf_density_class = " perf-tight" if len(summaries) >= 3 else ""
    slides.append(slide(
        "02 · Performance Summary",
        f"""<h2 class="section-title">Performance Summary</h2>
            <p class="lead">{escape(perf.get('headline', '—'))}</p>
            {summary_blocks}
            <div class="cols-3 perf-takeaways{perf_density_class}">
              <div class="takeaway-card"><h4>Strengths</h4>{ul(perf.get('whats_working', []))}</div>
              <div class="takeaway-card"><h4>Can do Better!</h4>{ul(perf.get('whats_not', []))}</div>
              <div class="takeaway-card"><h4>Opportunities</h4>{ul(perf.get('where_to_lean_in', []))}</div>
            </div>""",
        content_class=("perf-slide" + (" perf-slide-tight" if len(summaries) >= 3 else "")),
    ))

    # ─── News ────────────────────────────────────────────────────────────────
    if news:
        items = "".join(
            f"""<li>
              <div class="news-headline">{escape(n.get('headline', ''))}</div>
              <div class="news-detail">{escape(n.get('detail', ''))}</div>
              <div class="news-meta">
                <span>{escape(n.get('source', ''))}</span>
                {('<span class="dot">·</span><span>' + escape(n['recency']) + '</span>') if n.get('recency') else ''}
                {('<span class="dot">·</span><span>' + escape(n['category']) + '</span>') if n.get('category') else ''}
                {f'<span class="dot">·</span><a href="{escape(n["source_url"])}" target="_blank">source ↗</a>' if n.get('source_url') else ''}
              </div>
            </li>"""
            for n in news[:5]
        )
        slides.append(slide(
            "03 · Recent Highlights",
            f"""<h2 class="section-title">Recent Highlights</h2>
                <p class="lead-secondary">Material news from the last 90 days, prioritized by relevance to a Joveo conversation.</p>
                <ol class="news">{items}</ol>""",
        ))

    # ─── Past calls ──────────────────────────────────────────────────────────
    if avoma and (avoma.get("last_call_summary") or avoma.get("commitments") or avoma.get("open_threads")):
        # Cap each list at 7 items so the slide fits on one A4 landscape page even
        # when the model returns a long backlog from multiple transcripts. The
        # synthesis prompt also asks for the most important items first.
        commit_entries = [c for c in _as_list(avoma.get("commitments")) if isinstance(c, dict)][:7]
        open_threads = _as_list(avoma.get("open_threads"))[:7]

        def _render_commit(c: dict) -> str:
            by_html = f"<span class='by'>{escape(c.get('by', ''))}</span>"
            what_html = f"<span class='what'>{escape(c.get('what', ''))}</span>"
            due = c.get("due")
            due_html = f"<span class='due'>{escape(due)}</span>" if due else ""
            return f"<li>{by_html}{what_html}{due_html}</li>"

        commits = "".join(_render_commit(c) for c in commit_entries) or "<li class='empty'>No open commitments tracked</li>"
        sentiment = (avoma.get("sentiment") or "").lower()
        sentiment_pill_class = {"warm": "sent-warm", "neutral": "sent-neutral", "tense": "sent-tense"}.get(sentiment, "sent-neutral")
        slides.append(slide(
            "04 · Previous Discussion",
            f"""<h2 class="section-title">Previous Discussion</h2>
                <p class="lead">{escape(avoma.get('last_call_summary', '—'))}</p>
                <div class="cols-2">
                  <div class="takeaway-card">
                    <h4>Action Items</h4>
                    <ul class="commit-list">{commits}</ul>
                  </div>
                  <div class="takeaway-card">
                    <h4>Outstanding Items</h4>
                    {ul(open_threads)}
                  </div>
                </div>
                <div class="footer-meta">
                  <span class="sent-pill {sentiment_pill_class}">{escape(avoma.get('sentiment', '—'))}</span>
                  <span>Sentiment last call · {avoma.get('transcripts_used', 0)} transcript(s) analysed</span>
                </div>""",
        ))

    # ─── Talking points ──────────────────────────────────────────────────────
    tp_items = "".join(
        f"""<li>
          <div class="tp-point">{escape(tp.get('point', ''))}</div>
          <div class="tp-rationale">{escape(tp.get('rationale', ''))}{f' <span class="tag">{escape(tp["tied_to"])}</span>' if tp.get('tied_to') else ''}</div>
        </li>"""
        for tp in tps
    ) or "<li class='empty'>—</li>"
    slides.append(slide(
        "05 · Discussion Points",
        f"""<h2 class="section-title">Discussion Points</h2>
            <ol class="tps">{tp_items}</ol>""",
    ))

    # ─── Recommendations ─────────────────────────────────────────────────────
    rec_items = "".join(
        f"""<div class="rec">
          <div class="rec-title">{escape(r.get('title', ''))}</div>
          <div class="rec-row"><span class="rec-label">Why</span><span class="rec-text">{escape(r.get('rationale', ''))}</span></div>
          <div class="rec-row"><span class="rec-label good">Impact</span><span class="rec-text">{escape(r.get('expected_impact', ''))}</span></div>
          {f'<div class="rec-row warn"><span class="rec-label warn">Watch</span><span class="rec-text">{escape(r["risk"])}</span></div>' if r.get('risk') else ''}
        </div>"""
        for r in recs
    ) or '<p class="muted">No recommendations generated.</p>'
    slides.append(slide(
        "06 · Strategic Recommendations",
        f"""<h2 class="section-title">Strategic Recommendations</h2>
            <div class="recs">{rec_items}</div>""",
    ))

    # ─── Don't walk in blind ─────────────────────────────────────────────────
    if closes or risks:
        slides.append(slide(
            "07 · Key Considerations",
            f"""<h2 class="section-title">Key Considerations</h2>
                <div class="cols-2">
                  <div class="warn-block">
                    <h4>Items to Address</h4>
                    {ul(closes)}
                  </div>
                  <div class="risk-block">
                    <h4>Risks to Monitor</h4>
                    {ul(risks)}
                  </div>
                </div>""",
            content_class="blind",
        ))

    css = _DECK_CSS
    return f"""<!doctype html>
<html lang="en"><head>
  <meta charset="utf-8">
  <title>{escape(partner)} · Pre-call brief</title>
  <style>{css}</style>
</head>
<body>
  <div class="deck">{''.join(slides)}</div>
  <script>
    // Auto-trigger the browser's Print → Save as PDF flow when ?print=1 is set.
    // Used as a Vercel-friendly fallback for the Download PDF button when the
    // server-side Playwright renderer isn't available.
    (function() {{
      if (!location.search.includes('print=1')) return;
      const fire = () => setTimeout(() => window.print(), 250);
      if (document.fonts && document.fonts.ready) {{
        document.fonts.ready.then(fire);
      }} else {{
        window.addEventListener('load', fire);
      }}
    }})();
  </script>
</body></html>"""


# Pulled out so the f-string for render_deck stays readable. CSS is plain {{ }} -free here
# because it's a regular string (not f-string formatted).
_DECK_CSS = r"""
@import url("https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Lato:wght@400;500;600;700&family=Poppins:wght@500;600;700;800&display=swap");

@page {
  size: A4 landscape;
  margin: 0;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

html, body {
  background: #e7e7ee;
  font-family: Lato, -apple-system, BlinkMacSystemFont, sans-serif;
  color: #11111a;
  -webkit-font-smoothing: antialiased;
}

.deck {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12mm;
  padding: 12mm 0;
}

/* Each slide is a fixed A4 landscape page (297mm × 210mm). */
.slide {
  width: 297mm;
  height: 210mm;
  background: white;
  page-break-after: always;
  position: relative;
  overflow: hidden;
  display: grid;
  grid-template-rows: 14mm 1fr 10mm;
  box-shadow: 0 4px 16px rgba(15, 15, 30, 0.08);
}
.slide:last-child { page-break-after: auto; }

/* Header strip (kicker · partner · tier pill) */
.slide-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0 18mm;
  border-bottom: 1px solid #ececf2;
}
.slide-head-left { display: flex; align-items: center; gap: 10px; }
.slide-head-right { display: flex; align-items: center; gap: 10px; }
.kicker {
  font-family: Poppins, sans-serif;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: #5454BF;
}
.head-partner {
  font-family: Poppins, sans-serif;
  font-size: 12px;
  font-weight: 700;
  color: #1a1a24;
  letter-spacing: 0.02em;
}

/* Body — the actual slide content */
.slide-body {
  padding: 8mm 18mm 5mm;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

/* Footer strip (page number · attribution) */
.slide-foot {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0 18mm;
  border-top: 1px solid #ececf2;
  font-family: "DM Mono", monospace;
  font-size: 9px;
  color: #888;
  letter-spacing: 0.06em;
}
.page-num {
  font-family: "DM Mono", monospace;
  font-weight: 500;
  color: #11111a;
}

/* ─── Section title ─────────────────────────────────────────────────────── */
.section-title {
  font-family: Poppins, sans-serif;
  font-size: 32px;
  font-weight: 800;
  letter-spacing: -0.015em;
  margin-bottom: 10px;
  padding-bottom: 8px;
  border-bottom: 1px solid #ececf2;
  color: #202058;
}
.lead {
  font-size: 15px;
  line-height: 1.45;
  color: #2a2a36;
  margin-bottom: 8mm;
  max-width: 95%;
}
.lead-secondary {
  font-size: 12px;
  line-height: 1.45;
  color: #666;
  margin-bottom: 8px;
  max-width: 90%;
}
.body-prose {
  font-size: 14px;
  line-height: 1.55;
  color: #2a2a36;
}

/* ─── Sub-headings ──────────────────────────────────────────────────────── */
h4 {
  font-family: Poppins, sans-serif;
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: #5454BF;
  margin-bottom: 8px;
  padding-bottom: 4px;
  border-bottom: 1px solid #f0f0f5;
}

/* ─── Lists ─────────────────────────────────────────────────────────────── */
.bullet {
  list-style: none;
  padding: 0;
}
.bullet li {
  position: relative;
  padding: 5px 0 5px 18px;
  font-size: 14px;
  line-height: 1.5;
  color: #2a2a36;
}
.bullet li::before {
  content: "→";
  position: absolute;
  left: 0;
  top: 4px;
  line-height: 1.5;
  color: #5454BF;
  font-weight: 700;
}
.bullet li.empty {
  color: #aaa;
  font-style: italic;
}
.bullet li.empty::before { content: "—"; color: #aaa; }

/* ─── Two/three column layouts ──────────────────────────────────────────── */
.cols-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14mm; min-height: 0; align-items: start; }
.cols-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10mm; margin-top: 8mm; align-items: start; }

/* ─── Tier pill (in slide head) ─────────────────────────────────────────── */
.tier-pill {
  display: inline-block;
  padding: 3px 10px;
  border-radius: 4px;
  font-family: Poppins, sans-serif;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.1em;
}
.tier-p0 { background: rgba(248, 113, 113, 0.15); color: #c14b4b; }
.tier-p1 { background: rgba(251, 191, 36, 0.18); color: #a06c00; }
.tier-p2 { background: rgba(96, 165, 250, 0.15); color: #2b5fb1; }
.tier-unlisted { background: #efeff5; color: #888; }

/* ─── Cover slide ───────────────────────────────────────────────────────── */
.slide.cover {
  background: linear-gradient(135deg, #0a0a2e 0%, #15154a 50%, #202058 100%);
  color: white;
  position: relative;
  grid-template-rows: 14mm 1fr 10mm;
}
.cover-aurora {
  position: absolute;
  top: -20%; right: -10%;
  width: 70%; height: 80%;
  background: radial-gradient(circle, rgba(84, 84, 191, 0.45) 0%, transparent 60%);
  pointer-events: none;
  filter: blur(40px);
}
.slide.cover .slide-head { border-bottom: 0; }
.slide.cover .slide-foot {
  border-top: 1px solid rgba(255,255,255,0.08);
  color: #999;
}
.slide.cover .slide-foot .page-num { color: #ccc; }
.cover-body {
  padding: 0 30mm;
  display: flex;
  flex-direction: column;
  justify-content: center;
  position: relative;
  z-index: 1;
}
.cover-eyebrow {
  font-family: "DM Mono", monospace;
  font-size: 12px;
  letter-spacing: 0.3em;
  color: #888;
  margin-bottom: 16px;
}
.cover-title {
  font-family: Poppins, sans-serif;
  font-size: 110px;
  font-weight: 800;
  letter-spacing: -0.025em;
  line-height: 0.95;
  color: white;
  margin-bottom: 24px;
}
.cover-meta {
  display: flex;
  align-items: center;
  gap: 14px;
}
.cover-meta-text {
  font-size: 18px;
  color: #ccc;
}

/* ─── Performance blocks ────────────────────────────────────────────────── */
.perf-block { margin: 4mm 0 3mm; }
.perf-block-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 2mm;
  border-bottom: 1px solid #f0f0f5;
  padding-bottom: 2mm;
}
.perf-block-title {
  font-family: Poppins, sans-serif;
  font-size: 14px;
  font-weight: 700;
  color: #1a1a24;
}
.perf-block-meta {
  font-size: 11px;
  color: #888;
  margin-left: auto;
}
.cur-pill {
  display: inline-block;
  padding: 2px 8px;
  background: #5454BF15;
  color: #5454BF;
  font-family: "DM Mono", monospace;
  font-size: 10px;
  font-weight: 600;
  border-radius: 3px;
  letter-spacing: 0.04em;
}

.metric-row {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 3mm;
  margin-bottom: 3mm;
}
.metric {
  background: #f6f6fa;
  border-radius: 5px;
  padding: 6px 9px;
  border-left: 3px solid #5454BF;
}
.metric-label {
  font-family: "DM Mono", monospace;
  font-size: 9px;
  color: #888;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  margin-bottom: 2px;
}
.metric-value {
  font-family: Poppins, sans-serif;
  font-size: 16px;
  font-weight: 700;
  color: #1a1a24;
  letter-spacing: -0.01em;
}

table.top-clients,
table.compare-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 11px;
  margin-top: 2mm;
}
table.top-clients th,
table.compare-table th {
  font-family: Poppins, sans-serif;
  font-size: 9px;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: #888;
  text-align: left;
  padding: 5px 8px;
  border-bottom: 1px solid #ececf2;
  font-weight: 700;
}
table.top-clients td,
table.compare-table td {
  padding: 4px 8px;
  border-bottom: 1px solid #f5f5f8;
  color: #2a2a36;
  font-weight: 500;
}
table.top-clients td.client,
table.compare-table td.file { font-weight: 600; color: #1a1a24; }
table.compare-table td.file { display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap; }
.cur-pill.compact {
  padding: 1px 5px;
  font-size: 9px;
}
.active-meta {
  font-size: 9px;
  color: #888;
  font-weight: 400;
}

/* Tighter takeaways grid for multi-CSV slide where vertical room is scarce. */
.perf-takeaways { margin-top: 5mm !important; gap: 6mm !important; }
.perf-takeaways .bullet li { padding: 3px 0 3px 16px; font-size: 12.5px; }

/* 3+ uploads: comparison table + top-clients + 3 takeaway cards otherwise spill
   past the 173mm body. Squeeze every band a little. */
.perf-slide-tight .lead { margin-bottom: 4mm; font-size: 13.5px; }
.perf-slide-tight .perf-block { margin: 2mm 0; }
.perf-slide-tight .perf-block-head { margin-bottom: 1mm; padding-bottom: 1mm; }
.perf-slide-tight table.top-clients td,
.perf-slide-tight table.compare-table td { padding: 3px 8px; }
.perf-slide-tight .perf-takeaways { margin-top: 7mm !important; gap: 5mm !important; }
.perf-slide-tight .takeaway-card { padding: 3.5mm; }
.perf-slide-tight .takeaway-card h4 { font-size: 10px; margin-bottom: 4px; }
.perf-slide-tight .perf-takeaways .bullet li { padding: 2px 0 2px 14px; font-size: 12px; line-height: 1.4; }

/* Card wrapper around each takeaway column — matches the Recommendations cards. */
.takeaway-card {
  background: linear-gradient(135deg, #fafafe 0%, #f6f6fa 100%);
  border-radius: 8px;
  padding: 5mm;
  border-top: 3px solid #5454BF;
}
.takeaway-card h4 {
  border-bottom: none;
  padding-bottom: 0;
  margin-bottom: 6px;
}

/* ─── News ──────────────────────────────────────────────────────────────── */
.news {
  list-style: none;
  padding: 0;
  counter-reset: news;
  flex: 1;
  min-height: 0;
}
.news li {
  position: relative;
  padding: 4mm 0 4mm 13mm;
  border-bottom: 1px solid #f0f0f5;
  counter-increment: news;
}
.news li:last-child { border-bottom: none; }
.news li::before {
  content: counter(news, decimal-leading-zero);
  position: absolute;
  left: 0;
  top: 4mm;
  font-family: Poppins, sans-serif;
  font-size: 18px;
  font-weight: 800;
  color: #5454BF;
}
.news-headline {
  font-family: Poppins, sans-serif;
  font-size: 14px;
  font-weight: 700;
  color: #1a1a24;
  line-height: 1.3;
  margin-bottom: 2px;
}
.news-detail {
  font-size: 11px;
  line-height: 1.4;
  color: #444;
  margin-bottom: 3px;
  /* Cap detail at 2 lines so a wordy item can't push the next item off the page. */
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.news-meta {
  font-size: 9px;
  color: #888;
  display: flex;
  align-items: center;
  gap: 6px;
  font-family: "DM Mono", monospace;
}
.news-meta a { color: #5454BF; text-decoration: none; }
.news-meta .dot { color: #ccc; }

/* ─── Talking points ────────────────────────────────────────────────────── */
.tps {
  list-style: none;
  padding: 0;
  counter-reset: tp;
  flex: 1;
  min-height: 0;
}
.tps li {
  position: relative;
  padding: 4mm 0 4mm 13mm;
  border-bottom: 1px solid #f0f0f5;
  counter-increment: tp;
}
.tps li:last-child { border-bottom: none; }
.tps li::before {
  content: counter(tp, decimal-leading-zero);
  position: absolute;
  left: 0;
  top: 4mm;
  font-family: Poppins, sans-serif;
  font-size: 20px;
  font-weight: 800;
  color: #5454BF;
}
.tp-point {
  font-size: 16px;
  font-weight: 600;
  color: #1a1a24;
  margin-bottom: 3px;
  line-height: 1.4;
}
.tp-rationale {
  font-size: 13px;
  color: #666;
  line-height: 1.45;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.tag {
  display: inline-block;
  padding: 1px 6px;
  background: #5454BF15;
  color: #5454BF;
  border-radius: 3px;
  font-family: "DM Mono", monospace;
  font-size: 10px;
  letter-spacing: 0.04em;
  margin-left: 4px;
}

/* ─── Past calls (Avoma) ────────────────────────────────────────────────── */
.commit-list {
  list-style: none;
  padding: 0;
}
.commit-list li {
  display: grid;
  grid-template-columns: 60px 1fr auto;
  gap: 8px;
  padding: 4px 0;
  border-bottom: 1px solid #f5f5f8;
  align-items: baseline;
  font-size: 11.5px;
  line-height: 1.35;
}
.commit-list li:last-child { border-bottom: none; }
.commit-list .by {
  font-family: Poppins, sans-serif;
  font-weight: 700;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: #5454BF;
}
.commit-list .what { color: #2a2a36; line-height: 1.4; }
.commit-list .due {
  font-family: "DM Mono", monospace;
  font-size: 10px;
  color: #888;
  white-space: nowrap;
}
.commit-list .empty { color: #aaa; font-style: italic; grid-column: 1 / -1; }

.footer-meta {
  margin-top: 6mm;
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 11px;
  color: #888;
}
.sent-pill {
  display: inline-block;
  padding: 3px 10px;
  border-radius: 4px;
  font-family: Poppins, sans-serif;
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.1em;
}
.sent-warm { background: rgba(52, 211, 153, 0.15); color: #0c8456; }
.sent-neutral { background: #efeff5; color: #666; }
.sent-tense { background: rgba(248, 113, 113, 0.15); color: #c14b4b; }

/* ─── Recommendations ───────────────────────────────────────────────────── */
.recs {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 5mm;
  margin-top: 4mm;
}
.rec {
  background: linear-gradient(135deg, #fafafe 0%, #f6f6fa 100%);
  border-radius: 8px;
  padding: 5mm;
  border-top: 3px solid #5454BF;
}
.rec-title {
  font-family: Poppins, sans-serif;
  font-size: 17px;
  font-weight: 700;
  color: #1a1a24;
  margin-bottom: 8px;
  line-height: 1.3;
}
.rec-row {
  display: grid;
  grid-template-columns: 60px 1fr;
  gap: 8px;
  margin: 5px 0;
  align-items: baseline;
  font-size: 13px;
  line-height: 1.5;
}
.rec-label {
  font-family: Poppins, sans-serif;
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: #888;
}
.rec-label.good { color: #0c8456; }
.rec-label.warn { color: #c14b4b; }
.rec-text { color: #2a2a36; }
.rec-row.warn .rec-text { color: #8a3838; }

/* ─── Don't walk in blind ───────────────────────────────────────────────── */
.warn-block, .risk-block {
  background: #fafafe;
  border-radius: 8px;
  padding: 5mm 6mm;
}
.warn-block {
  border-left: 3px solid #fbbf24;
}
.warn-block h4 { color: #a06c00; }
.risk-block {
  border-left: 3px solid #f87171;
}
.risk-block h4 { color: #c14b4b; }

.muted { color: #888; font-size: 12px; font-style: italic; }

/* ─── Print ─────────────────────────────────────────────────────────────── */
@media print {
  @page {
    size: A4 landscape;
    margin: 0;
  }
  html, body { background: white; }
  .deck { gap: 0; padding: 0; }
  .slide { box-shadow: none; }
}
"""
