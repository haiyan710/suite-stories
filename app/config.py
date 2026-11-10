"""Application configuration, loaded from environment / .env file.

All credentials and tunables live here so the rest of the code never
reads os.environ directly.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional at import time so unit tests can run without python-dotenv
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _parse_user_allowlist(raw: str) -> frozenset[str]:
    return frozenset(uid.strip() for uid in raw.split(",") if uid.strip())


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the hotel research agent."""

    slack_bot_token: str
    slack_app_token: str
    zai_api_key: str = ""
    # "claude_cli" runs the local Claude Code CLI on the logged-in account;
    # "zai" calls GLM through Z.ai's Anthropic-compatible API.
    llm_provider: str = "zai"
    claude_cli_path: str = "claude"
    claude_cli_model: str = ""
    # Model for the researcher's extraction-style calls (search, page evidence, synthesis).
    # Editorial, critic, design and routing keep claude_cli_model.
    claude_cli_research_model: str = "claude-sonnet-5-5"
    # Sample drafts: "zai" writes with GLM (public evidence only; falls back to Claude),
    # "claude_cli" always uses Claude.
    writer_provider: str = "zai"
    # Reuse verified facts from earlier research of the same hotel when at least
    # FACT_CACHE_MIN_FACTS were verified within FACT_CACHE_DAYS (0 days = always research).
    fact_cache_days: float = 60.0
    fact_cache_min_facts: int = 20
    # Trip length (nights) for the default deliverable, a hotel recommendation with a
    # day-by-day itinerary, when a request names none. 0 = one stay experience instead.
    # load_settings defaults it to 3; 0 here keeps hand-built Settings (tests) on one experience.
    default_trip_nights: int = 0
    # House-style phrases a draft must not contain (deterministic check before the critic).
    banned_phrases: tuple[str, ...] = ()
    # Research guest reviews, insider tips and the hotel's stories before editorial.
    # load_settings turns it on; off here keeps hand-built Settings (tests) on the old path.
    experience_research: bool = False
    # Rewrite each sample draft once to remove AI-writing tells (prompt humanizer.md)
    # before QA. load_settings turns it on; off here keeps tests on the old call sequence.
    humanize_drafts: bool = False
    # Check itinerary legs (distance, drive and walk time) with OpenStreetMap services before
    # the outline and the draft. load_settings turns it on; off here keeps tests offline.
    logistics_check: bool = False
    photon_url: str = "https://photon.komoot.io/api/"
    osrm_url: str = "https://routing.openstreetmap.de"
    # On start, replay messages from the last N hours that arrived while offline (0 = off).
    catchup_hours: float = 48.0
    zai_anthropic_base_url: str = "https://api.z.ai/api/anthropic"
    glm_model: str = "GLM-5.3-Flash"
    allowed_user_ids: frozenset[str] = field(default_factory=frozenset)
    database_path: Path = _PROJECT_ROOT / "data" / "hotel_agent.db"
    publication_domain: str = ""
    fetch_timeout_seconds: float = 25.0
    max_concurrent_fetches: int = 4
    # Independent researcher LLM calls (searches, per-page extraction) run this many at once.
    max_concurrent_llm_calls: int = 4
    log_level: str = "INFO"
    artifacts_path: Path = _PROJECT_ROOT / "artifacts"

    @property
    def prompts_dir(self) -> Path:
        return Path(__file__).resolve().parent / "prompts"


def load_settings(
    env_file: Path | None = None,
    environ: dict[str, str] | None = None,
    *,
    require_slack: bool = True,
) -> Settings:
    """Build Settings from a .env file plus the process environment.

    Raises RuntimeError when a required value is missing. Set
    ``require_slack=False`` for CLI runs that don't touch Slack.
    """
    if load_dotenv is not None:
        target = env_file or (_PROJECT_ROOT / ".env")
        if target.exists():
            load_dotenv(target)

    env = environ if environ is not None else dict(os.environ)

    llm_provider = env.get("LLM_PROVIDER", "zai").strip().lower() or "zai"
    if llm_provider not in {"zai", "claude_cli"}:
        raise RuntimeError(f"LLM_PROVIDER must be 'zai' or 'claude_cli', got {llm_provider!r}")
    required = [("ZAI_API_KEY", env.get("ZAI_API_KEY", ""))] if llm_provider == "zai" else []
    if require_slack:
        required += [
            ("SLACK_BOT_TOKEN", env.get("SLACK_BOT_TOKEN", "")),
            ("SLACK_APP_TOKEN", env.get("SLACK_APP_TOKEN", "")),
        ]
    missing = [name for name, value in required if not value.strip()]
    if missing:
        raise RuntimeError(
            f"Missing required configuration: {', '.join(missing)}. "
            "Copy .env.example to .env and fill in the values."
        )

    db_path_raw = env.get("DATABASE_PATH", "data/hotel_agent.db").strip()
    db_path = Path(db_path_raw)
    if not db_path.is_absolute():
        db_path = _PROJECT_ROOT / db_path
    artifacts_path = Path(env.get("ARTIFACTS_PATH", "artifacts").strip() or "artifacts").expanduser()
    if not artifacts_path.is_absolute():
        artifacts_path = _PROJECT_ROOT / artifacts_path

    settings = Settings(
        slack_bot_token=env.get("SLACK_BOT_TOKEN", "").strip(),
        slack_app_token=env.get("SLACK_APP_TOKEN", "").strip(),
        zai_api_key=env.get("ZAI_API_KEY", "").strip(),
        llm_provider=llm_provider,
        claude_cli_path=env.get("CLAUDE_CLI_PATH", "claude").strip() or "claude",
        claude_cli_model=env.get("CLAUDE_CLI_MODEL", "").strip(),
        claude_cli_research_model=env.get("CLAUDE_CLI_RESEARCH_MODEL", "claude-sonnet-5-5").strip(),
        catchup_hours=float(env.get("CATCHUP_HOURS", "48") or 0),
        default_trip_nights=int(env.get("DEFAULT_TRIP_NIGHTS", "3") or 0),
        banned_phrases=tuple(x.strip() for x in env.get("BANNED_PHRASES", "").split("|") if x.strip()),
        experience_research=env.get("EXPERIENCE_RESEARCH", "1").strip().lower() not in {"0", "false", "no", ""},
        humanize_drafts=env.get("HUMANIZE_DRAFTS", "1").strip().lower() not in {"0", "false", "no", ""},
        logistics_check=env.get("LOGISTICS_CHECK", "1").strip().lower() not in {"0", "false", "no", ""},
        photon_url=env.get("PHOTON_URL", "https://photon.komoot.io/api/").strip() or "https://photon.komoot.io/api/",
        osrm_url=(env.get("OSRM_URL", "https://routing.openstreetmap.de").strip() or "https://routing.openstreetmap.de").rstrip("/"),
        writer_provider=env.get("WRITER_PROVIDER", "zai").strip().lower() or "zai",
        fact_cache_days=float(env.get("FACT_CACHE_DAYS", "60") or 0),
        fact_cache_min_facts=int(env.get("FACT_CACHE_MIN_FACTS", "20") or 20),
        zai_anthropic_base_url=env.get("ZAI_ANTHROPIC_BASE_URL", "https://api.z.ai/api/anthropic").strip().rstrip("/"),
        glm_model=env.get("GLM_MODEL", "GLM-5.3-Flash").strip(),
        allowed_user_ids=_parse_user_allowlist(env.get("ALLOWED_SLACK_USER_IDS", "")),
        database_path=db_path,
        publication_domain=env.get("PUBLICATION_DOMAIN", "").strip(),
        fetch_timeout_seconds=float(env.get("FETCH_TIMEOUT_SECONDS", "25")),
        max_concurrent_fetches=int(env.get("MAX_CONCURRENT_FETCHES", "4")),
        max_concurrent_llm_calls=max(1, int(env.get("MAX_CONCURRENT_LLM_CALLS", "4"))),
        log_level=env.get("LOG_LEVEL", "INFO").strip().upper(),
        artifacts_path=artifacts_path,
    )

    if not settings.allowed_user_ids:
        logger.warning(
            "ALLOWED_SLACK_USER_IDS is empty - the bot will ignore every user. "
            "Set it in .env to the two permitted Slack member IDs."
        )
    return settings
