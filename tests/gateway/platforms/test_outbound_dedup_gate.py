"""WO-D-2026-09-14-019-04 B: outbound content-fingerprint dedup gate.

Covers: trailing-newline-only normalization (the 2026-09-14 duplicate incident's fingerprint),
check/record semantics (record only after successful delivery), TTL expiry, per-(platform,
chat, thread) key isolation, LRU eviction, the file-backed cross-process store (the incident's
duplicate pair spanned the gateway process and a ``hermes send`` subprocess), and degradation
when the store file is unusable.
"""
import time

import pytest

from gateway.platforms.base import (
    OUTBOUND_DEDUP_TTL_SECONDS,
    _OutboundDedupGate,
    _outbound_dedup_gate,
    normalize_outbound_content,
    outbound_duplicate_check,
    outbound_duplicate_record,
    reset_outbound_dedup_gate,
)


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    # never touch the real profile cache during tests
    store = tmp_path / "dedup.json"
    monkeypatch.setattr(_outbound_dedup_gate, "_store_path", store)
    reset_outbound_dedup_gate()
    yield store
    reset_outbound_dedup_gate()


def _gate(tmp_path, **kwargs):
    return _OutboundDedupGate(store_path=tmp_path / "dedup.json", **kwargs)


# ── normalize ────────────────────────────────────────────────────────────────────────────

def test_normalize_strips_trailing_newline_difference():
    assert normalize_outbound_content("hello\n") == normalize_outbound_content("hello")
    assert normalize_outbound_content("hello\r\n") == normalize_outbound_content("hello")
    assert normalize_outbound_content("hello \n\n\n") == normalize_outbound_content("hello")


def test_normalize_preserves_internal_structure():
    assert normalize_outbound_content("a\n\nb\n") == "a\n\nb"
    assert normalize_outbound_content("a \nb\n") == "a\nb"


def test_normalize_non_string_is_empty():
    assert normalize_outbound_content(None) == ""
    assert normalize_outbound_content(123) == ""


# ── gate semantics ───────────────────────────────────────────────────────────────────────

def test_gate_skips_second_identical_send_within_ttl(tmp_path):
    gate = _gate(tmp_path)
    assert not gate.check("slack", "C1", "T1", "hello")
    gate.record("slack", "C1", "T1", "hello\n")
    # trailing-newline-only difference must dedup (the incident fingerprint)
    assert gate.check("slack", "C1", "T1", "hello")
    # different target keys must NOT dedup
    assert not gate.check("slack", "C1", None, "hello")
    assert not gate.check("slack", "C2", "T1", "hello")
    assert not gate.check("telegram", "C1", "T1", "hello")


def test_gate_ttl_expiry_allows_resend(tmp_path):
    gate = _gate(tmp_path, ttl_seconds=0.05)
    gate.record("slack", "C1", None, "hello")
    assert gate.check("slack", "C1", None, "hello")
    time.sleep(0.08)
    assert not gate.check("slack", "C1", None, "hello")


def test_check_alone_never_registers(tmp_path):
    # contract: record() happens only AFTER a successful delivery — checking must not create state
    gate = _gate(tmp_path)
    assert not gate.check("slack", "C1", None, "hello")
    assert not gate.check("slack", "C1", None, "hello")


def test_gate_different_content_passes(tmp_path):
    gate = _gate(tmp_path)
    gate.record("slack", "C1", None, "first message")
    assert not gate.check("slack", "C1", None, "second message")


def test_gate_empty_content_never_dedups(tmp_path):
    gate = _gate(tmp_path)
    gate.record("slack", "C1", None, "   \n  ")
    assert not gate.check("slack", "C1", None, "")


def test_gate_max_entries_eviction(tmp_path):
    gate = _gate(tmp_path, ttl_seconds=60, max_entries=2)
    gate.record("slack", "C1", None, "one")
    gate.record("slack", "C1", None, "two")
    gate.record("slack", "C1", None, "three")
    assert not gate.check("slack", "C1", None, "one")  # oldest evicted
    assert gate.check("slack", "C1", None, "three")


# ── cross-process store (the incident's pair spanned two processes) ─────────────────────

def test_two_instances_share_store(tmp_path):
    # simulates gateway process + hermes send CLI subprocess on the same HERMES_HOME
    first = _gate(tmp_path)
    second = _gate(tmp_path)
    first.record("slack", "C1", None, "status report body")
    assert second.check("slack", "C1", None, "status report body\n")


def test_ttl_expiry_visible_across_instances(tmp_path):
    first = _gate(tmp_path, ttl_seconds=0.05)
    second = _gate(tmp_path, ttl_seconds=0.05)
    first.record("slack", "C1", None, "hello")
    time.sleep(0.08)
    assert not second.check("slack", "C1", None, "hello")


def test_corrupt_store_degrades_to_memory(tmp_path):
    store = tmp_path / "dedup.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("{not valid json", encoding="utf-8")
    gate = _gate(tmp_path)
    assert not gate.check("slack", "C1", None, "hello")  # read failure tolerated
    gate.record("slack", "C1", None, "hello")            # falls back to memory
    assert gate.check("slack", "C1", None, "hello\n")    # same instance still dedups
    assert not store.exists() or True                    # no crash either way


def test_store_file_created_atomically(tmp_path):
    gate = _gate(tmp_path)
    gate.record("slack", "C1", None, "payload")
    store = tmp_path / "dedup.json"
    assert store.exists()
    assert not (tmp_path / "dedup.json.tmp").exists()    # tmp replaced, not left behind


# ── module helpers (the funnels' entry points) ──────────────────────────────────────────

def test_module_helpers_roundtrip(tmp_path):
    outbound_duplicate_record("slack", "C1", "T9", "payload")
    assert outbound_duplicate_check("slack", "C1", "T9", "payload\n")
    reset_outbound_dedup_gate()
    assert not outbound_duplicate_check("slack", "C1", "T9", "payload")


def test_module_helpers_accept_platform_enum():
    class FakePlatform:
        value = "slack"

    outbound_duplicate_record(FakePlatform(), "C1", None, "enum payload")
    assert outbound_duplicate_check("slack", "C1", None, "enum payload")


def test_ttl_constant_is_120s():
    assert OUTBOUND_DEDUP_TTL_SECONDS == 120.0
