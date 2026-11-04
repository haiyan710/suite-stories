"""Smoke test: Slack Bolt app wiring (no network, no Socket Mode)."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings
from app.orchestration.orchestrator import OrchestratorOutcome
from app.slack_app import AgentExecutor, SlackEventRouter, build_app
from app.storage import Database


class FakeExecutor:
    def __init__(self):
        self.jobs = []
        self.cancelled = []
        self.posts = []

    def submit(self, job):
        self.jobs.append(job)

    def cancel(self, thread_key):
        self.cancelled.append(thread_key)
        return False

    def _post(self, channel, thread_ts, text):
        self.posts.append((channel, thread_ts, text))


def router_fixture(tmp_path: Path):
    settings = Settings(
        slack_bot_token="x", slack_app_token="x", zai_api_key="x",
        database_path=tmp_path / "events.db",
        allowed_user_ids=frozenset({"U1"}),
    )
    db = Database(settings.database_path)
    executor = FakeExecutor()
    return SlackEventRouter(settings, db, executor), db, executor


def message_event(*, ts="100.1", thread_ts=None, user="U1", text="hello", **extra):
    event = {
        "type": "message", "channel": "C1", "channel_type": "channel",
        "user": user, "text": text, "ts": ts,
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    event.update(extra)
    return event


def test_build_app_registers_listeners_and_starts_executor(tmp_path: Path):
    settings = Settings(
        slack_bot_token="xoxb-fake",
        slack_app_token="xapp-fake",
        zai_api_key="fake-key",
        database_path=tmp_path / "smoke.db",
        allowed_user_ids=frozenset({"U1", "U2"}),
    )
    bolt_app, agent = build_app(settings)
    # listeners registered
    assert bolt_app._listeners is not None and len(bolt_app._listeners) > 0
    # agent is the pipeline the executor calls
    assert agent is not None
    assert settings.allowed_user_ids == frozenset({"U1", "U2"})


def test_a_initial_mention_invokes_agent_and_normalizes_thread(tmp_path: Path):
    router, db, executor = router_fixture(tmp_path)
    event = {**message_event(text="<@B1> Explain your role"), "type": "app_mention"}
    routing = router.route(event, {"event_id": "E1"}, mentioned=True, bot_user_id="B1")
    assert routing == "start_or_continue_agent_session"
    assert len(executor.jobs) == 1
    assert executor.jobs[0]["thread_ts"] == "100.1"
    assert executor.jobs[0]["message_ts"] == "100.1"
    assert executor.jobs[0]["thread_key"] == "C1:100.1"
    assert db.get_session("C1:100.1") is not None


def test_b_same_thread_plain_reply_invokes_agent(tmp_path: Path):
    router, db, executor = router_fixture(tmp_path)
    db.upsert_session("C1:100.1", thread_ts="100.1", channel_id="C1", status="complete")
    routing = router.route(
        message_event(ts="100.2", thread_ts="100.1", text="What tools can you use?"),
        {"event_id": "E2"}, mentioned=False, bot_user_id="B1",
    )
    assert routing == "continue_agent_session"
    assert executor.jobs[0]["thread_key"] == "C1:100.1"
    assert executor.jobs[0]["message_ts"] == "100.2"


def test_c_unrelated_channel_message_is_ignored(tmp_path: Path):
    router, _db, executor = router_fixture(tmp_path)
    routing = router.route(
        message_event(ts="200.2", thread_ts="200.1"),
        {"event_id": "E3"}, mentioned=False, bot_user_id="B1",
    )
    assert routing == "ignore"
    assert executor.jobs == []


def test_d_bots_own_message_is_ignored(tmp_path: Path):
    router, db, executor = router_fixture(tmp_path)
    db.upsert_session("C1:100.1", status="complete")
    routing = router.route(
        message_event(ts="100.3", thread_ts="100.1", user="B1", bot_id="BOT"),
        {"event_id": "E4"}, mentioned=False, bot_user_id="B1",
    )
    assert routing == "ignore"
    assert executor.jobs == []


def test_e_plain_reply_continues_awaiting_clarification(tmp_path: Path):
    router, db, executor = router_fixture(tmp_path)
    db.upsert_session(
        "C1:100.1", status="awaiting_clarification",
        pending_question_type="property_identity", pending_question_text="Which hotel?",
    )
    routing = router.route(
        message_event(ts="100.4", thread_ts="100.1", text="The Silveri Hotel Hong Kong"),
        {"event_id": "E5"}, mentioned=False, bot_user_id="B1",
    )
    assert routing == "continue_agent_session"
    assert executor.jobs[0]["text"] == "The Silveri Hotel Hong Kong"


def test_f_plain_reply_in_different_thread_is_ignored(tmp_path: Path):
    router, db, executor = router_fixture(tmp_path)
    db.upsert_session("C1:100.1", status="complete")
    routing = router.route(
        message_event(ts="300.2", thread_ts="300.1"),
        {"event_id": "E6"}, mentioned=False, bot_user_id="B1",
    )
    assert routing == "ignore"
    assert executor.jobs == []


def test_duplicate_and_unsupported_subtype_are_ignored(tmp_path: Path):
    router, db, executor = router_fixture(tmp_path)
    db.upsert_session("C1:100.1", status="complete")
    event = message_event(ts="100.5", thread_ts="100.1")
    assert router.route(event, {"event_id": "E7"}, bot_user_id="B1") == "continue_agent_session"
    assert router.route(event, {"event_id": "E8"}, bot_user_id="B1") == "ignore"
    edited = message_event(ts="100.6", thread_ts="100.1", subtype="message_changed")
    assert router.route(edited, {"event_id": "E9"}, bot_user_id="B1") == "ignore"
    assert len(executor.jobs) == 1


def test_executor_posts_progress_log_and_keeps_it_when_answering(tmp_path: Path):
    class Client:
        def __init__(self):
            self.posts = []
            self.updates = []
            self.added = []
            self.removed = []

        def chat_postMessage(self, **kwargs):
            self.posts.append(kwargs)
            return {"ts": "bot.1"}

        def chat_update(self, **kwargs):
            self.updates.append(kwargs)
            return {"ok": True}

        def reactions_add(self, **kwargs):
            self.added.append(kwargs)

        def reactions_remove(self, **kwargs):
            self.removed.append(kwargs)

    class Agent:
        def __init__(self):
            self.callbacks = []

        def add_progress_listener(self, callback):
            self.callbacks.append(callback)

        def remove_progress_listener(self, callback):
            self.callbacks.remove(callback)

        def handle_message(self, _text, **_kwargs):
            for stage in ("official", "step:Research: official sources", "step:Research: official sources",
                          "step:Editorial: building the recommendation"):
                for callback in self.callbacks:
                    callback(stage)
            return OrchestratorOutcome("A **formatted** answer.")

    class DB:
        def thread_history(self, _key):
            return []

        def append_thread_message(self, *_args):
            return None

        def upsert_session(self, *_args, **_kwargs):
            return None

    settings = Settings(
        slack_bot_token="x", slack_app_token="x", zai_api_key="x",
        database_path=tmp_path / "executor.db", allowed_user_ids=frozenset({"U1"}),
    )
    client = Client()
    executor = AgentExecutor(settings, SimpleNamespace(client=client), Agent(), DB())
    executor.submit({
        "channel": "C1", "thread_ts": "100.1", "thread_key": "C1:100.1",
        "message_ts": "100.2", "text": "question", "user_id": "U1",
    })
    job = executor._queue.get_nowait()
    executor._run_job(job)

    texts = [post["text"] for post in client.posts]
    assert texts[0].startswith("On it.")
    # One new message per step (researcher-internal stages and repeats are not posted),
    # each with elapsed time; nothing is edited, so the log survives the answer.
    assert texts[1].endswith("Research: official sources") and "`0:00`" in texts[1]
    assert texts[2].endswith("Editorial: building the recommendation")
    assert texts[3].startswith(":white_check_mark: `0:00` Done")
    assert texts[4] == "A *formatted* answer."
    assert len(texts) == 5
    assert client.updates == []
    assert client.added[0]["timestamp"] == "100.2"
    assert client.removed[-1]["timestamp"] == "100.2"
    assert client.added[-1]["name"] == "white_check_mark"


def test_second_bot_instance_cannot_take_the_lock(tmp_path: Path):
    from app.slack_app import acquire_single_instance_lock

    first = acquire_single_instance_lock(tmp_path / "bot.lock")
    assert first is not None
    assert acquire_single_instance_lock(tmp_path / "bot.lock") is None
    first.close()
    again = acquire_single_instance_lock(tmp_path / "bot.lock")
    assert again is not None
    again.close()


def test_socket_watchdog_exits_only_when_errors_pile_up_and_idle():
    from app.slack_app import SocketWatchdog

    now = [0.0]
    exits: list[int] = []
    busy = [False]
    dog = SocketWatchdog(
        max_errors=3, window_seconds=60, busy=lambda: busy[0],
        clock=lambda: now[0], exit_fn=exits.append,
    )
    dog.record_error(BrokenPipeError())
    dog.record_error(BrokenPipeError())
    assert dog.check() is False
    now[0] = 100.0  # old errors fall out of the window
    dog.record_error(BrokenPipeError())
    assert dog.check() is False
    dog.record_error(BrokenPipeError())
    dog.record_error(BrokenPipeError())
    busy[0] = True
    assert dog.check() is False and exits == []
    busy[0] = False
    assert dog.check() is True and exits == [1]
