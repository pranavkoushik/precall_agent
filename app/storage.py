"""Google Sheets persistence layer. Replaces SQLite from the Node version.

Six worksheets are auto-created on first use:
  - notes        | id | partner | kind | title | body | created_at
  - performance  | id | partner | filename | period_start | period_end | summary_json | created_at
  - transcripts  | id | partner | filename | call_date | call_title | content | truncated | created_at
  - briefings    | id | partner | call_time | call_title | status | inputs_json | output_json | edits_json | approved_at | sent_at | created_at
  - oauth_tokens | email | name | picture | token_json | updated_at | created_at
  - audit_log    | id | actor_email | actor_name | action | target_type | target_id | details_json | created_at
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

from app.config import settings

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

TAB_NOTES = "notes"
TAB_PERF = "performance"
TAB_TRANSCRIPTS = "transcripts"
TAB_BRIEFINGS = "briefings"
TAB_OAUTH = "oauth_tokens"
TAB_AUDIT = "audit_log"

NOTES_HEADERS = ["id", "partner", "kind", "title", "body", "created_at"]
PERF_HEADERS = ["id", "partner", "filename", "period_start", "period_end", "summary_json", "created_at"]
TRANSCRIPT_HEADERS = ["id", "partner", "filename", "call_date", "call_title", "content", "truncated", "created_at"]
BRIEFING_HEADERS = [
    "id", "partner", "call_time", "call_title", "status",
    "inputs_json", "output_json", "edits_json",
    "approved_at", "sent_at", "created_at",
]
OAUTH_HEADERS = ["email", "name", "picture", "token_json", "updated_at", "created_at"]
AUDIT_HEADERS = [
    "id", "actor_email", "actor_name", "action", "target_type", "target_id",
    "details_json", "created_at",
]

# Sheets cell hard limit is 50,000 chars. Cap transcript content at 45K to leave headroom.
TRANSCRIPT_CHAR_LIMIT = 45_000
# Max transcripts kept per partner. Older ones are auto-deleted on insert.
MAX_TRANSCRIPTS_PER_PARTNER = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@lru_cache(maxsize=1)
def _client() -> gspread.Client:
    if not settings.google_credentials_json:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON not set")
    creds_info = json.loads(settings.google_credentials_json)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return gspread.authorize(creds)


@lru_cache(maxsize=1)
def _spreadsheet():
    if not settings.google_sheet_id:
        raise RuntimeError("GOOGLE_SHEET_ID not set")
    sh = _client().open_by_key(settings.google_sheet_id)
    _ensure_tabs(sh)
    return sh


def _ensure_tabs(sh) -> None:
    existing = {ws.title for ws in sh.worksheets()}
    for tab, headers in (
        (TAB_NOTES, NOTES_HEADERS),
        (TAB_PERF, PERF_HEADERS),
        (TAB_TRANSCRIPTS, TRANSCRIPT_HEADERS),
        (TAB_BRIEFINGS, BRIEFING_HEADERS),
        (TAB_OAUTH, OAUTH_HEADERS),
        (TAB_AUDIT, AUDIT_HEADERS),
    ):
        if tab not in existing:
            ws = sh.add_worksheet(title=tab, rows=1000, cols=len(headers))
            ws.append_row(headers, value_input_option="RAW")


def _ws(name: str):
    return _spreadsheet().worksheet(name)


def _next_id(ws) -> int:
    ids = ws.col_values(1)[1:]  # skip header
    nums = [int(x) for x in ids if x.isdigit()]
    return (max(nums) + 1) if nums else 1


def _records(ws, partner: str | None = None) -> list[dict]:
    rows = ws.get_all_records()
    if partner is not None:
        rows = [r for r in rows if str(r.get("partner")) == partner]
    return rows


# ─── Notes ────────────────────────────────────────────────────────────────────
def insert_note(partner: str, kind: str, title: str | None, body: str) -> dict:
    ws = _ws(TAB_NOTES)
    nid = _next_id(ws)
    created = _now()
    ws.append_row([nid, partner, kind, title or "", body, created], value_input_option="RAW")
    return {"id": nid, "partner": partner, "kind": kind, "title": title, "body": body, "created_at": created}


def list_notes(partner: str) -> list[dict]:
    rows = _records(_ws(TAB_NOTES), partner)
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return rows


def delete_note(note_id: int) -> bool:
    ws = _ws(TAB_NOTES)
    try:
        cell = ws.find(str(note_id), in_column=1)
    except gspread.exceptions.CellNotFound:
        return False
    if cell:
        ws.delete_rows(cell.row)
        return True
    return False


# ─── Performance ──────────────────────────────────────────────────────────────
def insert_perf(partner: str, filename: str, summary: dict) -> dict:
    ws = _ws(TAB_PERF)
    pid = _next_id(ws)
    created = _now()
    ws.append_row([
        pid, partner, filename,
        summary.get("period_start") or "",
        summary.get("period_end") or "",
        json.dumps(summary),
        created,
    ], value_input_option="RAW")
    return {
        "id": pid, "partner": partner, "filename": filename,
        "period_start": summary.get("period_start"),
        "period_end": summary.get("period_end"),
        "summary": summary,
        "created_at": created,
    }


def list_perf(partner: str) -> list[dict]:
    rows = _records(_ws(TAB_PERF), partner)
    out = []
    for r in rows:
        out.append({
            **r,
            "summary": json.loads(r["summary_json"]) if r.get("summary_json") else None,
        })
    out.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return out


def latest_perf(partner: str) -> dict | None:
    rows = list_perf(partner)
    if not rows:
        return None
    r = rows[0]
    return {
        "filename": r.get("filename"),
        "period_start": r.get("period_start"),
        "period_end": r.get("period_end"),
        "summary": r.get("summary"),
    }


def recent_perf(partner: str, limit: int = 6) -> list[dict]:
    """Return up to `limit` most-recent performance uploads for a partner.
    Used when a partner has multiple CSVs (e.g. one per currency)."""
    rows = list_perf(partner)[:limit]
    return [
        {
            "filename": r.get("filename"),
            "period_start": r.get("period_start"),
            "period_end": r.get("period_end"),
            "summary": r.get("summary"),
            "created_at": r.get("created_at"),
        }
        for r in rows
    ]


def delete_perf(perf_id: int) -> bool:
    ws = _ws(TAB_PERF)
    try:
        cell = ws.find(str(perf_id), in_column=1)
    except gspread.exceptions.CellNotFound:
        return False
    if cell:
        ws.delete_rows(cell.row)
        return True
    return False


def _clear_data_rows(ws) -> int:
    """Delete every data row (everything below the header). Returns rows deleted."""
    total = len(ws.col_values(1))
    if total <= 1:
        return 0
    ws.delete_rows(2, total)
    return total - 1


def clear_all_uploads() -> dict:
    """Wipe both the performance and transcripts worksheets. Used on page reload
    so every session starts with a clean slate per the rep's workflow."""
    perf_cleared = _clear_data_rows(_ws(TAB_PERF))
    transcripts_cleared = _clear_data_rows(_ws(TAB_TRANSCRIPTS))
    return {"performance_rows_deleted": perf_cleared, "transcripts_rows_deleted": transcripts_cleared}


# ─── Briefings ────────────────────────────────────────────────────────────────
def insert_briefing(partner: str, call: dict | None, inputs: dict, output: dict) -> int:
    ws = _ws(TAB_BRIEFINGS)
    bid = _next_id(ws)
    ws.append_row([
        bid, partner,
        (call or {}).get("time", ""),
        (call or {}).get("title", ""),
        "draft",
        json.dumps(inputs),
        json.dumps(output),
        "",  # edits_json
        "",  # approved_at
        "",  # sent_at
        _now(),
    ], value_input_option="RAW")
    return bid


def _find_briefing_row(ws, briefing_id: int):
    cell = ws.find(str(briefing_id), in_column=1)
    return cell.row if cell else None


def get_briefing(briefing_id: int) -> dict | None:
    ws = _ws(TAB_BRIEFINGS)
    row_idx = _find_briefing_row(ws, briefing_id)
    if not row_idx:
        return None
    values = ws.row_values(row_idx)
    record = dict(zip(BRIEFING_HEADERS, values + [""] * (len(BRIEFING_HEADERS) - len(values))))
    return {
        "id": int(record["id"]) if str(record["id"]).isdigit() else record["id"],
        "partner": record["partner"],
        "call": {"time": record["call_time"], "title": record["call_title"], "partner": record["partner"]},
        "status": record["status"] or "draft",
        "output": json.loads(record["output_json"]) if record["output_json"] else None,
        "edits": json.loads(record["edits_json"]) if record["edits_json"] else None,
        "approved_at": record["approved_at"] or None,
        "sent_at": record["sent_at"] or None,
        "created_at": record["created_at"],
    }


def update_briefing_edits(briefing_id: int, edits: dict) -> None:
    ws = _ws(TAB_BRIEFINGS)
    row_idx = _find_briefing_row(ws, briefing_id)
    if not row_idx:
        raise ValueError(f"Briefing {briefing_id} not found")
    col = BRIEFING_HEADERS.index("edits_json") + 1
    ws.update_cell(row_idx, col, json.dumps(edits))


def approve_briefing(briefing_id: int, edits: dict | None = None) -> None:
    ws = _ws(TAB_BRIEFINGS)
    row_idx = _find_briefing_row(ws, briefing_id)
    if not row_idx:
        raise ValueError(f"Briefing {briefing_id} not found")
    status_col = BRIEFING_HEADERS.index("status") + 1
    edits_col = BRIEFING_HEADERS.index("edits_json") + 1
    approved_col = BRIEFING_HEADERS.index("approved_at") + 1
    updates = [
        {"range": gspread.utils.rowcol_to_a1(row_idx, status_col), "values": [["approved"]]},
        {"range": gspread.utils.rowcol_to_a1(row_idx, approved_col), "values": [[_now()]]},
    ]
    if edits is not None:
        updates.append({
            "range": gspread.utils.rowcol_to_a1(row_idx, edits_col),
            "values": [[json.dumps(edits)]],
        })
    ws.batch_update(updates, value_input_option="RAW")


def mark_briefing_sent(briefing_id: int) -> None:
    ws = _ws(TAB_BRIEFINGS)
    row_idx = _find_briefing_row(ws, briefing_id)
    if not row_idx:
        raise ValueError(f"Briefing {briefing_id} not found")
    status_col = BRIEFING_HEADERS.index("status") + 1
    sent_col = BRIEFING_HEADERS.index("sent_at") + 1
    ws.batch_update([
        {"range": gspread.utils.rowcol_to_a1(row_idx, status_col), "values": [["sent"]]},
        {"range": gspread.utils.rowcol_to_a1(row_idx, sent_col), "values": [[_now()]]},
    ], value_input_option="RAW")


def list_all_briefings(limit: int = 200) -> list[dict]:
    """Return every briefing, newest first, with a light projection suitable for the
    Past Briefings tab (no full inputs/output blobs — just preview fields)."""
    ws = _ws(TAB_BRIEFINGS)
    rows = ws.get_all_records()
    out = []
    for r in rows:
        try:
            inputs = json.loads(r.get("inputs_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            inputs = {}
        try:
            output = json.loads(r.get("output_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            output = {}
        try:
            edits = json.loads(r.get("edits_json") or "{}") if r.get("edits_json") else {}
        except (json.JSONDecodeError, TypeError):
            edits = {}
        # Edits override output for display.
        snapshot = (edits.get("account_snapshot") or output.get("account_snapshot") or {}) if (edits or output) else {}
        out.append({
            "id": r.get("id"),
            "partner": r.get("partner"),
            "call_time": r.get("call_time") or None,
            "call_title": r.get("call_title") or None,
            "tier": (inputs.get("call") or {}).get("tier") or None,
            "status": r.get("status") or "draft",
            "approved_at": r.get("approved_at") or None,
            "sent_at": r.get("sent_at") or None,
            "created_at": r.get("created_at"),
            "generated_by": inputs.get("generated_by") or None,
            "snapshot_headline": snapshot.get("headline") if isinstance(snapshot, dict) else None,
            "talking_points_count": len(output.get("talking_points") or []) if isinstance(output, dict) else 0,
            "recommendations_count": len(output.get("recommendations") or []) if isinstance(output, dict) else 0,
        })
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return out[:limit]


# ─── Transcripts ──────────────────────────────────────────────────────────────
def insert_transcript(
    partner: str,
    filename: str,
    content: str,
    call_date: str | None = None,
    call_title: str | None = None,
) -> dict:
    """Store a transcript. If the partner already has the max allowed, the oldest is auto-deleted.
    Returns the saved record plus a list of any deleted (auto-pruned) transcript ids."""
    truncated = len(content) > TRANSCRIPT_CHAR_LIMIT
    stored_content = content[:TRANSCRIPT_CHAR_LIMIT] if truncated else content

    ws = _ws(TAB_TRANSCRIPTS)
    tid = _next_id(ws)
    created = _now()
    ws.append_row(
        [tid, partner, filename, call_date or "", call_title or "", stored_content,
         "yes" if truncated else "", created],
        value_input_option="RAW",
    )

    existing = list_transcripts(partner)
    deleted: list[int] = []
    if len(existing) > MAX_TRANSCRIPTS_PER_PARTNER:
        for old in existing[MAX_TRANSCRIPTS_PER_PARTNER:]:
            old_id = int(old["id"])
            if delete_transcript(old_id):
                deleted.append(old_id)

    return {
        "id": tid,
        "partner": partner,
        "filename": filename,
        "call_date": call_date,
        "call_title": call_title,
        "content_length": len(stored_content),
        "truncated": truncated,
        "created_at": created,
        "deleted_ids": deleted,
    }


def list_transcripts(partner: str, *, include_content: bool = False) -> list[dict]:
    """List transcripts for a partner, sorted newest-first by call_date (or created_at).
    `content` is omitted by default to keep responses light."""
    rows = _records(_ws(TAB_TRANSCRIPTS), partner)
    out = []
    for r in rows:
        item = {
            "id": int(r["id"]) if str(r.get("id")).isdigit() else r.get("id"),
            "partner": r.get("partner"),
            "filename": r.get("filename"),
            "call_date": r.get("call_date") or None,
            "call_title": r.get("call_title") or None,
            "truncated": (r.get("truncated") or "").strip().lower() in {"yes", "true", "1"},
            "created_at": r.get("created_at"),
        }
        if include_content:
            item["content"] = r.get("content") or ""
        else:
            item["content_length"] = len(r.get("content") or "")
        out.append(item)
    out.sort(key=lambda r: (r.get("call_date") or "", r.get("created_at") or ""), reverse=True)
    return out


def delete_transcript(transcript_id: int) -> bool:
    ws = _ws(TAB_TRANSCRIPTS)
    try:
        cell = ws.find(str(transcript_id), in_column=1)
    except gspread.exceptions.CellNotFound:
        return False
    if cell:
        ws.delete_rows(cell.row)
        return True
    return False


def transcripts_for_synthesis(partner: str, limit: int = MAX_TRANSCRIPTS_PER_PARTNER) -> list[dict]:
    """Return the most recent transcripts (with full content) ready to feed into Gemini."""
    rows = list_transcripts(partner, include_content=True)[:limit]
    return [
        {
            "title": r.get("call_title") or r.get("filename") or "Untitled",
            "start": r.get("call_date") or r.get("created_at"),
            "text": r.get("content") or "",
        }
        for r in rows
        if (r.get("content") or "").strip()
    ]


def effective_brief(briefing: dict) -> dict:
    """Merge edits over output. Top-level keys in edits win."""
    return {**(briefing.get("output") or {}), **(briefing.get("edits") or {})}


# OAuth users -----------------------------------------------------------------
def get_oauth_user(email: str) -> dict | None:
    ws = _ws(TAB_OAUTH)
    try:
        cell = ws.find(email, in_column=1)
    except gspread.exceptions.CellNotFound:
        return None
    if not cell:
        return None
    values = ws.row_values(cell.row)
    record = dict(zip(OAUTH_HEADERS, values + [""] * (len(OAUTH_HEADERS) - len(values))))
    return {
        "email": record["email"],
        "name": record["name"],
        "picture": record["picture"],
        "token": json.loads(record["token_json"]) if record.get("token_json") else {},
        "updated_at": record["updated_at"] or None,
        "created_at": record["created_at"] or None,
    }


def upsert_oauth_user(email: str, name: str, picture: str, token: dict) -> dict:
    ws = _ws(TAB_OAUTH)
    now = _now()
    row = None
    created_at = now
    try:
        cell = ws.find(email, in_column=1)
        row = cell.row if cell else None
    except gspread.exceptions.CellNotFound:
        row = None

    if row:
        existing = get_oauth_user(email) or {}
        created_at = existing.get("created_at") or now
        ws.update(
            f"A{row}:F{row}",
            [[email, name, picture, json.dumps(token), now, created_at]],
            value_input_option="RAW",
        )
    else:
        ws.append_row([email, name, picture, json.dumps(token), now, created_at], value_input_option="RAW")

    return {
        "email": email,
        "name": name,
        "picture": picture,
        "token": token,
        "updated_at": now,
        "created_at": created_at,
    }


# ─── Audit log ────────────────────────────────────────────────────────────────
def audit(actor: dict | None, action: str, target_type: str, target_id: Any, details: dict | None = None) -> None:
    """Append a row to the audit_log worksheet. Failures are swallowed so a Sheets hiccup
    never blocks the user-facing action.

    actor       — the OAuth session user dict (`email`, `name`) or None for system events
    action      — short verb: 'created' | 'edited' | 'approved' | 'sent_slack' | 'sent_email' | 'uploaded' | 'deleted'
    target_type — 'briefing' | 'note' | 'transcript' | 'performance'
    target_id   — entity id (int or str)
    details     — free-form JSON (e.g. which fields were edited)
    """
    try:
        ws = _ws(TAB_AUDIT)
        aid = _next_id(ws)
        ws.append_row(
            [
                aid,
                (actor or {}).get("email", "") or "",
                (actor or {}).get("name", "") or "",
                action,
                target_type,
                str(target_id) if target_id is not None else "",
                json.dumps(details) if details else "",
                _now(),
            ],
            value_input_option="RAW",
        )
    except Exception as e:
        log.warning(f"Audit log write failed: {e}")


def list_audit(target_type: str | None = None, target_id: Any | None = None, limit: int = 200) -> list[dict]:
    """Return audit rows, optionally filtered by target. Newest first."""
    ws = _ws(TAB_AUDIT)
    rows = ws.get_all_records()
    out = []
    for r in rows:
        if target_type and r.get("target_type") != target_type:
            continue
        if target_id is not None and str(r.get("target_id")) != str(target_id):
            continue
        out.append({
            "id": r.get("id"),
            "actor_email": r.get("actor_email") or None,
            "actor_name": r.get("actor_name") or None,
            "action": r.get("action"),
            "target_type": r.get("target_type"),
            "target_id": r.get("target_id"),
            "details": json.loads(r["details_json"]) if r.get("details_json") else None,
            "created_at": r.get("created_at"),
        })
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return out[:limit]
