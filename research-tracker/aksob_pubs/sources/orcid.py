"""ORCID public API (free, no key): the list of works a researcher keeps on their own ORCID record."""
from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any

from ..utils import Http, clean_title
from .base import PubRecord, Source

log = logging.getLogger("aksob")
API = "https://pub.orcid.org/v3.0"
ORCID_RE = re.compile(r"(\d{4}-\d{4}-\d{4}-\d{3}[\dX])", re.I)
TYPE_MAP = {
    "journal-article": "article", "journal-issue": "article", "review": "review", "book": "book",
    "book-chapter": "book-chapter", "edited-book": "book", "conference-paper": "conference-paper",
    "conference-abstract": "conference-paper", "conference-poster": "conference-paper", "dissertation-thesis": "dissertation",
    "working-paper": "preprint", "preprint": "preprint", "report": "report", "other": "other",
}


class OrcidSource(Source):
    name = "orcid"

    def __init__(self, cfg, db):
        super().__init__(cfg, db)
        self.http = Http({"Accept": "application/json", "User-Agent": "aksob-research-tracker/0.2"}, min_interval=0.2)

    def search(self, name: str) -> dict[str, Any] | None:
        """Find a person's ORCID by name, accepted only when the record lists LAU as an institution."""
        from ..utils import name_tokens, strip_accents
        tokens = [t for t in strip_accents(name).split() if len(t) > 1]
        if len(tokens) < 2:
            return None
        given, family = tokens[0], tokens[-1]
        q = f'given-names:"{given}" AND family-name:"{family}"'
        data = self.http.get_json(f"{API}/expanded-search/", params={"q": q, "rows": 20})
        target = name_tokens(name)
        hits = []
        for r in data.get("expanded-result") or []:
            insts = " | ".join(r.get("institution-name") or []).lower()
            if "lebanese american" not in insts:
                continue
            disp = f"{r.get('given-names') or ''} {r.get('family-names') or ''}"
            overlap = len(target & name_tokens(disp))
            if overlap >= 2 or (overlap >= 1 and len(target) <= 2):
                hits.append({"orcid": r.get("orcid-id"), "name": disp.strip(), "institutions": insts})
        return hits[0] if len(hits) == 1 else None

    def fetch(self, faculty: sqlite3.Row | dict) -> tuple[list[PubRecord], dict[str, Any]]:
        f = dict(faculty)
        m = ORCID_RE.search(str(f.get("orcid") or ""))
        if not m:
            return [], {"fetch_status": "orcid: no ORCID id on record"}
        orcid = m.group(1).upper()
        data = self.http.get_json(f"{API}/{orcid}/works")
        records: list[PubRecord] = []
        for group in data.get("group", []):
            summaries = group.get("work-summary") or []
            if not summaries:
                continue
            s = summaries[0]
            title = clean_title(((s.get("title") or {}).get("title") or {}).get("value") or "")
            if not title:
                continue
            year_val = (((s.get("publication-date") or {}).get("year") or {}).get("value"))
            year = int(year_val) if year_val and str(year_val).isdigit() else None
            if year and year < self.cfg.year_from:
                continue
            journal = clean_title((s.get("journal-title") or {}).get("value") or "")
            doi = None
            for ext in ((group.get("external-ids") or {}).get("external-id") or []):
                if (ext.get("external-id-type") or "").lower() == "doi":
                    doi = (ext.get("external-id-value") or "").strip().lower()
                    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi) or None
                    break
            wtype = TYPE_MAP.get((s.get("type") or "").lower().replace("_", "-"), "")
            records.append(PubRecord(title=title, year=year, venue_raw=journal, venue=journal, doi=doi, type=wtype))
        return records, {}
