"""Command line interface: python -m aksob_pubs <command>."""
from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from . import roster as roster_mod
from .bibtex import import_exports
from .config import Config, load_config
from .dashboard import build_dashboard
from .db import Database, now_iso
from .enrich import Enricher
from .rank import rank_publications
from .report import build_report
from .scimago import ScimagoIndex
from .sources import SOURCE_CLASSES, OpenAlexSource, OrcidSource, build_sources
from .sources.base import SourceUnavailable
from .utils import QuotaExceeded

log = logging.getLogger("aksob")


def setup_logging(cfg: Config, verbose: bool) -> None:
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = logging.FileHandler(cfg.output_dir / "run.log", encoding="utf-8")
    fh.setFormatter(fmt)
    root.handlers[:] = [sh, fh]
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def merge_status(old: str | None, source: str, new: str) -> str:
    """Keep one status fragment per source: 'openalex: 120 records | orcid: 80 records'."""
    parts = [p for p in (old or "").split(" | ") if p and not p.startswith(source + ":")]
    parts.append(new)
    return " | ".join(parts)


# ------------------------------------------------------------------ stages
def stage_roster(cfg: Config, db: Database, profiles: bool) -> None:
    rid = db.run_start("roster")
    rows = roster_mod.build_roster(cfg, db, fetch_profiles=profiles)
    db.run_finish(rid, f"{len(rows)} people")


def stage_resolve(cfg: Config, db: Database, force: bool = False) -> None:
    oa = OpenAlexSource(cfg, db)
    found = missing = 0
    for f in db.faculty():
        if f["openalex_id"] and not force:
            continue
        try:
            r = oa.resolve_author(f["name"])
        except QuotaExceeded as exc:
            log.error("OpenAlex quota reached - remaining faculty will be resolved on the next run. (%s)", str(exc)[:160])
            break
        except Exception as exc:
            log.warning("OpenAlex lookup failed for %s: %s", f["name"], exc)
            continue
        if r:
            if f["orcid"]:
                r.pop("orcid", None)
            db.update_faculty(f["slug"], **{k: v for k, v in r.items() if v is not None})
            log.info("  %-28s -> %s (%s, %s works)", f["name"], r["openalex_id"], r["openalex_name"], r["openalex_works"])
            found += 1
        else:
            log.info("  %-28s -> not found in OpenAlex with an LAU affiliation", f["name"])
            missing += 1
    # ORCID name search (free, no budget) for anyone still without an ORCID
    orcid_src = OrcidSource(cfg, db)
    orcid_found = 0
    for f in db.faculty():
        if f["orcid"]:
            continue
        try:
            hit = orcid_src.search(f["name"])
        except Exception as exc:
            log.warning("ORCID search failed for %s: %s", f["name"], exc)
            continue
        if hit:
            db.update_faculty(f["slug"], orcid=hit["orcid"])
            log.info("  %-28s -> ORCID %s (%s)", f["name"], hit["orcid"], hit["name"])
            orcid_found += 1
    roster_mod.write_back_ids(cfg, db)
    log.info("OpenAlex ids: %d resolved, %d not found; ORCIDs found by name: %d", found, missing, orcid_found)


def stage_fetch(cfg: Config, db: Database, source_names: list[str] | None, only: str | None, limit: int | None) -> None:
    sources = build_sources(cfg, db, source_names)
    faculty = db.faculty()
    if only:
        wanted = {s.strip() for s in only.split(",")}
        faculty = [f for f in faculty if f["slug"] in wanted or f["name"] in wanted]
    faculty.sort(key=lambda f: (f["last_fetched"] or "", f["name"]))   # stalest first
    if limit:
        faculty = faculty[:limit]
    for src in sources:
        ok, msg = src.available()
        if not ok:
            log.error("Source %s skipped: %s", src.name, msg)
            continue
        rid = db.run_start(f"fetch:{src.name}")
        log.info("=== Fetching from %s for %d faculty", src.name, len(faculty))
        total_new = total_seen = 0
        try:
            for f in faculty:
                try:
                    pubs, updates = src.fetch(f)
                except (SourceUnavailable, QuotaExceeded) as exc:
                    log.error("Source %s stopped: %s", src.name, str(exc)[:300])
                    break
                except Exception as exc:  # one faculty must not kill the run
                    log.exception("  %s: %s failed: %s", f["name"], src.name, exc)
                    db.update_faculty(f["slug"], fetch_status=merge_status(
                        f["fetch_status"], src.name, f"{src.name}: error {str(exc)[:120]}"))
                    continue
                new = 0
                for rec in pubs:
                    _, created = db.upsert_publication(f["slug"], rec.as_dict(), src.name)
                    new += int(created)
                db.commit()
                total_new += new
                total_seen += len(pubs)
                status = updates.pop("fetch_status", None) or f"{src.name}: {len(pubs)} records ({new} new)"
                updates["fetch_status"] = merge_status(f["fetch_status"], src.name, status)
                if pubs or not status.startswith(f"{src.name}: no"):
                    updates["last_fetched"] = now_iso()
                db.update_faculty(f["slug"], **updates)
                log.info("  %-28s %-9s %4d records, %3d new", f["name"], src.name, len(pubs), new)
        finally:
            src.close()
        db.run_finish(rid, f"{total_seen} records, {total_new} new")
    roster_mod.write_back_ids(cfg, db)


def stage_import_bibtex(cfg: Config, db: Database) -> None:
    rid = db.run_start("import-bibtex")
    stats = import_exports(cfg, db)
    if stats["files"]:
        log.info("Scholar exports: %s", stats)
    else:
        log.info("No Scholar exports in %s (optional: profile > select all > Export > BibTeX)", cfg.scholar_exports_dir)
    db.run_finish(rid, str(stats))


def stage_enrich(cfg: Config, db: Database, force: bool, limit: int | None) -> None:
    rid = db.run_start("enrich")
    stats = Enricher(cfg, db, OpenAlexSource(cfg, db)).enrich(force=force, limit=limit)
    log.info("Enrichment: %s", stats)
    db.run_finish(rid, str(stats))


def stage_rank(cfg: Config, db: Database, scimago: ScimagoIndex, rerank: bool) -> None:
    rid = db.run_start("rank")
    stats = rank_publications(cfg, db, scimago, rerank=rerank)
    log.info("Ranking: %s", stats)
    db.run_finish(rid, str(stats))


def stage_outputs(cfg: Config, db: Database, scimago: ScimagoIndex | None) -> None:
    years = scimago.years() if scimago else None
    build_report(cfg, db, years)
    build_dashboard(cfg, db, years)


def cmd_status(cfg: Config, db: Database) -> None:
    fac = db.faculty()
    print(f"Faculty included: {len(fac)}  with OpenAlex id: {sum(1 for f in fac if f['openalex_id'])}  "
          f"with ORCID: {sum(1 for f in fac if f['orcid'])}  with Scholar export: {sum(1 for f in fac if f['scholar_import_at'])}")
    rows = db.conn.execute("SELECT COALESCE(quartile, COALESCE(match_method,'unranked')) AS k, COUNT(*) n "
                           "FROM publications GROUP BY k ORDER BY n DESC").fetchall()
    total = sum(r["n"] for r in rows)
    print(f"Publications (faculty x paper rows): {total}")
    for r in rows:
        print(f"  {r['k']:<16} {r['n']}")
    print("Sources configured:", ", ".join(cfg.sources), "| OpenAlex key:", "yes" if cfg.openalex_api_key else "no (shared daily budget)")
    print("Recent runs:")
    for r in db.last_runs(8):
        print(f"  {r['started']}  {r['stage']:<22} {r['notes'] or ''}")


# -------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m aksob_pubs", description="AKSOB Research Tracker")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("roster", help="Scrape the AKSOB faculty list into data/faculty.csv")
    p.add_argument("--no-profiles", action="store_true", help="Do not open individual profile pages")
    sub.add_parser("sync", help="Load a hand-edited data/faculty.csv into the database")
    p = sub.add_parser("resolve", help="Find OpenAlex author ids (and ORCIDs) for faculty")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("fetch", help="Retrieve publications from the configured sources")
    p.add_argument("--source", help=f"comma list of {', '.join(SOURCE_CLASSES)} (default: SOURCES env)")
    p.add_argument("--faculty", help="comma list of slugs or names to restrict the fetch")
    p.add_argument("--limit", type=int)
    sub.add_parser("import-bibtex", help="Import Google Scholar BibTeX exports from data/scholar_exports/")
    p = sub.add_parser("enrich", help="Resolve DOI / ISSN / journal for records that lack them")
    p.add_argument("--force", action="store_true")
    p.add_argument("--limit", type=int)
    sub.add_parser("scimago", help="Download / refresh the Scimago yearly rankings")
    p = sub.add_parser("rank", help="Assign Scimago quartiles")
    p.add_argument("--rerank", action="store_true", help="Re-evaluate every publication")
    sub.add_parser("report", help="Write the Excel workbook and CSV")
    sub.add_parser("dashboard", help="Write the HTML app")
    p = sub.add_parser("run", help="Full pipeline: roster, resolve, fetch, import-bibtex, enrich, rank, report, dashboard")
    p.add_argument("--skip-roster", action="store_true")
    p.add_argument("--no-profiles", action="store_true")
    p.add_argument("--source")
    p.add_argument("--faculty")
    p.add_argument("--limit", type=int)
    p.add_argument("--rerank", action="store_true")
    sub.add_parser("status", help="Show counts and recent runs")

    args = ap.parse_args(argv)
    cfg = load_config()
    setup_logging(cfg, args.verbose)
    db = Database(cfg.db_path)
    try:
        if args.cmd == "roster":
            stage_roster(cfg, db, profiles=not args.no_profiles)
        elif args.cmd == "sync":
            n = roster_mod.load_roster_from_csv(cfg, db)
            log.info("Synced %d faculty rows from %s", n, cfg.faculty_csv)
        elif args.cmd == "resolve":
            stage_resolve(cfg, db, force=args.force)
        elif args.cmd == "fetch":
            stage_fetch(cfg, db, args.source.split(",") if args.source else None, args.faculty, args.limit)
        elif args.cmd == "import-bibtex":
            stage_import_bibtex(cfg, db)
        elif args.cmd == "enrich":
            stage_enrich(cfg, db, args.force, args.limit)
        elif args.cmd == "scimago":
            years = ScimagoIndex(cfg.scimago_dir).load_range(cfg.scimago_from)
            log.info("Scimago years available: %s", years)
        elif args.cmd == "rank":
            stage_rank(cfg, db, ScimagoIndex(cfg.scimago_dir), args.rerank)
        elif args.cmd == "report":
            build_report(cfg, db)
        elif args.cmd == "dashboard":
            build_dashboard(cfg, db)
        elif args.cmd == "status":
            cmd_status(cfg, db)
        elif args.cmd == "run":
            if not args.skip_roster:
                try:
                    stage_roster(cfg, db, profiles=not args.no_profiles)
                except Exception as exc:  # the website being down must not stop the weekly refresh
                    log.error("Roster refresh failed (%s) - continuing with the existing faculty.csv", exc)
                    roster_mod.load_roster_from_csv(cfg, db)
            elif cfg.faculty_csv.exists():
                roster_mod.load_roster_from_csv(cfg, db)
            stage_resolve(cfg, db)
            stage_fetch(cfg, db, args.source.split(",") if args.source else None, args.faculty, args.limit)
            stage_import_bibtex(cfg, db)
            stage_enrich(cfg, db, force=False, limit=None)
            scimago = ScimagoIndex(cfg.scimago_dir)
            stage_rank(cfg, db, scimago, args.rerank)
            stage_outputs(cfg, db, scimago)
            log.info("Done. Outputs in %s", cfg.output_dir)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
