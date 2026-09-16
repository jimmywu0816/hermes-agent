"""Yielded-to-background partial output redaction (T3, WO-D-2026-09-16-003-01).

The foreground→background yield branch used to return ``output_so_far`` RAW to
the model, bypassing ``finalize_foreground_result``'s command-aware redaction
(independent review 2026-09-16: a secret printed before the user's mid-command
message arrived shipped verbatim to the LLM). The invariant: whatever the
foreground path redacts, the yielded branch must redact too. All values below
are known dummies; no real command is executed (``env.execute`` is faked).
"""

import json
from types import SimpleNamespace

from tools import terminal_tool


DUMMY = "sk-EXAMPLE-dummy-0123456789abcdef"


def _fake_plan():
    return SimpleNamespace(env_type="local", effective_task_id="task-x",
                           effective_timeout=10, cwd="/tmp")


def _run_yielded(monkeypatch, raw: str, command: str = "env && echo working"):
    """Run ``_run_foreground`` with a fake env that yields mid-command."""
    monkeypatch.setattr(terminal_tool, "_resolve_command_cwd", lambda **kwargs: "/tmp")

    def fake_execute(cmd, **kwargs):
        return {"yielded_session_id": "sess-y1", "output": raw, "pid": 4242}

    env = SimpleNamespace(execute=fake_execute)
    out = terminal_tool._run_foreground(
        command, env, _fake_plan(), task_id="task-x", session_id="s1",
        session_key="sess", workdir=None, approval_note=None, clear_interrupt=False)
    return json.loads(out)


def test_yielded_partial_output_redacted(monkeypatch):
    data = _run_yielded(monkeypatch, f"working...\nOPENAI_KEY={DUMMY}\nstill running")
    assert data["status"] == "yielded_to_background"
    assert data["session_id"] == "sess-y1"
    assert DUMMY not in data["output"]
    # The VALUE is masked while the NAME stays visible — same shape as the
    # foreground path (names are not secrets; values are).
    assert "OPENAI_KEY=***" in data["output"]
    assert "working..." in data["output"]  # benign partial content preserved
    assert data["note"]  # yielded note unchanged


def test_yielded_env_dump_output_redacted(monkeypatch):
    # env-dump shaped command: the command-aware ENV pass must fire too.
    data = _run_yielded(monkeypatch, f"OPENAI_KEY={DUMMY}\nPATH=/usr/bin",
                        command="env")
    assert DUMMY not in data["output"]


def test_yielded_empty_output_safe(monkeypatch):
    data = _run_yielded(monkeypatch, "", command="sleep 100")
    assert data["status"] == "yielded_to_background"
    assert data["output"] == ""
