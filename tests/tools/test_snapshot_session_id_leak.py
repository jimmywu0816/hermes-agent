"""Cross-session HERMES_SESSION_ID leak via the shared bash snapshot.

Regression coverage for the bug where a single long-lived backend serves many
sessions through ONE ``_active_environments["default"]`` LocalEnvironment (the
messaging gateway, TUI, and desktop/web dashboard all collapse the terminal to
"default"). That environment persists a bash *session snapshot* file and
``source``s it before every command. ``export -p`` dumped the FIRST session's
``HERMES_SESSION_ID`` into the snapshot, so every LATER session ``source``d that
stale value and its ``echo $HERMES_SESSION_ID`` reported a FOREIGN session's id
— overriding the correct per-command Popen env injected by
``_inject_session_context_env``.

The fix strips the per-session bridged vars (HERMES_SESSION_* / UI /
CRON_AUTO_DELIVER_) from the snapshot at both dump sites in
``tools/environments/base_session_env.py``; they are re-injected fresh on every
command. The same dump must drop the scope markers a delegate_task child / cron
run stamps per command (HERMES_DELEGATED_CHILD_CONTEXT, HERMES_CRON_SESSION), or
the parent's next command is misread as that child (#90782, #71941).
"""

import os
import re
import shutil
import subprocess
import sys

import pytest

from tools.environments.base_session_env import (
    _SNAPSHOT_EXCLUDED_ENV_REGEX,
    _export_dump_excluding_session_vars,
)


# ---------------------------------------------------------------------------
# Unit: the exclusion regex matches exactly the bridged vars, nothing else.
# ---------------------------------------------------------------------------

def test_regex_matches_bridged_session_vars():
    rx = re.compile(_SNAPSHOT_EXCLUDED_ENV_REGEX)
    # Every var the gateway bridges, and the delegate_task marker, must be excluded.
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER
    from gateway.session_context import _VAR_MAP

    for name in (*_VAR_MAP, DELEGATED_CHILD_ENV_MARKER):
        line = f'declare -x {name}="whatever"'
        assert rx.search(line), f"{name} should be excluded from the snapshot"


def test_export_snippet_shape():
    snippet = _export_dump_excluding_session_vars('"$__hermes_snap_tmp"')
    assert "export -p" in snippet
    # Unset-by-name (not line-grep): multi-line declare values must not leave
    # continuation lines in the snapshot (issue #71296).
    assert "unset" in snippet
    assert "${!HERMES_SESSION_*}" in snippet
    assert "${!HERMES_CRON_AUTO_DELIVER_*}" in snippet
    assert "${!HERMES_BROWSER_CONTROL_*}" in snippet
    assert "HERMES_UI_SESSION_ID" in snippet
    assert "grep -vE" not in snippet
    assert '"$__hermes_snap_tmp"' in snippet
    # The redirection must be attached to a brace group wrapping the dump,
    # NOT to a pipeline segment: a redirect on a pipeline segment expands the
    # temp-path variable inside that segment's subshell (potentially
    # inconsistently with the parent that expands the follow-up ``mv``
    # operand), silently orphaning the dump and breaking snapshot env
    # persistence entirely.
    assert snippet.lstrip().startswith("{ ")
    assert "|| true; }" in snippet
    assert snippet.rstrip().endswith('> "$__hermes_snap_tmp"')
    # Credential-name scrub invariants (T1 semantic re-port of 0006):
    # bash 3.2-compatible nocasematch + word-boundary globs, NOT ${__v^^}
    # (bash 4.0+ only); export -n so readonly exported creds are scrubbed;
    # while-IFS= read so a non-whitespace IFS cannot swallow the list.
    assert "shopt -s nocasematch" in snippet
    assert "while IFS= read -r __v" in snippet
    assert 'case "_${__v}_" in' in snippet
    for marker in ("*_key_*", "*_token_*", "*_secret_*", "*_password_*", "*_passwd_*", "*_credential_*"):
        assert marker in snippet, f"{marker} should be in the scrub pattern"
    # camelCase suffixes (ApiKey) match via the *key_ tail.
    for tail in ("*key_", "*token_", "*secret_", "*password_", "*passwd_", "*credential_"):
        assert tail in snippet, f"{tail} should be in the scrub pattern"
    assert "export -n" in snippet
    # bash-4-only case fold must NOT be present (macOS bash 3.2 compat).
    assert "${__v^^}" not in snippet


# ---------------------------------------------------------------------------
# Integration: real LocalEnvironment, two sessions, no cross-contamination.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_shared_snapshot_no_cross_session_leak(tmp_path):
    import threading

    from gateway.session_context import _VAR_MAP, _UNSET, set_session_vars
    from tools.environments.local import LocalEnvironment

    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    env.init_session()
    try:
        def run_as(sid):
            out = {}

            def worker():
                for v in _VAR_MAP.values():
                    v.set(_UNSET)
                set_session_vars(session_key="k" + sid, session_id=sid, source="desktop")
                out["r"] = env.execute('echo "[$HERMES_SESSION_ID]"')

            t = threading.Thread(target=worker)
            t.start()
            t.join()
            return out["r"].get("output", "")

        out_a = run_as("SIDAAA")
        out_b = run_as("SIDBBB")

        assert "SIDAAA" in out_a, f"session A saw {out_a!r}"
        # The core assertion: B must see its OWN id, not A's leaked via snapshot.
        assert "SIDBBB" in out_b, f"session B saw {out_b!r}"
        assert "SIDAAA" not in out_b, f"session B leaked A's id: {out_b!r}"

        # And the snapshot file must not carry the session id at all.
        snap = env._snapshot_path
        if os.path.exists(snap):
            with open(snap) as f:
                assert "HERMES_SESSION_ID" not in f.read()
    finally:
        env.cleanup()


# ---------------------------------------------------------------------------
# #90782 / #71941: scope markers (delegate_task child, cron run) must not
# persist into the snapshot either.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_export_dump_drops_every_bridged_var_and_the_delegation_marker():
    """Run the real dump: nothing the gateway bridges per command, nor the
    delegate_task marker, may survive ``export -p``; ordinary exports must."""
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER
    from gateway.session_context import _VAR_MAP

    scoped = [*_VAR_MAP, DELEGATED_CHILD_ENV_MARKER]
    exports = "; ".join([f'export {n}="x"' for n in scoped] + ['export HERMES_HOME="/h"', 'export MYVAR="keep"'])
    out = subprocess.run(
        ["bash", "-c", f"{exports}; {_export_dump_excluding_session_vars('/dev/stdout')}"],
        capture_output=True, text=True, check=True).stdout
    leaked = [n for n in scoped if f"declare -x {n}=" in out]
    assert not leaked, f"persisted into the snapshot: {leaked}"
    assert 'declare -x HERMES_HOME="/h"' in out
    assert 'declare -x MYVAR="keep"' in out


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_snapshot_does_not_turn_later_commands_into_delegated_children(tmp_path):
    """A snapshot re-dumped during a delegated child's command must not re-export
    the marker into the parent's next ``source`` (#90782)."""
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER, delegated_child_context
    from tools.environments.local import LocalEnvironment

    probe = f'printf "[${{{DELEGATED_CHILD_ENV_MARKER}+set}}]"'
    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    try:
        with delegated_child_context():
            child = env.execute(probe)
        assert "[set]" in child["output"], f"delegated child lost its marker: {child!r}"
        parent = env.execute(probe)
        assert "[]" in parent["output"], f"parent command inherited the marker: {parent!r}"
    finally:
        env.cleanup()


# ---------------------------------------------------------------------------
# Credential-name scrub invariants (T1 semantic re-port of 0006):
# mixed/lower-case names, readonly exported creds, non-whitespace IFS
# regression, benign-name (TOKENIZERS_PARALLELISM) retention. All values
# below are known dummies, never real credentials.
# ---------------------------------------------------------------------------

def _bash() -> str:
    return shutil.which("bash") or "/bin/bash"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_export_snapshot_scrubs_credential_names_any_case(tmp_path):
    """Mixed/lower-case credential NAMES (dummy values) must not reach the snapshot."""
    import shlex
    import subprocess

    snap = tmp_path / "snap.sh"
    dump = _export_dump_excluding_session_vars(shlex.quote(str(snap)))
    q_snap = shlex.quote(str(snap))
    script = "set -e\n"
    script += "export OpenAi_Key=dummy-mixed-0123456789\n"
    script += "export openai_api_key=dummy-lower-0123456789\n"
    script += "export normal_var=keepme\n"
    script += dump + "\n"
    script += "if grep -qE 'OpenAi_Key|openai_api_key|dummy-mixed|dummy-lower' " + q_snap + "; then echo 'LEAKED_INTO_SNAPSHOT' >&2; exit 2; fi\n"
    script += "if ! grep -qE '^declare -x normal_var=' " + q_snap + "; then echo 'NORMAL_VAR_MISSING' >&2; exit 3; fi\n"
    result = subprocess.run(
        [_bash(), "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": os.environ.get("PATH", "")},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_export_snapshot_keeps_tokenizers_var(tmp_path):
    """TOKENIZERS_PARALLELISM (benign huggingface knob) must NOT be scrubbed.

    Regression for the earlier substring match (*TOKEN*) which also deleted
    TOKENIZERS_* vars. Word-boundary matching keeps it.
    """
    import shlex
    import subprocess

    snap = tmp_path / "snap.sh"
    dump = _export_dump_excluding_session_vars(shlex.quote(str(snap)))
    q_snap = shlex.quote(str(snap))
    script = "set -e\n"
    script += "export TOKENIZERS_PARALLELISM=true\n"
    script += "export ACCESS_TOKEN=dummy-token-0123456789\n"
    script += dump + "\n"
    script += "if ! grep -qE '^declare -x TOKENIZERS_PARALLELISM=' " + q_snap + "; then echo 'TOKENIZERS_SCRUBBED' >&2; exit 2; fi\n"
    script += "if grep -qE 'ACCESS_TOKEN|dummy-token' " + q_snap + "; then echo 'LEAKED_INTO_SNAPSHOT' >&2; exit 3; fi\n"
    result = subprocess.run(
        [_bash(), "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": os.environ.get("PATH", "")},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_export_snapshot_scrubs_readonly_exported_creds(tmp_path):
    """readonly exported credential vars are scrubbed via export -n (unset would
    error out on readonly vars; export -n only clears the export attribute)."""
    import shlex
    import subprocess

    snap = tmp_path / "snap.sh"
    dump = _export_dump_excluding_session_vars(shlex.quote(str(snap)))
    q_snap = shlex.quote(str(snap))
    script = "set -e\n"
    script += "readonly RO_API_KEY=dummy-readonly-0123456789\n"
    script += "export RO_API_KEY\n"
    script += "export normal_ro_var=keepme\n"
    script += dump + "\n"
    script += "if grep -qE 'RO_API_KEY|dummy-readonly' " + q_snap + "; then echo 'READONLY_LEAKED' >&2; exit 2; fi\n"
    script += "if ! grep -qE '^declare -x normal_ro_var=' " + q_snap + "; then echo 'NORMAL_VAR_MISSING' >&2; exit 3; fi\n"
    result = subprocess.run(
        [_bash(), "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": os.environ.get("PATH", "")},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_export_snapshot_scrubs_credentials_under_nonwhitespace_ifs(tmp_path):
    """The scrub must survive a non-whitespace IFS (e.g. ``IFS=:``).

    Regression: ``for __v in $(compgen -e)`` splits on IFS; under ``IFS=:``
    the whole name list is one word, the case match never fires, and the
    swallowed ``export -n`` error hides the failure — every credential
    leaks.  A ``while IFS= read -r`` loop is IFS-independent.
    """
    import shlex
    import subprocess

    snap = tmp_path / "snap.sh"
    dump = _export_dump_excluding_session_vars(shlex.quote(str(snap)))
    q_snap = shlex.quote(str(snap))
    script = "set -e\n"
    script += "IFS=:\n"
    script += "export API_KEY=dummy-ifs-0123456789\n"
    script += "export db_password=dummy-ifspw-0123456789\n"
    script += "export normal_var=keepme\n"
    script += dump + "\n"
    script += "if grep -qE 'API_KEY|db_password|dummy-ifs|dummy-ifspw' " + q_snap + "; then echo 'LEAKED_UNDER_IFS' >&2; exit 2; fi\n"
    script += "if ! grep -qE '^declare -x normal_var=' " + q_snap + "; then echo 'NORMAL_VAR_MISSING' >&2; exit 3; fi\n"
    result = subprocess.run(
        [_bash(), "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": os.environ.get("PATH", "")},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
