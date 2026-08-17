import inspect
import pytest
import diagnose_hennge_certificate_search as certificate_search

from app.hennge_handler import HenngeHandler
from app.single_certificate_workflow import WorkflowContext
from app.workflow_service import ProductionWorkflowService
from app.smsm_handler import SmsmHandler


class FakeCell:
    def __init__(self, text):
        self.text = text

    def get_attribute(self, _name):
        return ""


class FakeRow:
    def __init__(self, cells):
        self.text = "\n".join(cells)
        self.cells = [FakeCell(cell) for cell in cells]

    def find_elements(self, _by, _selector):
        return self.cells

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True


class StructuredCell(FakeCell):
    def get_attribute(self, name):
        if name == "textContent":
            return self.text
        return ""


class StructuredRow(FakeRow):
    def __init__(self, cells, headers):
        super().__init__(cells)
        self.cells = [StructuredCell(cell) for cell in cells]
        self.headers = [StructuredCell(header) for header in headers]
        self.click_count = 0

    def find_elements(self, _by, selector):
        if selector.startswith("./ancestor::"):
            return self.headers
        return self.cells

    def click(self):
        self.click_count += 1


class FakeBrowser:
    class Driver:
        current_url = "https://admin.auth.hennge.com/certificates/"

    driver = Driver()

    def open(self, _url):
        pass

    def wait_for_page_ready(self, timeout=None):
        pass


class FakeLogger:
    def __init__(self):
        self.messages = []

    def info(self, *args, **kwargs):
        self.messages.append(" ".join(str(arg) for arg in args))


def test_search_wait_timeout_uses_final_dom_rows_without_resubmitting(monkeypatch):
    handler = HenngeHandler.__new__(HenngeHandler)
    handler.browser = FakeBrowser()
    handler.logger = FakeLogger()
    row = FakeRow(["alias", "iOS"])
    calls = []
    monkeypatch.setattr(certificate_search, "_wait_results_ready", lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()))
    monkeypatch.setattr(handler, "_certificate_result_rows", lambda: calls.append(True) or ("rows", [row]))

    result = handler.wait_certificate_search_result()

    assert result["row_candidate_count"] == 1
    assert result["timeout_final_dom_observed"] is True
    assert calls == [True]


def test_search_wait_explicit_zero_is_not_reported_as_timeout(monkeypatch):
    handler = HenngeHandler.__new__(HenngeHandler)
    handler.browser = FakeBrowser()
    handler.logger = FakeLogger()
    monkeypatch.setattr(certificate_search, "_wait_results_ready", lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()))
    monkeypatch.setattr(certificate_search, "_is_no_data_visible", lambda _driver: True)
    monkeypatch.setattr(handler, "_certificate_result_rows", lambda: ("rows", []))

    with pytest.raises(RuntimeError, match="0件"):
        handler.wait_certificate_search_result()


class ScrollableContainer:
    id = "certificate-scroll-container"

    def __init__(self):
        self.scroll_top = 0

    def value_of_css_property(self, name):
        return "auto" if name == "overflow-y" else ""

    def get_property(self, name):
        return {"scrollHeight": 200, "clientHeight": 100, "scrollTop": self.scroll_top}.get(name, 0)

    def find_elements(self, _by, _selector):
        return []


class ScrollingDriver:
    current_url = "https://admin.auth.hennge.com/certificates/"

    def __init__(self):
        self.container = ScrollableContainer()
        self.scroll_scripts = []

    def find_elements(self, _by, selector):
        if selector in {"table", "[role='table']", "[role='grid']", "[role='rowgroup']"}:
            return [self.container] if selector == "table" else []
        return []

    def execute_script(self, script, container, position):
        self.scroll_scripts.append((script, container))
        container.scroll_top = position


class ScrollingBrowser(FakeBrowser):
    def __init__(self):
        self.driver = ScrollingDriver()


def make_handler():
    return HenngeHandler({}, FakeLogger(), FakeBrowser())


def make_scrolling_handler(monkeypatch):
    browser = ScrollingBrowser()
    handler = HenngeHandler({}, FakeLogger(), browser)
    headers = ["ユーザー名", "メール件名のメモ", "OS"]
    top_rows = [StructuredRow(["same-user", "other-imei", "iOS"], headers)]
    bottom_rows = [StructuredRow(["same-user", "123456789012345", "iOS"], headers)]

    def rows_for_position():
        return "table tbody tr", bottom_rows if browser.driver.container.scroll_top else top_rows

    monkeypatch.setattr(handler, "_certificate_result_rows", rows_for_position)
    monkeypatch.setattr("diagnose_hennge_certificate_detail._assert_row_click_safety", lambda _row: None)
    monkeypatch.setattr(handler, "verify_certificate_detail_page", lambda _alias, before_path=None: {"detail_page_verified": True, "detail": {}})
    return handler, browser, top_rows, bottom_rows


def test_alias_is_the_only_hennge_search_argument(monkeypatch):
    handler = make_handler()
    row = FakeRow(["target-alias", "certificate"])
    calls = []
    monkeypatch.setattr(handler, "_certificate_result_rows", lambda: ("row", [row]))
    monkeypatch.setattr("diagnose_hennge_certificate_search._wait_certificate_search_input_ready", lambda driver, logger: object())
    monkeypatch.setattr("diagnose_hennge_certificate_search._set_query_and_submit", lambda input_element, value, logger: calls.append(value))
    monkeypatch.setattr("diagnose_hennge_certificate_search._wait_results_ready", lambda browser: 1)

    result = handler.search_certificate_by_alias_exact("  target-alias  ")

    assert calls == ["target-alias"]
    assert result["alias_exact_match_count"] == 1
    assert result["unique"] is True


def test_empty_or_excel_error_alias_does_not_search(monkeypatch):
    handler = make_handler()
    called = []
    monkeypatch.setattr(handler, "_certificate_result_rows", lambda: called.append(True))

    for alias in ("", "   ", "#N/A"):
        with pytest.raises(ValueError):
            handler.search_certificate_by_alias_exact(alias)

    assert called == []


@pytest.mark.parametrize("rows", [[], [FakeRow(["other-alias"])], [FakeRow(["target-alias"]), FakeRow(["target-alias"])]] )
def test_alias_search_requires_one_exact_match(monkeypatch, rows):
    handler = make_handler()
    monkeypatch.setattr(handler, "_certificate_result_rows", lambda: ("row", rows))
    monkeypatch.setattr("diagnose_hennge_certificate_search._wait_certificate_search_input_ready", lambda driver, logger: object())
    monkeypatch.setattr("diagnose_hennge_certificate_search._set_query_and_submit", lambda input_element, value, logger: None)
    monkeypatch.setattr("diagnose_hennge_certificate_search._wait_results_ready", lambda browser: len(rows))

    with pytest.raises(RuntimeError):
        handler.search_certificate_by_alias_exact("target-alias")


def test_one_exact_search_result_is_passed_to_result_selection(monkeypatch):
    handler = make_handler()
    rows = [FakeRow(["target-alias"])]
    monkeypatch.setattr(handler, "_certificate_result_rows", lambda: ("row", rows))
    monkeypatch.setattr("diagnose_hennge_certificate_search._wait_certificate_search_input_ready", lambda driver, logger: object())
    monkeypatch.setattr("diagnose_hennge_certificate_search._set_query_and_submit", lambda input_element, value, logger: None)
    monkeypatch.setattr("diagnose_hennge_certificate_search._wait_results_ready", lambda browser: 1)

    result = handler.search_certificate_by_alias_exact("target-alias")

    assert result["result_count"] == 1
    assert result["unique"] is True
    assert result["rows"] == rows


def test_workflow_service_passes_alias_to_hennge_and_not_other_keys():
    service = ProductionWorkflowService.__new__(ProductionWorkflowService)
    calls = []

    class FakeHennge:
        def submit_certificate_search_by_alias(self, value):
            calls.append(value)
            return {"search_key_type": "alias", "search_submitted": True}

    service.hennge = FakeHennge()
    context = WorkflowContext()
    context.set_target({"alias": "target-alias", "serial": "target-serial", "imei": "123456789012345"})

    service.hennge_search_certificate_by_alias(context)

    assert calls == [context.target_alias]
    assert calls[0] != context.target_serial
    assert calls[0] != context.target_imei
    assert context.observations["hennge_imei_search_called"] is False


def test_context_separates_alias_serial_and_imei():
    context = WorkflowContext()
    context.set_target({"alias": "target-alias", "serial": "target-serial", "imei": "123456789012345"})

    assert context.target_alias == "target-alias"
    assert context.target_serial == "target-serial"
    assert context.target_imei == "123456789012345"


def test_sensitive_target_observations_are_not_stored_as_values():
    context = WorkflowContext()
    context.record("alias_value", "target-alias")
    context.record("serial_value", "target-serial")
    context.record("imei_value", "123456789012345")

    assert context.observations == {
        "alias_value": True,
        "serial_value": True,
        "imei_value": True,
    }


def test_safe_target_metrics_keep_counts_and_presence():
    context = WorkflowContext()
    context.record("hennge_alias_exact_match_count", 2)
    context.record("hennge_alias_present", True)

    assert context.observations["hennge_alias_exact_match_count"] == 2
    assert context.observations["hennge_alias_present"] is True


def _install_selection_stubs(monkeypatch, handler, rows):
    monkeypatch.setattr(handler, "_certificate_result_rows", lambda: ("row", rows))
    monkeypatch.setattr("diagnose_hennge_certificate_detail._assert_row_click_safety", lambda _row: None)
    monkeypatch.setattr(handler, "verify_certificate_detail_page", lambda _alias, before_path=None: {
        "detail_page_verified": True,
        "detail": {},
    })


def test_three_results_selects_only_exact_subject_memo_imei_and_ios(monkeypatch):
    handler = make_handler()
    headers = ["ユーザー名", "メール件名のメモ", "OS"]
    rows = [
        StructuredRow(["same-user", "other-imei", "iOS"], headers),
        StructuredRow(["same-user", "123456789012345", "iOS"], headers),
        StructuredRow(["same-user", "third-imei", "Windows"], headers),
    ]
    _install_selection_stubs(monkeypatch, handler, rows)

    result = handler.select_certificate_result({"result_count": 3}, "alias", "123456789012345")

    assert rows[0].click_count == 0
    assert rows[1].click_count == 1
    assert rows[2].click_count == 0
    assert result["imei_matched_row_candidate_count"] == 1
    assert result["imei_matched_row_os_ios"] is True


@pytest.mark.parametrize("memo", ["1234567890123450", "012345678901234", "12345678901234"])
def test_subject_memo_requires_exact_match(monkeypatch, memo):
    handler = make_handler()
    row = StructuredRow([["user", memo, "iOS"]][0], ["ユーザー名", "メール件名のメモ", "OS"])
    _install_selection_stubs(monkeypatch, handler, [row])

    with pytest.raises(RuntimeError) as error:
        handler.select_certificate_result({"result_count": 1}, "alias", "123456789012345")

    assert getattr(error.value, "failed_stage") == "hennge_resolve_certificate_row_by_imei"
    assert row.click_count == 0


def test_duplicate_exact_imei_stops_without_click(monkeypatch):
    handler = make_handler()
    headers = ["ユーザー名", "メール件名のメモ", "OS"]
    rows = [
        StructuredRow(["same-user", "123456789012345", "iOS"], headers),
        StructuredRow(["same-user", "123456789012345", "iOS"], headers),
    ]
    _install_selection_stubs(monkeypatch, handler, rows)

    with pytest.raises(RuntimeError):
        handler.select_certificate_result({"result_count": 2}, "alias", "123456789012345")

    assert [row.click_count for row in rows] == [0, 0]


def test_windows_exact_imei_is_not_safe(monkeypatch):
    handler = make_handler()
    row = StructuredRow(["same-user", "123456789012345", "Windows"], ["ユーザー名", "メール件名のメモ", "OS"])
    _install_selection_stubs(monkeypatch, handler, [row])

    with pytest.raises(RuntimeError):
        handler.select_certificate_result({"result_count": 1}, "alias", "123456789012345")

    assert row.click_count == 0


def test_subject_memo_whitespace_and_unicode_normalization_is_exact(monkeypatch):
    handler = make_handler()
    row = StructuredRow(["same-user", "１２３４５６７８９０１２３４５\n", "iOS"], ["ユーザー名", "メール件名のメモ", "OS"])
    _install_selection_stubs(monkeypatch, handler, [row])

    result = handler.select_certificate_result({"result_count": 1}, "alias", "123456789012345")

    assert result["imei_matched_row_safe"] is True
    assert row.click_count == 1


def test_nested_header_whitespace_is_normalized_without_fixed_column_index(monkeypatch):
    handler = make_handler()
    row = StructuredRow(
        ["same-user", "123456789012345", "iOS"],
        ["ユーザー名", "  メール件名のメモ\n", "\tOS  "],
    )
    _install_selection_stubs(monkeypatch, handler, [row])

    result = handler.select_certificate_result({"result_count": 1}, "alias", "123456789012345")

    assert result["subject_memo_column_found"] is True
    assert result["os_column_found"] is True
    assert result["imei_matched_row_os_ios"] is True
    assert row.click_count == 1


def test_subject_memo_and_imei_values_are_not_logged(monkeypatch):
    handler = make_handler()
    secret_imei = "123456789012345"
    secret_memo = "secret-subject-memo"
    row = StructuredRow(["same-user", secret_imei, "iOS"], ["ユーザー名", "メール件名のメモ", "OS"])
    _install_selection_stubs(monkeypatch, handler, [row])
    handler.select_certificate_result({"result_count": 1}, "alias", secret_imei)

    messages = " ".join(handler.logger.messages) if hasattr(handler.logger, "messages") else ""
    assert secret_imei not in messages
    assert secret_memo not in messages


def _install_ambiguous_scroll_observation(monkeypatch, handler, rows):
    scroll_container = object()
    monkeypatch.setattr(handler, "_certificate_result_rows", lambda: ("row", rows))
    monkeypatch.setattr(handler, "_find_certificate_result_scroll_container", lambda _rows: (
        scroll_container,
        {
            "candidate_count": 4,
            "unique": False,
            "scrollable": True,
            "scroll_height_greater_than_client_height": True,
        },
    ))
    monkeypatch.setattr("diagnose_hennge_certificate_detail._assert_row_click_safety", lambda _row: None)
    monkeypatch.setattr(handler, "verify_certificate_detail_page", lambda _alias, before_path=None: {
        "detail_page_verified": True,
        "detail": {},
    })


def test_initial_rows_are_evaluated_before_ambiguous_scroll_container(monkeypatch):
    handler = make_handler()
    rows = [
        StructuredRow(["same-user", "other-imei", "iOS"], ["ユーザー名", "メール件名のメモ", "OS"]),
        StructuredRow(["same-user", "123456789012345", "iOS"], ["ユーザー名", "メール件名のメモ", "OS"]),
        StructuredRow(["same-user", "third-imei", "Windows"], ["ユーザー名", "メール件名のメモ", "OS"]),
    ]
    _install_ambiguous_scroll_observation(monkeypatch, handler, rows)

    result = handler.select_certificate_result({"result_count": 3}, "alias", "123456789012345")

    assert result["scroll_called"] is False
    assert result["scroll_container_candidate_count"] == 4
    assert result["subject_memo_exact_match_count"] == 1
    assert result["imei_matched_row_candidate_count"] == 1
    assert result["imei_matched_row_os_ios"] is True
    assert result["imei_matched_row_safe"] is True
    assert rows[1].click_count == 1


def test_windows_exact_match_is_saved_and_does_not_scroll(monkeypatch):
    handler = make_handler()
    row = StructuredRow(["same-user", "123456789012345", "Windows"], ["ユーザー名", "メール件名のメモ", "OS"])
    _install_ambiguous_scroll_observation(monkeypatch, handler, [row])

    with pytest.raises(RuntimeError):
        handler.select_certificate_result({"result_count": 1}, "alias", "123456789012345")

    assert handler.last_search_observation["subject_memo_exact_match_count"] == 1
    assert handler.last_search_observation["imei_matched_row_candidate_count"] == 1
    assert handler.last_search_observation["imei_matched_row_os_ios"] is False
    assert handler.last_search_observation["imei_matched_row_safe"] is False
    assert handler.last_search_observation["scroll_called"] is False
    assert row.click_count == 0


def test_duplicate_exact_match_is_saved_and_does_not_scroll(monkeypatch):
    handler = make_handler()
    headers = ["ユーザー名", "メール件名のメモ", "OS"]
    rows = [
        StructuredRow(["same-user", "123456789012345", "iOS"], headers),
        StructuredRow(["same-user", "123456789012345", "iOS"], headers),
    ]
    _install_ambiguous_scroll_observation(monkeypatch, handler, rows)

    with pytest.raises(RuntimeError):
        handler.select_certificate_result({"result_count": 2}, "alias", "123456789012345")

    assert handler.last_search_observation["subject_memo_exact_match_count"] == 2
    assert handler.last_search_observation["imei_matched_row_candidate_count"] == 2
    assert handler.last_search_observation["imei_matched_row_os_ios"] is False
    assert handler.last_search_observation["imei_matched_row_safe"] is False
    assert handler.last_search_observation["scroll_called"] is False
    assert [row.click_count for row in rows] == [0, 0]


def test_no_exact_match_stops_on_ambiguous_scroll_container_without_click(monkeypatch):
    handler = make_handler()
    row = StructuredRow(["same-user", "other-imei", "iOS"], ["ユーザー名", "メール件名のメモ", "OS"])
    _install_ambiguous_scroll_observation(monkeypatch, handler, [row])

    with pytest.raises(RuntimeError):
        handler.select_certificate_result({"result_count": 1}, "alias", "123456789012345")

    assert handler.last_search_observation["subject_memo_exact_match_count"] == 0
    assert handler.last_search_observation["imei_matched_row_candidate_count"] == 0
    assert handler.last_search_observation["imei_matched_row_os_ios"] is False
    assert handler.last_search_observation["imei_matched_row_safe"] is False
    assert handler.last_search_observation["scroll_container_candidate_count"] == 4
    assert row.click_count == 0


def test_verify_target_diagnostic_evaluates_rows_without_click(monkeypatch):
    handler = make_handler()
    rows = [StructuredRow(["same-user", "123456789012345", "iOS"], ["ユーザー名", "メール件名のメモ", "OS"])]
    monkeypatch.setattr(handler, "_scan_certificate_rows_for_imei", lambda _imei: (
        "row",
        rows,
        {
            "observed_exact_match_count": 1,
            "scroll_container_scrollable": True,
            "scroll_container_unique": False,
        },
    ))
    monkeypatch.setattr(handler, "_certificate_result_rows", lambda: ("row", rows))

    assert certificate_search._verify_certificate_result_without_click(handler, "123456789012345", 3, handler.logger)
    assert rows[0].click_count == 0
    assert handler.last_search_observation["result_row_click_called"] is False


def test_target_in_lower_scroll_region_uses_container_only_and_reacquires_row(monkeypatch):
    handler, browser, top_rows, bottom_rows = make_scrolling_handler(monkeypatch)

    result = handler.select_certificate_result({"result_count": 2}, "alias", "123456789012345")

    assert top_rows[0].click_count == 0
    assert bottom_rows[0].click_count == 1
    assert result["target_row_found_before_scroll"] is False
    assert result["target_row_found_after_scroll"] is True
    assert result["scroll_called"] is True
    assert result["scroll_end_reached"] is True
    assert result["scroll_step_count"] == 1
    assert browser.driver.scroll_scripts
    assert all("scrollTop" in script for script, _container in browser.driver.scroll_scripts)
    assert all("window" not in script for script, _container in browser.driver.scroll_scripts)


def test_no_exact_match_reaches_list_end_without_click(monkeypatch):
    handler, browser, _top_rows, _bottom_rows = make_scrolling_handler(monkeypatch)
    headers = ["ユーザー名", "メール件名のメモ", "OS"]
    bottom_rows = [StructuredRow(["same-user", "other-imei", "iOS"], headers)]
    monkeypatch.setattr(handler, "_certificate_result_rows", lambda: (
        "table tbody tr",
        bottom_rows if browser.driver.container.scroll_top else [StructuredRow(["same-user", "other-imei", "iOS"], headers)],
    ))

    with pytest.raises(RuntimeError) as error:
        handler.select_certificate_result({"result_count": 2}, "alias", "123456789012345")

    assert getattr(error.value, "failed_stage") == "hennge_resolve_certificate_row_by_imei"
    assert len(browser.driver.scroll_scripts) == 1
    assert all(row.click_count == 0 for row in bottom_rows)


def test_smsm_device_search_uses_serial():
    service = ProductionWorkflowService.__new__(ProductionWorkflowService)
    calls = []

    class FakeSmsm:
        def search_device(self, value):
            calls.append(value)

    service.smsm = FakeSmsm()
    service.device_observation = {}
    context = WorkflowContext()
    context.set_target({"alias": "target-alias", "serial": "target-serial", "imei": "123456789012345"})

    service.smsm_search_device_by_serial(context)

    assert calls == [context.target_serial]
    assert calls[0] != context.target_alias
    assert calls[0] != context.target_imei


def test_binding_service_search_resolves_to_smsm_handler_search_device():
    source_file = inspect.getsourcefile(SmsmHandler.search_device)
    service_source = inspect.getsource(ProductionWorkflowService.smsm_search_device_by_serial)

    assert source_file is not None and source_file.endswith("app\\smsm_handler.py")
    assert "search_method = self.smsm.search_device" in service_source
    assert "return self._search_device_identifier" in inspect.getsource(SmsmHandler.search_device)


def test_readonly_search_completes_without_exact_match_count():
    service = ProductionWorkflowService.__new__(ProductionWorkflowService)
    calls = []

    class FakeSmsm:
        def search_device(self, value, page_reached=False, read_only_observation=False):
            calls.append((value, page_reached, read_only_observation))
            return {
                "device_search_exact_match_count": None,
                "device_search_result_container_count": 1,
                "device_search_result_stable": True,
                "device_result_candidate_unique": True,
                "device_result_identity_verified": False,
            }

    service.smsm = FakeSmsm()
    service.device_observation = {}
    context = WorkflowContext()
    context.set_target({"alias": "target-alias", "serial": "target-serial", "imei": "123456789012345"})

    result = service.smsm_search_device_by_serial(context, read_only=True)

    assert result["device_search_exact_match_count"] is None
    assert calls == [(context.target_serial, True, True)]


def test_service_search_syncs_observation_to_smsm_handler():
    service = ProductionWorkflowService.__new__(ProductionWorkflowService)
    handler_observation = {}

    class FakeSmsm:
        device_observation = handler_observation

        def search_device(self, value, page_reached=False, read_only_observation=False):
            return {
                "device_search_result_container_count": 1,
                "device_search_result_total_count": None,
                "device_search_result_page_count": None,
                "device_search_result_structural_uniqueness_verified": True,
                "device_result_candidate_count": 1,
                "device_result_candidate_unique": True,
            }

    service.smsm = FakeSmsm()
    service.device_observation = {}
    context = WorkflowContext()
    context.set_target({"alias": "target-alias", "serial": "target-serial", "imei": "123456789012345"})

    service.smsm_search_device_by_serial(context, read_only=True)

    assert handler_observation["device_result_candidate_count"] == 1
    assert handler_observation["device_result_candidate_unique"] is True


def test_readonly_link_inspection_is_called_once_without_clicks():
    service = ProductionWorkflowService.__new__(ProductionWorkflowService)
    calls = []

    class FakeSmsm:
        def inspect_matched_device_result_links(self, serial, observation):
            calls.append((serial, observation))
            return {
                "device_result_link_inspection_called": True,
                "device_result_link_inspection_completed": True,
                "device_result_link_click_called": False,
                "device_result_link_click_count": 0,
            }

    service.smsm = FakeSmsm()
    context = WorkflowContext()
    context.set_target({"alias": "target-alias", "serial": "target-serial", "imei": "123456789012345"})
    context.record("device_result_candidate_unique", True)

    result = service.smsm_inspect_matched_device_result_links(context)

    assert len(calls) == 1
    assert calls[0][0] == context.target_serial
    assert result["device_result_link_inspection_completed"] is True
    assert result["device_result_link_click_count"] == 0


def test_readonly_search_does_not_resubmit():
    service = ProductionWorkflowService.__new__(ProductionWorkflowService)
    calls = []

    class FakeSmsm:
        def search_device(self, value, page_reached=False, read_only_observation=False):
            calls.append((value, page_reached, read_only_observation))
            return {"device_search_exact_match_count": None, "device_result_candidate_unique": True}

    service.smsm = FakeSmsm()
    service.device_observation = {}
    context = WorkflowContext()
    context.set_target({"alias": "target-alias", "serial": "target-serial", "imei": "123456789012345"})

    service.smsm_search_device_by_serial(context, read_only=True)

    assert len(calls) == 1


def test_binding_search_keeps_search_exception_safety_condition():
    service = ProductionWorkflowService.__new__(ProductionWorkflowService)

    class FakeSmsm:
        def search_device(self, _value, page_reached=False, read_only_observation=False):
            assert page_reached is True
            assert read_only_observation is False
            raise RuntimeError("exact match is required for binding")

    service.smsm = FakeSmsm()
    service.device_observation = {}
    context = WorkflowContext()
    context.set_target({"alias": "target-alias", "serial": "target-serial", "imei": "123456789012345"})

    with pytest.raises(RuntimeError, match="exact match is required"):
        service.smsm_search_device_by_serial(context)


def test_smsm_imei_input_uses_target_imei():
    service = ProductionWorkflowService.__new__(ProductionWorkflowService)
    captured = []

    class Element:
        def is_displayed(self):
            return True

        def is_enabled(self):
            return True

        def clear(self):
            pass

        def send_keys(self, value):
            captured.append(value)

    class Driver:
        def find_elements(self, _by, _selector):
            return [Element()]

    class FakeSmsm:
        browser = type("Browser", (), {"driver": Driver()})()

        @staticmethod
        def _safe_bool(element, name):
            return bool(getattr(element, name)())

        @staticmethod
        def _safe_attribute(_element, _name):
            return "imei"

    service.smsm = FakeSmsm()
    context = WorkflowContext()
    context.set_target({"alias": "target-alias", "serial": "target-serial", "imei": "123456789012345"})

    service.smsm_set_device_imei(context)

    assert captured == [context.target_imei]
    assert captured[0] != context.target_alias
    assert captured[0] != context.target_serial
