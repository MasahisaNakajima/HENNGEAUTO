from __future__ import annotations

import pytest
from selenium.common.exceptions import TimeoutException

from app.smsm_handler import SmsmHandler
from app.smsm_config import SmsmConfig, resolve_smsm_config


class FakeElement:
    def __init__(self, name, click_error=None, click_callback=None):
        self.name = name
        self.element_id = {
            "company": "user_company_code",
            "user": "user_login",
            "password": "user_password",
        }.get(name, name)
        self.click_error = click_error
        self.click_callback = click_callback
        self.displayed = True
        self.sent_values = []

    def clear(self):
        return None

    def send_keys(self, value):
        self.sent_values.append(value)

    def get_attribute(self, name):
        if name == "value":
            return self.sent_values[-1] if self.sent_values else ""
        if name == "id":
            return self.element_id
        if name == "name":
            return {
                "company": "user[company_code]",
                "user": "user[login]",
                "password": "user[password]",
            }.get(self.name, self.name)
        if name == "type":
            return "password" if self.name == "password" else "text"
        return None

    def is_displayed(self):
        return self.displayed

    def is_enabled(self):
        return True

    def click(self):
        if self.click_error:
            raise self.click_error
        if self.click_callback:
            self.click_callback()


class FakeDriver:
    current_url = "https://ausl.smartmanager.jp/devices"
    window_handles = ["main"]

    def __init__(self, elements, fail=None):
        self.elements = elements
        self.password_element = elements[2]
        self.fail = fail

    def find_elements(self, _by, value):
        if self.fail == "password" and "password" in value:
            raise TimeoutException("private password failure")
        if self.fail == "user" and value in {"user_login", "user[login]"}:
            return []
        if value in {"user_password", "user[password]"}:
            return [self.password_element]
        if value in {"user_company_code", "user[company_code]"}:
            return [self.elements[0]]
        if value in {"user_login", "user[login]"}:
            return [self.elements[1]]
        return []


class FakeBrowser:
    def __init__(self, *, fail=None, additional_auth=False, click_error=None):
        self.fail = fail
        self.additional_auth = additional_auth
        self.elements = [FakeElement("company"), FakeElement("user"), FakeElement("password")]
        self.driver = FakeDriver(self.elements, fail=fail)
        self.open_calls = []
        self.wait_calls = 0
        self.find_calls = 0
        self.click_error = click_error

    def _complete_login(self):
        self.driver.current_url = "https://ausl.smartmanager.jp/dashboard"
        self.find_calls = 3
        for element in self.elements:
            element.displayed = False

    def open(self, url):
        self.open_calls.append(url)
        if self.fail == "open":
            raise RuntimeError("private open failure")

    def wait_for_page_ready(self, timeout=15):
        self.wait_calls += 1
        if self.fail == "wait_page":
            raise RuntimeError("private page failure")

    def find_first(self, locators, timeout=10):
        self.find_calls += 1
        if self.fail == "company" and self.find_calls == 1:
            raise TimeoutException("private user failure")
        if self.fail == "user" and self.find_calls == 2:
            raise TimeoutException("private user failure")
        if self.fail == "password" and self.find_calls == 3:
            raise TimeoutException("private password failure")
        if self.find_calls > 3:
            locator_text = " ".join(str(value) for _by, value in locators)
            if self.fail == "error_banner" and "alert-danger" in locator_text:
                return FakeElement("error-banner")
            if self.additional_auth and "otp" in locator_text:
                return FakeElement("additional-auth")
            if "logout" in locator_text or "//nav" in locator_text:
                return FakeElement("logged-in-marker")
            raise TimeoutException("private marker not found")
        if self.fail == "complete":
            raise TimeoutException("private complete failure")
        return self.elements[min(self.find_calls - 1, 2)]

    def wait_for_clickable(self, by, value, timeout=20):
        if self.fail == "button":
            raise TimeoutException("private button failure")
        return FakeElement("button", click_error=self.click_error, click_callback=self._complete_login)


class SelectorElement(FakeElement):
    def __init__(self, element_id, element_name, element_type="text", *, displayed=True, enabled=True, readonly=False, disabled=False):
        super().__init__(element_id)
        self.element_id = element_id
        self.element_name = element_name
        self.element_type = element_type
        self.displayed = displayed
        self.enabled = enabled
        self.readonly = readonly
        self.disabled = disabled

    def get_attribute(self, name):
        if name == "id":
            return self.element_id
        if name == "name":
            return self.element_name
        if name == "type":
            return self.element_type
        if name == "readonly":
            return "readonly" if self.readonly else None
        if name == "disabled":
            return "disabled" if self.disabled else None
        return super().get_attribute(name)

    def is_displayed(self):
        return self.displayed

    def is_enabled(self):
        return self.enabled


class SelectorDriver:
    def __init__(self, elements):
        self.elements = elements

    def find_elements(self, by, value):
        if by == "id":
            return [element for element in self.elements if element.element_id == value]
        if by == "name":
            return [element for element in self.elements if element.element_name == value]
        return []


class SelectorBrowser:
    def __init__(self, elements):
        self.driver = SelectorDriver(elements)


def selector_handler(elements):
    return SmsmHandler(
        browser=SelectorBrowser(elements),
        logger=FakeLogger(),
        smsm_config=SmsmConfig(
            url="https://smsm.test.invalid",
            company_code="company_secret",
            username="user_secret",
            password="password_secret",
            source="test",
            valid=True,
        ),
    )


class FakeLogger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(message)

    def exception(self, message):
        self.messages.append(message)


def run_login(monkeypatch, *, config=None, browser=None):
    logger = FakeLogger()
    browser = browser or FakeBrowser()
    trace = []
    resolved_config = resolve_smsm_config(config) if isinstance(config, dict) else config
    handler = SmsmHandler(
        browser=browser,
        logger=logger,
        smsm_config=resolved_config or SmsmConfig(
            url="https://smsm.test.invalid",
            company_code="company_secret",
            username="user_secret",
            password="password_secret",
            source="test",
            valid=True,
        ),
    )
    monkeypatch.setattr("app.smsm_handler.time.sleep", lambda _seconds: None)
    return handler, logger, browser, trace


def invoke(handler, trace):
    try:
        handler.login(lambda key, value: trace.append((key, value)))
    except RuntimeError as exc:
        return exc
    return None


def trace_values(trace):
    return {key: value for key, value in trace}


def test_missing_credentials_stops_before_browser(monkeypatch):
    handler, logger, browser, trace = run_login(
        monkeypatch,
        config={"smsm": {"username": "", "password": ""}},
    )

    error = invoke(handler, trace)

    assert str(error) == "SMSMログイン失敗"
    assert browser.open_calls == []
    assert trace_values(trace)["smsm_config_validation"] is True
    assert "user_secret" not in "\n".join(logger.messages)
    assert "password_secret" not in "\n".join(logger.messages)


def test_login_page_open_failure_is_classified(monkeypatch):
    handler, _logger, browser, trace = run_login(monkeypatch, browser=FakeBrowser(fail="open"))
    invoke(handler, trace)
    values = trace_values(trace)
    assert browser.open_calls
    assert values["login_page_opened"] is False
    assert "smsm_open_login_page" in values


def test_user_field_failure_is_classified(monkeypatch):
    handler, _logger, _browser, trace = run_login(monkeypatch, browser=FakeBrowser(fail="user"))
    invoke(handler, trace)
    values = trace_values(trace)
    assert values["login_page_opened"] is True
    assert values["company_field_found"] is True
    assert values["user_field_found"] is False
    assert values["password_field_found"] is False


def test_password_field_failure_is_classified(monkeypatch):
    handler, _logger, _browser, trace = run_login(monkeypatch, browser=FakeBrowser(fail="password"))
    invoke(handler, trace)
    values = trace_values(trace)
    assert values["company_field_found"] is True
    assert values["user_field_found"] is True
    assert values["password_field_found"] is False
    assert values["login_submitted"] is False


def test_login_button_failure_is_classified(monkeypatch):
    handler, _logger, _browser, trace = run_login(monkeypatch, browser=FakeBrowser(fail="button"))
    invoke(handler, trace)
    values = trace_values(trace)
    assert values["login_button_found"] is False
    assert values["login_submitted"] is False


def test_login_submit_failure_is_classified(monkeypatch):
    handler, _logger, _browser, trace = run_login(
        monkeypatch,
        browser=FakeBrowser(click_error=RuntimeError("private click failure")),
    )
    invoke(handler, trace)
    values = trace_values(trace)
    assert values["login_button_found"] is True
    assert values["login_submitted"] is False
    assert isinstance(values["smsm_submit_login_elapsed_ms"], int)
    assert isinstance(values["smsm_login_click_elapsed_ms"], int)


def test_login_completion_failure_is_classified(monkeypatch):
    handler, _logger, _browser, trace = run_login(monkeypatch)
    monkeypatch.setattr(handler, "_wait_for_login_success", lambda timeout=30, trace=None: (_ for _ in ()).throw(RuntimeError("private completion failure")))
    invoke(handler, trace)
    values = trace_values(trace)
    assert values["login_submitted"] is True
    assert values["smsm_wait_login_complete"] is True
    assert values["login_completed"] is False


def test_login_form_disappearance_completes_login_without_generic_landmark(monkeypatch):
    handler, _logger, browser, trace = run_login(monkeypatch)

    assert invoke(handler, trace) is None
    values = trace_values(trace)
    assert values["login_form_still_visible"] is False
    assert values["company_field_still_visible"] is False
    assert values["user_field_still_visible"] is False
    assert values["password_field_still_visible"] is False
    assert values["login_path_changed"] is True
    assert values["same_smsm_host"] is True
    assert values["post_login_landmark_found"] is False
    assert values["login_completed"] is True
    assert browser.wait_calls < 10


def test_same_host_and_changed_path_are_required_for_success(monkeypatch):
    handler, _logger, browser, _trace = run_login(monkeypatch)
    handler._login_origin_url = "https://ausl.smartmanager.jp"
    browser.driver.current_url = "https://ausl.smartmanager.jp/devices"
    for element in browser.elements:
        element.displayed = False

    state = handler._login_completion_state()

    assert state["same_smsm_host"] is True
    assert state["login_path_changed"] is True
    assert state["login_completed"] is True


def test_login_form_remaining_prevents_success(monkeypatch):
    handler, _logger, browser, _trace = run_login(monkeypatch)
    handler._login_origin_url = "https://ausl.smartmanager.jp/login"
    browser.driver.current_url = "https://ausl.smartmanager.jp/devices"

    state = handler._login_completion_state()

    assert state["login_form_still_visible"] is True
    assert state["login_completed"] is False


def test_different_host_prevents_success(monkeypatch):
    handler, _logger, browser, _trace = run_login(monkeypatch)
    handler._login_origin_url = "https://ausl.smartmanager.jp/login"
    browser.driver.current_url = "https://other.invalid/devices"
    for element in browser.elements:
        element.displayed = False

    state = handler._login_completion_state()

    assert state["same_smsm_host"] is False
    assert state["login_completed"] is False


def test_generic_button_does_not_detect_additional_auth(monkeypatch):
    handler, _logger, _browser, _trace = run_login(monkeypatch)
    handler.browser.find_calls = 3
    result = handler._detect_additional_auth()
    assert result["additional_auth_detected"] is False
    assert result["confirmation_page_detected"] is False


def test_generic_nav_does_not_detect_confirmation_page(monkeypatch):
    handler, _logger, _browser, _trace = run_login(monkeypatch)
    handler.browser.find_calls = 3
    result = handler._detect_additional_auth()
    assert result["confirmation_page_detected"] is False


def test_explicit_additional_auth_marker_is_detected(monkeypatch):
    handler, _logger, _browser, _trace = run_login(monkeypatch, browser=FakeBrowser(additional_auth=True))
    handler.browser.find_calls = 3
    result = handler._detect_additional_auth()
    assert result["additional_auth_detected"] is True


def test_login_error_banner_stops_completion(monkeypatch):
    handler, _logger, browser, trace = run_login(monkeypatch, browser=FakeBrowser(fail="error_banner"))
    error = invoke(handler, trace)
    values = trace_values(trace)
    assert error is not None
    assert values["login_completed"] is False
    assert browser.elements[0].sent_values == ["company_secret"]


def test_additional_auth_is_recorded_without_page_text(monkeypatch):
    handler, logger, _browser, trace = run_login(monkeypatch, browser=FakeBrowser(additional_auth=True))
    error = invoke(handler, trace)
    values = trace_values(trace)
    assert error is None
    assert values["additional_auth_detected"] is True
    assert values["login_completed"] is True
    output = "\n".join(logger.messages)
    assert "private" not in output
    assert "https://" not in output
    assert "Cookie" not in output
    assert "session" not in output.lower()


def test_success_trace_contains_only_fixed_state(monkeypatch):
    handler, _logger, _browser, trace = run_login(monkeypatch)
    assert invoke(handler, trace) is None
    assert trace_values(trace)["login_completed"] is True
    values = trace_values(trace)
    for key in (
        "smsm_open_login_page_elapsed_ms",
        "smsm_wait_login_page_elapsed_ms",
        "smsm_find_company_field_elapsed_ms",
        "smsm_find_user_field_elapsed_ms",
        "smsm_find_password_field_elapsed_ms",
        "smsm_fill_credentials_elapsed_ms",
        "smsm_find_login_button_elapsed_ms",
        "smsm_submit_login_elapsed_ms",
        "smsm_wait_login_complete_elapsed_ms",
    ):
        assert isinstance(values[key], int)
    assert all(not isinstance(value, (dict, list)) for _key, value in trace)


def test_three_credentials_are_sent_to_distinct_fields_in_order(monkeypatch):
    handler, _logger, browser, trace = run_login(monkeypatch)

    assert invoke(handler, trace) is None
    assert browser.elements[0].sent_values == ["company_secret"]
    assert browser.elements[1].sent_values == ["user_secret"]
    assert browser.elements[2].sent_values == ["password_secret"]
    values = trace_values(trace)
    assert values["company_and_password_fields_distinct"] is True
    assert values["user_and_password_fields_distinct"] is True
    assert values["company_field_length_match"] is True
    assert values["user_field_length_match"] is True
    assert values["password_field_length_match"] is True


def test_login_fields_use_exact_ids_and_exclude_hidden_and_checkbox_candidates():
    company = SelectorElement("user_company_code", "user[company_code]")
    user = SelectorElement("user_login", "user[login]")
    password = SelectorElement("user_password", "user[password]", "password")
    elements = [
        SelectorElement("authenticity_token", "authenticity_token", displayed=False),
        SelectorElement("remember_me", "remember_me", "hidden", displayed=False),
        SelectorElement("remember_me", "remember_me", "checkbox"),
        company,
        user,
        password,
    ]
    handler = selector_handler(elements)

    assert handler._find_company_field() is company
    assert handler._find_user_field() is user
    assert handler._find_unique_password_field() is password


def test_login_fields_fallback_to_exact_names_when_ids_are_missing():
    company = SelectorElement("", "user[company_code]")
    user = SelectorElement("", "user[login]")
    password = SelectorElement("", "user[password]", "password")
    handler = selector_handler([company, user, password])

    assert handler._find_company_field() is company
    assert handler._find_user_field() is user
    assert handler._find_unique_password_field() is password


def test_duplicate_exact_login_candidates_fail_without_using_first_element():
    first = SelectorElement("user_login", "user[login]")
    second = SelectorElement("user_login", "user[login]")
    handler = selector_handler([first, second])

    with pytest.raises(RuntimeError):
        handler._find_user_field()
    assert first.sent_values == []
    assert second.sent_values == []


def test_invalid_login_field_is_rejected_before_any_value_is_sent():
    company = SelectorElement("user_company_code", "user[company_code]", readonly=True)
    user = SelectorElement("user_login", "user[login]")
    password = SelectorElement("user_password", "user[password]", "password")
    handler = selector_handler([company, user, password])

    with pytest.raises(RuntimeError):
        handler._find_company_field()
    assert company.sent_values == []
    assert user.sent_values == []
    assert password.sent_values == []


def test_powershell_password_is_rejected_before_browser(monkeypatch):
    config = SmsmConfig(
        url="https://smsm.test.invalid",
        company_code="company_secret",
        username="user_secret",
        password="PowerShell -Command Read-Host",
        source="test",
        valid=True,
    )
    handler, _logger, browser, trace = run_login(monkeypatch, config=config)

    assert invoke(handler, trace) is not None
    assert browser.open_calls == []
    values = trace_values(trace)
    assert values["login_submit_blocked"] is True
    assert values["credential_mapping_valid"] is False


def test_serial_dom_inspection_is_preselection_only_without_input_or_click(monkeypatch):
    class Option:
        def __init__(self, text):
            self.text = text

    class Element:
        def __init__(self, tag, element_id="", element_type="", options=None):
            self.tag_name = tag
            self.attributes = {"id": element_id, "type": element_type}
            self.displayed = True
            self.options = options or []
            self.selected = self.options[0] if self.options else None
            self.select_calls = 0
            self.sent_values = []
            self.click_calls = 0

        def get_attribute(self, name):
            return self.attributes.get(name)

        def is_displayed(self):
            return self.displayed

        def is_enabled(self):
            return True

        def clear(self):
            self.sent_values.append("clear")

        def send_keys(self, value):
            self.sent_values.append(value)

        def click(self):
            self.click_calls += 1

    serial_option = Option("シリアル番号")
    select_element = Element("select", options=[Option("IMEI"), serial_option])
    page_input = Element("input", "manual_page_input_assets", "text")
    checkbox = Element("input", element_type="checkbox")
    radio = Element("input", element_type="radio")
    hidden = Element("input", element_type="hidden")
    dynamic_input = Element("input", "asset_serial", "text")
    search_button = Element("button")

    class Driver:
        def __init__(self):
            self.inputs = [page_input, checkbox, radio, hidden]

        def find_elements(self, _by, value):
            if value == "select":
                return [select_element]
            if value == "input":
                return list(self.inputs)
            if value == "button, input[type='submit']":
                return [search_button]
            if value.startswith("label"):
                return []
            return []

    driver = Driver()

    class FakeSelect:
        def __init__(self, element):
            self.element = element
            self.options = element.options

        def select_by_visible_text(self, text):
            self.element.select_calls += 1
            self.element.selected = next(option for option in self.options if option.text == text)
            driver.inputs.append(dynamic_input)

        @property
        def first_selected_option(self):
            return self.element.selected

    monkeypatch.setattr("app.smsm_handler.Select", FakeSelect)
    browser = type("Browser", (), {"driver": driver})()
    handler = SmsmHandler(
        browser=browser,
        logger=type("Logger", (), {"info": lambda *_args: None})(),
        smsm_config=SmsmConfig(
            url="https://smsm.test.invalid",
            company_code="c",
            username="u",
            password="p",
            source="test",
            valid=True,
        ),
    )
    trace = {}
    summary, schema = handler.inspect_serial_search_dom(trace.__setitem__)

    assert select_element.select_calls == 0
    assert summary["search_type_control_count"] == 1
    assert summary["serial_option_count"] == 1
    assert summary["input_count_before_selection"] == 0
    assert summary["input_count_after_selection"] == 0
    assert summary["serial_input_candidate_count"] == 0
    assert summary["serial_input_unique"] is False
    assert all(not element.sent_values for element in (page_input, checkbox, radio, hidden, dynamic_input))
    assert search_button.click_calls == 0
    assert [item["element_index"] for item in schema] == list(range(len(schema)))
    assert all(key not in item for item in schema for key in ("value", "text", "placeholder"))
    assert "serial_option_selected" not in trace
    assert "serial_selection_verified" not in trace


def test_common_search_enumerator_scans_top_iframe_custom_ui_and_retries_stale(monkeypatch):
    class Element:
        def __init__(self, tag, element_id=""):
            self.tag_name = tag
            self.attributes = {"id": element_id, "type": ""}

        def get_attribute(self, name):
            return self.attributes.get(name)

        def is_displayed(self):
            return True

        def is_enabled(self):
            return True

    select = Element("select", "search_type")
    iframe = Element("iframe", "search_frame")

    class FakeSelect:
        def __init__(self, element):
            self.options = [object()]

    class Switcher:
        def __init__(self):
            self.in_frame = False

        def frame(self, _frame):
            self.in_frame = True

        def default_content(self):
            self.in_frame = False

    class Driver:
        def __init__(self):
            self.switch_to = Switcher()
            self.select_calls = 0

        def find_elements(self, _by, value):
            if value == "iframe":
                return [iframe]
            if value == "select":
                self.select_calls += 1
                if self.select_calls == 1:
                    from selenium.common.exceptions import StaleElementReferenceException
                    raise StaleElementReferenceException("stale")
                return [select]
            if value == "[role='combobox'], [aria-haspopup='listbox']":
                return [Element("div", "custom_search")] if self.switch_to.in_frame else []
            if value.startswith("label"):
                return []
            return []

    monkeypatch.setattr("app.smsm_handler.Select", FakeSelect)
    handler = SmsmHandler.__new__(SmsmHandler)
    driver = Driver()
    handler.browser = type("Browser", (), {"driver": driver})()
    observation = handler.enumerate_search_controls(driver)

    assert observation["top_document_select_count"] == 1
    assert observation["iframe_count"] == 1
    assert observation["iframe_with_select_count"] == 1
    assert observation["native_select_count"] == 2
    assert observation["custom_select_candidate_count"] == 1
    assert observation["stale_retry_count"] == 1
    assert observation["search_type_control_unique"] is False
    assert all("text" not in item and "value" not in item for item in observation["schema"])


def test_preselection_schema_writer_allows_only_safe_structure(tmp_path):
    path = tmp_path / "preselection.json"
    mod = __import__("diagnose_smsm_single_target_lookup")
    mod._write_serial_search_dom_schema(path, [{
        "element_index": 0,
        "document_context": "top",
        "iframe_index": -1,
        "tag": "select",
        "id": "search_type",
        "value": "secret",
        "text": "シリアル番号",
        "placeholder": "secret",
        "option_count": 2,
    }])
    saved = __import__("json").loads(path.read_text(encoding="utf-8"))
    assert saved[0]["element_index"] == 0
    assert "value" not in saved[0]
    assert "text" not in saved[0]
    assert "placeholder" not in saved[0]


def test_custom_page_stability_requires_three_identical_snapshots(monkeypatch):
    handler = SmsmHandler.__new__(SmsmHandler)
    handler.browser = type("Browser", (), {"driver": object()})()
    observations = [
        {"stability_snapshot": (1, 1, "interactive", 1)},
        {"stability_snapshot": (1, 1, "complete", 1)},
        {"stability_snapshot": (1, 1, "complete", 1)},
        {"stability_snapshot": (1, 1, "complete", 1)},
    ]
    calls = []

    def enumerate_controls(_driver):
        calls.append(True)
        return observations.pop(0)

    monkeypatch.setattr(handler, "enumerate_custom_search_controls", enumerate_controls)
    monkeypatch.setattr("app.smsm_handler.time.sleep", lambda _seconds: None)
    result = handler.wait_for_device_page_stable(timeout=1, trace=lambda key, value: calls.append((key, value)))

    assert result["stability_snapshot"] == (1, 1, "complete", 1)
    assert len([item for item in calls if item is True]) == 4
