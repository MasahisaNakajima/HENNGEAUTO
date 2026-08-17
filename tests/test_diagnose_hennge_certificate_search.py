from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.keys import Keys
import pytest

import diagnose_hennge_certificate_search as mod


class DummyElement:
    def __init__(self, *, displayed=True, enabled=True, attrs=None, text="", tag_name="input", effective_type=None):
        self.tag_name = "input"
        self._displayed = displayed
        self._enabled = enabled
        self._attrs = attrs or {}
        self.text = text
        self.sent_keys = []
        self.clicked = False
        self.tag_name = tag_name
        self._effective_type = effective_type

    def is_displayed(self):
        return self._displayed

    def is_enabled(self):
        return self._enabled

    def get_attribute(self, name):
        if name == "type" and name not in self._attrs:
            return self._effective_type
        return self._attrs.get(name)

    def send_keys(self, *args):
        self.sent_keys.append(args)

    def click(self):
        self.clicked = True


class DummyDriver:
    def __init__(self, mapping=None):
        self.mapping = mapping or {}
        self.current_url = "https://admin.auth.hennge.com/certificates/"

    def find_elements(self, by, selector):
        return self.mapping.get(selector, [])


class DummyLogger:
    def __init__(self):
        self.messages = []
        self.error_messages = []

    def info(self, message: str):
        self.messages.append(message)

    def error(self, message: str):
        self.error_messages.append(message)

    def exception(self, message: str):
        self.error_messages.append(message)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class SequenceDriver:
    def __init__(self, url_sequence, selector_sequences=None, heading_text="", raise_on_selector=None):
        self.url_sequence = list(url_sequence)
        self.url_index = 0
        self.selector_sequences = {
            selector: list(sequence)
            for selector, sequence in (selector_sequences or {}).items()
        }
        self.selector_index = {}
        self.find_calls = {}
        self.heading_text = heading_text
        self.raise_on_selector = raise_on_selector

    @property
    def current_url(self):
        if self.url_index < len(self.url_sequence):
            value = self.url_sequence[self.url_index]
            self.url_index += 1
            return value
        return self.url_sequence[-1]

    def find_elements(self, by, selector):
        self.find_calls[selector] = self.find_calls.get(selector, 0) + 1

        if self.raise_on_selector == selector:
            raise ValueError("find failed")

        if selector in self.selector_sequences:
            idx = self.selector_index.get(selector, 0)
            sequence = self.selector_sequences[selector]
            if idx < len(sequence):
                value = sequence[idx]
                self.selector_index[selector] = idx + 1
                return value
            return sequence[-1]

        if selector in {"h1", "h2", "[role='heading']"}:
            return [DummyElement(text=self.heading_text)] if self.heading_text else []

        return []


def _install_fake_time_and_wait(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mod.time, "sleep", clock.sleep)
    return clock


def test_find_single_visible_search_input_raises_zero_or_multiple() -> None:
    zero_driver = DummyDriver({mod.SEARCH_INPUT_FALLBACK_SELECTOR: []})
    try:
        mod._find_single_visible_search_input(zero_driver)
        assert False
    except mod.SearchInputError:
        pass

    many_driver = DummyDriver({mod.SEARCH_INPUT_FALLBACK_SELECTOR: [DummyElement(effective_type="text", attrs={"name": "query"}), DummyElement(effective_type="text", attrs={"name": "query"})]})
    try:
        mod._find_single_visible_search_input(many_driver)
        assert False
    except mod.SearchInputError:
        pass


def test_selector_constants_do_not_include_type_filter() -> None:
    assert "[type='text']" not in mod.SEARCH_INPUT_PRIMARY_SELECTOR
    assert "[type='text']" not in mod.SEARCH_INPUT_FALLBACK_SELECTOR


def test_wait_search_input_accepts_implicit_text_type_from_webdriver(monkeypatch) -> None:
    _install_fake_time_and_wait(monkeypatch)

    implicit_text = DummyElement(
        displayed=True,
        enabled=True,
        attrs={"name": "query", "aria-label": "Search"},
        effective_type="text",
    )
    driver = SequenceDriver(
        url_sequence=["https://admin.auth.hennge.com/certificates/"] * 5,
        selector_sequences={
            mod.SEARCH_INPUT_PRIMARY_SELECTOR: [[implicit_text]],
            mod.SEARCH_INPUT_FALLBACK_SELECTOR: [[]],
        },
    )

    found = mod._wait_certificate_search_input_ready(driver, DummyLogger(), timeout_seconds=2)
    assert found is implicit_text


def test_set_query_and_submit_sends_enter_once() -> None:
    search_input = DummyElement(attrs={"value": ""})
    logger = DummyLogger()
    mod._set_query_and_submit(search_input, "ALIAS", logger)

    enter_calls = [call for call in search_input.sent_keys if call == (Keys.ENTER,)]
    assert len(enter_calls) == 1
    assert logger.messages[-3:] == ["検索欄への入力開始", "検索欄への入力完了", "Enter送信完了"]


def test_determine_result_count_no_data_message_is_zero() -> None:
    driver = DummyDriver({
        "table tbody tr": [],
        "[data-testid='certificate-row']": [],
        "tr[data-testid*='certificate']": [],
        "li[data-testid*='certificate']": [],
        "body *": [DummyElement(text="データがありません")],
        ".pagination": [],
        ".table-footer": [],
        ".v-data-footer": [],
        "[class*='page']": [],
        "[class*='result']": [],
        "body": [],
    })

    assert mod._determine_result_count(driver) == 0


def test_determine_result_count_uses_range_label_as_fallback() -> None:
    driver = DummyDriver({
        "table tbody tr": [],
        "[data-testid='certificate-row']": [],
        "tr[data-testid*='certificate']": [],
        "li[data-testid*='certificate']": [],
        "body *": [],
        ".pagination": [DummyElement(text="1 - 1")],
        ".table-footer": [],
        ".v-data-footer": [],
        "[class*='page']": [],
        "[class*='result']": [],
        "body": [],
    })

    assert mod._determine_result_count(driver) == 1


def test_determine_result_count_distinguishes_one_and_multiple_rows() -> None:
    one_driver = DummyDriver({"table tbody tr": [DummyElement(text="row")]})
    assert mod._determine_result_count(one_driver) == 1

    many_driver = DummyDriver({"table tbody tr": [DummyElement(text="row1"), DummyElement(text="row2")]})
    assert mod._determine_result_count(many_driver) == 2


def test_log_result_state_does_not_include_sensitive_row_fields() -> None:
    logger = DummyLogger()
    mod._log_result_state(logger, 0)
    mod._log_result_state(logger, 1)
    mod._log_result_state(logger, 4)

    text = "\n".join(logger.messages).lower()
    assert "alias" not in text
    assert "@" not in text
    assert "href" not in text


def test_no_update_button_operation_in_submit_helper() -> None:
    search_input = DummyElement(attrs={"value": ""})
    mod._set_query_and_submit(search_input, "A", DummyLogger())
    assert search_input.clicked is False


def test_wait_search_input_allows_zero_then_success_with_fallback(monkeypatch) -> None:
    _install_fake_time_and_wait(monkeypatch)

    driver = SequenceDriver(
        url_sequence=[
            "https://admin.auth.hennge.com/certificates/",
            "https://admin.auth.hennge.com/certificates/",
            "https://admin.auth.hennge.com/certificates/",
        ],
        selector_sequences={
            mod.SEARCH_INPUT_PRIMARY_SELECTOR: [[], [], []],
            mod.SEARCH_INPUT_FALLBACK_SELECTOR: [
                [],
                [DummyElement(displayed=True, enabled=False, attrs={"name": "query", "type": "text", "disabled": "disabled"})],
                [DummyElement(displayed=True, enabled=True, attrs={"name": "query", "type": "text", "id": "q1"})],
            ],
        },
        heading_text="証明書一覧",
    )
    logger = DummyLogger()

    element = mod._wait_certificate_search_input_ready(driver, logger, timeout_seconds=5)
    assert element.get_attribute("name") == "query"


def test_wait_search_input_timeout_when_never_visible(monkeypatch) -> None:
    _install_fake_time_and_wait(monkeypatch)

    driver = SequenceDriver(
        url_sequence=["https://admin.auth.hennge.com/certificates/"] * 20,
        selector_sequences={
            mod.SEARCH_INPUT_PRIMARY_SELECTOR: [[]] * 20,
            mod.SEARCH_INPUT_FALLBACK_SELECTOR: [[]] * 20,
        },
    )
    logger = DummyLogger()

    with pytest.raises(RuntimeError, match="表示されませんでした"):
        mod._wait_certificate_search_input_ready(driver, logger, timeout_seconds=2)


def test_wait_search_input_raises_for_multiple_visible(monkeypatch) -> None:
    _install_fake_time_and_wait(monkeypatch)

    dup1 = DummyElement(displayed=True, enabled=True, attrs={"name": "query", "aria-label": "Search"}, effective_type="text")
    dup2 = DummyElement(displayed=True, enabled=True, attrs={"name": "query", "aria-label": "Search"}, effective_type="text")

    driver = SequenceDriver(
        url_sequence=["https://admin.auth.hennge.com/certificates/"] * 20,
        selector_sequences={
            mod.SEARCH_INPUT_PRIMARY_SELECTOR: [[dup1, dup2]] * 20,
            mod.SEARCH_INPUT_FALLBACK_SELECTOR: [[]] * 20,
        },
    )
    logger = DummyLogger()

    with pytest.raises(RuntimeError, match="複数"):
        mod._wait_certificate_search_input_ready(driver, logger, timeout_seconds=2)


def test_wait_search_input_waits_until_enabled(monkeypatch) -> None:
    _install_fake_time_and_wait(monkeypatch)

    disabled = DummyElement(displayed=True, enabled=False, attrs={"name": "query", "aria-label": "Search", "disabled": "disabled"}, effective_type="text")
    enabled = DummyElement(displayed=True, enabled=True, attrs={"name": "query", "aria-label": "Search", "id": "query"}, effective_type="text")
    driver = SequenceDriver(
        url_sequence=["https://admin.auth.hennge.com/certificates/"] * 20,
        selector_sequences={
            mod.SEARCH_INPUT_PRIMARY_SELECTOR: [[disabled], [disabled], [enabled]],
            mod.SEARCH_INPUT_FALLBACK_SELECTOR: [[]] * 20,
        },
    )
    logger = DummyLogger()

    element = mod._wait_certificate_search_input_ready(driver, logger, timeout_seconds=5)
    assert element is enabled


def test_wait_search_input_raises_when_url_not_certificates(monkeypatch) -> None:
    _install_fake_time_and_wait(monkeypatch)

    driver = SequenceDriver(
        url_sequence=["https://admin.auth.hennge.com/users/"] * 20,
        selector_sequences={
            mod.SEARCH_INPUT_PRIMARY_SELECTOR: [[]] * 20,
            mod.SEARCH_INPUT_FALLBACK_SELECTOR: [[]] * 20,
        },
    )
    logger = DummyLogger()

    with pytest.raises(RuntimeError, match="URLが/certificates/"):
        mod._wait_certificate_search_input_ready(driver, logger, timeout_seconds=2)


def test_wait_search_input_uses_primary_selector_first_and_skips_fallback(monkeypatch) -> None:
    _install_fake_time_and_wait(monkeypatch)

    primary = DummyElement(displayed=True, enabled=True, attrs={"name": "query", "aria-label": "Search"}, effective_type="text")
    driver = SequenceDriver(
        url_sequence=["https://admin.auth.hennge.com/certificates/"] * 5,
        selector_sequences={
            mod.SEARCH_INPUT_PRIMARY_SELECTOR: [[primary]],
            mod.SEARCH_INPUT_FALLBACK_SELECTOR: [[DummyElement(displayed=True)]],
        },
    )

    found = mod._wait_certificate_search_input_ready(driver, DummyLogger(), timeout_seconds=2)
    assert found is primary
    assert driver.find_calls.get(mod.SEARCH_INPUT_FALLBACK_SELECTOR, 0) == 0


def test_wait_search_input_returns_same_web_element_and_does_not_refetch(monkeypatch) -> None:
    _install_fake_time_and_wait(monkeypatch)

    primary = DummyElement(displayed=True, enabled=True, attrs={"name": "query", "aria-label": "Search"}, effective_type="text")
    driver = SequenceDriver(
        url_sequence=["https://admin.auth.hennge.com/certificates/"] * 5,
        selector_sequences={
            mod.SEARCH_INPUT_PRIMARY_SELECTOR: [[primary]],
            mod.SEARCH_INPUT_FALLBACK_SELECTOR: [[]],
        },
    )

    found = mod._wait_certificate_search_input_ready(driver, DummyLogger(), timeout_seconds=2)
    assert found is primary
    assert driver.find_calls.get(mod.SEARCH_INPUT_PRIMARY_SELECTOR, 0) == 1


def test_wait_search_input_logs_exception_type_once_and_reraises(monkeypatch) -> None:
    _install_fake_time_and_wait(monkeypatch)

    driver = SequenceDriver(
        url_sequence=["https://admin.auth.hennge.com/certificates/"] * 5,
        selector_sequences={},
        raise_on_selector=mod.SEARCH_INPUT_PRIMARY_SELECTOR,
    )
    logger = DummyLogger()

    with pytest.raises(ValueError):
        mod._wait_certificate_search_input_ready(driver, logger, timeout_seconds=2)

    assert len(logger.error_messages) == 1
    assert logger.error_messages[0].endswith("ValueError")


def test_wait_search_input_excludes_password_and_submit_types(monkeypatch) -> None:
    _install_fake_time_and_wait(monkeypatch)

    password_like = DummyElement(displayed=True, enabled=True, attrs={"name": "query", "aria-label": "Search"}, effective_type="password")
    submit_like = DummyElement(displayed=True, enabled=True, attrs={"name": "query", "aria-label": "Search"}, effective_type="submit")
    valid = DummyElement(displayed=True, enabled=True, attrs={"name": "query", "aria-label": "Search"}, effective_type="text")
    driver = SequenceDriver(
        url_sequence=["https://admin.auth.hennge.com/certificates/"] * 5,
        selector_sequences={
            mod.SEARCH_INPUT_PRIMARY_SELECTOR: [[password_like, submit_like, valid]],
            mod.SEARCH_INPUT_FALLBACK_SELECTOR: [[]],
        },
    )

    found = mod._wait_certificate_search_input_ready(driver, DummyLogger(), timeout_seconds=2)
    assert found is valid


def test_wait_search_input_excludes_non_query_name(monkeypatch) -> None:
    _install_fake_time_and_wait(monkeypatch)

    wrong_name = DummyElement(displayed=True, enabled=True, attrs={"name": "not_query", "aria-label": "Search"}, effective_type="text")
    driver = SequenceDriver(
        url_sequence=["https://admin.auth.hennge.com/certificates/"] * 20,
        selector_sequences={
            mod.SEARCH_INPUT_PRIMARY_SELECTOR: [[wrong_name]] * 20,
            mod.SEARCH_INPUT_FALLBACK_SELECTOR: [[]] * 20,
        },
    )

    with pytest.raises(RuntimeError, match="表示されませんでした"):
        mod._wait_certificate_search_input_ready(driver, DummyLogger(), timeout_seconds=2)


def test_main_retries_stale_only_once_with_primary_selector(monkeypatch):
    class MainDummyDriver:
        current_url = "https://admin.auth.hennge.com/certificates/"

    class MainDummyBrowser:
        def __init__(self, *_args, **_kwargs):
            self.driver = MainDummyDriver()

        def start(self):
            return None

        def open(self, _url):
            return None

        def wait_for_page_ready(self, timeout=20):
            _ = timeout
            return None

        def quit(self):
            return None

    class MainDummyHandler:
        def __init__(self, *_args, **_kwargs):
            return None

        def login(self):
            return None

    logger = DummyLogger()
    first_element = object()
    refreshed_element = object()
    calls = {"submit": 0, "refetch": 0}
    submit_targets = []

    monkeypatch.setattr(mod, "Browser", MainDummyBrowser)
    monkeypatch.setattr(mod, "HenngeHandler", MainDummyHandler)
    monkeypatch.setattr(mod, "load_config", lambda: {})
    monkeypatch.setattr(mod, "ensure_directories", lambda _config: None)
    monkeypatch.setattr(mod, "AppLogger", lambda _base_dir: logger)
    monkeypatch.setattr(mod, "_wait_certificate_search_input_ready", lambda _driver, _logger, timeout_seconds=20: first_element)
    monkeypatch.setattr(mod, "_wait_results_ready", lambda _browser, timeout_seconds=15: 1)
    monkeypatch.setattr(mod, "_save_diag_no_html", lambda _logger, _browser, _name: None)

    def fake_submit(search_input, query, submit_logger):
        _ = (query, submit_logger)
        calls["submit"] += 1
        submit_targets.append(search_input)
        if calls["submit"] == 1:
            raise StaleElementReferenceException("stale")

    def fake_refetch(_driver, selector):
        calls["refetch"] += 1
        assert selector == mod.SEARCH_INPUT_PRIMARY_SELECTOR
        return refreshed_element

    monkeypatch.setattr(mod, "_set_query_and_submit", fake_submit)
    monkeypatch.setattr(mod, "_find_single_visible_search_input", fake_refetch)

    rc = mod.main(["ALIAS"])
    assert rc == 0
    assert calls["submit"] == 2
    assert calls["refetch"] == 1
    assert submit_targets == [first_element, refreshed_element]
