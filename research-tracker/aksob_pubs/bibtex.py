"""Import Google Scholar BibTeX exports.

On a Google Scholar profile: tick the select-all box, Export, BibTeX, save the file as
data/scholar_exports/<name>.bib (for example "Ali Fakih.bib" or "dr-ali-fakih.bib"). Two clicks per
person, cannot be blocked, and it is the faculty member's own curated list. Each file is matched to
the roster by file name and imported as source "scholar_bibtex".
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from .config import Config
from .db import Database, now_iso
from .sources.base import PubRecord
from .utils import clean_title, norm_text, parse_scholar_venue

log = logging.getLogger("aksob")

TYPE_MAP = {
    "article": "article", "book": "book", "inbook": "book-chapter", "incollection": "book-chapter",
    "inproceedings": "conference-paper", "conference": "conference-paper", "proceedings": "conference-paper",
    "phdthesis": "dissertation", "mastersthesis": "dissertation", "techreport": "report", "unpublished": "preprint",
    "misc": "other",
}
_ENTRY_START = re.compile(r"@(\w+)\s*\{\s*([^,\s]*)\s*,", re.I)


def parse_bibtex(text: str) -> list[dict[str, str]]:
    """Tiny BibTeX reader that copes with nested braces and quoted values (enough for Scholar exports)."""
    entries: list[dict[str, str]] = []
    pos = 0
    while True:
        m = _ENTRY_START.search(text, pos)
        if not m:
            break
        entry: dict[str, str] = {"_type": m.group(1).lower(), "_key": m.group(2)}
        i = m.end()
        n = len(text)
        while i < n:
            while i < n and text[i] in " \t\r\n,":
                i += 1
            if i >= n or text[i] == "}":
                i += 1
                break
            j = i
            while j < n and text[j] not in "=":
                j += 1
            name = text[i:j].strip().lower()
            j += 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j >= n:
                break
            if text[j] == "{":
                depth, k = 0, j
                while k < n:
                    if text[k] == "{":
                        depth += 1
                    elif text[k] == "}":
                        depth -= 1
                        if depth == 0:
                            break
                    k += 1
                value = text[j + 1:k]
                i = k + 1
            elif text[j] == '"':
                k = j + 1
                while k < n and text[k] != '"':
                    k += 1
                value = text[j + 1:k]
                i = k + 1
            else:
                k = j
                while k < n and text[k] not in ",}\n":
                    k += 1
                value = text[j:k]
                i = k
            entry[name] = re.sub(r"\s+", " ", value.replace("{", "").replace("}", "")).strip()
        entries.append(entry)
        pos = i
    return entries


def bib_to_record(e: dict[str, str]) -> PubRecord | None:
    title = clean_title(e.get("title", ""))
    if not title:
        return None
    year_txt = e.get("year", "").strip()
    year = int(year_txt) if year_txt.isdigit() else None
    journal = e.get("journal") or e.get("booktitle") or e.get("publisher") or e.get("howpublished") or e.get("school") or ""
    parts = [journal]
    if e.get("volume"):
        parts.append(e["volume"] + (f" ({e['number']})" if e.get("number") else ""))
    venue_raw = (" ".join(p for p in parts if p) + (f", {e['pages']}" if e.get("pages") else "")
                 + (f", {year}" if year else "")).strip(" ,")
    authors = ", ".join(a.strip() for a in re.split(r"\s+and\s+", e.get("author", "")) if a.strip())
    return PubRecord(title=title, year=year, venue_raw=venue_raw, venue=parse_scholar_venue(venue_raw) or journal,
                     authors=authors, type=TYPE_MAP.get(e.get("_type", ""), ""),
                     scholar_pub_id=f"bib:{e.get('_key') or norm_text(title)[:60]}")


def match_faculty(stem: str, faculty: list) -> Any | None:
    s = norm_text(stem.replace("_", " ").replace("-", " "))
    for f in faculty:
        if stem == f["slug"] or s == norm_text(f["name"]) or s == norm_text(f["slug"].replace("-", " ")):
            return f
    for f in faculty:                                   # "Fakih Ali" / "fakih, ali" / with salutation
        toks = set(norm_text(f["name"]).split())
        if toks and toks <= set(s.split()):
            return f
    return None


def import_exports(cfg: Config, db: Database) -> dict[str, int]:
    folder: Path = cfg.scholar_exports_dir
    folder.mkdir(parents=True, exist_ok=True)
    files = sorted(folder.glob("*.bib"))
    stats = {"files": len(files), "matched": 0, "records": 0, "new": 0, "unmatched": 0}
    if not files:
        return stats
    faculty = db.faculty(include_only=False, active_only=False)
    for path in files:
        f = match_faculty(path.stem, faculty)
        if f is None:
            log.warning("  %s: cannot match this file name to anyone in faculty.csv (use the slug or the full name)", path.name)
            stats["unmatched"] += 1
            continue
        entries = parse_bibtex(path.read_text(encoding="utf-8", errors="replace"))
        new = count = 0
        for e in entries:
            rec = bib_to_record(e)
            if rec is None:
                continue
            if rec.year and rec.year < cfg.year_from:
                continue
            _, created = db.upsert_publication(f["slug"], rec.as_dict(), "scholar_bibtex")
            new += int(created)
            count += 1
        db.update_faculty(f["slug"], scholar_articles=count, scholar_import_at=now_iso())
        db.commit()
        stats["matched"] += 1
        stats["records"] += count
        stats["new"] += new
        log.info("  %-28s %s: %d items (%d new)", f["name"], path.name, count, new)
    return stats
