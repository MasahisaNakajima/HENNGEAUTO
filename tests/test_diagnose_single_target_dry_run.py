from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import diagnose_single_target_dry_run as mod


class DummyLogger:
    def __init__(self):
        self.info_messages = []
        self.error_messages = []

    def info(self, message):
        self.info_messages.append(message)

    def error(self, message):
        self.error_messages.append(message)


class FakeReader:
    def __init__(self, file_path, rows, calls, *, is_open=False):
        self.file_path = file_path
        self.rows = rows
        self.calls = calls
        self.open = is_open

    def is_file_open(self):
        self.calls.append("is_open_check")
        return self.open

    def read_targets(self):
        self.calls.append("read")
        return list(self.rows)


def install(monkeypatch, *, rows, is_open=False, detection_match=True, save_result=True, save_exc=None, unlock_exc=None):
    logger = DummyLogger()
    calls = []
    reader = FakeReader("C:/private/targets.xlsm", rows, calls, is_open=is_open)
    monkeypatch.setattr(mod, "AppLogger", lambda _base_dir: logger)
    monkeypatch.setattr(mod, "load_config", lambda: {"excel": {"path": "C:/private/targets.xlsm"}})
    monkeypatch.setattr(mod, "ExcelReader", lambda _path: reader)
    monkeypatch.setattr(mod.Path, "exists", lambda _self: True)
    monkeypatch.setattr(mod, "_resolve_web_identity_hash", lambda *_args: SimpleNamespace(hash_value="a" * 64, source="environment", valid=True))

    def detect(*_args, **_kwargs):
        calls.append("detect")
        return SimpleNamespace(
            workbook=object() if detection_match else None,
            application=object() if detection_match else None,
            matched_workbook_count=1 if detection_match else 0,
            target_match_method="web_identity" if detection_match else "none",
        )

    def save_close(*_args, **_kwargs):
        calls.append("save_close")
        if save_exc:
            raise save_exc
        return save_result

    def wait(*_args, **_kwargs):
        calls.append("unlock")
        if unlock_exc:
            raise unlock_exc

    def reopen(_path, _emit):
        calls.append("reopen")

    monkeypatch.setattr(mod, "detect_target_workbook", detect)
    monkeypatch.setattr(mod, "save_and_close_target_workbook", save_close)
    monkeypatch.setattr(mod, "_wait_unlock", wait)
    monkeypatch.setattr(mod, "reopen_excel", reopen)
    return logger, calls, reader


def joined(logger):
    return "\n".join(logger.info_messages + logger.error_messages)


def test_one_target_success_and_fixed_planned_stages(monkeypatch):
    logger, calls, _reader = install(
        monkeypatch,
        rows=[{"alias": "alias01", "serial": "serial01", "imei": "123456789012345"}],
    )

    assert mod.main([]) == 0
    output = joined(logger)
    assert "selected_target_count=1" in output
    assert "alias_present=True" in output
    assert "serial_present=True" in output
    assert "imei_valid=True" in output
    assert "dry_run=True" in output
    assert "planned_stage_count=8" in output
    assert "external_action_called=False" in output
    assert "alias01" not in output and "serial01" not in output and "123456789012345" not in output


def test_multiple_targets_selects_targets_zero_without_merging(monkeypatch):
    logger, _calls, _reader = install(
        monkeypatch,
        rows=[
            {"alias": "first-alias", "serial": "first-serial", "imei": "123456789012345"},
            {"alias": "second-alias", "serial": "second-serial", "imei": "223456789012345"},
        ],
    )

    assert mod.main([]) == 0
    assert "selected_target_count=1" in joined(logger)


def test_planned_stages_are_fixed_and_ordered():
    assert mod.PLANNED_STAGES == (
        "target_loaded",
        "target_validated",
        "hennge_lookup_planned",
        "certificate_download_planned",
        "filename_normalization_planned",
        "smsm_lookup_planned",
        "certificate_upload_planned",
        "result_recording_planned",
    )


def test_zero_targets_returns_two(monkeypatch):
    logger, _calls, _reader = install(monkeypatch, rows=[])
    assert mod.main([]) == 2
    assert "selected_target_count=0" in joined(logger)


def test_common_normalizer_is_used(monkeypatch):
    logger, _calls, _reader = install(
        monkeypatch,
        rows=[{"alias": "a", "serial": "s", "imei": "input"}],
    )
    observed = []

    def normalize(value):
        observed.append(value)
        return "123456789012345"

    monkeypatch.setattr(mod, "normalize_imei", normalize)
    assert mod.main([]) == 0
    assert observed == ["input"]
    assert "imei_valid=True" in joined(logger)


@pytest.mark.parametrize("is_open", [True, False])
def test_read_order_depends_on_initial_excel_state(monkeypatch, is_open):
    logger, calls, _reader = install(
        monkeypatch,
        rows=[{"alias": "a", "serial": "s", "imei": "123456789012345"}],
        is_open=is_open,
    )
    assert mod.main([]) == 0
    if is_open:
        assert calls == ["is_open_check", "detect", "save_close", "unlock", "read", "reopen"]
    else:
        assert calls == ["is_open_check", "read"]
    assert "external_action_called=False" in joined(logger)


def test_save_failure_does_not_read(monkeypatch):
    logger, calls, _reader = install(
        monkeypatch,
        rows=[{"alias": "a", "serial": "s", "imei": "123456789012345"}],
        is_open=True,
        save_exc=mod.SaveCloseWorkbookError("private"),
    )
    assert mod.main([]) == 12
    assert "read" not in calls and "reopen" not in calls
    assert "exception_type=SaveCloseWorkbookError" in joined(logger)


def test_close_failure_does_not_read(monkeypatch):
    logger, calls, _reader = install(
        monkeypatch,
        rows=[{"alias": "a", "serial": "s", "imei": "123456789012345"}],
        is_open=True,
        save_exc=mod.SaveCloseWorkbookError("private"),
    )
    assert mod.main([]) == 12
    assert calls.count("read") == 0


def test_unlock_failure_does_not_read(monkeypatch):
    logger, calls, _reader = install(
        monkeypatch,
        rows=[{"alias": "a", "serial": "s", "imei": "123456789012345"}],
        is_open=True,
        unlock_exc=mod.UnlockTimeoutError("private"),
    )
    assert mod.main([]) == 13
    assert "read" not in calls and "reopen" in calls


def test_detection_failure_does_not_save_close_or_read(monkeypatch):
    logger, calls, _reader = install(
        monkeypatch,
        rows=[],
        is_open=True,
        detection_match=False,
    )
    assert mod.main([]) == 9
    assert all(name not in calls for name in ("save_close", "read", "reopen"))
    assert "matched_workbook_count=0" in joined(logger)


def test_reopen_is_called_at_most_once(monkeypatch):
    _logger, calls, _reader = install(
        monkeypatch,
        rows=[{"alias": "a", "serial": "s", "imei": "123456789012345"}],
        is_open=True,
    )
    assert mod.main([]) == 0
    assert calls.count("reopen") == 1


def test_missing_required_and_invalid_imei_codes(monkeypatch):
    logger, _calls, _reader = install(monkeypatch, rows=[{"alias": "secret_alias_value", "serial": "", "imei": "123456789012345"}])
    assert mod.main([]) == 10
    assert "secret_alias_value" not in joined(logger)

    logger, _calls, _reader = install(monkeypatch, rows=[{"alias": "secret_alias_value", "serial": "secret_serial_value", "imei": "bad"}])
    assert mod.main([]) == 5


def test_no_external_modules_or_file_operations_are_used():
    source = Path(mod.__file__).read_text(encoding="utf-8")
    for forbidden in ("Browser(", "HenngeHandler(", "SmsmHandler(", "selenium", "Application.Quit", "taskkill", "Stop-Process", "os.startfile"):
        assert forbidden not in source
