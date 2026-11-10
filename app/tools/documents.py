"""Slack file attachments -> plain-text documents the researcher can cite.

Files are downloaded with the bot token (needs the `files:read` scope) and
reduced to text: HTML through the same parser as web pages, PDFs through
pypdf, plain text as-is. Anything else is reported as skipped rather than
guessed at.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Sequence

import httpx

from .pages import parse_html

logger = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 40_000_000
# Hotel photo files (press kits often send full-resolution TIFFs).
MAX_PHOTO_BYTES = 200_000_000
PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp")
MAX_TEXT_CHARS = 120_000
# Below this many extracted characters per page a PDF is treated as image-only.
MIN_PDF_CHARS_PER_PAGE = 200

PdfTranscriber = Callable[..., str]  # (path, *, page_count) -> text
# Photos smaller than this on their shorter side are icons, logos or thumbnails.
MIN_IMAGE_SIDE = 480
MAX_IMAGES_PER_DOCUMENT = 80
IMAGE_CAPTION_BATCH = 8
ImageDescriber = Callable[[Sequence[Path]], list[str]]  # paths -> one line each
# Layout copies are downscaled so a rendered PDF stays small enough for Slack.
MAX_IMAGE_EDGE = 1800


@dataclass(frozen=True)
class DocumentImage:
    """A photo extracted from a shared document, stored on disk for layouts."""

    path: str
    page: int
    width: int
    height: int
    caption: str = ""

    @property
    def orientation(self) -> str:
        return "landscape" if self.width >= self.height else "portrait"


@dataclass(frozen=True)
class Document:
    """One attachment's extracted text, or the reason it has none."""

    file_id: str
    name: str
    url: str  # Slack permalink, used as the source URL
    text: str = ""
    error: str = ""
    images: tuple[DocumentImage, ...] = field(default_factory=tuple)
    kind: str = "document"  # document | photo (a shared image file, no text)

    @property
    def ok(self) -> bool:
        return bool(self.text.strip()) and not self.error

    @property
    def usable(self) -> bool:
        """Text to read, or a photo to add to the layout catalogue."""
        return self.ok or (self.kind == "photo" and bool(self.images) and not self.error)


def slim_file(raw: dict[str, Any]) -> dict[str, Any]:
    """The fields of a Slack file object the bot needs, safe to queue."""
    return {
        "id": str(raw.get("id", "")),
        "name": str(raw.get("name") or raw.get("title") or raw.get("id", "")),
        "mimetype": str(raw.get("mimetype", "")),
        "size": int(raw.get("size") or 0),
        "url_private_download": str(raw.get("url_private_download") or raw.get("url_private") or ""),
        "permalink": str(raw.get("permalink", "")),
    }


def fetch_documents(
    files: Sequence[dict[str, Any]], bot_token: str, *, pdf_transcriber: PdfTranscriber | None = None,
    image_dir: Path | None = None, image_describer: ImageDescriber | None = None,
) -> list[Document]:
    with httpx.Client(
        headers={"Authorization": f"Bearer {bot_token}"}, follow_redirects=True, timeout=60.0,
    ) as client:
        documents = [_fetch_one(client, f, pdf_transcriber, image_dir) for f in files]
    if image_describer:
        documents = [
            replace(doc, images=caption_images(doc.images, image_describer)) if doc.images else doc
            for doc in documents
        ]
    return documents


def caption_images(images: Sequence[DocumentImage], describer: ImageDescriber) -> tuple[DocumentImage, ...]:
    """Per-image captions (a page-level description is shared by every photo in a collage)."""
    from concurrent.futures import ThreadPoolExecutor

    batches = [list(images[i:i + IMAGE_CAPTION_BATCH]) for i in range(0, len(images), IMAGE_CAPTION_BATCH)]

    def describe(batch: list[DocumentImage]) -> list[str]:
        try:
            return describer([Path(image.path) for image in batch])
        except Exception as exc:
            logger.warning("image captioning failed: %s", exc)
            return [""] * len(batch)

    with ThreadPoolExecutor(max_workers=4) as pool:
        lines = [line for batch_lines in pool.map(describe, batches) for line in batch_lines]
    return tuple(
        replace(image, caption=line.strip() or image.caption) for image, line in zip(images, lines)
    )


def _fetch_one(
    client: httpx.Client, file: dict[str, Any], pdf_transcriber: PdfTranscriber | None,
    image_dir: Path | None = None,
) -> Document:
    name = file.get("name") or file.get("id", "file")
    url = file.get("permalink") or file.get("url_private_download", "")
    base = {"file_id": file.get("id", ""), "name": name, "url": url}
    download = file.get("url_private_download", "")
    if not download:
        return Document(**base, error="Slack did not provide a download link (the bot needs the files:read scope)")
    photo = _is_photo(name, file.get("mimetype", ""))
    limit = MAX_PHOTO_BYTES if photo else MAX_DOWNLOAD_BYTES
    if int(file.get("size") or 0) > limit:
        return Document(**base, error=f"file is larger than {limit // 1_000_000} MB")
    try:
        response = client.get(download)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return Document(**base, error=f"download failed ({exc.__class__.__name__})")
    content_type = response.headers.get("content-type", "")
    # Without files:read Slack answers the download URL with its HTML sign-in page.
    if "text/html" in content_type and not _looks_like_html_name(name):
        return Document(**base, error="Slack refused the download (the bot needs the files:read scope)")
    if photo:
        return _save_photo(base, response.content, image_dir, str(file.get("id") or "file"), name)
    try:
        text = extract_text(name, file.get("mimetype", ""), response.content)
        if pdf_transcriber and _is_pdf(name, file.get("mimetype", ""), response.content):
            text = _transcribe_if_image_only(response.content, text, pdf_transcriber) or text
    except Exception as exc:  # malformed PDFs and similar
        logger.warning("attachment extraction failed name=%s error=%s", name, exc)
        return Document(**base, error=f"could not read the file ({exc.__class__.__name__})")
    if not text.strip():
        return Document(**base, error="no readable text (likely scanned or image-only)")
    images: tuple[DocumentImage, ...] = ()
    if image_dir is not None and _is_pdf(name, file.get("mimetype", ""), response.content):
        try:
            images = extract_pdf_images(
                response.content, image_dir / (file.get("id") or "file"), captions=page_visuals(text),
            )
        except Exception as exc:
            logger.warning("image extraction failed name=%s error=%s", name, exc)
    return Document(**base, text=text[:MAX_TEXT_CHARS], images=images)


def _is_photo(name: str, mimetype: str) -> bool:
    return mimetype.startswith("image/") or name.lower().endswith(PHOTO_EXTENSIONS)


def photo_caption_from_name(name: str) -> str:
    """'GardenRestaurantSalotto1.jpg' -> 'Garden Restaurant Salotto' (a first caption before the vision one)."""
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", name)
    stem = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", stem)
    stem = re.sub(r"[_\-]+|\s+\d+\s*$|\d+$", " ", stem)
    return " ".join(stem.split())


def _save_photo(base: dict[str, Any], data: bytes, image_dir: Path | None, file_id: str, name: str) -> Document:
    """A shared photo file, downscaled into the documents folder for layouts."""
    from PIL import Image, UnidentifiedImageError

    if image_dir is None:
        return Document(**base, kind="photo", error="photos are not stored in this setup")
    try:
        with Image.open(io.BytesIO(data)) as picture:
            picture = picture.convert("RGB")
            if min(picture.size) < MIN_IMAGE_SIDE:
                return Document(**base, kind="photo", error="the photo is too small for a layout")
            picture.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
            out_dir = image_dir / file_id
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / "photo.jpg"
            picture.save(path, "JPEG", quality=86)
            image = DocumentImage(
                path=str(path), page=0, width=picture.width, height=picture.height,
                caption=photo_caption_from_name(name),
            )
    except (UnidentifiedImageError, OSError) as exc:
        return Document(**base, kind="photo", error=f"could not read the image ({exc.__class__.__name__})")
    return Document(**base, kind="photo", images=(image,))


def page_text(text: str, page: int) -> str:
    """The transcription of one '[page N]' block, without its 'Visual:' line."""
    match = re.search(rf"\[page {page}\](.*?)(?=\[page \d+\]|\Z)", text, re.S)
    if not match:
        return ""
    body = re.sub(r"Visual:.*", "", match.group(1))
    return " ".join(body.split())


def page_visuals(text: str) -> dict[int, str]:
    """'Visual:' lines per page from a transcription, used as image captions."""
    visuals: dict[int, str] = {}
    for match in re.finditer(r"\[page (\d+)\](.*?)(?=\[page \d+\]|\Z)", text, re.S):
        line = re.search(r"Visual:\s*(.+)", match.group(2))
        if line:
            visuals[int(match.group(1))] = line.group(1).strip()[:400]
    return visuals


def extract_pdf_images(
    data: bytes, out_dir: Path, *, captions: dict[int, str] | None = None,
) -> tuple[DocumentImage, ...]:
    """Save the photos embedded in a PDF (deduplicated, downscaled) and describe them."""
    from pypdf import PdfReader

    # pypdf warns about every odd mask in slide decks; those images are skipped anyway.
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    out_dir.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(io.BytesIO(data))
    seen: set[str] = set()
    images: list[DocumentImage] = []
    for page_number, page in enumerate(reader.pages, 1):
        try:
            page_images = list(page.images)
        except Exception as exc:  # odd masks and colour spaces
            logger.debug("skipping images on page %d: %s", page_number, exc)
            continue
        for index, embedded in enumerate(page_images):
            picture = embedded.image
            if picture is None or min(picture.size) < MIN_IMAGE_SIDE:
                continue
            digest = hashlib.sha1(picture.tobytes()[:200_000]).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            picture = picture.convert("RGB")
            picture.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
            path = out_dir / f"p{page_number:02d}_{index}.jpg"
            picture.save(path, "JPEG", quality=84)
            images.append(DocumentImage(
                path=str(path), page=page_number, width=picture.width, height=picture.height,
                caption=(captions or {}).get(page_number, ""),
            ))
            if len(images) >= MAX_IMAGES_PER_DOCUMENT:
                return tuple(images)
    return tuple(images)


def extract_text(name: str, mimetype: str, data: bytes) -> str:
    lowered = name.lower()
    if _is_pdf(lowered, mimetype, data):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
        return "\n\n".join(f"[page {i}]\n{text}" for i, text in enumerate(pages, 1) if text)
    decoded = data.decode("utf-8", errors="replace")
    if _looks_like_html_name(lowered) or "html" in mimetype or decoded.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        return parse_html("about:blank", decoded).text
    if mimetype.startswith("text/") or lowered.endswith((".txt", ".md", ".csv")):
        return decoded
    raise ValueError(f"unsupported file type {mimetype or lowered.rsplit('.', 1)[-1]}")


def _is_pdf(name: str, mimetype: str, data: bytes) -> bool:
    return name.lower().endswith(".pdf") or mimetype == "application/pdf" or data[:5] == b"%PDF-"


def _transcribe_if_image_only(data: bytes, text: str, transcriber: PdfTranscriber) -> str:
    """Slide decks exported as images yield almost no text; let a vision reader transcribe them."""
    from pypdf import PdfReader

    page_count = len(PdfReader(io.BytesIO(data)).pages)
    if page_count == 0 or len(text) >= MIN_PDF_CHARS_PER_PAGE * page_count:
        return ""
    with tempfile.TemporaryDirectory(prefix="suite-stories-doc-") as tmp:
        path = Path(tmp) / "document.pdf"
        path.write_bytes(data)
        logger.info("transcribing image-only PDF pages=%d extracted_chars=%d", page_count, len(text))
        return transcriber(path, page_count=page_count)


def _looks_like_html_name(name: str) -> bool:
    return name.lower().endswith((".html", ".htm"))
