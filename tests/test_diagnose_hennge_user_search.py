from selenium.webdriver.common.keys import Keys

import diagnose_hennge_user_search as mod


class DummyElement:
    def __init__(self, *, displayed=True, enabled=True, attrs=None, text=""):
        self._displayed = displayed
        self._enabled = enabled
        self._attrs = attrs or {}
        self.text = text
        self.sent_keys = []
        self.clicked = False

    def is_displayed(self):
        return self._displayed

    def is_enabled(self):
        return self._enabled

    def get_attribute(self, name):
        return self._attrs.get(name)

    def send_keys(self, *args):
        self.sent_keys.append(args)

    def click(self):
        self.clicked = True


class DummyDriver:
    def __init__(self, mapping=None):
        self.mapping = mapping or {}

    def find_elements(self, by, selector):
        return self.mapping.get(selector, [])


class DummyLogger:
    def __init__(self):
        self.messages = []

    def info(self, message: str) -> None:
        self.messages.append(message)


def test_find_single_visible_search_input_raises_for_zero_candidates() -> None:
    driver = DummyDriver({"input[name='query'][type='text']": []})

    try:
        mod._find_single_visible_search_input(driver)
        assert False, "expected SearchInputError"
    except mod.SearchInputError:
        pass


def test_find_single_visible_search_input_raises_for_multiple_candidates() -> None:
    driver = DummyDriver({
        "input[name='query'][type='text']": [DummyElement(), DummyElement()],
    })

    try:
        mod._find_single_visible_search_input(driver)
        assert False, "expected SearchInputError"
    except mod.SearchInputError:
        pass


def test_set_query_and_submit_sends_enter_once() -> None:
    input_el = DummyElement(attrs={"value": ""})

    mod._set_query_and_submit(input_el, "TEST_ALIAS")

    enter_calls = [call for call in input_el.sent_keys if call == (Keys.ENTER,)]
    assert len(enter_calls) == 1


def test_count_visible_results_distinguishes_zero_one_multiple() -> None:
    zero_driver = DummyDriver({"table tbody tr": []})
    assert mod._count_visible_results(zero_driver) == 0

    one_driver = DummyDriver({"table tbody tr": [DummyElement(text="row1")]})
    assert mod._count_visible_results(one_driver) == 1

    many_driver = DummyDriver({
        "table tbody tr": [DummyElement(text="row1"), DummyElement(text="row2")],
    })
    assert mod._count_visible_results(many_driver) == 2


def test_mask_query_hides_sensitive_part() -> None:
    masked = mod._mask_query("TEST_ALIAS")
    assert masked.startswith("TE")
    assert masked.endswith("S")
    assert "TEST_ALIAS" != masked


def test_no_update_button_operation_in_search_submit() -> None:
    # Only search input is touched by submit helper; no button click should happen here.
    input_el = DummyElement(attrs={"value": ""})
    mod._set_query_and_submit(input_el, "Q")
    assert input_el.clicked is False


def test_log_result_state_does_not_include_user_details() -> None:
    logger = DummyLogger()

    mod._log_result_state(logger, 0)
    mod._log_result_state(logger, 1)
    mod._log_result_state(logger, 3)

    joined = "\n".join(logger.messages).lower()
    assert "alias" not in joined
    assert "@" not in joined
    assert "href" not in joined
