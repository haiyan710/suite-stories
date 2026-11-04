"""Page fetching and parsing: read_page / extract_page_evidence primitives.

Plain HTTP with graceful degradation. Sites that require JS rendering
or block bots are reported as such rather than worked around - the
agent then falls back to search-derived evidence at lower confidence.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

_TRACKING_PARAM_PREFIXES = ("utm_", "fb_", "gclid", "msclkid", "mc_", "igshid", "ref_", "spm")
_TRACKING_PARAMS_EXACT = {"ref", "source", "session", "sid"}


@dataclass(frozen=True)
class FetchedPage:
    url: str
    status: int
    title: str
    text: str
    meta_description: str
    headings: tuple[str, ...]
    images: tuple[dict[str, str], ...]
    links: tuple[str, ...]
    fetched_ok: bool
    note: str = ""

    @property
    def is_renderable_content(self) -> bool:
        return self.fetched_ok and len(self.text) > 400


def normalize_url(url: str) -> str:
    """Canonicalize a URL for deduplication and storage.

    Lowercases scheme/host, drops fragments and tracking params, strips
    default ports and trailing slashes (except on the root path).
    """
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    elif "://" not in url:
        url = "http://" + url
    parts = urlsplit(url)
    scheme = parts.scheme.lower() or "http"
    host = parts.netloc.lower()
    if ":" in host:
        host_raw, port = host.rsplit(":", 1)
        if port.isdigit() and ((scheme == "http" and port == "80") or (scheme == "https" and port == "443")):
            host = host_raw
    path = re.sub(r"/{2,}", "/", parts.path)
    if len(path) > 1:
        path = path.rstrip("/")
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS_EXACT
        and not any(k.lower().startswith(p) for p in _TRACKING_PARAM_PREFIXES)
    ]
    kept.sort()
    return urlunsplit((scheme, host, path, urlencode(kept), ""))


def host_of(url: str) -> str:
    raw = (url or "").strip()
    if raw and "://" not in raw:
        raw = "http://" + raw
    host = urlsplit(raw).netloc.lower()
    return host[4:] if host.startswith("www.") else host


class PageFetcher:
    """Fetches and parses pages into FetchedPage records."""

    def __init__(self, *, timeout_seconds: float = 25.0, max_bytes: int = 3_000_000) -> None:
        self._client = httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        self._max_bytes = max_bytes

    def close(self) -> None:
        self._client.close()

    def fetch(self, url: str) -> FetchedPage:
        try:
            response = self._client.get(url)
        except httpx.HTTPError as exc:
            logger.info("fetch failed %s: %s", url, exc.__class__.__name__)
            return FetchedPage(
                url=url, status=0, title="", text="", meta_description="",
                headings=(), images=(), links=(), fetched_ok=False,
                note=f"fetch error: {exc.__class__.__name__}",
            )
        content_type = response.headers.get("content-type", "")
        type_ok = "html" in content_type or "xml" in content_type
        if response.status_code != 200 or not type_ok:
            note = f"HTTP {response.status_code}" if response.status_code != 200 else f"non-html ({content_type.split(';')[0]})"
            logger.info("fetch unusable %s: %s", url, note)
            return FetchedPage(
                url=str(response.url), status=response.status_code, title="", text="",
                meta_description="", headings=(), images=(), links=(), fetched_ok=False, note=note,
            )
        html = response.text[: self._max_bytes]
        return parse_html(str(response.url), html)


def parse_html(url: str, html: str) -> FetchedPage:
    """Parse HTML into title / text / headings / images / links."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()

    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    meta_desc = ""
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if meta and meta.get("content"):
        meta_desc = meta["content"].strip()

    headings = tuple(
        h.get_text(" ", strip=True)
        for h in soup.find_all(["h1", "h2", "h3"])
        if h.get_text(" ", strip=True)
    )[:60]

    images: list[dict[str, str]] = []
    seen_src: set[str] = set()
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src or src.startswith("data:"):
            continue
        abs_url = _absolute(url, src)
        if abs_url in seen_src:
            continue
        seen_src.add(abs_url)
        images.append({
            "src": abs_url,
            "alt": (img.get("alt") or "").strip(),
        })
        if len(images) >= 120:
            break

    links = tuple(
        _absolute(url, a.get("href") or "")
        for a in soup.find_all("a", href=True)
    )[:400]

    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)

    normalized_title = re.sub(r"\s+", "", title.lower())
    path = urlsplit(url).path.lower()
    soft_404 = (
        "/404" in path
        or any(marker in normalized_title for marker in (
            "404", "notfound", "pagenotfound", "ページが見つかりません",
        ))
    )

    return FetchedPage(
        url=url, status=200, title=title, text=text, meta_description=meta_desc,
        headings=headings, images=tuple(images), links=links, fetched_ok=not soft_404,
        note="soft 404" if soft_404 else "",
    )


def _absolute(base: str, href: str) -> str:
    if href.startswith(("http://", "https://")):
        return href
    try:
        from urllib.parse import urljoin

        return urljoin(base, href)
    except Exception:
        return href


def condense_page_text(page: FetchedPage, *, max_chars: int = 24000) -> str:
    """Build a compact, evidence-friendly rendering of a page.

    Prioritizes title/meta/headings, then body text; hard-truncates at
    sentence boundaries where possible.
    """
    parts: list[str] = []
    if page.title:
        parts.append(f"PAGE TITLE: {page.title}")
    if page.meta_description:
        parts.append(f"META DESCRIPTION: {page.meta_description}")
    if page.headings:
        parts.append("HEADINGS: " + " | ".join(page.headings[:40]))
    parts.append("BODY TEXT:")
    body = page.text
    if len(body) > max_chars:
        body = body[:max_chars]
        # cut at the last full sentence to avoid mid-word ends
        cut = max(body.rfind(". "), body.rfind("。"), body.rfind("\n"))
        if cut > max_chars // 2:
            body = body[: cut + 1]
        body += "\n[page text truncated]"
    parts.append(body)
    return "\n".join(parts)
