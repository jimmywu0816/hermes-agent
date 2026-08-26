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
``tools/environments/base.py``; they are re-injected fresh on every command.
"""

import os
import re
import sys

import pytest

from tools.environments.base import (
    _SNAPSHOT_EXCLUDED_ENV_REGEX,
    _export_dump_excluding_session_vars,
)


def _bash() -> str:
    return "/bin/bash" if os.path.exists("/bin/bash") else "bash"


# ---------------------------------------------------------------------------
# Unit: the exclusion regex matches exactly the bridged vars, nothing else.
# ---------------------------------------------------------------------------

def test_regex_matches_bridged_session_vars():
    rx = re.compile(_SNAPSHOT_EXCLUDED_ENV_REGEX)
    # Every var the gateway bridges must be excluded.
    from gateway.session_context import _VAR_MAP

    for name in _VAR_MAP:
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


# ---------------------------------------------------------------------------
# Credential scrub: env vars whose NAME contains credential markers
# (KEY/TOKEN/SECRET/PASSWORD/PASSWD/CREDENTIAL, any case) must not land in
# the snapshot dump.
# ---------------------------------------------------------------------------

def test_export_snippet_contains_case_insensitive_credential_scrub():
    snippet = _export_dump_excluding_session_vars('"$__hermes_snap_tmp"')
    # The loop uppercases each var name before the case match, so a single
    # uppercase pattern set covers every casing variant.
    assert "compgen -e" in snippet
    assert "${__v^^}" in snippet
    assert 'case "${__v^^}" in' in snippet
    for marker in ("*KEY*", "*TOKEN*", "*SECRET*", "*PASSWORD*", "*PASSWD*", "*CREDENTIAL*"):
        assert marker in snippet, f"{marker} should be in the scrub pattern"
    # Lowercase-only patterns are gone: the case fold replaces them.
    assert "*key*" not in snippet
    assert "*password*" not in snippet


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash snapshot path")
def test_export_snapshot_scrubs_credential_names_any_case(tmp_path):
    import shlex
    import subprocess

    snap = tmp_path / "snap.sh"
    dump = _export_dump_excluding_session_vars(shlex.quote(str(snap)))
    q_snap = shlex.quote(str(snap))
    script = "set -e\n"
    script += "export db_password=leak1\n"
    script += "export ApiKey=leak2\n"
    script += "export MY_SECRET=leak3\n"
    script += "export ACCESS_TOKEN=leak4\n"
    script += "export normal_var=keepme\n"
    script += "export APPLE=keepme\n"
    script += dump + "\n"
    script += "if grep -qE 'db_password|ApiKey|MY_SECRET|ACCESS_TOKEN' " + q_snap + "; then echo 'LEAKED_INTO_SNAPSHOT' >&2; exit 2; fi\n"
    script += "source " + q_snap + "\n"
    script += "if grep -qE 'db_password|ApiKey|MY_SECRET|ACCESS_TOKEN' " + q_snap + "; then echo 'LEAKED_INTO_SNAPSHOT' >&2; exit 2; fi\n"
    script += "if ! grep -qE '^declare -x normal_var=' " + q_snap + "; then echo 'NORMAL_VAR_MISSING' >&2; exit 3; fi\n"
    script += "if ! grep -qE '^declare -x APPLE=' " + q_snap + "; then echo 'APPLE_MISSING' >&2; exit 4; fi\n"
    result = subprocess.run(
        [_bash(), "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": os.environ.get("PATH", "")},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


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
