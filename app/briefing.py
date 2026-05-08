"""Briefing orchestrator: Gather → Synthesize → store."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from app.prompts import NEWS_SYSTEM, SYNTHESIZE_SYSTEM
from app.services import call_gemini_json, get_avoma_context
from app import storage

log = logging.getLogger(__name__)


def _gather_news(partner: str) -> dict:
    try:
        return call_gemini_json(
            system=NEWS_SYSTEM,
            user=(
                f"Partner: {partner}. Today's date: {datetime.now().strftime('%a %b %d %Y')}. "
                f"Use Google Search to find recent news. Return ONLY the JSON object specified."
            ),
            use_search=True,
            max_tokens=2000,
        )
    except Exception as e:
        log.warning(f"News gather failed for {partner}: {e}")
        return {"news": [], "linkedin_signal": ""}


def _gather_avoma(partner: str) -> dict | None:
    try:
        return get_avoma_context(partner)
    except Exception as e:
        log.warning(f"Avoma gather failed for {partner}: {e}")
        return None


def _gather_performance(partner: str) -> list[dict]:
    """Return all recent performance uploads for the partner.
    Multiple CSVs are common when the partner runs in several currencies — each
    upload is kept as a separate feed and the agent reasons about them individually."""
    try:
        return storage.recent_perf(partner)
    except Exception as e:
        log.warning(f"Performance gather failed for {partner}: {e}")
        return []


def _gather_notes(partner: str) -> list[dict]:
    try:
        notes = storage.list_notes(partner)
        return [
            {"kind": n.get("kind"), "title": n.get("title"), "body": n.get("body"), "created_at": n.get("created_at")}
            for n in notes
        ]
    except Exception as e:
        log.warning(f"Notes gather failed for {partner}: {e}")
        return []


def _build_synthesis_input(*, partner: str, call: dict | None, news: dict, avoma: dict | None, performance: list[dict], notes: list[dict]) -> str:
    if not performance:
        perf_block = "(no performance uploads found for this partner)"
    else:
        perf_block = (
            f"{len(performance)} upload(s). Each may be a different currency or period — read filenames for hints "
            f"(e.g. '..._USD.csv'). Do NOT sum metrics across uploads unless they're clearly the same currency.\n"
            + json.dumps(performance, indent=2)
        )
    return "\n\n".join([
        f"CALL\n  partner: {partner}\n  time: {(call or {}).get('time', 'unknown')}\n  title: {(call or {}).get('title', '')}",
        f"NEWS\n{json.dumps(news, indent=2)}",
        f"AVOMA\n{json.dumps(avoma, indent=2) if avoma else '(no transcripts available)'}",
        f"PERFORMANCE\n{perf_block}",
        f"NOTES\n{json.dumps(notes, indent=2) if notes else '(no internal notes saved)'}",
    ])


async def run_briefing(*, partner: str, call: dict | None = None, user: dict | None = None) -> dict[str, Any]:
    call = call or {}
    # Fan out the four independent I/O calls in parallel — gather is dominated by the
    # news + transcript Gemini calls (~10s each); running them concurrently saves ~15s.
    news, avoma, performance, notes = await asyncio.gather(
        asyncio.to_thread(_gather_news, partner),
        asyncio.to_thread(_gather_avoma, partner),
        asyncio.to_thread(_gather_performance, partner),
        asyncio.to_thread(_gather_notes, partner),
    )

    inputs = {
        "partner": partner,
        "call": call,
        "news": news,
        "avoma": avoma,
        "performance": performance,
        "notes": notes,
        # Stash who generated this briefing so the Past Briefings tab can show it.
        "generated_by": (
            {"email": user.get("email"), "name": user.get("name")}
            if user else None
        ),
    }
    # Strip non-synthesis fields before building the model prompt — Gemini only cares about the call signals.
    user_msg = _build_synthesis_input(
        partner=partner, call=call, news=news, avoma=avoma, performance=performance, notes=notes,
    )

    synthesized = await asyncio.to_thread(
        call_gemini_json,
        system=SYNTHESIZE_SYSTEM,
        user=user_msg,
        json_mode=True,
        max_tokens=3500,
    )

    # `performance_summaries` is the full list (one entry per uploaded CSV);
    # `performance_summary` points at the most recent one for single-view deck rendering.
    perf_summaries = [
        {"filename": p.get("filename"), "summary": p.get("summary"), "created_at": p.get("created_at")}
        for p in (performance or [])
    ]
    output = {
        **synthesized,
        "news": news.get("news") or [],
        "linkedin_signal": news.get("linkedin_signal") or "",
        "avoma_summary": (
            {
                "last_call_summary": avoma.get("last_call_summary"),
                "commitments": avoma.get("commitments"),
                "open_threads": avoma.get("open_threads"),
                "sentiment": avoma.get("sentiment"),
                "transcripts_used": avoma.get("transcripts_used"),
            }
            if avoma else None
        ),
        "performance_summary": perf_summaries[0]["summary"] if perf_summaries else None,
        "performance_summaries": perf_summaries,
    }

    briefing_id = storage.insert_briefing(partner, call, inputs, output)
    return {"id": briefing_id, "partner": partner, "call": call, "output": output}
