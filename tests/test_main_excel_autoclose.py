from __future__ import annotations

from pathlib import Path

import app.main as mod


class DummyLogger:
    def __init__(self):
        self.info_messages = []
        self.error_messages = []
        self.saved_diags = []

    def info(self, message):
        self.info_messages.append(message)

    def error(self, message):
        self.error_messages.append(message)

    def save_browser_diagnostics(self, driver, name):
        self.saved_diags.append((driver, name))


class DummyProgress:
    def __init__(self):
        self.messages = []
        self.started = 0
        self.closed = 0

    def start(self):
        self.started += 1

    def update(self, message):
        self.messages.append(message)

    def close(self):
        self.closed += 1


class DummyBrowser:
    def __init__(self, _base_dir, _config):
        self.driver = None
        self.started = 0
        self.quit_count = 0

    def start(self):
        self.started += 1

    def quit(self):
        self.quit_count += 1

    def open(self, _url):
        return None

    def wait_for_page_ready(self):
        return None

    def current_handle(self):
        return "h1"

    def open_new_tab(self, _url):
        return "h2"

    def switch_to(self, _handle):
        return None


class DummyHandler:
    def __init__(self, _config, _logger, _browser):
        self.login_calls = 0

    def login(self):
        self.login_calls += 1

    def search_user(self, _alias):
        return None

    def download_certificate(self, _alias, _imei):
        return Path("x.p12")

    def upload_certificate(self, _path, _imei):
        return None

    def search_device(self, _serial):
        return None

    def associate_imei(self, _serial, _imei):
        return None


class DummyFileHandler:
    def __init__(self, _base_dir, _logger):
        return None

    def rename_to_imei(self, path, _imei):
        return path


class FakeReader:
    def __init__(self, _path, calls, *, is_open_seq=None, rows=None, read_exc=None):
        self.calls = calls
        self.is_open_seq = list(is_open_seq or [False])
        self.rows = list(rows or [])
        self.read_exc = read_exc

    def is_file_open(self):
        self.calls.append("is_file_open")
        if self.is_open_seq:
            return self.is_open_seq.pop(0)
        return False

    def read_targets(self):
        self.calls.append("read_targets")
        if self.read_exc is not None:
            raise self.read_exc
        return list(self.rows)


def _install_common(monkeypatch, tmp_path, *, save_close_result=False, save_close_exc=None, is_open_seq=None, rows=None, read_exc=None):
    excel_file = tmp_path / "targets.xlsm"
    excel_file.write_bytes(b"x")

    logger = DummyLogger()
    progress = DummyProgress()
    calls = []
    reopen_calls = []

    monkeypatch.setattr(mod, "AppLogger", lambda _base_dir: logger)
    monkeypatch.setattr(mod, "ProgressWindow", lambda: progress)
    monkeypatch.setattr(mod, "Browser", DummyBrowser)
    monkeypatch.setattr(mod, "HenngeHandler", DummyHandler)
    monkeypatch.setattr(mod, "SmsmHandler", DummyHandler)
    monkeypatch.setattr(mod, "FileHandler", DummyFileHandler)
    monkeypatch.setattr(mod, "load_config", lambda: {"excel": {"path": str(excel_file)}})
    monkeypatch.setattr(mod, "ensure_directories", lambda _cfg: None)

    reader = FakeReader(str(excel_file), calls, is_open_seq=is_open_seq, rows=rows or [], read_exc=read_exc)
    monkeypatch.setattr(mod, "ExcelReader", lambda _path: reader)

    def fake_save_close(_path, _logger):
        calls.append("save_and_close")
        if save_close_exc is not None:
            raise save_close_exc
        return save_close_result

    monkeypatch.setattr(mod, "save_and_close_target_workbook", fake_save_close)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(mod, "reopen_excel", lambda path, emit: reopen_calls.append((path, emit)))

    return logger, progress, calls, reopen_calls


def test_read_targets_called_when_file_initially_closed(monkeypatch, tmp_path):
    _logger, _progress, calls, _reopen_calls = _install_common(
        monkeypatch,
        tmp_path,
        save_close_result=False,
        is_open_seq=[False],
        rows=[],
    )

    mod.main()

    assert "save_and_close" in calls
    assert "read_targets" in calls
    assert calls.index("read_targets") > calls.index("save_and_close")


def test_read_targets_not_called_when_save_close_fails(monkeypatch, tmp_path):
    _logger, _progress, calls, reopen_calls = _install_common(
        monkeypatch,
        tmp_path,
        save_close_exc=RuntimeError("save failed"),
        is_open_seq=[False],
        rows=[],
    )

    mod.main()

    assert "save_and_close" in calls
    assert "read_targets" not in calls
    assert len(reopen_calls) == 1


def test_waits_for_unlock_before_read_targets(monkeypatch, tmp_path):
    _logger, _progress, calls, _reopen_calls = _install_common(
        monkeypatch,
        tmp_path,
        save_close_result=True,
        is_open_seq=[True, True, False],
        rows=[],
    )

    mod.main()

    assert calls.count("is_file_open") >= 2
    assert calls.index("read_targets") > calls.index("save_and_close")


def test_unlock_timeout_blocks_processing(monkeypatch, tmp_path):
    _logger, progress, calls, reopen_calls = _install_common(
        monkeypatch,
        tmp_path,
        save_close_result=True,
        is_open_seq=[True] * 100,
        rows=[],
    )

    ticks = {"v": 0.0}

    def fake_monotonic():
        ticks["v"] += 0.6
        return ticks["v"]

    monkeypatch.setattr(mod.time, "monotonic", fake_monotonic)

    mod.main()

    assert "read_targets" not in calls
    assert len(reopen_calls) == 1
    assert any("処理を中断しました" in msg for msg in progress.messages)


def test_reopen_called_once_on_normal_exit(monkeypatch, tmp_path):
    _logger, _progress, _calls, reopen_calls = _install_common(
        monkeypatch,
        tmp_path,
        save_close_result=False,
        is_open_seq=[False],
        rows=[],
    )

    mod.main()

    assert len(reopen_calls) == 1


def test_reopen_called_once_on_exception_exit(monkeypatch, tmp_path):
    logger, _progress, _calls, reopen_calls = _install_common(
        monkeypatch,
        tmp_path,
        save_close_result=False,
        is_open_seq=[False],
        rows=[],
        read_exc=TypeError("bad read"),
    )

    mod.main()

    assert len(reopen_calls) == 1
    assert any("例外型=TypeError" in msg for msg in logger.error_messages)


def test_logs_only_exception_type_for_save_close_failure(monkeypatch, tmp_path):
    logger, _progress, _calls, _reopen_calls = _install_common(
        monkeypatch,
        tmp_path,
        save_close_exc=PermissionError("C:/secret.xlsx alias=x imei=123456789012345"),
        is_open_seq=[False],
        rows=[],
    )

    mod.main()

    joined = "\n".join(logger.error_messages + logger.info_messages)
    assert "例外型=PermissionError" in joined
    assert "C:/secret.xlsx" not in joined
    assert "alias=x" not in joined
    assert "123456789012345" not in joined
