"""Contract tests for the gateway worker scope cgroup argv (T4, WO-D-2026-09-16-003-01).

A gateway-spawned worker must never spill unbounded swap: a 2026-09-15 incident on
a 32 GiB host had a single pytest worker fill 3.9 GiB of an 8 GiB host swap file,
driving the whole user slice into a file-backed refault livelock while ~11 GiB RAM
sat free. The scope therefore carries a MemorySwapMax bound alongside MemoryMax,
and that bound can never be wider than the RAM bound. Semantic re-port of carried
0009 (candidate commit 14d34cedd7).
"""

from unittest.mock import patch

from tools.process_registry import (
    _WORKER_MEMORY_SWAP_MAX_BYTES,
    _systemd_scope_argv,
    _systemd_run_user_scope_available,
    _worker_memory_max_bytes,
    _worker_memory_swap_max_bytes,
)


def _scope_properties(argv):
    props = {}
    for i, token in enumerate(argv):
        if token == "--property":
            key, _, value = argv[i + 1].partition("=")
            props[key] = value
    return props


def test_worker_scope_bounds_swap_spillover():
    argv = _systemd_scope_argv("/usr/bin/systemd-run", "hermes-worker-test", "/bin/true")
    props = _scope_properties(argv)
    assert "MemorySwapMax" in props
    assert int(props["MemorySwapMax"]) == _WORKER_MEMORY_SWAP_MAX_BYTES


def test_worker_scope_swap_bound_never_exceeds_memory_bound():
    assert _worker_memory_swap_max_bytes() <= _WORKER_MEMORY_SWAP_MAX_BYTES
    argv = _systemd_scope_argv("/usr/bin/systemd-run", "hermes-worker-test", "/bin/true")
    props = _scope_properties(argv)
    assert int(props["MemorySwapMax"]) <= int(props["MemoryMax"])


def test_worker_scope_swap_tightens_with_memory_override(monkeypatch):
    # A tightened MemoryMax (e.g. TERMINAL_LOCAL_MEMORY_MAX_MB=123) must pull the
    # swap bound down with it — the swap bound is min(cap, MemoryMax).
    monkeypatch.setenv("TERMINAL_LOCAL_MEMORY_MAX_MB", "123")
    argv = _systemd_scope_argv("/usr/bin/systemd-run", "hermes-worker-test", "/bin/true")
    props = _scope_properties(argv)
    assert int(props["MemorySwapMax"]) == 123 * 1024 * 1024
    assert int(props["MemorySwapMax"]) == int(props["MemoryMax"])


def test_worker_scope_never_emits_oompolicy():
    argv = _systemd_scope_argv("/usr/bin/systemd-run", "hermes-worker-test", "/bin/true")
    assert not any("OOMPolicy" in token for token in argv)


def test_probe_and_real_spawn_share_the_same_argv_builder():
    # The probe and the real spawn both go through _systemd_scope_argv — adding
    # a property there is automatically validated by the probe (systemd rejects
    # unsupported properties, which flips availability to False).
    probe_argv = _systemd_scope_argv("/usr/bin/systemd-run", "hermes-probe-scope-1", "/bin/sh", "-c", "exit 0")
    spawn_argv = _systemd_scope_argv("/usr/bin/systemd-run", "hermes-worker-1", "/bin/bash", "-lc", "true")
    probe_props = _scope_properties(probe_argv)
    spawn_props = _scope_properties(spawn_argv)
    assert set(probe_props) == set(spawn_props)
    assert "MemorySwapMax" in probe_props


def test_unsupported_property_degrades_scope_availability(monkeypatch):
    """A systemd too old to know ``MemorySwapMax`` rejects the probe command —
    the probe must then report scope UNAVAILABLE (worker degrades to the
    unscoped fallback) instead of crashing or silently spawning scoped."""
    import tools.process_registry as pr

    monkeypatch.setattr(pr, "_SYSTEMD_SCOPE_AVAILABLE", None)
    monkeypatch.setattr(pr, "_SYSTEMD_SCOPE_PROBED_AT", 0.0)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemd-run")

    class _RejectedProperty:
        returncode = 1
        stderr = b"Failed to start transient service unit: Unknown property MemorySwapMax"

    with patch("tools.process_registry.subprocess.run", return_value=_RejectedProperty()) as run:
        assert _systemd_run_user_scope_available() is False
        # The probe ran with the SAME argv builder the real spawn uses: the
        # rejection is about the property, proving the probe validates it.
        argv = run.call_args.args[0]
        assert "--property" in argv
        assert any(token.startswith("MemorySwapMax=") for token in argv)


def test_supported_property_keeps_scope_available(monkeypatch):
    import tools.process_registry as pr

    monkeypatch.setattr(pr, "_SYSTEMD_SCOPE_AVAILABLE", None)
    monkeypatch.setattr(pr, "_SYSTEMD_SCOPE_PROBED_AT", 0.0)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemd-run")

    class _Ok:
        returncode = 0
        stderr = b""

    with patch("tools.process_registry.subprocess.run", return_value=_Ok()):
        assert _systemd_run_user_scope_available() is True
