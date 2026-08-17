from __future__ import annotations

from pathlib import Path

import pytest
from selenium.webdriver.common.by import By

import diagnose_hennge_certificate_download as mod


class Node:
    def __init__(self, *, tag="div", attrs=None, text="", displayed=True, enabled=True, children=None, parent=None):
        self.tag_name = tag
        self._attrs = attrs or {}
        self.text = text
        self._displayed = displayed
        self._enabled = enabled
        self.children = []
        self.parent = parent
        self.click_count = 0
        if children:
            for child in children:
                self.add_child(child)

    def add_child(self, child):
        child.parent = self
        self.children.append(child)

    def is_displayed(self):
        return self._displayed

    def is_enabled(self):
        return self._enabled

    def get_attribute(self, name):
        return self._attrs.get(name)

    def click(self):
        self.click_count += 1

    def _walk_descendants(self):
        stack = list(self.children)
        while stack:
            node = stack.pop(0)
            yield node
            stack[0:0] = node.children

    def _matches(self, selector):
        if selector == "a":
            return self.tag_name == "a"
        if selector == "button":
            return self.tag_name == "button"
        if selector == "input":
            return self.tag_name == "input"
        if selector == "table tbody tr":
            return self.tag_name == "tr"
        if selector == "[role='dialog']":
            return (self.get_attribute("role") or "") == "dialog"
        if selector == "[class*='drawer']":
            return "drawer" in (self.get_attribute("class") or "")
        if selector == "[data-testid*='drawer']":
            return "drawer" in (self.get_attribute("data-testid") or "")
        if selector == "[aria-modal='true']":
            return (self.get_attribute("aria-modal") or "") == "true"
        if selector == "button[aria-label='Close']":
            return self.tag_name == "button" and (self.get_attribute("aria-label") or "") == "Close"
        if selector == "input[name='note']":
            return self.tag_name == "input" and (self.get_attribute("name") or "") == "note"
        if selector == "button[data-testid='send-installation-email-toolbar']":
            return self.tag_name == "button" and (self.get_attribute("data-testid") or "") == "send-installation-email-toolbar"
        return False

    def find_elements(self, by, selector):
        if by == By.XPATH and selector == "ancestor-or-self::*":
            chain = []
            cur = self
            while cur is not None:
                chain.append(cur)
                cur = cur.parent
            return list(reversed(chain))

        if by == By.CSS_SELECTOR:
            return [node for node in self._walk_descendants() if node._matches(selector)]

        return []


class Driver:
    def __init__(self, *, root, rows, current_url="https://admin.auth.hennge.com/certificates/"):
        self.root = root
        self.rows = rows
        self.current_url = current_url
        self.execute_script_called = 0
        self.cdp_calls = []

    def find_elements(self, by, selector):
        if by == By.CSS_SELECTOR and selector == "table tbody tr":
            return self.rows
        return self.root.find_elements(by, selector)

    def execute_script(self, *_args, **_kwargs):
        self.execute_script_called += 1
        raise AssertionError("execute_script must not be used")

    def execute_cdp_cmd(self, command, payload):
        self.cdp_calls.append((command, payload))
        return {}


class DummyBrowser:
    def __init__(self, _base_dir, _config, driver=None):
        self.driver = driver
        self.started = False
        self.start_count = 0
        self.open_calls = []
        self.wait_calls = []
        self.quit_count = 0

    def start(self):
        self.started = True
        self.start_count += 1

    def open(self, url):
        self.open_calls.append(url)

    def wait_for_page_ready(self, timeout=20):
        self.wait_calls.append(timeout)
        return None

    def quit(self):
        self.quit_count += 1


class DummyHandler:
    def __init__(self, _config, _logger, _browser):
        return None

    def login(self):
        return None


class DummyLogger:
    def __init__(self):
        self.info_messages = []
        self.error_messages = []
        self.exception_messages = []
        self.save_calls = []

    def info(self, message):
        self.info_messages.append(message)

    def error(self, message):
        self.error_messages.append(message)

    def exception(self, message):
        self.exception_messages.append(message)

    def save_browser_diagnostics(self, driver, name, save_html=True):
        self.save_calls.append((driver, name, save_html))


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class Timeline:
    def __init__(self, clock):
        self.clock = clock
        self.events = []

    def schedule(self, at_seconds, callback):
        self.events.append((at_seconds, callback))
        self.events.sort(key=lambda item: item[0])

    def sleep(self, seconds):
        self.clock.now += seconds
        ready = [item for item in self.events if item[0] <= self.clock.now]
        self.events = [item for item in self.events if item[0] > self.clock.now]
        for _, callback in ready:
            callback()


def _make_row(*, children=None):
    return Node(tag="tr", attrs={"class": "certificate-row"}, children=children or [])


def _make_detail_panel(*, actions=None):
    panel = Node(tag="section", attrs={"role": "dialog", "class": "detail-panel"})
    panel.add_child(Node(tag="button", attrs={"aria-label": "Close"}, text="Close"))
    panel.add_child(Node(tag="input", attrs={"name": "note"}))
    panel.add_child(Node(tag="button", attrs={"data-testid": "send-installation-email-toolbar"}, text="Send installation email"))
    for action in actions or []:
        panel.add_child(action)
    return panel


def _install_main_mocks(monkeypatch, *, base_dir, result_count=1, rows=None, detail_state=None, download_wait_result=None):
    logger = DummyLogger()
    row_list = rows if rows is not None else [_make_row()]
    detail_panel = detail_state["detail_area"] if detail_state else _make_detail_panel()
    driver = Driver(root=detail_panel, rows=row_list)
    browser = DummyBrowser(base_dir, None, driver=driver)
    search_input = Node(tag="input", attrs={"name": "query", "aria-label": "Search"})
    calls = {"wait_input": 0, "submit": 0, "wait_results": 0, "row_click": 0, "download_click": 0}

    monkeypatch.setattr(mod, "_base_dir", lambda: base_dir)
    monkeypatch.setattr(mod, "load_config", lambda: {})
    monkeypatch.setattr(mod, "ensure_directories", lambda _config: None)
    monkeypatch.setattr(mod, "AppLogger", lambda _base_dir: logger)
    monkeypatch.setattr(mod, "Browser", lambda _base_dir, _config: browser)
    monkeypatch.setattr(mod, "HenngeHandler", DummyHandler)

    def fake_wait_input(_driver, _logger, timeout_seconds=20):
        _ = timeout_seconds
        calls["wait_input"] += 1
        return search_input

    def fake_submit(search_input_arg, alias_arg, logger_arg):
        _ = (search_input_arg, alias_arg, logger_arg)
        calls["submit"] += 1

    def fake_wait_results(_browser, timeout_seconds=15):
        _ = timeout_seconds
        calls["wait_results"] += 1
        return result_count

    def fake_log_result_state(_logger, _count):
        return None

    def fake_wait_detail(_driver, _before_path, _logger, timeout_seconds=20):
        _ = timeout_seconds
        if detail_state is not None:
            return detail_state
        return {
            "path_changed": False,
            "has_heading": True,
            "has_surface": True,
            "detail_area": detail_panel,
            "detail_method": mod.cert_detail.DETAIL_METHOD_DIALOG,
            "close_count": 1,
            "note_count": 1,
            "mail_toolbar_count": 1,
            "dialog_count": 1,
            "detail_area_found": True,
        }

    def fake_download_wait(download_dir, before_names, logger_arg, timeout_seconds=60):
        _ = (download_dir, before_names, logger_arg, timeout_seconds)
        if isinstance(download_wait_result, Exception):
            raise download_wait_result
        return download_wait_result or base_dir / "downloads" / "hennge_download_diagnostic" / "cert.p12"

    monkeypatch.setattr(mod.cert_search, "_wait_certificate_search_input_ready", fake_wait_input)
    monkeypatch.setattr(mod.cert_search, "_set_query_and_submit", fake_submit)
    monkeypatch.setattr(mod.cert_search, "_wait_results_ready", fake_wait_results)
    monkeypatch.setattr(mod.cert_search, "_log_result_state", fake_log_result_state)
    monkeypatch.setattr(mod.cert_search, "_find_single_visible_search_input", lambda _driver, _selector: search_input)
    monkeypatch.setattr(mod.cert_detail, "_wait_detail_ready", fake_wait_detail)
    monkeypatch.setattr(mod, "_wait_for_single_download_file", fake_download_wait)

    original_row_click = Node.click
    original_download_click = Node.click

    def counted_click(self):
        if self in row_list:
            calls["row_click"] += 1
        if self.tag_name == "button" and (self.get_attribute("title") or "").lower().startswith("download"):
            calls["download_click"] += 1
        return original_row_click(self)

    monkeypatch.setattr(Node, "click", counted_click)
    return logger, browser, driver, calls, detail_panel


def test_main_downloads_single_candidate_once(monkeypatch, tmp_path):
    base_dir = tmp_path
    download_action = Node(tag="button", attrs={"id": "react-aria-123", "type": "button", "title": "Download"}, text="")
    detail_panel = _make_detail_panel(actions=[download_action])
    logger, browser, driver, calls, _panel = _install_main_mocks(
        monkeypatch,
        base_dir=base_dir,
        result_count=1,
        rows=[_make_row()],
        detail_state={
            "path_changed": False,
            "has_heading": True,
            "has_surface": True,
            "detail_area": detail_panel,
            "detail_method": mod.cert_detail.DETAIL_METHOD_DIALOG,
            "close_count": 1,
            "note_count": 1,
            "mail_toolbar_count": 1,
            "dialog_count": 1,
            "detail_area_found": True,
        },
        download_wait_result=base_dir / "downloads" / "hennge_download_diagnostic" / "certificate.p12",
    )

    download_dir = base_dir / "downloads" / "hennge_download_diagnostic"
    download_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("open must not be used")), raising=False)
    monkeypatch.setattr(Path, "rename", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("rename must not be used")), raising=False)
    monkeypatch.setattr(Path, "unlink", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unlink must not be used")), raising=False)

    rc = mod.main(["SECRET_ALIAS"])

    assert rc == 0
    assert browser.start_count == 1
    assert browser.quit_count == 1
    assert calls["wait_input"] == 1
    assert calls["submit"] == 1
    assert calls["wait_results"] == 1
    assert calls["row_click"] == 1
    assert calls["download_click"] == 1
    assert driver.execute_script_called == 0
    assert any(command == "Browser.setDownloadBehavior" for command, _ in driver.cdp_calls)
    joined = "\n".join(logger.info_messages + logger.error_messages + logger.exception_messages)
    assert "SECRET_ALIAS" not in joined
    assert "user@example.com" not in joined
    assert "certificate.p12" not in joined
    assert logger.save_calls == []


@pytest.mark.parametrize("result_count, expected_rc", [(0, 2), (2, 3)])
def test_main_stops_without_clicking_when_search_result_count_is_not_one(monkeypatch, tmp_path, result_count, expected_rc):
    base_dir = tmp_path
    logger, browser, _driver, calls, _panel = _install_main_mocks(
        monkeypatch,
        base_dir=base_dir,
        result_count=result_count,
        rows=[_make_row()],
    )

    rc = mod.main(["SECRET_ALIAS"])

    assert rc == expected_rc
    assert calls["row_click"] == 0
    assert calls["download_click"] == 0
    assert browser.quit_count == 1
    assert logger.save_calls == []


def test_main_stops_when_visible_rows_are_not_single(monkeypatch, tmp_path):
    base_dir = tmp_path
    row_one = _make_row()
    row_two = _make_row()
    logger, browser, _driver, calls, _panel = _install_main_mocks(
        monkeypatch,
        base_dir=base_dir,
        result_count=1,
        rows=[row_one, row_two],
    )

    rc = mod.main(["SECRET_ALIAS"])

    assert rc == 4
    assert calls["row_click"] == 0
    assert calls["download_click"] == 0
    assert browser.quit_count == 1


@pytest.mark.parametrize(
    "action, expected_rc",
    [
        (Node(tag="button", attrs={"data-testid": "send-installation-email-toolbar"}, text="Download"), 6),
        (Node(tag="button", attrs={"data-testid": "revoke-toolbar"}, text="Download"), 6),
        (Node(tag="button", attrs={"aria-label": "Close"}, text="Download"), 6),
        (Node(tag="button", attrs={"data-testid": "save-toolbar"}, text="Download"), 6),
        (Node(tag="button", attrs={"data-testid": "cancel-toolbar"}, text="Download"), 6),
        (Node(tag="button", attrs={"id": "unknown-action"}, text="Open"), 6),
        (Node(tag="button", attrs={"type": "submit"}, text="Download"), 6),
    ],
)
def test_main_never_clicks_non_download_categories(monkeypatch, tmp_path, action, expected_rc):
    base_dir = tmp_path
    detail_panel = _make_detail_panel(actions=[action])
    logger, browser, _driver, calls, _panel = _install_main_mocks(
        monkeypatch,
        base_dir=base_dir,
        result_count=1,
        rows=[_make_row()],
        detail_state={
            "path_changed": False,
            "has_heading": True,
            "has_surface": True,
            "detail_area": detail_panel,
            "detail_method": mod.cert_detail.DETAIL_METHOD_DIALOG,
            "close_count": 1,
            "note_count": 1,
            "mail_toolbar_count": 1,
            "dialog_count": 1,
            "detail_area_found": True,
        },
    )

    rc = mod.main(["SECRET_ALIAS"])

    assert rc == expected_rc
    assert calls["row_click"] == 1
    assert calls["download_click"] == 0
    assert browser.quit_count == 1
    assert logger.save_calls == []


def test_main_rejects_non_dialog_detail_area(monkeypatch, tmp_path):
    base_dir = tmp_path
    detail_panel = _make_detail_panel(actions=[Node(tag="button", attrs={"title": "Download"}, text="Download")])
    logger, browser, _driver, calls, _panel = _install_main_mocks(
        monkeypatch,
        base_dir=base_dir,
        result_count=1,
        rows=[_make_row()],
        detail_state={
            "path_changed": False,
            "has_heading": True,
            "has_surface": True,
            "detail_area": detail_panel,
            "detail_method": mod.cert_detail.DETAIL_METHOD_DRAWER,
            "close_count": 1,
            "note_count": 1,
            "mail_toolbar_count": 1,
            "dialog_count": 0,
            "detail_area_found": True,
        },
    )

    rc = mod.main(["SECRET_ALIAS"])

    assert rc == 5
    assert calls["row_click"] == 1
    assert calls["download_click"] == 0
    assert browser.quit_count == 1
    assert logger.save_calls == []


def test_main_rejects_non_empty_download_folder_before_browser_start(monkeypatch, tmp_path):
    base_dir = tmp_path
    download_dir = base_dir / "downloads" / "hennge_download_diagnostic"
    download_dir.mkdir(parents=True, exist_ok=True)
    (download_dir / "existing.p12").write_bytes(b"x")

    logger = DummyLogger()
    browser = DummyBrowser(base_dir, None, driver=Driver(root=_make_detail_panel(), rows=[_make_row()]))

    monkeypatch.setattr(mod, "_base_dir", lambda: base_dir)
    monkeypatch.setattr(mod, "load_config", lambda: {})
    monkeypatch.setattr(mod, "ensure_directories", lambda _config: None)
    monkeypatch.setattr(mod, "AppLogger", lambda _base_dir: logger)
    monkeypatch.setattr(mod, "Browser", lambda _base_dir, _config: browser)

    rc = mod.main(["SECRET_ALIAS"])

    assert rc == 10
    assert browser.start_count == 0
    assert browser.quit_count == 1
    assert logger.save_calls == []


def test_ensure_download_dir_ready_rejects_existing_files(tmp_path):
    download_dir = tmp_path / "downloads" / "hennge_download_diagnostic"
    download_dir.mkdir(parents=True, exist_ok=True)
    (download_dir / "leftover.p12").write_bytes(b"x")

    logger = DummyLogger()
    with pytest.raises(mod.DownloadFolderNotEmptyError):
        mod._ensure_download_dir_ready(download_dir, logger)


def test_wait_for_single_download_file_succeeds_after_size_stabilizes(tmp_path, monkeypatch):
    download_dir = tmp_path / "downloads" / "hennge_download_diagnostic"
    download_dir.mkdir(parents=True, exist_ok=True)
    logger = DummyLogger()
    clock = FakeClock()
    timeline = Timeline(clock)
    target = download_dir / "certificate.p12"

    timeline.schedule(0.5, lambda: target.write_bytes(b"abc"))
    monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mod.time, "sleep", timeline.sleep)

    found = mod._wait_for_single_download_file(download_dir, set(), logger, timeout_seconds=6)

    assert found == target
    assert any("extension=.p12" in message for message in logger.info_messages)
    assert any("new_file_count=1" in message for message in logger.info_messages)


def test_wait_for_single_download_file_ignores_temp_suffix(tmp_path, monkeypatch):
    download_dir = tmp_path / "downloads" / "hennge_download_diagnostic"
    download_dir.mkdir(parents=True, exist_ok=True)
    logger = DummyLogger()
    clock = FakeClock()
    timeline = Timeline(clock)
    temp_file = download_dir / "certificate.crdownload"
    target = download_dir / "certificate.p12"

    timeline.schedule(0.5, lambda: temp_file.write_bytes(b"abc"))
    timeline.schedule(1.5, lambda: temp_file.unlink())
    timeline.schedule(1.5, lambda: target.write_bytes(b"abc"))
    monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mod.time, "sleep", timeline.sleep)

    found = mod._wait_for_single_download_file(download_dir, set(), logger, timeout_seconds=6)

    assert found == target


def test_wait_for_single_download_file_rejects_zero_size_until_written(tmp_path, monkeypatch):
    download_dir = tmp_path / "downloads" / "hennge_download_diagnostic"
    download_dir.mkdir(parents=True, exist_ok=True)
    logger = DummyLogger()
    clock = FakeClock()
    timeline = Timeline(clock)
    target = download_dir / "certificate.p12"

    timeline.schedule(0.5, lambda: target.write_bytes(b""))
    timeline.schedule(1.5, lambda: target.write_bytes(b"abc"))
    monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mod.time, "sleep", timeline.sleep)

    found = mod._wait_for_single_download_file(download_dir, set(), logger, timeout_seconds=6)

    assert found == target


def test_wait_for_single_download_file_times_out_when_no_new_file(tmp_path, monkeypatch):
    download_dir = tmp_path / "downloads" / "hennge_download_diagnostic"
    download_dir.mkdir(parents=True, exist_ok=True)
    logger = DummyLogger()
    clock = FakeClock()
    timeline = Timeline(clock)
    monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mod.time, "sleep", timeline.sleep)

    with pytest.raises(mod.DownloadTimeoutError):
        mod._wait_for_single_download_file(download_dir, set(), logger, timeout_seconds=1.0)


def test_wait_for_single_download_file_fails_on_multiple_new_files(tmp_path, monkeypatch):
    download_dir = tmp_path / "downloads" / "hennge_download_diagnostic"
    download_dir.mkdir(parents=True, exist_ok=True)
    logger = DummyLogger()
    clock = FakeClock()
    timeline = Timeline(clock)
    first = download_dir / "a.p12"
    second = download_dir / "b.p12"

    timeline.schedule(0.5, lambda: first.write_bytes(b"abc"))
    timeline.schedule(0.5, lambda: second.write_bytes(b"def"))
    monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mod.time, "sleep", timeline.sleep)

    with pytest.raises(mod.DownloadMultipleFilesError):
        mod._wait_for_single_download_file(download_dir, set(), logger, timeout_seconds=3)
