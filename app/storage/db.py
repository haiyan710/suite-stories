"""SQLite access layer: connection management, migrations, CRUD.

One ``Database`` instance is shared across the app; SQLite connections
are used from the single worker thread pool, guarded by a lock (SQLite
in serialized process mode handles this fine at our scale).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from .schema import DEFAULT_STYLE_RULES, SCHEMA_SQL, SCHEMA_VERSION

logger = logging.getLogger(__name__)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


@dataclass(frozen=True)
class ConceptRecord:
    id: int
    hotel: str
    city: str
    country: str
    brand: str
    created_at: str
    name: str
    proposition: str
    central_tension: str
    territory: str
    visual_language: str
    story_structure: str
    keywords: tuple[str, ...]
    ranking: str
    status: str
    experience: str
    narrative_device: str
    visual_device: str
    source_project_id: int | None

    def as_summary_line(self) -> str:
        kw = ", ".join(self.keywords[:6])
        bits = [f"- {self.name} ({self.hotel}, {self.created_at[:10]}, status: {self.status})"]
        if self.proposition:
            bits.append(f"  proposition: {self.proposition}")
        if self.central_tension:
            bits.append(f"  tension: {self.central_tension}")
        if self.territory:
            bits.append(f"  territory: {self.territory}")
        if self.story_structure:
            bits.append(f"  story flow: {self.story_structure}")
        if self.narrative_device:
            bits.append(f"  narrative device: {self.narrative_device}")
        if self.visual_device:
            bits.append(f"  visual device: {self.visual_device}")
        if kw:
            bits.append(f"  keywords: {kw}")
        return "\n".join(bits)


@dataclass(frozen=True)
class SessionRecord:
    thread_key: str
    thread_ts: str
    channel_id: str
    user_id: str
    status: str
    current_property: str
    original_request: str
    current_goal: str
    pending_question_type: str
    pending_question_text: str
    conversation_summary: str
    research_project_id: int | None
    latest_concept_ids: tuple[int, ...]
    created_at: str
    updated_at: str


class Database:
    """Thin, typed convenience wrapper over sqlite3."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------

    def _migrate(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA_SQL)
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                self._seed_style_rules()
            else:
                current = int(row["value"])
                if current < 2:
                    existing = {
                        r["name"] for r in self._conn.execute("PRAGMA table_info(concepts)").fetchall()
                    }
                    for name, definition in (
                        ("experience", "TEXT DEFAULT ''"),
                        ("narrative_device", "TEXT DEFAULT ''"),
                        ("visual_device", "TEXT DEFAULT ''"),
                        ("source_project_id", "INTEGER"),
                    ):
                        if name not in existing:
                            self._conn.execute(f"ALTER TABLE concepts ADD COLUMN {name} {definition}")
                    self._conn.execute(
                        "UPDATE concepts SET status='generated' WHERE status='proposed' OR status='' OR status IS NULL"
                    )
                if current < 3:
                    project_columns = {
                        r["name"] for r in self._conn.execute("PRAGMA table_info(projects)").fetchall()
                    }
                    for name, definition in (
                        ("hotel_slug", "TEXT DEFAULT ''"),
                        ("research_state", "TEXT DEFAULT '{}'"),
                        ("concept_state", "TEXT DEFAULT '{}'"),
                        ("design_state", "TEXT DEFAULT '{}'"),
                        ("artifact_state", "TEXT DEFAULT '{}'"),
                        ("updated_at", "TEXT DEFAULT ''"),
                    ):
                        if name not in project_columns:
                            self._conn.execute(f"ALTER TABLE projects ADD COLUMN {name} {definition}")
                    self._conn.execute(
                        "UPDATE projects SET updated_at=COALESCE(NULLIF(updated_at,''), created_at)"
                    )
                if current < 5:
                    self._backfill_fact_cache = True
                if current < 7:
                    self._seed_style_rules()
                if current < 4:
                    project_columns = {
                        r["name"] for r in self._conn.execute("PRAGMA table_info(projects)").fetchall()
                    }
                    if "orchestration_state" not in project_columns:
                        self._conn.execute(
                            "ALTER TABLE projects ADD COLUMN orchestration_state TEXT DEFAULT '{}'"
                        )
                self._conn.execute(
                    "UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),)
                )
        logger.debug("database ready at %s (schema v%d)", self._path, SCHEMA_VERSION)
        if getattr(self, "_backfill_fact_cache", False):
            self._backfill_fact_cache = False
            self.rebuild_fact_cache()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # projects
    # ------------------------------------------------------------------

    def create_project(
        self,
        *,
        hotel: str,
        request: str,
        user_id: str = "",
        slack_channel: str = "",
        slack_thread_ts: str = "",
        city: str = "",
        country: str = "",
        brand: str = "",
        hotel_slug: str = "",
    ) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO projects (hotel, hotel_slug, request, user_id, slack_channel,
                                         slack_thread_ts, city, country, brand, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')""",
                (hotel, hotel_slug, request, user_id, slack_channel, slack_thread_ts, city, country, brand),
            )
            return int(cur.lastrowid)

    def complete_project(
        self,
        project_id: int,
        *,
        status: str = "completed",
        city: str | None = None,
        country: str | None = None,
        brand: str | None = None,
        research_snapshot: str | None = None,
        property_dna: str | None = None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """UPDATE projects
                   SET status = ?, completed_at = datetime('now'), updated_at = datetime('now'),
                       city = COALESCE(?, city),
                       country = COALESCE(?, country),
                       brand = COALESCE(?, brand),
                       research_snapshot = COALESCE(?, research_snapshot),
                       property_dna = COALESCE(?, property_dna)
                   WHERE id = ?""",
                (status, city, country, brand, research_snapshot, property_dna, project_id),
            )

    def get_project(self, project_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM projects WHERE id=?", (project_id,)
            ).fetchone()

    def update_project_state(
        self, project_id: int, section: str, state: dict[str, Any], *,
        status: str | None = None,
    ) -> None:
        column = {
            "research": "research_state", "concept": "concept_state",
            "design": "design_state", "artifact": "artifact_state",
            "orchestration": "orchestration_state",
        }.get(section)
        if column is None:
            raise ValueError(f"unknown project state section: {section}")
        with self.transaction() as conn:
            if status is None:
                conn.execute(
                    f"UPDATE projects SET {column}=?, updated_at=datetime('now') WHERE id=?",
                    (json.dumps(state, ensure_ascii=False), project_id),
                )
            else:
                conn.execute(
                    f"UPDATE projects SET {column}=?, status=?, updated_at=datetime('now') WHERE id=?",
                    (json.dumps(state, ensure_ascii=False), status, project_id),
                )
        if section == "research":
            self.cache_research_facts(project_id, state)

    # ------------------------------------------------------------------
    # verified-fact cache (per hotel, across projects)
    # ------------------------------------------------------------------

    CACHEABLE_CONFIDENCE = frozenset({
        "verified_official", "verified_authoritative_secondary", "corroborated_secondary",
    })

    def hotel_key(self, hotel: str) -> str:
        return " ".join(self.resolve_alias(hotel).lower().split())

    def cache_research_facts(
        self, project_id: int, research: dict[str, Any], *, retrieved_at: str | None = None,
    ) -> int:
        """Store a project's editorial-grade facts in the per-hotel cache. Returns rows written.

        Facts that were themselves reused from the cache (marked `cached_from`) are not
        re-dated: only a fresh extraction from the source counts as re-verification.
        """
        project = self.get_project(project_id)
        if project is None:
            return 0
        key = self.hotel_key(str(project["hotel"]))
        sources = {
            str(record.get("sid")): record for record in research.get("source_records") or []
            if isinstance(record, dict) and record.get("url")
        }
        rows = []
        for fact in research.get("verified_facts") or []:
            if not isinstance(fact, dict) or fact.get("cached_from"):
                continue
            text = str(fact.get("fact") or "").strip()
            if not text or str(fact.get("confidence", "")).lower() not in self.CACHEABLE_CONFIDENCE:
                continue
            source = next((sources[str(sid)] for sid in fact.get("source_ids") or [] if str(sid) in sources), None)
            if source is None:
                continue
            rows.append((
                key, text, " ".join(text.lower().split()), str(fact.get("dimension") or ""),
                str(fact.get("confidence")).lower(), str(fact.get("evidence_level") or ""),
                str(source.get("url")), str(source.get("title") or ""), str(source.get("source_type") or "other"),
                int(bool(source.get("private"))), project_id,
                # SQLite datetime format, so freshness compares with datetime('now', ...).
                retrieved_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            ))
        if not rows:
            return 0
        with self.transaction() as conn:
            conn.executemany(
                """INSERT INTO hotel_facts (hotel_key, fact, fact_key, dimension, confidence, evidence_level,
                       source_url, source_title, source_type, private, project_id, retrieved_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(hotel_key, fact_key) DO UPDATE SET
                       confidence=excluded.confidence, source_url=excluded.source_url,
                       source_title=excluded.source_title, source_type=excluded.source_type,
                       private=MAX(hotel_facts.private, excluded.private), project_id=excluded.project_id,
                       retrieved_at=MAX(hotel_facts.retrieved_at, excluded.retrieved_at)""",
                rows,
            )
        return len(rows)

    def _seed_style_rules(self) -> None:
        """Default house style, once (caller holds the lock)."""
        if self._conn.execute("SELECT COUNT(*) FROM style_rules").fetchone()[0] == 0:
            self._conn.executemany(
                "INSERT INTO style_rules (rule, kind, created_by) VALUES (?, ?, 'default')",
                [(rule, kind) for kind, rule in DEFAULT_STYLE_RULES],
            )

    def style_rules(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM style_rules WHERE active=1 ORDER BY kind DESC, id"
            ).fetchall()

    def add_style_rule(self, rule: str, *, kind: str = "rule", created_by: str = "") -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO style_rules (rule, kind, created_by) VALUES (?, ?, ?)",
                (rule.strip(), kind, created_by),
            )
            return int(cur.lastrowid)

    def remove_style_rule(self, rule_id: int) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("UPDATE style_rules SET active=0 WHERE id=? AND active=1", (rule_id,))
            return cur.rowcount > 0

    def set_contact(self, hotel: str, role: str, name: str) -> None:
        key = self.hotel_key(hotel) if hotel else ""  # takes the lock itself
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO contacts (hotel_key, role, name) VALUES (?, ?, ?) "
                "ON CONFLICT(hotel_key, role) DO UPDATE SET name=excluded.name",
                (key, role, name.strip()),
            )

    def contacts(self, hotel: str) -> dict[str, str]:
        """{'hotel': ..., 'editor': ...} for a hotel; the editor falls back to the default one."""
        key = self.hotel_key(hotel) if hotel else ""
        with self._lock:
            rows = self._conn.execute(
                "SELECT hotel_key, role, name FROM contacts WHERE hotel_key IN (?, '')", (key,)
            ).fetchall()
        found: dict[str, str] = {}
        for row in sorted(rows, key=lambda r: r["hotel_key"] != ""):  # default first, hotel's own wins
            found[row["role"]] = row["name"]
        return found

    def geo_lookup(self, query: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute("SELECT * FROM geo_cache WHERE query=?", (query,)).fetchone()

    def geo_store(self, query: str, *, found: bool, name: str = "", kind: str = "",
                  lat: float | None = None, lon: float | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO geo_cache (query, found, name, kind, lat, lon) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(query) DO UPDATE SET found=excluded.found, name=excluded.name,
                   kind=excluded.kind, lat=excluded.lat, lon=excluded.lon, fetched_at=datetime('now')""",
                (query, int(found), name, kind, lat, lon),
            )

    def cached_hotel_facts(self, hotel: str, *, max_age_days: float) -> list[sqlite3.Row]:
        """Cached facts for a hotel verified within the last `max_age_days`, newest first."""
        key = self.hotel_key(hotel)  # takes the lock itself; resolve before acquiring it
        with self._lock:
            return self._conn.execute(
                """SELECT * FROM hotel_facts WHERE hotel_key=? AND retrieved_at >= datetime('now', ?)
                   ORDER BY retrieved_at DESC, id DESC""",
                (key, f"-{float(max_age_days)} days"),
            ).fetchall()

    def rebuild_fact_cache(self) -> int:
        """Fill the cache from every project's stored research (dated by the project's last update)."""
        with self._lock:
            projects = self._conn.execute(
                "SELECT id, research_state, updated_at FROM projects ORDER BY id"
            ).fetchall()
        written = 0
        for project in projects:
            try:
                research = json.loads(project["research_state"] or "{}")
            except json.JSONDecodeError:
                continue
            written += self.cache_research_facts(
                int(project["id"]), research, retrieved_at=str(project["updated_at"] or "") or None,
            )
        logger.info("fact cache rebuilt: %d facts from %d projects", written, len(projects))
        return written

    def project_state(self, project_id: int) -> dict[str, Any]:
        row = self.get_project(project_id)
        if row is None:
            return {}
        out: dict[str, Any] = {"project": dict(row)}
        for section, column in (
            ("research", "research_state"), ("concept", "concept_state"),
            ("design", "design_state"), ("artifact", "artifact_state"),
            ("orchestration", "orchestration_state"),
        ):
            try:
                out[section] = json.loads(row[column] or "{}")
            except (json.JSONDecodeError, TypeError):
                out[section] = {}
        return out

    def append_workflow_trace(
        self, *, project_id: int | None, thread_key: str, goal: str,
        step_id: str = "", specialist: str = "", brief_summary: str = "",
        result_status: str = "", evaluation: dict[str, Any] | None = None,
        next_action: str = "", response_mode: str = "",
    ) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO workflow_traces
                   (project_id, thread_key, goal, step_id, specialist, brief_summary,
                    result_status, evaluation, next_action, response_mode)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id, thread_key, goal[:1000], step_id[:100], specialist[:40],
                    brief_summary[:2000], result_status[:80],
                    json.dumps(evaluation or {}, ensure_ascii=False)[:5000],
                    next_action[:1000], response_mode[:80],
                ),
            )
            return int(cur.lastrowid)

    def workflow_trace(
        self, *, project_id: int | None = None, thread_key: str = "", limit: int = 20,
    ) -> list[sqlite3.Row]:
        with self._lock:
            if project_id is not None:
                return self._conn.execute(
                    "SELECT * FROM workflow_traces WHERE project_id=? ORDER BY id DESC LIMIT ?",
                    (project_id, limit),
                ).fetchall()
            return self._conn.execute(
                "SELECT * FROM workflow_traces WHERE thread_key=? ORDER BY id DESC LIMIT ?",
                (thread_key, limit),
            ).fetchall()

    def project_for_thread(self, thread_key: str) -> sqlite3.Row | None:
        session = self.get_session(thread_key)
        if session and session.research_project_id:
            row = self.get_project(session.research_project_id)
            if row is not None:
                return row
        return self.latest_project_for_thread(thread_key)

    # ------------------------------------------------------------------
    # sources
    # ------------------------------------------------------------------

    def upsert_source(
        self,
        *,
        project_id: int,
        url: str,
        url_normalized: str,
        title: str = "",
        source_type: str = "other",
        evidence: str = "",
        confidence: str = "medium",
        fetched_ok: bool = False,
    ) -> int | None:
        """Insert a source; returns None when the normalized URL already exists."""
        try:
            with self.transaction() as conn:
                cur = conn.execute(
                    """INSERT INTO sources (project_id, url, url_normalized, title, source_type,
                                            evidence, confidence, fetched_ok)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (project_id, url, url_normalized, title, source_type, evidence, confidence, int(fetched_ok)),
                )
                return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None

    def add_evidence_to_source(self, source_id: int, evidence: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE sources SET evidence = ? WHERE id = ?",
                (evidence, source_id),
            )

    def list_sources(self, project_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM sources WHERE project_id = ? ORDER BY id", (project_id,)
            ).fetchall()

    # ------------------------------------------------------------------
    # concepts
    # ------------------------------------------------------------------

    def insert_concept(
        self,
        *,
        project_id: int,
        hotel: str,
        name: str,
        city: str = "",
        country: str = "",
        brand: str = "",
        user_id: str = "",
        proposition: str = "",
        central_tension: str = "",
        territory: str = "",
        visual_language: str = "",
        story_structure: str = "",
        keywords: Sequence[str] = (),
        ranking: str = "",
        status: str = "generated",
        experience: str = "",
        narrative_device: str = "",
        visual_device: str = "",
        source_project_id: int | None = None,
    ) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO concepts (project_id, hotel, city, country, brand, user_id, name,
                                         proposition, central_tension, territory, visual_language,
                                         story_structure, keywords, ranking, status, experience,
                                         narrative_device, visual_device, source_project_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id, hotel, city, country, brand, user_id, name,
                    proposition, central_tension, territory, visual_language,
                    story_structure, json.dumps(list(keywords)), ranking, status,
                    experience or proposition, narrative_device or territory,
                    visual_device or visual_language,
                    source_project_id if source_project_id is not None else project_id,
                ),
            )
            return int(cur.lastrowid)

    def recent_concepts(
        self, *, exclude_project_id: int | None = None, limit: int = 40
    ) -> list[ConceptRecord]:
        # Concepts from "[test]" threads are kept for the thread but never used as memory.
        query = "SELECT * FROM concepts WHERE status != 'test'"
        params: list[Any] = []
        if exclude_project_id is not None:
            query += " AND project_id IS NOT ?"
            params.append(exclude_project_id)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            ConceptRecord(
                id=r["id"], hotel=r["hotel"], city=r["city"], country=r["country"],
                brand=r["brand"], created_at=r["created_at"], name=r["name"],
                proposition=r["proposition"], central_tension=r["central_tension"],
                territory=r["territory"], visual_language=r["visual_language"],
                story_structure=r["story_structure"],
                keywords=tuple(json.loads(r["keywords"] or "[]")),
                ranking=r["ranking"], status=r["status"],
                experience=r["experience"] or r["proposition"],
                narrative_device=r["narrative_device"] or r["territory"],
                visual_device=r["visual_device"] or r["visual_language"],
                source_project_id=r["source_project_id"] or r["project_id"],
            )
            for r in rows
        ]

    def concepts_for_hotel(self, hotel: str, *, exclude_project_id: int | None = None) -> list[ConceptRecord]:
        records = self.recent_concepts(exclude_project_id=exclude_project_id, limit=500)
        target = hotel.strip().lower()
        if not target:
            return []
        return [
            r for r in records
            if target in r.hotel.lower() or r.hotel.lower() in target
        ]

    def latest_concept_id(self, project_id: int) -> int | None:
        """The concept-memory record most recently saved for a project, if any."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM concepts WHERE project_id=? ORDER BY id DESC LIMIT 1", (project_id,)
            ).fetchone()
        return int(row[0]) if row else None

    def update_concept_status(self, concept_id: int, status: str) -> None:
        valid = {"generated", "shortlisted", "approved", "published", "rejected"}
        if status not in valid:
            raise ValueError(f"invalid concept lifecycle status: {status}")
        with self.transaction() as conn:
            conn.execute("UPDATE concepts SET status=? WHERE id=? AND status != 'test'", (status, concept_id))

    def project_is_test(self, project_id: int) -> bool:
        """Whether a project already saved a concept from a "[test]" thread."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM concepts WHERE project_id=? AND status='test' LIMIT 1", (project_id,)
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # aliases
    # ------------------------------------------------------------------

    def resolve_alias(self, name: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT canonical FROM aliases WHERE alias = ?", (name.strip().lower(),)
            ).fetchone()
        return row["canonical"] if row else name

    def add_alias(self, alias: str, canonical: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO aliases (alias, canonical) VALUES (?, ?)",
                (alias.strip().lower(), canonical),
            )

    # ------------------------------------------------------------------
    # thread history
    # ------------------------------------------------------------------

    def append_thread_message(self, thread_key: str, role: str, content: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO thread_messages (thread_key, role, content) VALUES (?, ?, ?)",
                (thread_key, role, content[:8000]),
            )
            # The session row carries a compact summary; raw turns are a small
            # rolling window rather than an unbounded transcript archive.
            conn.execute(
                """DELETE FROM thread_messages
                   WHERE thread_key=? AND id NOT IN (
                     SELECT id FROM thread_messages WHERE thread_key=?
                     ORDER BY id DESC LIMIT 20
                   )""",
                (thread_key, thread_key),
            )

    def thread_history(self, thread_key: str, *, limit: int = 8) -> list[tuple[str, str]]:
        """Most recent messages for a thread, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content FROM thread_messages WHERE thread_key = ? "
                "ORDER BY id DESC LIMIT ?",
                (thread_key, limit),
            ).fetchall()
        return [(r["role"], r["content"]) for r in reversed(rows)]

    def latest_project_for_thread(self, thread_key: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM projects WHERE slack_thread_ts = ? ORDER BY id DESC LIMIT 1",
                (thread_key,),
            ).fetchone()

    # ------------------------------------------------------------------
    # thread sessions
    # ------------------------------------------------------------------

    @staticmethod
    def _session_from_row(row: sqlite3.Row | None) -> SessionRecord | None:
        if row is None:
            return None
        return SessionRecord(
            thread_key=row["thread_key"], thread_ts=row["thread_ts"],
            channel_id=row["channel_id"], user_id=row["user_id"], status=row["status"],
            current_property=row["current_property"], original_request=row["original_request"],
            current_goal=row["current_goal"], pending_question_type=row["pending_question_type"],
            pending_question_text=row["pending_question_text"],
            conversation_summary=row["conversation_summary"],
            research_project_id=row["research_project_id"],
            latest_concept_ids=tuple(json.loads(row["latest_concept_ids"] or "[]")),
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def get_session(self, thread_key: str) -> SessionRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM thread_sessions WHERE thread_key=?", (thread_key,)
            ).fetchone()
        return self._session_from_row(row)

    def upsert_session(
        self, thread_key: str, *, thread_ts: str | None = None,
        channel_id: str | None = None, user_id: str | None = None,
        status: str | None = None, current_property: str | None = None,
        original_request: str | None = None, current_goal: str | None = None,
        pending_question_type: str | None = None, pending_question_text: str | None = None,
        conversation_summary: str | None = None,
        research_project_id: int | None = None,
        latest_concept_ids: Sequence[int] | None = None,
    ) -> SessionRecord:
        current = self.get_session(thread_key)
        values = {
            "thread_ts": thread_ts if thread_ts is not None else (current.thread_ts if current else thread_key.rsplit(":", 1)[-1]),
            "channel_id": channel_id if channel_id is not None else (current.channel_id if current else thread_key.split(":", 1)[0]),
            "user_id": user_id if user_id is not None else (current.user_id if current else ""),
            "status": status if status is not None else (current.status if current else "idle"),
            "current_property": current_property if current_property is not None else (current.current_property if current else ""),
            "original_request": original_request if original_request is not None else (current.original_request if current else ""),
            "current_goal": current_goal if current_goal is not None else (current.current_goal if current else ""),
            "pending_question_type": pending_question_type if pending_question_type is not None else (current.pending_question_type if current else ""),
            "pending_question_text": pending_question_text if pending_question_text is not None else (current.pending_question_text if current else ""),
            "conversation_summary": conversation_summary if conversation_summary is not None else (current.conversation_summary if current else ""),
            "research_project_id": research_project_id if research_project_id is not None else (current.research_project_id if current else None),
            "latest_concept_ids": json.dumps(list(latest_concept_ids)) if latest_concept_ids is not None else json.dumps(list(current.latest_concept_ids) if current else []),
        }
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO thread_sessions
                   (thread_key, thread_ts, channel_id, user_id, status, current_property,
                    original_request, current_goal, pending_question_type, pending_question_text,
                    conversation_summary, research_project_id, latest_concept_ids)
                   VALUES (:thread_key, :thread_ts, :channel_id, :user_id, :status, :current_property,
                           :original_request, :current_goal, :pending_question_type, :pending_question_text,
                           :conversation_summary, :research_project_id, :latest_concept_ids)
                   ON CONFLICT(thread_key) DO UPDATE SET
                     thread_ts=excluded.thread_ts, channel_id=excluded.channel_id,
                     user_id=excluded.user_id, status=excluded.status,
                     current_property=excluded.current_property,
                     original_request=excluded.original_request, current_goal=excluded.current_goal,
                     pending_question_type=excluded.pending_question_type,
                     pending_question_text=excluded.pending_question_text,
                     conversation_summary=excluded.conversation_summary,
                     research_project_id=excluded.research_project_id,
                     latest_concept_ids=excluded.latest_concept_ids,
                     updated_at=datetime('now')""",
                {"thread_key": thread_key, **values},
            )
        session = self.get_session(thread_key)
        assert session is not None
        return session

    def clear_pending_question(self, thread_key: str) -> None:
        self.upsert_session(
            thread_key, pending_question_type="", pending_question_text=""
        )

    def has_active_session(self, thread_key: str) -> bool:
        session = self.get_session(thread_key)
        return bool(session and session.status not in {"cancelled"})

    def has_bot_participated(self, thread_key: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM thread_messages WHERE thread_key=? AND role='assistant' LIMIT 1",
                (thread_key,),
            ).fetchone()
        return row is not None

    def recent_thread_keys(self, *, hours: float) -> list[str]:
        """Thread keys of sessions touched in the last `hours` (not cancelled)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT thread_key FROM thread_sessions WHERE status != 'cancelled' "
                "AND updated_at >= datetime('now', ?) ORDER BY updated_at",
                (f"-{float(hours)} hours",),
            ).fetchall()
        return [str(row[0]) for row in rows if ":" in str(row[0])]

    def is_agent_thread(self, thread_key: str) -> bool:
        return self.has_active_session(thread_key) or self.has_bot_participated(thread_key)

    def claim_slack_event(self, event_id: str) -> bool:
        """Atomically claim an event. False means Slack already delivered it."""
        if not event_id:
            return True
        try:
            with self.transaction() as conn:
                conn.execute("INSERT INTO slack_events(event_id) VALUES (?)", (event_id,))
            return True
        except sqlite3.IntegrityError:
            return False

    # ------------------------------------------------------------------
    # design and artifact versions
    # ------------------------------------------------------------------

    def save_design_version(
        self, project_id: int, design_spec: dict[str, Any], *,
        status: str = "draft", feedback: str = "",
    ) -> int:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM design_versions WHERE project_id=?",
                (project_id,),
            ).fetchone()
            version = int(row["v"]) + 1
            if status == "approved":
                conn.execute(
                    "UPDATE design_versions SET status='superseded' WHERE project_id=? AND status='approved'",
                    (project_id,),
                )
            conn.execute(
                """INSERT INTO design_versions(project_id, version, status, design_spec, feedback)
                   VALUES (?, ?, ?, ?, ?)""",
                (project_id, version, status, json.dumps(design_spec, ensure_ascii=False), feedback),
            )
        return version

    def design_versions(self, project_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM design_versions WHERE project_id=? ORDER BY version",
                (project_id,),
            ).fetchall()

    def latest_design(self, project_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM design_versions WHERE project_id=? ORDER BY version DESC LIMIT 1",
                (project_id,),
            ).fetchone()

    def save_artifact(
        self, *, project_id: int, path: str, artifact_type: str = "html",
        source_design_version: int = 0, status: str = "current",
        user_feedback: str = "", metadata: dict[str, Any] | None = None,
    ) -> tuple[int, int]:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM artifacts WHERE project_id=? AND type=?",
                (project_id, artifact_type),
            ).fetchone()
            version = int(row["v"]) + 1
            if status in {"current", "approved"}:
                conn.execute(
                    "UPDATE artifacts SET status='superseded' WHERE project_id=? AND type=? AND status IN ('current','approved')",
                    (project_id, artifact_type),
                )
            cur = conn.execute(
                """INSERT INTO artifacts(project_id, type, version, status, path,
                                         source_design_version, user_feedback, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id, artifact_type, version, status, path,
                    source_design_version, user_feedback,
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            return int(cur.lastrowid), version

    def list_artifacts(self, project_id: int, artifact_type: str = "html") -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM artifacts WHERE project_id=? AND type=? ORDER BY version",
                (project_id, artifact_type),
            ).fetchall()

    def get_artifact(self, project_id: int, version: int, artifact_type: str = "html") -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM artifacts WHERE project_id=? AND type=? AND version=?",
                (project_id, artifact_type, version),
            ).fetchone()

    def current_artifact(self, project_id: int, artifact_type: str = "html") -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                """SELECT * FROM artifacts WHERE project_id=? AND type=?
                   ORDER BY CASE status WHEN 'current' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,
                            version DESC LIMIT 1""",
                (project_id, artifact_type),
            ).fetchone()

    def restore_artifact(self, project_id: int, version: int, artifact_type: str = "html") -> sqlite3.Row:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE project_id=? AND type=? AND version=?",
                (project_id, artifact_type, version),
            ).fetchone()
            if row is None:
                raise ValueError(f"artifact v{version} does not exist")
            conn.execute(
                "UPDATE artifacts SET status='superseded' WHERE project_id=? AND type=? AND status='current'",
                (project_id, artifact_type),
            )
            conn.execute("UPDATE artifacts SET status='current' WHERE id=?", (row["id"],))
        restored = self.get_artifact(project_id, version, artifact_type)
        assert restored is not None
        return restored
