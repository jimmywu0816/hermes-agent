"""WO-D-2026-09-14-019-04 C: ``hermes send`` receipt carries ts + channel.

The 2026-09-14 duplicate-send incident's root enabler: the CLI printed a bare ``sent`` with no
message id, so an agent could not self-verify what it had just delivered. The receipt now carries
``ts=<message_id>`` and ``channel=<target>`` (and JSON mode gains a ``channel`` key).
"""
import json

import pytest

from hermes_cli.send_cmd import _emit_result


def _success(message_id="1789397526.225269"):
    return json.dumps({"success": True, "message_id": message_id})


def test_success_receipt_has_ts_and_channel(capsys):
    code = _emit_result(_success(), json_mode=False, quiet=False, channel_hint="slack:C0BUCJ2SJGK:1789.088")
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == "sent ts=1789397526.225269 channel=slack:C0BUCJ2SJGK:1789.088"


def test_success_without_message_id_still_names_channel(capsys):
    code = _emit_result(json.dumps({"success": True}), json_mode=False, quiet=False, channel_hint="telegram:-100")
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == "sent channel=telegram:-100"


def test_json_mode_payload_gains_channel(capsys):
    code = _emit_result(_success(), json_mode=True, quiet=False, channel_hint="slack:C1")
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["channel"] == "slack:C1"
    assert out["message_id"] == "1789397526.225269"
    # an explicit channel from the tool result must not be overwritten
    _emit_result(json.dumps({"success": True, "channel": "native"}), json_mode=True, quiet=False,
                 channel_hint="slack:C1")
    assert json.loads(capsys.readouterr().out)["channel"] == "native"


def test_skipped_receipt_prints_note(capsys):
    payload = json.dumps({"success": True, "skipped": True, "reason": "origin_auto_delivery_duplicate_target",
                          "note": "Skipped send_message to slack:C1."})
    code = _emit_result(payload, json_mode=False, quiet=False, channel_hint="slack:C1")
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == "Skipped send_message to slack:C1."


def test_skipped_without_note_names_reason(capsys):
    code = _emit_result(json.dumps({"success": True, "skipped": True, "reason": "cron_auto_delivery_duplicate_target"}),
                        json_mode=False, quiet=False, channel_hint=None)
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == "skipped (cron_auto_delivery_duplicate_target)"


def test_error_still_goes_to_stderr(capsys):
    code = _emit_result(json.dumps({"error": "boom"}), json_mode=False, quiet=False, channel_hint="slack:C1")
    captured = capsys.readouterr()
    assert code != 0
    assert "boom" in captured.err
    assert captured.out.strip() == ""


def test_quiet_mode_prints_nothing(capsys):
    code = _emit_result(_success(), json_mode=False, quiet=True, channel_hint="slack:C1")
    assert code == 0
    assert capsys.readouterr().out.strip() == ""


def test_unknown_shape_still_fails(capsys):
    code = _emit_result(json.dumps({"weird": 1}), json_mode=False, quiet=False, channel_hint=None)
    assert code != 0
    assert capsys.readouterr().out.strip() != ""  # dumped verbatim


@pytest.mark.parametrize("payload,expected_exit", [
    (json.dumps({"success": True}), 0),
    (json.dumps({"success": True, "skipped": True}), 0),
    (json.dumps({"error": "x"}), 1),
    ("", 1),
])
def test_exit_code_contract_unchanged(payload, expected_exit, capsys):
    assert _emit_result(payload, json_mode=True, quiet=False) == expected_exit
