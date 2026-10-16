"""Assign Scimago quartiles to publications."""
from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Any

from .config import Config
from .db import RANK_FIELDS, Database, now_iso
from .scimago import ScimagoIndex, year_preference
from .utils import is_preprint_venue, looks_non_journal, norm_issn, norm_text

log = logging.getLogger("aksob")

NON_RANK_TYPES = {
    "book", "book-chapter", "dissertation", "preprint", "dataset", "report", "other", "reference-entry",
    "editorial", "letter", "supplementary-materials", "peer-review", "standard", "grant", "libguides",
}
VALID_OVERRIDE_Q = {"Q1", "Q2", "Q3", "Q4", "NONE", "N/A"}


def load_overrides(path: Path) -> dict[str, dict[str, str]]:
    """journal_overrides.csv: venue_pattern,issn,quartile,note - manual fixes for venues that will not match."""
    if not path.exists():
        return {}
    out: dict[str, dict[str, str]] = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            pat = norm_text(row.get("venue_pattern"))
            if pat:
                out[pat] = row
    return out


def rank_publications(cfg: Config, db: Database, scimago: ScimagoIndex, rerank: bool = False) -> dict[str, int]:
    years = scimago.load_range(cfg.scimago_from)
    if not years:
        raise SystemExit("No Scimago data could be loaded. Download the yearly CSVs from scimagojr.com "
                         f"and place them in {cfg.scimago_dir} as scimagojr_YYYY.csv")
    overrides = load_overrides(cfg.overrides_csv)
    rows = db.publications("" if rerank else "ranked_at IS NULL")
    log.info("Ranking %d publications against Scimago %d-%d", len(rows), years[0], years[-1])
    stats = {"ranked": 0, "issn": 0, "title": 0, "fuzzy": 0, "override": 0, "none": 0, "n/a": 0}

    for i, p in enumerate(rows, 1):
        issns: list[str] = json.loads(p["issns"]) if p["issns"] else []
        venue = p["venue"] or p["venue_raw"] or ""
        fields: dict[str, Any] = {k: None for k in RANK_FIELDS}
        fields.update(ranked_at=now_iso(), match_method="none", match_score=0)

        ov = overrides.get(norm_text(p["venue_raw"])) or overrides.get(norm_text(p["venue"]))
        prefix = ""
        if ov:
            ov_issn = norm_issn(ov.get("issn"))
            ov_q = (ov.get("quartile") or "").strip().upper()
            if ov_issn:
                issns = [ov_issn] + issns
                prefix = "override+"
            elif ov_q in VALID_OVERRIDE_Q:
                fields.update(quartile=ov_q if ov_q.startswith("Q") else None, match_method="override",
                              match_score=100, scimago_title=(ov.get("note") or venue))
                db.update_publication(p["id"], **fields)
                stats["override"] += 1
                stats["ranked"] += 1
                continue

        preprint = is_preprint_venue(venue, issns)       # SSRN & co. have ISSNs but are not journals
        if preprint or (not issns and ((p["type"] or "") in NON_RANK_TYPES or looks_non_journal(venue) or not venue)):
            fields["match_method"] = "n/a"
            db.update_publication(p["id"], **fields)
            stats["n/a"] += 1
            continue

        prefs = year_preference(p["year"], years)
        hit = method = None
        score = 0.0
        if issns:
            for y in prefs:
                j = scimago.index(y).by_issns(issns)
                if j:
                    hit, method, score = j, "issn", 100.0
                    break
        if hit is None and venue:
            for y in prefs[:3]:
                j = scimago.index(y).by_exact_title(venue)
                if j:
                    hit, method, score = j, "title", 100.0
                    break
        if hit is None and venue:
            for y in prefs[:2]:
                j, s = scimago.index(y).by_fuzzy_title(venue, cfg.fuzzy_threshold)
                if j:
                    hit, method, score = j, "fuzzy", s
                    break

        if hit is not None:
            fields.update(scimago_year=hit.year, scimago_sourceid=hit.sourceid, scimago_title=hit.title,
                          quartile=hit.quartile, sjr=hit.sjr, journal_h_index=hit.h_index,
                          categories=hit.categories, areas=hit.areas, publisher=hit.publisher,
                          country=hit.country, match_method=prefix + method, match_score=score)
            stats[method] += 1
            stats["ranked"] += 1
        else:
            stats["none"] += 1
        db.update_publication(p["id"], **fields)
        if i % 200 == 0:
            db.commit()
    db.commit()
    return stats
