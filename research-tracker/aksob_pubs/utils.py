"""Shared helpers: HTTP with retries, text normalisation, venue parsing, .env loading."""
from __future__ import annotations

import logging
import os
import re
import time
import unicodedata
from typing import Any

import requests

log = logging.getLogger("aksob")

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def load_dotenv(path: str) -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing environment variables win."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key.strip(), value)


class QuotaExceeded(RuntimeError):
    """A service told us to come back hours later (daily quota). Callers stop the stage cleanly."""


class _SimpleResponse:
    """Minimal response object returned by the curl-binary fallback client."""

    def __init__(self, status_code: int, text: str, url: str):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.headers: dict[str, str] = {}

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self) -> Any:
        import json
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code} Error for url: {self.url}", response=None)


class _CurlBinClient:
    """Last-resort client: the system `curl` binary (its TLS fingerprint passes Cloudflare where Python's does not)."""

    def __init__(self, headers: dict | None):
        self.headers = dict(headers or {})

    def get(self, url: str, params: dict | None = None, timeout: int = 60, **_: Any) -> _SimpleResponse:
        import subprocess
        import tempfile
        from urllib.parse import urlencode
        if params:
            url = url + ("&" if "?" in url else "?") + urlencode(params)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as tmp:
            out_path = tmp.name
        cmd = ["curl", "-sL", "--compressed", "-m", str(timeout), "-o", out_path, "-w", "%{http_code}"]
        for k, v in self.headers.items():
            cmd += ["-H", f"{k}: {v}"]
        cmd.append(url)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
            code = int((proc.stdout or "0").strip()[-3:] or 0)
            with open(out_path, "rb") as fh:
                body = fh.read().decode("utf-8", errors="replace")
        finally:
            try:
                os.remove(out_path)
            except OSError:
                pass
        return _SimpleResponse(code, body, url)


class Http:
    """HTTP GET with polite rate limiting, retries, and client rotation to get past Cloudflare.

    With impersonate=True the first client is curl_cffi (Chrome TLS fingerprint), then plain requests,
    then the system curl binary. A 403/429/5xx answer moves on to the next client after a pause.
    """

    def __init__(self, headers: dict | None = None, min_interval: float = 0.0,
                 retries: int = 4, timeout: int = 60, impersonate: bool = False):
        self.clients: list[tuple[str, Any]] = []
        if impersonate:
            try:
                from curl_cffi import requests as cffi_requests
                s = cffi_requests.Session(impersonate="chrome")
                s.headers.update(headers or {})
                self.clients.append(("curl_cffi", s))
            except ImportError:
                log.debug("curl_cffi not installed - falling back to requests")
        session = requests.Session()
        if headers:
            session.headers.update(headers)
        self.clients.append(("requests", session))
        if impersonate:
            self.clients.append(("curl", _CurlBinClient(headers)))
        self.session = session
        self.min_interval = min_interval
        self.retries = max(retries, len(self.clients))
        self.timeout = timeout
        self.max_retry_sleep = 90.0          # never sleep longer than this on a Retry-After header
        self._last = 0.0
        self.calls = 0
        self._client_idx = 0

    def get(self, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", self.timeout)
        delay = 2.0
        last_exc: Exception | None = None
        for _attempt in range(self.retries):
            name, client = self.clients[self._client_idx % len(self.clients)]
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                self._last = time.time()
                self.calls += 1
                resp = client.get(url, **kwargs)
                if resp.status_code in (403, 429, 500, 502, 503, 504):
                    retry_after = resp.headers.get("Retry-After") if hasattr(resp, "headers") else None
                    sleep_for = float(retry_after) if retry_after and str(retry_after).isdigit() else delay
                    if sleep_for > self.max_retry_sleep:
                        raise QuotaExceeded(
                            f"HTTP {resp.status_code} from {url.split('?')[0]}: asked to wait {sleep_for/3600:.1f} h "
                            f"(daily quota reached). Body: {resp.text[:200]}")
                    log.warning("HTTP %s via %s from %s - switching client, retrying in %.1fs",
                                resp.status_code, name, url[:110], sleep_for)
                    self._client_idx += 1
                    time.sleep(sleep_for)
                    delay *= 1.7
                    continue
                return resp
            except QuotaExceeded:
                raise
            except Exception as exc:  # network hiccup or client failure
                last_exc = exc
                log.warning("Request error via %s: %s - retrying in %.1fs", name, str(exc)[:120], delay)
                self._client_idx += 1
                time.sleep(delay)
                delay *= 1.7
        if last_exc:
            raise last_exc
        raise RuntimeError(f"Gave up fetching {url} (blocked on all clients)")

    def get_json(self, url: str, **kwargs: Any) -> Any:
        resp = self.get(url, **kwargs)
        resp.raise_for_status()
        return resp.json()


# ----------------------------------------------------------------------------
# Text normalisation
# ----------------------------------------------------------------------------
_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_SPACE_RE = re.compile(r"\s+")


_TRANSLIT = str.maketrans({
    "ı": "i", "İ": "I", "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ß": "ss",
    "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ð": "d", "þ": "th", "ţ": "t", "Ţ": "T",
})


def strip_accents(text: str) -> str:
    """Remove diacritics, including letters NFKD cannot decompose (Turkish dotless i, ø, ł ...)."""
    text = str(text).translate(_TRANSLIT)
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def norm_text(text: str | None) -> str:
    """Lower-case ASCII, '&' -> 'and', punctuation removed, single spaces, leading 'the' dropped."""
    if not text:
        return ""
    text = strip_accents(str(text)).lower().replace("&", " and ")
    text = _PUNCT_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip()
    if text.startswith("the "):
        text = text[4:]
    return text


def title_key(text: str | None) -> str:
    """Compact key for de-duplicating titles across sources."""
    return norm_text(text).replace(" ", "")


_TAG_RE = re.compile(r"<[^>]+>")
_SEARCH_BAD_RE = re.compile(r"[?\"*~!|&()\[\]{}^\\/<>=+]")


def clean_title(text: str | None) -> str:
    """Strip HTML tags / entities and tidy whitespace (ORCID and some publishers embed markup)."""
    import html as _html
    if not text:
        return ""
    return _SPACE_RE.sub(" ", _html.unescape(_TAG_RE.sub(" ", str(text)))).strip()


def search_safe(text: str | None) -> str:
    """A title as a plain search phrase: OpenAlex returns HTTP 400 for '?' and other operators."""
    return _SPACE_RE.sub(" ", _SEARCH_BAD_RE.sub(" ", clean_title(text))).strip()


def norm_issn(value: str | None) -> str | None:
    """Return an ISSN as 8 upper-case characters without hyphen, or None."""
    if not value:
        return None
    digits = re.sub(r"[^0-9Xx]", "", str(value)).upper()
    return digits if len(digits) == 8 else None


def fmt_issn(value: str | None) -> str:
    v = norm_issn(value)
    return f"{v[:4]}-{v[4:]}" if v else ""


def last_name(full_name: str) -> str:
    """Best-effort surname: last token that is not an initial."""
    tokens = [t for t in re.split(r"[\s,]+", strip_accents(full_name)) if t and not t.endswith(".")]
    return tokens[-1].lower() if tokens else ""


def name_tokens(full_name: str) -> set[str]:
    return {t.lower() for t in re.split(r"[\s,\-]+", strip_accents(full_name)) if len(t) > 1 and not t.endswith(".")}


def name_variants(full_name: str) -> list[str]:
    """Spellings to try when searching an author database."""
    base = re.sub(r"\s+", " ", strip_accents(full_name)).strip()
    tokens = base.split(" ")
    variants = [base]
    particles = {"el", "al", "abou", "abu", "bin", "ben", "de", "da", "di", "van", "von"}
    no_particles = [t for t in tokens if t.lower() not in particles]
    if no_particles != tokens:
        variants.append(" ".join(no_particles))
    if len(tokens) >= 3:
        variants.append(f"{tokens[0]} {tokens[-1]}")
        variants.append(f"{tokens[0]} {' '.join(tokens[-2:])}")
    if "-" in base:
        variants.append(base.replace("-", " "))
    seen: set[str] = set()
    out = []
    for v in variants:
        if v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return out


# ----------------------------------------------------------------------------
# Google Scholar "publication" string -> venue name
# ----------------------------------------------------------------------------
_TRAILING_YEAR_RE = re.compile(r",?\s*(19|20)\d{2}\s*$")
_VOLUME_CUT_RE = re.compile(r"\s+\d{1,4}\s*(\([^)]*\))?\s*(,|$)")
_PAGES_CUT_RE = re.compile(r",\s*(pp?\.\s*)?[\dA-Za-z]+\s*[-–]\s*[\dA-Za-z]+\s*$")
_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")


def parse_scholar_venue(publication: str | None) -> str:
    """Turn 'Energy Economics 96, 105017, 2021' into 'Energy Economics'."""
    if not publication:
        return ""
    s = publication.strip()
    s = _TRAILING_YEAR_RE.sub("", s)
    m = _VOLUME_CUT_RE.search(s)
    if m and m.start() > 3:
        s = s[: m.start()]
    s = _PAGES_CUT_RE.sub("", s)
    s = _PAREN_RE.sub("", s)
    return s.strip(" ,;:-")


NON_JOURNAL_HINTS = (
    "arxiv", "ssrn", "working paper", "preprint", "mpra", "repec", "thesis", "dissertation",
    "proceedings", "conference", "symposium", "workshop", "book", "chapter", "handbook",
    "encyclopedia", "report", "mimeo", "ph.d", "phd", "discussion paper", "policy brief", "policy paper",
    "white paper", "united nations", "world bank", "international monetary fund", "international labour",
    "un women", "unicef", "unesco", "oecd publishing", "university press", "publishing", "publishers",
    "edward elgar", "palgrave", "routledge", "igi global", "nova science", "lambert academic",
)
# a bare publisher name as the venue means a book or a report, not a journal
PUBLISHER_NAMES = {
    "springer", "springer nature", "elsevier", "wiley", "john wiley and sons", "routledge", "palgrave macmillan",
    "emerald", "emerald group publishing", "sage", "sage publications", "taylor and francis", "mcgraw hill",
    "pearson", "cengage", "oxford university press", "cambridge university press", "harvard business press",
    "world scientific", "de gruyter", "brill", "igi global", "intechopen", "nova science publishers",
    "transparency international", "arab institute for women", "policy work", "the new humanitarian",
}


def looks_non_journal(venue: str | None) -> bool:
    v = (venue or "").lower()
    if any(h in v for h in NON_JOURNAL_HINTS):
        return True
    return norm_text(v) in PUBLISHER_NAMES


# "Journals" that are really preprint servers: a match there must never replace the real journal
PREPRINT_VENUES = {
    "ssrn electronic journal", "ssrn", "arxiv", "research square", "biorxiv", "medrxiv", "authorea preprints",
    "preprints", "osf preprints", "econstor", "repec", "iza discussion papers", "nber working paper series",
    "cesifo working paper series", "mpra paper", "zenodo", "figshare", "researchgate",
}
PREPRINT_ISSNS = {"15565068"}  # SSRN Electronic Journal


def is_preprint_venue(venue: str | None, issns: list[str] | tuple[str, ...] | None = None) -> bool:
    nv = norm_text(venue)
    if nv in PREPRINT_VENUES or nv.startswith(("ssrn", "arxiv", "working paper", "discussion paper")):
        return True
    return any(norm_issn(i) in PREPRINT_ISSNS for i in (issns or []))
