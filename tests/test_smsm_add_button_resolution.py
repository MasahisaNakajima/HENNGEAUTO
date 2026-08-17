from __future__ import annotations

import inspect

import pytest

from app.smsm_handler import SmsmHandler
import diagnose_smsm_single_target_lookup as diagnostic


SOURCE = inspect.getsource(SmsmHandler._inspect_client_certificate_add_button_dom)
FORM_SOURCE = inspect.getsource(SmsmHandler._inspect_add_form_state)
CONTROLS_SOURCE = inspect.getsource(SmsmHandler._inspect_add_form_controls_dom)
PROBE_SOURCE = inspect.getsource(SmsmHandler.inspect_client_certificate_add_form_dom_for_diagnostic)
DIAGNOSTIC_SOURCE = inspect.getsource(diagnostic._run_lookup)
STRICT_SOURCE = inspect.getsource(SmsmHandler._strict_client_certificate_page_state)


@pytest.mark.parametrize("icon_tag", ("button", "span", "svg", "path"))
def test_add_button_resolution_supports_icon_shapes(icon_tag):
    assert icon_tag in {"button", "span", "svg", "path"}
    assert "clickableAncestor" in SOURCE
    assert "plusRoots" in SOURCE
    assert "verified_unique_safe_plus_icon" in SOURCE


def test_resolution_deduplicates_clickable_ancestor_dom_nodes():
    assert "new Set(resolvedAncestors)" in SOURCE
    assert "deduplicated_add_button_candidate_count" in SOURCE


@pytest.mark.parametrize("excluded", ("isSearch", "isDropdown", "isRow", "isPage", "isDestructive"))
def test_resolution_keeps_destructive_and_non_add_exclusions(excluded):
    assert excluded in SOURCE
    assert "add_button_has_unique_plus_icon" in SOURCE


def test_resolution_requires_one_safe_candidate_and_normal_click():
    assert "eligible.length === 1" in SOURCE
    assert "control.click()" in inspect.getsource(SmsmHandler.inspect_client_certificate_add_form_dom_for_diagnostic)
    assert ".execute_script" not in inspect.getsource(SmsmHandler.inspect_client_certificate_add_form_dom_for_diagnostic)


def test_resolution_records_single_click_and_does_not_send_values():
    method_source = inspect.getsource(SmsmHandler.inspect_client_certificate_add_form_dom_for_diagnostic)
    assert 'add_button_click_count"] = 1' in method_source
    assert 'file_input_send_keys_called": False' in method_source
    assert 'password_input_send_keys_called": False' in method_source
    assert 'certificate_submit_button_click_called": False' in method_source


def test_add_form_resolves_create_side_panel_and_three_controls():
    assert "role=\"complementary\"" in FORM_SOURCE
    assert "side-panel" in FORM_SOURCE
    assert "新規作成" in FORM_SOURCE
    assert "クライアント証明書" in FORM_SOURCE
    assert "smsm_client_certificate_create_side_panel" in FORM_SOURCE
    assert "file_input_count: fileInputs.length" in FORM_SOURCE
    assert "password_input_count: passwordInputs.length" in FORM_SOURCE
    assert "submit_button_candidate_count: saveButtons.length" in FORM_SOURCE


def test_add_form_keeps_hidden_file_input_and_requires_labels():
    assert "hasLabel" in FORM_SOURCE
    assert "!item.disabled" in FORM_SOURCE
    assert "file_input_hidden_allowed" in FORM_SOURCE
    assert "visible(fileInputs[0])" not in FORM_SOURCE.split("const fileInputs", 1)[1].split("const passwordInputs", 1)[0]


def test_add_form_does_not_perform_upload_or_save_actions():
    assert 'file_input_send_keys_called: false' in FORM_SOURCE
    assert 'password_input_send_keys_called: false' in FORM_SOURCE
    assert 'certificate_submit_button_click_called: false' in FORM_SOURCE


def test_add_form_probe_preserves_last_snapshot_and_probe_exception_type():
    assert 'add_form_probe_called' in PROBE_SOURCE
    assert 'add_form_probe_completed' in PROBE_SOURCE
    assert 'add_form_probe_exception_type' in PROBE_SOURCE
    assert 'add_form_probe_iteration_count' in PROBE_SOURCE
    assert 'add_form_last_snapshot_available' in PROBE_SOURCE
    assert 'failed_stage": "smsm_probe_certificate_add_form_dom"' in PROBE_SOURCE
    assert 'failed_stage": "smsm_wait_certificate_add_form"' in PROBE_SOURCE
    assert 'WebDriverWait(driver, 15.0' in PROBE_SOURCE


def test_add_form_probe_has_fixed_phases_and_stops_on_javascript_exception():
    for phase in (
        "probe_base_counts", "resolve_file_input", "resolve_save_button",
        "resolve_right_panel", "resolve_password_label", "resolve_password_input",
        "calculate_common_ancestor", "build_snapshot",
    ):
        assert phase in CONTROLS_SOURCE
    assert "add_form_probe_failed_phase" in CONTROLS_SOURCE
    assert "add_form_probe_javascript_error_name" in CONTROLS_SOURCE
    assert "add_form_probe_snapshot_before_failure_available" in CONTROLS_SOURCE
    assert "JavascriptException" in CONTROLS_SOURCE
    assert "add_form_probe_iteration_count" in PROBE_SOURCE


def test_add_form_probe_counts_iframes_shadow_roots_and_top_document_controls():
    for key in (
        'top_document_iframe_count', 'visible_iframe_count', 'same_origin_iframe_count',
        'cross_origin_iframe_count', 'open_shadow_root_host_count',
        'shadow_root_file_input_count', 'shadow_root_password_input_count',
        'shadow_root_save_button_count', 'top_document_file_input_count',
        'top_document_password_input_count', 'top_document_text_input_count',
        'top_document_button_count', 'top_document_submit_input_count',
    ):
        assert key in CONTROLS_SOURCE
    assert 'frame.contentDocument' in CONTROLS_SOURCE
    assert 'catch (_error)' in CONTROLS_SOURCE


def test_add_form_failure_checkpoint_is_supported_without_manual_controls():
    assert '--manual-checkpoint-on-smsm-add-form-failure' in DIAGNOSTIC_SOURCE
    assert 'handler._inspect_add_form_controls_dom(handler.browser.driver)' in DIAGNOSTIC_SOURCE
    assert '保存は手動操作しないでください' in DIAGNOSTIC_SOURCE
    assert 'failure_observation.get("failed_stage") == "smsm_wait_certificate_add_form"' in DIAGNOSTIC_SOURCE


def test_strict_navigation_candidates_are_scoped_and_deduplicated():
    for selector in (
        "nav,header,[role=\"navigation\"]",
        "[role=\"tab\"]",
        "[role=\"menuitem\"]",
        "aria-current",
        "aria-selected",
        "active|selected",
    ):
        assert selector in STRICT_SOURCE
    assert "new Set(nodes.filter" in STRICT_SOURCE
    assert "closest('a,button" in STRICT_SOURCE
    assert "smsm_settings_nav_candidate_count: settings.length" in STRICT_SOURCE


def test_strict_device_and_certificate_menu_use_scoped_active_evidence():
    assert "roots.some" not in STRICT_SOURCE
    assert "smsm_device_nav_active: devices.length === 1" in STRICT_SOURCE
    assert "root.querySelectorAll('a,button" in STRICT_SOURCE
    assert "item.pathname.replace" in STRICT_SOURCE
    assert "クライアント証明書管理" in STRICT_SOURCE


def test_strict_search_resolution_records_counts_and_excludes_side_inputs():
    for key in (
        "smsm_search_input_global_count",
        "smsm_search_input_inside_center_content_count",
        "smsm_search_input_inside_certificate_toolbar_count",
        "smsm_search_input_after_exclusion_count",
        "smsm_certificate_search_input_candidate_count",
    ):
        assert key in STRICT_SOURCE
    assert "input[type=\"search\"]" not in STRICT_SOURCE
    assert "role') === 'searchbox'" in STRICT_SOURCE
    assert "closest('header,nav,aside" in STRICT_SOURCE
    assert "[class*=\"right\" i]" in STRICT_SOURCE


def test_page_diagnostic_never_clicks_add_or_writes_controls():
    page_only_block = DIAGNOSTIC_SOURCE.split("if inspect_client_certificate_page_dom:", 1)[1]
    assert '_emit(logger, "add_button_click_called", False)' in page_only_block
    assert "set_certificate_file" not in page_only_block
    assert "set_certificate_password" not in page_only_block
    assert '"certificate_submit_button_click_called", "certificate_upload_called"' in page_only_block


def test_add_form_snapshot_is_json_safe_and_does_not_return_dom_objects():
    assert "add_form_probe_completed_phases" in CONTROLS_SOURCE
    assert "add_form_probe_failed: true" in CONTROLS_SOURCE
    assert "schema:" not in CONTROLS_SOURCE
    assert "new Set(resolvedLabels.map" in CONTROLS_SOURCE


@pytest.mark.parametrize("input_type", ("password", "text"))
def test_password_is_resolved_by_certificate_password_label(input_type):
    assert input_type in CONTROLS_SOURCE
    assert "証明書を保護するパスワード" in CONTROLS_SOURCE
    assert "label_for_id" in CONTROLS_SOURCE
    assert "aria_labelledby" in CONTROLS_SOURCE
    assert "same_field_group" in CONTROLS_SOURCE
    assert "adjacent_label_input" in CONTROLS_SOURCE
    assert "unique_right_panel_input" in CONTROLS_SOURCE


def test_password_resolution_has_staged_counts_and_exclusions():
    for key in (
        "password_input_global_candidate_count", "password_input_inside_right_panel_count",
        "password_label_candidate_count", "password_label_associated_input_count",
        "password_input_after_type_filter_count", "password_input_after_exclusion_count",
        "password_input_after_visibility_count", "password_input_resolution_method",
    ):
        assert key in CONTROLS_SOURCE
    assert 'input[type="file"]' in CONTROLS_SOURCE
    assert 'input[type="hidden"]' in CONTROLS_SOURCE
    assert 'input[type="checkbox"]' in CONTROLS_SOURCE
    assert 'input[type="radio"]' in CONTROLS_SOURCE


def test_password_resolution_recomputes_common_ancestor_without_actions():
    assert "panelFiles.length === 1" in CONTROLS_SOURCE
    assert "panelSaves.length === 1" in CONTROLS_SOURCE
    assert "visiblePasswords.length === 1" in CONTROLS_SOURCE
    assert "upload_controls_common_ancestor_count" in CONTROLS_SOURCE
    assert 'file_input_send_keys_called: false' in CONTROLS_SOURCE
    assert 'password_input_send_keys_called: false' in CONTROLS_SOURCE
    assert 'certificate_submit_button_click_called: false' in CONTROLS_SOURCE