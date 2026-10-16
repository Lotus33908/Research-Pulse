"""Runtime configuration (environment variables / .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .utils import load_dotenv

FACULTY_CATEGORIES = ("Full-Time Faculty", "Adjunct Faculty", "Part-Time Faculty", "Visiting Faculty")


@dataclass
class Config:
    root: Path
    data_dir: Path
    output_dir: Path
    docs_dir: Path
    db_path: Path
    faculty_csv: Path
    overrides_csv: Path
    scimago_dir: Path
    scholar_exports_dir: Path             # drop Google Scholar BibTeX exports here (one file per person)

    roster_url: str = "https://sb.lau.edu.lb/about/faculty-staff/"
    school_name: str = "Adnan Kassar School of Business"
    university_name: str = "Lebanese American University"
    openalex_institution_id: str = "I56306041"  # Lebanese American University
    openalex_mailto: str = ""
    openalex_api_key: str | None = None   # free key from openalex.org - lifts the shared per-IP daily budget
    sources: list[str] = field(default_factory=lambda: ["openalex", "orcid"])
    year_from: int = 1900                 # 1900 = keep everything
    scimago_from: int = 1999              # first Scimago edition to load (1999 is the earliest)
    fuzzy_threshold: int = 94
    include_categories: tuple[str, ...] = FACULTY_CATEGORIES


def load_config(root: str | os.PathLike | None = None) -> Config:
    root_path = Path(root) if root else Path(__file__).resolve().parent.parent
    load_dotenv(str(root_path / ".env"))

    data_dir = root_path / "data"
    output_dir = root_path / "output"
    docs_dir = root_path.parent / "docs"

    sources_env = os.environ.get("SOURCES", "").strip()
    sources = [s.strip() for s in sources_env.split(",") if s.strip()] if sources_env else ["openalex", "orcid"]

    cats_env = os.environ.get("INCLUDE_CATEGORIES", "").strip()
    cats = tuple(c.strip() for c in cats_env.split(";") if c.strip()) if cats_env else FACULTY_CATEGORIES

    return Config(
        root=root_path,
        data_dir=data_dir,
        output_dir=output_dir,
        docs_dir=docs_dir,
        db_path=Path(os.environ["AKSOB_DB"]) if os.environ.get("AKSOB_DB") else data_dir / "aksob_pubs.db",
        faculty_csv=data_dir / "faculty.csv",
        overrides_csv=data_dir / "journal_overrides.csv",
        scimago_dir=data_dir / "scimago",
        scholar_exports_dir=data_dir / "scholar_exports",
        openalex_mailto=os.environ.get("OPENALEX_MAILTO", ""),
        openalex_api_key=os.environ.get("OPENALEX_API_KEY") or None,
        sources=sources,
        year_from=int(os.environ.get("YEAR_FROM", "1900")),
        scimago_from=int(os.environ.get("SCIMAGO_FROM", "1999")),
        fuzzy_threshold=int(os.environ.get("FUZZY_THRESHOLD", "94")),
        include_categories=cats,
    )
