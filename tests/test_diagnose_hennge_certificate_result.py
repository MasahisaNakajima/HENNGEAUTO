import pytest
from selenium.common.exceptions import StaleElementReferenceException

import diagnose_hennge_certificate_result as mod


class DummyElement:
    def __init__(self, *, tag_name="div", displayed=True, enabled=True, attrs=None, text="", children=None):
        self.tag_name = tag_name
        self._displayed = displayed
        self._enabled = enabled
        self._attrs = attrs or {}
        self.text = text
        self._children = children or {}
        self.clicked = 0
        self.sent_keys = []

    def is_displayed(self):
        return self._displayed

    def is_enabled(self):
        return self._enabled

    def get_attribute(self, name):
        return self._attrs.get(name)

    def find_elements(self, by, selector):
        return self._children.get(selector, [])

    def click(self):
        self.clicked += 1

    def send_keys(self, *args):
        self.sent_keys.append(args)


class DummyDriver:
    def __init__(self, rows=None):
        self.current_url = "https://admin.auth.hennge.com/certificates/?q=secret#frag"
        self._rows = rows or []

    def find_elements(self, by, selector):
        if selector in mod.RESULT_ROW_SELECTORS:
            if selector == mod.RESULT_ROW_SELECTORS[0]:
                return self._rows
            return []
        return []


class DummyBrowser:
    def __init__(self, _base_dir, _config, driver=None):
        self.driver = driver or DummyDriver()

    def start(self):
        return None

    def open(self, _url):
        return None

    def wait_for_page_ready(self, timeout=20):
        _ = timeout
        return None

    def quit(self):
        return None


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


def _make_row(action_elements):
    children = {
        "a": [e for e in action_elements if e.tag_name == "a"],
        "button": [e for e in action_elements if e.tag_name == "button"],
        "input": [e for e in action_elements if e.tag_name == "input"],
    }
    return DummyElement(
        tag_name="tr",
        attrs={
            "role": "row",
            "class": "certificate-row",
            "data-testid": "certificate-row-1",
            "aria-label": "row",
        },
        children=children,
    )


def _install_main_mocks(monkeypatch, *, result_count=1, rows=None, stale_once=False):
    logger = DummyLogger()
    search_input = DummyElement(tag_name="input", attrs={"name": "query", "aria-label": "Search"})
    driver = DummyDriver(rows=rows or [])
    browser = DummyBrowser(None, None, driver=driver)

    calls = {
        "wait_input": 0,
        "set_submit": 0,
        "wait_results": 0,
        "find_single": 0,
        "set_targets": [],
    }

    monkeypatch.setattr(mod, "load_config", lambda: {})
    monkeypatch.setattr(mod, "ensure_directories", lambda _config: None)
    monkeypatch.setattr(mod, "AppLogger", lambda _base_dir: logger)
    monkeypatch.setattr(mod, "Browser", lambda _base_dir, _config: browser)
    monkeypatch.setattr(mod, "HenngeHandler", DummyHandler)

    def fake_wait_input(driver_arg, logger_arg, timeout_seconds=20):
        _ = (driver_arg, logger_arg, timeout_seconds)
        calls["wait_input"] += 1
        return search_input

    def fake_set_submit(search_input_arg, alias_arg, logger_arg):
        _ = (alias_arg, logger_arg)
        calls["set_submit"] += 1
        calls["set_targets"].append(search_input_arg)
        if stale_once and calls["set_submit"] == 1:
            raise StaleElementReferenceException("stale")

    def fake_wait_results(browser_arg, timeout_seconds=15):
        _ = (browser_arg, timeout_seconds)
        calls["wait_results"] += 1
        return result_count

    def fake_log_result_state(logger_arg, count_arg):
        _ = (logger_arg, count_arg)
        return None

    def fake_find_single(driver_arg, selector):
        _ = driver_arg
        calls["find_single"] += 1
        assert selector == mod.cert_search.SEARCH_INPUT_PRIMARY_SELECTOR
        return DummyElement(tag_name="input", attrs={"name": "query", "aria-label": "Search"})

    monkeypatch.setattr(mod.cert_search, "_wait_certificate_search_input_ready", fake_wait_input)
    monkeypatch.setattr(mod.cert_search, "_set_query_and_submit", fake_set_submit)
    monkeypatch.setattr(mod.cert_search, "_wait_results_ready", fake_wait_results)
    monkeypatch.setattr(mod.cert_search, "_log_result_state", fake_log_result_state)
    monkeypatch.setattr(mod.cert_search, "_find_single_visible_search_input", fake_find_single)

    return logger, calls


def test_sanitize_href_host_path_removes_query_and_fragment():
    href = "https://admin.auth.hennge.com/certificates/download?id=1#anchor"
    assert mod._sanitize_href_host_path(href) == "admin.auth.hennge.com/certificates/download"


def test_collect_row_structure_does_not_include_text_or_value_fields():
    action = DummyElement(
        tag_name="a",
        attrs={
            "id": "dl1",
            "name": "download",
            "class": "btn",
            "role": "button",
            "aria-label": "download",
            "title": "download",
            "data-testid": "dl-btn",
            "href": "https://admin.auth.hennge.com/certificates/download?token=secret#frag",
            "value": "SECRET",
        },
        text="user@example.com",
    )
    row = _make_row([action])

    row_info, elements = mod._collect_row_structure(row)
    assert row_info["a_count"] == 1
    assert "text" not in elements[0]
    assert "value" not in elements[0]
    assert elements[0]["href_host_path"] == "admin.auth.hennge.com/certificates/download"


def test_main_reuses_certificate_search_functions(monkeypatch):
    download_action = DummyElement(tag_name="a", attrs={"href": "https://admin.auth.hennge.com/certificates/download"})
    row = _make_row([download_action])
    logger, calls = _install_main_mocks(monkeypatch, result_count=1, rows=[row])

    rc = mod.main(["TEST_ALIAS"])

    assert rc == 0
    assert calls["wait_input"] == 1
    assert calls["set_submit"] == 1
    assert calls["wait_results"] == 1
    assert all(save_html is False for _, _, save_html in logger.save_calls)


@pytest.mark.parametrize("count, expected_rc", [(0, 2), (2, 3)])
def test_main_skips_row_inspection_when_not_single_result(monkeypatch, count, expected_rc):
    logger, calls = _install_main_mocks(monkeypatch, result_count=count, rows=[])

    rc = mod.main(["TEST_ALIAS"])

    assert rc == expected_rc
    assert calls["wait_results"] == 1
    assert all("検索結果行構造" not in msg for msg in logger.info_messages)


@pytest.mark.parametrize("actions, expected_rc", [
    ([DummyElement(tag_name="button", attrs={"class": "plain-action"})], 4),
    ([DummyElement(tag_name="a", attrs={"href": "https://admin.auth.hennge.com/certificates/download"})], 0),
    ([
        DummyElement(tag_name="a", attrs={"href": "https://admin.auth.hennge.com/certificates/download"}),
        DummyElement(tag_name="button", attrs={"title": "download file"}),
    ], 5),
])
def test_main_distinguishes_download_candidate_counts(monkeypatch, actions, expected_rc):
    row = _make_row(actions)
    logger, _calls = _install_main_mocks(monkeypatch, result_count=1, rows=[row])

    rc = mod.main(["TEST_ALIAS"])

    assert rc == expected_rc
    assert all(save_html is False for _, _, save_html in logger.save_calls)


def test_main_does_not_click_row_or_download_candidates(monkeypatch):
    update_button = DummyElement(tag_name="button", attrs={"class": "update delete", "title": "update"})
    download_link = DummyElement(tag_name="a", attrs={"href": "https://admin.auth.hennge.com/certificates/download"})
    row = _make_row([update_button, download_link])
    _logger, _calls = _install_main_mocks(monkeypatch, result_count=1, rows=[row])

    rc = mod.main(["TEST_ALIAS"])

    assert rc == 0
    assert row.clicked == 0
    assert update_button.clicked == 0
    assert download_link.clicked == 0
    assert row.sent_keys == []
    assert update_button.sent_keys == []
    assert download_link.sent_keys == []


def test_main_stale_retries_once_with_primary_selector(monkeypatch):
    download_action = DummyElement(tag_name="a", attrs={"href": "https://admin.auth.hennge.com/certificates/download"})
    row = _make_row([download_action])
    _logger, calls = _install_main_mocks(monkeypatch, result_count=1, rows=[row], stale_once=True)

    rc = mod.main(["TEST_ALIAS"])

    assert rc == 0
    assert calls["set_submit"] == 2
    assert calls["find_single"] == 1


def test_log_output_does_not_include_alias_or_personal_data(monkeypatch):
    action = DummyElement(
        tag_name="a",
        attrs={
            "href": "https://admin.auth.hennge.com/certificates/download?mail=user@example.com",
            "title": "download",
        },
        text="user@example.com",
    )
    row = _make_row([action])
    logger, _calls = _install_main_mocks(monkeypatch, result_count=1, rows=[row])

    rc = mod.main(["SECRET_ALIAS"])

    assert rc == 0
    joined = "\n".join(logger.info_messages + logger.error_messages + logger.exception_messages)
    assert "SECRET_ALIAS" not in joined
    assert "user@example.com" not in joined
    assert "?mail=" not in joined
