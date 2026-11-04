"""Safe, append-only artifact version manager with an optional renderer seam."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from ..orchestration.handoffs import ArtifactResult
from ..storage import Database


class Renderer(Protocol):
    def render(self, html_path: Path) -> Path | None:
        ...


class NoopRenderer:
    """Default MVP renderer. A browser renderer can be injected later."""

    def render(self, html_path: Path) -> Path | None:
        return None


class ArtifactManager:
    def __init__(self, root: Path, db: Database, *, renderer: Renderer | None = None) -> None:
        self._root = root
        self._db = db
        self._renderer = renderer or NoopRenderer()

    @staticmethod
    def safe_slug(value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
        return slug[:80] or "project"

    def create_html(
        self, *, project_id: int, hotel_slug: str, html: str, css: str,
        js: str, source_design_version: int, notes: list[str] | None = None,
        user_feedback: str = "",
    ) -> ArtifactResult:
        existing = self._db.list_artifacts(project_id)
        next_version = max((int(row["version"]) for row in existing), default=0) + 1
        directory = self._root / self.safe_slug(hotel_slug) / f"v{next_version:03d}"
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "index.html").write_text(html, encoding="utf-8")
        (directory / "styles.css").write_text(css, encoding="utf-8")
        files = ["index.html", "styles.css"]
        if js.strip():
            (directory / "script.js").write_text(js, encoding="utf-8")
            files.append("script.js")
        preview = self._renderer.render(directory / "index.html")
        artifact_id, db_version = self._db.save_artifact(
            project_id=project_id, path=str(directory), source_design_version=source_design_version,
            status="current", user_feedback=user_feedback,
            metadata={"files": files, "notes": notes or [], "preview_path": str(preview or "")},
        )
        if db_version != next_version:
            raise RuntimeError("artifact version allocation changed unexpectedly")
        return ArtifactResult(
            artifact_id=artifact_id, project_id=project_id, version=db_version,
            status="current", path=str(directory), files=files,
            source_design_version=source_design_version,
            preview_path=str(preview or ""), notes=notes or [],
        )

    def restore(self, project_id: int, version: int) -> ArtifactResult:
        row = self._db.restore_artifact(project_id, version)
        metadata = __import__("json").loads(row["metadata"] or "{}")
        return ArtifactResult(
            artifact_id=row["id"], project_id=project_id, version=row["version"],
            status="current", path=row["path"], files=metadata.get("files", []),
            source_design_version=row["source_design_version"],
            preview_path=metadata.get("preview_path", ""), notes=metadata.get("notes", []),
        )
