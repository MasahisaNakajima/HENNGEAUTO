from __future__ import annotations

import pytest

import app.smsm_handler as smsm_handler_module
from app.smsm_handler import SmsmHandler
from app.smsm_config import SmsmConfig


class Form:
    pass


class Element:
    def __init__(self, element_id, *, tag="button", text="検索", disabled=None, form=None):
        self.tag_name = tag
        self.text = text
        self.form = form or Form()
        self.click_calls = 0
        self.attributes = {
            "id": element_id,
            "type": "submit" if tag == "input" else "button",
            "disabled": disabled,
        }
        self.on_click = None

    def get_attribute(self, name):
        return self.attributes.get(name)

    def is_displayed(self):
        return True

    def is_enabled(self):
        return self.attributes.get("disabled") is None

    def find_element(self, _by, _value):
        return self.form

    def click(self):
        self.click_calls += 1
        if self.on_click:
            self.on_click()


class Row:
    tag_name = "tr"
    text = "sensitive row contents"

    def is_displayed(self):
        return True


class EmptyState:
    def is_displayed(self):
        return True


class DomNode:
    def __init__(self, tag, *, attrs=None, displayed=True, children=None, text=""):
        self.tag_name = tag
        self.attrs = attrs or {}
        self.displayed = displayed
        self.children = children or {}
        self.text = text
        self.parent = None
        for values in self.children.values():
            for child in values:
                child.parent = self

    def get_attribute(self, name):
        return self.attrs.get(name)

    def is_displayed(self):
        return self.displayed

    def is_enabled(self):
        return True

    def find_element(self, _by, value):
        if value == "./..":
            return self.parent
        raise LookupError(value)

    def find_elements(self, _by, value):
        return list(self.children.get(value, []))


class ResultDomDriver:
    def __init__(self, tables, buttons=None):
        self.tables = tables
        self.buttons = buttons or []

    def find_elements(self, _by, value):
        if value == "table":
            return self.tables
        if value == "button, input[type='submit']":
            return self.buttons
        return []


class Driver:
    def __init__(self, buttons, *, result="rows"):
        self.buttons = buttons
        self.rows = []
        self.empty = False
        self.result = result
        for button in buttons:
            button.on_click = self.submit

    def submit(self):
        if self.result == "rows":
            self.rows = [Row()]
        elif self.result == "empty":
            self.empty = True

    def execute_script(self, *_args):
        raise AssertionError("JavaScript must not be used for search submission")

    def find_elements(self, _by, value):
        if value == "button, input[type='submit']":
            return self.buttons
        if value == "table tbody tr":
            return self.rows
        if value in {"table", "[data-testid*='result' i]", "[data-testid*='device' i]"}:
            return [object()]
        if value in {"[data-testid*='empty' i]", "[data-testid*='no-result' i]", "[data-testid*='no_result' i]", ".no-results", ".no-result"}:
            return [EmptyState()] if self.empty else []
        return []


def handler_for(driver):
    browser = type("Browser", (), {"driver": driver})()
    logger = type("Logger", (), {"info": lambda *_args: None, "error": lambda *_args: None})()
    config = SmsmConfig(url="https://example.invalid", company_code="", username="", password="", source="test", valid=False)
    return SmsmHandler(browser=browser, logger=logger, smsm_config=config)


class SearchInput:
    def __init__(self):
        self.value = ""
        self.clear_calls = 0
        self.send_keys_calls = 0

    def get_attribute(self, name):
        return self.value if name == "value" else None

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def clear(self):
        self.clear_calls += 1
        self.value = ""

    def send_keys(self, value):
        self.send_keys_calls += 1
        self.value = value


class SearchButton:
    def __init__(self):
        self.click_calls = 0

    def click(self):
        self.click_calls += 1


def configured_search_handler(input_count=1, button_count=1, exact_count=1):
    handler = handler_for(type("SearchDriver", (), {})())
    inputs = [SearchInput() for _ in range(input_count)]
    buttons = [SearchButton() for _ in range(button_count)]
    handler.logger = type("Logger", (), {"info": lambda *_args: None})()
    handler._select_serial_search_type = lambda: None
    handler._serial_input_candidates = lambda _driver: inputs
    handler._search_button_candidates = lambda: buttons
    handler._search_button_is_safe = lambda _button: True
    handler.browser.wait_for_page_ready = lambda: None
    handler.count_exact_device_serial_results = lambda _serial: exact_count
    handler._serial_search_results_dom_snapshot = lambda _driver, before_signature=None: {
        "result_table_count": 0, "tbody_count": 0, "visible_row_count": 0,
        "checkbox_row_count": 0, "empty_state_count": 0, "loading_count": 0,
        "pagination_count": 0, "result_dom_changed": False, "result_table_unique": False,
        "result_rows_scoped_to_table": False, "result_count": -1, "schema": [],
        "signature": (), "result_rows": [], "result_headers": [],
    }
    handler._observe_serial_search_after_submit = lambda _before, _value: {
        "device_search_result_container_count": 1,
        "device_search_result_row_candidate_count": exact_count,
        "device_search_visible_result_row_count": exact_count,
        "device_search_post_result_visible_row_count": exact_count,
        "device_search_result_total_count": exact_count,
        "device_search_result_page_count": 1,
        "device_search_result_transition_verified": True,
        "device_result_candidate_count": exact_count,
        "device_result_candidate_unique": exact_count == 1,
        "device_result_identity_verified": False,
        "device_search_serial_cell_candidate_count": exact_count,
        "device_search_serial_cell_nonblank_count": exact_count,
        "exact_match_count": exact_count,
        "device_search_zero_result_indicator_found": False,
        "device_search_result_collection_method": "unique_result_table_serial_column",
        "device_search_result_stable": True,
        "device_search_count_failed_phase": "completed",
        "device_search_count_exception_type": "",
    }
    return handler, inputs, buttons


class DeviceListDriver:
    def __init__(self, *, navigation=None, pathname="/devices", inputs=1, buttons=1, main_visible=True):
        self.navigation = navigation or []
        self.current_url = f"https://smsm.example{pathname}"
        self.main_visible = main_visible
        self.inputs = [SearchInput() for _ in range(inputs)]
        self.buttons = [Element("device_search_button", text="検索") for _ in range(buttons)]
        for button in self.buttons:
            button.form = Form()

    def find_elements(self, _by, value):
        if value.startswith("//"):
            return self.navigation
        if value == "input, textarea":
            return self.inputs
        if value == "button, input[type='submit']":
            return self.buttons
        if value in {"main", "[role='main']", "[data-testid*='device' i]", "[data-testid*='terminal' i]", "section"}:
            return [VisibleNode()] if self.main_visible else []
        return []


class VisibleNode:
    def is_displayed(self):
        return True


def device_list_handler(*, navigation=None, pathname="/devices", inputs=1, buttons=1, main_visible=True):
    driver = DeviceListDriver(navigation=navigation, pathname=pathname, inputs=inputs, buttons=buttons, main_visible=main_visible)
    handler = handler_for(driver)
    handler.logger = type("Logger", (), {"info": lambda *_args: None})()
    for element in driver.navigation:
        element.on_click = lambda driver=driver: setattr(driver, "current_url", "https://smsm.example/devices")
    return handler, driver


def test_device_list_already_active_succeeds_without_click():
    active = navigation_element(element_id="devices", text="端末", href="/devices")
    handler, _driver = device_list_handler(navigation=[active])

    result = handler.reach_device_search_page()

    assert result["device_list_page_verified"] is True
    assert result["device_list_nav_click_count"] == 0
    assert result["device_list_pathname_matches"] is True


def test_device_list_unique_navigation_clicks_at_most_once():
    device = navigation_element(element_id="devices", text="端末", href="/devices")
    handler, _driver = device_list_handler(navigation=[device], pathname="/home")

    result = handler.reach_device_search_page()

    assert result["device_list_page_verified"] is True
    assert result["device_list_nav_unique"] is True
    assert result["device_list_nav_click_count"] == 1


def test_device_list_without_navigation_candidate_can_verify_current_dom():
    handler, _driver = device_list_handler(navigation=[])

    result = handler.reach_device_search_page()

    assert result["device_list_page_verified"] is True
    assert result["device_list_nav_candidate_count"] == 0
    assert result["device_list_nav_click_count"] == 0


def test_device_list_main_container_false_does_not_block_verified_page():
    handler, _driver = device_list_handler(main_visible=False)

    result = handler.reach_device_search_page()

    assert result["device_list_condition_pathname_matches"] is True
    assert result["device_list_condition_search_input_unique"] is True
    assert result["device_list_condition_search_button_unique"] is True
    assert result["device_list_condition_main_container_visible"] is False
    assert result["device_list_page_verified"] is True
    assert result["device_list_navigation_completed"] is True
    assert result["device_list_failed_phase"] == "completed"


def test_current_device_list_pathname_false_reports_verify_phase():
    handler, _driver = device_list_handler(pathname="/")

    with pytest.raises(RuntimeError) as error:
        handler.inspect_device_list_page()

    assert getattr(error.value, "failed_phase", "verify_device_list_pathname") == "verify_device_list_pathname"


def test_device_list_multiple_navigation_candidates_fail_when_path_is_unconfirmed():
    navigation = [
        navigation_element(element_id="devices-a", text="端末", href="/devices-a"),
        navigation_element(element_id="devices-b", text="端末", href="/devices-b"),
    ]
    handler, _driver = device_list_handler(navigation=navigation, pathname="/home")

    with pytest.raises(RuntimeError):
        handler.reach_device_search_page()


@pytest.mark.parametrize("inputs,buttons", [(0, 1), (2, 1), (1, 0), (1, 2)])
def test_device_list_requires_one_search_input_and_button(inputs, buttons):
    handler, _driver = device_list_handler(inputs=inputs, buttons=buttons)

    with pytest.raises(RuntimeError):
        handler.reach_device_search_page()


def test_serial_search_target_missing_never_sends_input():
    handler, inputs, buttons = configured_search_handler()

    with pytest.raises(ValueError):
        handler.search_device("", page_reached=True)

    assert all(item.send_keys_calls == 0 for item in inputs)
    assert all(item.click_calls == 0 for item in buttons)


def test_serial_search_page_navigation_failure_never_sends_input():
    handler, inputs, buttons = configured_search_handler()
    handler.reach_device_search_page = lambda: (_ for _ in ()).throw(RuntimeError("not reached"))

    with pytest.raises(RuntimeError):
        handler.search_device("target-serial", page_reached=False)

    assert all(item.send_keys_calls == 0 for item in inputs)
    assert all(item.click_calls == 0 for item in buttons)


@pytest.mark.parametrize("input_count", [0, 2])
def test_serial_search_non_unique_input_never_sends_or_submits(input_count):
    handler, inputs, buttons = configured_search_handler(input_count=input_count)

    with pytest.raises(RuntimeError):
        handler.search_device("target-serial", page_reached=True)

    assert all(item.send_keys_calls == 0 for item in inputs)
    assert all(item.click_calls == 0 for item in buttons)


@pytest.mark.parametrize("exact_count", [0, 2])
def test_serial_search_non_unique_exact_result_stops_after_one_submit(exact_count):
    handler, inputs, buttons = configured_search_handler(exact_count=exact_count)

    with pytest.raises(RuntimeError):
        handler.search_device("target-serial", page_reached=True)

    assert inputs[0].send_keys_calls == 1
    assert buttons[0].click_calls == 1


def test_serial_search_unique_controls_and_exact_result_submit_once():
    handler, inputs, buttons = configured_search_handler()

    result = handler.search_device("target-serial", page_reached=True)

    assert result["device_search_input_candidate_count"] == 1
    assert result["device_search_button_candidate_count"] == 1
    assert result["device_search_exact_match_count"] == 1
    assert inputs[0].send_keys_calls == 1
    assert buttons[0].click_calls == 1


def test_serial_search_records_fixed_metrics_and_clears_once():
    handler, inputs, buttons = configured_search_handler()
    handler._select_serial_search_type = lambda: {"already_selected": True, "click_count": 0}
    trace = {}

    result = handler.search_device("target-serial", trace=trace.__setitem__, page_reached=True)

    assert inputs[0].clear_calls == 1
    assert trace["device_search_type_selection_called"] is True
    assert trace["device_search_type_already_selected"] is True
    assert trace["device_search_type_click_count"] == 0
    assert trace["device_search_dom_reobserve_called"] is True
    assert trace["device_search_dom_reobserve_completed"] is True
    assert trace["device_search_target_present"] is True
    assert trace["device_search_page_verified"] is True
    assert trace["device_search_input_candidate_count"] == 1
    assert trace["device_search_button_candidate_count"] == 1
    assert trace["device_search_send_keys_called"] is True
    assert trace["device_search_send_keys_count"] == 1
    assert trace["device_search_submit_called"] is True
    assert trace["device_search_submit_count"] == 1
    assert trace["device_search_wait_called"] is True
    assert trace["device_search_wait_completed"] is True
    assert trace["device_search_failed_phase"] == "completed"
    assert result["device_search_exact_match_count"] == 1


class CustomSearchNode:
    def __init__(self, tag, *, attrs=None, text="", displayed=True, enabled=True):
        self.tag_name = tag
        self.attrs = attrs or {}
        self.text = text
        self.displayed = displayed
        self.enabled = enabled
        self.parent = None
        self.children = []
        self.click_calls = 0
        self.on_click = None

    def get_attribute(self, name):
        return self.attrs.get(name)

    def is_displayed(self):
        return self.displayed

    def is_enabled(self):
        return self.enabled

    def find_element(self, _by, value):
        if value == "./..":
            return self.parent
        raise LookupError(value)

    def find_elements(self, _by, value):
        if value == "option":
            return []
        if value in {"[role='option'], option, [data-value], [data-option]", "[role='option']", "[data-value], [data-option]"}:
            return list(self.children)
        if value == "button":
            return []
        if value == "[role='listbox']":
            return [child for child in self.children if child.get_attribute("role") == "listbox"]
        return []

    def click(self):
        self.click_calls += 1
        if self.on_click:
            self.on_click()


class CustomSearchDriver:
    def __init__(self, *, selected=False, control_count=1, option_count=1):
        self.form = CustomSearchNode("form", attrs={"id": "device-search-form", "class": "search-form"})
        self.native = CustomSearchNode(
            "select",
            attrs={"id": "search_item", "name": "target", "disabled": "true"},
            displayed=False,
            enabled=False,
        )
        self.controls = []
        self.listbox = CustomSearchNode("div", attrs={"role": "listbox"}, displayed=False)
        self.form.children = [self.native]
        self.native.parent = self.form
        for index in range(control_count):
            control = CustomSearchNode(
                "span",
                attrs={"id": "search_item-button" if index == 0 else f"search_item-button-{index}", "role": "combobox", "aria-haspopup": "listbox", "aria-expanded": "false"},
                text="シリアル番号" if selected and index == 0 else "IMEI",
            )
            control.parent = self.form
            control.on_click = lambda control=control: self.open_control(control)
            self.controls.append(control)
            self.form.children.append(control)
        self.options = [CustomSearchNode("li", attrs={"role": "option"}, text="シリアル番号") for _ in range(option_count)]
        for option in self.options:
            option.parent = self.listbox
            option.on_click = lambda option=option: self.select_option(option)
        self.listbox.children = self.options
        self.listbox.parent = self.form
        self.selected = selected

    def open_control(self, control):
        self.listbox.displayed = True
        control.attrs["aria-expanded"] = "true"

    def select_option(self, _option):
        self.selected = True
        self.listbox.displayed = False
        self.controls[0].text = "シリアル番号"
        self.controls[0].attrs["aria-expanded"] = "false"

    def execute_script(self, script, *_args):
        if "readyState" in script:
            return "complete"
        return {"same_parent": True, "custom_immediately_after_native": True}

    def find_elements(self, _by, value):
        if value == "select":
            return [self.native]
        if value == "[role='combobox'], [aria-haspopup='listbox']":
            return self.controls
        if value == "[role='listbox']":
            return [self.listbox]
        if value in {"[role='option'], option, [data-value], [data-option]", "[role='option']", "[data-value], [data-option]"}:
            return self.options if self.listbox.displayed else []
        if value == "form":
            return [self.form]
        return []


def custom_search_handler(*, selected=False, control_count=1, option_count=1):
    driver = CustomSearchDriver(selected=selected, control_count=control_count, option_count=option_count)
    handler = handler_for(driver)
    return handler, driver


def test_serial_search_type_custom_ui_selects_once_with_hidden_disabled_native_backing():
    handler, driver = custom_search_handler()

    result = handler._select_serial_search_type()

    assert result["already_selected"] is False
    assert result["click_count"] == 1
    assert driver.controls[0].click_calls == 1
    assert driver.options[0].click_calls == 1
    assert result["device_search_type_option_candidate_count"] == 1


def test_serial_search_type_custom_label_already_selected_uses_zero_clicks():
    handler, driver = custom_search_handler(selected=True)

    result = handler._select_serial_search_type()

    assert result["already_selected"] is True
    assert result["click_count"] == 0
    assert driver.controls[0].click_calls == 0


@pytest.mark.parametrize("control_count,option_count", [(0, 1), (2, 1), (1, 0), (1, 2)])
def test_serial_search_type_ambiguous_custom_ui_stops_before_any_click(control_count, option_count):
    handler, driver = custom_search_handler(control_count=control_count, option_count=option_count)

    with pytest.raises(RuntimeError):
        handler._select_serial_search_type()

    expected_control_clicks = 1 if control_count == 1 else 0
    assert all(control.click_calls == expected_control_clicks for control in driver.controls)
    assert all(option.click_calls == 0 for option in driver.options)


def test_serial_search_blank_after_send_does_not_click_button():
    handler, inputs, buttons = configured_search_handler()
    inputs[0].send_keys = lambda _value: None
    trace = {}

    with pytest.raises(RuntimeError):
        handler.search_device("target-serial", trace=trace.__setitem__, page_reached=True)

    assert inputs[0].clear_calls == 1
    assert inputs[0].send_keys_calls == 0
    assert buttons[0].click_calls == 0
    assert trace["device_search_send_keys_called"] is True
    assert trace["device_search_submit_count"] == 0
    assert trace["device_search_failed_phase"] == "set_device_search_serial"


def test_serial_search_wait_failure_keeps_exact_count_unknown():
    handler, inputs, buttons = configured_search_handler()
    handler.browser.wait_for_page_ready = lambda: (_ for _ in ()).throw(RuntimeError("wait failed"))
    trace = {}

    with pytest.raises(RuntimeError):
        handler.search_device("target-serial", trace=trace.__setitem__, page_reached=True)

    assert inputs[0].clear_calls == 1
    assert buttons[0].click_calls == 1
    assert trace["device_search_wait_called"] is True
    assert trace["device_search_wait_completed"] is False
    assert trace["device_search_exact_match_count"] is None
    assert trace["device_search_failed_phase"] == "wait_device_search_results"


def test_device_list_and_search_use_the_same_resolvers_on_current_dom():
    handler, _driver = device_list_handler()
    input_calls = []
    button_calls = []
    original_input_resolver = handler._serial_input_candidates
    original_button_resolver = handler._search_button_candidates
    handler._serial_input_candidates = lambda driver: (input_calls.append(driver) or original_input_resolver(driver))
    handler._search_button_candidates = lambda: (button_calls.append(True) or original_button_resolver())
    handler._select_serial_search_type = lambda: {"already_selected": True, "click_count": 0}
    handler.count_exact_device_serial_results = lambda _serial: 1
    handler._serial_search_results_dom_snapshot = lambda _driver, before_signature=None: {
        "result_table_count": 0, "tbody_count": 0, "visible_row_count": 0,
        "checkbox_row_count": 0, "empty_state_count": 0, "loading_count": 0,
        "pagination_count": 0, "result_dom_changed": False, "result_table_unique": False,
        "result_rows_scoped_to_table": False, "result_count": -1, "schema": [],
        "signature": (), "result_rows": [], "result_headers": [],
    }
    handler._observe_serial_search_after_submit = lambda _before, _value: {
        "device_search_result_container_count": 1,
        "device_search_result_row_candidate_count": 1,
        "device_search_visible_result_row_count": 1,
        "device_search_serial_cell_candidate_count": 1,
        "device_search_serial_cell_nonblank_count": 1,
        "exact_match_count": 1,
        "device_search_zero_result_indicator_found": False,
        "device_search_result_collection_method": "unique_result_table_serial_column",
        "device_search_result_stable": True,
        "device_search_count_failed_phase": "completed",
        "device_search_count_exception_type": "",
    }

    handler.reach_device_search_page()
    handler.logger = type("Logger", (), {"info": lambda *_args: None})()
    handler.browser.wait_for_page_ready = lambda: None
    result = handler.search_device("target-serial", page_reached=True)

    assert result["device_search_input_candidate_count"] == 1
    assert result["device_search_button_candidate_count"] == 1
    assert len(input_calls) >= 2
    assert len(button_calls) >= 2


def test_serial_search_reobserves_after_one_type_selection_click():
    handler, inputs, buttons = configured_search_handler()
    input_calls = 0
    button_calls = 0

    def input_resolver(_driver):
        nonlocal input_calls
        input_calls += 1
        return [] if input_calls == 1 else inputs

    def button_resolver():
        nonlocal button_calls
        button_calls += 1
        return [] if button_calls == 1 else buttons

    handler._serial_input_candidates = input_resolver
    handler._search_button_candidates = button_resolver
    handler._select_serial_search_type = lambda: {"already_selected": False, "click_count": 1}
    handler.count_exact_device_serial_results = lambda _serial: 1
    trace = {}

    result = handler.search_device("target-serial", trace=trace.__setitem__, page_reached=True)

    assert result["device_search_type_selection_called"] is True
    assert result["device_search_type_already_selected"] is False
    assert result["device_search_type_click_count"] == 1
    assert result["device_search_dom_reobserve_called"] is True
    assert result["device_search_dom_reobserve_completed"] is True
    assert result["device_search_send_keys_count"] == 1
    assert result["device_search_submit_count"] == 1


def test_serial_search_reobserve_timeout_never_sends_or_submits():
    handler, inputs, buttons = configured_search_handler()
    handler._select_serial_search_type = lambda: {"already_selected": False, "click_count": 1}
    handler._serial_input_candidates = lambda _driver: []
    handler._search_button_candidates = lambda: []
    trace = {}

    with pytest.raises(RuntimeError) as error:
        handler.search_device("target-serial", trace=trace.__setitem__, page_reached=True)

    assert getattr(error.value, "failed_phase") == "wait_device_search_controls_after_type_selection"
    assert trace["device_search_dom_reobserve_called"] is True
    assert trace["device_search_dom_reobserve_completed"] is False
    assert trace["device_search_input_candidate_count"] == 0
    assert trace["device_search_send_keys_count"] == 0
    assert trace["device_search_submit_count"] == 0
    assert inputs[0].send_keys_calls == 0
    assert buttons[0].click_calls == 0


class NavigationDriver:
    def __init__(self, elements):
        self.elements = elements

    def find_elements(self, _by, _value):
        return self.elements


def navigation_element(*, element_id=None, text="", role="link", href=None, displayed=True):
    element = Element(element_id or "", text=text)
    element.attributes.update({"role": role, "href": href})
    element.displayed = displayed
    element.is_displayed = lambda: element.displayed
    return element


@pytest.mark.parametrize(
    "elements",
    [
        [navigation_element(element_id="settings", text="設定"), navigation_element(element_id="settings-copy", text="設定")],
        [navigation_element(element_id="settings-menu", text="設定を開く")],
    ],
)
def test_certificate_navigation_does_not_choose_multiple_or_partial_candidates(elements):
    handler = handler_for(NavigationDriver(elements))

    candidate, count = handler._find_diagnostic_navigation_element("settings")

    assert candidate is None
    assert count != 1


def test_certificate_navigation_prefers_exact_attribute_and_rejects_unrelated_text():
    exact = navigation_element(element_id="settings", text="別の表示")
    unrelated = navigation_element(element_id="other", text="設定を開く")
    handler = handler_for(NavigationDriver([exact, unrelated]))

    candidate, count = handler._find_diagnostic_navigation_element("settings")

    assert candidate is exact
    assert count == 1


def test_search_button_is_safe_and_clicked_once_without_javascript_or_enter():
    button = Element("device_search_button")
    driver = Driver([button])
    trace = []

    count = handler_for(driver).search_serial_results_for_diagnostic(lambda key, value: trace.append((key, value)))

    assert count == 1
    assert button.click_calls == 1
    values = dict(trace)
    assert values["search_button_candidate_count"] == 1
    assert values["search_button_unique"] is True
    assert values["search_button_safe"] is True
    assert values["search_button_click_called"] is True
    assert values["search_submitted"] is True
    assert values["lookup_result_count"] == 1


@pytest.mark.parametrize(
    "buttons",
    [
        [Element("device_search_one"), Element("device_search_two")],
        [Element("device_search_save", text="検索保存")],
        [Element("device_search_update", text="検索更新")],
        [Element("device_search_disabled", disabled="true")],
    ],
)
def test_unsafe_or_non_unique_search_button_is_never_clicked(buttons):
    driver = Driver(buttons)
    trace = []

    with pytest.raises(RuntimeError):
        handler_for(driver).search_serial_results_for_diagnostic(lambda key, value: trace.append((key, value)))

    assert all(button.click_calls == 0 for button in buttons)
    values = dict(trace)
    assert values["search_button_click_called"] is False
    assert values["search_submitted"] is False
    assert values["search_button_safe"] is False


def test_explicit_empty_results_are_zero():
    button = Element("device_search_button")
    driver = Driver([button], result="empty")

    assert handler_for(driver).search_serial_results_for_diagnostic() == 0
    assert button.click_calls == 1


def test_unconfirmed_results_timeout_is_failure():
    button = Element("device_search_button")
    driver = Driver([button], result="unconfirmed")
    handler = handler_for(driver)
    button.click()

    with pytest.raises(RuntimeError, match="タイムアウト"):
        handler._wait_for_serial_search_results(timeout=0)
    assert button.click_calls == 1


def test_result_snapshot_excludes_non_data_rows_and_rejects_multiple_tables():
    def row(*, attrs=None, displayed=True, data=True, header=False, checkbox=False):
        children = {
            "td": [DomNode("td")] if data else [],
            "th": [DomNode("th")] if header else [],
            "input[type='checkbox'], [role='checkbox']": [DomNode("input", attrs={"type": "checkbox"})] if checkbox else [],
            "a": [],
        }
        return DomNode("tr", attrs=attrs, displayed=displayed, children=children)

    result_rows = [
        row(header=True),
        row(displayed=False),
        row(attrs={"class": "empty-state"}),
        row(attrs={"class": "pagination"}),
        row(attrs={"class": "loading"}),
        row(checkbox=True),
        row(checkbox=True),
    ]
    result_table = DomNode("table", children={"tbody": [DomNode("tbody", children={"tr": result_rows})]})
    unrelated_table = DomNode("table", children={"tbody": [DomNode("tbody", children={"tr": [row(checkbox=True)]})]})
    driver = ResultDomDriver([result_table, unrelated_table])
    handler = handler_for(driver)

    snapshot = handler._serial_search_results_dom_snapshot(driver)

    assert snapshot["result_table_count"] == 2
    assert snapshot["visible_row_count"] == 3
    assert snapshot["checkbox_row_count"] == 3
    assert snapshot["empty_state_count"] == 1
    assert snapshot["loading_count"] == 1
    assert snapshot["pagination_count"] == 1
    assert snapshot["result_table_unique"] is False
    assert snapshot["result_count"] == -1
    assert all("text" not in item for table in snapshot["schema"] for item in table["rows"])


def test_result_column_headers_require_exact_allowed_names():
    assert SmsmHandler._map_result_columns(["Serial Number", "IMEI", "Alias"])["serial_column_unique"] is True
    assert SmsmHandler._map_result_columns(["Serial Number (contains)", "IMEI", "Alias"])["serial_column_found"] is False


def _match_driver(headers, rows):
    header_nodes = [DomNode("th", text=header) for header in headers]
    row_nodes = [DomNode("tr", children={"td": [DomNode("td", text=value) for value in values], "th": [], "input[type='checkbox'], [role='checkbox']": [DomNode("input", attrs={"type": "checkbox"})], "a": []}) for values in rows]
    table = DomNode("table", children={
        "thead th": header_nodes,
        "tr th": header_nodes,
        "tbody": [DomNode("tbody", children={"tr": row_nodes})],
    })
    button = Element("device_search_button")
    driver = ResultDomDriver([], [button])
    button.on_click = lambda: setattr(driver, "tables", [table])
    return driver, button


@pytest.mark.parametrize("rows, expected", [
    (["unused"], 0),
])
def test_result_match_requires_serial_and_imei_for_unique_candidate(rows, expected):
    driver, button = _match_driver(
        ["Alias", "Serial Number", "IMEI", "C3", "C4", "C5", "C6", "C7"],
        [
            ["target_alias", "target_serial", "111111111111111", "", "", "", "", ""],
            ["other_alias", "target_serial", "222222222222222", "", "", "", "", ""],
        ],
    )
    handler = handler_for(driver)
    result = handler.match_serial_search_results_for_diagnostic({"alias": "target_alias", "serial": "target_serial", "imei": "111111111111111"})
    assert result["matched_result_count"] == 1
    assert result["unique_result_match"] is True
    assert result["result_match_unresolved"] is False
    assert button.click_calls == 1


def test_result_match_uses_alias_only_when_imei_column_is_absent():
    driver, _button = _match_driver(
        ["Alias", "Serial Number", "C3", "C4", "C5", "C6", "C7", "C8"],
        [
            ["target_alias", "target_serial", "", "", "", "", "", ""],
            ["other_alias", "target_serial", "", "", "", "", "", ""],
        ],
    )
    result = handler_for(driver).match_serial_search_results_for_diagnostic({"alias": "target_alias", "serial": "target_serial", "imei": "111111111111111"})
    assert result["matched_result_count"] == 1
    assert result["serial_and_alias_match_count"] == 1


def test_result_match_is_unresolved_without_imei_and_alias_columns():
    driver, _button = _match_driver(
        ["Serial Number", "C2", "C3", "C4", "C5", "C6", "C7", "C8"],
        [["target_serial", "", "", "", "", "", "", ""], ["target_serial", "", "", "", "", "", "", ""]],
    )
    result = handler_for(driver).match_serial_search_results_for_diagnostic({"alias": "target_alias", "serial": "target_serial", "imei": "111111111111111"})
    assert result["result_match_unresolved"] is True


def _virtualized_result_driver(headers, visible_rows, *, empty_state=False, extra_tables=0):
    """Simulate the past-successful SMSM serial-search result DOM:

    - one <table> with a <thead th> row and a <tbody> containing displayed <tr> rows.
    - 98 additional non-displayed <tr> placeholders (virtualized template rows) that
      must be filtered out by ``is_displayed`` in ``_serial_search_results_dom_snapshot``.
    Matches the shape recorded in
    ``logs/smsm_serial_search_results_dom_20260812_192627.json`` (100 tr, only 2 visible).
    """
    header_nodes = [DomNode("th", text=header) for header in headers]
    displayed_tr_nodes = []
    for values in visible_rows:
        td_nodes = [DomNode("td", text=value) for value in values]
        displayed_tr_nodes.append(
            DomNode("tr", displayed=True, children={
                "td": td_nodes, "th": [],
                "input[type='checkbox'], [role='checkbox']": [DomNode("input", attrs={"type": "checkbox"})],
                "a": [DomNode("a"), DomNode("a")],
            })
        )
    virtual_placeholders = [
        DomNode("tr", displayed=False, children={
            "td": [], "th": [],
            "input[type='checkbox'], [role='checkbox']": [], "a": [],
        })
        for _ in range(98)
    ]
    all_rows = displayed_tr_nodes + virtual_placeholders
    tbody = DomNode("tbody", children={"tr": all_rows})
    empty_indicators = []
    if empty_state:
        empty_indicators.append(
            DomNode("tr", attrs={"class": "empty-state"}, displayed=True, children={
                "td": [DomNode("td", text="該当なし")], "th": [],
                "input[type='checkbox'], [role='checkbox']": [], "a": [],
            })
        )
        tbody = DomNode("tbody", children={"tr": all_rows + empty_indicators})
    main_table = DomNode("table", children={
        "thead th": header_nodes,
        "tr th": header_nodes,
        "tbody": [tbody],
    })
    extras = []
    for _ in range(extra_tables):
        extra_tbody = DomNode("tbody", children={"tr": [
            DomNode("tr", displayed=True, children={
                "td": [DomNode("td", text="unrelated")], "th": [],
                "input[type='checkbox'], [role='checkbox']": [], "a": [],
            })
        ]})
        extras.append(DomNode("table", children={
            "thead th": [], "tr th": [],
            "tbody": [extra_tbody],
        }))
    return ResultDomDriver([main_table] + extras)


def test_exact_match_row_clicks_result_row_once_and_verifies_detail_serial():
    handler = _link_inspection_handler(target_links=[("", "")])
    handler.device_observation.update({
        "device_search_input_exact_match": True,
        "device_search_identity_context_verified": True,
    })
    target_row = handler.fixture_refs["target_row"]
    machine_link = handler.fixture_refs["machine"]
    target_row.click_calls = 0
    target_row.click = lambda: setattr(target_row, "click_calls", target_row.click_calls + 1)
    handler._wait_for_device_detail_panel = lambda timeout: {
        "device_detail_panel_candidate_count": 1,
        "device_detail_panel_unique": True,
        "device_detail_panel_visible": True,
    }
    result = handler.select_matched_device_row("target_serial")

    assert result["device_result_click_candidate_count"] == 1
    assert result["device_result_click_unique"] is True
    assert result["device_result_candidate_count"] == 1
    assert result["device_result_candidate_unique"] is True
    assert result["device_result_detail_column_candidate_count"] == 0
    assert result["device_result_detail_control_candidate_count"] == 0
    assert result["device_result_detail_control_unique"] is False
    assert result["device_result_click_called"] is True
    assert result["device_result_click_count"] == 1
    assert result["device_result_selected"] is True
    assert result["device_detail_navigation_wait_called"] is True
    assert result["device_detail_navigation_verified"] is True
    assert target_row.click_calls == 1
    assert machine_link.click_calls == 0


class DetailValueNode(DomNode):
    pass


def _detail_serial_handler(monkeypatch, *, labels, values, panel_count=1):
    fields = []
    for label in labels:
        field_values = [DetailValueNode("span", text=value) for value in values]
        fields.append(DomNode("dt", attrs={"data-field": "serial"}, text=label, children={
            "following-sibling::*[1]": field_values[:1],
            "input,textarea,[data-value],dd,span": field_values,
        }))

    serial_selector = "[data-field*='serial' i],[data-testid*='serial' i],[name*='serial' i],[id*='serial' i],[aria-label*='serial' i],dt,th,label"
    panels = [DomNode("aside", text="Other settings", children={serial_selector: fields}) for _ in range(panel_count)]

    class DetailDriver:
        def find_elements(self, _by, _selector):
            return panels if _selector == "aside" else []

    class ImmediateWait:
        def __init__(self, driver, *_args, **_kwargs):
            self.driver = driver

        def until(self, predicate):
            return predicate(self.driver)

    handler = handler_for(DetailDriver())
    monkeypatch.setattr(smsm_handler_module, "WebDriverWait", ImmediateWait)
    return handler


def test_detail_serial_exact_match_requires_one_label_and_one_value(monkeypatch):
    handler = _detail_serial_handler(monkeypatch, labels=["Serial Number"], values=[" target_serial "])

    result = handler._wait_for_device_detail_serial("target_serial", timeout=1)

    assert result["device_detail_serial_field_candidate_count"] == 1
    assert result["device_detail_serial_value_candidate_count"] == 1
    assert result["device_detail_serial_exact_match"] is True


@pytest.mark.parametrize(
    ("labels", "values"),
    [(["Serial Number"], ["other_serial"]), ([], ["target_serial"]), (["Serial Number", "Serial"], ["target_serial"]), (["Serial Number"], [])],
)
def test_detail_serial_mismatch_or_ambiguous_stops_identity(monkeypatch, labels, values):
    handler = _detail_serial_handler(monkeypatch, labels=labels, values=values)

    result = handler._wait_for_device_detail_serial("target_serial", timeout=1)

    assert result["device_detail_serial_exact_match"] is False


@pytest.mark.parametrize("panel_count", [0, 2])
def test_detail_panel_not_unique_stops_identity(monkeypatch, panel_count):
    handler = _detail_serial_handler(
        monkeypatch,
        labels=["Serial Number"],
        values=["target_serial"],
        panel_count=panel_count,
    )

    result = handler._wait_for_device_detail_serial("target_serial", timeout=1)

    assert result["device_detail_panel_unique"] is False
    assert result["device_detail_serial_exact_match"] is False


@pytest.mark.parametrize("row_count", [0, 2])
def test_result_row_count_not_one_does_not_click(monkeypatch, row_count):
    handler = _link_inspection_handler(target_links=[])
    rows = handler._serial_search_results_dom_snapshot(handler.browser.driver)["result_rows"]
    handler.device_observation.update({
        "device_search_input_exact_match": True,
        "device_search_identity_context_verified": True,
        "device_search_result_total_count": 1,
        "device_search_result_page_count": 1,
        "device_search_result_container_count": 1,
        "device_search_post_result_visible_row_count": row_count,
        "device_result_candidate_count": row_count,
        "device_result_candidate_unique": False,
        "device_result_identity_verified": False,
    })
    handler._serial_search_results_dom_snapshot = lambda _driver: {
        "result_table_unique": True,
        "result_rows_scoped_to_table": True,
        "result_rows": rows[:row_count],
    }
    result = handler.select_matched_device_row("target_serial")
    assert result["device_result_click_count"] == 0


def test_row_click_exception_is_not_retried():
    handler = _link_inspection_handler(target_links=[])
    row = handler._serial_search_results_dom_snapshot(handler.browser.driver)["result_rows"][0]
    clicks = []
    row.click = lambda: (clicks.append(1), (_ for _ in ()).throw(RuntimeError("click failed")))
    handler.device_observation.update({
        "device_search_input_exact_match": True,
        "device_search_identity_context_verified": True,
        "device_search_result_total_count": 1,
        "device_search_result_page_count": 1,
        "device_search_result_container_count": 1,
        "device_search_post_result_visible_row_count": 1,
        "device_result_candidate_count": 1,
        "device_result_candidate_unique": True,
        "device_result_identity_verified": False,
    })
    handler._serial_search_results_dom_snapshot = lambda _driver: {
        "result_table_unique": True,
        "result_rows_scoped_to_table": True,
        "result_rows": [row],
    }
    result = handler.select_matched_device_row("target_serial")
    assert clicks == [1]
    assert result["device_result_click_count"] == 1
    assert result["device_detail_navigation_wait_called"] is False


class SearchPageBody:
    def __init__(self, driver):
        self.driver = driver

    @property
    def text(self):
        return self.driver.body_text


class SearchPageInput(DomNode):
    def __init__(self):
        super().__init__("input", attrs={"type": "text", "id": "serial-search"})

    def clear(self):
        self.attrs["value"] = ""

    def send_keys(self, value):
        self.attrs["value"] = value


class SearchPageDriver:
    def __init__(self, pre_table, post_table):
        self.tables = [pre_table]
        self.post_table = post_table
        self.input = SearchPageInput()
        self.search_button = Element("device_search_button")
        self.search_button.on_click = self.submit
        self.submits = 0

    @property
    def body_text(self):
        if self.submits == 0:
            return "5件 1 / 2ページ"
        return f"検索条件: {self.input.get_attribute('value')} 1件 1 / 1ページ"

    def submit(self):
        self.submits += 1
        self.tables = [self.post_table]

    def find_elements(self, _by, value):
        if value == "table":
            return self.tables
        if value == "input, textarea":
            return [self.input]
        if value == "button, input[type='submit']":
            return [self.search_button]
        return []

    def find_element(self, _by, value):
        if value == "body":
            return SearchPageBody(self)
        raise LookupError(value)


def _search_result_table(headers, row_values):
    header_nodes = [DomNode("th", text=header) for header in headers]
    row_cells = [DomNode("td", text=value) for value in row_values]
    row = DomNode("tr", children={
        "td": row_cells,
        "th": [],
        "input[type='checkbox'], [role='checkbox']": [DomNode("input", attrs={"type": "checkbox"})],
        "a": [],
    })
    tbody = DomNode("tbody", children={"tr": [row]})
    return DomNode("table", children={
        "thead th": header_nodes,
        "tr th": header_nodes,
        "tbody": [tbody],
    }), row, row_cells


def _link_inspection_handler(*, target_links, other_links=None):
    pre_table = _virtualized_result_driver(
        ["機器名", "OS", "電話番号", "ユーザー", "組織", "通信日時", "詳細"],
        [[f"device_{index}", "OS", "", "", "", "", ""] for index in range(5)],
    ).tables[0]
    post_table, target_row, target_cells = _search_result_table(
        ["機器名", "OS", "電話番号", "ユーザー", "組織", "通信日時", "詳細"],
        ["target_device", "OS", "", "", "", "", ""],
    )
    detail_controls = []
    for text, href in target_links:
        detail_control = DomNode("button", text=text, attrs={"href": href})
        detail_control.click_calls = 0
        detail_control.click = lambda control=detail_control: setattr(control, "click_calls", control.click_calls + 1)
        detail_controls.append(detail_control)
    target_cells[-1].children["a,button,[role='link'],[role='button']"] = detail_controls
    for detail_control in detail_controls:
        detail_control.parent = target_cells[-1]
    machine_link = DomNode("a", text="device_name")
    machine_link.click_calls = 0
    machine_link.click = lambda: setattr(machine_link, "click_calls", machine_link.click_calls + 1)
    target_row.children["a"] = [machine_link]
    machine_link.parent = target_row
    driver = SearchPageDriver(pre_table, post_table)
    handler = handler_for(driver)
    handler._serial_search_results_dom_snapshot(driver)
    before = handler._serial_search_results_dom_snapshot(driver)
    driver.input.send_keys("target_serial")
    driver.search_button.click()
    observation = handler._observe_serial_search_after_submit(before, "target_serial")
    handler.device_observation.update(observation)
    handler.fixture_refs = {"detail": detail_controls, "machine": machine_link, "target_row": target_row}
    handler.search_observation = observation
    return handler


def test_readonly_link_inspection_unique_detail_candidate_never_clicks():
    handler = _link_inspection_handler(target_links=[("詳細", "/devices/detail")])
    result = handler.inspect_matched_device_result_links("target_serial", handler.search_observation)
    assert handler.search_observation["device_search_pre_result_visible_row_count"] == 5
    assert handler.search_observation["device_search_post_result_visible_row_count"] == 1
    assert handler.search_observation["device_search_result_signature_changed"] is True
    assert handler.search_observation["device_search_filter_condition_updated"] is True
    assert handler.search_observation["device_search_result_total_count"] == 1
    assert handler.search_observation["device_search_result_page_count"] == 1
    assert handler.search_observation["device_search_result_transition_verified"] is True
    assert handler.search_observation["device_result_candidate_count"] == 1
    assert handler.search_observation["device_result_candidate_unique"] is True
    assert handler.search_observation["device_result_identity_verified"] is False
    assert result["device_result_link_candidate_count"] == 1
    assert result["device_result_link_inspection_completed"] is True
    assert result["device_result_link_unique_detail_candidate_count"] == 1
    assert result["device_result_link_click_called"] is False
    assert result["device_result_link_click_count"] == 0


def test_readonly_link_inspection_without_detail_candidate_never_clicks():
    handler = _link_inspection_handler(target_links=[("別操作", "")])
    result = handler.inspect_matched_device_result_links("target_serial", handler.search_observation)
    assert result["device_result_link_candidate_count"] == 1
    assert result["device_result_link_unique_detail_candidate_count"] == 0
    assert result["device_result_link_click_count"] == 0


def test_readonly_link_inspection_multiple_detail_candidates_never_clicks():
    handler = _link_inspection_handler(target_links=[("詳細", "/devices/detail"), ("Details", "/devices/detail")])
    result = handler.inspect_matched_device_result_links("target_serial", handler.search_observation)
    assert result["device_result_link_unique_detail_candidate_count"] == 2
    assert result["device_result_link_click_called"] is False
    assert result["device_result_link_click_count"] == 0


def test_readonly_link_inspection_excludes_other_and_hidden_rows():
    handler = _link_inspection_handler(
        target_links=[("詳細", "/devices/detail")],
        other_links=[("詳細", "/devices/detail"), ("詳細", "/devices/detail")],
    )
    result = handler.inspect_matched_device_result_links("target_serial", handler.search_observation)
    assert result["device_result_link_candidate_count"] == 1
    assert result["device_result_link_inside_matched_row_count"] == 1
    assert result["device_result_link_unique_detail_candidate_count"] == 1
    assert len(result["device_result_link_metadata"]) == 1
    assert result["device_result_link_click_count"] == 0


def test_observe_serial_search_after_submit_reports_exact_one_on_virtualized_dom():
    """Case 1: unique table, header-mapped serial column, exactly one row matches."""
    driver = _virtualized_result_driver(
        ["Alias", "Serial Number", "IMEI", "C4", "C5", "C6", "C7", "C8"],
        [
            ["target_alias", "target_serial", "111111111111111", "", "", "", "", ""],
            ["other_alias", "other_serial", "222222222222222", "", "", "", "", ""],
        ],
    )
    handler = handler_for(driver)
    before = handler._serial_search_results_dom_snapshot(driver)

    # Simulate a search that produces a DOM change: adjust the visible content so the
    # signature differs from ``before`` (needed for result_dom_changed).
    driver.tables[0].children["tbody"][0].children["tr"][0].children["td"][1].text = "target_serial"
    metrics = handler._observe_serial_search_after_submit({"signature": ()}, "target_serial")

    assert metrics["exact_match_count"] is None
    assert metrics["device_search_result_container_count"] == 1
    assert metrics["device_search_result_row_candidate_count"] == 2
    assert metrics["device_search_visible_result_row_count"] == 2
    assert metrics["device_search_serial_cell_candidate_count"] == 0
    assert metrics["device_search_serial_cell_nonblank_count"] == 0
    assert metrics["device_search_zero_result_indicator_found"] is False
    assert metrics["device_search_result_collection_method"] == "unique_result_table_without_identity"
    assert metrics["device_search_result_stable"] is True
    assert metrics["device_search_count_failed_phase"] == "completed"
    assert metrics["device_search_count_exception_type"] == ""


def test_readonly_search_accepts_missing_exact_count_and_completes():
    handler, _inputs, _buttons = configured_search_handler(exact_count=None)
    handler._observe_serial_search_after_submit = lambda _before, _value: {
        "device_search_result_container_count": 1,
        "device_search_result_row_candidate_count": 1,
        "device_search_visible_result_row_count": 1,
        "device_search_post_result_visible_row_count": 1,
        "device_search_result_total_count": 1,
        "device_search_result_page_count": 1,
        "device_search_result_transition_verified": True,
        "device_result_candidate_count": 1,
        "device_result_candidate_unique": True,
        "device_result_identity_verified": False,
        "device_search_exact_match_count": None,
        "exact_match_count": None,
        "device_search_result_stable": True,
        "device_search_result_collection_method": "unique_result_table_without_identity",
        "device_search_count_failed_phase": "completed",
        "device_search_count_exception_type": "",
    }

    result = handler.search_device("target-serial", page_reached=True, read_only_observation=True)

    assert result["device_search_exact_match_count"] is None
    assert result["device_search_failed_phase"] == "completed"
    assert result["device_search_exception_type"] == ""
    assert result["device_search_read_only_observation"] is True
    assert result["device_result_candidate_unique"] is True


def test_snapshot_uses_total_one_and_excludes_previous_and_non_data_rows():
    handler = handler_for(type("SnapshotDriver", (), {})())
    detail_link = DomNode("a", attrs={"href": "/devices/detail"})

    def data_row(link=False):
        details = [detail_link] if link else []
        cells = [DomNode("td", text="opaque"), DomNode("td", children={"a,button,[role='link'],[role='button']": details})]
        return DomNode("tr", children={
            "td": cells,
            "th": [],
            "input[type='checkbox'], [role='checkbox']": [],
            "a": details,
        })

    previous_rows = [data_row() for _ in range(4)]
    tbody = DomNode("tbody", children={"tr": previous_rows})
    table = DomNode("table", children={
        "thead th": [DomNode("th", text="Alias"), DomNode("th", text="Details")],
        "tr th": [DomNode("th", text="Alias"), DomNode("th", text="Details")],
        "tbody": [tbody],
    })
    driver = ResultDomDriver([table])
    driver.find_element = lambda _by, _value: DomNode("body", text="合計 1 件 1 / 1 ページ")
    handler.browser.driver = driver
    before = handler._serial_search_results_dom_snapshot(driver)

    current_row = data_row(link=True)
    tbody.children["tr"] = previous_rows + [current_row]
    for row in tbody.children["tr"]:
        row.parent = tbody
    after = handler._serial_search_results_dom_snapshot(
        driver,
        before_signature=before["signature"],
        before_rows=before["result_rows"],
    )

    assert after["result_count"] == 1
    assert after["visible_row_count"] == 1
    assert after["result_rows"] == [current_row]
    assert handler._find_detail_column_controls(after["result_rows"][0].find_elements(None, "td")[1]) == [detail_link]


def test_role_grid_result_container_resolves_one_row_without_table():
    cell = DomNode("div", attrs={"role": "gridcell"})

    class RoleRow(DomNode):
        def __init__(self):
            super().__init__("div", attrs={"role": "row"})

        def find_elements(self, _by, selector):
            return [cell] if "gridcell" in selector or "cell" in selector else []

    row = RoleRow()
    grid = DomNode("div", attrs={"role": "grid"}, children={"[role='row']": [row]})

    class RoleDriver:
        def find_elements(self, _by, selector):
            if selector == "table":
                return []
            if selector == "[role='table'],[role='grid']":
                return [grid]
            if selector == "[role='row']":
                return [row]
            return []

    handler = handler_for(RoleDriver())
    snapshot = handler._serial_search_results_dom_snapshot(RoleDriver())

    assert snapshot["device_search_result_container_resolution_method"] == "aria_table_or_grid"
    assert snapshot["device_search_result_container_unique"] is True
    assert snapshot["device_search_result_row_scoped_candidate_count"] == 1
    assert snapshot["device_search_result_structural_uniqueness_verified"] is True


def test_page_metrics_unavailable_are_not_fabricated():
    class NoCountDriver:
        def find_element(self, _by, _selector):
            return type("Body", (), {"text": "検索結果"})()

    metrics = handler_for(NoCountDriver())._search_result_page_metrics(NoCountDriver())

    assert metrics["device_search_result_total_count"] is None
    assert metrics["device_search_result_page_count"] is None


def test_client_certificate_panel_classifies_view_state_without_control():
    handler = handler_for(type("Driver", (), {})())
    panel = DomNode("aside", text="クライアント証明書 クライアント証明書（デフォルト） （設定なし） 編集", children={
        "span,div,label,dt,dd": [DomNode("span", text="クライアント証明書"), DomNode("span", text="クライアント証明書（デフォルト）"), DomNode("span", text="（設定なし）")],
    })
    edit = DomNode("button", text="編集")
    handler._safe_find_elements_from = lambda element, _by, selector: (element.children.get("span,div,label,dt,dd", []) + [edit]) if ("span,div,label" in selector or selector == "*") else [edit] if "a,button" in selector else []

    state = handler._classify_client_certificate_panel(panel)

    assert state["client_certificate_view_state_detected"] is True
    assert state["client_certificate_edit_state_detected"] is False
    assert state["client_certificate_selection_control_candidate_count"] == 0


@pytest.mark.parametrize(
    ("default_text", "unconfigured_text", "variant"),
    [
        ("クライアント証明書(デフォルト)", "(設定なし)", "ascii_parentheses"),
        ("クライアント証明書（デフォルト）", "（設定なし）", "fullwidth_parentheses"),
    ],
)
def test_client_certificate_reference_accepts_parenthesis_variants(default_text, unconfigured_text, variant):
    handler = handler_for(type("Driver", (), {})())
    panel = DomNode("aside", text=f"クライアント証明書 {default_text} {unconfigured_text} 編集", children={
        "*": [DomNode("span", text="クライアント証明書"), DomNode("span", text=default_text), DomNode("span", text=unconfigured_text)],
        "button,a,[role='button'],[role='link'],[onclick],[tabindex],span,div,label": [DomNode("button", text="編集")],
        "a,button,[role='link'],[role='button']": [DomNode("button", text="編集")],
    })
    handler._safe_find_elements_from = lambda element, _by, selector: element.children.get(selector, [])

    state = handler._classify_client_certificate_panel(panel)

    assert state["client_certificate_default_label_unique"] is True
    assert state["client_certificate_unconfigured_text_unique"] is True
    assert state["client_certificate_default_label_variant"] == variant
    assert state["client_certificate_unconfigured_text_variant"] == variant


def test_client_certificate_reference_does_not_accept_partial_parenthesis_text():
    handler = handler_for(type("Driver", (), {})())
    panel = DomNode("aside", text="クライアント証明書 設定なし 編集")
    handler._safe_find_elements_from = lambda *_args: []

    state = handler._classify_client_certificate_panel(panel)

    assert state["client_certificate_default_label_unique"] is False
    assert state["client_certificate_unconfigured_text_unique"] is False


def test_client_certificate_panel_classifies_edit_state_and_native_select_without_click():
    handler = handler_for(type("Driver", (), {})())
    panel = DomNode("aside", text="クライアント証明書 クライアント証明書（デフォルト） 保存 取消", children={"span,div,label,dt,dd": [DomNode("span", text="クライアント証明書")]})
    save = DomNode("button", text="保存")
    cancel = DomNode("button", text="取消")
    select = DomNode("select", attrs={"value": ""})
    heading = panel.children["span,div,label,dt,dd"][0]
    handler._safe_find_elements_from = lambda element, _by, selector: (
        [save, cancel] if "a,button" in selector else [select, heading] if selector == "*" else [select] if selector == "select" else [heading] if "span,div,label" in selector else []
    )

    state = handler._classify_client_certificate_panel(panel)

    assert state["client_certificate_view_state_detected"] is False
    assert state["client_certificate_edit_state_detected"] is True
    assert state["client_certificate_control_resolution_method"] == "native_select"
    assert state["client_certificate_control_current_value_blank"] is True


def test_client_certificate_panel_metrics_exclude_search_background():
    handler = handler_for(type("Driver", (), {})())
    panel = DomNode("aside", text="クライアント証明書（デフォルト） 設定なし 編集 戻る 閉じる", children={
        "a,button,[role='link'],[role='button']": [DomNode("button", text="編集"), DomNode("button", text="戻る"), DomNode("button", text="閉じる")],
        "input[name*='query' i],input[aria-label*='search' i]": [],
        "button[type='submit']": [],
        "table": [],
    })

    metrics = handler._client_certificate_panel_metrics(panel)

    assert metrics["client_certificate_panel_contains_search_input"] is False
    assert metrics["client_certificate_panel_contains_search_button"] is False
    assert metrics["client_certificate_panel_contains_result_table"] is False
    assert metrics["client_certificate_panel_heading_exact_match"] is True


def test_client_certificate_edit_state_reports_save_cancel_visibility_and_enabled():
    handler = handler_for(type("Driver", (), {})())
    panel = DomNode("aside", text="クライアント証明書（デフォルト） 保存 取消", children={
        "a,button,[role='link'],[role='button']": [DomNode("button", text="保存"), DomNode("button", text="取消")],
        "select": [DomNode("select", attrs={"value": ""})],
        "[role='combobox']": [],
        "input": [],
    })

    state = handler._classify_client_certificate_panel(panel)

    assert state["client_certificate_save_unique"] is True
    assert state["client_certificate_save_displayed"] is True
    assert state["client_certificate_save_enabled"] is True
    assert state["client_certificate_cancel_unique"] is True
    assert state["client_certificate_cancel_displayed"] is True
    assert state["client_certificate_cancel_enabled"] is True


def test_certificate_edit_transition_requires_reference_markers_to_disappear_and_edit_markers_to_appear():
    handler = handler_for(type("Driver", (), {})())
    result = {
        "client_certificate_edit_click_completed": True,
        "client_certificate_before_unconfigured_count": 1,
        "client_certificate_before_edit_count": 1,
        "client_certificate_before_save_count": 0,
        "client_certificate_before_cancel_count": 0,
        "client_certificate_after_unconfigured_count": 0,
        "client_certificate_after_edit_count": 0,
        "client_certificate_after_save_count": 1,
        "client_certificate_after_cancel_count": 1,
        "client_certificate_after_control_element_count": 2,
    }

    assert handler._certificate_edit_transition_detected(result) is True


def test_certificate_edit_transition_rejects_remaining_reference_markers():
    handler = handler_for(type("Driver", (), {})())
    result = {
        "client_certificate_edit_click_completed": True,
        "client_certificate_before_unconfigured_count": 1,
        "client_certificate_before_edit_count": 1,
        "client_certificate_before_save_count": 0,
        "client_certificate_before_cancel_count": 0,
        "client_certificate_after_unconfigured_count": 1,
        "client_certificate_after_edit_count": 0,
        "client_certificate_after_save_count": 1,
        "client_certificate_after_cancel_count": 1,
        "client_certificate_after_control_element_count": 2,
    }

    assert handler._certificate_edit_transition_detected(result) is False


def test_client_certificate_input_and_expand_button_are_one_logical_control():
    handler = handler_for(type("Driver", (), {})())
    panel = DomNode("aside", text="クライアント証明書（デフォルト） 保存 取消")
    save = DomNode("button", text="保存")
    cancel = DomNode("button", text="取消")
    input_control = DomNode("input", attrs={"value": "", "aria-controls": "certificate-options"})
    expand = DomNode("button", attrs={"aria-controls": "certificate-options"})
    group = DomNode("div", children={"input": [input_control], "a,button,[role='link'],[role='button']": [expand]})
    input_control.parent = group
    expand.parent = group
    def find_from(_element, _by, selector):
        if "a,button" in selector:
            return [save, cancel, expand]
        if selector == "input":
            return [input_control]
        return []
    handler._safe_find_elements_from = find_from

    state = handler._classify_client_certificate_panel(panel)

    assert state["client_certificate_control_raw_element_count"] == 2
    assert state["client_certificate_control_deduplicated_candidate_count"] == 1
    assert state["client_certificate_selection_control_candidate_count"] == 1


def test_edit_click_calls_edit_form_wait_once_and_merges_failed_result():
    handler = handler_for(type("Driver", (), {})())
    edit = DomNode("button", text="編集")
    edit.click_calls = 0
    edit.click = lambda: setattr(edit, "click_calls", edit.click_calls + 1)
    panel = DomNode("aside")
    handler.inspect_client_certificate_navigation_only = lambda _serial, trace=None: {
        "device_result_identity_verified": True,
        "device_result_selected": True,
        "client_certificate_panel": panel,
        "client_certificate_panel_unique": True,
    }
    handler._wait_for_client_certificate_state = lambda **_kwargs: {
        "panel": panel,
        "client_certificate_view_state_detected": True,
        "client_certificate_edit_state_detected": False,
    }
    handler._certificate_edit_candidates = lambda _panel: [edit]
    calls = []
    handler._wait_for_client_certificate_edit_form = lambda **_kwargs: calls.append(_kwargs) or {
        "client_certificate_edit_form_wait_called": True,
        "client_certificate_edit_form_wait_completed": False,
        "client_certificate_edit_form_wait_timeout": True,
        "client_certificate_edit_form_candidate_count": 0,
        "client_certificate_edit_state_detected": False,
    }

    result = handler.inspect_client_certificate_edit_form_only("serial")

    assert len(calls) == 1
    assert edit.click_calls == 1
    assert result["client_certificate_edit_form_wait_called"] is True
    assert result["client_certificate_edit_form_wait_completed"] is False
    assert result["client_certificate_edit_form_wait_timeout"] is True


def test_edit_form_wait_reuses_same_panel_and_refreshes_children():
    handler = handler_for(type("Driver", (), {})())
    panel = DomNode("aside")
    classifications = iter([
        {"client_certificate_edit_state_detected": True, "client_certificate_selection_control_candidate_count": 1, "client_certificate_save_candidate_count": 1, "client_certificate_cancel_candidate_count": 1},
    ])
    handler._classify_client_certificate_panel = lambda current: next(classifications)
    result = handler._wait_for_client_certificate_edit_form(timeout=1, panel=panel)

    assert result["client_certificate_edit_form_wait_completed"] is True
    assert result["client_certificate_panel_same_dom_identity_after_edit"] is True
    assert result["client_certificate_panel_reacquired_after_edit"] is False
    assert result["client_certificate_edit_form_same_panel_state_refresh"] is True
    assert result["client_certificate_edit_form_wait_received_old_panel"] is True
    assert result["client_certificate_panel_identity_available_after_edit"] is True


def test_edit_form_wait_reacquires_panel_when_old_panel_is_stale(monkeypatch):
    handler = handler_for(type("Driver", (), {})())
    old_panel = DomNode("aside")
    new_panel = DomNode("aside")
    old_panel.is_enabled = lambda: (_ for _ in ()).throw(smsm_handler_module.StaleElementReferenceException())
    handler._wait_for_named_panel = lambda *_args, **_kwargs: {"unique": True, "panel": new_panel}
    handler._classify_client_certificate_panel = lambda _panel: {"client_certificate_edit_state_detected": True, "client_certificate_selection_control_candidate_count": 1, "client_certificate_save_candidate_count": 1, "client_certificate_cancel_candidate_count": 1}

    result = handler._wait_for_client_certificate_edit_form(timeout=1, panel=old_panel)

    assert result["client_certificate_panel_stale_after_edit"] is True
    assert result["client_certificate_panel_reacquired_after_edit"] is True
    assert result["client_certificate_panel_same_dom_identity_after_edit"] is False


def test_edit_form_wait_is_not_called_when_edit_click_fails():
    handler = handler_for(type("Driver", (), {})())
    edit = DomNode("button", text="編集")
    edit.click = lambda: (_ for _ in ()).throw(RuntimeError("click failed"))
    panel = DomNode("aside")
    handler.inspect_client_certificate_navigation_only = lambda _serial, trace=None: {
        "device_result_identity_verified": True,
        "client_certificate_panel": panel,
        "client_certificate_panel_unique": True,
    }
    handler._wait_for_client_certificate_state = lambda **_kwargs: {"panel": panel, "client_certificate_view_state_detected": True, "client_certificate_edit_state_detected": False}
    handler._certificate_edit_candidates = lambda _panel: [edit]
    handler._wait_for_client_certificate_edit_form = lambda **_kwargs: (_ for _ in ()).throw(AssertionError("wait must not run"))

    result = handler.inspect_client_certificate_edit_form_only("serial")

    assert result["client_certificate_edit_click_called"] is False
    assert result["client_certificate_edit_form_wait_called"] is False


def test_edit_form_visibility_metrics_are_consistent_for_current_panel():
    handler = handler_for(type("Driver", (), {})())
    panel = DomNode("aside", displayed=True)
    handler.browser.driver.execute_script = lambda *_args: {"display": "block", "visibility": "visible", "width": 100, "height": 100, "rects": 1}

    metrics = handler._edit_form_visibility_metrics(panel)

    assert metrics["client_certificate_edit_form_candidate_dom_attached_count"] == 1
    assert metrics["client_certificate_edit_form_candidate_visibility_evaluated_count"] == 1
    assert metrics["client_certificate_edit_form_candidate_nonzero_rect_count"] == 1
    assert metrics["client_certificate_edit_form_candidate_unclassified_count"] == 0
    assert metrics["client_certificate_edit_form_candidate_metrics_consistent"] is True
    assert metrics["client_certificate_edit_form_visibility_script_result_valid"] is True


@pytest.mark.parametrize("script_result", [None, True, {}, {"display": "block"}])
def test_edit_form_visibility_probe_schema_never_raises_key_error(monkeypatch, script_result):
    handler = handler_for(type("Driver", (), {})())
    panel = DomNode("aside", displayed=True)
    handler.browser.driver.execute_script = lambda *_args: script_result

    metrics = handler._edit_form_visibility_metrics(panel)

    assert "dom_visible" in metrics
    assert "client_certificate_edit_form_visibility_script_result_valid" in metrics
    assert metrics["dom_visible"] is False


@pytest.mark.parametrize("expected_state", ["view", "edit"])
def test_client_certificate_state_wait_uses_current_snapshot_without_name_error(monkeypatch, expected_state):
    handler = handler_for(type("Driver", (), {})())
    panel = DomNode("aside")
    handler._wait_for_named_panel = lambda *_args, **_kwargs: {"candidate_count": 1, "unique": True, "visible": True, "panel": panel}
    handler._classify_client_certificate_panel = lambda _panel: {
        "client_certificate_view_state_detected": expected_state == "view",
        "client_certificate_edit_state_detected": expected_state == "edit",
        "client_certificate_selection_control_candidate_count": 1 if expected_state == "edit" else 0,
        "client_certificate_save_candidate_count": 1 if expected_state == "edit" else 0,
        "client_certificate_cancel_candidate_count": 1 if expected_state == "edit" else 0,
        "client_certificate_reference_edit_control_candidate_count": 1 if expected_state == "view" else 0,
    }

    class ImmediateWait:
        def __init__(self, *_args, **_kwargs):
            pass

        def until(self, predicate):
            return predicate(handler.browser.driver)

    monkeypatch.setattr(smsm_handler_module, "WebDriverWait", ImmediateWait)

    result = handler._wait_for_client_certificate_state(timeout=1, expected_state=expected_state)

    assert result["client_certificate_state_wait_completed"] is True
    assert result["client_certificate_edit_state_detected"] is (expected_state == "edit")


class _FallbackPanelNode:
    def __init__(self, *, text="", parent=None, clickables=None):
        self.text = text
        self.parent = parent
        self.clickables = clickables or []

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def get_attribute(self, _name):
        return None

    def find_element(self, _by, value):
        if value == "./.." and self.parent is not None:
            return self.parent
        raise LookupError(value)

    def find_elements(self, _by, value):
        if value == "a,button,[role='link'],[role='button']":
            return self.clickables
        return []


def test_detail_panel_fallback_uses_clickable_ancestor_within_six_levels(monkeypatch):
    setting = _FallbackPanelNode(text="他の設定を見る")
    close = _FallbackPanelNode(text="閉じる")
    panel = _FallbackPanelNode(
        text="管理情報の編集 設定の割り当て 設定テンプレートの割り当て",
        clickables=[setting, close],
    )
    setting.parent = panel
    current = setting
    for _ in range(5):
        current.parent = _FallbackPanelNode(parent=current.parent)
        current = current.parent
    class Driver:
        def find_elements(self, _by, _selector):
            return []
    handler = handler_for(Driver())
    monkeypatch.setattr(handler, "_find_exact_clickables", lambda *_args: [setting])
    class ImmediateWait:
        def __init__(self, *_args, **_kwargs): pass
        def until(self, predicate): return predicate(handler.browser.driver)
    monkeypatch.setattr(smsm_handler_module, "WebDriverWait", ImmediateWait)

    result = handler._wait_for_device_detail_panel(timeout=1)

    assert result["device_detail_panel_candidate_count"] == 1
    assert result["device_detail_panel_unique"] is True


def test_detail_panel_fallback_does_not_adopt_seventh_ancestor(monkeypatch):
    setting = _FallbackPanelNode(text="他の設定を見る")
    close = _FallbackPanelNode(text="閉じる")
    panel = _FallbackPanelNode(text="管理情報の編集 設定の割り当て 設定テンプレートの割り当て", clickables=[setting, close])
    current = setting
    for _ in range(6):
        current.parent = _FallbackPanelNode(parent=current.parent)
        current = current.parent
    current.parent = panel
    class Driver:
        def find_elements(self, _by, _selector):
            return []
    handler = handler_for(Driver())
    monkeypatch.setattr(handler, "_find_exact_clickables", lambda *_args: [setting])
    class ImmediateWait:
        def __init__(self, *_args, **_kwargs): pass
        def until(self, predicate): return predicate(handler.browser.driver)
    monkeypatch.setattr(smsm_handler_module, "WebDriverWait", ImmediateWait)

    result = handler._wait_for_device_detail_panel(timeout=1)

    assert result["device_detail_panel_unique"] is False


@pytest.mark.parametrize("candidate_count", [0, 2])
def test_detail_panel_fallback_requires_one_qualifying_ancestor(monkeypatch, candidate_count):
    settings = [_FallbackPanelNode(text="他の設定を見る") for _ in range(max(candidate_count, 1))]
    candidates = []
    for _ in range(candidate_count):
        panel = _FallbackPanelNode(
            text="管理情報の編集 設定の割り当て 設定テンプレートの割り当て",
            clickables=[_FallbackPanelNode(text="閉じる"), settings[len(candidates)]],
        )
        candidates.append(panel)
    for setting, candidate in zip(settings, candidates):
        setting.parent = candidate
    class Driver:
        def find_elements(self, _by, _selector):
            return []
    handler = handler_for(Driver())
    monkeypatch.setattr(handler, "_find_exact_clickables", lambda *_args: settings[:candidate_count])
    monkeypatch.setattr(handler, "_safe_find_elements_from", lambda element, _by, selector: element.clickables if selector == "a,button,[role='link'],[role='button']" else [])
    class ImmediateWait:
        def __init__(self, *_args, **_kwargs): pass
        def until(self, predicate): return predicate(handler.browser.driver)
    monkeypatch.setattr(smsm_handler_module, "WebDriverWait", ImmediateWait)

    result = handler._wait_for_device_detail_panel(timeout=1)

    assert result["device_detail_panel_unique"] is (candidate_count == 1)


def test_observe_serial_search_after_submit_reports_zero_on_no_matching_cell():
    """Case 2: unique table, header-mapped serial column, no row's serial cell matches."""
    driver = _virtualized_result_driver(
        ["Alias", "Serial Number", "IMEI", "C4", "C5", "C6", "C7", "C8"],
        [
            ["neighbor_alias", "not_the_target", "111111111111111", "", "", "", "", ""],
            ["other_alias", "target_serial_but_different", "222222222222222", "", "", "", "", ""],
        ],
    )
    handler = handler_for(driver)
    metrics = handler._observe_serial_search_after_submit({"signature": ()}, "target_serial")

    assert metrics["exact_match_count"] is None
    assert metrics["device_search_result_row_candidate_count"] == 2
    assert metrics["device_search_serial_cell_candidate_count"] == 0
    assert metrics["device_search_serial_cell_nonblank_count"] == 0
    assert metrics["device_search_result_collection_method"] == "unique_result_table_without_identity"
    assert metrics["device_search_result_stable"] is True
    assert metrics["device_search_count_failed_phase"] == "completed"


def test_observe_serial_search_after_submit_flags_ambiguous_when_two_rows_match():
    """Case 3: unique table, two rows carry the same serial (ambiguous, exact=2)."""
    driver = _virtualized_result_driver(
        ["Alias", "Serial Number", "IMEI", "C4", "C5", "C6", "C7", "C8"],
        [
            ["alias_one", "same_serial", "111111111111111", "", "", "", "", ""],
            ["alias_two", "same_serial", "222222222222222", "", "", "", "", ""],
        ],
    )
    handler = handler_for(driver)
    metrics = handler._observe_serial_search_after_submit({"signature": ()}, "same_serial")

    assert metrics["exact_match_count"] is None
    assert metrics["device_search_result_collection_method"] == "unique_result_table_without_identity"
    assert metrics["device_search_serial_cell_candidate_count"] == 0


def test_observe_serial_search_after_submit_reports_zero_on_explicit_empty_state():
    """Case 4: unique table with an explicit empty-state indicator → 0 with zero indicator."""
    driver = _virtualized_result_driver(
        ["Alias", "Serial Number", "IMEI", "C4", "C5", "C6", "C7", "C8"],
        [],
        empty_state=True,
    )
    handler = handler_for(driver)
    metrics = handler._observe_serial_search_after_submit({"signature": ()}, "target_serial")

    assert metrics["exact_match_count"] == 0
    assert metrics["device_search_zero_result_indicator_found"] is True
    assert metrics["device_search_result_collection_method"] == "zero_result_indicator"
    assert metrics["device_search_count_failed_phase"] == "completed"


def test_observe_serial_search_after_submit_keeps_none_when_result_table_is_not_unique():
    """Case 5: multiple result containers → count is undecidable; exact stays None."""
    driver = _virtualized_result_driver(
        ["Alias", "Serial Number", "IMEI", "C4", "C5", "C6", "C7", "C8"],
        [
            ["target_alias", "target_serial", "111111111111111", "", "", "", "", ""],
        ],
        extra_tables=1,
    )
    handler = handler_for(driver)
    metrics = handler._observe_serial_search_after_submit({"signature": ()}, "target_serial")

    assert metrics["exact_match_count"] is None
    assert metrics["device_search_result_container_count"] >= 2
    assert metrics["device_search_result_collection_method"] == "unresolved"
    assert metrics["device_search_result_stable"] is False
    assert metrics["device_search_count_failed_phase"] == "scope_result_rows"