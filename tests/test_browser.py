from pathlib import Path

import pytest
from selenium.common.exceptions import WebDriverException
from urllib3.exceptions import ReadTimeoutError

from app.browser import Browser


class DummySwitchTo:
    def __init__(self, driver):
        self._driver = driver

    def new_window(self, kind: str) -> None:
        assert kind == "tab"
        new_handle = f"tab-{len(self._driver.window_handles) + 1}"
        self._driver.window_handles.append(new_handle)
        self._driver.current_window_handle = new_handle

    def window(self, handle: str) -> None:
        self._driver.current_window_handle = handle


class DummyDriver:
    def __init__(self, handles=None):
        self.window_handles = handles if handles is not None else ["tab-1"]
        self.current_window_handle = self.window_handles[0] if self.window_handles else ""
        self.current_url = ""
        self.title = "dummy"
        self.switch_to = DummySwitchTo(self)

    def get(self, url: str) -> None:
        self.current_url = url

    def quit(self) -> None:
        return None


class DummyCommandConfig:
    timeout = 120


class DiagnosticDriver(DummyDriver):
    def __init__(self, error=None):
        super().__init__()
        self.command_executor = type("Executor", (), {"_client_config": DummyCommandConfig()})()
        self.error = error
        self.quit_calls = 0

    def quit(self) -> None:
        self.quit_calls += 1
        if self.error:
            raise self.error


class DiagnosticPool:
    def __init__(self, timeout):
        self.connection_pool_kw = {"timeout": timeout}
        self.cleared = False

    def clear(self):
        self.cleared = True


class DiagnosticExecutor:
    def __init__(self):
        self._client_config = DummyCommandConfig()
        self._conn = DiagnosticPool(120)
        self.previous_connection = self._conn

    def _get_connection_manager(self):
        return DiagnosticPool(self._client_config.timeout)

    def close(self):
        self._conn.clear()


class DiagnosticRequest:
    def __init__(self):
        self.dispose_calls = 0

    def dispose(self):
        self.dispose_calls += 1


class ExecutableDiagnosticDriver(DiagnosticDriver):
    def __init__(self, execute_error=None):
        super().__init__()
        self.command_executor = DiagnosticExecutor()
        self._request = DiagnosticRequest()
        self.execute_calls = 0
        self.stop_client_calls = 0
        self.execute_error = execute_error

    def execute(self, command):
        assert command == "quit"
        self.execute_calls += 1
        if self.execute_error:
            raise self.execute_error
        return {"value": None}

    def stop_client(self):
        self.stop_client_calls += 1


@pytest.mark.parametrize("handles, should_raise", [([], True), (["tab-1"], False)])
def test_start_validates_window_handle(monkeypatch, tmp_path: Path, handles, should_raise: bool) -> None:
    dummy = DummyDriver(handles=handles)
    captured = {}

    from app import browser as browser_module

    def create_driver(options=None):
        captured["options"] = options
        return dummy

    monkeypatch.setattr(browser_module.webdriver, "Edge", create_driver)

    browser = Browser(tmp_path, {})
    if should_raise:
        with pytest.raises(RuntimeError):
            browser.start()
    else:
        browser.start()
        assert browser.current_handle() == "tab-1"
        assert captured["options"].experimental_options["prefs"]["download.default_directory"] == str((tmp_path / "downloads").resolve())


def test_tab_management_and_capture_state(monkeypatch, tmp_path: Path) -> None:
    dummy = DummyDriver(handles=["tab-1"])

    from app import browser as browser_module

    monkeypatch.setattr(browser_module.webdriver, "Edge", lambda options=None: dummy)

    browser = Browser(tmp_path, {})
    browser.start()

    first = browser.current_handle()
    second = browser.open_new_tab("https://example.com")

    assert first == "tab-1"
    assert second == "tab-2"
    assert dummy.current_url == "https://example.com"

    browser.switch_to(first)
    assert browser.current_handle() == first

    state = browser.capture_state()
    assert state["started"] is True
    assert state["handle"] == first

    with pytest.raises(RuntimeError):
        browser.switch_to("does-not-exist")


@pytest.mark.parametrize("error", [None, ConnectionRefusedError("closed"), WebDriverException("closed"), ReadTimeoutError("host", "quit", "timeout")])
def test_diagnostic_quit_is_single_best_effort_command_with_safe_stage_logs(tmp_path: Path, error) -> None:
    driver = DiagnosticDriver(error=error)
    browser = Browser(tmp_path, {})
    browser.driver = driver
    browser.started = True
    logs = []

    result = browser.quit_diagnostic(lambda key, value: logs.append((key, value)))

    assert driver.quit_calls == 1
    assert driver.command_executor._client_config.timeout == 10
    assert result["completed"] is (error is None)
    assert "browser_quit_started" in dict(logs)
    assert "browser_quit_command_started" in dict(logs)
    assert "browser_quit_cleanup_started" in dict(logs)
    assert "browser_quit_finished" in dict(logs)
    assert "browser_quit_elapsed_ms" in dict(logs)
    assert "url" not in dict(logs)
    assert "session_id" not in dict(logs)


def test_diagnostic_quit_marks_timeout_without_sleeping(tmp_path: Path) -> None:
    driver = DiagnosticDriver(error=TimeoutError("timeout"))
    browser = Browser(tmp_path, {})
    browser.driver = driver
    browser.started = True
    logs = []

    result = browser.quit_diagnostic(lambda key, value: logs.append((key, value)))

    assert result["completed"] is False
    assert result["timed_out"] is True
    assert dict(logs)["browser_quit_timed_out"] is True
    assert browser.driver is None


def test_diagnostic_quit_rebuilds_pool_with_effective_timeout(tmp_path: Path) -> None:
    driver = DiagnosticDriver()
    driver.command_executor = DiagnosticExecutor()
    browser = Browser(tmp_path, {})
    browser.driver = driver
    browser.started = True
    logs = []

    result = browser.quit_diagnostic(lambda key, value: logs.append((key, value)))

    assert result["timeout_configured"] is True
    assert result["timeout_effective"] is True
    assert dict(logs)["browser_quit_timeout_configured"] is True
    assert dict(logs)["browser_quit_timeout_effective"] is True
    assert driver.command_executor.previous_connection.cleared is True
    assert driver.quit_calls == 1


def test_diagnostic_quit_separates_http_and_cleanup_stages(tmp_path: Path) -> None:
    driver = ExecutableDiagnosticDriver()
    request = driver._request
    browser = Browser(tmp_path, {})
    browser.driver = driver
    browser.started = True
    logs = []

    result = browser.quit_diagnostic(lambda key, value: logs.append((key, value)))
    values = dict(logs)

    assert result["http_timed_out"] is False
    assert result["cleanup_slow"] is False
    assert result["total_over_limit"] is False
    assert driver.execute_calls == 1
    assert request.dispose_calls == 1
    assert driver._request is None
    assert driver.stop_client_calls == 1
    for key in (
        "browser_quit_prepare_elapsed_ms",
        "browser_quit_http_elapsed_ms",
        "browser_quit_request_dispose_elapsed_ms",
        "browser_quit_connection_close_elapsed_ms",
        "browser_quit_wrapper_cleanup_elapsed_ms",
        "browser_quit_pool_clear_elapsed_ms",
    ):
        assert isinstance(values[key], int)
    assert values["browser_quit_http_timed_out"] is False
    assert values["browser_quit_cleanup_slow"] is False
    assert values["browser_quit_total_over_limit"] is False


def test_diagnostic_quit_http_timeout_still_measures_cleanup(tmp_path: Path) -> None:
    driver = ExecutableDiagnosticDriver(execute_error=ReadTimeoutError("host", "quit", "timeout"))
    request = driver._request
    browser = Browser(tmp_path, {})
    browser.driver = driver
    browser.started = True
    logs = []

    result = browser.quit_diagnostic(lambda key, value: logs.append((key, value)))
    values = dict(logs)

    assert result["http_timed_out"] is True
    assert result["completed"] is False
    assert values["browser_quit_http_timed_out"] is True
    assert "browser_quit_request_dispose_elapsed_ms" in values
    assert "browser_quit_connection_close_elapsed_ms" in values
    assert request.dispose_calls == 1
    assert driver.execute_calls == 1
