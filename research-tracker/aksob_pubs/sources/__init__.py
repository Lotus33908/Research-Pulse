"""Publication sources. Each source turns a faculty row into a list of PubRecord."""
from __future__ import annotations

from ..config import Config
from ..db import Database
from .base import PubRecord, Source
from .openalex import OpenAlexSource
from .orcid import OrcidSource

SOURCE_CLASSES: dict[str, type[Source]] = {
    OpenAlexSource.name: OpenAlexSource,
    OrcidSource.name: OrcidSource,
}


def build_sources(cfg: Config, db: Database, names: list[str] | None = None) -> list[Source]:
    out: list[Source] = []
    for name in names or cfg.sources:
        cls = SOURCE_CLASSES.get(name)
        if cls is None:
            raise SystemExit(f"Unknown source '{name}'. Known: {', '.join(SOURCE_CLASSES)}")
        out.append(cls(cfg, db))
    return out


__all__ = ["PubRecord", "Source", "OpenAlexSource", "OrcidSource", "SOURCE_CLASSES", "build_sources"]
