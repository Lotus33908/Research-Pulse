"""Render the static HTML app (self-contained, data embedded as JSON)."""
from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

from .config import Config
from .db import Database
from .summaries import citations, coverage_status, faculty_citations, is_covered, on_scholar, pub_key, rank_status

log = logging.getLogger("aksob")
TEMPLATE = Path(__file__).parent / "templates" / "dashboard.html"


def export_data(cfg: Config, db: Database, scimago_years: list[int] | None = None) -> dict:
    faculty = db.faculty()
    pubs = db.publications_joined()
    fac_out = []
    for f in faculty:
        cites, h, src = faculty_citations(f)
        fac_out.append({
            "slug": f["slug"], "name": f["name"], "dept": f["department"], "cat": f["category"],
            "title": f["title"] or "", "url": f["profile_url"] or "",
            "openalex": f["openalex_id"] or "", "orcid": f["orcid"] or "", "scholar": f["scholar_id"] or "",
            "cites": cites, "h": h, "msrc": src,
            "covered": is_covered(f), "coverage": coverage_status(f),
            "gsimp": str(f["scholar_import_at"])[:10] if f["scholar_import_at"] else None,
            "gsn": f["scholar_articles"],
            "fetched": str(f["last_fetched"])[:10] if f["last_fetched"] else None,
        })
    pub_out = []
    for p in pubs:
        pub_out.append({
            "k": pub_key(p), "f": p["faculty_slug"], "t": p["title"] or "", "y": p["year"],
            "j": p["scimago_title"] or p["venue"] or p["venue_raw"] or "", "q": p["quartile"] or "",
            "s": rank_status(p), "sjr": p["sjr"], "abdc": p["abdc"] or "", "ty": p["type"] or "",
            "c": citations(p), "doi": p["doi"] or "", "cat": p["categories"] or "", "src": p["sources"] or "",
            "m": p["match_method"] or "", "a": (p["authors"] or "")[:400], "gs": 1 if on_scholar(p) else 0,
            "fs": str(p["first_seen"])[:10] if p["first_seen"] else None,
        })
    return {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "school": cfg.school_name, "university": cfg.university_name,
        "year_from": cfg.year_from, "sources": cfg.sources,
        "scimago_years": scimago_years or [],
        "faculty": fac_out, "pubs": pub_out,
    }


def build_dashboard(cfg: Config, db: Database, scimago_years: list[int] | None = None) -> Path:
    if scimago_years is None:
        from .scimago import cached_years
        scimago_years = cached_years(cfg.scimago_dir)
    data = export_data(cfg, db, scimago_years)
    html = TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = html.replace("__DATA_JSON__", payload)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    out = cfg.output_dir / "dashboard.html"
    out.write_text(html, encoding="utf-8")
    cfg.docs_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(out, cfg.docs_dir / "index.html")
    log.info("App written to %s (and docs/index.html for GitHub Pages)", out)
    return out
