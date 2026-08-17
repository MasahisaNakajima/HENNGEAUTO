from pathlib import Path
import inspect

import pytest
from selenium.common.exceptions import JavascriptException

from app.smsm_handler import SmsmHandler
from app.single_certificate_workflow import SingleCertificateWorkflow, WorkflowContext, WorkflowStageError
from app.workflow_service import ProductionWorkflowService


class Element:
    def __init__(self, text="", *, displayed=True, enabled=True, type_name=None, name=None, value=""):
        self.text = text
        self.displayed = displayed
        self.enabled = enabled
        self.type_name = type_name
        self.name = name
        self.value = value
        self.send_keys_calls = []
        self.click_count = 0

    def is_displayed(self):
        return self.displayed

    def is_enabled(self):
        return self.enabled

    def get_attribute(self, name):
        if name == "tagName":
            return "button" if self.name == "search" else "input"
        if name == "type":
            return self.type_name
        if name == "name":
            return self.name
        if name == "value":
            return self.value
        if name == "disabled":
            return None
        if name == "readonly":
            return None
        return ""

    def clear(self):
        self.value = ""
        return None

    def send_keys(self, value):
        self.send_keys_calls.append(value)
        self.value = str(value)

    def click(self):
        self.click_count += 1
        return None


def _strict_page_observation():
    return {
        "smsm_settings_nav_candidate_count": 1,
        "smsm_settings_nav_active": True,
        "smsm_device_nav_active": False,
        "smsm_ios_settings_active": True,
        "smsm_android_settings_active": False,
        "smsm_client_certificate_menu_candidate_count": 1,
        "smsm_client_certificate_menu_active": True,
        "smsm_certificate_search_input_candidate_count": 1,
        "smsm_certificate_add_icon_candidate_count": 1,
        "smsm_certificate_pathname_matches": True,
    }


def test_device_page_is_not_a_verified_client_certificate_page():
    observation = _strict_page_observation()
    observation.update({
        "smsm_settings_nav_active": False,
        "smsm_device_nav_active": True,
        "smsm_ios_settings_active": False,
        "smsm_client_certificate_menu_active": False,
        "smsm_certificate_search_input_candidate_count": 0,
        "smsm_certificate_add_icon_candidate_count": 0,
        "smsm_certificate_pathname_matches": False,
    })
    assert SmsmHandler._strict_client_certificate_page_verified(observation) is False


def test_common_header_without_selected_navigation_is_not_verified():
    observation = _strict_page_observation()
    observation.update({
        "smsm_settings_nav_candidate_count": 1,
        "smsm_settings_nav_unique": True,
        "smsm_settings_nav_active": False,
        "smsm_ios_settings_candidate_count": 1,
        "smsm_ios_settings_unique": True,
        "smsm_ios_settings_active": False,
        "smsm_client_certificate_menu_candidate_count": 1,
        "smsm_client_certificate_menu_unique": True,
        "smsm_client_certificate_menu_active": False,
    })
    assert SmsmHandler._strict_client_certificate_page_verified(observation) is False


@pytest.mark.parametrize("key", [
    "smsm_settings_nav_active",
    "smsm_ios_settings_active",
    "smsm_client_certificate_menu_active",
    "smsm_certificate_pathname_matches",
])
def test_each_required_active_or_path_condition_is_required(key):
    observation = _strict_page_observation()
    observation[key] = False
    assert SmsmHandler._strict_client_certificate_page_verified(observation) is False


def test_device_or_android_active_is_rejected():
    for key in ("smsm_device_nav_active", "smsm_android_settings_active"):
        observation = _strict_page_observation()
        observation[key] = True
        assert SmsmHandler._strict_client_certificate_page_verified(observation) is False


@pytest.mark.parametrize("count", [0, 2])
def test_certificate_search_input_must_be_unique(count):
    observation = _strict_page_observation()
    observation["smsm_certificate_search_input_candidate_count"] = count
    assert SmsmHandler._strict_client_certificate_page_verified(observation) is False


def test_certificate_add_icon_is_required():
    observation = _strict_page_observation()
    observation["smsm_certificate_add_icon_candidate_count"] = 0
    assert SmsmHandler._strict_client_certificate_page_verified(observation) is False


def test_strict_conditions_are_required_for_live_verified():
    assert SmsmHandler._strict_client_certificate_page_verified(_strict_page_observation()) is True


def _strict_snapshot_for_predicate():
    return {
        "smsm_strict_page_probe_completed": True,
        "smsm_strict_page_probe_snapshot_available": True,
        "smsm_settings_nav_candidate_count": 1,
        "smsm_settings_nav_active": True,
        "smsm_device_nav_active": False,
        "smsm_ios_settings_candidate_count": 1,
        "smsm_ios_settings_active": True,
        "smsm_android_settings_active": False,
        "smsm_client_certificate_menu_candidate_count": 1,
        "smsm_client_certificate_menu_active": True,
        "smsm_certificate_search_input_candidate_count": 1,
        "smsm_certificate_add_icon_candidate_count": 1,
    }


def test_snapshot_predicate_expands_scalar_metrics_and_all_conditions():
    result = SmsmHandler._evaluate_strict_client_certificate_snapshot(_strict_snapshot_for_predicate(), True)

    assert result["smsm_client_certificate_page_live_verified"] is True
    assert result["smsm_condition_page_specific_landmarks_verified"] is True
    assert result["smsm_condition_settings_nav_consistent_if_observed"] is True
    assert result["smsm_condition_client_certificate_menu_consistent_if_observed"] is True
    assert all(result[key] is True for key in (
        "smsm_condition_settings_nav_unique", "smsm_condition_settings_nav_active",
        "smsm_condition_device_nav_inactive", "smsm_condition_ios_settings_unique",
        "smsm_condition_ios_settings_active", "smsm_condition_android_settings_inactive",
        "smsm_condition_client_certificate_menu_unique", "smsm_condition_client_certificate_menu_active",
        "smsm_condition_search_input_unique", "smsm_condition_add_icon_present",
        "smsm_condition_pathname_matches",
    ))
    assert result["smsm_settings_nav_candidate_count"] == 1
    assert result["smsm_certificate_add_icon_candidate_count"] == 1


@pytest.mark.parametrize(("key", "value", "condition"), [
    ("smsm_settings_nav_active", False, "smsm_condition_settings_nav_active"),
    ("smsm_ios_settings_active", False, "smsm_condition_ios_settings_active"),
    ("smsm_client_certificate_menu_active", False, "smsm_condition_client_certificate_menu_active"),
    ("smsm_certificate_search_input_candidate_count", 0, "smsm_condition_search_input_unique"),
    ("smsm_certificate_search_input_candidate_count", 2, "smsm_condition_search_input_unique"),
    ("smsm_certificate_add_icon_candidate_count", 0, "smsm_condition_add_icon_present"),
])
def test_snapshot_predicate_records_each_failed_condition(key, value, condition):
    snapshot = _strict_snapshot_for_predicate()
    snapshot[key] = value

    result = SmsmHandler._evaluate_strict_client_certificate_snapshot(snapshot, True)

    assert result[condition] is False
    assert result["smsm_client_certificate_page_live_verified"] is False


def test_snapshot_predicate_accepts_unobserved_settings_and_menu():
    snapshot = _strict_snapshot_for_predicate()
    snapshot.update({
        "smsm_settings_nav_candidate_count": 0,
        "smsm_settings_nav_active": False,
        "smsm_device_nav_active": True,
        "smsm_client_certificate_menu_candidate_count": 0,
        "smsm_client_certificate_menu_active": False,
    })

    result = SmsmHandler._evaluate_strict_client_certificate_snapshot(snapshot, True)

    assert result["smsm_condition_page_specific_landmarks_verified"] is True
    assert result["smsm_condition_settings_nav_consistent_if_observed"] is True
    assert result["smsm_condition_client_certificate_menu_consistent_if_observed"] is True
    assert result["smsm_client_certificate_page_live_verified"] is True


@pytest.mark.parametrize(("key", "value"), [
    ("smsm_settings_nav_active", False),
    ("smsm_device_nav_active", True),
    ("smsm_client_certificate_menu_active", False),
])
def test_snapshot_predicate_rejects_observed_navigation_contradiction(key, value):
    snapshot = _strict_snapshot_for_predicate()
    snapshot[key] = value

    result = SmsmHandler._evaluate_strict_client_certificate_snapshot(snapshot, True)

    assert result["smsm_client_certificate_page_live_verified"] is False


def test_strict_probe_uses_fixed_phases_and_returns_json_safe_snapshot():
    handler = object.__new__(SmsmHandler)
    phases = []

    class Driver:
        def execute_script(self, script, _expected_path):
            phase = script.split("/* ", 1)[1].split(" */", 1)[0]
            phases.append(phase)
            values = {
                "probe_base_dom": {"smsm_certificate_pathname_matches": True},
                "resolve_settings_navigation": {"smsm_settings_nav_candidate_count": 1, "smsm_settings_nav_active": True, "smsm_device_nav_active": False},
                "resolve_device_navigation": {"smsm_device_nav_active": False},
                "resolve_ios_settings": {"smsm_ios_settings_candidate_count": 1, "smsm_ios_settings_active": True, "smsm_android_settings_active": False},
                "resolve_android_settings": {"smsm_android_settings_active": False},
                "resolve_client_certificate_menu": {"smsm_client_certificate_menu_candidate_count": 1, "smsm_client_certificate_menu_active": True},
                "resolve_certificate_search_input": {"smsm_certificate_search_input_candidate_count": 1},
                "resolve_certificate_add_icon": {"smsm_certificate_add_icon_candidate_count": 1},
                "evaluate_active_states": {"smsm_strict_page_active_states_evaluated": True},
                "build_snapshot": {"smsm_strict_page_snapshot_built": True},
            }
            return values[phase]

    observation = handler._strict_client_certificate_page_state(Driver(), "/ios/client-certificates")

    assert phases == [
        "probe_base_dom", "resolve_settings_navigation", "resolve_device_navigation",
        "resolve_ios_settings", "resolve_android_settings", "resolve_client_certificate_menu",
        "resolve_certificate_search_input", "resolve_certificate_add_icon",
        "evaluate_active_states", "build_snapshot",
    ]
    assert observation["smsm_strict_page_probe_completed"] is True
    assert observation["smsm_strict_page_probe_snapshot_available"] is True
    assert observation["smsm_client_certificate_page_live_verified"] is True
    assert all(isinstance(value, (str, int, float, bool)) or value is None for value in observation.values())


def test_strict_probe_javascript_exception_records_phase_and_no_zero_candidates():
    handler = object.__new__(SmsmHandler)

    class Driver:
        def execute_script(self, script, _expected_path):
            if "resolve_certificate_search_input" in script:
                raise JavascriptException("hidden test detail")
            return {"smsm_certificate_pathname_matches": True}

    observation = handler._strict_client_certificate_page_state(Driver(), "/ios/client-certificates")

    assert observation["smsm_strict_page_probe_failed_phase"] == "resolve_certificate_search_input"
    assert observation["smsm_strict_page_probe_javascript_error_name"] == "JavascriptException"
    assert observation["smsm_strict_page_probe_completed"] is False
    assert observation["smsm_strict_page_probe_snapshot_available"] is False
    for key in (
        "smsm_settings_nav_candidate_count", "smsm_ios_settings_candidate_count",
        "smsm_client_certificate_menu_candidate_count", "smsm_certificate_search_input_candidate_count",
        "smsm_certificate_add_icon_candidate_count",
    ):
        assert key not in observation
    assert all(observation[key] is False for key in (
        "smsm_settings_nav_observed", "smsm_ios_settings_observed",
        "smsm_client_certificate_menu_observed", "smsm_certificate_search_input_observed",
        "smsm_certificate_add_icon_observed",
    ))


def test_strict_probe_zero_candidates_completes_without_javascript_exception():
    handler = object.__new__(SmsmHandler)

    class Driver:
        def execute_script(self, script, _expected_path):
            phase = script.split("/* ", 1)[1].split(" */", 1)[0]
            if phase == "probe_base_dom":
                return {"smsm_certificate_pathname_matches": True}
            if phase == "resolve_settings_navigation":
                return {"smsm_settings_nav_candidate_count": 0, "smsm_settings_nav_active": False, "smsm_device_nav_active": False}
            if phase == "resolve_ios_settings":
                return {"smsm_ios_settings_candidate_count": 0, "smsm_ios_settings_active": False, "smsm_android_settings_active": False}
            if phase == "resolve_client_certificate_menu":
                return {"smsm_client_certificate_menu_candidate_count": 0, "smsm_client_certificate_menu_active": False}
            if phase == "resolve_certificate_search_input":
                return {"smsm_certificate_search_input_candidate_count": 0}
            if phase == "resolve_certificate_add_icon":
                return {"smsm_certificate_add_icon_candidate_count": 0}
            return {}

    observation = handler._strict_client_certificate_page_state(Driver(), "/ios/client-certificates")

    assert observation["smsm_strict_page_probe_completed"] is True
    assert observation["smsm_strict_page_probe_exception_type"] == ""
    assert observation["smsm_client_certificate_page_live_verified"] is False


def test_strict_probe_has_no_duplicate_attrstats_declaration_or_dom_return():
    source = inspect.getsource(SmsmHandler._strict_client_certificate_page_state)

    assert "const attrStats" not in source
    assert "return item" not in source
    assert "return {" in source


def test_candidate_zero_observation_is_not_verified():
    observation = _strict_page_observation()
    observation.update({
        "smsm_settings_nav_candidate_count": 0,
        "smsm_ios_settings_candidate_count": 0,
        "smsm_client_certificate_menu_candidate_count": 0,
        "smsm_certificate_search_input_candidate_count": 0,
        "smsm_certificate_add_icon_candidate_count": 0,
    })
    assert SmsmHandler._strict_client_certificate_page_verified(observation) is False


def test_structural_diagnostics_are_scalar_and_do_not_replace_strict_predicate():
    observation = _strict_page_observation()
    observation.update({
        "smsm_settings_nav_raw_match_count": 3,
        "smsm_settings_nav_tag_a_count": 1,
        "smsm_settings_nav_role_attribute_count": 1,
        "smsm_settings_nav_active_attribute_count": 1,
        "smsm_certificate_add_icon_raw_candidate_count": 2,
    })
    assert all(isinstance(value, int) for key, value in observation.items() if key.endswith("count"))
    assert SmsmHandler._strict_client_certificate_page_verified(observation) is True


def test_client_certificate_navigation_requires_saved_manifest():
    handler = object.__new__(SmsmHandler)
    with pytest.raises(RuntimeError):
        handler.open_client_certificate_page(None)


def test_navigation_failure_stops_before_preparation_stage():
    calls = []

    def open_page(_context):
        calls.append("open")
        raise RuntimeError("page not verified")

    def prepare(_context):
        calls.append("prepare")

    workflow = SingleCertificateWorkflow(
        handlers={
            "open": open_page,
            "prepare": prepare,
        },
        context=WorkflowContext(),
        stages=("open", "prepare"),
    )
    result = workflow.run()
    assert result["failed_stage"] == "open"
    assert calls == ["open"]


class Driver:
    def __init__(self, rows=None, panels=None):
        self.rows = rows or []
        self.panels = panels or []
        self.search_input = Element(type_name="text", name="imei", value="")
        self.search_button = Element(name="search")

    def find_elements(self, _by, selector):
        if selector == "table tbody tr":
            return self.rows
        if selector == "input":
            return [self.search_input]
        if selector == "button, input[type='submit']":
            return [self.search_button]
        if selector.startswith("button,a,[role='button']"):
            return [self.search_button]
        if selector.startswith("[role='complementary']"):
            return self.panels
        return []


class Browser:
    def __init__(self, driver):
        self.driver = driver

    def wait_for_page_ready(self):
        return None


def make_handler(driver):
    return SmsmHandler(browser=Browser(driver), logger=object(), smsm_config=object())


def make_prepare_service(handler):
    service = object.__new__(ProductionWorkflowService)
    service.smsm = handler
    return service


def prepare_context():
    context = WorkflowContext()
    context.set_target({"alias": "alias", "serial": "serial", "imei": "123456789012345"})
    context.certificate_path = Path("123456789012345.p12")
    context.certificate_password = "secret"
    context.record("smsm_client_certificate_page_live_verified", True)
    return context


def test_service_prepare_calls_handler_once_when_context_is_complete():
    calls = []

    class Handler:
        def prepare_certificate_upload_for_diagnostic(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {"upload_ready": True, "smsm_prepare_failed_phase": "completed", "smsm_prepare_exception_type": ""}

    context = prepare_context()
    result = make_prepare_service(Handler()).smsm_prepare_certificate_upload(context)

    assert result["upload_ready"] is True
    assert len(calls) == 1
    assert context.observations["smsm_prepare_called"] is True
    assert context.observations["smsm_prepare_page_verified"] is True
    assert context.observations["smsm_prepare_target_imei_present"] is True
    assert context.observations["smsm_prepare_certificate_path_present"] is True
    assert context.observations["smsm_prepare_certificate_password_present"] is True


def test_service_prepare_does_not_call_handler_when_context_is_incomplete():
    calls = []

    class Handler:
        def prepare_certificate_upload_for_diagnostic(self, *args, **kwargs):
            calls.append(True)

    context = prepare_context()
    context.certificate_password = None

    with pytest.raises(WorkflowStageError):
        make_prepare_service(Handler()).smsm_prepare_certificate_upload(context)

    assert calls == []
    assert context.observations["smsm_prepare_failed_phase"] == "validate_preparation_context"
    assert context.observations["smsm_prepare_exception_type"] == "RuntimeError"


def test_service_prepare_persists_duplicate_gate_result():
    duplicate_result = {
        "smsm_prepare_duplicate_check_called": True,
        "smsm_prepare_duplicate_check_completed": True,
        "duplicate_search_called": True,
        "duplicate_check_determinate": True,
        "duplicate_exact_match_count": 1,
        "duplicate_same_name_match_count": 0,
        "duplicate_upload_allowed": False,
        "smsm_prepare_failed_phase": "check_certificate_duplicate",
        "smsm_prepare_exception_type": "",
        "upload_ready": False,
    }

    context = prepare_context()
    with pytest.raises(WorkflowStageError):
        make_prepare_service(type("Handler", (), {"prepare_certificate_upload_for_diagnostic": lambda self, *args, **kwargs: duplicate_result})()).smsm_prepare_certificate_upload(context)
    for key, value in duplicate_result.items():
        if key == "smsm_prepare_exception_type":
            assert context.observations[key] == "RuntimeError"
        else:
            assert context.observations[key] == value


def test_service_prepare_persists_handler_exception_phase():
    observation = {
        "smsm_prepare_duplicate_check_called": True,
        "smsm_prepare_duplicate_check_completed": False,
        "duplicate_search_called": True,
        "duplicate_check_determinate": False,
        "duplicate_exact_match_count": None,
        "duplicate_same_name_match_count": None,
        "duplicate_upload_allowed": False,
        "smsm_prepare_failed_phase": "check_certificate_duplicate",
        "smsm_prepare_exception_type": "RuntimeError",
    }

    class Handler:
        def prepare_certificate_upload_for_diagnostic(self, *args, **kwargs):
            error = RuntimeError("duplicate probe failed")
            error.observation = observation
            raise error

    context = prepare_context()
    with pytest.raises(RuntimeError, match="duplicate probe failed"):
        make_prepare_service(Handler()).smsm_prepare_certificate_upload(context)

    assert context.observations["smsm_prepare_failed_phase"] == "check_certificate_duplicate"
    assert context.observations["smsm_prepare_exception_type"] == "RuntimeError"
    assert context.observations["duplicate_check_determinate"] is False


@pytest.mark.parametrize(
    ("rows", "expected_allowed", "expected_exact"),
    [([], True, 0), (["IMEI 123456789012345"], False, 1), (["123456789012345", "123456789012345"], False, 2)],
)
def test_duplicate_gate_requires_zero_exact_imei_rows(rows, expected_allowed, expected_exact):
    result = make_handler(Driver(rows=[Element(text) for text in rows])).check_certificate_duplicate_by_imei("123456789012345")

    assert result["exact_imei_match_count"] == expected_exact
    assert result["upload_allowed"] is expected_allowed


def test_duplicate_gate_stops_on_same_name_certificate():
    result = make_handler(Driver(rows=[Element("certificate 123456789012345.p12")])).check_certificate_duplicate_by_imei("123456789012345")

    assert result["exact_imei_match_count"] == 0
    assert result["same_name_certificate_present"] is True
    assert result["upload_allowed"] is False


def test_duplicate_check_starts_with_called_flag_and_rejects_invalid_imei():
    result = make_handler(Driver()).check_certificate_duplicate_by_imei("invalid")

    assert result["duplicate_search_called"] is True
    assert result["duplicate_check_determinate"] is False
    assert result["duplicate_upload_allowed"] is False
    assert result["duplicate_check_failed_phase"] == "validate_duplicate_target"
    assert result["duplicate_check_exception_type"] == "ValueError"


@pytest.mark.parametrize("input_count", [0, 2])
def test_duplicate_check_stops_when_central_search_input_is_not_unique(input_count):
    class SearchInputDriver(Driver):
        def find_elements(self, by, selector):
            if selector == "input":
                return [Element(type_name="text", name="imei") for _ in range(input_count)]
            return super().find_elements(by, selector)

    result = make_handler(SearchInputDriver()).check_certificate_duplicate_by_imei("123456789012345")

    assert result["duplicate_search_called"] is True
    assert result["duplicate_check_determinate"] is False
    assert result["duplicate_upload_allowed"] is False
    assert result["duplicate_check_failed_phase"] == "resolve_duplicate_search_input"


def test_duplicate_check_inputs_imei_and_submits_search_once():
    handler = make_handler(Driver())

    result = handler.check_certificate_duplicate_by_imei("123456789012345")

    assert result["duplicate_search_called"] is True
    assert result["duplicate_check_determinate"] is True
    assert handler.browser.driver.search_input.send_keys_calls == ["123456789012345"]
    assert handler.browser.driver.search_button.click_count == 1


def test_duplicate_check_exception_never_becomes_zero_match_success(monkeypatch):
    handler = make_handler(Driver())
    monkeypatch.setattr(handler.browser, "wait_for_page_ready", lambda: (_ for _ in ()).throw(TimeoutError()))

    result = handler.check_certificate_duplicate_by_imei("123456789012345")

    assert result["duplicate_search_called"] is True
    assert result["duplicate_check_determinate"] is False
    assert result["duplicate_upload_allowed"] is False
    assert result["duplicate_check_failed_phase"] == "wait_duplicate_results"
    assert result["duplicate_check_exception_type"] == "TimeoutError"


def test_file_input_is_limited_to_right_panel_and_called_once(tmp_path: Path):
    certificate = tmp_path / "123456789012345.p12"
    certificate.write_bytes(b"certificate")
    file_input = Element(type_name="file")
    panel = Element()
    panel.find_elements = lambda _by, selector: [file_input] if selector == "input[type='file']" else []

    result = make_handler(Driver(panels=[panel])).set_certificate_file(certificate, allow_upload=True)

    assert result["file_input_send_keys_called"] is True
    assert len(file_input.send_keys_calls) == 1
    assert Path(file_input.send_keys_calls[0]) == certificate.resolve()


def test_file_input_stops_on_ambiguous_right_panel_candidates(tmp_path: Path):
    certificate = tmp_path / "123456789012345.p12"
    certificate.write_bytes(b"certificate")
    first = Element(type_name="file")
    second = Element(type_name="file")
    panel = Element()
    panel.find_elements = lambda _by, selector: [first, second] if selector == "input[type='file']" else []

    with pytest.raises(RuntimeError):
        make_handler(Driver(panels=[panel])).set_certificate_file(certificate, allow_upload=True)

    assert first.send_keys_calls == []
    assert second.send_keys_calls == []


def test_upload_preparation_stops_before_add_controls_on_duplicate(tmp_path: Path, monkeypatch):
    certificate = tmp_path / "123456789012345.p12"
    certificate.write_bytes(b"certificate")
    handler = make_handler(Driver())
    calls = []
    monkeypatch.setattr(handler, "login", lambda: calls.append("login"))
    monkeypatch.setattr(handler, "_open_client_certificate_management_for_preparation", lambda: calls.append("navigation"))
    monkeypatch.setattr(handler, "check_certificate_duplicate_by_imei", lambda _imei: {"exact_imei_match_count": 1, "same_name_certificate_match_count": 0})
    monkeypatch.setattr(handler, "_inspect_client_certificate_add_button_dom", lambda _driver: calls.append("plus_probe") or {"candidates": [{"element_index": 0}]})
    monkeypatch.setattr(handler, "set_certificate_file", lambda *_args, **_kwargs: calls.append("file"))
    monkeypatch.setattr(handler, "set_certificate_password", lambda *_args, **_kwargs: calls.append("password"))

    result = handler.prepare_certificate_upload_for_diagnostic(certificate, "secret", "123456789012345")

    assert result["duplicate_search_called"] is True
    assert result["duplicate_exact_match_count"] == 1
    assert result["upload_ready"] is False
    assert calls == ["login", "navigation"]


def test_upload_preparation_fills_controls_but_never_saves_or_uploads(tmp_path: Path, monkeypatch):
    certificate = tmp_path / "123456789012345.p12"
    certificate.write_bytes(b"certificate")
    file_input = Element(type_name="file")
    panel = Element()
    panel.find_elements = lambda _by, selector: [file_input] if selector == "input[type='file']" else []

    class PreparationDriver(Driver):
        def find_elements(self, by, selector):
            if selector == "input[type='file']":
                return [file_input]
            if selector == "button,a,[role='button'],[role='link'],[tabindex],input[type='button'],input[type='submit']":
                return [Element("追加")]
            return super().find_elements(by, selector)

    handler = make_handler(PreparationDriver(panels=[panel]))
    monkeypatch.setattr(handler, "login", lambda: None)
    monkeypatch.setattr(handler, "_open_client_certificate_management_for_preparation", lambda: None)
    monkeypatch.setattr(handler, "check_certificate_duplicate_by_imei", lambda _imei: {"exact_imei_match_count": 0, "same_name_certificate_match_count": 0, "duplicate_check_determinate": True})
    monkeypatch.setattr(handler, "_inspect_client_certificate_add_button_dom", lambda _driver: {"candidates": [{"element_index": 0}], "add_button_candidate_count": 1, "add_button_unique": True})
    monkeypatch.setattr(handler, "_inspect_add_form_controls_dom", lambda _driver: {"add_form_opened": True, "right_side_visible_container_count": 1, "file_input_dom_count": 1, "file_input_enabled_count": 1, "file_input_inside_right_panel_count": 1, "password_input_count": 1, "submit_button_candidate_count": 1})
    monkeypatch.setattr(handler, "_wait_and_resolve_certificate_password_input", lambda _driver, timeout: {"password_input_visible_count": 1, "password_input_candidate_count": 1, "password_input_unique": True})
    monkeypatch.setattr(handler, "_send_certificate_password_in_add_form", lambda _driver, _password: {"password_input_count": 1, "password_input_send_keys_called": True})
    monkeypatch.setattr(handler, "_resolve_certificate_save_button_in_add_form", lambda _driver: {"save_button_candidate_count": 1, "save_button_enabled": True, "save_button_displayed": True})

    result = handler.prepare_certificate_upload_for_diagnostic(certificate, "secret", "123456789012345")

    expected = {
        "duplicate_search_called": True,
        "duplicate_exact_match_count": 0,
        "duplicate_same_name_match_count": 0,
        "duplicate_check_determinate": True,
        "certificate_file_input_send_keys_called": True,
        "file_input_dom_count": 1,
        "file_input_enabled_count": 1,
        "file_input_inside_right_panel_count": 1,
        "file_input_count": 1,
        "file_input_unique": True,
        "file_input_send_keys_called": True,
        "selected_certificate_filename_exact_imei_match": True,
        "password_input_send_keys_called": True,
        "password_input_nonblank": True,
        "save_button_click_called": False,
        "certificate_upload_called": False,
        "upload_ready": True,
        "smsm_prepare_failed_phase": "completed",
    }
    assert {key: result[key] for key in expected} == expected
    assert len(file_input.send_keys_calls) == 1


def test_submit_upload_requires_ready_before_click(monkeypatch):
    handler = make_handler(Driver())
    handler._last_preparation_observation = {"certificate_upload_ready": False}
    with pytest.raises(RuntimeError):
        handler.submit_certificate_upload(allow_upload=True, imei="123456789012345")


@pytest.mark.parametrize("candidate_count", [0, 2])
def test_submit_upload_refetch_cardinality_stops_without_click(monkeypatch, candidate_count):
    handler = make_handler(Driver())
    handler._last_preparation_observation = {"certificate_upload_ready": True}
    candidates = [Element("保存") for _ in range(candidate_count)]
    monkeypatch.setattr(handler, "_add_form_control_groups", lambda _driver: {"container": Element(), "saveCandidates": candidates})

    with pytest.raises(RuntimeError) as error:
        handler.submit_certificate_upload(allow_upload=True, imei="123456789012345")

    assert error.value.observation["save_button_click_count"] == 0
    assert sum(item.click_count for item in candidates) == 0


def test_submit_upload_clicks_once_and_verifies_one_post_search_match(monkeypatch):
    handler = make_handler(Driver())
    handler._last_preparation_observation = {"certificate_upload_ready": True}
    save = Element("保存")
    monkeypatch.setattr(handler, "_add_form_control_groups", lambda _driver: {"container": Element(), "saveCandidates": [save]})
    monkeypatch.setattr(handler, "_inspect_client_certificate_upload_dom", lambda: {"upload_form_count": 0, "certificate_table_count": 1, "upload_success_message_detected": False})
    monkeypatch.setattr(handler, "check_certificate_duplicate_by_imei", lambda _imei: {"duplicate_check_determinate": True, "exact_imei_match_count": 1})

    result = handler.submit_certificate_upload(allow_upload=True, imei="123456789012345")

    assert result["save_button_click_called"] is True
    assert result["save_button_click_count"] == 1
    assert result["certificate_upload_called"] is True
    assert result["certificate_upload_completion_verified"] is True
    assert result["post_upload_search_called"] is True
    assert result["post_upload_exact_match_count"] == 1
    assert result["certificate_upload_verified"] is True
    assert save.click_count == 1


@pytest.mark.parametrize("match_count", [0, 2])
def test_submit_upload_rejects_zero_or_multiple_post_search_matches(monkeypatch, match_count):
    handler = make_handler(Driver())
    handler._last_preparation_observation = {"certificate_upload_ready": True}
    save = Element("保存")
    monkeypatch.setattr(handler, "_add_form_control_groups", lambda _driver: {"container": Element(), "saveCandidates": [save]})
    monkeypatch.setattr(handler, "_inspect_client_certificate_upload_dom", lambda: {"upload_form_count": 0, "certificate_table_count": 1, "upload_success_message_detected": False})
    monkeypatch.setattr(handler, "check_certificate_duplicate_by_imei", lambda _imei: {"duplicate_check_determinate": True, "exact_imei_match_count": match_count})

    with pytest.raises(RuntimeError) as error:
        handler.submit_certificate_upload(allow_upload=True, imei="123456789012345")

    assert error.value.observation["post_upload_exact_match_count"] == match_count
    assert error.value.observation["certificate_upload_verified"] is False
    assert save.click_count == 1


@pytest.mark.parametrize("candidates", [[], [{"element_index": 0}, {"element_index": 1}]])
def test_upload_preparation_add_button_candidate_count_stops_at_resolve_add_button(tmp_path: Path, monkeypatch, candidates):
    certificate = tmp_path / "123456789012345.p12"
    certificate.write_bytes(b"certificate")
    handler = make_handler(Driver())
    monkeypatch.setattr(handler, "login", lambda: None)
    monkeypatch.setattr(handler, "_open_client_certificate_management_for_preparation", lambda: None)
    monkeypatch.setattr(handler, "check_certificate_duplicate_by_imei", lambda _imei: {"exact_imei_match_count": 0, "same_name_certificate_match_count": 0, "duplicate_check_determinate": True})
    monkeypatch.setattr(handler, "_inspect_client_certificate_add_button_dom", lambda _driver: {"candidates": candidates, "add_button_candidate_count": len(candidates), "add_button_unique": len(candidates) == 1})

    with pytest.raises(RuntimeError) as error:
        handler.prepare_certificate_upload_for_diagnostic(certificate, "secret", "123456789012345")

    assert error.value.observation["smsm_prepare_failed_phase"] == "resolve_add_button"
    assert error.value.observation["add_button_candidate_count"] == len(candidates)
    assert error.value.observation["add_button_click_called"] is False
    assert error.value.observation["file_input_send_keys_called"] is not True


def test_upload_preparation_click_exception_stops_at_click_add_button(tmp_path: Path, monkeypatch):
    certificate = tmp_path / "123456789012345.p12"
    certificate.write_bytes(b"certificate")
    handler = make_handler(Driver())
    monkeypatch.setattr(handler, "login", lambda: None)
    monkeypatch.setattr(handler, "_open_client_certificate_management_for_preparation", lambda: None)
    monkeypatch.setattr(handler, "check_certificate_duplicate_by_imei", lambda _imei: {"exact_imei_match_count": 0, "same_name_certificate_match_count": 0, "duplicate_check_determinate": True})
    monkeypatch.setattr(handler, "_inspect_client_certificate_add_button_dom", lambda _driver: {"candidates": [{"element_index": 0}], "add_button_candidate_count": 1, "add_button_unique": True})
    handler.browser.driver.search_button.click = lambda: (_ for _ in ()).throw(RuntimeError("click"))

    with pytest.raises(RuntimeError) as error:
        handler.prepare_certificate_upload_for_diagnostic(certificate, "secret", "123456789012345")

    assert error.value.observation["smsm_prepare_failed_phase"] == "click_add_button"
    assert error.value.observation["file_input_send_keys_called"] is not True


def test_upload_preparation_waits_for_add_form_before_resolving_file_input(tmp_path: Path, monkeypatch):
    certificate = tmp_path / "123456789012345.p12"
    certificate.write_bytes(b"certificate")
    handler = make_handler(Driver())
    calls = []
    monkeypatch.setattr(handler, "login", lambda: None)
    monkeypatch.setattr(handler, "_open_client_certificate_management_for_preparation", lambda: None)
    monkeypatch.setattr(handler, "check_certificate_duplicate_by_imei", lambda _imei: {"exact_imei_match_count": 0, "same_name_certificate_match_count": 0, "duplicate_check_determinate": True})
    monkeypatch.setattr(handler, "_inspect_client_certificate_add_button_dom", lambda _driver: {"candidates": [{"element_index": 0}], "add_button_candidate_count": 1, "add_button_unique": True})
    monkeypatch.setattr(handler, "_inspect_add_form_controls_dom", lambda _driver: calls.append("form") or {"add_form_opened": False, "right_side_visible_container_count": 0})
    monkeypatch.setattr(handler, "set_certificate_file", lambda *_args, **_kwargs: calls.append("file") or {"file_input_send_keys_called": True})

    with pytest.raises(Exception) as error:
        handler.prepare_certificate_upload_for_diagnostic(certificate, "secret", "123456789012345")

    assert error.value.observation["smsm_prepare_failed_phase"] == "wait_initial_add_form"
    assert calls and "file" not in calls


def test_upload_preparation_records_fixed_start_metrics_when_context_is_missing(tmp_path: Path):
    certificate = tmp_path / "123456789012345.p12"
    certificate.write_bytes(b"certificate")
    handler = make_handler(Driver())

    result = handler.prepare_certificate_upload_for_diagnostic(certificate, "", "")

    assert result["smsm_prepare_called"] is True
    assert result["smsm_prepare_target_imei_present"] is False
    assert result["smsm_prepare_certificate_password_present"] is False
    assert result["smsm_prepare_duplicate_check_called"] is False
    assert result["smsm_prepare_failed_phase"] == "validate_preparation_context"


@pytest.mark.parametrize("duplicate", [
    {"exact_imei_match_count": 1, "same_name_certificate_match_count": 0, "duplicate_check_determinate": True},
    {"exact_imei_match_count": 0, "same_name_certificate_match_count": 0, "duplicate_check_determinate": False},
])
def test_upload_preparation_never_reaches_add_button_for_duplicate_or_indeterminate(tmp_path: Path, monkeypatch, duplicate):
    certificate = tmp_path / "123456789012345.p12"
    certificate.write_bytes(b"certificate")
    handler = make_handler(Driver())
    calls = []
    monkeypatch.setattr(handler, "login", lambda: calls.append("login"))
    monkeypatch.setattr(handler, "_open_client_certificate_management_for_preparation", lambda: calls.append("navigation"))
    monkeypatch.setattr(handler, "_inspect_client_certificate_add_button_dom", lambda _driver: calls.append("plus") or {"candidates": []})
    monkeypatch.setattr(handler, "check_certificate_duplicate_by_imei", lambda _imei: duplicate)

    result = handler.prepare_certificate_upload_for_diagnostic(certificate, "secret", "123456789012345")

    assert result["smsm_prepare_duplicate_check_called"] is True
    assert result["smsm_prepare_duplicate_check_completed"] is (duplicate["duplicate_check_determinate"] is True)
    assert result["upload_ready"] is False
    assert calls == ["login", "navigation"]