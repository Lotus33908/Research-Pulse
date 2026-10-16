"""Scimago Journal Rank (SJR): download the yearly ranking, index it, match venues to journals.

Scimago publishes one CSV per year (about 32,000 sources). "SJR Best Quartile" is the Q1-Q4
label most people mean by "a Q1 journal". A paper is matched against the ranking of its own
publication year (nearest available year otherwise), first by ISSN, then by exact title, then
by a strict fuzzy title match.
"""
from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from rapidfuzz import fuzz, process

from .utils import BROWSER_HEADERS, Http, norm_issn, norm_text

log = logging.getLogger("aksob")
URL = "https://www.scimagojr.com/journalrank.php?out=xls&year={year}"
MIN_YEAR = 1999


@dataclass(slots=True)
class Journal:
    year: int
    sourceid: str
    title: str
    type: str
    issns: tuple[str, ...]
    sjr: float | None
    quartile: str | None
    h_index: int | None
    categories: str
    areas: str
    publisher: str
    country: str


def _float(s: str | None) -> float | None:
    try:
        return float((s or "").strip().replace(",", "."))
    except ValueError:
        return None


def _int(s: str | None) -> int | None:
    try:
        return int((s or "").strip())
    except ValueError:
        return None


class YearIndex:
    def __init__(self, year: int, rows: list[dict[str, str]]):
        self.year = year
        self.by_issn: dict[str, Journal] = {}
        self.by_title: dict[str, Journal] = {}
        self._fuzzy_map: dict[str, Journal] = {}
        for r in rows:
            quartile = (r.get("SJR Best Quartile") or "").strip()
            j = Journal(
                year=year,
                sourceid=(r.get("Sourceid") or "").strip(),
                title=(r.get("Title") or "").strip(),
                type=(r.get("Type") or "").strip(),
                issns=tuple(i for i in (norm_issn(x) for x in (r.get("Issn") or "").split(",")) if i),
                sjr=_float(r.get("SJR")),
                quartile=quartile if quartile.startswith("Q") else None,
                h_index=_int(r.get("H index")),
                categories=(r.get("Categories") or "").strip(),
                areas=(r.get("Areas") or "").strip(),
                publisher=(r.get("Publisher") or "").strip(),
                country=(r.get("Country") or "").strip(),
            )
            for issn in j.issns:
                self.by_issn.setdefault(issn, j)
            nt = norm_text(j.title)
            if nt:
                self.by_title.setdefault(nt, j)
                if j.type == "journal":
                    self._fuzzy_map.setdefault(nt, j)
        self._fuzzy_titles = list(self._fuzzy_map)

    @classmethod
    def from_csv(cls, year: int, path: Path) -> "YearIndex":
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh, delimiter=";"))
        return cls(year, rows)

    def by_issns(self, issns: list[str]) -> Journal | None:
        for i in issns:
            j = self.by_issn.get(i)
            if j:
                return j
        return None

    def by_exact_title(self, venue: str) -> Journal | None:
        return self.by_title.get(norm_text(venue))

    def by_fuzzy_title(self, venue: str, threshold: int) -> tuple[Journal | None, float]:
        nt = norm_text(venue)
        if len(nt) < 12 or " " not in nt:
            return None, 0.0
        hit = process.extractOne(nt, self._fuzzy_titles, scorer=fuzz.ratio, score_cutoff=threshold)
        if not hit:
            return None, 0.0
        return self._fuzzy_map[hit[0]], float(hit[1])


class ScimagoIndex:
    def __init__(self, scimago_dir: Path):
        self.dir = scimago_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.http = Http({**BROWSER_HEADERS, "Referer": "https://www.scimagojr.com/journalrank.php"},
                         min_interval=1.5, timeout=180, impersonate=True)
        self._years: dict[int, YearIndex | None] = {}
        self._meta_path = self.dir / "meta.json"
        try:
            self._meta = json.loads(self._meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._meta = {}

    def _save_meta(self) -> None:
        self._meta_path.write_text(json.dumps(self._meta, indent=1), encoding="utf-8")

    def path(self, year: int) -> Path:
        return self.dir / f"scimagojr_{year}.csv"

    def download(self, year: int) -> bool:
        resp = self.http.get(URL.format(year=year))
        text = resp.text if resp.ok else ""
        if not text.startswith("Rank;"):          # Cloudflare challenge page or "no data for year"
            return False
        # Scimago answers a request for a not-yet-published year with its latest edition; the header
        # carries the real year ("Total Docs. (2025)"), so refuse anything that is not the year asked for.
        header = text.split("\n", 1)[0]
        if f"Total Docs. ({year})" not in header:
            log.info("Scimago %d is not published yet (server returned another edition)", year)
            return False
        self.path(year).write_text(text, encoding="utf-8")
        return True

    def ensure(self, year: int) -> YearIndex | None:
        if year in self._years:
            return self._years[year]
        p = self.path(year)
        if not p.exists():
            missing: dict[str, str] = self._meta.setdefault("missing", {})
            checked = missing.get(str(year))
            if checked and (date.today() - date.fromisoformat(checked)).days < 7:
                self._years[year] = None
                return None
            log.info("Downloading Scimago %d ...", year)
            ok = False
            try:
                ok = self.download(year)
            except Exception as exc:
                log.warning("Scimago %d download failed: %s", year, exc)
            if not ok:
                missing[str(year)] = date.today().isoformat()
                self._save_meta()
                self._years[year] = None
                return None
            missing.pop(str(year), None)
            self._save_meta()
        idx = YearIndex.from_csv(year, p)
        self._years[year] = idx
        log.info("Scimago %d: %d sources indexed", year, len(idx.by_title))
        return idx

    def load_range(self, year_from: int) -> list[int]:
        loaded = []
        for y in range(max(MIN_YEAR, year_from), date.today().year + 1):
            if self.ensure(y) is not None:
                loaded.append(y)
        return loaded

    def years(self) -> list[int]:
        return sorted(y for y, v in self._years.items() if v is not None)

    def index(self, year: int) -> YearIndex | None:
        return self._years.get(year)


def cached_years(scimago_dir: Path) -> list[int]:
    """Editions already on disk (for reports built outside a full run)."""
    out = []
    for p in scimago_dir.glob("scimagojr_*.csv"):
        try:
            out.append(int(p.stem.split("_")[1]))
        except (IndexError, ValueError):
            pass
    return sorted(out)


def year_preference(target: int | None, available: list[int]) -> list[int]:
    """Available Scimago years ordered by closeness to the publication year (later year wins ties)."""
    if not available:
        return []
    if target is None:
        return [max(available)]
    return sorted(available, key=lambda y: (abs(y - target), -y))
