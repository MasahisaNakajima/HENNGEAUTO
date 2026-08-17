from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import diagnose_hennge_single_target_lookup as mod


class DummyLogger:
    def __init__(self):
        self.info_messages = []
        self.error_messages = []

    def info(self, message):
        self.info_messages.append(message)

    def error(self, message):
        self.error_messages.append(message)


class FakeReader:
    def __init__(self, rows, calls, is_open=False):
        self.rows = rows
        self.calls = calls
        self.open = is_open

    def is_file_open(self):
        self.calls.append("is_open")
        return self.open

    def read_targets(self):
        self.calls.append("read")
        return list(self.rows)


class FakeBrowser:
    def __init__(self, calls, quit_error=None):
        self.calls = calls
        self.driver = object()
        self.quit_error = quit_error

    def start(self):
        self.calls.append("browser")

    def quit(self):
        self.calls.append("quit")
        if self.quit_error:
            raise self.quit_error


class FakeHandler:
    last_target = None

    def __init__(self, _config, _logger, _browser):
        self.calls = _browser.calls

    def login(self):
        self.calls.append("login")

    def search_user(self, alias):
        self.calls.append("lookup")
        FakeHandler.last_target = alias

    def download_certificate(self, *_args):
        raise AssertionError("certificate operation must not be called")



def install(monkeypatch, *, rows, result_count=1, is_open=False, detection_match=True, save_error=None, unlock_error=None, quit_error=None):
    logger = DummyLogger()
    calls = []
    reader = FakeReader(rows, calls, is_open=is_open)
    browser = FakeBrowser(calls, quit_error=quit_error)
    monkeypatch.setattr(mod, "AppLogger", lambda _base_dir: logger)
    monkeypatch.setattr(mod, "load_config", lambda: {"excel": {"path": "C:/private/target.xlsm"}})
    monkeypatch.setattr(mod, "ExcelReader", lambda _path: reader)
    monkeypatch.setattr(mod.Path, "exists", lambda _self: True)
    monkeypatch.setattr(mod, "Browser", lambda _base_dir, _config: browser)
    monkeypatch.setattr(mod, "HenngeHandler", FakeHandler)
    monkeypatch.setattr(mod, "_resolve_web_identity_hash", lambda *_args: SimpleNamespace(hash_value="a" * 64, source="environment", valid=True))

    def detect(*_args, **kwargs):
        calls.append("detect")
        assert kwargs["web_identity_mode"] == "test"
        return SimpleNamespace(
            workbook=object() if detection_match else None,
            application=object() if detection_match else None,
            matched_workbook_count=1 if detection_match else 0,
            target_match_method="web_identity" if detection_match else "none",
        )

    def save_close(*_args, **_kwargs):
        calls.append("save_close")
        if save_error:
            raise save_error
        return True

    def wait_unlock(*_args, **_kwargs):
        calls.append("unlock")
        if unlock_error:
            raise unlock_error

    monkeypatch.setattr(mod, "detect_target_workbook", detect)
    monkeypatch.setattr(mod, "save_and_close_target_workbook", save_close)
    monkeypatch.setattr(mod, "_wait_unlock", wait_unlock)
    monkeypatch.setattr(mod, "_wait_results_ready", lambda _browser: result_count)
    monkeypatch.setattr(mod, "reopen_excel", lambda _path, _emit: calls.append("reopen"))
    return logger, calls, browser


def joined(logger):
    return "\n".join(logger.info_messages + logger.error_messages)


def test_one_result_is_success_and_ordered(monkeypatch):
    logger, calls, _browser = install(
        monkeypatch,
        rows=[{"alias": "secret_alias", "serial": "secret_serial", "imei": "123456789012345"}],
        is_open=True,
    )

    assert mod.main([]) == 0
    assert calls == ["is_open", "detect", "save_close", "unlock", "read", "browser", "login", "lookup", "quit", "reopen"]
    output = joined(logger)
    for expected in (
        "lookup_mode=read_only",
        "selected_target_count=1",
        "alias_present=True",
        "browser_started=True",
        "hennge_login_completed=True",
        "lookup_called=True",
        "lookup_result_count=1",
        "lookup_unique=True",
        "certificate_action_called=False",
        "smsm_action_called=False",
        "excel_write_called=False",
    ):
        assert expected in output
    assert "secret_alias" not in output and "secret_serial" not in output and "123456789012345" not in output


@pytest.mark.parametrize("result_count, expected_code", [(0, 22), (1, 0), (2, 23), (5, 23)])
def test_result_count_classification(monkeypatch, result_count, expected_code):
    logger, calls, _browser = install(
        monkeypatch,
        rows=[{"alias": "alias_secret", "serial": "serial_secret", "imei": "123456789012345"}],
        result_count=result_count,
    )

    assert mod.main([]) == expected_code
    assert calls[-1] == "quit"
    assert f"lookup_result_count={result_count}" in joined(logger)


def test_only_targets_zero_is_selected_and_pair_is_preserved(monkeypatch):
    logger, _calls, _browser = install(
        monkeypatch,
        rows=[
            {"alias": "first_alias_secret", "serial": "first_serial_secret", "imei": "123456789012345"},
            {"alias": "second_alias_secret", "serial": "second_serial_secret", "imei": "223456789012345"},
        ],
    )

    assert mod.main([]) == 0
    assert FakeHandler.last_target == "first_alias_secret"
    assert "selected_target_count=1" in joined(logger)


def test_zero_targets_returns_two_without_browser(monkeypatch):
    logger, calls, _browser = install(monkeypatch, rows=[])
    assert mod.main([]) == 2
    assert "browser" not in calls and "lookup" not in calls
    assert "selected_target_count=0" in joined(logger)


def test_search_user_receives_alias_from_same_dictionary(monkeypatch):
    _logger, _calls, _browser = install(
        monkeypatch,
        rows=[{"alias": "paired_alias", "serial": "paired_serial", "imei": "123456789012345"}],
    )
    observed = []
    original = FakeHandler.search_user

    def capture(self, alias):
        observed.append(alias)
        return original(self, alias)

    monkeypatch.setattr(FakeHandler, "search_user", capture)
    assert mod.main([]) == 0
    assert observed == ["paired_alias"]


def test_save_failure_blocks_read_and_browser(monkeypatch):
    _logger, calls, _browser = install(
        monkeypatch,
        rows=[{"alias": "a", "serial": "s", "imei": "123456789012345"}],
        is_open=True,
        save_error=mod.SaveCloseWorkbookError("private"),
    )
    assert mod.main([]) == 12
    assert "read" not in calls and "browser" not in calls and "reopen" not in calls


def test_unlock_failure_blocks_read_and_browser(monkeypatch):
    _logger, calls, _browser = install(
        monkeypatch,
        rows=[{"alias": "a", "serial": "s", "imei": "123456789012345"}],
        is_open=True,
        unlock_error=mod.UnlockTimeoutError("private"),
    )
    assert mod.main([]) == 13
    assert "read" not in calls and "browser" not in calls
    assert calls.count("reopen") == 1


def test_detection_failure_blocks_save_read_and_browser(monkeypatch):
    _logger, calls, _browser = install(monkeypatch, rows=[], is_open=True, detection_match=False)
    assert mod.main([]) == 9
    assert all(name not in calls for name in ("save_close", "read", "browser", "reopen"))


def test_browser_start_failure_returns_24(monkeypatch):
    logger, calls, browser = install(monkeypatch, rows=[{"alias": "a", "serial": "s", "imei": "123456789012345"}])
    def fail_start():
        calls.append("browser")
        raise RuntimeError("private")
    browser.start = fail_start
    assert mod.main([]) == 24
    assert "login" not in calls
    assert "private" not in joined(logger)


def test_browser_quit_failure_returns_25(monkeypatch):
    _logger, _calls, _browser = install(
        monkeypatch,
        rows=[{"alias": "a", "serial": "s", "imei": "123456789012345"}],
        quit_error=RuntimeError("private"),
    )
    assert mod.main([]) == 25


def test_login_failure_returns_20(monkeypatch):
    logger, calls, _browser = install(monkeypatch, rows=[{"alias": "a", "serial": "s", "imei": "123456789012345"}])
    original = FakeHandler.login

    def fail_login(self):
        self.calls.append("login")
        raise RuntimeError("private")

    monkeypatch.setattr(FakeHandler, "login", fail_login)
    assert mod.main([]) == 20
    assert "lookup" not in calls
    assert "private" not in joined(logger)


def test_forbidden_operations_are_not_referenced_or_called():
    source = Path(mod.__file__).read_text(encoding="utf-8")
    for forbidden in ("download_certificate", "SmsmHandler", "upload_certificate", "associate_imei", "Application.Quit", "taskkill", "Stop-Process", "os.startfile"):
        assert forbidden not in source
