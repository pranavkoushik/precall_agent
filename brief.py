"""Local CLI: generate a briefing for a single partner without spinning up the web server.

Usage:
    python brief.py "Indeed"
    python brief.py "LinkedIn Talent Solutions" --time 14:30 --title "Quarterly Review"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.briefing import run_briefing


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a pre-call briefing for one partner.")
    parser.add_argument("partner", help="Partner / publisher name")
    parser.add_argument("--time", default="", help="Call time HH:MM")
    parser.add_argument("--title", default="", help="Calendar event title")
    parser.add_argument("--out", default=None, help="Write JSON output to this file (default: stdout)")
    args = parser.parse_args()

    call = {"time": args.time, "title": args.title, "partner": args.partner}
    result = asyncio.run(run_briefing(partner=args.partner, call=call))

    payload = json.dumps(result, indent=2, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(payload)
        print(f"Wrote briefing #{result['id']} → {args.out}", file=sys.stderr)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
