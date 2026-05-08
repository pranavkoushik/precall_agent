"""Joveo supply-side publisher list, ported from the Publisher-Intel reference repo.

Tier mapping from source:
- Source `P0_PUBLISHERS`        -> P0 here (26 publishers, top strategic priority)
- Source `P1_P2_PUBLISHERS`     -> P1 here (113 publishers, active + long-tail combined)
- P2 is left empty as a placeholder; promote any P1 entries into P2 if you want
  to demote them to "monitoring only" without removing them.

The calendar matcher uses:
- `name` and `aliases` to match event titles (case-insensitive substring)
- `domains` to match attendee email domains

Domains are populated for the obvious cases. Add more as you encounter them in
real calendar events — domain matching is more reliable than name matching for
ambiguous brands (e.g. "Monster" the company vs "Monster" in any other context).
"""
from __future__ import annotations

from typing import Literal, TypedDict

Tier = Literal["P0", "P1", "P2"]


class Publisher(TypedDict, total=False):
    name: str
    tier: Tier
    aliases: list[str]
    domains: list[str]


PUBLISHERS: list[Publisher] = [
    # ─── P0 — top strategic partners (from Publisher-Intel P0_PUBLISHERS) ────
    {"name": "1840", "tier": "P0", "aliases": ["1840 & Company"], "domains": ["1840andcompany.com"]},
    {"name": "Allthetopbananas", "tier": "P0", "domains": ["allthetopbananas.com"]},
    {"name": "employers.io", "tier": "P0", "domains": ["employers.io"]},
    {"name": "Geographic Solutions", "tier": "P0", "domains": ["geographicsolutions.com"]},
    {"name": "Handshake", "tier": "P0", "domains": ["joinhandshake.com"]},
    {"name": "Hokify", "tier": "P0", "domains": ["hokify.com"]},
    {"name": "Indeed", "tier": "P0",
     "aliases": ["Indeed.com", "Indeed Hiring", "Indeed Sponsored Jobs"],
     "domains": ["indeed.com"]},
    {"name": "JobCloud", "tier": "P0", "domains": ["jobcloud.ch"]},
    {"name": "JobGet", "tier": "P0", "domains": ["jobget.com"]},
    {"name": "JobRapido", "tier": "P0", "aliases": ["Jobrapido"], "domains": ["jobrapido.com"]},
    {"name": "Jobbland", "tier": "P0", "domains": ["jobbland.se"]},
    {"name": "Jobbsafari.se", "tier": "P0", "aliases": ["Jobbsafari"], "domains": ["jobbsafari.se"]},
    {"name": "Jobcase", "tier": "P0", "domains": ["jobcase.com"]},
    {"name": "Joblift", "tier": "P0", "domains": ["joblift.com"]},
    {"name": "Jooble", "tier": "P0", "domains": ["jooble.org"]},
    {"name": "Monster", "tier": "P0", "aliases": ["Monster.com", "Monster Jobs"], "domains": ["monster.com"]},
    {"name": "Nurse.com", "tier": "P0", "domains": ["nurse.com"]},
    {"name": "OnTimeHire", "tier": "P0", "domains": ["ontimehire.com"]},
    {"name": "Reed", "tier": "P0", "aliases": ["Reed.co.uk"], "domains": ["reed.co.uk"]},
    {"name": "Sercanto", "tier": "P0", "domains": ["sercanto.com"]},
    {"name": "Snagajob", "tier": "P0", "domains": ["snagajob.com"]},
    {"name": "Talent.com", "tier": "P0", "aliases": ["Neuvoo"], "domains": ["talent.com", "neuvoo.com"]},
    {"name": "Talroo", "tier": "P0", "domains": ["talroo.com"]},
    {"name": "Upward.net", "tier": "P0", "aliases": ["Upward"], "domains": ["upward.net"]},
    {"name": "YadaJobs", "tier": "P0", "domains": ["yadajobs.com"]},
    {"name": "ZipRecruiter", "tier": "P0", "aliases": ["Zip", "Zip Recruiter"], "domains": ["ziprecruiter.com"]},

    # ─── P1 — active partners (from Publisher-Intel P1_P2_PUBLISHERS) ────────
    {"name": "Adzuna", "tier": "P1", "domains": ["adzuna.com"]},
    {"name": "adway.ai", "tier": "P1", "domains": ["adway.ai"]},
    {"name": "AllJobs", "tier": "P1", "domains": ["alljobs.co.il"]},
    {"name": "American Nurses Association", "tier": "P1", "aliases": ["ANA"], "domains": ["nursingworld.org"]},
    {"name": "AppJobs", "tier": "P1", "domains": ["appjobs.com"]},
    {"name": "Arbeitnow", "tier": "P1", "domains": ["arbeitnow.com"]},
    {"name": "Arya by Leoforce", "tier": "P1", "aliases": ["Arya", "Leoforce"], "domains": ["leoforce.com"]},
    {"name": "Bakeca.it", "tier": "P1", "aliases": ["Bakeca"], "domains": ["bakeca.it"]},
    {"name": "Botson.ai", "tier": "P1", "domains": ["botson.ai"]},
    {"name": "Bravado", "tier": "P1", "domains": ["bravado.co"]},
    {"name": "CareerCross", "tier": "P1", "domains": ["careercross.com"]},
    {"name": "Catho", "tier": "P1", "domains": ["catho.com.br"]},
    {"name": "CDLlife", "tier": "P1", "aliases": ["CDL Life"], "domains": ["cdllife.com"]},
    {"name": "ClickaJobs", "tier": "P1", "domains": ["clickajobs.com"]},
    {"name": "CMP Jobs", "tier": "P1", "domains": ["cmpjobs.com"]},
    {"name": "CollabWORK", "tier": "P1", "domains": ["collabwork.com"]},
    {"name": "Consultants 500", "tier": "P1", "domains": ["consultants500.com"]},
    {"name": "Craigslist", "tier": "P1", "domains": ["craigslist.org"]},
    {"name": "Curriculum", "tier": "P1"},
    {"name": "CV Library", "tier": "P1", "aliases": ["CV-Library"], "domains": ["cv-library.co.uk"]},
    {"name": "Daijob.com", "tier": "P1", "aliases": ["Daijob"], "domains": ["daijob.com"]},
    {"name": "deBanenSite.nl", "tier": "P1", "aliases": ["DeBanenSite"], "domains": ["debanensite.nl"]},
    {"name": "Dental Post", "tier": "P1", "aliases": ["DentalPost"], "domains": ["dentalpost.net"]},
    {"name": "Dice", "tier": "P1", "aliases": ["Dice.com"], "domains": ["dice.com"]},
    {"name": "Diversity Jobs", "tier": "P1", "aliases": ["DiversityJobs"], "domains": ["diversityjobs.com"]},
    {"name": "Doximity", "tier": "P1", "domains": ["doximity.com"]},
    {"name": "EarnBetter", "tier": "P1", "domains": ["earnbetter.com"]},
    {"name": "eFinancialCareers", "tier": "P1", "aliases": ["eFC"], "domains": ["efinancialcareers.com"]},
    {"name": "Expresso Emprego", "tier": "P1", "domains": ["expresso.pt"]},
    {"name": "Facebook", "tier": "P1", "aliases": ["Meta"], "domains": ["facebook.com", "meta.com"]},
    {"name": "FATj", "tier": "P1"},
    {"name": "Foh and Boh", "tier": "P1", "aliases": ["FOH&BOH"], "domains": ["fohandboh.com"]},
    {"name": "GaijinPot", "tier": "P1", "domains": ["gaijinpot.com"]},
    {"name": "Galois", "tier": "P1"},
    {"name": "Google Ads", "tier": "P1", "aliases": ["Google", "Google for Jobs"], "domains": ["google.com"]},
    {"name": "GoWork.pl", "tier": "P1", "aliases": ["GoWork"], "domains": ["gowork.pl"]},
    {"name": "Gumtree", "tier": "P1", "domains": ["gumtree.com"]},
    {"name": "Health Ecareers", "tier": "P1", "aliases": ["HealthECareers"], "domains": ["healthecareers.com"]},
    {"name": "HeyTempo", "tier": "P1", "domains": ["heytempo.com"]},
    {"name": "HH.ru", "tier": "P1", "aliases": ["HeadHunter"], "domains": ["hh.ru"]},
    {"name": "Hunar.ai", "tier": "P1", "domains": ["hunar.ai"]},
    {"name": "Info Jobs", "tier": "P1", "aliases": ["InfoJobs"], "domains": ["infojobs.net", "infojobs.com.br"]},
    {"name": "Instagram", "tier": "P1", "domains": ["instagram.com"]},
    {"name": "Instawork", "tier": "P1", "domains": ["instawork.com"]},
    {"name": "Intermediair", "tier": "P1", "domains": ["intermediair.nl"]},
    {"name": "IrishJobs", "tier": "P1", "aliases": ["Irish Jobs"], "domains": ["irishjobs.ie"]},
    {"name": "J-Vers", "tier": "P1"},
    {"name": "Job Bank", "tier": "P1", "aliases": ["JobBank Canada"], "domains": ["jobbank.gc.ca"]},
    {"name": "Job Traffic", "tier": "P1", "aliases": ["JobTraffic"]},
    {"name": "Jobbird.de", "tier": "P1", "aliases": ["Jobbird"], "domains": ["jobbird.de"]},
    {"name": "JobHubCentral", "tier": "P1", "domains": ["jobhubcentral.com"]},
    {"name": "JobIndex", "tier": "P1", "domains": ["jobindex.dk"]},
    {"name": "JobKorea", "tier": "P1", "aliases": ["Job Korea"], "domains": ["jobkorea.co.kr"]},
    {"name": "JobMESH", "tier": "P1", "domains": ["jobmesh.com"]},
    {"name": "JobNet", "tier": "P1", "domains": ["jobnet.com.mm"]},
    {"name": "Jobs In Japan", "tier": "P1", "aliases": ["JobsInJapan"], "domains": ["jobsinjapan.com"]},
    {"name": "Jobs.at", "tier": "P1", "domains": ["jobs.at"]},
    {"name": "Jobs.ch", "tier": "P1", "domains": ["jobs.ch"]},
    {"name": "Jobs.ie", "tier": "P1", "domains": ["jobs.ie"]},
    {"name": "Jobsdb", "tier": "P1", "aliases": ["JobsDB"], "domains": ["jobsdb.com"]},
    {"name": "JobsInNetwork", "tier": "P1", "domains": ["jobsinnetwork.com"]},
    {"name": "Jobsora", "tier": "P1", "domains": ["jobsora.com"]},
    {"name": "JobSwipe", "tier": "P1", "aliases": ["Job Swipe"], "domains": ["jobswipe.com"]},
    {"name": "Jobtome", "tier": "P1", "domains": ["jobtome.com"]},
    {"name": "JobUp", "tier": "P1", "domains": ["jobup.ch"]},
    {"name": "Jobwinner", "tier": "P1", "domains": ["jobwinner.ch"]},
    {"name": "Jora", "tier": "P1", "domains": ["jora.com"]},
    {"name": "Ladders.com", "tier": "P1", "aliases": ["The Ladders", "Ladders"], "domains": ["theladders.com"]},
    {"name": "Laborum", "tier": "P1", "domains": ["laborum.com"]},
    {"name": "LinkedIn", "tier": "P1",
     "aliases": ["LinkedIn Talent Solutions", "LTS", "LinkedIn Jobs", "LinkedIn Recruiter"],
     "domains": ["linkedin.com"]},
    {"name": "Manymore.jobs", "tier": "P1", "aliases": ["Manymore"], "domains": ["manymore.jobs"]},
    {"name": "Mindmatch.ai", "tier": "P1", "aliases": ["Mindmatch"], "domains": ["mindmatch.ai"]},
    {"name": "MyJobScanner", "tier": "P1", "domains": ["myjobscanner.com"]},
    {"name": "Myjobhelper", "tier": "P1", "aliases": ["MyJobHelper"], "domains": ["myjobhelper.com"]},
    {"name": "Nationale Vacaturebank", "tier": "P1", "domains": ["nationalevacaturebank.nl"]},
    {"name": "New Zealand Jobs", "tier": "P1", "domains": ["newzealandjobs.co.nz"]},
    {"name": "Nexxt", "tier": "P1", "aliases": ["Nexxt.com"], "domains": ["nexxt.com"]},
    {"name": "OfferUp", "tier": "P1", "domains": ["offerup.com"]},
    {"name": "Otta", "tier": "P1", "domains": ["otta.com"]},
    {"name": "PlacedApp", "tier": "P1", "aliases": ["Placed"], "domains": ["placedapp.com"]},
    {"name": "Pnet", "tier": "P1", "domains": ["pnet.co.za"]},
    {"name": "PostJobFree", "tier": "P1", "aliases": ["Post Job Free"], "domains": ["postjobfree.com"]},
    {"name": "Pracuj.pl", "tier": "P1", "aliases": ["Pracuj"], "domains": ["pracuj.pl"]},
    {"name": "Profesia", "tier": "P1", "domains": ["profesia.sk"]},
    {"name": "Profession.hu", "tier": "P1", "aliases": ["Profession"], "domains": ["profession.hu"]},
    {"name": "Professional Diversity Network", "tier": "P1", "aliases": ["PDN"], "domains": ["prodivnet.com"]},
    {"name": "Propel", "tier": "P1"},
    {"name": "Rabota.bg", "tier": "P1", "aliases": ["Rabota"], "domains": ["rabota.bg"]},
    {"name": "Reddit", "tier": "P1", "domains": ["reddit.com"]},
    {"name": "Remote.co", "tier": "P1", "domains": ["remote.co"]},
    {"name": "Resume-Library.com", "tier": "P1", "aliases": ["Resume Library"], "domains": ["resume-library.com"]},
    {"name": "SAPO Emprego", "tier": "P1", "domains": ["emprego.sapo.pt"]},
    {"name": "SonicJobs", "tier": "P1", "domains": ["sonicjobs.com"]},
    {"name": "Spotify", "tier": "P1", "domains": ["spotify.com"]},
    {"name": "Stack Overflow", "tier": "P1", "aliases": ["Stack Overflow Jobs", "Stackoverflow"], "domains": ["stackoverflow.com"]},
    {"name": "Stellenanzeigen.de", "tier": "P1", "aliases": ["Stellenanzeigen"], "domains": ["stellenanzeigen.de"]},
    {"name": "StellenSMS", "tier": "P1"},
    {"name": "Study Smarter", "tier": "P1", "aliases": ["StudySmarter"], "domains": ["studysmarter.com"]},
    {"name": "Tideri", "tier": "P1", "domains": ["tideri.com"]},
    {"name": "Topjobs.ch", "tier": "P1", "aliases": ["TopJobs"], "domains": ["topjobs.ch"]},
    {"name": "Totaljobs", "tier": "P1", "aliases": ["Total Jobs"], "domains": ["totaljobs.com"]},
    {"name": "TransForce", "tier": "P1", "domains": ["transforce.com"]},
    {"name": "Turing", "tier": "P1", "domains": ["turing.com"]},
    {"name": "Vagas", "tier": "P1", "aliases": ["Vagas.com.br"], "domains": ["vagas.com.br"]},
    {"name": "Vetted Health", "tier": "P1", "domains": ["vettedhealth.com"]},
    {"name": "VietnamWorks", "tier": "P1", "aliases": ["Vietnam Works"], "domains": ["vietnamworks.com"]},
    {"name": "Visage Jobs", "tier": "P1", "aliases": ["Visage"], "domains": ["visagejobs.com"]},
    {"name": "Welcome to the Jungle", "tier": "P1", "aliases": ["WTTJ"], "domains": ["welcometothejungle.com"]},
    {"name": "WhatJobs", "tier": "P1", "domains": ["whatjobs.com"]},
    {"name": "Women for Hire", "tier": "P1", "domains": ["womenforhire.com"]},
    {"name": "Wonderkind", "tier": "P1", "domains": ["wonderkind.com"]},
    {"name": "Xing", "tier": "P1", "domains": ["xing.com"]},
    {"name": "YM Careers", "tier": "P1", "aliases": ["YM"], "domains": ["ymcareers.com"]},
    {"name": "YouTube", "tier": "P1", "domains": ["youtube.com"]},
    {"name": "Zaplata.bg", "tier": "P1", "aliases": ["Zaplata"], "domains": ["zaplata.bg"]},

    # ─── P2 — long-tail / regional / monitoring only ─────────────────────────
    # Empty by default. Promote any P1 entries down to P2 if you want them
    # surfaced only as "monitoring" rather than active outreach.
]


def by_tier(tier: Tier) -> list[Publisher]:
    return [p for p in PUBLISHERS if p["tier"] == tier]


def all_names() -> list[str]:
    names: set[str] = set()
    for p in PUBLISHERS:
        names.add(p["name"])
        names.update(p.get("aliases", []) or [])
    return sorted(names)


def all_domains() -> list[str]:
    domains: set[str] = set()
    for p in PUBLISHERS:
        domains.update(p.get("domains", []) or [])
    return sorted(domains)


def find(name_or_alias: str) -> Publisher | None:
    needle = name_or_alias.strip().lower()
    for p in PUBLISHERS:
        if p["name"].lower() == needle:
            return p
        if any(a.lower() == needle for a in (p.get("aliases") or [])):
            return p
    return None


def match_event(event: dict, internal_domains: set[str] | None = None) -> dict | None:
    """Try to match a calendar event to a curated publisher using fast Python rules
    (no Gemini call). Returns a match dict if confident, otherwise None — caller can
    fall back to Gemini for ambiguous events.

    Match rules, in priority order:
      1. Attendee email domain matches a publisher's domain (strong signal)
      2. Title contains the canonical name OR an alias as a whole word
    """
    import re as _re

    title = (event.get("title") or "").lower()
    attendees = [(a or "").lower() for a in (event.get("attendees") or [])]

    # 1. Attendee-domain match (most reliable)
    for p in PUBLISHERS:
        for domain in p.get("domains") or []:
            d = domain.lower()
            for attendee in attendees:
                if attendee.endswith("@" + d) or attendee == d:
                    return {
                        "partner": p["name"],
                        "tier": p["tier"],
                        "time": event.get("time", ""),
                        "title": event.get("title", ""),
                        "attendees": event.get("attendees", []),
                        "match_reason": "attendee_domain",
                        "match_evidence": d,
                    }

    # 2. Title match — canonical name OR aliases, as whole-word substrings
    for p in PUBLISHERS:
        candidates = [p["name"]] + (p.get("aliases") or [])
        for term in candidates:
            t = term.lower().strip()
            if not t or len(t) < 3:  # avoid pathological 1-2 char aliases (none currently, but safety)
                continue
            # Word-boundary so "indeed" doesn't match "indeed-able-team"; allow dot/hyphen as boundaries.
            pattern = r"(?:^|[^a-z0-9])" + _re.escape(t) + r"(?:$|[^a-z0-9])"
            if _re.search(pattern, title):
                return {
                    "partner": p["name"],
                    "tier": p["tier"],
                    "time": event.get("time", ""),
                    "title": event.get("title", ""),
                    "attendees": event.get("attendees", []),
                    "match_reason": "title",
                    "match_evidence": term,
                }
    return None


def is_obviously_internal(event: dict, internal_domains: set[str] | None = None) -> bool:
    """Heuristic: does this event look like an internal-only meeting we can skip
    without consulting Gemini? Returns True if all attendees share a common domain
    that is in `internal_domains`, or there are no external attendees."""
    internal_domains = internal_domains or {"joveo.com"}
    attendees = [(a or "").lower() for a in (event.get("attendees") or [])]
    if not attendees:
        # No attendees beyond the organiser → looks like a personal block.
        return True
    for attendee in attendees:
        domain = attendee.split("@", 1)[-1] if "@" in attendee else ""
        if domain and domain not in internal_domains:
            return False
    return True


def format_for_prompt() -> str:
    """Render the publisher list as a compact block for inclusion in a system prompt."""
    lines: list[str] = []
    for tier in ("P0", "P1", "P2"):
        pubs = by_tier(tier)
        if not pubs:
            continue
        lines.append(f"{tier}:")
        for p in pubs:
            aliases = ", ".join(p.get("aliases") or [])
            domains = ", ".join(p.get("domains") or [])
            extras: list[str] = []
            if aliases:
                extras.append(f"aliases: {aliases}")
            if domains:
                extras.append(f"domains: {domains}")
            extras_str = f" ({'; '.join(extras)})" if extras else ""
            lines.append(f"  - {p['name']}{extras_str}")
    return "\n".join(lines)
