"""Aggregations shared by the Excel report and the dashboard export."""
from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from datetime import date
from typing import Any, Iterable

QUARTILES = ("Q1", "Q2", "Q3", "Q4")


def pub_key(p: sqlite3.Row | dict) -> str:
    """Identity of a paper across faculty rows (co-authored papers appear once per author)."""
    if p["doi"]:
        return "doi:" + str(p["doi"]).lower()
    return f"t:{p['title_key'] or ''}:{p['year']}"


def on_scholar(p: sqlite3.Row | dict) -> bool:
    """True when the paper appears in the person's own Google Scholar export."""
    return "scholar" in (p["sources"] or "")


def rank_status(p: sqlite3.Row | dict) -> str:
    if p["quartile"] in QUARTILES:
        return p["quartile"]
    method = p["match_method"] or ""
    if method == "n/a":
        return "Not applicable"
    if method and method not in ("none", "n/a"):
        return "Indexed, no quartile"
    return "Unranked"


def citations(p: sqlite3.Row | dict) -> int:
    for k in ("cited_by_scholar", "cited_by_openalex"):
        if p[k] is not None:
            return int(p[k])
    return 0


def faculty_citations(f: sqlite3.Row | dict) -> tuple[int | None, int | None, str]:
    """(citations, h-index, source label) preferring Google Scholar when it was imported."""
    if f["scholar_citations"] is not None or f["scholar_h_index"] is not None:
        return f["scholar_citations"], f["scholar_h_index"], "Google Scholar"
    if f["openalex_citations"] is not None or f["openalex_h_index"] is not None:
        return f["openalex_citations"], f["openalex_h_index"], "OpenAlex"
    return None, None, ""


def is_covered(f: sqlite3.Row | dict) -> bool:
    return bool(f["openalex_id"] or f["scholar_import_at"] or f["orcid"])


def coverage_status(f: sqlite3.Row | dict) -> str:
    """Where this person's publication list comes from."""
    parts = []
    if f["openalex_id"]:
        parts.append(f"OpenAlex record ({f['openalex_works'] or 0} works)")
    if f["orcid"]:
        parts.append("ORCID")
    if f["scholar_import_at"]:
        parts.append(f"Scholar export {str(f['scholar_import_at'])[:10]} ({f['scholar_articles'] or 0} items)")
    if not parts:
        return "NOT COVERED - no OpenAlex record found, no ORCID, no Scholar export"
    return "OK - " + ", ".join(parts)


def faculty_summary(faculty: Iterable[sqlite3.Row], pubs: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    by_fac: dict[str, list] = defaultdict(list)
    for p in pubs:
        by_fac[p["faculty_slug"]].append(p)
    recent_from = date.today().year - 4
    rows = []
    for f in faculty:
        ps = by_fac.get(f["slug"], [])
        c = Counter(rank_status(p) for p in ps)
        q12_recent = sum(1 for p in ps if p["quartile"] in ("Q1", "Q2") and p["year"] and p["year"] >= recent_from)
        ranked = sum(c[q] for q in QUARTILES)
        journal_like = ranked + c["Unranked"] + c["Indexed, no quartile"]
        cites, h, src = faculty_citations(f)
        n_scholar = sum(1 for p in ps if on_scholar(p))
        rows.append({
            "slug": f["slug"], "name": f["name"], "department": f["department"], "category": f["category"],
            "title": f["title"], "total": len(ps),
            "Q1": c["Q1"], "Q2": c["Q2"], "Q3": c["Q3"], "Q4": c["Q4"],
            "unranked": c["Unranked"] + c["Indexed, no quartile"], "na": c["Not applicable"],
            "q12": c["Q1"] + c["Q2"],
            "q12_share": (c["Q1"] + c["Q2"]) / journal_like if journal_like else None,
            "q12_recent": q12_recent,
            "abdc_top": sum(1 for p in ps if (p["abdc"] or "") in ("A*", "A")),
            "on_scholar": n_scholar, "not_in_scholar_export": (len(ps) - n_scholar) if f["scholar_import_at"] else None,
            "scholar_articles": f["scholar_articles"], "coverage": coverage_status(f), "covered": is_covered(f),
            "citations": cites, "h_index": h, "metrics_source": src,
            "scholar_id": f["scholar_id"], "openalex_id": f["openalex_id"], "orcid": f["orcid"],
            "profile_url": f["profile_url"], "fetch_status": f["fetch_status"], "last_fetched": f["last_fetched"],
        })
    return rows


def unique_pubs(pubs: Iterable[sqlite3.Row]) -> list[sqlite3.Row]:
    seen: set[str] = set()
    out = []
    for p in pubs:
        k = pub_key(p)
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def department_summary(faculty: Iterable[sqlite3.Row], pubs: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    fac_dept = {f["slug"]: f["department"] for f in faculty}
    dept_pubs: dict[str, list] = defaultdict(list)
    for p in pubs:
        dept_pubs[fac_dept.get(p["faculty_slug"], "Unknown")].append(p)
    recent_from = date.today().year - 2
    rows = []
    for dept in sorted(dept_pubs):
        ups = unique_pubs(dept_pubs[dept])
        c = Counter(rank_status(p) for p in ups)
        n_fac = sum(1 for s, d in fac_dept.items() if d == dept)
        active = {p["faculty_slug"] for p in dept_pubs[dept]
                  if p["quartile"] in ("Q1", "Q2") and p["year"] and p["year"] >= recent_from}
        rows.append({
            "department": dept, "faculty": n_fac, "total": len(ups),
            "Q1": c["Q1"], "Q2": c["Q2"], "Q3": c["Q3"], "Q4": c["Q4"],
            "unranked": c["Unranked"] + c["Indexed, no quartile"], "na": c["Not applicable"],
            "q12": c["Q1"] + c["Q2"], "faculty_q12_recent": len(active),
        })
    return rows


def year_summary(pubs: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    ups = unique_pubs(pubs)
    by_year: dict[int, Counter] = defaultdict(Counter)
    for p in ups:
        if p["year"]:
            by_year[int(p["year"])][rank_status(p)] += 1
    rows = []
    for y in sorted(by_year):
        c = by_year[y]
        rows.append({"year": y, "total": sum(c.values()), "Q1": c["Q1"], "Q2": c["Q2"], "Q3": c["Q3"],
                     "Q4": c["Q4"], "unranked": c["Unranked"] + c["Indexed, no quartile"],
                     "na": c["Not applicable"]})
    return rows


def journal_summary(pubs: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    groups: dict[str, list] = defaultdict(list)
    for p in pubs:
        name = p["scimago_title"] or p["venue"] or p["venue_raw"]
        if name and (p["match_method"] or "") != "n/a":
            groups[name].append(p)
    rows = []
    for name, ps in groups.items():
        ups = unique_pubs(ps)
        latest = max(ps, key=lambda p: (p["scimago_year"] or 0))
        rows.append({
            "journal": name, "papers": len(ups), "quartile": latest["quartile"] or "",
            "scimago_year": latest["scimago_year"], "sjr": latest["sjr"], "abdc": latest["abdc"] or "",
            "categories": latest["categories"] or "", "publisher": latest["publisher"] or "",
            "faculty": ", ".join(sorted({p["faculty_name"] for p in ps})),
        })
    rows.sort(key=lambda r: (-r["papers"], r["journal"]))
    return rows
