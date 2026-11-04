"""Output formatting: markdown -> Slack mrkdwn, message splitting, sources.

Pure functions; unit-tested in tests/test_formatting.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .tools.web import BRAND, DESTINATION, EDITORIAL_PRESS, PUBLICATION, OFFICIAL, OTHER, REVIEW_SIGNAL


def host_of(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host

SOURCE_TYPE_ORDER = [OFFICIAL, BRAND, PUBLICATION, DESTINATION, EDITORIAL_PRESS, REVIEW_SIGNAL, OTHER]
SOURCE_TYPE_LABELS = {
    OFFICIAL: "official",
    BRAND: "brand",
    PUBLICATION: "Our publication",
    DESTINATION: "destination",
    EDITORIAL_PRESS: "editorial / press",
    REVIEW_SIGNAL: "guest signals",
    OTHER: "other",
}

_SLACK_MAX_CHARS = 2800  # Slack rejects messages over ~3000 after formatting


@dataclass(frozen=True)
class SourceLine:
    sid: str  # "S1"
    title: str
    url: str
    source_type: str


def markdown_to_slack(text: str) -> str:
    """Convert our house markdown to Slack mrkdwn.

    - ## / ### headings -> bold lines
    - tables -> bullet lists (Slack renders tables badly)
    - **bold** -> *bold* (Slack does not use CommonMark double stars)
    - [label](url) -> <url|label>
    """
    lines = text.splitlines()
    out: list[str] = []
    in_table = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                continue  # separator row
            out.append("• " + " — ".join(c for c in cells if c))
            in_table = True
            continue
        if in_table and not stripped.startswith("|"):
            in_table = False
        heading = re.match(r"^#{1,6}\s+(.*)$", stripped)
        if heading:
            out.append(f"*{_inline_markdown_to_mrkdwn(heading.group(1).strip())}*")
            continue
        out.append(_inline_markdown_to_mrkdwn(line))
    return "\n".join(out)


def _inline_markdown_to_mrkdwn(line: str) -> str:
    """Convert inline CommonMark without touching inline-code spans."""
    parts = re.split(r"(`[^`]*`)", line)
    for index in range(0, len(parts), 2):
        part = parts[index]
        part = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", part)
        part = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"*\1*", part)
        parts[index] = part
    return "".join(parts)


def split_for_slack(text: str, *, max_chars: int = _SLACK_MAX_CHARS) -> list[str]:
    """Split a long message into Slack-sized chunks on section boundaries.

    Prefers splitting before headings, then blank lines, then anywhere.
    """
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        split_at = -1
        for pattern in ("\n## ", "\n### ", "\n**", "\n\n", "\n"):
            idx = window.rfind(pattern)
            if idx > max_chars // 4:
                split_at = idx + (0 if pattern == "\n\n" else 1 if pattern.startswith("\n#") else len(pattern))
                break
        if split_at <= 0:
            split_at = max_chars
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining.strip():
        chunks.append(remaining.strip())
    return [c for c in chunks if c.strip()]


def build_sources_block(sources: list[SourceLine]) -> str:
    """Render the Sources section, grouped by category, from verified rows only."""
    if not sources:
        return "### 7. Sources\nNo usable sources were retained for this project."
    lines = ["### 7. Sources"]
    for source_type in SOURCE_TYPE_ORDER:
        group = [s for s in sources if s.source_type == source_type]
        if not group:
            continue
        label = SOURCE_TYPE_LABELS[source_type]
        lines.append(f"\n*{label}*")
        for s in group:
            title = s.title.strip() or s.url
            lines.append(f"- [{s.sid}] <{s.url}|{title}>")
    return "\n".join(lines)


_CITATION_RE = re.compile(r"\[(S\d+)\]")


def validate_citations(text: str, valid_sids: set[str]) -> tuple[str, list[str]]:
    """Strip citations pointing at unknown sources; return (text, removed_ids)."""
    removed: list[str] = []

    def repl(match: re.Match[str]) -> str:
        sid = match.group(1)
        if sid in valid_sids:
            return match.group(0)
        removed.append(sid)
        return ""

    cleaned = _CITATION_RE.sub(repl, text)
    cleaned = re.sub(r"  +", " ", cleaned)
    return cleaned, sorted(set(removed))


def strip_citations(text: str) -> str:
    """Remove ALL [S#] markers — normal copilot mode shows no opaque ids."""
    return re.sub(r"\s*\[(S\d+)\]", "", text)


_COMPACT_TYPE_PRIORITY = [OFFICIAL, PUBLICATION, DESTINATION, EDITORIAL_PRESS, BRAND]
_COMPACT_LABELS = {
    OFFICIAL: "Official hotel",
    PUBLICATION: "Our publication",
    DESTINATION: "Destination",
    EDITORIAL_PRESS: "Editorial",
    BRAND: "Brand",
}


def build_compact_sources(sources: list[SourceLine], *, max_links: int = 6) -> str:
    """Compact, human-readable sources for the default conversational reply.

    3-6 strong links; the full set stays in the internal database.
    """
    if not sources:
        return "*Sources*\n(no usable sources retained)"
    picked: list[SourceLine] = []
    for source_type in _COMPACT_TYPE_PRIORITY:
        for s in sources:
            if s.source_type == source_type and len(picked) < max_links:
                if any(p.url == s.url for p in picked):
                    continue
                picked.append(s)
    lines = ["*Sources*"]
    for s in picked:
        label = _COMPACT_LABELS.get(s.source_type, "Source")
        title = (s.title or "").strip()
        if not title or title.startswith(("http://", "https://")):
            title = host_of(s.url) or s.url
        title = title[:70] + ("…" if len(title) > 70 else "")
        lines.append(f"• {label} — <{s.url}|{title}>")
    if len(sources) > len(picked):
        lines.append(f"(+{len(sources) - len(picked)} more stored internally)")
    return "\n".join(lines)
