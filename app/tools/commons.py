"""Freely licensed photos of public places from Wikimedia Commons, for the Korean guide.

Web images are not cleared for use, so the guide's destination photos come only
from Commons, and only under licences that allow commercial reuse with credit:
CC0, public domain, CC BY and CC BY-SA (unmodified, with the author, licence and
source shown next to the photo). Files with extra restrictions (personality
rights, trademarks) are skipped. No API key; downloads are cached on disk.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

API_URL = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "SuiteStoriesBot/1.0 (internal editorial tool)"
REQUEST_GAP_SECONDS = 0.5
THUMB_WIDTH = 1600
MIN_WIDTH = 1200
_FREE_LICENCE = re.compile(r"^(?:cc0|cc[- ]zero|public domain|pd\b|pdm|cc by(?:-sa)? \d)", re.I)
# Not a photo of the place itself.
_NOT_A_PHOTO = re.compile(
    r"\b(?:map|plan|logo|diagram|coat of arms|flag|drawing|engraving|painting|print|sketch|"
    r"poster|stamp|postcard|ticket|sign|panorama|360)\b",
    re.I,
)
_STOPWORDS = {"the", "of", "and", "di", "de", "del", "della", "la", "le", "il", "da", "a"}


@dataclass(frozen=True)
class CommonsPhoto:
    title: str
    page_url: str
    thumb_url: str
    width: int
    height: int
    licence: str
    licence_url: str
    artist: str
    description: str
    local_path: str = ""

    @property
    def credit(self) -> str:
        return f"{self.artist or 'Wikimedia Commons'} · {self.licence} · Wikimedia Commons"


def _plain(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def _name_tokens(place: str) -> set[str]:
    """Words of the place's own name (before the first comma, which starts the city)."""
    name = place.split(",")[0]
    return {w for w in re.findall(r"[^\W\d_]{3,}", name.lower()) if w not in _STOPWORDS}


def _series(title: str) -> str:
    return re.sub(r"[\d\W_]+", " ", title.lower()).strip()


class CommonsPhotos:
    def __init__(self, cache_dir: Path, *, timeout: float = 20.0) -> None:
        self._cache_dir = Path(cache_dir)
        self._timeout = timeout
        self._last_request = 0.0

    def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        wait = REQUEST_GAP_SECONDS - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            response = httpx.get(
                url, params=params, headers={"User-Agent": USER_AGENT}, timeout=self._timeout,
                follow_redirects=True,
            )
            response.raise_for_status()
            return response
        finally:
            self._last_request = time.monotonic()

    def _search(self, query: str, limit: int = 30) -> list[CommonsPhoto]:
        data = self._get(API_URL, {
            "action": "query", "format": "json", "generator": "search", "gsrsearch": query,
            "gsrnamespace": 6, "gsrlimit": limit, "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata", "iiurlwidth": THUMB_WIDTH,
            "iiextmetadatafilter": "LicenseShortName|LicenseUrl|Artist|Restrictions|ImageDescription",
        }).json()
        pages = sorted((data.get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 99))
        photos = []
        for page in pages:
            info = (page.get("imageinfo") or [{}])[0]
            meta = info.get("extmetadata") or {}
            value = lambda key: str((meta.get(key) or {}).get("value") or "")  # noqa: E731
            if info.get("mime") != "image/jpeg" or value("Restrictions").strip():
                continue
            photos.append(CommonsPhoto(
                title=str(page.get("title") or ""), page_url=str(info.get("descriptionurl") or ""),
                thumb_url=str(info.get("thumburl") or info.get("url") or ""),
                width=int(info.get("width") or 0), height=int(info.get("height") or 0),
                licence=_plain(value("LicenseShortName")), licence_url=value("LicenseUrl"),
                artist=_plain(value("Artist"))[:80], description=_plain(value("ImageDescription"))[:300],
            ))
        return photos

    @staticmethod
    def usable(photo: CommonsPhoto, place: str) -> bool:
        """A free licence, a large landscape photo, and the place's name in its title or description."""
        if not _FREE_LICENCE.match(photo.licence) or photo.width < MIN_WIDTH:
            return False
        if not 1.2 <= photo.width / max(photo.height, 1) <= 2.2:
            return False
        title = photo.title.removeprefix("File:")
        if _NOT_A_PHOTO.search(title):
            return False
        tokens = _name_tokens(place)
        shown = f"{title} {photo.description}".lower()
        return not tokens or any(token in shown for token in tokens)

    def candidates(self, place: str, limit: int = 8) -> list[CommonsPhoto]:
        """Usable photos of a place ("Trevi Fountain, Rome"), best-ranked first, not downloaded.

        Quality images first, then a plain search. Commons search is loose (a "Pantheon,
        Rome" search also returns Paris), so the caller has a model pick among them.
        """
        name = place.split(",")[0].strip()
        found: list[CommonsPhoto] = []
        for query in (f'"{name}" incategory:Quality_images', place):
            try:
                results = self._search(query)
            except Exception:
                logger.warning("Commons search failed for %r", query, exc_info=True)
                break
            for photo in results:
                # One file per numbered series, so four frescoes from one set do not
                # crowd out the garden.
                if self.usable(photo, place) and _series(photo.title) not in {_series(p.title) for p in found}:
                    found.append(photo)
            if len(found) >= limit:
                break
        return found[:limit]

    def download(self, photo: CommonsPhoto) -> CommonsPhoto | None:
        local = self._download(photo)
        return CommonsPhoto(**{**photo.__dict__, "local_path": local}) if local else None

    def _download(self, photo: CommonsPhoto) -> str:
        target = self._cache_dir / (hashlib.sha1(photo.title.encode()).hexdigest()[:16] + ".jpg")
        if target.exists() and target.stat().st_size:
            return str(target)
        try:
            data = self._get(photo.thumb_url).content
        except Exception:
            logger.warning("Commons download failed for %s", photo.title, exc_info=True)
            return ""
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return str(target)
