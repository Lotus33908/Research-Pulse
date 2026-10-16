"""Build the faculty roster from the AKSOB "Faculty and Staff" page and individual profile pages.

data/faculty.csv is the single source of truth that humans may edit:
  - scholar_id, openalex_id, include, notes are preserved across refreshes
  - new people are appended, departed people are marked active=0
"""
from __future__ import annotations

import csv
import logging
import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from .config import Config
from .db import Database
from .utils import BROWSER_HEADERS, Http

log = logging.getLogger("aksob")

CSV_COLUMNS = [
    "slug", "name", "salutation", "title", "department", "departments", "category", "email",
    "profile_url", "scholar_id", "openalex_id", "orcid", "include", "active", "manual", "notes",
]
# manual=1 marks people added by hand (e.g. the Dean, who is not on the faculty page); they are never
# marked inactive when the website roster is refreshed.
SALUTATION_RE = re.compile(r"^(Dr|Mr|Mrs|Ms|Prof|Professor)\.?\s+", re.I)
SCHOLAR_ID_RE = re.compile(r"[?&]user=([A-Za-z0-9_-]{8,16})")
ORCID_RE = re.compile(r"orcid\.org/(\d{4}-\d{4}-\d{4}-\d{3}[\dX])", re.I)
TRUE_VALUES = ("1", "true", "yes", "y")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_roster_page(html: str) -> list[dict[str, Any]]:
    """Walk h2 (department) -> h3 (category) -> profile anchors in document order."""
    soup = BeautifulSoup(html, "html.parser")
    people: dict[str, dict[str, Any]] = {}
    department = category = ""
    for el in soup.find_all(["h2", "h3", "a"]):
        if el.name == "h2":
            department = _clean(el.get_text()).replace("Department of ", "")
            category = ""
        elif el.name == "h3":
            category = _clean(el.get_text())
        else:
            href = el.get("href") or ""
            if "/about/faculty-staff/" not in href or not href.endswith(".php") or not department:
                continue
            slug = href.rstrip("/").split("/")[-1].removesuffix(".php")
            display = _clean(el.get_text())
            if not display:
                continue
            if slug in people:                      # listed under two departments
                deps = people[slug]["departments"]
                if department not in deps:
                    deps.append(department)
                continue
            m = SALUTATION_RE.match(display)
            people[slug] = {
                "slug": slug,
                "name": SALUTATION_RE.sub("", display).strip(),
                "salutation": (m.group(1) + ".") if m else "",
                "department": department,
                "departments": [department],
                "category": category,
                "profile_url": href,
            }
    return list(people.values())


def parse_profile_page(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    out = {"title": "", "email": "", "scholar_id": "", "orcid": ""}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("mailto:"):
            continue                                     # emails are deliberately not stored
        m = SCHOLAR_ID_RE.search(href)
        if m and "scholar.google" in href and not out["scholar_id"]:
            out["scholar_id"] = m.group(1)
        m = ORCID_RE.search(href)
        if m and not out["orcid"]:
            out["orcid"] = m.group(1).upper()
    heading = None
    for h in soup.find_all(["h1", "h2"]):
        if SALUTATION_RE.match(_clean(h.get_text())):
            heading = h
            break
    if heading is not None:
        for sib in heading.find_all_next(["p", "h2", "h3", "h4", "span", "li"], limit=12):
            text = _clean(sib.get_text(" "))
            if not text or text.lower().startswith(("email", "ext", "tel", "view cv")) or "@" in text:
                continue
            if text.lower() in ("biography", "bio", "about", "research interests", "teaching interests"):
                break                                    # page has no title line
            if len(text) < 140:
                out["title"] = text
            break
    return out


def read_faculty_csv(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return {row["slug"]: row for row in csv.DictReader(fh) if row.get("slug")}


def write_faculty_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in sorted(rows, key=lambda r: (str(r.get("active", "1")) != "1", r.get("department", ""), r.get("name", ""))):
            writer.writerow({c: row.get(c, "") for c in CSV_COLUMNS})


def build_roster(cfg: Config, db: Database, fetch_profiles: bool = True) -> list[dict[str, Any]]:
    http = Http(BROWSER_HEADERS, min_interval=0.8, impersonate=True)
    log.info("Fetching roster page %s", cfg.roster_url)
    resp = http.get(cfg.roster_url)
    resp.raise_for_status()
    people = parse_roster_page(resp.text)
    if not people:
        raise RuntimeError("No faculty found on roster page - the page layout may have changed.")
    log.info("Found %d people in %d departments", len(people), len({p['department'] for p in people}))

    existing = read_faculty_csv(cfg.faculty_csv)
    rows: list[dict[str, Any]] = []
    seen = set()
    fetched = 0
    for p in people:
        seen.add(p["slug"])
        is_faculty = p["category"] in cfg.include_categories
        old = existing.get(p["slug"], {})
        row: dict[str, Any] = {
            **p,
            "departments": "; ".join(p["departments"]),
            "title": old.get("title", ""),
            "email": "",
            "orcid": old.get("orcid", ""),
            "scholar_id": old.get("scholar_id", ""),
            "openalex_id": old.get("openalex_id", ""),
            "include": old.get("include") or ("1" if is_faculty else "0"),
            "active": "1",
            "manual": old.get("manual", ""),
            "notes": old.get("notes", ""),
        }
        if row["title"].strip().lower() in ("biography", "bio", "about"):
            row["title"] = ""
        needs_profile = not old or not row["title"]
        if fetch_profiles and is_faculty and needs_profile:   # new people, or details still missing
            try:
                pr = http.get(p["profile_url"])
                fetched += 1
                if pr.ok:
                    info = parse_profile_page(pr.text)
                    row["title"] = row["title"] or info["title"]
                    row["orcid"] = row["orcid"] or info["orcid"]
                    row["scholar_id"] = row["scholar_id"] or info["scholar_id"]
            except Exception as exc:  # profile pages are nice-to-have
                log.warning("Profile fetch failed for %s: %s", p["slug"], exc)
        rows.append(row)

    for slug, old in existing.items():            # people no longer listed on the site
        if slug not in seen:
            old = dict(old)
            if str(old.get("manual", "")).strip().lower() not in TRUE_VALUES:
                old["active"] = "0"
            rows.append(old)

    write_faculty_csv(cfg.faculty_csv, rows)
    sync_faculty_to_db(rows, db)
    log.info("Roster written to %s (%d included faculty, %d profile pages read)", cfg.faculty_csv,
             sum(1 for r in rows if str(r.get("include")) == "1" and str(r.get("active")) == "1"), fetched)
    return rows


def sync_faculty_to_db(rows: list[dict[str, Any]], db: Database) -> None:
    for r in rows:
        db.upsert_faculty({
            "slug": r["slug"], "name": r["name"], "salutation": r.get("salutation", ""),
            "title": r.get("title", ""), "department": r.get("department", ""),
            "departments": r.get("departments") or r.get("department", ""), "category": r.get("category", ""),
            "email": r.get("email", ""), "profile_url": r.get("profile_url", ""),
            "scholar_id": (r.get("scholar_id") or "").strip() or None,
            "openalex_id": (r.get("openalex_id") or "").strip().replace("https://openalex.org/", "") or None,
            "orcid": (r.get("orcid") or "").strip() or None,
            "include": 1 if str(r.get("include", "1")).strip().lower() in TRUE_VALUES else 0,
            "active": 1 if str(r.get("active", "1")).strip().lower() in TRUE_VALUES else 0,
            "notes": r.get("notes", ""),
        })


def load_roster_from_csv(cfg: Config, db: Database) -> int:
    """Sync data/faculty.csv (possibly hand-edited) into the database."""
    rows = list(read_faculty_csv(cfg.faculty_csv).values())
    sync_faculty_to_db(rows, db)
    return len(rows)


def write_back_ids(cfg: Config, db: Database) -> None:
    """Persist IDs resolved automatically (OpenAlex / Scholar discovery) into the CSV."""
    rows = read_faculty_csv(cfg.faculty_csv)
    changed = False
    for f in db.faculty(include_only=False, active_only=False):
        row = rows.get(f["slug"])
        if row is None:
            continue
        for col in ("openalex_id", "scholar_id", "title", "orcid"):
            if f[col] and not (row.get(col) or "").strip():
                row[col] = f[col]
                changed = True
    if changed:
        write_faculty_csv(cfg.faculty_csv, list(rows.values()))
