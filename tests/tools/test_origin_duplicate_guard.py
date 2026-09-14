"""WO-D-2026-09-14-019-04 D: origin duplicate-send guard.

The cron auto-delivery guard generalized to every session origin: a send_message to the
session's own (platform, chat, thread) is redundant because the final response auto-delivers
there. Origin is read via ``get_session_env`` — ContextVars in-process, os.environ fallback so
a ``hermes send`` subprocess (inheriting the gateway's exported origin) is covered too.
"""
import pytest

from gateway.session_context import reset_session_vars, set_session_vars
from tools.send_message_tool import _maybe_skip_origin_duplicate_send


@pytest.fixture(autouse=True)
def _reset_session_ctx(monkeypatch):
    reset_session_vars()
    for name in ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_CHAT_ID", "HERMES_SESSION_THREAD_ID"):
        monkeypatch.delenv(name, raising=False)
    yield
    reset_session_vars()


def test_origin_match_skips_with_note():
    set_session_vars(platform="slack", chat_id="C0BUCJ2SJGK", thread_id="1789.088")
    result = _maybe_skip_origin_duplicate_send("slack", "C0BUCJ2SJGK", "1789.088")
    assert result is not None
    assert result["skipped"] is True
    assert result["reason"] == "origin_auto_delivery_duplicate_target"
    assert result["target"] == "slack:C0BUCJ2SJGK:1789.088"
    assert "final response will already" in result["note"]


def test_different_thread_does_not_skip():
    set_session_vars(platform="slack", chat_id="C1", thread_id="T1")
    assert _maybe_skip_origin_duplicate_send("slack", "C1", "T2") is None
    assert _maybe_skip_origin_duplicate_send("slack", "C1", None) is None


def test_different_chat_or_platform_does_not_skip():
    set_session_vars(platform="slack", chat_id="C1")
    assert _maybe_skip_origin_duplicate_send("slack", "C2", None) is None
    assert _maybe_skip_origin_duplicate_send("telegram", "C1", None) is None


def test_no_origin_bound_returns_none():
    assert _maybe_skip_origin_duplicate_send("slack", "C1", None) is None


def test_none_chat_id_returns_none():
    assert _maybe_skip_origin_duplicate_send("slack", None, None) is None


def test_os_environ_fallback_covers_cli_subprocess(monkeypatch):
    # a hermes send subprocess has no ContextVars — it inherits the gateway's os.environ mirror
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "Slack")  # platform compare is case-insensitive
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "C1")
    monkeypatch.delenv("HERMES_SESSION_THREAD_ID", raising=False)
    result = _maybe_skip_origin_duplicate_send("slack", "C1", None)
    assert result is not None and result["skipped"] is True


def test_empty_origin_chat_id_does_not_skip(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "slack")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "")
    assert _maybe_skip_origin_duplicate_send("slack", "C1", None) is None
