"""SQLite storage: faculty, publications (one row per faculty x publication), caches."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from rapidfuzz import fuzz

from .utils import norm_issn, title_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS faculty (
    slug TEXT PRIMARY KEY,
    name TEXT, salutation TEXT, title TEXT, department TEXT, departments TEXT, category TEXT,
    email TEXT, profile_url TEXT, scholar_id TEXT, openalex_id TEXT, orcid TEXT,
    include INTEGER DEFAULT 1, active INTEGER DEFAULT 1, notes TEXT,
    scholar_name TEXT, scholar_affiliation TEXT, scholar_citations INTEGER, scholar_h_index INTEGER, scholar_i10 INTEGER,
    scholar_articles INTEGER, scholar_import_at TEXT,
    openalex_name TEXT, openalex_works INTEGER, openalex_citations INTEGER, openalex_h_index INTEGER,
    last_fetched TEXT, fetch_status TEXT
);
CREATE TABLE IF NOT EXISTS publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    faculty_slug TEXT NOT NULL,
    title TEXT, title_key TEXT, year INTEGER,
    venue_raw TEXT, venue TEXT, authors TEXT,
    doi TEXT, issns TEXT, type TEXT,
    cited_by_scholar INTEGER, cited_by_openalex INTEGER,
    scholar_pub_id TEXT, openalex_work_id TEXT, openalex_source_id TEXT, abdc TEXT,
    sources TEXT,
    enriched_at TEXT, enrich_status TEXT,
    scimago_year INTEGER, scimago_sourceid TEXT, scimago_title TEXT, quartile TEXT, sjr REAL,
    journal_h_index INTEGER, categories TEXT, areas TEXT, publisher TEXT, country TEXT,
    match_method TEXT, match_score REAL, ranked_at TEXT,
    first_seen TEXT, last_seen TEXT,
    FOREIGN KEY (faculty_slug) REFERENCES faculty(slug)
);
CREATE INDEX IF NOT EXISTS idx_pub_fac ON publications(faculty_slug);
CREATE INDEX IF NOT EXISTS idx_pub_doi ON publications(doi);
CREATE INDEX IF NOT EXISTS idx_pub_tkey ON publications(title_key);
CREATE TABLE IF NOT EXISTS enrich_cache (
    title_key TEXT PRIMARY KEY, payload TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, stage TEXT, started TEXT, finished TEXT, notes TEXT
);
"""

PUB_FIELDS = (
    "title", "year", "venue_raw", "venue", "authors", "doi", "issns", "type",
    "cited_by_scholar", "cited_by_openalex", "scholar_pub_id", "openalex_work_id",
    "openalex_source_id", "abdc",
)

RANK_FIELDS = (
    "scimago_year", "scimago_sourceid", "scimago_title", "quartile", "sjr", "journal_h_index",
    "categories", "areas", "publisher", "country", "match_method", "match_score",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created (idempotent)."""
        wanted = {"faculty": [("scholar_articles", "INTEGER"), ("scholar_import_at", "TEXT")],
                  "publications": [("areas", "TEXT"), ("publisher", "TEXT"), ("country", "TEXT")]}
        for table, cols in wanted.items():
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols:
                if col not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.conn.commit()

    # ------------------------------------------------------------------ faculty
    def upsert_faculty(self, rec: dict[str, Any]) -> None:
        cols = list(rec.keys())
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "slug")
        self.conn.execute(
            f"INSERT INTO faculty ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(slug) DO UPDATE SET {updates}",
            [rec[c] for c in cols],
        )
        self.conn.commit()

    def update_faculty(self, slug: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ",".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE faculty SET {sets} WHERE slug=?", [*fields.values(), slug])
        self.conn.commit()

    def faculty(self, include_only: bool = True, active_only: bool = True) -> list[sqlite3.Row]:
        where = []
        if include_only:
            where.append("include=1")
        if active_only:
            where.append("active=1")
        sql = "SELECT * FROM faculty" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY department, name"
        return list(self.conn.execute(sql))

    def get_faculty(self, slug: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM faculty WHERE slug=?", (slug,)).fetchone()

    # -------------------------------------------------------------- publications
    def _find_existing(self, slug: str, rec: dict[str, Any]) -> sqlite3.Row | None:
        c = self.conn
        doi = (rec.get("doi") or "").lower() or None
        if doi:
            row = c.execute("SELECT * FROM publications WHERE faculty_slug=? AND lower(doi)=?", (slug, doi)).fetchone()
            if row:
                return row
        if rec.get("scholar_pub_id"):
            row = c.execute("SELECT * FROM publications WHERE faculty_slug=? AND scholar_pub_id=?",
                            (slug, rec["scholar_pub_id"])).fetchone()
            if row:
                return row
        if rec.get("openalex_work_id"):
            row = c.execute("SELECT * FROM publications WHERE faculty_slug=? AND openalex_work_id=?",
                            (slug, rec["openalex_work_id"])).fetchone()
            if row:
                return row
        tkey = title_key(rec.get("title"))
        if not tkey:
            return None

        def distinct(row: sqlite3.Row) -> bool:
            """Two different Google Scholar entries (or two different OpenAlex works) are never merged."""
            for k in ("scholar_pub_id", "openalex_work_id"):
                if rec.get(k) and row[k] and row[k] != rec[k]:
                    return True
            return False

        year = rec.get("year")
        rows = c.execute("SELECT * FROM publications WHERE faculty_slug=? AND title_key=?", (slug, tkey)).fetchall()
        for row in rows:
            if distinct(row):
                continue
            if year is None or row["year"] is None or abs(int(row["year"]) - int(year)) <= 1:
                return row
        # fuzzy fallback on titles of the same faculty within +/-1 year (punctuation / subtitle drift)
        if year is not None and len(tkey) >= 25:
            cands = c.execute(
                "SELECT * FROM publications WHERE faculty_slug=? AND year BETWEEN ? AND ? AND title_key IS NOT NULL",
                (slug, int(year) - 1, int(year) + 1)).fetchall()
            best, best_score = None, 0.0
            for row in cands:
                if distinct(row):
                    continue
                score = fuzz.ratio(tkey, row["title_key"] or "")
                if score > best_score:
                    best, best_score = row, score
            if best is not None and best_score >= 96:
                return best
        return None

    def upsert_publication(self, slug: str, rec: dict[str, Any], source: str) -> tuple[int, bool]:
        """Insert or merge a publication record. Returns (row id, created?)."""
        rec = dict(rec)
        if isinstance(rec.get("issns"), (list, tuple, set)):
            issns = sorted({i for i in (norm_issn(x) for x in rec["issns"]) if i})
            rec["issns"] = json.dumps(issns) if issns else None
        elif not rec.get("issns"):
            rec["issns"] = None
        if rec.get("doi"):
            rec["doi"] = str(rec["doi"]).lower().replace("https://doi.org/", "").strip() or None
        existing = self._find_existing(slug, rec)
        ts = now_iso()
        if existing is None:
            fields = {k: rec.get(k) for k in PUB_FIELDS}
            fields.update(faculty_slug=slug, title_key=title_key(rec.get("title")), sources=source,
                          first_seen=ts, last_seen=ts)
            cols = list(fields)
            cur = self.conn.execute(
                f"INSERT INTO publications ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                [fields[c] for c in cols])
            return int(cur.lastrowid), True

        updates: dict[str, Any] = {"last_seen": ts}
        sources = set((existing["sources"] or "").split(",")) - {""}
        sources.add(source)
        updates["sources"] = ",".join(sorted(sources))
        for k in PUB_FIELDS:
            new = rec.get(k)
            if new in (None, "", "[]"):
                continue
            old = existing[k]
            if k in ("cited_by_scholar", "cited_by_openalex"):
                updates[k] = new                      # citation counts always refresh
            elif k == "title" and source.startswith("scholar"):
                updates[k] = new                      # Scholar carries the faculty-curated title
                updates["title_key"] = title_key(new)
            elif old in (None, ""):
                updates[k] = new
        if updates.get("issns") and not existing["issns"]:
            updates["ranked_at"] = None               # new identifiers -> rank again
        sets = ",".join(f"{k}=?" for k in updates)
        self.conn.execute(f"UPDATE publications SET {sets} WHERE id=?", [*updates.values(), existing["id"]])
        return int(existing["id"]), False

    def update_publication(self, pub_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ",".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE publications SET {sets} WHERE id=?", [*fields.values(), pub_id])

    def publications(self, where: str = "", params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        sql = "SELECT * FROM publications" + (f" WHERE {where}" if where else "") + " ORDER BY year DESC, title"
        return list(self.conn.execute(sql, tuple(params)))

    def publications_joined(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT p.*, f.name AS faculty_name, f.department, f.category "
            "FROM publications p JOIN faculty f ON f.slug = p.faculty_slug "
            "WHERE f.include=1 AND f.active=1 ORDER BY p.year DESC, p.title"))

    def delete_publications(self, slug: str, source: str | None = None) -> int:
        if source:
            cur = self.conn.execute("DELETE FROM publications WHERE faculty_slug=? AND sources=?", (slug, source))
        else:
            cur = self.conn.execute("DELETE FROM publications WHERE faculty_slug=?", (slug,))
        self.conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------- cache
    def cache_get(self, key: str) -> dict | None:
        row = self.conn.execute("SELECT payload FROM enrich_cache WHERE title_key=?", (key,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def cache_set(self, key: str, payload: dict | None) -> None:
        self.conn.execute("INSERT OR REPLACE INTO enrich_cache (title_key, payload, fetched_at) VALUES (?,?,?)",
                          (key, json.dumps(payload), now_iso()))

    # -------------------------------------------------------------------- runs
    def run_start(self, stage: str) -> int:
        cur = self.conn.execute("INSERT INTO runs (stage, started) VALUES (?,?)", (stage, now_iso()))
        self.conn.commit()
        return int(cur.lastrowid)

    def run_finish(self, run_id: int, notes: str = "") -> None:
        self.conn.execute("UPDATE runs SET finished=?, notes=? WHERE id=?", (now_iso(), notes, run_id))
        self.conn.commit()

    def last_runs(self, n: int = 10) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (n,)))

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
