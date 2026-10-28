"""Create-time validation order: field errors win over the explicit-deliver gate.

D-2026-09-18-054 / WO-D-2026-09-18-067-01 keep the hard gate: an omitted deliver
is still rejected, and no job may be persisted first. These tests pin the
ordering rule from the user-visible side — a more specific, actionable error is
reported before the generic "explicit deliver" one.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from cron import jobs
from tools import cronjob_tools


@pytest.fixture
def cron_store(tmp_path):
    with jobs.use_cron_store(tmp_path / "cron-home"):
        yield tmp_path


def _dispatch(**kwargs):
    return json.loads(cronjob_tools.registry.dispatch("cronjob_manage", kwargs))


def test_omitted_deliver_does_not_mask_script_traversal(cron_store):
    result = _dispatch(
        action="create",
        schedule="every 1h",
        prompt="monitor things",
        script="../../etc/passwd",
    )

    assert result["success"] is False
    assert ("escapes" in result["error"].lower()
            or "traversal" in result["error"].lower()), result
    assert "explicit deliver" not in result["error"], result
    assert jobs.load_jobs() == []


def test_omitted_deliver_does_not_mask_bad_failure_deliver_profile(cron_store):
    result = _dispatch(
        action="create",
        schedule="every 1h",
        prompt="scan",
        failure_deliver="bot-chat:no-such-profile-xyz",
    )

    assert result["success"] is False
    assert "bot-chat delivery profile" in result["error"], result
    assert "explicit deliver" not in result["error"], result
    assert jobs.load_jobs() == []


def test_omitted_deliver_does_not_mask_stale_one_shot(cron_store, monkeypatch):
    now = datetime(2026, 3, 18, 4, 30, 0, tzinfo=UTC)
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)
    stale = (now - timedelta(minutes=5)).isoformat()

    result = _dispatch(
        action="create",
        schedule=stale,
        prompt="too late",
    )

    assert result["success"] is False
    assert "past and cannot be scheduled" in result["error"], result
    assert "explicit deliver" not in result["error"], result
    assert jobs.load_jobs() == []


def test_omitted_deliver_with_valid_fields_still_rejected_without_a_job(cron_store):
    result = _dispatch(
        action="create",
        schedule="every 1h",
        prompt="valid payload",
    )

    assert result["success"] is False
    assert "explicit deliver" in result["error"], result
    assert jobs.load_jobs() == []
