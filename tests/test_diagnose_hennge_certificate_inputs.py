import pytest

import diagnose_hennge_certificate_inputs as mod


class DummyParent:
    def __init__(self, *, tag_name="div", attrs=None):
        self.tag_name = tag_name
        self._attrs = attrs or {}

    def get_attribute(self, name):
        return self._attrs.get(name)


class DummyInput:
    def __init__(self, *, displayed=True, enabled=True, attrs=None, parent=None):
        self.tag_name = "input"
        self._displayed = displayed
        self._enabled = enabled
        self._attrs = attrs or {}
        self._parent = parent or DummyParent()

    def is_displayed(self):
        return self._displayed

    def is_enabled(self):
        return self._enabled

    def get_attribute(self, name):
        return self._attrs.get(name)

    def find_elements(self, by, selector):
        if selector == "ancestor::*[1]":
            return [self._parent]
        return []


class DummyDriver:
    def __init__(self, *, url_sequence=None, title_sequence=None, input_sequence=None):
        self._url_seq = list(url_sequence or [])
        self._title_seq = list(title_sequence or [])
        self._input_seq = list(input_sequence or [])
        self._url_i = 0
        self._title_i = 0
        self._input_i = 0

    @property
    def current_url(self):
        if not self._url_seq:
            return ""
        if self._url_i < len(self._url_seq):
            v = self._url_seq[self._url_i]
            self._url_i += 1
            return v
        return self._url_seq[-1]

    @property
    def title(self):
        if not self._title_seq:
            return ""
        if self._title_i < len(self._title_seq):
            v = self._title_seq[self._title_i]
            self._title_i += 1
            return v
        return self._title_seq[-1]

    def find_elements(self, by, selector):
        if selector == "input":
            if not self._input_seq:
                return []
            if self._input_i < len(self._input_seq):
                v = self._input_seq[self._input_i]
                self._input_i += 1
                return v
            return self._input_seq[-1]
        return []


class DummyBrowser:
    def __init__(self, driver):
        self.driver = driver

    def wait_for_page_ready(self, timeout=3):
        return True


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _install_fake_clock(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mod.time, "sleep", clock.sleep)
    return clock


def test_wait_certificates_page_ready_success(monkeypatch):
    _install_fake_clock(monkeypatch)
    driver = DummyDriver(
        url_sequence=[
            "https://admin.auth.hennge.com/users/",
            "https://admin.auth.hennge.com/certificates/?s=secret",
            "https://admin.auth.hennge.com/certificates/",
        ],
        title_sequence=["Loading", "証明書一覧", "証明書一覧"],
    )
    browser = DummyBrowser(driver)

    mod._wait_certificates_page_ready(browser, timeout_seconds=5)


def test_wait_certificates_page_ready_timeout(monkeypatch):
    _install_fake_clock(monkeypatch)
    driver = DummyDriver(
        url_sequence=["https://admin.auth.hennge.com/users/"] * 20,
        title_sequence=["Users"] * 20,
    )
    browser = DummyBrowser(driver)

    with pytest.raises(RuntimeError, match="タイムアウト"):
        mod._wait_certificates_page_ready(browser, timeout_seconds=2)


def test_wait_visible_inputs_success_after_delay(monkeypatch):
    _install_fake_clock(monkeypatch)
    visible_input = DummyInput(displayed=True)
    driver = DummyDriver(input_sequence=[[], [], [visible_input]])

    found = mod._wait_visible_inputs(driver, timeout_seconds=5)
    assert len(found) == 1


def test_wait_visible_inputs_timeout(monkeypatch):
    _install_fake_clock(monkeypatch)
    driver = DummyDriver(input_sequence=[[]] * 20)

    with pytest.raises(RuntimeError, match="タイムアウト"):
        mod._wait_visible_inputs(driver, timeout_seconds=2)


def test_inspect_visible_inputs_logs_hidden_count_only():
    parent = DummyParent(tag_name="div", attrs={"role": "search", "aria-label": "search box", "data-testid": "search-wrap"})
    hidden = DummyInput(displayed=True, attrs={"type": "hidden", "name": "_xsrf", "value": "token"}, parent=parent)
    normal = DummyInput(
        displayed=True,
        enabled=True,
        attrs={
            "type": "text",
            "id": "query",
            "name": "query",
            "class": "search-input",
            "placeholder": "検索",
            "aria-label": "query",
            "role": "textbox",
            "autocomplete": "off",
        },
        parent=parent,
    )

    hidden_count, details = mod._inspect_visible_inputs([hidden, normal])

    assert hidden_count == 1
    assert len(details) == 1
    assert details[0]["name"] == "query"
    assert "value" not in details[0]
