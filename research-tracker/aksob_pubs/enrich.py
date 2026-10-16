"""Fill in DOI / ISSN / journal for records that lack them (mostly Google Scholar entries).

Google Scholar gives a title and a free-text venue string. To rank a paper we need the journal's
ISSN, so each unresolved title is looked up in OpenAlex, then Crossref. Results (including
negative ones) are cached so re-runs only cost requests for new papers.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from rapidfuzz import fuzz

from .config import Config
from .db import Database, now_iso
from .sources.base import PubRecord
from .sources.openalex import OpenAlexSource
from .utils import (Http, QuotaExceeded, clean_title, is_preprint_venue, last_name, looks_non_journal, norm_issn,
                    norm_text, title_key)

log = logging.getLogger("aksob")
CROSSREF = "https://api.crossref.org/works"
CROSSREF_TYPES = {
    "journal-article": "article", "proceedings-article": "conference-paper", "book-chapter": "book-chapter",
    "book": "book", "monograph": "book", "edited-book": "book", "posted-content": "preprint",
    "dissertation": "dissertation", "report": "report",
}


def rank_candidate(rec: PubRecord, scholar_venue: str = "") -> tuple:
    """Sort key: real journal (not a preprint server) > venue agrees with Scholar > has ISSN > is an article."""
    preprint = is_preprint_venue(rec.venue, rec.issns)
    sim = fuzz.token_set_ratio(norm_text(rec.venue), norm_text(scholar_venue)) if scholar_venue and rec.venue else 0
    return (not preprint, sim >= 60, bool(rec.issns), rec.type == "article", sim)


class Enricher:
    def __init__(self, cfg: Config, db: Database, openalex: OpenAlexSource):
        self.cfg, self.db, self.oa = cfg, db, openalex
        ua = "aksob-research-tracker/0.1" + (f" (mailto:{cfg.openalex_mailto})" if cfg.openalex_mailto else "")
        self.crossref = Http({"User-Agent": ua, "Accept": "application/json"}, min_interval=0.25)
        self.exhausted: set[str] = set()

    def crossref_search(self, title: str, year: int | None, surname: str | None,
                        scholar_venue: str = "") -> PubRecord | None:
        params: dict[str, Any] = {"query.bibliographic": clean_title(title), "rows": 5,
                                  "select": "DOI,title,container-title,ISSN,author,issued,type"}
        if self.cfg.openalex_mailto:
            params["mailto"] = self.cfg.openalex_mailto
        data = self.crossref.get_json(CROSSREF, params=params)
        target = norm_text(title)
        candidates: list[PubRecord] = []
        for item in (data.get("message") or {}).get("items", []):
            cand = norm_text((item.get("title") or [""])[0])
            if fuzz.ratio(cand, target) < 93:
                continue
            parts = ((item.get("issued") or {}).get("date-parts") or [[None]])[0]
            issued_year = parts[0] if parts else None
            if year and issued_year and abs(int(issued_year) - int(year)) > 2:
                continue
            if surname and item.get("author"):
                families = " ".join(norm_text(a.get("family", "")) for a in item["author"])
                if families and surname not in families:
                    continue
            container = (item.get("container-title") or [""])[0]
            candidates.append(PubRecord(title=title, year=year, venue_raw=container, venue=container,
                                        issns=list(item.get("ISSN") or []), doi=item.get("DOI"),
                                        type=CROSSREF_TYPES.get(item.get("type", ""), item.get("type", ""))))
        # The same title often exists as a working paper AND a journal article: prefer the journal version.
        candidates.sort(key=lambda r: rank_candidate(r, scholar_venue), reverse=True)
        return candidates[0] if candidates else None

    def lookup(self, title: str, year: int | None, surname: str | None, scholar_venue: str = "") -> tuple[dict[str, Any], bool]:
        """Return ({"source": ..., "record": {...}} or {}, complete) - complete is False when a backend errored."""
        complete = True
        for source, fn in (("openalex", self.oa.search_by_title), ("crossref", self.crossref_search)):
            if source in self.exhausted:
                complete = False
                continue
            try:
                rec = fn(title, year, surname, scholar_venue)
            except QuotaExceeded as exc:
                log.warning("  %s daily quota reached - using the other backends for the rest of this run (%s)",
                            source, str(exc)[:120])
                self.exhausted.add(source)
                complete = False
                continue
            except Exception as exc:  # one failing backend must not stop the run
                log.warning("  %s lookup failed for '%s': %s", source, title[:60], str(exc)[:160])
                complete = False
                continue
            if rec is not None:
                return {"source": source, "record": rec.as_dict()}, True
        return {}, complete

    def enrich(self, force: bool = False, limit: int | None = None) -> dict[str, int]:
        where = "(issns IS NULL OR issns='' OR issns='[]')" + ("" if force else " AND enriched_at IS NULL")
        rows = self.db.publications(where)
        if limit:
            rows = rows[:limit]
        stats = {"checked": 0, "matched_openalex": 0, "matched_crossref": 0, "no_match": 0, "skipped": 0}
        faculty_cache: dict[str, Any] = {}
        log.info("Enriching %d publications without ISSN", len(rows))
        for i, row in enumerate(rows, 1):
            stats["checked"] += 1
            ts = now_iso()
            title = row["title"] or ""
            venue = row["venue"] or row["venue_raw"] or ""
            if len(title) < 15:
                self.db.update_publication(row["id"], enriched_at=ts, enrich_status="skipped: short title")
                stats["skipped"] += 1
                continue
            if not row["doi"] and looks_non_journal(venue):
                self.db.update_publication(row["id"], enriched_at=ts, enrich_status="skipped: non-journal venue")
                stats["skipped"] += 1
                continue
            fac = faculty_cache.get(row["faculty_slug"])
            if fac is None:
                fac = self.db.get_faculty(row["faculty_slug"])
                faculty_cache[row["faculty_slug"]] = fac
            surname = last_name(fac["name"]) if fac else None

            key = title_key(title)
            payload = None if force else self.db.cache_get(key)
            if payload is None:
                payload, complete = self.lookup(title, row["year"], surname, venue)
                if payload or complete:            # never cache a miss caused by a backend error
                    self.db.cache_set(key, payload)

            if payload:
                rec = payload["record"]
                issns = sorted({x for x in (norm_issn(v) for v in rec.get("issns") or []) if x})
                preprint = is_preprint_venue(rec.get("venue"), issns)
                strong = bool(issns) and not preprint  # a real journal version; otherwise take the DOI at most
                label = "" if strong else (" (preprint version ignored)" if preprint else " (no issn)")
                updates: dict[str, Any] = {
                    "enriched_at": ts, "ranked_at": None,
                    "enrich_status": f"matched:{payload['source']}{label}",
                }
                if strong:
                    updates["issns"] = json.dumps(issns)
                if rec.get("doi") and not row["doi"] and not preprint:
                    updates["doi"] = str(rec["doi"]).lower()
                if strong and rec.get("venue"):
                    updates["venue"] = rec["venue"]
                if rec.get("type") and not row["type"] and (strong or looks_non_journal(venue)):
                    updates["type"] = rec["type"]
                if strong:
                    for k in ("openalex_work_id", "openalex_source_id", "abdc"):
                        if rec.get(k) and not row[k]:
                            updates[k] = rec[k]
                    if rec.get("cited_by_openalex") is not None and row["cited_by_openalex"] is None:
                        updates["cited_by_openalex"] = rec["cited_by_openalex"]
                self.db.update_publication(row["id"], **updates)
                stats[f"matched_{payload['source']}"] += 1
            else:
                self.db.update_publication(row["id"], enriched_at=ts, enrich_status="no match")
                stats["no_match"] += 1
            if i % 50 == 0:
                self.db.commit()
                log.info("  enriched %d/%d", i, len(rows))
        self.db.commit()
        return stats
