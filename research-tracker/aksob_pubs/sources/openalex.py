"""OpenAlex (https://openalex.org) - free, open scholarly index. No key needed.

Used for three jobs:
  1. as a publication source (works of an author affiliated with LAU),
  2. to resolve each faculty member to an OpenAlex author id,
  3. to enrich Google Scholar records (title -> DOI, ISSN, journal, ABDC rating).
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from rapidfuzz import fuzz

from ..utils import (Http, clean_title, is_preprint_venue, last_name, name_tokens, name_variants, norm_text,
                     search_safe, title_key)
from .base import PubRecord, Source

log = logging.getLogger("aksob")
API = "https://api.openalex.org"
ABDC_MAP = {"abdc-a-star": "A*", "abdc-a": "A", "abdc-b": "B", "abdc-c": "C"}
WORK_SELECT = "id,doi,title,display_name,publication_year,type,primary_location,cited_by_count,authorships"
AUTHOR_SELECT = "id,display_name,works_count,cited_by_count,summary_stats,ids,last_known_institutions,affiliations"
PARTICLES = {"el", "al", "abou", "abu", "bin", "ben", "de", "da", "di", "van", "von"}


def short_id(value: str | None) -> str | None:
    return value.rsplit("/", 1)[-1] if value else None


def work_to_record(w: dict[str, Any]) -> PubRecord:
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    issns = list(src.get("issn") or ([src["issn_l"]] if src.get("issn_l") else []))
    abdc = next((ABDC_MAP[x] for x in (src.get("listed_in") or []) if x in ABDC_MAP), None)
    venue = src.get("display_name") or loc.get("raw_source_name") or ""
    authors = ", ".join(a["author"]["display_name"] for a in w.get("authorships", [])
                        if a.get("author") and a["author"].get("display_name"))
    wtype = w.get("type") or ""
    if src.get("type") == "repository" and wtype == "article":
        wtype = "preprint"
    return PubRecord(
        title=clean_title(w.get("title") or w.get("display_name") or ""),
        year=w.get("publication_year"),
        venue_raw=venue,
        venue=venue,
        authors=authors,
        doi=(w.get("doi") or "").replace("https://doi.org/", "") or None,
        issns=issns,
        type=wtype,
        cited_by_openalex=w.get("cited_by_count"),
        openalex_work_id=short_id(w.get("id")),
        openalex_source_id=short_id(src.get("id")),
        abdc=abdc,
    )


class OpenAlexSource(Source):
    name = "openalex"

    def __init__(self, cfg, db):
        super().__init__(cfg, db)
        ua = "aksob-research-tracker/0.1" + (f" (mailto:{cfg.openalex_mailto})" if cfg.openalex_mailto else "")
        self.http = Http({"User-Agent": ua, "Accept": "application/json"}, min_interval=0.12)

    def _params(self, **kw: Any) -> dict[str, Any]:
        if self.cfg.openalex_mailto:
            kw["mailto"] = self.cfg.openalex_mailto
        if self.cfg.openalex_api_key:
            kw["api_key"] = self.cfg.openalex_api_key
        return kw

    # ------------------------------------------------------------ author ids
    @staticmethod
    def _record(a: dict[str, Any], note: str = "") -> dict[str, Any]:
        stats = a.get("summary_stats") or {}
        out = {
            "openalex_id": short_id(a["id"]),
            "openalex_name": a.get("display_name"),
            "openalex_works": a.get("works_count"),
            "openalex_citations": a.get("cited_by_count"),
            "openalex_h_index": stats.get("h_index"),
            "orcid": short_id((a.get("ids") or {}).get("orcid")),
        }
        if note:
            out["notes"] = note
        return out

    def resolve_author(self, name: str) -> dict[str, Any] | None:
        """Find the OpenAlex author record for a person affiliated with the university.

        Pass 1: name search restricted to authors with an LAU affiliation on record (any name variant,
        best token overlap, then most works). Pass 2, only if pass 1 finds nothing: unrestricted name
        search, accepted only when every name token matches AND the record shows a Lebanese
        institution, so a professor whose LAU papers are not yet attributed is still found.
        """
        inst = self.cfg.openalex_institution_id
        target_last = last_name(name)
        target_tokens = name_tokens(name) - PARTICLES
        for variant in name_variants(name):
            data = self.http.get_json(f"{API}/authors", params=self._params(
                search=variant, filter=f"affiliations.institution.id:{inst}", per_page=10, select=AUTHOR_SELECT))
            candidates = []
            for a in data.get("results", []):
                disp = a.get("display_name") or ""
                toks = name_tokens(disp)
                if target_last and target_last not in toks and fuzz.token_set_ratio(norm_text(disp), norm_text(name)) < 85:
                    continue
                candidates.append((len(target_tokens & toks), a.get("works_count") or 0, a))
            if candidates:
                candidates.sort(key=lambda t: (t[0], t[1]), reverse=True)
                return self._record(candidates[0][2])

        data = self.http.get_json(f"{API}/authors", params=self._params(search=name, per_page=10, select=AUTHOR_SELECT))
        exact = []
        for a in data.get("results", []):
            toks = name_tokens(a.get("display_name") or "") - PARTICLES
            if not target_tokens <= toks:
                continue
            insts = [(aff.get("institution") or {}) for aff in (a.get("affiliations") or [])]
            insts += list(a.get("last_known_institutions") or [])
            lebanese = any((i.get("country_code") == "LB") or "lebanese american" in (i.get("display_name") or "").lower() for i in insts)
            if lebanese:
                exact.append(a)
        if len(exact) == 1:
            return self._record(exact[0], note="OpenAlex record found by exact name with a Lebanese affiliation (LAU not on record) - please confirm")
        return None

    def author_stats(self, author_id: str) -> dict[str, Any]:
        a = self.http.get_json(f"{API}/authors/{author_id}", params=self._params(select=AUTHOR_SELECT))
        stats = a.get("summary_stats") or {}
        return {
            "openalex_name": a.get("display_name"),
            "openalex_works": a.get("works_count"),
            "openalex_citations": a.get("cited_by_count"),
            "openalex_h_index": stats.get("h_index"),
        }

    # ----------------------------------------------------------------- works
    def fetch_works(self, author_id: str) -> list[PubRecord]:
        records: list[PubRecord] = []
        cursor = "*"
        while cursor:
            data = self.http.get_json(f"{API}/works", params=self._params(
                filter=f"authorships.author.id:{author_id},publication_year:>{self.cfg.year_from - 1}",
                per_page=200, cursor=cursor, select=WORK_SELECT, sort="publication_year:desc"))
            for w in data.get("results", []):
                if w.get("type") in ("paratext", "erratum", "retraction"):
                    continue
                rec = work_to_record(w)
                if rec.title:
                    records.append(rec)
            cursor = (data.get("meta") or {}).get("next_cursor")
            if not data.get("results"):
                break
        # OpenAlex often lists a preprint and the published article separately; keep the published one.
        best: dict[str, PubRecord] = {}
        for rec in records:
            key = title_key(rec.title)
            cur = best.get(key)
            if cur is None or (cur.type == "preprint" and rec.type != "preprint"):
                best[key] = rec
        return list(best.values())

    def fetch(self, faculty: sqlite3.Row | dict) -> tuple[list[PubRecord], dict[str, Any]]:
        f = dict(faculty)
        updates: dict[str, Any] = {}
        author_id = f.get("openalex_id")
        if not author_id:
            resolved = self.resolve_author(f["name"])
            if not resolved:
                return [], {"fetch_status": "openalex: author not found (set openalex_id in faculty.csv)"}
            updates.update({k: v for k, v in resolved.items() if v is not None and (k != "orcid" or not f.get("orcid"))})
            author_id = resolved["openalex_id"]
            log.info("  resolved %s -> %s (%s, %s works)", f["name"], author_id,
                     resolved["openalex_name"], resolved["openalex_works"])
        else:
            try:
                updates.update(self.author_stats(author_id))
            except Exception as exc:
                if "404" in str(exc):                 # OpenAlex merged or deleted this author id
                    log.info("  openalex id %s no longer exists for %s - resolving again", author_id, f["name"])
                    resolved = self.resolve_author(f["name"])
                    if not resolved:
                        return [], {"openalex_id": None, "fetch_status": "openalex: author id disappeared and no replacement found"}
                    updates.update({k: v for k, v in resolved.items() if v is not None and (k != "orcid" or not f.get("orcid"))})
                    author_id = resolved["openalex_id"]
                else:
                    log.warning("  openalex author stats failed for %s: %s", author_id, exc)
        records = self.fetch_works(author_id)
        return records, updates

    # ------------------------------------------------------------ enrichment
    def search_by_title(self, title: str, year: int | None, surname: str | None,
                        scholar_venue: str = "") -> PubRecord | None:
        params = self._params(search=search_safe(title), per_page=5, select=WORK_SELECT)
        if year:
            params["filter"] = f"publication_year:{int(year) - 2}-{int(year) + 2}"
        data = self.http.get_json(f"{API}/works", params=params)
        target = norm_text(title)
        candidates: list[PubRecord] = []
        for w in data.get("results", []):
            cand = norm_text(w.get("title") or w.get("display_name"))
            if fuzz.ratio(cand, target) < 93:
                continue
            if surname:
                names = " ".join(norm_text((a.get("author") or {}).get("display_name", "")) for a in w.get("authorships", []))
                if names and surname not in names:
                    continue
            candidates.append(work_to_record(w))
        # prefer the published journal version over a preprint / repository copy of the same title
        def key(r: PubRecord) -> tuple:
            preprint = r.type == "preprint" or is_preprint_venue(r.venue, r.issns)
            sim = fuzz.token_set_ratio(norm_text(r.venue), norm_text(scholar_venue)) if scholar_venue and r.venue else 0
            return (not preprint, sim >= 60, bool(r.issns), r.type == "article", sim)
        candidates.sort(key=key, reverse=True)
        return candidates[0] if candidates else None
