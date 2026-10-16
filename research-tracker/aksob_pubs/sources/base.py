from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

from ..config import Config
from ..db import Database


@dataclass
class PubRecord:
    title: str
    year: int | None = None
    venue_raw: str = ""
    venue: str = ""
    authors: str = ""
    doi: str | None = None
    issns: list[str] = field(default_factory=list)
    type: str = ""
    cited_by_scholar: int | None = None
    cited_by_openalex: int | None = None
    scholar_pub_id: str | None = None
    openalex_work_id: str | None = None
    openalex_source_id: str | None = None
    abdc: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class SourceUnavailable(RuntimeError):
    """Raised when a source cannot run at all (missing key, blocked, budget exhausted)."""


class Source:
    name = "base"

    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db

    def available(self) -> tuple[bool, str]:
        return True, ""

    def fetch(self, faculty: sqlite3.Row | dict) -> tuple[list[PubRecord], dict[str, Any]]:
        """Return (publications, faculty-level updates such as h-index / resolved ids)."""
        raise NotImplementedError

    def close(self) -> None:
        """Release resources. Default: nothing to do."""
