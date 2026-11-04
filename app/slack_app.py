"""Slack Bolt app (Socket Mode) — the chat surface of the agent.

Design points for a two-user MVP:
- Socket Mode: no public HTTP endpoint needed.
- Allowlist of Slack user IDs; everyone else is ignored.
- All replies go into the originating thread.
- Long research runs in a worker thread so the socket stays responsive;
  one research run per thread at a time.
- Sparse progress updates + emoji reaction lifecycle.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError

from .agent import AgentCancelled, AgentError
from .config import Settings
from .formatting import markdown_to_slack, split_for_slack
from .llm import build_provider
from .orchestration.orchestrator import STEP_PROGRESS_PREFIX, EditorialOrchestrator
from .slack_dns import install_slack_socket_dns_fallback
from .storage import Database
from .tools.documents import fetch_documents, slim_file

logger = logging.getLogger(__name__)

PROGRESS_MESSAGES = {
    "start": "On it — starting research for this property.",
    "official": "Researching official property sources…",
    "coverage": "Checking existing coverage in our publication and destination context…",
    "validate": "I've found some possible stay experiences; validating them against the evidence…",
    "compare": "Comparing them against previous concepts…",
    "documents": "Reading the attached documents…",
}


def accepts_slack_event(
    event: dict[str, Any], *, mentioned: bool, db: Database,
    bot_user_id: str = "",
) -> bool:
    """Whether a non-bot message belongs to this conversation surface.

    DMs and mentions are entry points. An unmentioned channel message is
    accepted only when it is a reply in a thread with a persisted bot session.
    """
    if event.get("bot_id") or event.get("subtype"):
        return False
    if bot_user_id and event.get("user") == bot_user_id:
        return False
    if mentioned or event.get("channel_type") == "im":
        return True
    channel = event.get("channel", "")
    thread_ts = event.get("thread_ts", "")
    return bool(thread_ts and db.is_agent_thread(f"{channel}:{thread_ts}"))


def build_app(settings: Settings) -> tuple[App, EditorialOrchestrator]:
    """Wire Slack Bolt around the orchestrator. Returns (bolt_app, orchestrator)."""
    bolt_app = App(
        token=settings.slack_bot_token,
        process_before_response=False,
        # Skip the eager auth.test call at startup: bot self-messages are
        # already filtered by bot_id in the event handlers.
        token_verification_enabled=False,
    )

    provider = build_provider(settings)
    research_provider = build_provider(settings, role="research")
    db = Database(settings.database_path)
    agent = EditorialOrchestrator(
        settings, provider, db, research_provider=research_provider,
        writer_provider=build_provider(settings, role="writer"),
    )

    executor = AgentExecutor(settings, bolt_app, agent, db)
    router = _register_listeners(bolt_app, settings, agent, db, executor)
    threading.Thread(target=executor.serve, daemon=True, name="agent-executor").start()
    bolt_app._luxury_router = router  # read by main() for the startup catch-up
    bolt_app._luxury_executor = executor  # read by main() for the socket watchdog
    return bolt_app, agent


def _elapsed(started: float) -> str:
    seconds = int(time.monotonic() - started)
    return f"{seconds // 60}:{seconds % 60:02d}"


class AgentExecutor:
    """Single background worker consuming queued Slack events.

    A queue keeps one research run from stepping on another and keeps
    the Socket Mode listener responsive.
    """

    def __init__(self, settings: Settings, bolt_app: App, agent: EditorialOrchestrator, db: Database) -> None:
        import queue

        self._settings = settings
        self._app = bolt_app
        self._agent = agent
        # Image-only PDFs need a reader that can see the pages (Claude CLI only).
        reader = build_provider(settings, role="research")
        self._pdf_transcriber = getattr(reader, "transcribe_pdf", None)
        self._image_describer = getattr(reader, "describe_images", None)
        self._db = db
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._busy_threads: set[str] = set()
        self._cancel_events: dict[str, threading.Event] = {}

    def submit(self, job: dict[str, Any]) -> None:
        self._cancel_events.setdefault(job["thread_key"], threading.Event()).clear()
        activity_ts = str(job.get("message_ts") or job["thread_ts"])
        self._react(job["channel"], activity_ts, "eyes")
        job["status_ts"] = self._post(
            job["channel"], job["thread_ts"], "On it. I'll post each step here as I go.",
        )
        self._queue.put(job)

    def cancel(self, thread_key: str) -> bool:
        event = self._cancel_events.setdefault(thread_key, threading.Event())
        event.set()
        self._db.upsert_session(
            thread_key, status="cancelled", current_goal="",
            pending_question_type="", pending_question_text="",
        )
        return thread_key in self._busy_threads

    def serve(self) -> None:  # runs in daemon thread
        while True:
            job = self._queue.get()
            try:
                self._run_job(job)
            except Exception:
                logger.exception("job crashed")
            finally:
                self._queue.task_done()

    def _run_job(self, job: dict[str, Any]) -> None:
        channel = job["channel"]
        thread_ts = job["thread_ts"]
        thread_key = job["thread_key"]
        text = job["text"]
        user_id = job["user_id"]
        activity_ts = str(job.get("message_ts") or thread_ts)
        status_ts = str(job.get("status_ts") or "")
        cancel_event = self._cancel_events.setdefault(thread_key, threading.Event())

        if thread_key in self._busy_threads:
            self._post(channel, thread_ts, "I'm still working on the previous request in this thread — give me a moment.")
            return
        self._busy_threads.add(thread_key)
        started = time.monotonic()
        try:
            history = self._db.thread_history(thread_key)
            last_step = ""

            def on_progress(stage: str) -> None:
                # Each step is its own thread message, never edited or deleted, so the
                # thread reads as a progress log with elapsed time. Researcher-internal
                # stages are sub-steps of an orchestrator step and are not posted.
                nonlocal last_step
                if stage.startswith(STEP_PROGRESS_PREFIX):
                    message = stage[len(STEP_PROGRESS_PREFIX):]
                elif stage == "documents":
                    message = PROGRESS_MESSAGES["documents"]
                else:
                    return
                if message == last_step:
                    return
                last_step = message
                self._post(channel, thread_ts, f":hourglass_flowing_sand: `{_elapsed(started)}` {message}")

            self._agent.add_progress_listener(on_progress)
            documents = []
            if job.get("files"):
                on_progress("documents")
                documents = fetch_documents(
                    job["files"], self._settings.slack_bot_token, pdf_transcriber=self._pdf_transcriber,
                    image_dir=self._settings.artifacts_path / "documents",
                    image_describer=self._image_describer,
                )
            outcome = self._agent.handle_message(
                text, user_id=user_id, thread_key=thread_key, history=history,
                channel_id=channel, thread_ts=thread_ts,
                cancel_check=cancel_event.is_set, documents=documents,
            )

            if outcome.clarification:
                self._deliver(channel, thread_ts, status_ts, outcome.clarification)
                self._db.append_thread_message(thread_key, "user", text)
                self._db.append_thread_message(thread_key, "assistant", outcome.clarification)
                self._finish_activity(channel, activity_ts, "speech_balloon")
                return

            self._post(channel, thread_ts, f":white_check_mark: `{_elapsed(started)}` Done. Here's the result:")
            for part in split_for_slack(markdown_to_slack(outcome.markdown)):
                self._post(channel, thread_ts, part)
            for path in outcome.attachments:
                self._upload(channel, thread_ts, path)
            self._db.append_thread_message(thread_key, "user", text)
            self._db.append_thread_message(thread_key, "assistant", outcome.markdown[:4000])
            self._finish_activity(channel, activity_ts, "white_check_mark")
        except AgentCancelled:
            self._deliver(
                channel, thread_ts, status_ts,
                "Stopped. I won't continue this research unless you ask me to resume.",
            )
            self._finish_activity(channel, activity_ts, "no_entry_sign")
        except AgentError as exc:
            logger.warning("agent error: %s", exc)
            self._deliver(channel, thread_ts, status_ts, f"Research hit a problem: {exc}")
            self._finish_activity(channel, activity_ts, "warning")
        except Exception:
            logger.exception("unexpected failure while handling message")
            self._deliver(
                channel, thread_ts, status_ts,
                "Something went wrong on my side — please try again.",
            )
            self._finish_activity(channel, activity_ts, "warning")
        finally:
            self._agent.remove_progress_listener(on_progress)
            self._busy_threads.discard(thread_key)

    def _post(self, channel: str, thread_ts: str, text: str) -> str | None:
        try:
            response = self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=text, mrkdwn=True
            )
            return str(response.get("ts") or "") or None
        except SlackApiError as exc:
            logger.error("chat_postMessage failed: %s", exc.response["error"])
            return None

    def _upload(self, channel: str, thread_ts: str, path: str) -> None:
        try:
            self._app.client.files_upload_v2(
                channel=channel, thread_ts=thread_ts, file=path, filename=Path(path).name,
                title=Path(path).stem.replace("-", " "),
            )
        except SlackApiError as exc:
            error = exc.response.get("error", "")
            logger.error("files_upload_v2 failed: %s", error)
            hint = " (the bot needs the files:write scope)" if error == "missing_scope" else ""
            self._post(channel, thread_ts, f"I couldn't upload {Path(path).name}{hint}.")

    def _update(self, channel: str, message_ts: str, text: str) -> bool:
        try:
            self._app.client.chat_update(
                channel=channel, ts=message_ts, text=text, mrkdwn=True
            )
            return True
        except SlackApiError as exc:
            logger.error("chat_update failed: %s", exc.response["error"])
            return False

    def _deliver(
        self, channel: str, thread_ts: str, status_ts: str, text: str,
    ) -> None:
        # Always a new message: earlier status and progress lines stay in the thread.
        self._post(channel, thread_ts, text)

    def _react(self, channel: str, thread_ts: str, emoji: str) -> None:
        try:
            self._app.client.reactions_add(channel=channel, timestamp=thread_ts, name=emoji)
        except SlackApiError as exc:
            # "already_reacted" and similar are harmless
            logger.debug("reactions_add %s: %s", emoji, exc.response["error"])

    def _remove_reaction(self, channel: str, message_ts: str, emoji: str) -> None:
        try:
            self._app.client.reactions_remove(
                channel=channel, timestamp=message_ts, name=emoji
            )
        except SlackApiError as exc:
            logger.debug("reactions_remove %s: %s", emoji, exc.response["error"])

    def _finish_activity(self, channel: str, message_ts: str, emoji: str) -> None:
        self._remove_reaction(channel, message_ts, "eyes")
        self._react(channel, message_ts, emoji)


class SlackEventRouter:
    """Transport boundary for Slack events, independent of agent semantics."""

    def __init__(self, settings: Settings, db: Database, executor: AgentExecutor) -> None:
        self._settings = settings
        self._db = db
        self._executor = executor

    def route(
        self, event: dict[str, Any], body: dict[str, Any] | None = None, *,
        mentioned: bool = False, bot_user_id: str = "",
    ) -> str:
        user_id = str(event.get("user", ""))
        text = str(event.get("text") or "").strip()
        channel = str(event.get("channel", ""))
        ts = str(event.get("ts", ""))
        event_thread_ts = str(event.get("thread_ts", ""))
        thread_ts = event_thread_ts or ts
        thread_key = f"{channel}:{thread_ts}" if channel and thread_ts else ""
        subtype = str(event.get("subtype", ""))
        allowed_user = bool(user_id and user_id in self._settings.allowed_user_ids)
        active_thread = bool(thread_key and self._db.is_agent_thread(thread_key))

        def finish(routing: str, reason: str = "") -> str:
            logger.info(
                "slack_event type=%s user=%s channel=%s ts=%s thread_ts=%s "
                "subtype=%s allowed_user=%s active_thread=%s routing=%s reason=%s",
                event.get("type", ""), user_id, channel, ts, event_thread_ts,
                subtype, str(allowed_user).lower(), str(active_thread).lower(),
                routing, reason,
            )
            return routing

        if event.get("bot_id") or (bot_user_id and user_id == bot_user_id):
            return finish("ignore", "bot_message")
        files = [slim_file(f) for f in event.get("files") or [] if isinstance(f, dict)]
        if subtype and subtype != "file_share":
            return finish("ignore", f"unsupported_subtype:{subtype}")
        if not (text or files) or not channel or not ts:
            return finish("ignore", "missing_message_fields")
        if not allowed_user:
            return finish("ignore", "user_not_allowed")

        is_dm = event.get("channel_type") == "im"
        if not mentioned and not is_dm:
            if not event_thread_ts:
                return finish("ignore", "plain_top_level_message")
            if not active_thread:
                return finish("ignore", "no_active_thread")

        # channel+ts identifies the underlying Slack message across retries and
        # across app_mention/message.channels delivery paths more reliably than
        # the Events API envelope id alone.
        event_id = str((body or {}).get("event_id", ""))
        dedup_key = f"message:{channel}:{ts}" if channel and ts else event_id
        if not self._db.claim_slack_event(dedup_key):
            return finish("ignore", "duplicate_event")

        if mentioned and thread_key and not active_thread:
            self._db.upsert_session(
                thread_key, thread_ts=thread_ts, channel_id=channel,
                user_id=user_id, status="idle",
            )
            active_thread = True

        if text.lower().strip(" .!") in {
            "stop", "cancel", "stop researching", "cancel research"
        }:
            self._executor.cancel(thread_key)
            self._executor._post(
                channel, thread_ts,
                "Stopped. I won't run more research in this thread unless you ask me to resume.",
            )
            return finish("cancel_agent_session")

        self._executor.submit(
            {
                "channel": channel,
                "thread_ts": thread_ts,
                "thread_key": thread_key,
                "message_ts": ts,
                "text": text or "(shared files)",
                "user_id": user_id,
                "files": files,
            }
        )
        if mentioned:
            return finish("start_or_continue_agent_session")
        if is_dm and not event_thread_ts:
            return finish("start_dm_session")
        return finish("continue_agent_session")


def _register_listeners(
    bolt_app: App,
    settings: Settings,
    agent: EditorialOrchestrator,
    db: Database,
    executor: "AgentExecutor",
) -> None:
    router = SlackEventRouter(settings, db, executor)

    def handle_mention(
        event: dict[str, Any], body: dict[str, Any], context: dict[str, Any]
    ) -> None:
        router.route(
            event, body, mentioned=True,
            bot_user_id=str(context.get("bot_user_id", "")),
        )

    def handle_message(
        event: dict[str, Any], body: dict[str, Any], context: dict[str, Any]
    ) -> None:
        # app_mention may also arrive as a message event; event-id dedup makes
        # this safe. Plain channel replies are admitted only for active threads.
        router.route(
            event, body, mentioned=False,
            bot_user_id=str(context.get("bot_user_id", "")),
        )

    bolt_app.event("app_mention")(handle_mention)
    bolt_app.event("message")(handle_message)
    return router


def catch_up_missed_messages(
    client: Any, router: "SlackEventRouter", db: Database, *, hours: float, bot_user_id: str,
) -> int:
    """Route messages that arrived while the bot was offline.

    Looks at agent threads active in the last `hours` plus top-level messages
    in their channels. Already-handled messages are skipped by the router's
    per-message dedup, so this is safe to run on every start.
    """
    import time

    oldest = f"{time.time() - hours * 3600:.6f}"
    mention = f"<@{bot_user_id}>"
    routed = 0

    def replay(message: dict[str, Any], channel: str) -> None:
        nonlocal routed
        if message.get("bot_id") or message.get("user") == bot_user_id:
            return
        event = {**message, "type": "message", "channel": channel}
        if channel.startswith("D"):
            event["channel_type"] = "im"
        if router.route(event, {}, mentioned=mention in str(message.get("text", "")),
                        bot_user_id=bot_user_id) not in {"ignore"}:
            routed += 1

    threads = db.recent_thread_keys(hours=hours)
    for channel in sorted({key.split(":", 1)[0] for key in threads}):
        try:
            history = client.conversations_history(channel=channel, oldest=oldest, limit=100)
        except SlackApiError as exc:
            logger.warning("catch-up history failed channel=%s error=%s", channel, exc.response.get("error"))
            continue
        for message in reversed(history.get("messages", [])):
            if not message.get("thread_ts") or message.get("thread_ts") == message.get("ts"):
                replay(message, channel)
    for key in threads:
        channel, thread_ts = key.split(":", 1)
        try:
            replies = client.conversations_replies(channel=channel, ts=thread_ts, oldest=oldest, limit=200)
        except SlackApiError as exc:
            logger.warning("catch-up replies failed thread=%s error=%s", key, exc.response.get("error"))
            continue
        for message in replies.get("messages", []):
            if message.get("ts") != thread_ts:
                replay(message, channel)
    logger.info("catch-up routed=%d threads=%d hours=%s", routed, len(threads), hours)
    return routed


def acquire_single_instance_lock(lock_path: "Path") -> Any:
    """Hold an exclusive lock for the process lifetime, or return None if another bot has it.

    Two bots on one Socket Mode app split Slack's events between them, so requests
    land on whichever (possibly stale) process Slack picks.
    """
    import fcntl

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


class SocketWatchdog:
    """Exits the process when the Socket Mode connection keeps failing.

    After a network change the built-in Socket Mode client can loop on
    reconnects that break at once (BrokenPipeError every few seconds) while
    the process stays alive, so no events arrive and launchd never restarts
    it. A fresh process reconnects cleanly, so once errors pile up and no job
    is running, exit and let launchd (KeepAlive) start a new one.
    """

    def __init__(
        self, *, max_errors: int = 20, window_seconds: float = 600.0,
        busy: Any = lambda: False, clock: Any = time.monotonic, exit_fn: Any = os._exit,
    ) -> None:
        self._max_errors = max_errors
        self._window = window_seconds
        self._busy = busy
        self._clock = clock
        self._exit = exit_fn
        self._errors: list[float] = []
        self._lock = threading.Lock()

    def record_error(self, _error: Exception) -> None:
        with self._lock:
            self._errors.append(self._clock())

    def check(self) -> bool:
        """True (after calling exit_fn) when the connection is judged broken."""
        with self._lock:
            cutoff = self._clock() - self._window
            self._errors = [t for t in self._errors if t >= cutoff]
            failing = len(self._errors) >= self._max_errors
        if not failing or self._busy():
            return False
        logger.error(
            "socket mode failed %d times in %.0f s; exiting so launchd restarts the bot",
            self._max_errors, self._window,
        )
        logging.shutdown()
        self._exit(1)
        return True

    def watch(self, interval: float = 30.0) -> None:  # runs in daemon thread
        while not self.check():
            time.sleep(interval)


def main() -> None:
    """Entry point: `python -m app.slack_app`."""
    import os
    import sys

    from .config import load_settings

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = load_settings()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    lock = acquire_single_instance_lock(settings.database_path.parent / "bot.lock")
    if lock is None:
        print("Another bot instance is already running (data/bot.lock). Stop it first.", file=sys.stderr)
        sys.exit(1)

    bolt_app, _agent = build_app(settings)
    logger.info(
        "hotel-research agent connected (provider %s, model %s)", settings.llm_provider,
        (settings.claude_cli_model or "CLI default") if settings.llm_provider == "claude_cli" else settings.glm_model,
    )
    install_slack_socket_dns_fallback()
    if settings.catchup_hours > 0:
        try:
            bot_user_id = bolt_app.client.auth_test()["user_id"]
            catch_up_missed_messages(
                bolt_app.client, bolt_app._luxury_router, Database(settings.database_path),
                hours=settings.catchup_hours, bot_user_id=bot_user_id,
            )
        except Exception:
            logger.exception("startup catch-up failed; continuing with live events only")
    handler = SocketModeHandler(bolt_app, settings.slack_app_token)
    executor = bolt_app._luxury_executor
    watchdog = SocketWatchdog(busy=lambda: executor._queue.unfinished_tasks > 0)
    handler.client.on_error_listeners.append(watchdog.record_error)
    threading.Thread(target=watchdog.watch, daemon=True, name="socket-watchdog").start()
    handler.start()


if __name__ == "__main__":
    main()
