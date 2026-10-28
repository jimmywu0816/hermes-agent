"""Fleet delivery_policy: creation-time auto-derive + update-path governance (D-2026-09-18-054).

Contract under test:

- Job store (cron/jobs.py): ``create_job`` auto-derives ``delivery_policy`` from the
  resolved ``deliver`` value (local / slack_report / slack_alert / slack_project — the
  fleet lint's vocabulary and channel mapping). Unmanaged targets (origin, telegram:*,
  unknown channels) stay policy-less so the fleet lint flags them (governance visibility
  over guessing). An explicit invalid value raises before persisting.
- Update path (update_job): the policy is settable/clearable and invalid values raise
  BEFORE the merge; unrelated runtime writebacks must never disturb the stored policy.
- Tool path (cronjob_manage): creation without an explicit deliver target is rejected
  (implicit-origin jobs can no longer slip past the fleet lint); create echoes the
  derived policy; update sets/clears it ('' clears).
"""
import json

import pytest

from cron import jobs
from cron.jobs import DELIVERY_POLICY_VALUES, derive_delivery_policy
from tools import cronjob_tools


def _dispatch(**kwargs):
    return json.loads(cronjob_tools.registry.dispatch("cronjob_manage", kwargs))


class TestDeriveDeliveryPolicy:
    @pytest.mark.parametrize("deliver,expected", [
        ("local", "local"),
        ("slack:C0BUKBA6LBE:1788366624.588429", "slack_report"),
        ("slack:C0BU1JH6XDM", "slack_alert"),
        ("slack:C0BU1JH6XDM:1789652233.740149", "slack_alert"),
        ("slack:C0BULDX38KT", "slack_project"),
        ("slack:C0BUE6N6AS1:1788603276.8", "slack_project"),
        ("origin", None),
        ("telegram:-1001234567890", None),
        ("slack:C0BUNKNOWN9", None),
        ("slack:C0BU1JH6XDM,local", None),
        ("slack:C0BUK7C8JAW:1789669561.491669", "slack_project"),
        ("slack:C0BUK7C8JAW", "slack_project"),
        ("slack:C0BVBTDK6NL", "slack_project"),
        ("slack:C0BUKBA6LBE", None),  # bare report channel: lint requires a thread suffix
        (None, None),
        ("", None),
    ])
    def test_derive_mapping(self, deliver, expected):
        assert derive_delivery_policy(deliver) == expected


class TestCreateJobDeliveryPolicy:
    def test_absent_arg_derives_and_persists(self, tmp_path):
        with jobs.use_cron_store(tmp_path / "cron"):
            job = jobs.create_job(prompt="hi", schedule="every 1h",
                                  deliver="slack:C0BUKBA6LBE:1788366624.588429")
            assert job["delivery_policy"] == "slack_report"
            stored = jobs.load_jobs()[0]
            assert stored["delivery_policy"] == "slack_report"

    def test_local_creation_derives_local(self, tmp_path):
        with jobs.use_cron_store(tmp_path / "cron"):
            job = jobs.create_job(prompt="hi", schedule="every 1h", deliver="local")
            assert job["delivery_policy"] == "local"

    def test_origin_without_explicit_deliver_stays_policyless(self, tmp_path):
        # deliver defaults to "origin" (origin given) — unmanaged: no policy key, lint flags.
        with jobs.use_cron_store(tmp_path / "cron"):
            job = jobs.create_job(prompt="hi", schedule="every 1h",
                                  origin={"platform": "slack", "chat_id": "C123", "thread_id": "1.2"})
            assert job["deliver"] == "origin"
            assert job.get("delivery_policy") is None
            assert "delivery_policy" not in job

    def test_explicit_policy_respected(self, tmp_path):
        with jobs.use_cron_store(tmp_path / "cron"):
            job = jobs.create_job(prompt="hi", schedule="every 1h",
                                  deliver="slack:C0BU1JH6XDM", delivery_policy="slack_alert")
            assert job["delivery_policy"] == "slack_alert"

    def test_explicit_invalid_policy_raises_without_persisting(self, tmp_path):
        with jobs.use_cron_store(tmp_path / "cron"):
            with pytest.raises(ValueError, match="delivery_policy"):
                jobs.create_job(prompt="hi", schedule="every 1h", deliver="local",
                                delivery_policy="slack_banana")
            assert jobs.load_jobs() == []

    def test_explicit_policy_conflicting_with_deliver_raises(self, tmp_path):
        # WO-D-2026-09-18-067-01: an explicit policy must agree with the derived
        # one; unmanaged targets (derived None) keep the conscious-choice path.
        with jobs.use_cron_store(tmp_path / "cron"):
            with pytest.raises(ValueError, match="conflicts with deliver"):
                jobs.create_job(prompt="hi", schedule="every 1h", deliver="local",
                                delivery_policy="slack_project")
            assert jobs.load_jobs() == []
            job = jobs.create_job(prompt="hi", schedule="every 1h",
                                  deliver="slack:C0BU1JH6XDM",
                                  delivery_policy="slack_alert")
            assert job["delivery_policy"] == "slack_alert"


class TestUpdateJobDeliveryPolicy:
    def _seed(self, tmp_path):
        return jobs.create_job(prompt="hi", schedule="every 1h", deliver="local")

    def test_set_and_clear(self, tmp_path):
        with jobs.use_cron_store(tmp_path / "cron"):
            job = self._seed(tmp_path)
            updated = jobs.update_job(job["id"], {"delivery_policy": "local"})
            assert updated["delivery_policy"] == "local"
            cleared = jobs.update_job(job["id"], {"delivery_policy": None})
            assert cleared.get("delivery_policy") is None

    def test_invalid_value_raises_before_merge(self, tmp_path):
        with jobs.use_cron_store(tmp_path / "cron"):
            job = self._seed(tmp_path)
            before = json.dumps(jobs.load_jobs(), sort_keys=True)
            with pytest.raises(ValueError, match="delivery_policy"):
                jobs.update_job(job["id"], {"delivery_policy": "nonsense"})
            assert json.dumps(jobs.load_jobs(), sort_keys=True) == before

    def test_unrelated_runtime_writeback_preserves_policy(self, tmp_path):
        # Simulate the engine's state writeback: an unrelated field update through the
        # merge path must leave the stored policy byte-identical.
        with jobs.use_cron_store(tmp_path / "cron"):
            job = jobs.create_job(prompt="hi", schedule="every 1h",
                                  deliver="slack:C0BU1JH6XDM")
            updated = jobs.update_job(job["id"], {"last_error": "transient"})
            assert updated["delivery_policy"] == "slack_alert"

    def test_explicit_policy_conflict_raises_before_merge(self, tmp_path):
        # WO-D-2026-09-18-067-01: explicit policy vs the deliver in effect after
        # the merge — conflict raises before persisting, store byte-identical.
        with jobs.use_cron_store(tmp_path / "cron"):
            job = jobs.create_job(prompt="hi", schedule="every 1h", deliver="local")
            before = json.dumps(jobs.load_jobs(), sort_keys=True)
            with pytest.raises(ValueError, match="conflicts with deliver"):
                jobs.update_job(job["id"], {"delivery_policy": "slack_project"})
            assert json.dumps(jobs.load_jobs(), sort_keys=True) == before

    def test_deliver_retarget_rederives_policy(self, tmp_path):
        # Deliver retargeted without an explicit policy: managed target re-derives.
        with jobs.use_cron_store(tmp_path / "cron"):
            job = jobs.create_job(prompt="hi", schedule="every 1h", deliver="local")
            updated = jobs.update_job(job["id"], {"deliver": "slack:C0BU1JH6XDM"})
            assert updated["delivery_policy"] == "slack_alert"


class TestToolPathDeliveryPolicy:
    def test_create_without_deliver_is_rejected(self, tmp_path):
        with jobs.use_cron_store(tmp_path / "cron"):
            result = _dispatch(action="create", schedule="every 1h", prompt="canary")
            assert result["success"] is False
            assert "explicit deliver" in result["error"]
            assert jobs.load_jobs() == []

    def test_create_derives_and_echoes_policy(self, tmp_path, make_cron_provider):
        provider = make_cron_provider(register_job=lambda job: None)
        with jobs.use_cron_store(tmp_path / "cron"):
            import cron.scheduler_provider
            original = cron.scheduler_provider.resolve_cron_scheduler
            cron.scheduler_provider.resolve_cron_scheduler = lambda: provider
            try:
                result = _dispatch(action="create", schedule="every 1h", prompt="canary",
                                   deliver="local")
            finally:
                cron.scheduler_provider.resolve_cron_scheduler = original
            assert result["success"], result
            assert result["deliver"] == "local"
            assert result["delivery_policy"] == "local"
            assert jobs.load_jobs()[0]["delivery_policy"] == "local"

    def test_update_sets_and_clears(self, tmp_path, make_cron_provider):
        provider = make_cron_provider(register_job=lambda job: None)
        with jobs.use_cron_store(tmp_path / "cron"):
            import cron.scheduler_provider
            original = cron.scheduler_provider.resolve_cron_scheduler
            cron.scheduler_provider.resolve_cron_scheduler = lambda: provider
            try:
                created = _dispatch(action="create", schedule="every 1h", prompt="canary",
                                    deliver="local")
                job_id = created["job_id"]
                updated = _dispatch(action="update", job_id=job_id, delivery_policy="local")
                assert updated["success"], updated
                assert jobs.load_jobs()[0]["delivery_policy"] == "local"
                cleared = _dispatch(action="update", job_id=job_id, delivery_policy="")
                assert cleared["success"], cleared
                assert jobs.load_jobs()[0].get("delivery_policy") is None
            finally:
                cron.scheduler_provider.resolve_cron_scheduler = original
