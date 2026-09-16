"""Guardian egress redaction invariants (T2, WO-D-2026-09-16-003-01).

The smart-approval guardian is a THIRD-PARTY inference call, so its prompt is
an egress boundary: the command and description must be redacted there —
fail-closed (a redactor failure never falls back to raw text) — and escalate
verdicts must be audited like approve/deny. Semantic re-port of carried 0010
hardened per the 2026-09-16 independent review. All secret-looking values
below are known dummies; no external request is ever made (call_llm is faked).
"""

from types import SimpleNamespace

import pytest

from tools import approval_context as _ctx
from tools import approval_smart


DUMMY_MIXED = "hunter2-EXAMPLE-0123456789"
DUMMY_SPACED = "hunter2 EXAMPLE 0123456789"
DUMMY_MULTI = "hunter2-FIRST SECOND-EXAMPLE"


def _fake_llm(captured: list, answer: str = "APPROVE"):
    """Return a fake ``call_llm`` that records prompts and never hits the wire."""

    def fake_call_llm(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=answer))]
        )

    return fake_call_llm


def _capture_prompt(monkeypatch, command: str, description: str = "flagged command",
                    answer: str = "APPROVE") -> str:
    captured: list = []
    monkeypatch.setattr("agent.auxiliary_client.call_llm", _fake_llm(captured, answer))
    verdict = approval_smart._smart_approve(command, description)
    assert verdict == ("approve" if answer == "APPROVE" else answer.lower() if answer in {"DENY"} else "escalate")
    assert captured, "guardian prompt was never captured — call_llm not invoked?"
    return captured[0]["messages"][1]["content"]


# ---------------------------------------------------------------------------
# AC3: raw secrets never reach the guardian prompt.
# ---------------------------------------------------------------------------

def test_env_absolute_path_normalized(monkeypatch):
    prompt = _capture_prompt(monkeypatch, "cat /srv/app/.env && deploy")
    assert "/srv/app" not in prompt
    assert "<secret-file:.env>" in prompt
    assert "deploy" in prompt  # operation semantics preserved


def test_git_credentials_path_normalized(monkeypatch):
    prompt = _capture_prompt(monkeypatch, "git push", "reads /home/u/.git-credentials")
    assert "/home/u/.git-credentials" not in prompt
    assert "<secret-file:.git-credentials>" in prompt


def test_url_userinfo_redacted(monkeypatch):
    prompt = _capture_prompt(
        monkeypatch, f"curl https://alice:{DUMMY_MIXED}@example.test/path")
    assert DUMMY_MIXED not in prompt
    assert "alice" in prompt  # username is not a secret


def test_quoted_value_with_spaces_redacted(monkeypatch):
    prompt = _capture_prompt(monkeypatch, f"export OpenAi_Key='{DUMMY_SPACED}'")
    assert DUMMY_SPACED not in prompt


def test_multiline_quoted_value_redacted(monkeypatch):
    command = f'DB_PASSWORD="{DUMMY_MULTI.split()[0]}\n{DUMMY_MULTI.split()[1]}"\ndeploy'
    prompt = _capture_prompt(monkeypatch, command)
    assert DUMMY_MULTI.split()[0] not in prompt
    assert DUMMY_MULTI.split()[1] not in prompt


def test_bare_env_name_normalized(monkeypatch):
    prompt = _capture_prompt(monkeypatch, "source .env && run")
    assert "<secret-file:.env>" in prompt


def test_run_secrets_path_normalized(monkeypatch):
    prompt = _capture_prompt(monkeypatch, "cat /run/secrets/db_pass")
    assert "/run/secrets/db_pass" not in prompt
    assert "<secret-file:" in prompt


# ---------------------------------------------------------------------------
# Benign content survives: the guardian must still see the real command.
# ---------------------------------------------------------------------------

def test_benign_names_and_values_untouched(monkeypatch):
    command = "MAX_TOKENS='100' TOKENIZERS_PARALLELISM=true KEYBOARD=x python train.py"
    prompt = _capture_prompt(monkeypatch, command)
    assert "MAX_TOKENS='100'" in prompt
    assert "TOKENIZERS_PARALLELISM=true" in prompt
    assert "KEYBOARD=x" in prompt


# ---------------------------------------------------------------------------
# Fail-closed: a redactor failure never ships raw text.
# ---------------------------------------------------------------------------

def test_redactor_failure_fails_closed(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("redactor exploded (test)")

    monkeypatch.setattr("agent.redact.redact_sensitive_text", boom)
    captured: list = []
    monkeypatch.setattr("agent.auxiliary_client.call_llm", _fake_llm(captured, "ESCALATE"))
    verdict = approval_smart._smart_approve(
        f"cat /srv/app/.env  # {DUMMY_MIXED}", "read config")
    assert verdict == "escalate"
    prompt = captured[0]["messages"][1]["content"]
    assert DUMMY_MIXED not in prompt
    assert "guardian-egress-redaction-failed" in prompt


# ---------------------------------------------------------------------------
# Escalate verdicts are audited (AC3): choice=smart_escalate in the post hook.
# ---------------------------------------------------------------------------

def _capture_hooks(monkeypatch, answer: str):
    hooks: list = []
    monkeypatch.setattr(_ctx, "_fire_approval_hook",
                        lambda name, **kwargs: hooks.append((name, kwargs)))
    captured: list = []
    monkeypatch.setattr("agent.auxiliary_client.call_llm", _fake_llm(captured, answer))
    verdict = approval_smart._smart_verdict(
        "rm -rf /tmp/x", "recursive delete", "rm_recursive", ["rm_recursive"], "sess")
    return verdict, hooks


def test_escalate_verdict_recorded_in_audit(monkeypatch):
    verdict, hooks = _capture_hooks(monkeypatch, "ESCALATE")
    assert verdict == "escalate"
    names = [name for name, _ in hooks]
    assert names == ["pre_approval_request", "post_approval_response"]
    post = dict(hooks[1][1])
    assert post["choice"] == "smart_escalate"
    assert post["decided_by"] == "aux_llm"


def test_approve_verdict_recorded_in_audit(monkeypatch):
    verdict, hooks = _capture_hooks(monkeypatch, "APPROVE")
    assert verdict == "approve"
    post = dict(hooks[1][1])
    assert post["choice"] == "smart_approve"


# ---------------------------------------------------------------------------
# Word-boundary policy of the helper itself (unit level).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("OpenAi_Key", True),
    ("access_token", True),
    ("MY_SECRET", True),
    ("db_password", True),
    ("MAX_TOKENS", True),  # conservative: name-level match, benign counters are kept by the value-shape gate
    ("KEYBOARD", False),
    ("TOKENIZERS_PARALLELISM", False),
    ("MONKEY", False),
    ("git_credentials", True),
    ("apikey", True),
])
def test_name_has_secret_word(name, expected):
    assert approval_smart._name_has_secret_word(name) is expected
