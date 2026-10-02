"""Stale-code guard: a cron worker whose checkout changed while the run was pending must abort
before the agent is constructed (WO-D-2026-09-23-009-08).

Root cause this closes: a long pre-run script (brand ``gbp-random-delay.py``, up to 1260 min)
crosses the daily 04:30 update window; the worker's already-imported modules then disagree with
the new on-disk code and agent construction raises ImportError / signature errors. The guard
aborts that round before state.db is opened or the agent is built, and the next scheduled fire
runs on the updated code.

The probe is deliberately subprocess-free (it reads git metadata directly): the cron run path
must not spawn extra processes — fault-injection harnesses key off Popen ordering, and installs
without a git binary on PATH must keep working.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

from cron.scheduler import _repo_code_fingerprint, _stale_code_abort_reason, run_job

_RUNTIME = {
    "api_key": "test-key",
    "base_url": "https://example.invalid/v1",
    "provider": "openrouter",
    "api_mode": "chat_completions",
}

_START = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"
_CHANGED = "9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c"


# --- fingerprint probe ---------------------------------------------------------------------


def test_fingerprint_none_without_git_dir(tmp_path):
    assert _repo_code_fingerprint(tmp_path) is None


def test_fingerprint_reads_attached_head(tmp_path):
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "refs" / "heads" / "main").write_text(f"{_START}\n")
    assert _repo_code_fingerprint(tmp_path) == _START


def test_fingerprint_reads_detached_head(tmp_path):
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text(f"{_CHANGED}\n")
    assert _repo_code_fingerprint(tmp_path) == _CHANGED


def test_fingerprint_rejects_malformed_oid(tmp_path):
    git = tmp_path / ".git"
    git.mkdir()
    for bad in ("f" * 38, "f" * 41, "z" * 40, "1234", "  "):
        (git / "HEAD").write_text(f"{bad}\n")
        assert _repo_code_fingerprint(tmp_path) is None, bad


def test_fingerprint_accepts_sha256_oid(tmp_path):
    git = tmp_path / ".git"
    git.mkdir()
    sha256 = "0123456789abcdef" * 4  # 64 hex chars
    (git / "HEAD").write_text(sha256.upper() + "\n")
    assert _repo_code_fingerprint(tmp_path) == sha256  # normalized to lower case


def test_fingerprint_reads_packed_ref(tmp_path):
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        f"{_START} refs/heads/main\n"
        f"{_CHANGED} refs/heads/other\n"
    )
    assert _repo_code_fingerprint(tmp_path) == _START


def test_fingerprint_follows_gitdir_indirection(tmp_path):
    real_git = tmp_path / "real-gitdir"
    real_git.mkdir()
    (real_git / "HEAD").write_text(f"{_START}\n")
    repo = tmp_path / "worktree"
    repo.mkdir()
    (repo / ".git").write_text(f"gitdir: {real_git}\n")
    assert _repo_code_fingerprint(repo) == _START


def test_fingerprint_follows_linked_worktree_commondir(tmp_path):
    """Linked worktree: HEAD lives in the per-worktree gitdir, refs in the common dir."""
    common = tmp_path / "main-git"
    (common / "refs" / "heads").mkdir(parents=True)
    (common / "refs" / "heads" / "main").write_text(f"{_START}\n")
    wt_git = common / "worktrees" / "wt1"
    wt_git.mkdir(parents=True)
    (wt_git / "HEAD").write_text("ref: refs/heads/main\n")
    (wt_git / "commondir").write_text("../..\n")
    repo = tmp_path / "wt1"
    repo.mkdir()
    (repo / ".git").write_text(f"gitdir: {wt_git}\n")
    assert _repo_code_fingerprint(repo) == _START


def test_fingerprint_linked_worktree_packed_ref(tmp_path):
    common = tmp_path / "main-git"
    common.mkdir()
    (common / "packed-refs").write_text(f"{_CHANGED} refs/heads/main\n")
    wt_git = common / "worktrees" / "wt2"
    wt_git.mkdir(parents=True)
    (wt_git / "HEAD").write_text("ref: refs/heads/main\n")
    (wt_git / "commondir").write_text("../..\n")
    repo = tmp_path / "wt2"
    repo.mkdir()
    (repo / ".git").write_text(f"gitdir: {wt_git}\n")
    assert _repo_code_fingerprint(repo) == _CHANGED


def test_fingerprint_none_on_bogus_git_file(tmp_path):
    (tmp_path / ".git").write_text("not a gitdir marker\n")
    assert _repo_code_fingerprint(tmp_path) is None


def test_fingerprint_none_on_unreadable_head(tmp_path):
    git = tmp_path / ".git"
    (git / "HEAD").mkdir(parents=True)  # a directory where a file is expected
    assert _repo_code_fingerprint(tmp_path) is None


def test_fingerprint_live_checkout_matches_head_file():
    fp = _repo_code_fingerprint()
    head_file = Path(__file__).resolve().parent.parent.parent / ".git" / "HEAD"
    if head_file.exists():
        assert fp is not None and re.fullmatch(r"[0-9a-f]{40}", fp)
    else:  # source tarball / no checkout: fail-open
        assert fp is None


# --- abort decision ------------------------------------------------------------------------


def test_no_abort_when_fingerprint_unchanged():
    with patch("cron.scheduler._repo_code_fingerprint", return_value=_START):
        assert _stale_code_abort_reason(_START) is None


def test_no_abort_when_start_unknown():
    # Fail-open: a run that could not fingerprint its start must never be aborted.
    with patch("cron.scheduler._repo_code_fingerprint", return_value=_CHANGED) as probe:
        assert _stale_code_abort_reason(None) is None
    probe.assert_not_called()


def test_no_abort_when_current_unknown():
    with patch("cron.scheduler._repo_code_fingerprint", return_value=None):
        assert _stale_code_abort_reason(_START) is None


def test_abort_reason_names_both_heads():
    with patch("cron.scheduler._repo_code_fingerprint", return_value=_CHANGED):
        reason = _stale_code_abort_reason(_START)
    assert reason is not None
    assert _START[:10] in reason and _CHANGED[:10] in reason
    assert "aborted before agent construction" in reason


# --- run_job integration -------------------------------------------------------------------


def test_run_job_aborts_before_constructing_agent_on_code_change(tmp_path):
    """The stale-worker path: start fingerprint differs from the pre-construction check."""
    job = {"id": "stale-code-guard", "name": "stale guard test", "prompt": "hello"}
    fingerprints = iter([_START, _CHANGED])

    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=_RUNTIME), \
         patch("cron.scheduler._repo_code_fingerprint", side_effect=lambda: next(fingerprints)), \
         patch("cron.scheduler._open_cron_session_db") as open_db, \
         patch("cron.scheduler._construct_cron_agent") as construct:
        success, output, final_response, error = run_job(job)

    assert success is False
    assert error and "Stale cron worker aborted" in error
    assert "Stale cron worker aborted" in output
    construct.assert_not_called()
    open_db.assert_not_called()
    # Not misclassified as a retryable / parked failure (Codex review 建議 1).
    assert not job.get("_model_unreachable")
    assert not job.get("_quota_hold_seconds")


def test_run_job_probe_order_and_no_setup_before_check(tmp_path):
    """Order guarantee (WO-009-08 必改 #1/#4): first probe → pre-run script → second probe → abort,
    with NO runtime setup or lazy agent import happening before the check."""
    import sys

    job = {"id": "stale-order", "name": "order test", "prompt": "hello", "script": "prerun.py"}
    order = []
    fingerprints = iter([_START, _CHANGED])

    def probe():
        order.append("probe")
        return next(fingerprints)

    def prerun(*_args, **_kwargs):
        order.append("prerun")
        return (True, "ok\n")  # non-empty output: the wake gate and prompt build both proceed

    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=_RUNTIME), \
         patch("cron.scheduler._repo_code_fingerprint", side_effect=probe), \
         patch("cron.scheduler._run_job_script_with_claim_heartbeat", side_effect=prerun), \
         patch("cron.scheduler._CronRunScope") as scope_cls, \
         patch("cron.scheduler._load_cron_job_config") as load_cfg, \
         patch("cron.scheduler._resolve_cron_agent_setup") as resolve_setup, \
         patch("cron.scheduler._reload_dotenv_and_publish_delivery_target") as reload_hook, \
         patch("cron.scheduler._open_cron_session_db") as open_db, \
         patch("cron.scheduler._construct_cron_agent") as construct:
        saved_run_agent = sys.modules.pop("run_agent", None)
        try:
            success, output, final_response, error = run_job(job)
            run_agent_imported = "run_agent" in sys.modules
        finally:
            if saved_run_agent is not None:
                sys.modules["run_agent"] = saved_run_agent

    assert order == ["probe", "prerun", "probe"]
    assert success is False
    assert error and "Stale cron worker aborted" in error
    # Nothing in the run path may execute before the check:
    scope_cls.assert_not_called()        # no run-scope creation/entry
    load_cfg.assert_not_called()         # no cron job config load
    resolve_setup.assert_not_called()    # no provider/runtime resolution
    reload_hook.assert_not_called()      # no dotenv reload
    open_db.assert_not_called()          # no state.db
    construct.assert_not_called()        # no agent construction
    assert run_agent_imported is False   # the lazy `from run_agent import AIAgent` did not run


def test_run_job_constructs_normally_when_fingerprint_unchanged(tmp_path):
    """Control: an unchanged checkout keeps the normal path (the guard is a no-op)."""
    job = {"id": "stale-code-guard-control", "name": "stale guard control", "prompt": "hello"}
    constructed = MagicMock()
    fake_agent = MagicMock()
    fake_agent.run_conversation.return_value = {"final_response": "ok"}
    constructed.return_value = fake_agent

    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=_RUNTIME), \
         patch("cron.scheduler._open_cron_session_db", return_value=None), \
         patch("cron.scheduler._repo_code_fingerprint", return_value=_START), \
         patch("cron.scheduler._construct_cron_agent", constructed):
        success, output, final_response, error = run_job(job)

    assert constructed.called
    assert success is True
    assert final_response == "ok"
