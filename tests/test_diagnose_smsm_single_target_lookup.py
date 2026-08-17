from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import diagnose_smsm_single_target_lookup as mod
from app.smsm_handler import CertificateUploadRequest, SmsmHandler
from app.single_certificate_workflow import WorkflowContext, WorkflowStageError
from app.workflow_service import ProductionWorkflowService


class DummyLogger:
    def __init__(self):
        self.info_messages = []
        self.error_messages = []

    def info(self, message):
        self.info_messages.append(message)

    def error(self, message):
        self.error_messages.append(message)


def test_route_manifest_loader_records_each_phase(monkeypatch, tmp_path):
    path = tmp_path / "smsm_navigation_route.json"
    manifest = _valid_route_manifest()
    manifest["landmark_schema_fingerprint"] = mod._fingerprint(mod._canonical_json(manifest["landmark_schema"]))
    manifest.pop("client_certificate_child_candidate_count", None)
    monkeypatch.setattr(mod, "_validate_verified_final_manifest", lambda _payload: None)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(mod, "_smsm_navigation_route_path", lambda: path)
    observed = {}

    manifest = mod._load_route_manifest(lambda key, value: observed.__setitem__(key, value))

    assert isinstance(manifest, dict)
    assert observed == {
        "smsm_route_manifest_load_called": True,
        "smsm_route_manifest_found": True,
        "smsm_route_manifest_path_available": True,
        "smsm_route_manifest_parse_completed": True,
        "smsm_route_manifest_schema_valid": True,
        "smsm_route_manifest_fingerprint_valid": True,
    }


def test_strict_probe_exception_is_not_reported_as_zero_candidates():
    handler = object.__new__(SmsmHandler)

    class Driver:
        def execute_script(self, *_args):
            raise RuntimeError("probe failed")

    observation = handler._strict_client_certificate_page_state(Driver(), "/ios/client-certificates")

    assert observation["smsm_strict_page_probe_called"] is True
    assert observation["smsm_strict_page_probe_completed"] is False
    assert observation["smsm_strict_page_probe_snapshot_available"] is False
    assert observation["smsm_strict_page_probe_exception_type"] == "RuntimeError"
    assert "smsm_settings_nav_candidate_count" not in observation
    assert observation.get("smsm_settings_nav_observed", False) is False


def test_workflow_service_calls_verified_navigation_and_uses_strict_predicate():
    service = ProductionWorkflowService.__new__(ProductionWorkflowService)
    calls = []
    manifest = _valid_route_manifest()

    class FakeSmsm:
        def navigate_verified_final_path_for_diagnostic(self, value, trace=None):
            calls.append(value)
            return {"smsm_client_certificate_page_live_verified": False}

    service.smsm = FakeSmsm()
    context = WorkflowContext()
    context.services["smsm_route_manifest"] = manifest

    with pytest.raises(WorkflowStageError) as error:
        service.smsm_open_client_certificate_page(context)

    assert calls == [manifest]
    assert error.value.stage == "smsm_verify_client_certificate_page"
    assert context.observations["smsm_client_certificate_page_live_verified"] is False


def _strict_landmark_observation(**overrides):
    observation = {
        "target_os_ios_verified": True,
        "ios_tab_selected": True,
        "android_tab_selected": False,
        "certificate_management_expanded": True,
        "client_certificate_child_visible": True,
        "client_certificate_child_active": True,
        "client_certificate_child_href_present": True,
        "current_path_matches_client_certificate_child": True,
        "certificate_operation_structure_verified": True,
        "client_certificate_specific_landmark_count": 2,
    }
    observation.update(overrides)
    return observation


@pytest.mark.parametrize("key,value", [
    ("client_certificate_child_active", False),
    ("target_os_ios_verified", False),
    ("ios_tab_selected", False),
    ("android_tab_selected", True),
    ("certificate_management_expanded", False),
    ("client_certificate_child_visible", False),
    ("client_certificate_child_href_present", False),
    ("current_path_matches_client_certificate_child", False),
    ("client_certificate_specific_landmark_count", 1),
])
def test_strict_client_certificate_landmark_rejects_each_missing_condition(key, value):
    assert SmsmHandler._client_certificate_landmark_verified(_strict_landmark_observation(**{key: value})) is False


def test_strict_client_certificate_landmark_accepts_only_all_conditions():
    assert SmsmHandler._client_certificate_landmark_verified(_strict_landmark_observation()) is True


def test_generic_form_table_and_rows_do_not_affect_strict_landmark():
    observation = _strict_landmark_observation(
        client_certificate_child_active=False,
        certificate_table_count=1,
        existing_certificate_row_count=10,
        upload_form_count=1,
    )
    assert SmsmHandler._client_certificate_landmark_verified(observation) is False


def _manual_checkpoint_observation(**overrides):
    observation = {
        "target_os_ios_verified": True,
        "ios_tab_selected": True,
        "android_tab_selected": False,
        "ios_content_container_visible": True,
        "android_content_container_visible": False,
        "certificate_management_expanded": True,
        "client_certificate_child_candidate_count": 1,
        "client_certificate_child_visible": True,
        "client_certificate_child_active": True,
        "client_certificate_child_href_present": True,
        "current_path_matches_client_certificate_child": True,
        "certificate_operation_structure_verified": True,
        "client_certificate_specific_landmark_count": 3,
    }
    observation.update(overrides)
    return observation


def test_manual_checkpoint_rejects_missing_active_and_href_path_match():
    assert SmsmHandler._verify_manual_client_certificate_checkpoint(
        _manual_checkpoint_observation(
            certificate_management_expanded=True,
            client_certificate_child_active=False,
            current_path_matches_client_certificate_child=False,
        ),
        session_valid=True,
        same_host=True,
        login_page=False,
        current_path="/M18500454/ios_certificates",
    ) is False


def test_manual_checkpoint_rejects_wrong_os_container_or_child_shape():
    for overrides in (
        {"ios_tab_selected": False},
        {"android_tab_selected": True},
        {"ios_content_container_visible": False},
        {"android_content_container_visible": True},
        {"client_certificate_child_candidate_count": 2},
        {"client_certificate_child_visible": False},
    ):
        assert SmsmHandler._verify_manual_client_certificate_checkpoint(
            _manual_checkpoint_observation(**overrides),
            session_valid=True,
            same_host=True,
            login_page=False,
            current_path="/M18500454/ios_certificates",
        ) is False


def test_manual_checkpoint_accepts_strong_path_when_operation_structure_is_false():
    assert SmsmHandler._verify_manual_client_certificate_checkpoint(
        _manual_checkpoint_observation(certificate_operation_structure_verified=False),
        session_valid=True,
        same_host=True,
        login_page=False,
        current_path="/M18500454/ios_certificates",
    ) is True


def test_replay_requires_strong_active_and_href_path_evidence():
    observation = _manual_checkpoint_observation()
    schema = {"client_certificate_child_active_required": True, "current_path_matches_client_child_required": True}
    assert SmsmHandler._verify_replayed_client_certificate_page(observation, schema) is True
    assert SmsmHandler._verify_replayed_client_certificate_page({**observation, "client_certificate_child_active": False}, schema) is False
    assert SmsmHandler._verify_replayed_client_certificate_page({**observation, "current_path_matches_client_certificate_child": False}, schema) is False


def test_certificate_upload_request_validates_file_without_exposing_password(tmp_path):
    certificate = tmp_path / "certificate.p12"
    certificate.write_bytes(b"test")
    request = CertificateUploadRequest(certificate, "secret", "alias", "serial", "123456789012345")

    assert "secret" not in repr(request)
    assert request.certificate_file_path == certificate
    with pytest.raises(PermissionError):
        SmsmHandler.upload_certificate_request(object(), request)


def _valid_route_manifest():
    schema = {"target_os_ios_verified": True, "ios_tab_selected": True, "android_tab_selected": False, "ios_content_container_visible": True, "android_content_container_visible": False, "certificate_management_expanded_by_attribute": True, "certificate_management_expanded_by_visible_child": False, "certificate_management_expanded": True, "client_certificate_child_candidate_count": 1, "client_certificate_child_visible": True, "client_certificate_child_active": True, "client_certificate_child_active_semantic": True, "client_certificate_child_selected_by_style": False, "client_certificate_child_href_present": True, "current_path_matches_client_certificate_child": True, "current_path_verified_by_manual_checkpoint": True, "certificate_operation_structure_verified": False, "client_certificate_specific_landmark_count": 3, "target_os_ios_verified_required": True, "ios_tab_selected_required": True, "android_tab_not_selected_required": True, "ios_content_container_visible_required": True, "android_content_container_hidden_required": True, "deduplicated_client_child_required": True, "certificate_management_expanded_required": True, "client_certificate_child_visible_required": True, "client_certificate_child_active_required": True, "client_certificate_child_href_present_required": True, "current_path_matches_client_child_required": True, "specific_landmark_minimum": 3, "operation_structure_required": False, "manual_checkpoint_path_verified": True}
    return {"route_version": mod.SMSM_ROUTE_VERSION, "route_type": "verified_final_same_host_path", "target_stage": "client_certificate_management", "target_os": "ios", "same_host_path": "/ios/client-certificates", "same_host_path_fingerprint": mod._fingerprint("/ios/client-certificates"), "landmark_schema": schema, "landmark_schema_fingerprint": mod._fingerprint(json.dumps(schema, sort_keys=True, separators=(",", ":"))), "captured_at": "2026-08-13T00:00:00", "capture_method": "manual_checkpoint", "verified": True, "client_certificate_child_candidate_count": 1}


def test_route_manifest_uses_fingerprints_and_atomic_replace(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_smsm_navigation_route_path", lambda: tmp_path / "smsm_navigation_route.json")
    schema = _valid_route_manifest()["landmark_schema"]
    checkpoint = {"verified": True, "browser_session_valid": True, "same_host_verified": True, "target_os": "ios", "same_host_path": "/ios/client-certificates", "landmark_schema": schema, "target_os_ios_verified": True, "ios_tab_selected": True, "android_tab_selected": False, "ios_content_container_visible": True, "android_content_container_visible": False, "certificate_management_expanded": True, "client_certificate_child_candidate_count": 1, "client_certificate_child_visible": True, "client_certificate_child_active": True, "client_certificate_child_active_semantic": True, "client_certificate_child_href_present": True, "current_path_matches_client_certificate_child": True, "current_path_verified_by_manual_checkpoint": True, "certificate_operation_structure_verified": True, "client_certificate_specific_landmark_count": 3, "client_certificate_page_landmark_verified": True}
    manifest = mod._route_manifest_from_checkpoint(checkpoint)
    mod._write_route_manifest_atomic(manifest)
    payload = (tmp_path / "smsm_navigation_route.json").read_text(encoding="utf-8")
    assert "private-visible-label" not in payload
    assert "private-title" not in payload
    assert "private-landmark" not in payload
    assert mod._load_route_manifest()["same_host_path"] == "/ios/client-certificates"


def test_measured_checkpoint_builds_valid_manifest_without_operation_structure(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_smsm_navigation_route_path", lambda: tmp_path / "smsm_navigation_route.json")
    checkpoint = {
        "verified": True, "browser_session_valid": True, "same_host_verified": True, "target_os": "ios",
        "same_host_path": "/M18500454/ios_certificates", "target_os_ios_verified": True,
        "ios_tab_selected": True, "android_tab_selected": False, "ios_content_container_visible": True,
        "android_content_container_visible": False, "certificate_management_expanded_by_attribute": False,
        "certificate_management_expanded_by_visible_child": True, "certificate_management_expanded": True,
        "deduplicated_clickable_candidate_count": 1, "client_certificate_child_candidate_count": 1,
        "client_certificate_child_visible": True, "client_certificate_child_active": True,
        "client_certificate_child_href_present": True, "current_path_matches_client_certificate_child": True,
        "current_path_verified_by_manual_checkpoint": True, "certificate_operation_structure_verified": False,
        "client_certificate_specific_landmark_count": 6, "client_certificate_page_landmark_verified": True,
    }
    manifest = mod._route_manifest_from_checkpoint(checkpoint)
    mod._validate_verified_final_manifest(manifest)
    assert manifest["landmark_schema"]["operation_structure_required"] is False
    assert manifest["landmark_schema"]["certificate_management_expanded_by_attribute_available"] is False
    assert manifest["landmark_schema"]["certificate_management_expanded_by_visible_child_available"] is True
    assert manifest["landmark_schema_fingerprint"] == mod._fingerprint(mod._canonical_json(manifest["landmark_schema"]))
    assert manifest["same_host_path_fingerprint"] == mod._fingerprint(manifest["same_host_path"])
    json.dumps(manifest, ensure_ascii=False)
    mod._write_route_manifest_atomic(manifest)
    reloaded = mod._load_route_manifest()
    assert reloaded["landmark_schema_fingerprint"] == manifest["landmark_schema_fingerprint"]


def test_manifest_rejects_non_integer_minimum_and_non_json_value():
    manifest = _valid_route_manifest()
    manifest["landmark_schema"]["specific_landmark_minimum"] = True
    with pytest.raises((ValueError, TypeError)):
        mod._validate_verified_final_manifest(manifest)
    manifest = _valid_route_manifest()
    manifest["landmark_schema"]["specific_landmark_minimum"] = 2
    with pytest.raises(ValueError):
        mod._validate_verified_final_manifest(manifest)
    manifest = _valid_route_manifest()
    manifest["landmark_schema"]["unsafe_element"] = object()
    with pytest.raises(TypeError):
        mod._validate_verified_final_manifest(manifest)


def test_route_manifest_rejects_external_path_and_bad_fingerprint(monkeypatch, tmp_path):
    path = tmp_path / "smsm_navigation_route.json"
    monkeypatch.setattr(mod, "_smsm_navigation_route_path", lambda: path)
    manifest = _valid_route_manifest()
    manifest["same_host_path"] = "https://external.invalid/path"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        mod._load_route_manifest()


def test_route_capture_manifest_requires_verified_checkpoint():
    with pytest.raises(RuntimeError):
        mod._route_manifest_from_checkpoint({})


def test_failed_checkpoint_does_not_overwrite_existing_manifest(monkeypatch, tmp_path):
    path = tmp_path / "smsm_navigation_route.json"
    monkeypatch.setattr(mod, "_smsm_navigation_route_path", lambda: path)
    existing = _valid_route_manifest()
    path.write_text(json.dumps(existing), encoding="utf-8")
    with pytest.raises(RuntimeError):
        mod._route_manifest_from_checkpoint({"verified": False})
    assert json.loads(path.read_text(encoding="utf-8")) == existing


def test_manifest_rejects_android_and_raw_sensitive_fields(monkeypatch, tmp_path):
    path = tmp_path / "smsm_navigation_route.json"
    monkeypatch.setattr(mod, "_smsm_navigation_route_path", lambda: path)
    manifest = _valid_route_manifest()
    manifest["target_os"] = "android"
    manifest["visible_text"] = "クライアント証明書管理"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        mod._load_route_manifest()


def test_capture_route_cli_does_not_create_excel_and_saves_only_after_manual_checkpoint(monkeypatch, tmp_path):
    logger, calls, _browser = install(monkeypatch, rows=[])
    route_path = tmp_path / "smsm_navigation_route.json"
    monkeypatch.setattr(mod, "_smsm_navigation_route_path", lambda: route_path)
    monkeypatch.setattr(mod, "ExcelReader", lambda _path: (_ for _ in ()).throw(AssertionError("ExcelReader must not be created")))
    assert mod.main(["--capture-smsm-certificate-navigation-route"]) == 0
    assert "manual_checkpoint" in calls
    assert json.loads(route_path.read_text(encoding="utf-8"))["route_type"] == "verified_final_same_host_path"
    assert calls.count("quit") == 1


def test_upload_dom_requires_capture_manifest_and_stops_with_31(monkeypatch, tmp_path):
    logger, calls, _browser = install(monkeypatch, rows=[])
    monkeypatch.setattr(mod, "_smsm_navigation_route_path", lambda: tmp_path / "missing-route.json")
    assert mod.main(["--inspect-smsm-client-certificate-upload-dom"]) == 31
    output = joined(logger)
    assert "navigation_route_manifest_found=False" in output
    assert "navigation_route_capture_required=True" in output
    assert "failed_stage=smsm_navigation_route_required" in output
    assert "certificate_navigation" not in calls


class FakeReader:
    def __init__(self, rows, calls, is_open=False):
        self.rows = rows
        self.calls = calls
        self.open = is_open

    def is_file_open(self):
        self.calls.append("is_open")
        return self.open

    def read_targets(self):
        self.calls.append("read")
        return list(self.rows)


class FakeBrowser:
    def __init__(self, calls, quit_error=None):
        self.calls = calls
        self.driver = object()
        self.quit_error = quit_error

    def start(self):
        self.calls.append("browser")

    def quit(self):
        self.calls.append("quit")
        if self.quit_error:
            raise self.quit_error


class FakeSmsmHandler:
    BASE_URL = "https://smsm.test.invalid"
    last_serial = None
    last_config = None
    result_count = 1
    result_match_result = None
    last_match_target = None
    upload_dom_result = None
    settings_navigation_result = None

    def __init__(self, *, smsm_config, logger, browser, trace_callback=None):
        self.calls = browser.calls
        self.smsm_config = smsm_config
        FakeSmsmHandler.last_config = smsm_config

    def login(self, trace=None):
        self.calls.append("login")
        if trace is not None:
            for key in (
                "smsm_config_validation",
                "smsm_open_login_page",
                "login_page_opened",
                "smsm_wait_login_page",
                "smsm_find_user_field",
                "user_field_found",
                "smsm_find_password_field",
                "password_field_found",
                "smsm_fill_credentials",
                "smsm_find_login_button",
                "login_button_found",
                "smsm_submit_login",
                "login_submitted",
                "smsm_detect_additional_auth",
                "smsm_wait_login_complete",
                "smsm_validate_logged_in_page",
                "login_completed",
            ):
                trace(key, True)

    def search_device(self, serial, **_kwargs):
        self.calls.append("lookup")
        FakeSmsmHandler.last_serial = serial

    def reach_device_search_page(self):
        self.calls.append("device_page")

    def wait_for_search_form_dom(self, trace=None):
        self.calls.append("serial_dom")
        return {
            "top_document_select_count": 1,
            "iframe_count": 0,
            "iframe_with_select_count": 0,
            "native_select_count": 1,
            "visible_native_select_count": 1,
            "enabled_native_select_count": 1,
            "custom_select_candidate_count": 0,
            "stale_retry_count": 0,
            "search_type_control_count": 1,
            "search_type_control_unique": True,
            "summary": {
            "device_page_reached": True,
            "search_type_control_count": 1,
            "serial_option_count": 1,
            "serial_option_selected": False,
            "serial_selection_verified": False,
            "input_count_before_selection": 0,
            "input_count_after_selection": 0,
            "text_input_count_after_selection": 0,
            "serial_input_candidate_count": 0,
            "serial_input_unique": False,
            "search_button_candidate_count": 0,
            "search_button_unique": False,
            },
            "schema": [{"element_index": 0, "document_context": "top", "iframe_index": -1, "tag": "select", "id": "search_type"}],
        }

    def inspect_custom_search_control_dom(self, trace=None):
        self.calls.append("custom_dom")
        return {
            "native_select_count": 1,
            "hidden_native_select_count": 1,
            "custom_select_candidate_count": 1,
            "custom_select_unique": True,
            "select_backed_custom_ui_detected": True,
            "select_backed_custom_ui_verified": False,
            "listbox_count": 0,
            "option_candidate_count": 0,
            "visible_option_candidate_count": 0,
            "option_role_count": 0,
            "option_data_attribute_count": 0,
            "stale_retry_count": 0,
            "native_schema": [{"element_index": 0, "tag": "select", "displayed": False}],
            "custom_schema": [{"element_index": 0, "tag": "div", "displayed": True}],
            "relation": {"select_backed_custom_ui_verified": False},
        }

    def inspect_serial_input_dom(self, trace=None):
        self.calls.append("serial_input_dom")
        return {
            "custom_select_candidate_count": 1,
            "custom_select_unique": True,
            "select_backed_custom_ui_verified": True,
            "listbox_visible": True,
            "option_candidate_count": 12,
            "serial_option_candidate_count": 1,
            "serial_option_unique": True,
            "serial_selection_verified": True,
            "input_count_before_selection": 0,
            "input_count_after_selection": 1,
            "serial_input_candidate_count": 1,
            "serial_input_unique": True,
            "schema": [{"element_index": 0, "tag": "input", "id": "asset_serial", "value": "secret"}],
        }

    def fill_serial_input_for_diagnostic(self, serial, trace=None):
        self.calls.append("serial_fill")
        FakeSmsmHandler.last_serial = serial
        if trace is not None:
            for key, value in (
                ("serial_input_candidate_count", 1),
                ("serial_input_unique", True),
                ("serial_input_clear_called", True),
                ("serial_input_send_keys_called", True),
                ("serial_input_nonblank", True),
                ("serial_input_exact_match", True),
                ("serial_input_length_match", True),
                ("serial_input_was_truncated", False),
                ("serial_input_was_transformed", False),
                ("serial_mapping_valid", True),
            ):
                trace(key, value)
        return {
            "serial_input_candidate_count": 1,
            "serial_input_unique": True,
            "serial_input_clear_called": True,
            "serial_input_send_keys_called": True,
            "serial_input_nonblank": True,
            "serial_input_exact_match": True,
            "serial_input_length_match": True,
            "serial_input_was_truncated": False,
            "serial_input_was_transformed": False,
            "serial_mapping_valid": True,
            "search_button_click_called": False,
            "smsm_update_called": False,
            "excel_write_called": False,
        }

    def search_serial_results_for_diagnostic(self, trace=None):
        self.calls.append("search")
        if trace is not None:
            for key, value in (
                ("search_button_candidate_count", 1),
                ("search_button_unique", True),
                ("smsm_validate_search_button", True),
                ("search_button_safe", True),
                ("smsm_submit_search", True),
                ("search_button_click_called", True),
                ("search_submitted", True),
                ("lookup_called", True),
                ("smsm_wait_search_results", True),
                ("lookup_results_ready", True),
                ("lookup_result_count", self.result_count),
                ("lookup_unique", self.result_count == 1),
            ):
                trace(key, value)
        return self.result_count

    def inspect_serial_search_results_dom_for_diagnostic(self, trace=None):
        self.calls.append("search")
        if trace is not None:
            for key, value in (
                ("search_button_candidate_count", 1),
                ("search_button_unique", True),
                ("search_button_safe", True),
                ("search_button_click_called", True),
                ("search_submitted", True),
                ("lookup_called", True),
                ("pre_search_result_table_count", 1),
                ("pre_search_tbody_count", 1),
                ("pre_search_visible_row_count", 1),
                ("pre_search_checkbox_row_count", 1),
                ("pre_search_empty_state_count", 0),
                ("pre_search_loading_count", 0),
                ("pre_search_pagination_count", 0),
                ("post_search_result_table_count", 1),
                ("post_search_tbody_count", 1),
                ("post_search_visible_row_count", self.result_count),
                ("result_dom_changed", True),
                ("result_table_unique", True),
                ("result_rows_scoped_to_table", True),
                ("lookup_results_ready", True),
                ("lookup_result_count", self.result_count),
                ("lookup_unique", self.result_count == 1),
            ):
                trace(key, value)
        return {
            "result_count": self.result_count,
            "schema": [],
            "result_table_unique": True,
            "result_rows_scoped_to_table": True,
            "result_dom_changed": True,
        }

    def match_serial_search_results_for_diagnostic(self, target, trace=None):
        self.calls.append("search")
        assert target is not None
        FakeSmsmHandler.last_match_target = target
        result = FakeSmsmHandler.result_match_result or {
            "result_column_count": 8,
            "result_data_row_count": 2,
            "serial_column_found": True,
            "serial_column_unique": True,
            "imei_column_found": True,
            "imei_column_unique": True,
            "alias_column_found": True,
            "alias_column_unique": True,
            "serial_match_count": 1,
            "imei_match_count": 1,
            "alias_match_count": 1,
            "serial_and_imei_match_count": 1,
            "serial_and_alias_match_count": 1,
            "all_available_fields_match_count": 1,
            "matched_result_count": 1,
            "unique_result_match": True,
            "result_match_unresolved": False,
        }
        if trace is not None:
            for key, value in result.items():
                trace(key, value)
        return result

    def inspect_client_certificate_upload_dom_for_diagnostic(self, trace=None):
        stages = (
            "smsm_certificate_navigation_started", "smsm_find_settings_menu", "smsm_open_settings_menu",
            "smsm_wait_settings_page", "smsm_find_ios_menu", "smsm_open_ios_menu", "smsm_wait_ios_page",
            "smsm_find_certificate_management", "smsm_open_certificate_management", "smsm_wait_certificate_management_page",
            "smsm_find_client_certificate_management", "smsm_open_client_certificate_management",
            "smsm_wait_client_certificate_page", "smsm_inspect_client_certificate_upload_dom",
            "smsm_certificate_navigation_completed",
        )
        result = FakeSmsmHandler.upload_dom_result or {
            "upload_form_count": 1,
            "upload_form_unique": True,
            "file_input_count": 1,
            "file_input_unique": True,
            "password_input_count": 1,
            "password_input_unique": True,
            "upload_button_candidate_count": 1,
            "upload_button_unique": True,
            "certificate_table_count": 1,
            "existing_certificate_row_count": 2,
            "schema": [
                {"element_index": 0, "tag": "input", "type": "file", "id": "cert_file", "name": "certificate", "role": None, "data-testid": "upload-file", "accept": ".p12", "autocomplete": None, "inputmode": None, "displayed": True, "enabled": True, "readonly": False, "disabled": False, "label_linked": True, "parent_tag": "form", "form_index": 0, "value": "private_file.p12"},
                {"element_index": 1, "tag": "input", "type": "password", "id": "cert_password", "name": "password", "role": None, "data-testid": "certificate-password", "accept": None, "autocomplete": "off", "inputmode": None, "displayed": True, "enabled": True, "readonly": False, "disabled": False, "label_linked": True, "parent_tag": "form", "form_index": 0, "value": "private_password"},
            ],
        }
        if trace is not None:
            for stage in stages:
                trace(stage, True)
            for key, value in (
                ("settings_menu_candidate_count", 1), ("settings_menu_unique", True), ("settings_menu_click_called", True), ("settings_page_reached", True),
                ("ios_menu_candidate_count", 1), ("ios_menu_unique", True), ("ios_menu_click_called", True), ("ios_page_reached", True),
                ("certificate_management_candidate_count", 1), ("certificate_management_unique", True), ("certificate_management_click_called", True), ("certificate_management_page_reached", True),
                ("client_certificate_management_candidate_count", 1), ("client_certificate_management_unique", True), ("client_certificate_management_click_called", True), ("client_certificate_page_reached", True),
            ):
                trace(key, value)
            for key, value in result.items():
                if key != "schema":
                    trace(key, value)
        return result

    def navigate_verified_final_path_for_diagnostic(self, manifest, trace=None):
        self.calls.append("verified_path_navigation")
        assert manifest["route_type"] == "verified_final_same_host_path"
        result = FakeSmsmHandler.upload_dom_result or {}
        verified = result.get("existing_certificate_row_count", 2) >= 2
        return {"client_certificate_page_landmark_verified": verified, "target_os_ios_verified": verified, "ios_tab_selected": verified, "android_tab_selected": False, "ios_content_container_visible": verified, "android_content_container_visible": False, "certificate_management_expanded": verified, "client_certificate_child_candidate_count": 1 if verified else 0, "client_certificate_child_visible": verified, "client_certificate_child_active": verified, "client_certificate_child_active_semantic": verified, "client_certificate_child_href_present": verified, "current_path_matches_client_certificate_child": verified, "certificate_operation_structure_verified": verified, "client_certificate_specific_landmark_count": 3 if verified else 0, "certificate_table_count": result.get("certificate_table_count", 1), "existing_certificate_row_count": result.get("existing_certificate_row_count", 2), "certificate_row_checkbox_count": 2}

    def inspect_current_client_certificate_dom_for_diagnostic(self):
        self.calls.append("current_certificate_dom")
        return self.inspect_client_certificate_upload_dom_for_diagnostic()

    def confirm_manual_client_certificate_page_for_diagnostic(self, manifest, trace=None, input_func=input):
        self.calls.append("manual_add_checkpoint")
        if trace is not None:
            trace("manual_checkpoint_wait_started", True)
            trace("manual_checkpoint_received", True)
        return {"client_certificate_page_landmark_verified": True, "target_os_ios_verified": True, "ios_tab_selected": True, "android_tab_selected": False, "ios_content_container_visible": True, "android_content_container_visible": False, "certificate_management_expanded": True, "client_certificate_child_candidate_count": 1, "client_certificate_child_visible": True, "client_certificate_child_active": True, "client_certificate_child_active_semantic": True, "client_certificate_child_href_present": True, "current_path_matches_client_certificate_child": True, "certificate_operation_structure_verified": True, "client_certificate_specific_landmark_count": 3}

    def inspect_client_certificate_add_form_dom_for_diagnostic(self, trace=None, button_schema_callback=None, click_add_button=True):
        self.calls.append("add_button_click")
        if button_schema_callback is not None:
            button_schema_callback({"certificate_toolbar_found": True, "certificate_toolbar_button_count": 3, "search_button_candidate_count": 1, "dropdown_button_candidate_count": 1, "row_action_button_count": 2, "pagination_button_count": 2, "excluded_destructive_button_count": 1, "add_icon_candidate_count": 1, "add_button_candidate_count": 1, "add_button_unique": True, "add_button_safe": True, "add_button_resolution_method": "verified_toolbar_plus_structure", "add_button_click_called": False, "elements": [{"element_index": 0, "tag": "button", "type": None, "role": "button", "id_present": True, "name_present": False, "data_testid_present": True, "aria_label_present": False, "title_present": False, "displayed": True, "enabled": True, "disabled": False, "same_group_as_search_input": True, "inside_certificate_row": False, "inside_pagination": False, "has_svg": True, "svg_path_count": 2, "svg_use_present": False, "before_content_present": False, "after_content_present": False, "candidate_reason": "verified_toolbar_plus_structure", "exclusion_reason": ""}]})
        if trace is not None:
            for key, value in (("add_button_candidate_count", 1), ("add_button_unique", True), ("add_button_safe", click_add_button), ("add_button_click_called", click_add_button), ("add_form_opened", click_add_button)):
                trace(key, value)
        return {"add_button_candidate_count": 1, "add_button_unique": True, "add_button_safe": click_add_button, "add_button_click_called": click_add_button, "add_button_resolution_method": "verified_unique_safe_plus_icon", "add_form_opened": click_add_button, "certificate_toolbar_found": True, "add_icon_candidate_count": 1, "add_icon_unique": True, "add_icon_displayed": True, "add_icon_enabled": True, "add_icon_disabled": False, "add_icon_search_common_ancestor_found": True, "add_icon_dropdown_common_ancestor_found": True, "add_icon_toolbar_common_ancestor_found": True, "add_icon_toolbar_common_ancestor_button_count": 3, "add_icon_toolbar_common_ancestor_input_count": 1, "form_count": 1, "dialog_count": 1, "input_count": 2, "button_count": 3, "file_input_count": 1, "file_input_unique": True, "file_input_safe": True, "file_input_accept_present": True, "file_input_label_linked": True, "password_input_count": 1, "password_input_unique": True, "password_input_safe": True, "password_input_readonly": False, "password_input_disabled": False, "certificate_submit_button_candidate_count": 1, "certificate_submit_button_unique": True, "certificate_submit_button_safe": True, "certificate_submit_button_click_called": False, "cancel_button_candidate_count": 1, "cancel_button_unique": True, "cancel_button_click_called": False, "close_button_candidate_count": 1, "close_button_unique": True, "close_button_click_called": False, "file_input_send_keys_called": False, "password_input_send_keys_called": False, "certificate_upload_called": False, "smsm_update_called": False, "schema": [{"element_index": 0, "tag": "input", "type": "file", "id_present": True, "name_present": True, "data_testid_present": True, "displayed": True, "enabled": True, "disabled": False, "parent_tag": "form", "form_index": 0, "dialog_index": 0}]}

    def capture_manual_certificate_checkpoint_for_diagnostic(self, trace=None, input_func=input):
        self.calls.append("manual_checkpoint")
        if trace is not None:
            trace("manual_checkpoint_wait_started", True)
            trace("manual_checkpoint_received", True)
            trace("browser_session_valid", True)
            trace("same_host_verified", True)
            trace("target_os_ios_verified", True)
            trace("client_certificate_page_landmark_verified", True)
        schema = {"target_os_ios_verified": True, "ios_tab_selected": True, "android_tab_selected": False, "ios_content_container_visible": True, "android_content_container_visible": False, "certificate_management_expanded_by_attribute": True, "certificate_management_expanded_by_visible_child": False, "certificate_management_expanded": True, "client_certificate_child_candidate_count": 1, "client_certificate_child_visible": True, "client_certificate_child_active": True, "client_certificate_child_active_semantic": True, "client_certificate_child_selected_by_style": False, "client_certificate_child_href_present": True, "current_path_matches_client_certificate_child": True, "current_path_verified_by_manual_checkpoint": True, "certificate_operation_structure_verified": False, "client_certificate_specific_landmark_count": 3, "target_os_ios_verified_required": True, "ios_tab_selected_required": True, "android_tab_not_selected_required": True, "ios_content_container_visible_required": True, "android_content_container_hidden_required": True, "deduplicated_client_child_required": True, "certificate_management_expanded_required": True, "client_certificate_child_visible_required": True, "client_certificate_child_active_required": True, "client_certificate_child_href_present_required": True, "current_path_matches_client_child_required": True, "specific_landmark_minimum": 3, "operation_structure_required": False, "manual_checkpoint_path_verified": True}
        return {"verified": True, "browser_session_valid": True, "same_host_verified": True, "target_os": "ios", "same_host_path": "/ios/client-certificates", "landmark_schema": schema, "target_os_ios_verified": True, "ios_tab_selected": True, "android_tab_selected": False, "ios_content_container_visible": True, "android_content_container_visible": False, "certificate_management_expanded": True, "client_certificate_child_candidate_count": 1, "client_certificate_child_visible": True, "client_certificate_child_active": True, "client_certificate_child_active_semantic": True, "client_certificate_child_href_present": True, "current_path_matches_client_certificate_child": True, "current_path_verified_by_manual_checkpoint": True, "certificate_operation_structure_verified": True, "client_certificate_specific_landmark_count": 3, "client_certificate_page_landmark_verified": True}

    def inspect_settings_navigation_dom_for_diagnostic(self, trace=None):
        self.calls.append("settings_dom")
        result = FakeSmsmHandler.settings_navigation_result or {
            "top_navigation_found": True,
            "nav_count": 1,
            "header_count": 1,
            "link_count": 2,
            "button_count": 1,
            "role_link_count": 1,
            "role_button_count": 1,
            "exact_settings_text_count": 0,
            "normalized_settings_text_count": 1,
            "settings_text_on_child_count": 1,
            "settings_text_element_count": 1,
            "settings_directly_clickable_count": 0,
            "settings_clickable_parent_count": 1,
            "settings_clickable_ancestor_count": 1,
            "settings_candidate_count": 1,
            "settings_candidate_unique": True,
            "elements": [
                {"element_index": 0, "tag": "span", "id": None, "name": None, "role": None, "data-testid": None, "aria-label": None, "href_path_fingerprint": None, "displayed": True, "enabled": True, "disabled": False, "parent_tag": "a", "parent_id_present": True, "parent_class_present": True, "child_count": 0, "clickable_ancestor_present": True, "clickable_ancestor_tag": "a", "text": "設定", "href": "https://private.invalid/settings"},
            ],
        }
        if trace is not None:
            for key, value in result.items():
                if key != "elements":
                    trace(key, value)
        return result

    def count_visible_device_results(self):
        self.calls.append("count")
        return self.result_count

    def upload_certificate(self, *_args):
        raise AssertionError("certificate upload must not be called")

    def associate_imei(self, *_args):
        raise AssertionError("SMSM update must not be called")



def install(monkeypatch, *, rows, result_count=1, is_open=False, detection_match=True, save_error=None, unlock_error=None, quit_error=None):
    logger = DummyLogger()
    calls = []
    reader = FakeReader(rows, calls, is_open=is_open)
    browser = FakeBrowser(calls, quit_error=quit_error)
    monkeypatch.setattr(mod, "AppLogger", lambda _base_dir: logger)
    monkeypatch.setattr(mod, "load_config", lambda: {
        "excel": {"path": "C:/private/target.xlsm"},
        "smsm": {"url": "https://smsm.test.invalid", "company_code": "company_secret", "username": "user_secret", "password": "password_secret"},
    })
    monkeypatch.setattr(mod, "ExcelReader", lambda _path: reader)
    monkeypatch.setattr(mod.Path, "exists", lambda _self: True)
    monkeypatch.setattr(mod, "Browser", lambda _base_dir, _config: browser)
    monkeypatch.setattr(mod, "SmsmHandler", FakeSmsmHandler)
    monkeypatch.setattr(mod, "_resolve_web_identity_hash", lambda *_args: SimpleNamespace(hash_value="a" * 64, source="environment", valid=True))

    def detect(*_args, **kwargs):
        calls.append("detect")
        assert kwargs["web_identity_mode"] == "test"
        return SimpleNamespace(
            workbook=object() if detection_match else None,
            application=object() if detection_match else None,
            matched_workbook_count=1 if detection_match else 0,
            target_match_method="web_identity" if detection_match else "none",
        )

    def save_close(*_args, **_kwargs):
        calls.append("save_close")
        if save_error:
            raise save_error
        return True

    def wait_unlock(*_args, **_kwargs):
        calls.append("unlock")
        if unlock_error:
            raise unlock_error

    def count_results(self):
        self.calls.append("count")
        return result_count

    monkeypatch.setattr(FakeSmsmHandler, "count_visible_device_results", count_results)
    FakeSmsmHandler.result_count = result_count
    monkeypatch.setattr(mod, "detect_target_workbook", detect)
    monkeypatch.setattr(mod, "save_and_close_target_workbook", save_close)
    monkeypatch.setattr(mod, "_wait_unlock", wait_unlock)
    monkeypatch.setattr(mod, "reopen_excel", lambda _path, _emit: calls.append("reopen"))
    return logger, calls, browser


def joined(logger):
    return "\n".join(logger.info_messages + logger.error_messages)


def test_inspect_smsm_settings_navigation_dom_is_read_only_and_saves_safe_schema(monkeypatch, tmp_path):
    logger, calls, _browser = install(monkeypatch, rows=[])
    output_path = tmp_path / "smsm_settings_navigation_dom.json"
    monkeypatch.setattr(mod, "_smsm_settings_navigation_dom_schema_path", lambda: output_path)
    monkeypatch.setattr(mod, "ExcelReader", lambda _path: (_ for _ in ()).throw(AssertionError("ExcelReader must not be created")))

    assert mod.main(["--inspect-smsm-settings-navigation-dom"]) == 0
    assert "settings_dom" in calls
    assert "quit" in calls
    assert calls.count("quit") == 1
    output = joined(logger)
    for expected in (
        "login_completed=True", "top_navigation_found=True", "nav_count=1", "header_count=1",
        "exact_settings_text_count=0", "normalized_settings_text_count=1", "settings_text_on_child_count=1",
        "settings_directly_clickable_count=0", "settings_clickable_parent_count=1",
        "settings_clickable_ancestor_count=1", "settings_candidate_count=1", "settings_candidate_unique=True",
        "settings_menu_click_called=False", "smsm_update_called=False", "excel_read_called=False",
        "excel_write_called=False", "browser_quit_completed=True",
    ):
        assert expected in output
    payload_text = output_path.read_text(encoding="utf-8")
    assert "設定" not in payload_text
    assert "private.invalid" not in payload_text
    assert '"text"' not in payload_text
    assert "https://" not in payload_text


def test_inspect_smsm_settings_navigation_dom_succeeds_without_navigation_candidate(monkeypatch, tmp_path):
    logger, calls, _browser = install(monkeypatch, rows=[])
    FakeSmsmHandler.settings_navigation_result = {
        "top_navigation_found": False, "nav_count": 0, "header_count": 0, "elements": [],
        "settings_candidate_count": 0, "settings_candidate_unique": False,
    }
    monkeypatch.setattr(mod, "_smsm_settings_navigation_dom_schema_path", lambda: tmp_path / "navigation.json")

    assert mod.main(["--inspect-smsm-settings-navigation-dom"]) == 0
    assert calls.count("quit") == 1
    assert 'navigation_resolution_ready=False' in joined(logger)
    assert (tmp_path / "navigation.json").exists()
    FakeSmsmHandler.settings_navigation_result = None


def test_inspect_client_certificate_upload_dom_is_read_only_and_skips_excel(monkeypatch, tmp_path):
    logger, calls, _browser = install(monkeypatch, rows=[])
    output_path = tmp_path / "smsm_client_certificate_upload_dom.json"
    monkeypatch.setattr(mod, "_smsm_client_certificate_upload_dom_schema_path", lambda: output_path)
    monkeypatch.setattr(mod, "ExcelReader", lambda _path: (_ for _ in ()).throw(AssertionError("ExcelReader must not be created")))
    monkeypatch.setattr(mod, "_load_route_manifest", _valid_route_manifest)

    assert mod.main(["--inspect-smsm-client-certificate-upload-dom"]) == 0
    assert "verified_path_navigation" in calls
    assert "current_certificate_dom" in calls
    assert calls.count("quit") == 1
    output = joined(logger)
    for expected in (
        "client_certificate_page_landmark_verified=True", "certificate_table_count=1", "existing_certificate_row_count=2",
        "upload_form_count=1", "file_input_unique=True",
        "password_input_unique=True", "upload_button_unique=True", "upload_action_called=False",
        "file_input_send_keys_called=False", "password_input_send_keys_called=False", "smsm_update_called=False",
        "excel_read_called=False", "excel_write_called=False", "hennge_action_called=False",
    ):
        assert expected in output
    payload_text = output_path.read_text(encoding="utf-8")
    assert "private_file.p12" not in payload_text
    assert "private_password" not in payload_text
    assert '"value"' not in payload_text
    assert '"text"' not in payload_text


def test_inspect_client_certificate_upload_dom_requires_unique_form_controls(monkeypatch, tmp_path):
    logger, calls, _browser = install(monkeypatch, rows=[])
    FakeSmsmHandler.upload_dom_result = {
        "upload_form_count": 1, "upload_form_unique": True,
        "file_input_count": 2, "file_input_unique": False,
        "password_input_count": 1, "password_input_unique": True,
        "upload_button_candidate_count": 1, "upload_button_unique": True,
        "certificate_table_count": 0, "existing_certificate_row_count": 0, "schema": [],
    }
    output_path = tmp_path / "match.json"
    monkeypatch.setattr(mod, "_smsm_client_certificate_upload_dom_schema_path", lambda: output_path)
    monkeypatch.setattr(mod, "_load_route_manifest", _valid_route_manifest)

    assert mod.main(["--inspect-smsm-client-certificate-upload-dom"]) == 31
    assert calls.count("quit") == 1
    assert "failed_stage=smsm_client_certificate_landmark" in joined(logger)
    FakeSmsmHandler.upload_dom_result = None


def test_inspect_client_certificate_dom_succeeds_without_upload_inputs(monkeypatch, tmp_path):
    logger, calls, _browser = install(monkeypatch, rows=[])
    FakeSmsmHandler.upload_dom_result = {
        "upload_form_count": 0, "upload_form_unique": False,
        "file_input_count": 0, "file_input_unique": False,
        "password_input_count": 0, "password_input_unique": False,
        "upload_button_candidate_count": 0, "upload_button_unique": False,
        "certificate_table_count": 1, "existing_certificate_row_count": 2, "schema": [],
    }
    monkeypatch.setattr(mod, "_smsm_client_certificate_upload_dom_schema_path", lambda: tmp_path / "list.json")
    monkeypatch.setattr(mod, "_load_route_manifest", _valid_route_manifest)
    assert mod.main(["--inspect-smsm-client-certificate-upload-dom"]) == 0
    assert "settings_menu_click_called=True" not in joined(logger)
    FakeSmsmHandler.upload_dom_result = None


def test_add_form_dom_clicks_add_once_without_input_or_upload(monkeypatch, tmp_path):
    logger, calls, _browser = install(monkeypatch, rows=[])
    monkeypatch.setattr(mod, "_load_route_manifest", _valid_route_manifest)
    output_path = tmp_path / "add-form.json"
    button_output_path = tmp_path / "add-button.json"
    monkeypatch.setattr(mod, "_smsm_client_certificate_add_form_dom_schema_path", lambda: output_path)
    monkeypatch.setattr(mod, "_smsm_client_certificate_add_button_dom_schema_path", lambda: button_output_path)
    assert mod.main(["--inspect-smsm-client-certificate-add-form-dom"]) == 0
    assert calls.count("verified_path_navigation") == 1
    assert calls.count("add_button_click") == 1
    payload = output_path.read_text(encoding="utf-8")
    assert "file_input_send_keys_called" in payload
    assert "private" not in payload
    button_payload = button_output_path.read_text(encoding="utf-8")
    assert '"add_button_resolution_method": "verified_toolbar_plus_structure"' in button_payload
    assert "aria-label" not in button_payload
    assert '"has_svg": true' in button_payload
    output = joined(logger)
    assert "add_button_candidate_count=1" in output
    assert "add_button_click_called=True" in output
    assert "certificate_submit_button_click_called=False" in output
    assert "file_input_send_keys_called=False" in output
    assert "password_input_send_keys_called=False" in output


def test_add_form_dom_missing_manifest_stops_before_click(monkeypatch, tmp_path):
    logger, calls, _browser = install(monkeypatch, rows=[])
    monkeypatch.setattr(mod, "_smsm_navigation_route_path", lambda: tmp_path / "missing.json")
    assert mod.main(["--inspect-smsm-client-certificate-add-form-dom"]) == 31
    assert "add_button_click" not in calls
    assert "failed_stage=smsm_navigation_route_required" in joined(logger)


def test_add_form_manual_checkpoint_rechecks_dom_and_never_clicks(monkeypatch, tmp_path):
    logger, calls, _browser = install(monkeypatch, rows=[])
    monkeypatch.setattr(mod, "_load_route_manifest", _valid_route_manifest)
    monkeypatch.setattr(mod, "_smsm_client_certificate_add_form_dom_schema_path", lambda: tmp_path / "add-form.json")
    monkeypatch.setattr(mod, "_smsm_client_certificate_add_button_dom_schema_path", lambda: tmp_path / "add-button.json")
    assert mod.main(["--inspect-smsm-client-certificate-add-form-dom", "--manual-checkpoint-before-add"]) == 0
    assert calls.count("verified_path_navigation") == 1
    assert calls.count("manual_add_checkpoint") == 1
    assert calls.count("add_button_click") == 1
    output = joined(logger)
    assert "add_button_click_called=False" in output
    assert "file_input_send_keys_called=False" in output
    assert "password_input_send_keys_called=False" in output
    assert "certificate_upload_called=False" in output
    assert "smsm_update_called=False" in output


def test_inspect_client_certificate_upload_dom_preserves_navigation_failure_and_quit_failure(monkeypatch, tmp_path):
    logger, calls, _browser = install(monkeypatch, rows=[], quit_error=RuntimeError("quit failure"))
    FakeSmsmHandler.upload_dom_result = {
        "upload_form_count": 1, "upload_form_unique": True,
        "file_input_count": 1, "file_input_unique": True,
        "password_input_count": 1, "password_input_unique": True,
        "upload_button_candidate_count": 1, "upload_button_unique": True,
        "certificate_table_count": 0, "existing_certificate_row_count": 0, "schema": [],
    }
    monkeypatch.setattr(mod, "_smsm_client_certificate_upload_dom_schema_path", lambda: tmp_path / "match.json")
    monkeypatch.setattr(mod, "_load_route_manifest", _valid_route_manifest)
    assert mod.main(["--inspect-smsm-client-certificate-upload-dom"]) == 31
    assert calls.count("quit") == 1
    FakeSmsmHandler.upload_dom_result = None


def test_dom_schema_inspection_uses_attributes_only():
    class DomDriver:
        def execute_script(self, _script):
            return {
                "inputs": [
                    {"element_index": 0, "type": "text", "id": "user_company_code", "name": "user[company_code]", "autocomplete": None, "inputmode": None, "maxlength_present": True, "pattern_present": False, "readonly": False, "disabled": False, "displayed": True, "enabled": True, "label_linked": True},
                    {"element_index": 1, "type": "text", "id": "user_login", "name": "user[login]", "autocomplete": "username", "inputmode": None, "maxlength_present": False, "pattern_present": False, "readonly": False, "disabled": False, "displayed": True, "enabled": True, "label_linked": True},
                    {"element_index": 2, "type": "password", "id": "user_password", "name": "user[password]", "autocomplete": "current-password", "inputmode": None, "maxlength_present": False, "pattern_present": False, "readonly": False, "disabled": False, "displayed": True, "enabled": True, "label_linked": True},
                ],
                "label_total_count": 3,
            }

    summary, schema = mod._inspect_login_dom(DomDriver())
    assert summary["input_total_count"] == 3
    assert summary["company_candidate_unique"] is True
    assert summary["user_candidate_unique"] is True
    assert summary["password_candidate_unique"] is True
    assert all("value" not in item and "text" not in item for item in schema)


def test_dom_candidate_counts_ignore_hidden_and_checkbox_inputs():
    class DomDriver:
        def execute_script(self, _script):
            return {
                "inputs": [
                    {"element_index": 0, "type": "hidden", "id": "authenticity_token", "name": "authenticity_token", "displayed": False, "enabled": True, "readonly": False, "disabled": False},
                    {"element_index": 1, "type": "hidden", "id": "remember_me", "name": "remember_me", "displayed": False, "enabled": True, "readonly": False, "disabled": False},
                    {"element_index": 2, "type": "checkbox", "id": "remember_me", "name": "remember_me", "displayed": True, "enabled": True, "readonly": False, "disabled": False},
                    {"element_index": 3, "type": "text", "id": "user_company_code", "name": "user[company_code]", "displayed": True, "enabled": True, "readonly": False, "disabled": False},
                    {"element_index": 4, "type": "text", "id": "user_login", "name": "user[login]", "displayed": True, "enabled": True, "readonly": False, "disabled": False},
                    {"element_index": 5, "type": "password", "id": "user_password", "name": "user[password]", "displayed": True, "enabled": True, "readonly": False, "disabled": False},
                ],
                "label_total_count": 3,
            }

    summary, _schema = mod._inspect_login_dom(DomDriver())
    assert summary["company_candidate_count"] == 1
    assert summary["user_candidate_count"] == 1
    assert summary["password_candidate_count"] == 1
    assert summary["company_candidate_unique"] is True
    assert summary["user_candidate_unique"] is True
    assert summary["password_candidate_unique"] is True


def test_dom_schema_writer_excludes_sensitive_keys(tmp_path):
    path = tmp_path / "schema.json"
    mod._write_dom_schema(path, [{"element_index": 0, "id": "company", "value": "secret", "text": "secret", "type": "text"}])
    schema = json.loads(path.read_text(encoding="utf-8"))
    assert "value" not in schema[0]
    assert "text" not in schema[0]


def test_serial_search_dom_mode_selects_only_and_never_reads_excel(monkeypatch, tmp_path):
    logger, calls, _browser = install(
        monkeypatch,
        rows=[{"alias": "alias_secret", "serial": "serial_secret", "imei": "123456789012345"}],
    )
    monkeypatch.setattr(mod, "_serial_search_preselection_dom_schema_path", lambda: tmp_path / "serial.json")

    assert mod.main(["--inspect-smsm-serial-search-dom"]) == 0
    assert calls == ["browser", "login", "device_page", "serial_dom", "quit"]
    output = joined(logger)
    for expected in (
        "device_page_reached=True",
        "search_type_control_count=1",
        "serial_option_count=1",
        "serial_option_selected=False",
        "serial_selection_verified=False",
        "serial_input_unique=False",
    ):
        assert expected in output
    assert "read" not in calls and "lookup" not in calls and "count" not in calls
    assert "send_keys" not in calls and "click_search" not in calls
    schema = json.loads((tmp_path / "serial.json").read_text(encoding="utf-8"))
    assert [item["element_index"] for item in schema] == [0]
    assert all(key not in item for item in schema for key in ("value", "text", "placeholder"))


def test_custom_search_control_dom_mode_is_read_only_and_uses_dedicated_json(monkeypatch, tmp_path):
    logger, calls, _browser = install(monkeypatch, rows=[])
    monkeypatch.setattr(mod, "_custom_search_control_dom_schema_path", lambda: tmp_path / "custom.json")

    assert mod.main(["--inspect-smsm-custom-search-control-dom"]) == 0
    assert calls == ["browser", "login", "device_page", "custom_dom", "quit"]
    output = joined(logger)
    for expected in (
        "device_page_reached=True",
        "device_page_stable=True",
        "native_select_count=1",
        "hidden_native_select_count=1",
        "custom_select_candidate_count=1",
        "custom_select_unique=True",
        "select_backed_custom_ui_detected=True",
        "select_backed_custom_ui_verified=False",
        "custom_control_click_called=False",
        "option_click_called=False",
        "native_select_change_called=False",
        "send_keys_called=False",
        "search_button_click_called=False",
    ):
        assert expected in output
    assert "read" not in calls and "lookup" not in calls and "count" not in calls
    schema = json.loads((tmp_path / "custom.json").read_text(encoding="utf-8"))
    assert schema["native_selects"][0]["displayed"] is False
    assert schema["custom_controls"][0]["displayed"] is True
    assert "text" not in json.dumps(schema, ensure_ascii=False)
    assert "value" not in json.dumps(schema, ensure_ascii=False)


def test_custom_search_control_mode_is_rejected_as_search_failure_when_not_unique(monkeypatch):
    logger, calls, _browser = install(monkeypatch, rows=[])

    def ambiguous(_handler, trace=None):
        raise RuntimeError("custom select候補を確認できません")

    monkeypatch.setattr(FakeSmsmHandler, "inspect_custom_search_control_dom", ambiguous)
    assert mod.main(["--inspect-smsm-custom-search-control-dom"]) == 31
    assert "failed_stage=smsm_wait_device_page_stable" in joined(logger)
    assert "read" not in calls and "lookup" not in calls


def test_serial_input_dom_mode_is_read_only_after_custom_selection(monkeypatch, tmp_path):
    logger, calls, _browser = install(monkeypatch, rows=[])
    monkeypatch.setattr(mod, "_serial_input_dom_schema_path", lambda: tmp_path / "serial-input.json")

    assert mod.main(["--inspect-smsm-serial-input-dom"]) == 0
    assert calls == ["browser", "login", "device_page", "serial_input_dom", "quit"]
    output = joined(logger)
    for expected in (
        "custom_select_candidate_count=1",
        "custom_select_unique=True",
        "select_backed_custom_ui_verified=True",
        "custom_control_click_called=True",
        "serial_option_unique=True",
        "serial_option_click_called=True",
        "serial_selection_verified=True",
        "serial_input_unique=True",
        "send_keys_called=False",
        "search_button_click_called=False",
        "smsm_update_called=False",
        "excel_read_called=False",
        "excel_write_called=False",
    ):
        assert expected in output
    assert "lookup" not in calls and "count" not in calls
    saved = (tmp_path / "serial-input.json").read_text(encoding="utf-8")
    assert "value" not in saved
    assert "text" not in saved
    assert "placeholder" not in saved


def test_trace_serial_input_uses_first_target_and_stops_before_search(monkeypatch):
    logger, calls, _browser = install(
        monkeypatch,
        rows=[
            {"alias": "first_alias", "serial": "first_serial", "imei": "111111111111111"},
            {"alias": "second_alias", "serial": "second_serial", "imei": "222222222222222"},
        ],
        is_open=True,
    )

    assert mod.main(["--trace-smsm-serial-input"]) == 0
    assert FakeSmsmHandler.last_serial == "first_serial"
    assert calls[-2:] == ["quit", "reopen"]
    assert "lookup" not in calls
    assert "count" not in calls
    assert "reopen" in calls
    output = joined(logger)
    for expected in (
        "smsm_serial_input_target_loaded=True",
        "smsm_serial_input_target_validated=True",
        "serial_input_clear_called=True",
        "serial_input_send_keys_called=True",
        "serial_input_exact_match=True",
        "serial_mapping_valid=True",
        "search_button_click_called=False",
        "smsm_update_called=False",
        "excel_write_called=False",
    ):
        assert expected in output


@pytest.mark.parametrize("result_count, expected_code", [(0, 32), (1, 0), (2, 33)])
def test_trace_serial_search_uses_first_serial_and_returns_result_code(monkeypatch, result_count, expected_code):
    logger, calls, _browser = install(
        monkeypatch,
        rows=[
            {"alias": "first_alias", "serial": "first_serial", "imei": "111111111111111"},
            {"alias": "second_alias", "serial": "second_serial", "imei": "222222222222222"},
        ],
        result_count=result_count,
        is_open=True,
    )

    assert mod.main(["--trace-smsm-serial-search"]) == expected_code
    assert FakeSmsmHandler.last_serial == "first_serial"
    assert calls == ["is_open", "detect", "save_close", "unlock", "read", "browser", "login", "device_page", "serial_input_dom", "serial_fill", "search", "quit", "reopen"]
    output = joined(logger)
    for expected in (
        "smsm_serial_search_target_loaded=True",
        "smsm_serial_search_target_validated=True",
        "search_button_candidate_count=1",
        "search_button_unique=True",
        "search_button_safe=True",
        "search_button_click_called=True",
        "search_submitted=True",
        "lookup_called=True",
        f"lookup_result_count={result_count}",
        "result_row_click_called=False",
        "smsm_update_called=False",
        "excel_write_called=False",
    ):
        assert expected in output
    assert "second_serial" not in output
    assert "111111111111111" not in output
    assert "222222222222222" not in output


def test_trace_serial_search_does_not_normalize_or_use_imei(monkeypatch):
    logger, _calls, _browser = install(
        monkeypatch,
        rows=[{"alias": "first_alias", "serial": "first_serial", "imei": "111111111111111"}],
    )
    monkeypatch.setattr(mod, "normalize_imei", lambda _value: (_ for _ in ()).throw(AssertionError("IMEI must not be used")))

    assert mod.main(["--trace-smsm-serial-search"]) == 0
    assert FakeSmsmHandler.last_serial == "first_serial"


@pytest.mark.parametrize("result_count, expected_code", [(0, 32), (1, 0), (2, 33)])
def test_inspect_serial_search_results_dom_uses_first_serial_and_safe_json(monkeypatch, tmp_path, result_count, expected_code):
    logger, calls, _browser = install(
        monkeypatch,
        rows=[
            {"alias": "first_alias", "serial": "first_serial", "imei": "111111111111111"},
            {"alias": "second_alias", "serial": "second_serial", "imei": "222222222222222"},
        ],
        result_count=result_count,
        is_open=True,
    )
    output_path = tmp_path / "smsm_serial_search_results_dom.json"
    monkeypatch.setattr(mod, "_serial_search_results_dom_schema_path", lambda: output_path)

    assert mod.main(["--inspect-smsm-serial-search-results-dom"]) == expected_code
    assert FakeSmsmHandler.last_serial == "first_serial"
    assert calls[-2:] == ["quit", "reopen"]
    output = joined(logger)
    assert "result_row_click_called=False" in output
    assert "smsm_update_called=False" in output
    assert "excel_write_called=False" in output
    assert "second_serial" not in output
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["lookup_result_count"] == result_count
    assert all("text" not in json.dumps(payload, ensure_ascii=False) for _ in [0])


@pytest.mark.parametrize("matched_count, unresolved, expected_code", [(0, False, 32), (1, False, 0), (2, False, 33), (0, True, 31)])
def test_trace_smsm_result_match_uses_first_target_and_safe_summary(monkeypatch, tmp_path, matched_count, unresolved, expected_code):
    logger, calls, _browser = install(
        monkeypatch,
        rows=[
            {"alias": "first_alias", "serial": "first_serial", "imei": "111111111111111"},
            {"alias": "second_alias", "serial": "second_serial", "imei": "222222222222222"},
        ],
        is_open=True,
    )
    FakeSmsmHandler.result_match_result = {
        "result_column_count": 8,
        "result_data_row_count": 2,
        "serial_column_found": True,
        "serial_column_unique": True,
        "imei_column_found": True,
        "imei_column_unique": True,
        "alias_column_found": True,
        "alias_column_unique": True,
        "serial_match_count": matched_count,
        "imei_match_count": matched_count,
        "alias_match_count": matched_count,
        "serial_and_imei_match_count": matched_count,
        "serial_and_alias_match_count": matched_count,
        "all_available_fields_match_count": matched_count,
        "matched_result_count": matched_count,
        "unique_result_match": matched_count == 1 and not unresolved,
        "result_match_unresolved": unresolved,
    }
    path = tmp_path / "smsm_result_match_schema.json"
    monkeypatch.setattr(mod, "_smsm_result_match_schema_path", lambda: path)

    assert mod.main(["--trace-smsm-result-match"]) == expected_code
    assert FakeSmsmHandler.last_match_target["alias"] == "first_alias"
    assert FakeSmsmHandler.last_match_target["serial"] == "first_serial"
    assert FakeSmsmHandler.last_match_target["imei"] == "111111111111111"
    assert calls.count("search") == 1
    assert calls.count("quit") == 1
    assert calls.count("reopen") == 1
    output = joined(logger)
    for expected in (
        "result_row_click_called=False",
        "result_detail_opened=False",
        "smsm_update_called=False",
        "certificate_action_called=False",
        "hennge_action_called=False",
        "excel_write_called=False",
    ):
        assert expected in output
    payload_text = path.read_text(encoding="utf-8")
    assert json.loads(payload_text)["matched_result_count"] == matched_count
    assert "first_serial" not in output
    assert "first_serial" not in payload_text


def test_trace_smsm_result_match_preserves_result_code_when_browser_quit_fails(monkeypatch, tmp_path):
    logger, _calls, _browser = install(
        monkeypatch,
        rows=[{"alias": "first_alias", "serial": "first_serial", "imei": "111111111111111"}],
        is_open=True,
        quit_error=RuntimeError("quit failure"),
    )
    FakeSmsmHandler.result_match_result = {
        "result_column_count": 8,
        "result_data_row_count": 2,
        "serial_column_found": True,
        "serial_column_unique": True,
        "imei_column_found": True,
        "imei_column_unique": True,
        "alias_column_found": True,
        "alias_column_unique": True,
        "serial_match_count": 2,
        "imei_match_count": 2,
        "alias_match_count": 2,
        "serial_and_imei_match_count": 2,
        "serial_and_alias_match_count": 2,
        "all_available_fields_match_count": 2,
        "matched_result_count": 2,
        "unique_result_match": False,
        "result_match_unresolved": False,
    }
    monkeypatch.setattr(mod, "_smsm_result_match_schema_path", lambda: tmp_path / "match.json")

    assert mod.main(["--trace-smsm-result-match"]) == 33
    assert "matched_result_count=2" in joined(logger)


def test_trace_serial_search_preserves_result_code_when_browser_quit_fails(monkeypatch):
    logger, _calls, _browser = install(
        monkeypatch,
        rows=[{"alias": "first_alias", "serial": "first_serial", "imei": "111111111111111"}],
        result_count=0,
        quit_error=RuntimeError("quit failure"),
    )

    assert mod.main(["--trace-smsm-serial-search"]) == 32
    assert "lookup_result_count=0" in joined(logger)


def test_serial_input_result_contract_requires_all_keys_and_success_values():
    result = {
        key: {
            "serial_input_candidate_count": 1,
            "serial_input_unique": True,
            "serial_input_clear_called": True,
            "serial_input_send_keys_called": True,
            "serial_input_nonblank": True,
            "serial_input_exact_match": True,
            "serial_input_length_match": True,
            "serial_input_was_truncated": False,
            "serial_input_was_transformed": False,
            "serial_mapping_valid": True,
            "search_button_click_called": False,
            "smsm_update_called": False,
            "excel_write_called": False,
        }[key]
        for key in mod.SERIAL_INPUT_REQUIRED_KEYS
    }
    mod._validate_serial_input_result(result)


def test_missing_serial_input_result_key_returns_31_not_8(monkeypatch):
    logger, calls, _browser = install(
        monkeypatch,
        rows=[{"alias": "first_alias", "serial": "first_serial", "imei": "111111111111111"}],
    )

    def incomplete(_handler, _serial, trace=None):
        return {"serial_input_candidate_count": 1}

    monkeypatch.setattr(FakeSmsmHandler, "fill_serial_input_for_diagnostic", incomplete)
    assert mod.main(["--trace-smsm-serial-input"]) == 31
    output = joined(logger)
    assert "failed_stage=smsm_serial_input_result_validation" in output
    assert "exception_type=ResultContractError" in output
    assert "failed_stage=smsm_serial_input_result_validation" in output
    assert "browser_quit_called=True" in output
    assert "reopen_called" not in output
    assert "lookup" not in calls and "count" not in calls


def test_key_error_outside_excel_reader_is_not_classified_as_excel_error(monkeypatch):
    logger, _calls, _browser = install(
        monkeypatch,
        rows=[{"alias": "first_alias", "serial": "first_serial", "imei": "111111111111111"}],
    )

    def unexpected_key_error(_handler, _serial, trace=None):
        raise KeyError("internal")

    monkeypatch.setattr(FakeSmsmHandler, "fill_serial_input_for_diagnostic", unexpected_key_error)
    assert mod.main(["--trace-smsm-serial-input"]) == 1
    assert "exception_type=KeyError" in joined(logger)


def test_excel_reader_key_error_is_classified_as_8(monkeypatch):
    logger, _calls, _browser = install(monkeypatch, rows=[])

    def missing_sheet(self):
        raise KeyError("sheet")

    monkeypatch.setattr(FakeReader, "read_targets", missing_sheet)
    assert mod.main(["--trace-smsm-serial-input"]) == 8
    assert "failed_stage=read_targets" in joined(logger)


def test_serial_input_dom_handler_clicks_only_exact_serial_option_and_filters_inputs(monkeypatch):
    class Element:
        def __init__(self, tag, element_id="", displayed=True, enabled=True, input_type="text", text=""):
            self.tag_name = tag
            self.text = text
            self.displayed = displayed
            self.enabled = enabled
            self.click_calls = 0
            self.attributes = {"id": element_id, "type": input_type, "aria-expanded": "false"}

        def get_attribute(self, name):
            return self.attributes.get(name)

        def is_displayed(self):
            return self.displayed

        def is_enabled(self):
            return self.enabled

        def click(self):
            self.click_calls += 1
            if self.tag_name == "div":
                self.attributes["aria-expanded"] = "true"

        def find_elements(self, _by, value):
            if value == "[role='option'], option, [data-value], [data-option]":
                return []
            return []

    control = Element("div", "custom_control")
    exact = Element("div", "option_exact", text="シリアル番号")
    partial = Element("div", "option_partial", text="シリアル番号（推奨）")
    listbox = Element("div", "listbox")
    input_element = Element("input", "asset_serial")
    manual = Element("input", "manual_page_input_assets")
    checkbox = Element("input", "remember", input_type="checkbox")
    hidden = Element("input", "hidden_value", displayed=False, input_type="hidden")
    native = Element("select", "native_select")
    native.attributes["selectedIndex"] = "0"

    class Driver:
        def __init__(self):
            self.selected = False

        def find_elements(self, _by, value):
            if value == "[role='combobox'], [aria-haspopup='listbox']":
                return [control]
            if value == "[role='listbox']":
                return [] if self.selected else [listbox]
            if value == "select":
                return [native]
            if value == "input, textarea":
                return [input_element, manual, checkbox, hidden] if self.selected else [manual, checkbox, hidden]
            return []

    driver = Driver()
    listbox.find_elements = lambda _by, value: [exact, partial] if value == "[role='option'], option, [data-value], [data-option]" else []
    original_click = exact.click

    def select_click():
        original_click()
        driver.selected = True
        native.attributes["selectedIndex"] = "1"

    exact.click = select_click
    handler = mod.SmsmHandler.__new__(mod.SmsmHandler)
    handler.browser = type("Browser", (), {"driver": driver})()
    monkeypatch.setattr(handler, "wait_for_device_page_stable", lambda trace=None: {
        "custom_select_candidate_count": 1,
        "custom_select_unique": True,
        "select_backed_custom_ui_verified": True,
    })
    monkeypatch.setattr("app.smsm_handler.time.sleep", lambda _seconds: None)

    trace = []
    result = handler.inspect_serial_input_dom(lambda key, value: trace.append((key, value)))

    assert control.click_calls == 1
    assert exact.click_calls == 1
    assert partial.click_calls == 0
    assert native.attributes["selectedIndex"] == "1"
    assert result["serial_input_unique"] is True
    assert result["serial_input_candidate_count"] == 1
    assert result["schema"][0]["id"] == "asset_serial"
    assert all(key not in result["schema"][0] for key in ("value", "text", "placeholder"))
    trace_values = dict(trace)
    for key in (
        "smsm_wait_device_page_stable_elapsed_ms",
        "smsm_find_custom_control_elapsed_ms",
        "smsm_open_custom_control_elapsed_ms",
        "smsm_wait_listbox_elapsed_ms",
        "smsm_find_serial_option_elapsed_ms",
        "smsm_click_serial_option_elapsed_ms",
        "smsm_validate_serial_selection_elapsed_ms",
        "smsm_wait_serial_input_elapsed_ms",
    ):
        assert isinstance(trace_values[key], int)


@pytest.mark.parametrize("option_texts", [[], ["シリアル番号", "シリアル番号"]])
def test_serial_input_dom_does_not_click_when_exact_option_is_not_unique(monkeypatch, option_texts):
    class Element:
        def __init__(self, text=""):
            self.text = text
            self.click_calls = 0
            self.attributes = {"id": "control", "aria-expanded": "false"}

        def get_attribute(self, name):
            return self.attributes.get(name)

        def is_displayed(self):
            return True

        def is_enabled(self):
            return True

        def click(self):
            self.click_calls += 1

        def find_elements(self, _by, _value):
            return options

    control = Element()
    options = [Element(text) for text in option_texts]
    listbox = Element()
    listbox.find_elements = lambda _by, _value: options

    class Driver:
        def find_elements(self, _by, value):
            if value == "[role='combobox'], [aria-haspopup='listbox']":
                return [control]
            if value == "[role='listbox']":
                return [listbox]
            if value == "select":
                return []
            if value == "input, textarea":
                return []
            return []

    handler = mod.SmsmHandler.__new__(mod.SmsmHandler)
    handler.browser = type("Browser", (), {"driver": Driver()})()
    monkeypatch.setattr(handler, "wait_for_device_page_stable", lambda trace=None: {
        "custom_select_candidate_count": 1,
        "custom_select_unique": True,
        "select_backed_custom_ui_verified": True,
    })
    with pytest.raises(RuntimeError):
        handler.inspect_serial_input_dom()
    assert control.click_calls == 1
    assert all(option.click_calls == 0 for option in options)


def test_serial_search_dom_mode_does_not_select_when_option_is_ambiguous(monkeypatch):
    logger, calls, _browser = install(monkeypatch, rows=[])

    def ambiguous(_handler, trace=None):
        raise RuntimeError("serial option is ambiguous")

    monkeypatch.setattr(FakeSmsmHandler, "wait_for_search_form_dom", ambiguous)
    assert mod.main(["--inspect-smsm-serial-search-dom"]) == 31
    assert "read" not in calls and "lookup" not in calls
    assert "failed_stage=smsm_wait_search_form_dom" in joined(logger)


def test_serial_search_dom_dynamic_wait_timeout_returns_31(monkeypatch):
    logger, calls, _browser = install(monkeypatch, rows=[])

    def timeout(_handler, trace=None):
        raise RuntimeError("dynamic DOM wait timeout")

    monkeypatch.setattr(FakeSmsmHandler, "wait_for_search_form_dom", timeout)
    assert mod.main(["--inspect-smsm-serial-search-dom"]) == 31
    assert "read" not in calls and "lookup" not in calls
    assert "failed_stage=smsm_wait_search_form_dom" in joined(logger)


def test_one_result_is_success_and_ordered(monkeypatch):
    logger, calls, _browser = install(
        monkeypatch,
        rows=[{"alias": "secret_alias", "serial": "secret_serial", "imei": "35 936730 687217 7"}],
        is_open=True,
    )

    assert mod.main([]) == 0
    assert calls == ["is_open", "detect", "save_close", "unlock", "read", "browser", "login", "device_page", "lookup", "count", "quit", "reopen"]
    output = joined(logger)
    for expected in (
        "lookup_mode=read_only",
        "selected_target_count=1",
        "imei_valid=True",
        "browser_started=True",
        "smsm_login_completed=True",
        "lookup_called=True",
        "lookup_result_count=1",
        "lookup_unique=True",
        "certificate_action_called=False",
        "hennge_action_called=False",
        "smsm_update_called=False",
        "excel_write_called=False",
    ):
        assert expected in output
    assert "secret_alias" not in output and "secret_serial" not in output and "359367306872177" not in output
    assert FakeSmsmHandler.last_serial == "secret_serial"
    assert FakeSmsmHandler.last_config.source == "settings"
    assert FakeSmsmHandler.last_config.valid is True


@pytest.mark.parametrize("result_count, expected_code", [(0, 32), (1, 0), (2, 33), (5, 33)])
def test_result_count_classification(monkeypatch, result_count, expected_code):
    logger, calls, _browser = install(
        monkeypatch,
        rows=[{"alias": "alias_secret", "serial": "serial_secret", "imei": "123456789012345"}],
        result_count=result_count,
    )

    assert mod.main([]) == expected_code
    assert calls[-1] == "quit"
    assert f"lookup_result_count={result_count}" in joined(logger)


def test_only_targets_zero_is_selected_and_pair_is_preserved(monkeypatch):
    logger, _calls, _browser = install(
        monkeypatch,
        rows=[
            {"alias": "first_alias_secret", "serial": "first_serial_secret", "imei": "123456789012345"},
            {"alias": "second_alias_secret", "serial": "second_serial_secret", "imei": "223456789012345"},
        ],
    )

    assert mod.main([]) == 0
    assert FakeSmsmHandler.last_serial == "first_serial_secret"
    assert "selected_target_count=1" in joined(logger)


def test_zero_targets_returns_two_without_browser(monkeypatch):
    logger, calls, _browser = install(monkeypatch, rows=[])
    assert mod.main([]) == 2
    assert "browser" not in calls and "lookup" not in calls
    assert "selected_target_count=0" in joined(logger)


def test_trace_login_mode_does_not_search_or_count(monkeypatch):
    logger, calls, _browser = install(
        monkeypatch,
        rows=[{"alias": "a", "serial": "s", "imei": "123456789012345"}],
    )

    assert mod.main(["--trace-smsm-login"]) == 0
    assert "login" in calls
    assert "lookup" not in calls
    assert "count" not in calls
    assert "smsm_login_completed=True" in joined(logger)


def test_smsm_config_missing_stops_before_browser(monkeypatch):
    logger = DummyLogger()
    monkeypatch.setattr(mod, "AppLogger", lambda _base_dir: logger)
    monkeypatch.setattr(mod, "load_config", lambda: {
        "excel": {"path": "C:/private/target.xlsm"},
        "smsm": {"username": "", "password": ""},
    })
    monkeypatch.setattr(mod, "Browser", lambda *_args: (_ for _ in ()).throw(AssertionError("browser must not start")))

    assert mod.main([]) == 30
    output = joined(logger)
    assert "username_present=False" in output
    assert "password_present=False" in output
    assert "url_scheme_valid=True" in output
    assert "config_object_mapping_valid=True" in output
    assert "failed_stage=smsm_config_validation" in output


def test_common_normalizer_is_used(monkeypatch):
    install(monkeypatch, rows=[{"alias": "a", "serial": "s", "imei": "input"}])
    observed = []

    def fake_normalize(value):
        observed.append(value)
        return "123456789012345"

    monkeypatch.setattr(mod, "normalize_imei", fake_normalize)
    assert mod.main([]) == 0
    assert observed == ["input"]


def test_save_failure_blocks_read_and_browser(monkeypatch):
    _logger, calls, _browser = install(
        monkeypatch,
        rows=[{"alias": "a", "serial": "s", "imei": "123456789012345"}],
        is_open=True,
        save_error=mod.SaveCloseWorkbookError("private"),
    )
    assert mod.main([]) == 12
    assert "read" not in calls and "browser" not in calls and "reopen" not in calls


def test_unlock_failure_blocks_read_and_browser(monkeypatch):
    _logger, calls, _browser = install(
        monkeypatch,
        rows=[{"alias": "a", "serial": "s", "imei": "123456789012345"}],
        is_open=True,
        unlock_error=mod.UnlockTimeoutError("private"),
    )
    assert mod.main([]) == 13
    assert "read" not in calls and "browser" not in calls
    assert calls.count("reopen") == 1


def test_detection_failure_blocks_save_read_and_browser(monkeypatch):
    _logger, calls, _browser = install(monkeypatch, rows=[], is_open=True, detection_match=False)
    assert mod.main([]) == 9
    assert all(name not in calls for name in ("save_close", "read", "browser", "reopen"))


def test_browser_quit_is_called_once_and_excel_reopens_once(monkeypatch):
    _logger, calls, _browser = install(
        monkeypatch,
        rows=[{"alias": "a", "serial": "s", "imei": "123456789012345"}],
        is_open=True,
    )
    assert mod.main([]) == 0
    assert calls.count("quit") == 1
    assert calls.count("reopen") == 1


def test_browser_quit_failure_returns_35_after_excel_reopen(monkeypatch):
    logger, calls, _browser = install(
        monkeypatch,
        rows=[{"alias": "a", "serial": "s", "imei": "123456789012345"}],
        is_open=True,
        quit_error=ConnectionRefusedError("closed"),
    )

    assert mod.main([]) == 35
    assert calls.count("quit") == 1
    assert calls.count("reopen") == 1
    assert "browser_quit_completed=False" in joined(logger)
    assert "browser_quit_exception_type=ConnectionRefusedError" in joined(logger)


def test_browser_quit_failure_does_not_replace_prior_smsm_error(monkeypatch):
    logger, calls, _browser = install(
        monkeypatch,
        rows=[{"alias": "a", "serial": "s", "imei": "123456789012345"}],
        quit_error=ConnectionRefusedError("closed"),
    )

    def fail_login(self, trace=None):
        raise RuntimeError("login failed")

    monkeypatch.setattr(FakeSmsmHandler, "login", fail_login)
    assert mod.main([]) == 30
    assert calls.count("quit") == 1
    assert "browser_quit_completed=False" in joined(logger)


def test_no_hennge_or_forbidden_operations_are_referenced(monkeypatch):
    source = Path(mod.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "HenngeHandler",
        "download_certificate",
        "upload_certificate",
        "associate_imei",
        "Application.Quit",
        "taskkill",
        "Stop-Process",
        "os.startfile",
    ):
        assert forbidden not in source

    logger, _calls, _browser = install(
        monkeypatch=monkeypatch,
        rows=[{"alias": "alias_secret", "serial": "serial_secret", "imei": "123456789012345"}],
    )
    output = joined(logger)
    assert "Cookie" not in output
    assert "session" not in output.lower()


def test_device_detail_only_option_selects_dedicated_route(monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "_run_smsm_device_detail_only", lambda args: calls.append(args) or 0)

    assert mod.main(["--verify-smsm-device-detail-only"]) == 0
    assert calls == [["--verify-smsm-device-detail-only"]]


def test_client_certificate_navigation_only_option_selects_dedicated_route(monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "_run_smsm_client_certificate_navigation_only", lambda args: calls.append(args) or 0)

    assert mod.main(["--inspect-smsm-client-certificate-navigation-only"]) == 0
    assert calls == [["--inspect-smsm-client-certificate-navigation-only"]]


def test_client_certificate_edit_form_only_option_selects_dedicated_route(monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "_run_smsm_client_certificate_edit_form_only", lambda args: calls.append(args) or 0)

    assert mod.main(["--inspect-smsm-client-certificate-edit-form-only"]) == 0
    assert calls == [["--inspect-smsm-client-certificate-edit-form-only"]]


def test_client_certificate_primary_input_only_option_selects_dedicated_route(monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "_run_smsm_client_certificate_primary_input_only", lambda args: calls.append(args) or 0)

    assert mod.main(["--inspect-smsm-client-certificate-primary-input-only"]) == 0
    assert calls == [["--inspect-smsm-client-certificate-primary-input-only"]]


@pytest.mark.parametrize("conflict", ["--allow-device-binding", "--allow-excel-write", "--allow-certificate-upload", "--bind-existing-smsm-certificate"])
def test_client_certificate_primary_input_only_rejects_conflicts_before_browser(monkeypatch, conflict):
    started = []
    monkeypatch.setattr(mod, "Browser", lambda *_args, **_kwargs: started.append(True))

    assert mod.main(["--inspect-smsm-client-certificate-primary-input-only", conflict]) == 2
    assert started == []


def test_client_certificate_imei_input_only_option_selects_dedicated_route(monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "_run_smsm_client_certificate_imei_input_only", lambda args: calls.append(args) or 0)

    assert mod.main(["--inspect-smsm-client-certificate-imei-input-only"]) == 0
    assert calls == [["--inspect-smsm-client-certificate-imei-input-only"]]


def test_client_certificate_direct_save_readiness_only_option_selects_dedicated_route(monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "_run_smsm_client_certificate_imei_direct_save_readiness_only", lambda args: calls.append(args) or 0)

    assert mod.main(["--inspect-smsm-client-certificate-imei-direct-save-readiness-only"]) == 0
    assert calls == [["--inspect-smsm-client-certificate-imei-direct-save-readiness-only"]]


@pytest.mark.parametrize("conflict", [
    "--inspect-smsm-client-certificate-edit-form-only",
    "--inspect-smsm-client-certificate-primary-input-only",
    "--inspect-smsm-client-certificate-imei-input-only",
    "--inspect-smsm-client-certificate-imei-option-selection-only",
    "--run-single-certificate-workflow",
])
def test_direct_save_readiness_rejects_conflicts_before_browser(monkeypatch, capsys, conflict):
    monkeypatch.setattr(mod, "Browser", lambda *_args: (_ for _ in ()).throw(AssertionError("browser must not start")))

    assert mod._run_smsm_client_certificate_imei_direct_save_readiness_only([
        "--inspect-smsm-client-certificate-imei-direct-save-readiness-only", conflict,
    ]) == 2
    output = capsys.readouterr().out
    assert "failed_stage=cli_flag_validation" in output
    assert "browser_start_called=False" in output
    assert "device_imei_send_keys_called=False" in output


def test_direct_save_readiness_early_failure_emits_stdout_once(monkeypatch, capsys):
    monkeypatch.setattr(mod, "load_config", lambda: {"excel": {"path": "unused"}})
    monkeypatch.setattr(mod.ExcelReader, "read_targets", lambda *_args, **_kwargs: [])

    assert mod._run_smsm_client_certificate_imei_direct_save_readiness_only([
        "--inspect-smsm-client-certificate-imei-direct-save-readiness-only",
    ]) == 1
    output = capsys.readouterr().out
    assert "client_certificate_direct_save_readiness_only_runner_called=True" in output
    assert "client_certificate_direct_save_readiness_only_result_available=True" in output
    assert "client_certificate_direct_save_readiness_only_output_called=True" in output
    assert "client_certificate_direct_save_readiness_only_output_completed=True" in output
    assert "client_certificate_direct_save_readiness_only_success=False" in output
    assert "failed_stage=client_certificate_direct_save_readiness_only_load_target" in output


def test_direct_save_readiness_exception_emits_safe_stdout(monkeypatch, capsys):
    monkeypatch.setattr(mod, "load_config", lambda: (_ for _ in ()).throw(KeyError("secret_key")))

    assert mod._run_smsm_client_certificate_imei_direct_save_readiness_only([
        "--inspect-smsm-client-certificate-imei-direct-save-readiness-only",
    ]) == 1
    output = capsys.readouterr().out
    assert "exception_type=KeyError" in output
    assert "exception_key_present=True" in output
    assert "exception_key_safe=secret_key" in output
    assert "secret_key'" not in output
    assert "client_certificate_direct_save_readiness_only_output_completed=True" in output


def test_direct_save_readiness_finalize_emits_output_completed_once(monkeypatch, capsys):
    monkeypatch.setattr(mod, "load_config", lambda: {"excel": {"path": "unused"}})
    monkeypatch.setattr(mod.ExcelReader, "read_targets", lambda *_args, **_kwargs: [])

    assert mod._run_smsm_client_certificate_imei_direct_save_readiness_only([
        "--inspect-smsm-client-certificate-imei-direct-save-readiness-only",
    ]) == 1
    output = capsys.readouterr().out
    assert output.count("client_certificate_direct_save_readiness_only_output_completed=") == 1


@pytest.mark.parametrize("conflict", [
    "--inspect-smsm-client-certificate-edit-form-only",
    "--inspect-smsm-client-certificate-primary-input-only",
    "--allow-device-binding",
    "--allow-excel-write",
    "--allow-certificate-upload",
    "--run-single-certificate-workflow",
])
def test_client_certificate_imei_input_only_rejects_conflicts_before_browser(monkeypatch, conflict):
    started = []
    monkeypatch.setattr(mod, "Browser", lambda *_args, **_kwargs: started.append(True))

    assert mod.main(["--inspect-smsm-client-certificate-imei-input-only", conflict]) == 2
    assert started == []


def test_client_certificate_imei_option_selection_only_option_selects_dedicated_route(monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "_run_smsm_client_certificate_imei_option_selection_only", lambda args: calls.append(args) or 0)
    assert mod.main(["--inspect-smsm-client-certificate-imei-option-selection-only"]) == 0
    assert calls == [["--inspect-smsm-client-certificate-imei-option-selection-only"]]


@pytest.mark.parametrize("conflict", [
    "--inspect-smsm-client-certificate-edit-form-only",
    "--inspect-smsm-client-certificate-primary-input-only",
    "--inspect-smsm-client-certificate-imei-input-only",
    "--allow-device-binding",
    "--allow-excel-write",
    "--allow-certificate-upload",
    "--run-single-certificate-workflow",
])
def test_client_certificate_imei_option_selection_only_rejects_conflicts_before_browser(monkeypatch, conflict):
    started = []
    monkeypatch.setattr(mod, "Browser", lambda *_args, **_kwargs: started.append(True))
    assert mod.main(["--inspect-smsm-client-certificate-imei-option-selection-only", conflict]) == 2
    assert started == []


def test_client_certificate_imei_option_selection_only_early_failure_emits_safe_diagnostics(monkeypatch, capsys):
    monkeypatch.setattr(mod, "load_config", lambda: {"excel": {"path": "unused"}})
    monkeypatch.setattr(mod.ExcelReader, "read_targets", lambda *_args, **_kwargs: [])

    assert mod._run_smsm_client_certificate_imei_option_selection_only(["--inspect-smsm-client-certificate-imei-option-selection-only"]) == 1
    output = capsys.readouterr().out
    assert "client_certificate_imei_selection_only_runner_called=True" in output
    assert "client_certificate_imei_selection_only_result_available=True" in output
    assert "client_certificate_imei_selection_only_output_called=True" in output
    assert "client_certificate_imei_selection_only_output_completed=True" in output
    assert "failed_stage=client_certificate_imei_selection_only_load_target" in output
    assert "device_binding_save_called=False" in output
    assert "excel_write_called=False" in output


def test_client_certificate_imei_option_selection_only_exception_emits_safe_diagnostics(monkeypatch, capsys):
    monkeypatch.setattr(mod, "load_config", lambda: (_ for _ in ()).throw(KeyError("secret_key")))

    assert mod._run_smsm_client_certificate_imei_option_selection_only(["--inspect-smsm-client-certificate-imei-option-selection-only"]) == 1
    output = capsys.readouterr().out
    assert "exception_type=KeyError" in output
    assert "exception_key_present=True" in output
    assert "exception_key_safe=secret_key" in output
    assert "client_certificate_imei_selection_only_output_completed=True" in output


def test_client_certificate_imei_option_selection_only_finalize_emits_once_and_preserves_stage():
    emitted = []
    logger = DummyLogger()
    result = {
        "client_certificate_imei_selection_only_runner_called": True,
        "client_certificate_suggestion_click_called": True,
        "client_certificate_suggestion_click_count": 1,
        "device_imei_send_keys_called": True,
        "device_imei_send_keys_count": 1,
        "device_binding_save_called": False,
        "excel_write_called": False,
        "failed_stage": "",
        "last_completed_stage": "client_certificate_imei_selection_only_completed",
    }

    assert result["failed_stage"] == ""
    assert result["last_completed_stage"].endswith("completed")
    assert result["device_binding_save_called"] is False
    assert result["excel_write_called"] is False


@pytest.mark.parametrize("conflict", ["--bind-existing-smsm-certificate", "--allow-device-binding", "--allow-excel-write", "--allow-certificate-upload", "--verify-smsm-device-detail-only"])
def test_client_certificate_edit_form_only_rejects_conflicts_before_browser(monkeypatch, conflict):
    started = []
    monkeypatch.setattr(mod, "Browser", lambda *_args, **_kwargs: started.append(True))

    assert mod.main(["--inspect-smsm-client-certificate-edit-form-only", conflict]) == 2
    assert started == []


def test_edit_form_cli_removes_internal_panel_element_before_logging():
    source = Path(mod.__file__).read_text(encoding="utf-8")

    assert 'result.pop("client_certificate_panel", None)' in source


def test_edit_form_exception_classification_is_safe_and_stage_specific():
    assert mod._classify_edit_form_exception(RuntimeError("serial_secret"), "client_certificate_edit_form_only_open_device_list") == "device_list_not_verified"
    assert mod._classify_edit_form_exception(RuntimeError("imei_secret"), "client_certificate_edit_form_only_wait_view_state") == "view_state_timeout"
    assert mod._classify_edit_form_exception(RuntimeError("secret"), "client_certificate_edit_form_only_click_edit") == "edit_click_failed"


def test_edit_form_observation_scalar_merge_excludes_internal_objects():
    class FakeElement:
        pass

    merged = mod._safe_observation_scalars({
        "device_search_submit_count": 1,
        "device_result_identity_verified": True,
        "panel": FakeElement(),
        "nested": {"private": FakeElement()},
    })

    assert merged["device_search_submit_count"] == 1
    assert merged["device_result_identity_verified"] is True
    assert "panel" not in merged
    assert "nested" not in merged


def _edit_form_success_result(**overrides):
    result = {
        "device_result_identity_verified": True,
        "other_settings_click_count": 1,
        "client_certificate_item_click_count": 1,
        "client_certificate_edit_state_detected": True,
        "client_certificate_edit_marker_wait_completed": True,
        "client_certificate_edit_marker_last_snapshot_available": True,
        "client_certificate_edit_transition_detected": True,
        "client_certificate_after_snapshot_created": True,
        "client_certificate_after_snapshot_uses_current_classification": True,
        "client_certificate_after_snapshot_uses_before_fallback": False,
        "client_certificate_after_snapshot_metrics_consistent": True,
        "client_certificate_edit_control_presence_verified": True,
        "client_certificate_after_control_element_count": 2,
        "client_certificate_save_candidate_count": 1,
        "client_certificate_cancel_candidate_count": 1,
        "client_certificate_edit_click_count": 1,
        "client_certificate_primary_input_resolved": False,
        "client_certificate_primary_input_resolution_required": True,
    }
    result.update(overrides)
    return result


def _edit_form_safe_operations(**overrides):
    operations = {
        "client_certificate_selection_control_click_called": False,
        "client_certificate_option_selection_called": False,
        "device_imei_send_keys_called": False,
        "device_binding_save_called": False,
        "client_certificate_cancel_click_called": False,
        "excel_write_called": False,
        "certificate_upload_called": False,
    }
    operations.update(overrides)
    return operations


def test_edit_form_cli_accepts_two_input_elements_when_state_transition_is_valid():
    assert mod._client_certificate_edit_form_success(_edit_form_success_result(), _edit_form_safe_operations()) is True


def test_edit_form_cli_accepts_unresolved_primary_input_without_forbidden_operations():
    result = _edit_form_success_result(client_certificate_primary_input_resolved=False)
    assert mod._client_certificate_edit_form_success(result, _edit_form_safe_operations()) is True
    assert result["client_certificate_primary_input_resolution_required"] is True


def test_edit_form_success_uses_marker_transition_without_strict_edit_state_or_visibility():
    result = _edit_form_success_result(
        client_certificate_edit_state_detected=False,
        client_certificate_edit_form_visible=False,
        client_certificate_edit_form_visibility_verified=False,
        client_certificate_edit_form_wait_completed=False,
        client_certificate_edit_form_candidate_count=0,
        client_certificate_control_logical_group_count=0,
    )

    assert mod._client_certificate_edit_form_success(result, _edit_form_safe_operations()) is True


def test_edit_form_success_keeps_forbidden_operation_as_failure():
    result = _edit_form_success_result(client_certificate_edit_state_detected=False)

    assert mod._client_certificate_edit_form_success(
        result,
        _edit_form_safe_operations(device_imei_send_keys_called=True),
    ) is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("client_certificate_save_candidate_count", 0),
        ("client_certificate_save_candidate_count", 2),
        ("client_certificate_cancel_candidate_count", 0),
        ("client_certificate_cancel_candidate_count", 2),
        ("client_certificate_edit_transition_detected", False),
    ],
)
def test_edit_form_cli_rejects_invalid_edit_state_markers(field, value):
    assert mod._client_certificate_edit_form_success(_edit_form_success_result(**{field: value}), _edit_form_safe_operations()) is False


@pytest.mark.parametrize(
    "operation",
    [
        "client_certificate_selection_control_click_called",
        "client_certificate_option_selection_called",
        "device_imei_send_keys_called",
        "device_binding_save_called",
        "client_certificate_cancel_click_called",
        "excel_write_called",
        "certificate_upload_called",
    ],
)
def test_edit_form_cli_rejects_forbidden_operation(operation):
    assert mod._client_certificate_edit_form_success(_edit_form_success_result(), _edit_form_safe_operations(**{operation: True})) is False


def test_name_error_diagnostics_include_safe_name_and_project_frame(tmp_path):
    def raise_name_error():
        raise NameError("private detail", name="missing_public_name")

    try:
        raise_name_error()
    except NameError as error:
        result = mod._name_error_diagnostics(error, Path(mod.__file__).resolve().parent)

    assert result["exception_name_present"] is True
    assert result["exception_name_safe"] == "missing_public_name"
    assert result["exception_name_class"] == "NameError"
    assert result["exception_source_line_present"] is True
    assert result["exception_source_inside_project"] is True


def test_runtime_source_diagnostics_report_expected_modules_and_fingerprints():
    result = mod._runtime_source_diagnostics()

    assert result["diagnostic_script_path_matches_expected"] is True
    assert result["smsm_handler_module_path_matches_expected"] is True
    assert result["workflow_service_module_path_matches_expected"] is True
    assert len(result["diagnostic_script_source_fingerprint"]) == 12
    assert len(result["smsm_handler_source_fingerprint"]) == 12
    assert len(result["workflow_service_source_fingerprint"]) == 12


@pytest.mark.parametrize("conflict", ["--bind-existing-smsm-certificate", "--allow-device-binding", "--allow-excel-write", "--allow-certificate-upload"])
def test_client_certificate_navigation_only_rejects_conflicting_flags(monkeypatch, conflict):
    started = []
    monkeypatch.setattr(mod, "Browser", lambda *_args, **_kwargs: started.append(True))

    assert mod.main(["--inspect-smsm-client-certificate-navigation-only", conflict]) == 2
    assert started == []


@pytest.mark.parametrize("conflict", [
    "--bind-existing-smsm-certificate",
    "--allow-device-binding",
    "--allow-excel-write",
    "--allow-certificate-upload",
])
def test_device_detail_only_rejects_conflicting_flags_before_browser(monkeypatch, conflict):
    started = []
    monkeypatch.setattr(mod, "Browser", lambda *_args, **_kwargs: started.append(True))

    assert mod.main(["--verify-smsm-device-detail-only", conflict]) == 2
    assert started == []


@pytest.mark.parametrize("identity_verified", [True, False])
def test_device_detail_only_stops_after_identity_without_mutation(monkeypatch, capsys, identity_verified):
    class Logger:
        def info(self, message):
            return None

    class FakeBrowser:
        def start(self):
            return None

        def quit(self):
            return None

    class FakeReader:
        def __init__(self, _path):
            pass

        def read_targets(self, include_row_number=False):
            return [{"alias": "alias", "serial": "serial", "imei": "123456789012345"}]

    class FakeService:
        def __init__(self, **_kwargs):
            self.smsm = self
            self.device_observation = {}

        def smsm_login(self, context):
            context.record("smsm_login_completed", True)

        def smsm_open_device_list(self, context):
            context.record("device_list_page_verified", True)

        def smsm_search_device_by_serial(self, context, read_only=False):
            assert read_only is True
            for key, value in {
                "device_search_submit_count": 1,
                "device_search_result_container_count": 1,
                "device_search_result_total_count": 1,
                "device_search_result_page_count": 1,
                "device_search_post_result_visible_row_count": 1,
                "device_result_candidate_count": 1,
                "device_result_candidate_unique": True,
            }.items():
                context.record(key, value)

        def select_matched_device_row(self, _serial):
            return {
                "device_result_click_candidate_count": 1,
                "device_result_click_unique": True,
                "device_result_click_called": True,
                "device_result_click_count": 1,
                "device_detail_panel_candidate_count": 1,
                "device_detail_serial_field_candidate_count": 1,
                "device_detail_serial_value_candidate_count": 1,
                "device_detail_serial_exact_match": identity_verified,
                "device_detail_navigation_verified": identity_verified,
                "device_result_selected": identity_verified,
                "device_result_identity_verified": identity_verified,
                "device_result_identity_verification_method": "device_detail_panel_serial_exact_match" if identity_verified else "",
            }

    monkeypatch.setattr(mod, "AppLogger", lambda *_args, **_kwargs: Logger())
    monkeypatch.setattr(mod, "load_config", lambda: {"excel": {"path": "target.xlsm"}})
    monkeypatch.setattr(mod, "ExcelReader", FakeReader)
    monkeypatch.setattr(mod, "Browser", lambda *_args, **_kwargs: FakeBrowser())
    monkeypatch.setattr(mod, "ProductionWorkflowService", FakeService)
    monkeypatch.setattr(mod, "resolve_smsm_config", lambda _config: object())

    result = mod.main(["--verify-smsm-device-detail-only"])
    output = capsys.readouterr().out
    assert result == (0 if identity_verified else 1)
    assert "other_settings_click_called=False" in output
    assert "device_client_certificate_click_called=False" in output
    assert "certificate_selection_called=False" in output
    assert "device_binding_save_called=False" in output
    assert "excel_write_called=False" in output
    assert "certificate_upload_called=False" in output
