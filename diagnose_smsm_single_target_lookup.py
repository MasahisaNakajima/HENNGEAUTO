from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import socket
import sys
import time
import tempfile
import argparse
import inspect
import re
import traceback
from datetime import datetime
from pathlib import Path

from app.browser import Browser
from app.config import load_config
from app.excel_reader import ExcelReader
from app.excel_session import (
    ReadOnlyWorkbookError,
    SaveCloseWorkbookError,
    _resolve_web_identity_hash,
    detect_target_workbook,
    save_and_close_target_workbook,
)
from app.imei_normalizer import is_target_row, normalize_imei
from app.logger import AppLogger
from app.main import reopen_excel
from app.smsm_handler import SmsmHandler, enumerate_smsm_search_controls
from app.smsm_config import (
    config_mapping_is_valid,
    credential_status,
    password_contains_assignment_syntax,
    password_contains_unsafe_syntax,
    resolve_smsm_config,
    url_validation_status,
)
from app.workflow_service import ProductionWorkflowService
from diagnose_excel_target import UnlockTimeoutError, _wait_unlock
from app.single_certificate_workflow import (
    PREPARE_SMSM_CERTIFICATE_UPLOAD_STAGES,
    WORKFLOW_STAGES,
    WorkflowContext,
    WorkflowOptions,
    make_default_handlers,
    make_preparation_handlers,
    run_single_certificate_workflow,
)


LOGIN_TARGET_SECONDS = 10
DEVICE_PAGE_TARGET_SECONDS = 10
SERIAL_OPTION_TARGET_SECONDS = 10
SERIAL_INPUT_TARGET_SECONDS = 10
BROWSER_QUIT_TARGET_SECONDS = 10
EXCEL_VISIBLE_TARGET_SECONDS = 10
ENGINEERING_STAGE_TIMEOUT_SECONDS = 30
SMSM_ROUTE_VERSION = "2"


def _emit_elapsed(logger, name: str, started_at: float, target_seconds: float) -> None:
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    _emit(logger, f"{name}_elapsed_ms", elapsed_ms)
    if elapsed_ms > target_seconds * 1000:
        _emit(logger, f"{name}_slow", True)


def _excel_window_state() -> tuple[bool, bool, bool]:
    try:
        import win32com.client
        application = win32com.client.GetActiveObject("Excel.Application")
        workbooks = application.Workbooks
        workbook_opened = bool(workbooks.Count)
        visible = bool(application.Visible)
        responsive = bool(application.Ready)
        return True, workbook_opened and visible, responsive
    except Exception:
        return False, False, False


def _wait_excel_window_visible(timeout_seconds: float, emit) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() <= deadline:
        process_detected, visible, responsive = _excel_window_state()
        if not process_detected:
            return
        emit("excel_process_detected", process_detected)
        emit("excel_workbook_opened", visible)
        emit("excel_window_visible", visible)
        emit("excel_window_responsive", responsive)
        if visible and responsive:
            return
        time.sleep(0.2)


class BrowserStartFailedError(RuntimeError):
    pass


class BrowserQuitFailedError(RuntimeError):
    pass


class ReopenFailedError(RuntimeError):
    pass


class ResultContractError(RuntimeError):
    pass


class ManifestProcessingError(RuntimeError):
    def __init__(self, stage: str, reason: str) -> None:
        super().__init__(reason)
        self.stage = stage
        self.reason = reason


SERIAL_INPUT_REQUIRED_KEYS = (
    "serial_input_candidate_count",
    "serial_input_unique",
    "serial_input_clear_called",
    "serial_input_send_keys_called",
    "serial_input_nonblank",
    "serial_input_exact_match",
    "serial_input_length_match",
    "serial_input_was_truncated",
    "serial_input_was_transformed",
    "serial_mapping_valid",
    "search_button_click_called",
    "smsm_update_called",
    "excel_write_called",
)


def _validate_serial_input_result(result) -> None:
    if not isinstance(result, dict) or any(key not in result for key in SERIAL_INPUT_REQUIRED_KEYS):
        raise ResultContractError("serial input result contract is incomplete")
    success_conditions = {
        "serial_input_candidate_count": result.get("serial_input_candidate_count") == 1,
        "serial_input_unique": result.get("serial_input_unique") is True,
        "serial_input_clear_called": result.get("serial_input_clear_called") is True,
        "serial_input_send_keys_called": result.get("serial_input_send_keys_called") is True,
        "serial_input_nonblank": result.get("serial_input_nonblank") is True,
        "serial_input_exact_match": result.get("serial_input_exact_match") is True,
        "serial_input_length_match": result.get("serial_input_length_match") is True,
        "serial_input_was_truncated": result.get("serial_input_was_truncated") is False,
        "serial_input_was_transformed": result.get("serial_input_was_transformed") is False,
        "serial_mapping_valid": result.get("serial_mapping_valid") is True,
        "search_button_click_called": result.get("search_button_click_called") is False,
        "smsm_update_called": result.get("smsm_update_called") is False,
        "excel_write_called": result.get("excel_write_called") is False,
    }
    if not all(success_conditions.values()):
        raise ResultContractError("serial input result validation failed")


class _SilentHandlerLogger:
    def info(self, _message: str) -> None:
        return None

    def error(self, _message: str) -> None:
        return None

    def exception(self, _message: str) -> None:
        return None

    def save_browser_diagnostics(self, _driver, _name: str, save_html: bool = True) -> None:
        return None


def _base_dir() -> Path:
    return Path(__file__).resolve().parent


def _emit(logger, key: str, value) -> None:
    logger.info(f"{key}={value}")


def _log_failure(logger, stage: str, exc: BaseException) -> None:
    _emit(logger, "failed_stage", stage)
    _emit(logger, "exception_type", type(exc).__name__)


def _smsm_config_status(smsm_config) -> dict[str, object]:
    url_status = url_validation_status(smsm_config.url)
    return {
        **url_status,
        **credential_status(smsm_config),
        "company_code_present": bool(smsm_config.company_code),
        "username_present": bool(smsm_config.username),
        "password_present": bool(smsm_config.password),
        "company_code_length_positive": len(smsm_config.company_code) > 0,
        "username_length_positive": len(smsm_config.username) > 0,
        "password_length_positive": len(smsm_config.password) > 0,
        "company_code_looks_like_email": "@" in smsm_config.company_code,
        "username_looks_like_email": "@" in smsm_config.username,
        "password_contains_powershell_syntax": password_contains_unsafe_syntax(smsm_config.password),
        "password_contains_assignment_syntax": password_contains_assignment_syntax(smsm_config.password),
        "config_object_mapping_valid": config_mapping_is_valid(smsm_config),
    }


def _dom_schema_path() -> Path:
    return _base_dir() / "logs" / f"smsm_login_dom_schema_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"


def _inspect_login_dom(driver) -> tuple[dict[str, object], list[dict[str, object]]]:
    script = """
        const inputs = Array.from(document.querySelectorAll('input'));
        const labels = Array.from(document.querySelectorAll('label'));
        const labelFor = new Set(labels.map(label => label.getAttribute('for')).filter(Boolean));
        return {
            inputs: inputs.map((input, index) => ({
                element_index: index,
                type: input.getAttribute('type'),
                id: input.getAttribute('id'),
                name: input.getAttribute('name'),
                autocomplete: input.getAttribute('autocomplete'),
                inputmode: input.getAttribute('inputmode'),
                maxlength_present: input.hasAttribute('maxlength'),
                pattern_present: input.hasAttribute('pattern'),
                readonly: input.hasAttribute('readonly'),
                disabled: input.hasAttribute('disabled'),
                displayed: Boolean(input.offsetWidth || input.offsetHeight || input.getClientRects().length),
                enabled: !input.disabled,
                label_linked: Boolean(input.id && labelFor.has(input.id))
            })),
            label_total_count: labels.length
        };
    """
    result = driver.execute_script(script)
    inputs = result.get("inputs", []) if isinstance(result, dict) else []
    inputs = [item for item in inputs if isinstance(item, dict)]
    def text(value) -> str:
        return value.casefold() if isinstance(value, str) else ""
    def usable(item, expected_type: str) -> bool:
        return (
            text(item.get("type")) == expected_type
            and item.get("displayed") is True
            and item.get("enabled") is True
            and item.get("readonly") is False
            and item.get("disabled") is False
        )
    def exact_candidates(element_id: str, element_name: str, expected_type: str):
        by_id = [item for item in inputs if item.get("id") == element_id and usable(item, expected_type)]
        if by_id:
            return by_id
        return [item for item in inputs if item.get("name") == element_name and usable(item, expected_type)]
    company_candidates = exact_candidates("user_company_code", "user[company_code]", "text")
    user_candidates = exact_candidates("user_login", "user[login]", "text")
    password_candidates = exact_candidates("user_password", "user[password]", "password")
    summary = {
        "login_form_found": bool(inputs),
        "input_total_count": len(inputs),
        "text_input_count": sum(text(item.get("type")) == "text" for item in inputs),
        "password_input_count": len(password_candidates),
        "label_total_count": int(result.get("label_total_count", 0)) if isinstance(result, dict) else 0,
        "labeled_input_count": sum(bool(item.get("label_linked")) for item in inputs),
        "unique_id_count": len({item.get("id") for item in inputs if item.get("id")}),
        "unique_name_count": len({item.get("name") for item in inputs if item.get("name")}),
        "company_candidate_count": len(company_candidates),
        "user_candidate_count": len(user_candidates),
        "password_candidate_count": len(password_candidates),
        "company_candidate_unique": len(company_candidates) == 1,
        "user_candidate_unique": len(user_candidates) == 1,
        "password_candidate_unique": len(password_candidates) == 1,
    }
    return summary, inputs


def _write_dom_schema(schema_path: Path, inputs: list[dict[str, object]]) -> None:
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    allowed_keys = {
        "element_index", "type", "id", "name", "autocomplete", "inputmode",
        "maxlength_present", "pattern_present", "readonly", "disabled",
        "displayed", "enabled", "label_linked",
    }
    safe_inputs = [{key: item.get(key) for key in allowed_keys} for item in inputs]
    schema_path.write_text(json.dumps(safe_inputs, ensure_ascii=False, indent=2), encoding="utf-8")


def _device_dom_schema_path() -> Path:
    return _base_dir() / "logs" / f"smsm_device_search_dom_schema_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"


def _serial_search_dom_schema_path() -> Path:
    return _base_dir() / "logs" / f"smsm_serial_search_dom_schema_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"


def _serial_search_preselection_dom_schema_path() -> Path:
    return _base_dir() / "logs" / f"smsm_serial_search_preselection_dom_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"


def _serial_search_results_dom_schema_path() -> Path:
    return _base_dir() / "logs" / f"smsm_serial_search_results_dom_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"


def _smsm_result_match_schema_path() -> Path:
    return _base_dir() / "logs" / f"smsm_result_match_schema_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"


def _smsm_client_certificate_upload_dom_schema_path() -> Path:
    return _base_dir() / "logs" / f"smsm_client_certificate_upload_dom_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"


def _smsm_settings_navigation_dom_schema_path() -> Path:
    return _base_dir() / "logs" / f"smsm_settings_navigation_dom_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"


def _smsm_navigation_failure_schema_path(stage: str) -> Path:
    safe_stage = {
        "settings": "settings",
        "ios": "ios",
        "certificate_management": "certificate_management",
        "client_certificate_management": "client_certificate_management",
    }.get(stage, "unknown")
    return _base_dir() / "logs" / f"smsm_navigation_{safe_stage}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"


def _smsm_certificate_workflow_diagnostic_path() -> Path:
    return _base_dir() / "logs" / f"smsm_certificate_workflow_diagnostic_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"


def _smsm_navigation_route_path() -> Path:
    return _base_dir() / "config" / "smsm_navigation_route.json"


def _smsm_invalid_navigation_route_path() -> Path:
    return _base_dir() / "config" / "smsm_navigation_route.invalid.json"


def _backup_invalid_navigation_route() -> bool:
    source = _smsm_navigation_route_path()
    target = _smsm_invalid_navigation_route_path()
    if not source.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return True


def _fingerprint(value: object) -> str | None:
    if value in (None, ""):
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _assert_json_safe(value: object) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, list):
        for item in value:
            _assert_json_safe(item)
        return
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("manifest dictionary keys must be strings")
        for item in value.values():
            _assert_json_safe(item)
        return
    raise TypeError("manifest contains a non-JSON value")


def _manifest_schema_from_checkpoint(checkpoint: dict[str, object]) -> dict[str, object]:
    observed_boolean_keys = (
        "target_os_ios_verified", "ios_tab_selected", "android_tab_selected",
        "ios_content_container_visible", "android_content_container_visible",
        "certificate_management_expanded_by_attribute", "certificate_management_expanded_by_visible_child",
        "certificate_management_expanded", "client_certificate_child_visible",
        "client_certificate_child_active", "client_certificate_child_active_semantic",
        "client_certificate_child_selected_by_style", "client_certificate_child_href_present",
        "current_path_matches_client_certificate_child", "current_path_verified_by_manual_checkpoint",
        "certificate_operation_structure_verified", "client_certificate_page_landmark_verified",
    )
    schema = {key: checkpoint.get(key) is True for key in observed_boolean_keys}
    schema["client_certificate_child_candidate_count"] = checkpoint.get("deduplicated_clickable_candidate_count", checkpoint.get("client_certificate_child_candidate_count", 0))
    schema["client_certificate_specific_landmark_count"] = checkpoint.get("client_certificate_specific_landmark_count", 0)
    schema.update({
        "target_os_ios_verified_required": True,
        "ios_tab_selected_required": True,
        "android_tab_not_selected_required": True,
        "ios_content_container_visible_required": True,
        "android_content_container_hidden_required": True,
        "deduplicated_client_child_required": True,
        "certificate_management_expanded_required": True,
        "client_certificate_child_visible_required": True,
        "client_certificate_child_active_required": True,
        "client_certificate_child_href_present_required": True,
        "current_path_matches_client_child_required": True,
        "specific_landmark_minimum": 3,
        "operation_structure_required": False,
        "manual_checkpoint_path_verified": True,
        "child_active_semantic_available": True,
        "child_href_path_match_available": True,
        "certificate_management_expanded_by_attribute_available": False,
        "certificate_management_expanded_by_visible_child_available": True,
    })
    _assert_json_safe(schema)
    return schema


def _route_manifest_from_checkpoint(checkpoint: dict[str, object]) -> dict[str, object]:
    path = str(checkpoint.get("same_host_path") or "")
    required = ("target_os_ios_verified", "ios_tab_selected", "ios_content_container_visible", "certificate_management_expanded", "client_certificate_child_visible", "client_certificate_child_active", "client_certificate_child_href_present", "current_path_matches_client_certificate_child", "client_certificate_page_landmark_verified")
    path_verified = checkpoint.get("current_path_matches_client_certificate_child") is True or checkpoint.get("current_path_verified_by_manual_checkpoint") is True
    candidate_count = checkpoint.get("deduplicated_clickable_candidate_count", checkpoint.get("client_certificate_child_candidate_count"))
    if not checkpoint.get("verified") or checkpoint.get("browser_session_valid") is not True or checkpoint.get("same_host_verified") is not True or checkpoint.get("target_os") != "ios" or not all(checkpoint.get(key) is True for key in required) or checkpoint.get("android_tab_selected") is not False or candidate_count != 1 or not path_verified or not isinstance(checkpoint.get("client_certificate_specific_landmark_count"), int) or isinstance(checkpoint.get("client_certificate_specific_landmark_count"), bool) or checkpoint.get("client_certificate_specific_landmark_count", 0) < 3 or not path.startswith("/") or path in {"", "/"} or "?" in path or "#" in path or "://" in path or re.search(r"login|signin|sign-in|android", path, re.IGNORECASE):
        raise RuntimeError("手動チェックポイントrouteを検証できません")
    schema = _manifest_schema_from_checkpoint(checkpoint)
    manifest = {
        "route_version": SMSM_ROUTE_VERSION,
        "route_type": "verified_final_same_host_path",
        "target_stage": "client_certificate_management",
        "target_os": "ios",
        "same_host_path": path,
        "same_host_path_fingerprint": _fingerprint(path),
        "landmark_schema": schema,
        "landmark_schema_fingerprint": _fingerprint(_canonical_json(schema)),
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "capture_method": "manual_checkpoint",
        "verified": True,
    }
    return manifest


def _write_route_manifest_atomic(manifest: dict[str, object]) -> None:
    path = _smsm_navigation_route_path()
    _assert_json_safe(manifest)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix="smsm_navigation_route_", suffix=".tmp", dir=path.parent)
    except Exception as exc:
        raise ManifestProcessingError("smsm_manual_checkpoint_manifest_temp_write", "manifest_temp_write_failed") from exc
    try:
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(manifest, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception as exc:
            raise ManifestProcessingError("smsm_manual_checkpoint_manifest_temp_write", "manifest_temp_write_failed") from exc
        try:
            with open(temporary_name, "r", encoding="utf-8") as stream:
                reread = json.load(stream)
        except Exception as exc:
            raise ManifestProcessingError("smsm_manual_checkpoint_manifest_temp_read", "manifest_temp_read_failed") from exc
        try:
            _validate_verified_final_manifest(reread)
        except Exception as exc:
            raise ManifestProcessingError("smsm_manual_checkpoint_manifest_validate", "manifest_validation_failed") from exc
        try:
            os.replace(temporary_name, path)
        except Exception as exc:
            raise ManifestProcessingError("smsm_manual_checkpoint_manifest_atomic_replace", "manifest_save_failed") from exc
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _validate_verified_final_manifest(payload: dict[str, object]) -> None:
    _assert_json_safe(payload)
    path = str(payload.get("same_host_path") or "")
    schema = payload.get("landmark_schema")
    allowed_keys = {"route_version", "route_type", "target_stage", "target_os", "same_host_path", "same_host_path_fingerprint", "landmark_schema", "landmark_schema_fingerprint", "captured_at", "capture_method", "verified"}
    allowed_schema_keys = {"target_os_ios_verified", "ios_tab_selected", "android_tab_selected", "ios_content_container_visible", "android_content_container_visible", "certificate_management_expanded_by_attribute", "certificate_management_expanded_by_visible_child", "certificate_management_expanded", "client_certificate_child_candidate_count", "client_certificate_child_visible", "client_certificate_child_active", "client_certificate_child_active_semantic", "client_certificate_child_selected_by_style", "client_certificate_child_href_present", "current_path_matches_client_certificate_child", "current_path_verified_by_manual_checkpoint", "certificate_operation_structure_verified", "client_certificate_page_landmark_verified", "client_certificate_specific_landmark_count", "target_os_ios_verified_required", "ios_tab_selected_required", "android_tab_not_selected_required", "ios_content_container_visible_required", "android_content_container_hidden_required", "deduplicated_client_child_required", "certificate_management_expanded_required", "client_certificate_child_visible_required", "client_certificate_child_active_required", "client_certificate_child_href_present_required", "current_path_matches_client_child_required", "specific_landmark_minimum", "operation_structure_required", "manual_checkpoint_path_verified", "child_active_semantic_available", "child_href_path_match_available", "certificate_management_expanded_by_attribute_available", "certificate_management_expanded_by_visible_child_available"}
    boolean_schema_keys = allowed_schema_keys - {"client_certificate_specific_landmark_count", "client_certificate_child_candidate_count", "specific_landmark_minimum"}
    required_true_schema_keys = {"target_os_ios_verified_required", "ios_tab_selected_required", "android_tab_not_selected_required", "ios_content_container_visible_required", "android_content_container_hidden_required", "deduplicated_client_child_required", "certificate_management_expanded_required", "client_certificate_child_visible_required", "client_certificate_child_active_required", "client_certificate_child_href_present_required", "current_path_matches_client_child_required", "manual_checkpoint_path_verified", "child_active_semantic_available", "child_href_path_match_available", "certificate_management_expanded_by_visible_child_available"}
    required_false_schema_keys = {"operation_structure_required", "certificate_management_expanded_by_attribute_available"}
    path_schema_verified = isinstance(schema, dict) and (schema.get("current_path_matches_client_certificate_child") is True or schema.get("current_path_verified_by_manual_checkpoint") is True)
    integer_schema_keys = {"specific_landmark_minimum"}
    if payload.keys() - allowed_keys or not isinstance(schema, dict) or schema.keys() - allowed_schema_keys or any(type(schema.get(key)) is not bool for key in boolean_schema_keys if key in schema) or not all(schema.get(key) is True for key in required_true_schema_keys) or not all(schema.get(key) is False for key in required_false_schema_keys) or schema.get("android_tab_selected") is not False or not path_schema_verified or any(type(schema.get(key)) is not int for key in integer_schema_keys) or schema.get("specific_landmark_minimum", 0) < 3 or type(schema.get("client_certificate_specific_landmark_count")) is not int or schema.get("client_certificate_specific_landmark_count", 0) < schema.get("specific_landmark_minimum", 3) or not isinstance(payload.get("route_version"), str) or payload.get("route_version") != SMSM_ROUTE_VERSION or not isinstance(payload.get("captured_at"), str) or payload.get("route_type") != "verified_final_same_host_path" or payload.get("target_stage") != "client_certificate_management" or payload.get("target_os") != "ios" or payload.get("verified") is not True or payload.get("capture_method") != "manual_checkpoint" or not path.startswith("/") or path in {"", "/"} or "?" in path or "#" in path or "://" in path or re.search(r"(?:^|[/_-])(?:login|signin|sign-in)(?:[/_-]|$)", path, re.IGNORECASE) or payload.get("same_host_path_fingerprint") != _fingerprint(path) or payload.get("landmark_schema_fingerprint") != _fingerprint(_canonical_json(schema)):
        raise ValueError("SMSM確認済み最終routeのmanifestが不正です")


def _load_route_manifest(trace=None) -> dict[str, object]:
    if trace is not None:
        trace("smsm_route_manifest_load_called", True)
    path = _smsm_navigation_route_path()
    if not path.is_file():
        if trace is not None:
            trace("smsm_route_manifest_found", False)
            trace("smsm_route_manifest_path_available", False)
        raise FileNotFoundError("SMSMナビゲーションrouteの採取が必要です")
    if trace is not None:
        trace("smsm_route_manifest_found", True)
        trace("smsm_route_manifest_path_available", True)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        if trace is not None:
            trace("smsm_route_manifest_parse_completed", False)
        raise ManifestProcessingError("smsm_load_client_certificate_route_manifest", "manifest_parse_failed") from exc
    if trace is not None:
        trace("smsm_route_manifest_parse_completed", True)
    if not isinstance(payload, dict):
        if trace is not None:
            trace("smsm_route_manifest_schema_valid", False)
            trace("smsm_route_manifest_fingerprint_valid", False)
        raise ValueError("SMSM確認済み最終routeのmanifestが不正です")
    try:
        _validate_verified_final_manifest(payload)
    except Exception as exc:
        if trace is not None:
            trace("smsm_route_manifest_schema_valid", False)
            trace("smsm_route_manifest_fingerprint_valid", False)
            raise ManifestProcessingError("smsm_validate_client_certificate_route_manifest", "manifest_validation_failed") from exc
        raise
    if trace is not None:
        trace("smsm_route_manifest_schema_valid", True)
        trace("smsm_route_manifest_fingerprint_valid", True)
    return payload


def _custom_search_control_dom_schema_path() -> Path:
    return _base_dir() / "logs" / f"smsm_custom_search_control_dom_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"


def _serial_input_dom_schema_path() -> Path:
    return _base_dir() / "logs" / f"smsm_serial_input_dom_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"


def _inspect_device_search_dom(driver) -> tuple[dict[str, object], list[dict[str, object]]]:
    if driver is None:
        raise RuntimeError("SMSM端末検索画面を確認できません")
    observation = enumerate_smsm_search_controls(driver)
    inputs = []
    buttons = []
    selects = observation["schema"]
    serial_option_count = observation["summary"]["serial_option_count"]
    summary = {
        "device_page_reached": True,
        "select_count": observation["native_select_count"],
        "search_type_control_count": observation["search_type_control_count"],
        "serial_option_count": serial_option_count,
        "serial_input_candidate_count": 0,
        "search_button_candidate_count": 0,
        "search_type_control_unique": observation["search_type_control_unique"],
        "serial_option_unique": serial_option_count == 1,
        "serial_input_unique": False,
        "search_button_unique": False,
    }
    return summary, selects


def _write_device_dom_schema(schema_path: Path, schema: list[dict[str, object]]) -> None:
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_serial_search_dom_schema(schema_path: Path, schema: list[dict[str, object]]) -> None:
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    allowed_keys = {
        "element_index", "document_context", "iframe_index", "tag", "id", "name", "type", "role", "data-testid",
        "displayed", "enabled", "readonly", "disabled", "option_count",
        "selected_index_present", "parent_tag", "associated_label_present",
    }
    safe_schema = [{key: item.get(key) for key in allowed_keys} for item in schema]
    schema_path.write_text(json.dumps(safe_schema, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_serial_search_results_dom_schema(schema_path: Path, observation: dict[str, object]) -> None:
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    allowed = {
        "element_index", "tag", "id_present", "name_present", "role", "data_testid_present", "class_present",
        "displayed", "enabled", "parent_tag", "ancestor_table_index", "ancestor_tbody_index", "cell_count",
        "checkbox_count", "link_count", "header_cell_count", "data_cell_count", "tbody_count", "visible_row_count",
        "data_row_count", "checkbox_row_count", "empty_state_count", "loading_count", "pagination_count", "rows",
    }
    safe_tables = []
    for table in observation.get("schema", []):
        safe_table = {key: table.get(key) for key in allowed if key != "rows"}
        safe_table["rows"] = [
            {key: row.get(key) for key in allowed if key not in {"rows", "tbody_count", "visible_row_count", "data_row_count", "checkbox_row_count", "empty_state_count", "loading_count", "pagination_count"}}
            for row in table.get("rows", [])
        ]
        safe_tables.append(safe_table)
    safe = {
        "pre_search_result_table_count": observation.get("pre_search_result_table_count", 0),
        "pre_search_tbody_count": observation.get("pre_search_tbody_count", 0),
        "pre_search_visible_row_count": observation.get("pre_search_visible_row_count", 0),
        "pre_search_checkbox_row_count": observation.get("pre_search_checkbox_row_count", 0),
        "pre_search_empty_state_count": observation.get("pre_search_empty_state_count", 0),
        "pre_search_loading_count": observation.get("pre_search_loading_count", 0),
        "pre_search_pagination_count": observation.get("pre_search_pagination_count", 0),
        "post_search_result_table_count": observation.get("result_table_count", 0),
        "post_search_tbody_count": observation.get("tbody_count", 0),
        "post_search_visible_row_count": observation.get("visible_row_count", 0),
        "result_dom_changed": bool(observation.get("result_dom_changed")),
        "result_table_unique": bool(observation.get("result_table_unique")),
        "result_rows_scoped_to_table": bool(observation.get("result_rows_scoped_to_table")),
        "lookup_result_count": observation.get("result_count", -1),
        "tables": safe_tables,
    }
    schema_path.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_result_match_schema(schema_path: Path, result: dict[str, object]) -> None:
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    allowed = {
        "result_column_count", "result_data_row_count", "serial_column_found", "serial_column_unique",
        "imei_column_found", "imei_column_unique", "alias_column_found", "alias_column_unique",
        "serial_match_count", "imei_match_count", "alias_match_count", "serial_and_imei_match_count",
        "serial_and_alias_match_count", "all_available_fields_match_count", "matched_result_count",
        "unique_result_match", "result_match_unresolved",
    }
    schema_path.write_text(json.dumps({key: result.get(key) for key in allowed}, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_client_certificate_upload_dom_schema(schema_path: Path, observation: dict[str, object]) -> None:
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    allowed = {
        "element_index", "tag", "type", "id", "name", "role", "data-testid", "accept", "autocomplete",
        "inputmode", "displayed", "enabled", "readonly", "disabled", "label_linked", "parent_tag", "form_index",
    }
    schema = [
        {key: item.get(key) for key in allowed}
        for item in observation.get("schema", [])
        if isinstance(item, dict)
    ]
    safe = {
        key: observation.get(key, 0 if key.endswith("count") else False)
        for key in (
            "settings_menu_candidate_count", "settings_menu_unique", "settings_menu_click_called", "settings_page_reached",
            "ios_menu_candidate_count", "ios_menu_unique", "ios_menu_click_called", "ios_page_reached",
            "certificate_management_candidate_count", "certificate_management_unique", "certificate_management_click_called", "certificate_management_page_reached",
            "client_certificate_management_candidate_count", "client_certificate_management_unique",
            "client_certificate_management_click_called", "client_certificate_page_reached",
        )
    }
    safe.update({
        "upload_form_count": observation.get("upload_form_count", 0),
        "upload_form_unique": bool(observation.get("upload_form_unique")),
        "file_input_count": observation.get("file_input_count", 0),
        "file_input_unique": bool(observation.get("file_input_unique")),
        "password_input_count": observation.get("password_input_count", 0),
        "password_input_unique": bool(observation.get("password_input_unique")),
        "upload_button_candidate_count": observation.get("upload_button_candidate_count", 0),
        "upload_button_unique": bool(observation.get("upload_button_unique")),
        "certificate_table_count": observation.get("certificate_table_count", 0),
        "existing_certificate_row_count": observation.get("existing_certificate_row_count", 0),
        "elements": schema,
    })
    schema_path.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")


def _smsm_client_certificate_add_form_dom_schema_path() -> Path:
    return _base_dir() / "logs" / f"smsm_client_certificate_add_form_dom_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"


def _smsm_client_certificate_add_button_dom_schema_path() -> Path:
    return _base_dir() / "logs" / f"smsm_client_certificate_add_button_dom_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"


def _write_client_certificate_add_button_dom_schema(schema_path: Path, observation: dict[str, object]) -> None:
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    allowed = {"element_index", "tag", "type", "role", "id_present", "name_present", "data_testid_present", "aria_label_present", "title_present", "displayed", "enabled", "disabled", "parent_tag", "grandparent_tag", "same_group_as_search_input", "search_common_ancestor_found", "search_common_ancestor_depth", "dropdown_common_ancestor_found", "dropdown_common_ancestor_depth", "toolbar_common_ancestor_found", "toolbar_common_ancestor_button_count", "toolbar_common_ancestor_input_count", "inside_certificate_row", "inside_pagination", "inside_destructive_region", "has_svg", "svg_path_count", "svg_use_present", "before_content_present", "after_content_present", "candidate_reason", "exclusion_reason"}
    summary_keys = ("certificate_toolbar_found", "certificate_toolbar_button_count", "search_button_candidate_count", "dropdown_button_candidate_count", "row_action_button_count", "pagination_button_count", "excluded_destructive_button_count", "add_icon_candidate_count", "add_icon_unique", "add_icon_displayed", "add_icon_enabled", "add_icon_disabled", "add_icon_inside_row", "add_icon_inside_pagination", "add_icon_inside_destructive_region", "add_icon_search_common_ancestor_found", "add_icon_dropdown_common_ancestor_found", "add_icon_toolbar_common_ancestor_found", "add_icon_toolbar_common_ancestor_button_count", "add_icon_toolbar_common_ancestor_input_count", "add_button_candidate_count", "add_button_unique", "add_button_safe", "add_button_resolution_method", "add_button_click_called")
    safe = {key: observation.get(key, "unresolved" if key == "add_button_resolution_method" else 0 if key.endswith("count") else False) for key in summary_keys}
    safe["elements"] = [{key: item.get(key) for key in allowed} for item in observation.get("elements", []) if isinstance(item, dict)]
    schema_path.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_client_certificate_add_form_dom_schema(schema_path: Path, observation: dict[str, object]) -> None:
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    allowed = {"element_index", "tag", "type", "id_present", "name_present", "role", "data_testid_present", "aria_label_present", "title_present", "accept_present", "autocomplete_present", "inputmode_present", "displayed", "enabled", "disabled", "readonly", "label_linked", "parent_tag", "form_index", "dialog_index"}
    safe = {key: observation.get(key, 0 if key.endswith("count") else False) for key in ("route_version", "route_type", "target_os", "same_host_path_fingerprint", "landmark_schema_fingerprint", "verified", "add_button_candidate_count", "add_button_unique", "add_button_safe", "add_button_click_called", "add_form_opened", "form_count", "dialog_count", "input_count", "button_count", "file_input_count", "file_input_unique", "file_input_safe", "file_input_accept_present", "file_input_label_linked", "password_input_count", "password_input_unique", "password_input_safe", "password_input_readonly", "password_input_disabled", "certificate_submit_button_candidate_count", "certificate_submit_button_unique", "certificate_submit_button_safe", "certificate_submit_button_click_called", "cancel_button_candidate_count", "cancel_button_unique", "cancel_button_click_called", "close_button_candidate_count", "close_button_unique", "close_button_click_called", "file_input_send_keys_called", "password_input_send_keys_called")}
    safe["elements"] = [{key: item.get(key) for key in allowed} for item in observation.get("schema", []) if isinstance(item, dict)]
    schema_path.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_settings_navigation_dom_schema(schema_path: Path, observation: dict[str, object]) -> None:
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    allowed = {
        "element_index", "tag", "id", "name", "role", "data-testid", "aria-label", "href_present",
        "href_path_fingerprint", "displayed", "enabled", "disabled", "parent_tag", "parent_id_present",
        "parent_class_present", "child_count", "clickable_ancestor_present", "clickable_ancestor_tag",
    }
    elements = []
    for item in observation.get("elements", []):
        if not isinstance(item, dict):
            continue
        safe = {key: item.get(key) for key in allowed}
        safe["href_present"] = bool(item.get("href_path_fingerprint"))
        elements.append(safe)
    safe_summary = {
        key: observation.get(key, False if key.endswith("found") or key.endswith("unique") else 0)
        for key in (
            "top_navigation_found", "nav_count", "header_count", "link_count", "button_count", "role_link_count",
            "role_button_count", "exact_settings_text_count", "normalized_settings_text_count",
            "settings_text_on_child_count", "settings_text_element_count", "settings_directly_clickable_count",
            "settings_clickable_parent_count", "settings_clickable_ancestor_count", "settings_candidate_count",
            "settings_candidate_unique", "navigation_resolution_ready",
            "accessible_name_source_count", "accessible_name_nonblank_count", "japanese_name_detected_count",
            "expected_name_exact_match_count",
        )
    }
    safe_summary["elements"] = elements
    schema_path.write_text(json.dumps(safe_summary, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_navigation_failure_schema(schema_path: Path, observation: dict[str, object]) -> None:
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    navigation = observation.get("navigation_observation", {}) if isinstance(observation.get("navigation_observation"), dict) else {}
    safe = {
        "failed_stage": observation.get("failed_stage"),
        "candidate_count": int(observation.get("candidate_count", 0)),
        "accessible_name_source_count": int(navigation.get("accessible_name_source_count", 0)),
        "accessible_name_nonblank_count": int(navigation.get("accessible_name_nonblank_count", 0)),
        "japanese_name_detected_count": int(navigation.get("japanese_name_detected_count", 0)),
        "expected_name_exact_match_count": int(navigation.get("expected_name_exact_match_count", 0)),
        "candidates": [
            {
                key: item.get(key)
                for key in ("element_index", "tag", "href_present", "href_path_fingerprint", "displayed", "enabled", "disabled", "accessible_name_source", "candidate_reason")
            }
            for item in observation.get("candidates", [])
            if isinstance(item, dict)
        ],
    }
    schema_path.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_certificate_workflow_diagnostic(schema_path: Path, observation: dict[str, object]) -> None:
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    navigation = observation.get("navigation_observation", {}) if isinstance(observation.get("navigation_observation"), dict) else {}
    candidates = observation.get("candidates", [])
    first = candidates[0] if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict) else {}
    safe = {
        "failed_stage": observation.get("failed_stage"),
        "resolution_method": first.get("candidate_reason", "unresolved"),
        "candidate_count": int(observation.get("candidate_count", 0)),
        "candidate_unique": int(observation.get("candidate_count", 0)) == 1,
        "landmark_verified": bool(observation.get("landmark_verified", False)),
        "document_context": navigation.get("document_context", "top"),
        "iframe_count": int(navigation.get("iframe_count", 0)),
        "shadow_root_count": int(navigation.get("shadow_root_count", 0)),
        "accessible_name_source_count": int(navigation.get("accessible_name_source_count", 0)),
        "accessible_name_nonblank_count": int(navigation.get("accessible_name_nonblank_count", 0)),
        "japanese_name_detected_count": int(navigation.get("japanese_name_detected_count", 0)),
        "expected_name_exact_match_count": int(navigation.get("expected_name_exact_match_count", 0)),
        "top_menu_group_count": int(navigation.get("top_menu_group_count", 0)),
        "top_menu_item_count": int(navigation.get("top_menu_item_count", 0)),
        "top_navigation_fallback_preconditions_valid": bool(navigation.get("top_navigation_fallback_preconditions_valid", False)),
        "href_present": bool(first.get("href_present")),
        "href_path_fingerprint": first.get("href_path_fingerprint"),
        "elapsed_ms": int(observation.get("elapsed_ms", 0)),
        "slow": bool(observation.get("slow", False)),
    }
    schema_path.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_custom_search_control_dom_schema(schema_path: Path, observation: dict[str, object]) -> None:
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    allowed_native = {
        "element_index", "tag", "id", "name", "type", "class_present", "displayed", "enabled",
        "disabled", "readonly", "aria_hidden", "option_count", "selected_index_present", "parent_tag",
        "parent_id_present", "parent_class_present",
    }
    allowed_custom = {
        "element_index", "tag", "id", "name", "role", "data-testid", "aria-haspopup", "aria-expanded",
        "aria_controls_present", "aria_labelledby_present", "tabindex_present", "class_present", "displayed",
        "enabled", "disabled", "readonly", "parent_tag", "child_count", "button_child_count",
        "listbox_child_count",
    }
    safe = {
        "native_selects": [{key: item.get(key) for key in allowed_native} for item in observation.get("native_schema", [])],
        "custom_controls": [{key: item.get(key) for key in allowed_custom} for item in observation.get("custom_schema", [])],
        "relation": {key: bool(value) for key, value in observation.get("relation", {}).items()},
        "counts": {
            key: observation.get(key, 0)
            for key in (
                "native_select_count", "hidden_native_select_count", "custom_select_candidate_count",
                "listbox_count", "option_candidate_count", "visible_option_candidate_count",
                "option_role_count", "option_data_attribute_count", "stale_retry_count",
            )
        },
    }
    schema_path.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_serial_input_dom_schema(schema_path: Path, schema: list[dict[str, object]]) -> None:
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    allowed_keys = {
        "element_index", "tag", "id", "name", "type", "role", "data-testid", "autocomplete", "inputmode",
        "maxlength_present", "pattern_present", "displayed", "enabled", "readonly", "disabled", "label_linked",
    }
    safe_schema = [{key: item.get(key) for key in allowed_keys} for item in schema]
    schema_path.write_text(json.dumps(safe_schema, ensure_ascii=False, indent=2), encoding="utf-8")


def _run_lookup(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    trace_login_only = args == ["--trace-smsm-login"]
    inspect_login_dom = args == ["--inspect-smsm-login-dom"]
    inspect_device_dom = args == ["--inspect-smsm-device-search-dom"]
    inspect_serial_search_dom = args == ["--inspect-smsm-serial-search-dom"]
    inspect_custom_search_control_dom = args == ["--inspect-smsm-custom-search-control-dom"]
    inspect_serial_input_dom = args == ["--inspect-smsm-serial-input-dom"]
    inspect_serial_search_results_dom = args == ["--inspect-smsm-serial-search-results-dom"]
    trace_serial_input = args == ["--trace-smsm-serial-input"]
    trace_serial_search = args == ["--trace-smsm-serial-search"]
    trace_result_match = args == ["--trace-smsm-result-match"]
    inspect_client_certificate_upload_dom = args == ["--inspect-smsm-client-certificate-upload-dom"]
    inspect_client_certificate_add_form_dom = args == ["--inspect-smsm-client-certificate-add-form-dom"]
    inspect_client_certificate_page_dom = args == ["--inspect-smsm-client-certificate-page"]
    manual_checkpoint_before_add = args == ["--inspect-smsm-client-certificate-add-form-dom", "--manual-checkpoint-before-add"]
    manual_checkpoint_on_add_form_failure = args == ["--inspect-smsm-client-certificate-add-form-dom", "--manual-checkpoint-on-smsm-add-form-failure"]
    inspect_client_certificate_add_form_dom = args in (
        ["--inspect-smsm-client-certificate-add-form-dom"],
        ["--inspect-smsm-client-certificate-add-form-dom", "--manual-checkpoint-before-add"],
        ["--inspect-smsm-client-certificate-add-form-dom", "--manual-checkpoint-on-smsm-add-form-failure"],
    )
    inspect_settings_navigation_dom = args == ["--inspect-smsm-settings-navigation-dom"]
    inspect_device_client_certificate_dom = args == ["--inspect-smsm-device-client-certificate-dom"]
    capture_smsm_certificate_route = args == ["--capture-smsm-certificate-navigation-route"]
    serial_search_workflow = trace_serial_input or trace_serial_search or inspect_serial_search_results_dom or trace_result_match or inspect_device_client_certificate_dom
    if args not in ([], ["--lookup"], ["--trace-smsm-login"], ["--inspect-smsm-login-dom"], ["--inspect-smsm-device-search-dom"], ["--inspect-smsm-serial-search-dom"], ["--inspect-smsm-custom-search-control-dom"], ["--inspect-smsm-serial-input-dom"], ["--inspect-smsm-serial-search-results-dom"], ["--trace-smsm-serial-input"], ["--trace-smsm-serial-search"], ["--trace-smsm-result-match"], ["--inspect-smsm-client-certificate-upload-dom"], ["--inspect-smsm-client-certificate-add-form-dom"], ["--inspect-smsm-client-certificate-page"], ["--inspect-smsm-client-certificate-add-form-dom", "--manual-checkpoint-before-add"], ["--inspect-smsm-client-certificate-add-form-dom", "--manual-checkpoint-on-smsm-add-form-failure"], ["--inspect-smsm-settings-navigation-dom"], ["--inspect-smsm-device-client-certificate-dom"], ["--capture-smsm-certificate-navigation-route"]):
        return 1

    logger = AppLogger(_base_dir())
    current_stage = "init"
    reader = None
    browser = None
    reopen_path: Path | None = None
    reopen_required = False
    reopen_attempted = False
    browser_started = False
    primary_failure = False
    result_code: int | None = None
    total_started_at = time.monotonic()

    if serial_search_workflow:
        _emit(logger, "certificate_action_called", False)
        _emit(logger, "hennge_action_called", False)
        _emit(logger, "smsm_update_called", False)
        _emit(logger, "excel_write_called", False)
        _emit(logger, "result_row_click_called", False)
    elif inspect_client_certificate_upload_dom or inspect_client_certificate_add_form_dom or inspect_client_certificate_page_dom:
        for key in (
            "certificate_action_called", "certificate_upload_called", "file_input_send_keys_called",
            "password_input_send_keys_called", "upload_button_click_called", "smsm_update_called",
            "result_row_click_called", "hennge_action_called", "excel_read_called", "excel_write_called", "add_button_click_called",
        ):
            _emit(logger, key, False)
        if inspect_client_certificate_add_form_dom:
            for key in ("certificate_submit_button_click_called", "cancel_button_click_called", "close_button_click_called"):
                _emit(logger, key, False)
    elif inspect_settings_navigation_dom or inspect_device_client_certificate_dom or capture_smsm_certificate_route:
        for key in (
            "settings_menu_click_called", "smsm_update_called", "excel_read_called", "excel_write_called",
        ):
            _emit(logger, key, False)
        if capture_smsm_certificate_route:
            for key in ("manual_checkpoint_wait_started", "manual_checkpoint_received", "browser_session_valid", "same_host_verified", "target_os_ios_verified", "client_certificate_page_landmark_verified", "navigation_route_saved"):
                _emit(logger, key, False)
    elif not inspect_custom_search_control_dom:
        _emit(logger, "lookup_mode", "read_only")
        _emit(logger, "certificate_action_called", False)
        _emit(logger, "hennge_action_called", False)
        _emit(logger, "smsm_update_called", False)
        _emit(logger, "excel_write_called", False)

    def _reopen_once() -> None:
        nonlocal reopen_attempted
        if reopen_attempted or reopen_path is None:
            return
        reopen_attempted = True
        signal = {"failed": False}

        def _capture_reopen_result(message: str) -> None:
            if "起動に失敗" in message:
                signal["failed"] = True

        command_started_at = time.monotonic()
        reopen_excel(reopen_path, _capture_reopen_result)
        _emit_elapsed(logger, "excel_reopen_command", command_started_at, EXCEL_VISIBLE_TARGET_SECONDS)
        _emit(logger, "reopen_called", True)
        _emit(logger, "reopen_command_completed", not signal["failed"])
        if signal["failed"]:
            raise ReopenFailedError("reopen failed")
        visible_started_at = time.monotonic()
        _wait_excel_window_visible(ENGINEERING_STAGE_TIMEOUT_SECONDS, lambda key, value: _emit(logger, key, value))
        _emit_elapsed(logger, "excel_window_visible", visible_started_at, EXCEL_VISIBLE_TARGET_SECONDS)
        if time.monotonic() - visible_started_at > EXCEL_VISIBLE_TARGET_SECONDS:
            _emit(logger, "excel_reopen_slow", True)

    try:
        current_stage = "config_loaded"
        config = load_config()
        smsm_config = resolve_smsm_config(config)
        config_status = _smsm_config_status(smsm_config)
        if not inspect_custom_search_control_dom and not inspect_serial_input_dom and not serial_search_workflow and not inspect_settings_navigation_dom:
            for key, value in config_status.items():
                _emit(logger, key, value)
        if not config_status["url_config_valid"]:
            current_stage = "smsm_url_validation"
            _log_failure(logger, current_stage, RuntimeError("SMSM URL validation failed"))
            return 30
        if capture_smsm_certificate_route:
            current_stage = "browser_start"
            browser = Browser(_base_dir(), config)
            try:
                browser.start()
            except Exception as exc:
                raise BrowserStartFailedError("browser start failed") from exc
            browser_started = True
            _emit(logger, "browser_started", True)
            handler = SmsmHandler(browser=browser, logger=_SilentHandlerLogger(), smsm_config=smsm_config)

            def _trace_route_capture(key: str, value) -> None:
                nonlocal current_stage
                if key.startswith("smsm_") and value:
                    current_stage = key
                _emit(logger, key, value)

            current_stage = "smsm_login"
            handler.login(_trace_route_capture)
            _emit(logger, "smsm_login_completed", True)
            current_stage = "smsm_manual_checkpoint"
            checkpoint = handler.capture_manual_certificate_checkpoint_for_diagnostic(_trace_route_capture)
            for key in (
                "target_os_ios_verified", "ios_tab_selected", "android_tab_selected",
                "ios_content_container_visible", "android_content_container_visible",
                "raw_text_match_count", "visible_text_match_count", "left_navigation_match_count", "clickable_resolution_count",
                "deduplicated_clickable_candidate_count", "hidden_match_count", "outside_navigation_match_count",
                "certificate_management_expanded", "client_certificate_child_visible",
                "client_certificate_child_active_on_self", "client_certificate_child_active_on_ancestor",
                "client_certificate_child_active", "client_certificate_child_href_present",
                "current_path_matches_client_certificate_child", "current_path_verified_by_manual_checkpoint",
                "certificate_operation_structure_verified", "client_certificate_specific_landmark_count", "client_certificate_page_landmark_verified",
            ):
                _emit(logger, key, checkpoint.get(key, False if key != "client_certificate_specific_landmark_count" else 0))
            try:
                manifest = _route_manifest_from_checkpoint(checkpoint)
            except Exception as exc:
                current_stage = "smsm_manual_checkpoint_manifest_build"
                print("navigation_route_saved=False")
                print("manual_checkpoint_failure_reason=manifest_build_failed")
                raise ManifestProcessingError(current_stage, "manifest_build_failed") from exc
            try:
                current_stage = "smsm_manual_checkpoint_manifest_validate"
                _validate_verified_final_manifest(manifest)
            except Exception as exc:
                print("navigation_route_saved=False")
                print("manual_checkpoint_failure_reason=manifest_validation_failed")
                raise ManifestProcessingError(current_stage, "manifest_validation_failed") from exc
            try:
                _write_route_manifest_atomic(manifest)
            except ManifestProcessingError as exc:
                current_stage = exc.stage
                print("navigation_route_saved=False")
                print(f"manual_checkpoint_failure_reason={exc.reason}")
                raise
            _emit(logger, "navigation_route_manifest_saved", True)
            _emit(logger, "navigation_route_saved", True)
            print("navigation_route_saved=True")
            print("manual_checkpoint_failure_reason=none")
            _emit(logger, "navigation_route_capture_required", False)
            return 0
        if inspect_settings_navigation_dom:
            current_stage = "browser_start"
            browser = Browser(_base_dir(), config)
            try:
                browser.start()
            except Exception as exc:
                raise BrowserStartFailedError("browser start failed") from exc
            browser_started = True
            _emit(logger, "browser_started", True)
            handler = SmsmHandler(browser=browser, logger=_SilentHandlerLogger(), smsm_config=smsm_config)

            def _trace_settings_navigation(key: str, value) -> None:
                nonlocal current_stage
                if key.startswith("smsm_") and value:
                    current_stage = key
                _emit(logger, key, value)

            current_stage = "smsm_login"
            try:
                handler.login(_trace_settings_navigation)
            except Exception as exc:
                raise RuntimeError("SMSM login failed") from exc
            _emit(logger, "login_completed", True)
            current_stage = "smsm_inspect_settings_navigation_dom"
            observation = handler.inspect_settings_navigation_dom_for_diagnostic(_trace_settings_navigation)
            _write_settings_navigation_dom_schema(_smsm_settings_navigation_dom_schema_path(), observation)
            _emit(logger, "settings_menu_click_called", False)
            _emit(logger, "navigation_resolution_ready", observation.get("navigation_resolution_ready", False))
            for key in (
                "accessible_name_source_count", "accessible_name_nonblank_count", "japanese_name_detected_count",
                "expected_name_exact_match_count",
            ):
                _emit(logger, key, observation.get(key, 0))
            _emit(logger, "smsm_update_called", False)
            _emit(logger, "excel_read_called", False)
            _emit(logger, "excel_write_called", False)
            return 0
        if inspect_client_certificate_upload_dom or inspect_client_certificate_add_form_dom or inspect_client_certificate_page_dom:
            current_stage = "browser_start"
            browser = Browser(_base_dir(), config)
            try:
                browser.start()
            except Exception as exc:
                raise BrowserStartFailedError("browser start failed") from exc
            browser_started = True
            _emit(logger, "browser_started", True)
            handler = SmsmHandler(browser=browser, logger=_SilentHandlerLogger(), smsm_config=smsm_config)

            def _trace_certificate_navigation(key: str, value) -> None:
                nonlocal current_stage
                if key.startswith("smsm_") and value:
                    current_stage = key
                _emit(logger, key, value)

            current_stage = "smsm_login"
            try:
                handler.login(_trace_certificate_navigation)
            except Exception as exc:
                raise RuntimeError("SMSM login failed") from exc
            _emit(logger, "smsm_login_completed", True)
            current_stage = "smsm_verified_route_manifest_loaded"
            route_load_started_at = time.monotonic()
            try:
                try:
                    route_manifest = _load_route_manifest(_trace_certificate_navigation)
                except TypeError as exc:
                    if "positional argument" not in str(exc) and "trace" not in str(exc):
                        raise
                    route_manifest = _load_route_manifest()
                    _emit(logger, "smsm_route_manifest_load_called", True)
                    _emit(logger, "smsm_route_manifest_found", True)
                    _emit(logger, "smsm_route_manifest_parse_completed", True)
                    _emit(logger, "smsm_route_manifest_schema_valid", True)
                    _emit(logger, "smsm_route_manifest_fingerprint_valid", True)
                    _emit(logger, "smsm_route_manifest_path_available", True)
                _emit(logger, "navigation_route_manifest_found", True)
                _emit(logger, "navigation_route_valid", True)
                _emit(logger, "navigation_route_fingerprint_valid", True)
                _emit(logger, "navigation_route_landmark_schema_valid", True)
                _emit(logger, "smsm_verified_route_manifest_loaded", True)
                _emit(logger, "smsm_verified_route_manifest_validated", True)
            except FileNotFoundError as exc:
                current_stage = "smsm_navigation_route_required"
                _emit(logger, "navigation_route_manifest_found", False)
                _emit(logger, "navigation_route_capture_required", True)
                error = RuntimeError("教師付きSMSMナビゲーション採取を先に実行してください")
                error.observation = {"failed_stage": current_stage, "navigation_route_manifest_found": False, "navigation_route_capture_required": True}
                raise error from exc
            except (ValueError, json.JSONDecodeError) as exc:
                current_stage = "smsm_navigation_route_invalid"
                _backup_invalid_navigation_route()
                _emit(logger, "navigation_route_manifest_found", True)
                _emit(logger, "navigation_route_valid", False)
                _emit(logger, "navigation_route_recapture_required", True)
                error = RuntimeError("SMSMナビゲーションrouteを再採取してください")
                error.observation = {"failed_stage": current_stage, "navigation_route_manifest_found": True, "navigation_route_valid": False, "navigation_route_recapture_required": True}
                raise error from exc
            _emit_elapsed(logger, "navigation_route_load", route_load_started_at, 1.0)
            verified_path_started_at = time.monotonic()
            current_stage = "smsm_verified_route_origin_resolved"
            try:
                observation = handler.navigate_verified_final_path_for_diagnostic(route_manifest, _trace_certificate_navigation)
            except RuntimeError as exc:
                failure_observation = getattr(exc, "observation", {})
                failure_stage = failure_observation.get("failed_stage") if isinstance(failure_observation, dict) else None
                wrong_target = bool(isinstance(failure_observation, dict) and failure_observation.get("navigation_get_completed") and failure_observation.get("post_navigation_path_matches_manifest") is False and failure_observation.get("client_certificate_page_landmark_verified") is False)
                if wrong_target:
                    _backup_invalid_navigation_route()
                    _emit(logger, "navigation_route_recapture_required", True)
                    _emit(logger, "failed_stage", "smsm_navigation_route_wrong_target")
                if isinstance(failure_stage, str):
                    current_stage = failure_stage
                for key in (
                    "navigation_route_manifest_found", "navigation_route_valid", "navigation_route_fingerprint_valid",
                    "navigation_route_landmark_schema_valid", "current_origin_valid", "target_url_built", "target_same_host",
                    "target_path_matches_manifest", "navigation_get_called", "navigation_get_completed", "post_navigation_same_host",
                    "post_navigation_path_matches_manifest", "post_navigation_login_page_detected", "post_navigation_redirect_detected",
                    "smsm_route_navigation_called", "smsm_route_target_built", "smsm_route_same_host", "smsm_route_get_called",
                    "smsm_route_get_completed", "smsm_route_post_path_checked", "smsm_route_post_path_matches", "smsm_route_login_page_detected",
                    "smsm_strict_page_probe_called", "smsm_strict_page_probe_completed", "smsm_strict_page_probe_exception_type",
                    "smsm_strict_page_probe_snapshot_available", "smsm_strict_page_probe_failed_phase", "smsm_strict_page_probe_javascript_error_name",
                    "smsm_settings_nav_candidate_count", "smsm_settings_nav_active", "smsm_device_nav_active",
                    "smsm_ios_settings_candidate_count", "smsm_ios_settings_active", "smsm_android_settings_active",
                    "smsm_client_certificate_menu_candidate_count", "smsm_client_certificate_menu_active",
                    "smsm_search_input_global_count", "smsm_search_input_inside_center_content_count",
                    "smsm_search_input_inside_certificate_toolbar_count", "smsm_search_input_after_exclusion_count",
                    "smsm_certificate_search_input_candidate_count", "smsm_certificate_add_icon_candidate_count", "smsm_client_certificate_page_live_verified",
                    "smsm_condition_settings_nav_unique", "smsm_condition_settings_nav_active", "smsm_condition_device_nav_inactive",
                    "smsm_condition_ios_settings_unique", "smsm_condition_ios_settings_active", "smsm_condition_android_settings_inactive",
                    "smsm_condition_client_certificate_menu_unique", "smsm_condition_client_certificate_menu_active",
                    "smsm_condition_search_input_unique", "smsm_condition_add_icon_present", "smsm_condition_pathname_matches",
                    "smsm_condition_settings_nav_consistent_if_observed", "smsm_condition_client_certificate_menu_consistent_if_observed",
                    "smsm_condition_page_specific_landmarks_verified",
                        "target_os_ios_verified", "ios_tab_candidate_count", "ios_tab_selected", "android_tab_candidate_count",
                        "android_tab_selected", "certificate_management_parent_found", "certificate_management_expanded",
                        "client_certificate_child_candidate_count", "client_certificate_child_visible", "client_certificate_child_active",
                        "client_certificate_child_href_present", "current_path_matches_client_certificate_child",
                        "client_certificate_specific_landmark_count", "client_certificate_page_landmark_verified",
                ):
                    if key in failure_observation:
                        _emit(logger, key, failure_observation[key])
                if isinstance(failure_observation, dict) and isinstance(failure_observation.get("failed_stage"), str):
                    _emit(logger, "failed_stage", failure_observation["failed_stage"])
                raise
            _emit_elapsed(logger, "verified_path_navigation", verified_path_started_at, 10.0)
            for key in (
                "navigation_route_manifest_found", "navigation_route_valid", "navigation_route_fingerprint_valid",
                "navigation_route_landmark_schema_valid", "current_origin_valid", "target_url_built", "target_same_host",
                "target_path_matches_manifest", "navigation_get_called", "navigation_get_completed", "post_navigation_same_host",
                "post_navigation_path_matches_manifest", "post_navigation_login_page_detected", "post_navigation_redirect_detected",
                "smsm_route_manifest_load_called", "smsm_route_manifest_found", "smsm_route_manifest_parse_completed",
                "smsm_route_manifest_schema_valid", "smsm_route_manifest_fingerprint_valid", "smsm_route_manifest_path_available",
                "smsm_route_navigation_called", "smsm_route_target_built", "smsm_route_same_host", "smsm_route_get_called",
                "smsm_route_get_completed", "smsm_route_post_path_checked", "smsm_route_post_path_matches", "smsm_route_login_page_detected",
                "smsm_strict_page_probe_called", "smsm_strict_page_probe_completed", "smsm_strict_page_probe_exception_type",
                "smsm_strict_page_probe_snapshot_available", "smsm_strict_page_probe_failed_phase", "smsm_strict_page_probe_javascript_error_name",
                "target_os_ios_verified", "ios_tab_candidate_count", "ios_tab_selected", "android_tab_candidate_count",
                "android_tab_selected", "ios_content_container_visible", "android_content_container_visible", "certificate_management_parent_found",
                "certificate_management_expanded_by_attribute", "certificate_management_expanded_by_visible_child", "certificate_management_expanded",
                "client_certificate_child_candidate_count", "client_certificate_child_visible", "client_certificate_child_active_semantic", "client_certificate_child_selected_by_style", "client_certificate_child_active",
                "client_certificate_child_href_present", "current_path_matches_client_certificate_child",
                "client_child_href_same_host", "client_child_href_path_nonempty", "client_child_href_has_query", "client_child_href_has_fragment", "client_child_href_source", "client_child_href_normalized_match",
                "certificate_operation_structure_verified", "client_certificate_specific_landmark_count", "client_certificate_page_landmark_verified",
                "smsm_client_certificate_page_live_verified", "smsm_settings_nav_active", "smsm_device_nav_active",
                "smsm_ios_settings_active", "smsm_android_settings_active", "smsm_client_certificate_menu_active",
                "smsm_settings_nav_candidate_count", "smsm_ios_settings_candidate_count", "smsm_client_certificate_menu_candidate_count",
                "smsm_search_input_global_count", "smsm_search_input_inside_center_content_count",
                "smsm_search_input_inside_certificate_toolbar_count", "smsm_search_input_after_exclusion_count",
                "smsm_certificate_search_input_candidate_count", "smsm_certificate_add_icon_candidate_count", "smsm_client_certificate_page_live_verified",
                "smsm_condition_settings_nav_unique", "smsm_condition_settings_nav_active", "smsm_condition_device_nav_inactive",
                "smsm_condition_ios_settings_unique", "smsm_condition_ios_settings_active", "smsm_condition_android_settings_inactive",
                "smsm_condition_client_certificate_menu_unique", "smsm_condition_client_certificate_menu_active",
                "smsm_condition_search_input_unique", "smsm_condition_add_icon_present", "smsm_condition_pathname_matches",
                "smsm_condition_settings_nav_consistent_if_observed", "smsm_condition_client_certificate_menu_consistent_if_observed",
                "smsm_condition_page_specific_landmarks_verified",
            ):
                if key in observation:
                    _emit(logger, key, observation[key])
            _emit(logger, "client_certificate_page_landmark_verified", bool(observation.get("client_certificate_page_landmark_verified", False)))
            if observation.get("client_certificate_page_landmark_verified") is not True:
                current_stage = "smsm_add_form_page_landmark_verified" if inspect_client_certificate_add_form_dom else "smsm_client_certificate_landmark"
                if inspect_client_certificate_add_form_dom:
                    _emit(logger, "add_button_resolution_blocked_by_page_mismatch", True)
                    _emit(logger, "add_button_click_called", False)
                raise RuntimeError("クライアント証明書管理画面の複合ランドマークを確認できません")
            if inspect_client_certificate_page_dom:
                live_verified = observation.get("smsm_client_certificate_page_live_verified") is True
                _emit(logger, "smsm_client_certificate_page_live_verified", live_verified)
                if not live_verified:
                    current_stage = "smsm_verify_client_certificate_page"
                    raise RuntimeError("SMSMクライアント証明書管理画面のstrict到達確認に失敗しました")
                _emit(logger, "add_button_click_called", False)
                _emit(logger, "file_input_send_keys_called", False)
                _emit(logger, "password_input_send_keys_called", False)
                _emit(logger, "certificate_upload_called", False)
                _emit(logger, "smsm_update_called", False)
                return 0
            if inspect_client_certificate_add_form_dom:
                current_stage = "smsm_find_certificate_add_button"
                if manual_checkpoint_before_add:
                    try:
                        handler.confirm_manual_client_certificate_page_for_diagnostic(route_manifest, _trace_certificate_navigation)
                    except RuntimeError:
                        _emit(logger, "add_button_resolution_blocked_by_page_mismatch", True)
                        _emit(logger, "add_button_click_called", False)
                        raise
                add_started_at = time.monotonic()
                add_button_observation = {}
                def _save_add_button_observation(value):
                    add_button_observation.update(value if isinstance(value, dict) else {})
                    _write_client_certificate_add_button_dom_schema(_smsm_client_certificate_add_button_dom_schema_path(), add_button_observation)
                try:
                    add_observation = handler.inspect_client_certificate_add_form_dom_for_diagnostic(_trace_certificate_navigation, button_schema_callback=_save_add_button_observation, click_add_button=not manual_checkpoint_before_add)
                except TypeError as exc:
                    if "button_schema_callback" not in str(exc):
                        raise
                    add_observation = handler.inspect_client_certificate_add_form_dom_for_diagnostic(_trace_certificate_navigation)
                    if add_button_observation:
                        _write_client_certificate_add_button_dom_schema(_smsm_client_certificate_add_button_dom_schema_path(), add_button_observation)
                except RuntimeError as exc:
                    failure_observation = getattr(exc, "observation", {})
                    if manual_checkpoint_on_add_form_failure and failure_observation.get("failed_stage") == "smsm_wait_certificate_add_form":
                        print("SMSMでプラスボタン押下後の右側「新規作成」パネルを確認してください。")
                        print("「証明書ファイル」「証明書を保護するパスワード」「保存」が表示されているか確認してください。")
                        print("ファイル選択、パスワード入力、保存は手動操作しないでください。")
                        input("確認後、PowerShellへ戻り空Enterを押してください。")
                        _emit(logger, "manual_checkpoint_wait_started", True)
                        try:
                            final_snapshot = handler._inspect_add_form_controls_dom(handler.browser.driver)
                            if isinstance(failure_observation, dict):
                                failure_observation = {**failure_observation, **final_snapshot, "add_form_last_snapshot_available": bool(final_snapshot)}
                            _emit(logger, "manual_checkpoint_received", True)
                        except Exception as final_probe_error:
                            if isinstance(failure_observation, dict):
                                failure_observation = {**failure_observation, "add_form_probe_exception_type": type(final_probe_error).__name__}
                            _emit(logger, "manual_checkpoint_received", False)
                        exc.observation = failure_observation
                    raise
                _emit_elapsed(logger, "add_button_resolution", add_started_at, 10.0)
                _emit_elapsed(logger, "add_form_open", add_started_at, 10.0)
                _emit_elapsed(logger, "add_form_dom_inspection", add_started_at, 10.0)
                add_observation.update({"route_version": route_manifest.get("route_version"), "route_type": route_manifest.get("route_type"), "target_os": route_manifest.get("target_os"), "same_host_path_fingerprint": route_manifest.get("same_host_path_fingerprint"), "landmark_schema_fingerprint": route_manifest.get("landmark_schema_fingerprint"), "verified": route_manifest.get("verified")})
                _write_client_certificate_add_form_dom_schema(_smsm_client_certificate_add_form_dom_schema_path(), add_observation)
                for key, value in add_observation.items():
                    if key != "schema" and isinstance(value, (bool, int)):
                        _emit(logger, key, value)
                if add_observation.get("add_button_resolution_method") in {"stable_attribute", "accessible_name", "verified_toolbar_plus_structure", "unresolved"}:
                    _emit(logger, "add_button_resolution_method", add_observation["add_button_resolution_method"])
                _emit_elapsed(logger, "client_certificate_add_form_total", route_load_started_at, 10.0)
                if manual_checkpoint_before_add:
                    for key in ("add_button_click_called", "file_input_send_keys_called", "password_input_send_keys_called", "certificate_submit_button_click_called", "certificate_upload_called", "smsm_update_called"):
                        _emit(logger, key, False)
                return 0
            landmark_started_at = time.monotonic()
            upload_started_at = time.monotonic()
            observation.update(handler.inspect_current_client_certificate_dom_for_diagnostic())
            strict_reader = getattr(handler, "_client_certificate_page_landmark_state", None)
            if callable(strict_reader):
                observation.update(strict_reader(handler.browser.driver))
            _emit_elapsed(logger, "client_certificate_landmark", landmark_started_at, 5.0)
            _emit_elapsed(logger, "upload_dom_inspection", upload_started_at, 10.0)
            _emit_elapsed(logger, "certificate_navigation_total", route_load_started_at, 10.0)
            _write_client_certificate_upload_dom_schema(_smsm_client_certificate_upload_dom_schema_path(), observation)
            for key in (
                "settings_menu_candidate_count", "settings_menu_unique", "settings_menu_click_called", "settings_page_reached",
                "ios_menu_candidate_count", "ios_menu_unique", "ios_menu_click_called", "ios_page_reached",
                "certificate_management_candidate_count", "certificate_management_unique", "certificate_management_click_called", "certificate_management_page_reached",
                "client_certificate_management_candidate_count", "client_certificate_management_unique", "client_certificate_management_click_called", "client_certificate_page_reached",
            ):
                if key not in observation:
                    continue
            for key in (
                "upload_form_count", "upload_form_unique", "file_input_count", "file_input_unique",
                "password_input_count", "password_input_unique", "upload_button_candidate_count", "upload_button_unique",
                "certificate_table_count", "existing_certificate_row_count",
            ):
                _emit(logger, key, observation.get(key, 0 if key.endswith("count") else False))
            for key in (
                "certificate_list_container_visible", "certificate_search_input_visible", "add_button_candidate_visible",
                "paging_visible", "client_certificate_child_active", "certificate_selection_guidance_visible",
                "target_os_ios_verified", "ios_tab_selected", "android_tab_selected", "certificate_management_expanded",
                "client_certificate_child_visible", "client_certificate_child_href_present", "current_path_matches_client_certificate_child",
            ):
                _emit(logger, key, bool(observation.get(key, False)))
            _emit(logger, "ios_tab_candidate_count", observation.get("ios_tab_candidate_count", 0))
            _emit(logger, "android_tab_candidate_count", observation.get("android_tab_candidate_count", 0))
            _emit(logger, "client_certificate_specific_landmark_count", observation.get("client_certificate_specific_landmark_count", 0))
            for key in (
                "upload_action_called", "file_input_send_keys_called", "password_input_send_keys_called", "smsm_update_called",
            ):
                _emit(logger, key, False)
            for key in ("upload_button_click_called", "certificate_upload_called"):
                _emit(logger, key, False)
            final_landmark_verified = bool(observation.get("client_certificate_page_landmark_verified", False))
            _emit(logger, "client_certificate_page_landmark_verified", final_landmark_verified)
            for key in (
                "final_target_os_ios_verified", "final_ios_tab_selected", "final_android_tab_selected",
                "final_certificate_management_expanded", "final_client_certificate_child_visible",
                "final_client_certificate_child_active", "final_path_matches_client_certificate_child",
                "final_client_certificate_specific_landmark_count", "final_client_certificate_landmark_verified",
            ):
                source = {
                    "final_path_matches_client_certificate_child": "current_path_matches_client_certificate_child",
                    "final_client_certificate_landmark_verified": "client_certificate_page_landmark_verified",
                }.get(key, key.removeprefix("final_"))
                _emit(logger, key, observation.get(source, 0 if source.endswith("count") else False))
            if not final_landmark_verified:
                current_stage = "smsm_inspect_client_certificate_upload_dom"
                error = RuntimeError("アップロードフォーム要素を一意に確認できません")
                error.observation = {"failed_stage": "client_certificate_management", "candidate_count": 0, "navigation_observation": observation, "candidates": []}
                raise error
            return 0
        if inspect_login_dom:
            current_stage = "browser_start"
            browser = Browser(_base_dir(), config)
            try:
                browser.start()
            except Exception as exc:
                raise BrowserStartFailedError("browser start failed") from exc
            browser_started = True
            _emit(logger, "browser_started", True)
            current_stage = "smsm_dom_open"
            browser.open(smsm_config.url)
            browser.wait_for_page_ready()
            current_stage = "smsm_dom_inspection"
            summary, inputs = _inspect_login_dom(browser.driver)
            for key, value in summary.items():
                _emit(logger, key, value)
            _write_dom_schema(_dom_schema_path(), inputs)
            if not summary["login_form_found"] or not all(summary[key] for key in (
                "company_candidate_unique", "user_candidate_unique", "password_candidate_unique",
            )):
                _log_failure(logger, current_stage, RuntimeError("SMSM login DOM is not uniquely identifiable"))
                return 30
            return 0
        if not smsm_config.valid or config_status["password_contains_powershell_syntax"]:
            current_stage = "smsm_config_validation"
            _emit(logger, "login_page_opened", False)
            _emit(logger, "user_field_found", False)
            _emit(logger, "company_field_found", False)
            _emit(logger, "password_field_found", False)
            _emit(logger, "company_and_user_fields_distinct", False)
            _emit(logger, "company_and_password_fields_distinct", False)
            _emit(logger, "user_and_password_fields_distinct", False)
            _emit(logger, "company_field_filled", False)
            _emit(logger, "user_field_filled", False)
            _emit(logger, "password_field_filled", False)
            _emit(logger, "login_button_found", False)
            _emit(logger, "login_submitted", False)
            _emit(logger, "login_submit_blocked", True)
            _emit(logger, "credential_mapping_valid", False)
            _emit(logger, "additional_auth_detected", False)
            _emit(logger, "confirmation_page_detected", False)
            _emit(logger, "redirected_window_detected", False)
            _emit(logger, "login_error_banner_detected", False)
            _emit(logger, "login_completed", False)
            _log_failure(logger, current_stage, RuntimeError("configuration validation failed"))
            return 30

        if trace_login_only:
            current_stage = "browser_start"
            browser = Browser(_base_dir(), config)
            try:
                browser.start()
            except Exception as exc:
                raise BrowserStartFailedError("browser start failed") from exc
            browser_started = True
            _emit(logger, "browser_started", True)
            handler = SmsmHandler(
                browser=browser,
                logger=_SilentHandlerLogger(),
                smsm_config=smsm_config,
            )

            def _trace_login_only(key: str, value) -> None:
                nonlocal current_stage
                if key.startswith("smsm_"):
                    current_stage = key
                _emit(logger, key, value)

            current_stage = "smsm_config_validation"
            try:
                handler.login(_trace_login_only)
            except Exception as exc:
                raise RuntimeError("SMSM login failed") from exc
            _emit(logger, "smsm_login_completed", True)
            return 0

        if inspect_device_dom or inspect_custom_search_control_dom or inspect_serial_input_dom:
            current_stage = "browser_start"
            browser = Browser(_base_dir(), config)
            try:
                browser.start()
            except Exception as exc:
                raise BrowserStartFailedError("browser start failed") from exc
            browser_started = True
            _emit(logger, "browser_started", True)
            handler = SmsmHandler(browser=browser, logger=_SilentHandlerLogger(), smsm_config=smsm_config)

            def _trace_device_login(key: str, value) -> None:
                nonlocal current_stage
                if key.startswith("smsm_") and value:
                    current_stage = key
                _emit(logger, key, value)

            current_stage = "smsm_config_validation"
            handler.login(_trace_device_login)
            _emit(logger, "smsm_login_completed", True)
            current_stage = "smsm_device_page_reached"
            handler.reach_device_search_page()
            _emit(logger, "device_page_reached", True)
            if inspect_custom_search_control_dom:
                current_stage = "smsm_wait_device_page_stable"
                try:
                    observation = handler.inspect_custom_search_control_dom(_trace_device_login)
                except RuntimeError as exc:
                    observation = getattr(exc, "observation", None) or {}
                    _write_custom_search_control_dom_schema(_custom_search_control_dom_schema_path(), observation)
                    raise
                _emit(logger, "device_page_stable", True)
                for key, value in (
                    ("native_select_count", observation["native_select_count"]),
                    ("hidden_native_select_count", observation["hidden_native_select_count"]),
                    ("custom_select_candidate_count", observation["custom_select_candidate_count"]),
                    ("custom_select_unique", observation["custom_select_unique"]),
                    ("select_backed_custom_ui_detected", observation["select_backed_custom_ui_detected"]),
                    ("select_backed_custom_ui_verified", observation["select_backed_custom_ui_verified"]),
                    ("listbox_count", observation["listbox_count"]),
                    ("option_candidate_count", observation["option_candidate_count"]),
                    ("stale_retry_count", observation["stale_retry_count"]),
                    ("custom_control_click_called", False),
                    ("option_click_called", False),
                    ("native_select_change_called", False),
                    ("send_keys_called", False),
                    ("search_button_click_called", False),
                ):
                    _emit(logger, key, value)
                _write_custom_search_control_dom_schema(_custom_search_control_dom_schema_path(), observation)
                return 0
            if inspect_serial_input_dom:
                current_stage = "smsm_wait_device_page_stable"
                result = handler.inspect_serial_input_dom(_trace_device_login)
                for key, value in (
                    ("custom_select_candidate_count", result["custom_select_candidate_count"]),
                    ("custom_select_unique", result["custom_select_unique"]),
                    ("select_backed_custom_ui_verified", result["select_backed_custom_ui_verified"]),
                    ("custom_control_click_called", True),
                    ("listbox_visible", result["listbox_visible"]),
                    ("option_candidate_count", result["option_candidate_count"]),
                    ("serial_option_candidate_count", result["serial_option_candidate_count"]),
                    ("serial_option_unique", result["serial_option_unique"]),
                    ("serial_option_click_called", True),
                    ("serial_selection_verified", result["serial_selection_verified"]),
                    ("input_count_before_selection", result["input_count_before_selection"]),
                    ("input_count_after_selection", result["input_count_after_selection"]),
                    ("serial_input_candidate_count", result["serial_input_candidate_count"]),
                    ("serial_input_unique", result["serial_input_unique"]),
                    ("send_keys_called", False),
                    ("search_button_click_called", False),
                    ("smsm_update_called", False),
                    ("excel_read_called", False),
                    ("excel_write_called", False),
                ):
                    _emit(logger, key, value)
                _write_serial_input_dom_schema(_serial_input_dom_schema_path(), result["schema"])
                return 0
            summary, schema = _inspect_device_search_dom(browser.driver)
            for key, value in summary.items():
                _emit(logger, key, value)
            _write_device_dom_schema(_device_dom_schema_path(), schema)
            return 0

        if inspect_serial_search_dom:
            current_stage = "browser_start"
            browser = Browser(_base_dir(), config)
            try:
                browser.start()
            except Exception as exc:
                raise BrowserStartFailedError("browser start failed") from exc
            browser_started = True
            _emit(logger, "browser_started", True)
            handler = SmsmHandler(browser=browser, logger=_SilentHandlerLogger(), smsm_config=smsm_config)

            def _trace_serial_search(key: str, value) -> None:
                nonlocal current_stage
                if key in {"search_type_control_count", "serial_option_count"}:
                    return
                if key.startswith("smsm_") and value:
                    current_stage = key
                _emit(logger, key, value)

            current_stage = "smsm_config_validation"
            handler.login(_trace_serial_search)
            _emit(logger, "smsm_login_completed", True)
            current_stage = "smsm_device_page_reached"
            handler.reach_device_search_page()
            _emit(logger, "device_page_reached", True)
            current_stage = "smsm_wait_search_form_dom"
            try:
                observation = handler.wait_for_search_form_dom(trace=_trace_serial_search)
            except RuntimeError as exc:
                observation = getattr(exc, "observation", None)
                if isinstance(observation, dict):
                    _write_serial_search_dom_schema(_serial_search_preselection_dom_schema_path(), observation.get("schema", []))
                else:
                    _write_serial_search_dom_schema(_serial_search_preselection_dom_schema_path(), [])
                raise
            summary = observation["summary"]
            schema = observation["schema"]
            _write_serial_search_dom_schema(_serial_search_preselection_dom_schema_path(), schema)
            _emit(logger, "top_document_select_count", observation["top_document_select_count"])
            _emit(logger, "iframe_count", observation["iframe_count"])
            _emit(logger, "iframe_with_select_count", observation["iframe_with_select_count"])
            _emit(logger, "native_select_count", observation["native_select_count"])
            _emit(logger, "visible_native_select_count", observation["visible_native_select_count"])
            _emit(logger, "enabled_native_select_count", observation["enabled_native_select_count"])
            _emit(logger, "custom_select_candidate_count", observation["custom_select_candidate_count"])
            _emit(logger, "stale_retry_count", observation["stale_retry_count"])
            _emit(logger, "search_type_control_count", observation["search_type_control_count"])
            _emit(logger, "search_type_control_unique", observation["search_type_control_unique"])
            _emit(logger, "native_select_detected", observation["native_select_count"] > 0)
            _emit(logger, "native_select_displayed", observation["visible_native_select_count"] > 0)
            _emit(logger, "custom_select_detected", observation["custom_select_candidate_count"] > 0)
            _emit(logger, "select_backed_custom_ui_detected", observation.get("select_backed_custom_ui_detected", False))
            for key, value in summary.items():
                if key != "device_page_reached":
                    _emit(logger, key, value)
            if observation["iframe_with_select_count"] > 1 or not observation["search_type_control_unique"]:
                raise RuntimeError("シリアル番号動的DOMを一意に確認できません")
            return 0

        excel_config = config.get("excel", {}) or {}
        excel_path = excel_config.get("path", "")
        if not excel_path:
            return 8
        reopen_path = Path(excel_path)
        if not reopen_path.exists():
            return 8

        current_stage = "reader_created"
        reader = ExcelReader(excel_path)
        current_stage = "file_open_check"
        target_was_open = reader.is_file_open()

        if target_was_open:
            current_stage = "workbook_detection"
            web_identity = _resolve_web_identity_hash(excel_config, "test")
            detection = detect_target_workbook(
                reopen_path.resolve(),
                configured_web_identity_hash=web_identity.hash_value,
                web_identity_source=web_identity.source,
                web_identity_valid=web_identity.valid,
                web_identity_mode="test",
                timeout_sec=15.0,
                interval_sec=0.5,
            )
            if (
                detection.workbook is None
                or detection.application is None
                or detection.matched_workbook_count != 1
            ):
                return 9

            current_stage = "save_close"
            if not save_and_close_target_workbook(
                reopen_path.resolve(),
                _SilentHandlerLogger(),
                configured_web_identity_hash=web_identity.hash_value,
                web_identity_source=web_identity.source,
                web_identity_valid=web_identity.valid,
                web_identity_mode="test",
            ):
                return 12
            reopen_required = True

            current_stage = "unlock_wait"
            _wait_unlock(reader, timeout_sec=15.0, interval_sec=0.5)

        current_stage = "read_targets"
        try:
            targets = reader.read_targets()
        except (FileNotFoundError, KeyError) as exc:
            _log_failure(logger, current_stage, exc)
            return 8
        target_count = len(targets)
        _emit(logger, "target_count", target_count)
        if target_count == 0:
            _emit(logger, "selected_target_count", 0)
            return 2

        _emit(logger, "selected_target_count", 1)
        target = targets[0]
        if not isinstance(target, dict):
            return 1
        alias = target.get("alias")
        serial = target.get("serial")
        raw_imei = target.get("imei")
        _emit(logger, "serial_present", bool(str(serial or "").strip()))
        if serial_search_workflow:
            if not str(serial or "").strip():
                return 10
        elif not str(alias or "").strip() or not str(serial or "").strip() or not str(raw_imei or "").strip():
            return 1

        if serial_search_workflow:
            serial_value = str(serial).strip()
            current_stage = "smsm_serial_input_target_loaded"
            _emit(logger, "smsm_serial_input_target_loaded", True)
            _emit(logger, "smsm_serial_input_target_validated", True)
            if trace_result_match:
                current_stage = "smsm_result_match_target_loaded"
                _emit(logger, "smsm_result_match_target_loaded", True)
                current_stage = "smsm_result_match_target_validated"
                _emit(logger, "smsm_result_match_target_validated", True)
        else:
            serial_value = str(serial).strip()

        if not serial_search_workflow:
            current_stage = "imei_normalization"
            imei = normalize_imei(raw_imei)
            if not imei:
                return 5
            _emit(logger, "imei_valid", True)

        current_stage = "browser_start"
        browser = Browser(_base_dir(), config)
        try:
            browser.start()
        except Exception as exc:
            raise BrowserStartFailedError("browser start failed") from exc
        browser_started = True
        _emit(logger, "browser_started", True)

        handler = SmsmHandler(browser=browser, logger=_SilentHandlerLogger(), smsm_config=smsm_config)
        login_started_at = time.monotonic()

        login_stage = "smsm_login"

        def _trace_login(key: str, value) -> None:
            nonlocal current_stage, login_stage
            if key.startswith("smsm_"):
                login_stage = key
                current_stage = login_stage
            _emit(logger, key, value)

        current_stage = "smsm_config_validation"
        try:
            handler.login(_trace_login)
        except Exception as exc:
            current_stage = login_stage
            raise RuntimeError("SMSM login failed") from exc
        _emit_elapsed(logger, "smsm_login", login_started_at, LOGIN_TARGET_SECONDS)
        _emit(logger, "smsm_login_completed", True)
        if trace_login_only:
            return 0

        current_stage = "smsm_device_page_reached"
        device_page_started_at = time.monotonic()
        reach_page = getattr(handler, "reach_device_search_page", None)
        if callable(reach_page):
            reach_page()
        _emit_elapsed(logger, "smsm_device_page", device_page_started_at, DEVICE_PAGE_TARGET_SECONDS)
        _emit(logger, "device_page_reached", True)
        if inspect_device_dom:
            summary, schema = _inspect_device_search_dom(browser.driver)
            for key, value in summary.items():
                _emit(logger, key, value)
            _write_device_dom_schema(_device_dom_schema_path(), schema)
            return 0

        if inspect_device_client_certificate_dom:
            current_stage = "smsm_device_client_certificate_workflow"
            workflow = handler.inspect_device_client_certificate_settings_dom_for_diagnostic(serial_value, _trace_login)
            for key in (
                "device_result_row_count", "device_detail_click_count", "other_settings_click_count", "device_client_certificate_click_count",
                "upload_form_count", "upload_form_unique", "file_input_count", "file_input_unique",
                "password_input_count", "password_input_unique", "upload_button_candidate_count", "upload_button_unique",
                "certificate_table_count", "existing_certificate_row_count",
            ):
                _emit(logger, key, workflow.get(key, 0 if key.endswith("count") else False))
            for key in ("certificate_action_called", "certificate_upload_called", "file_input_send_keys_called", "password_input_send_keys_called", "upload_button_click_called", "smsm_update_called", "hennge_action_called", "excel_write_called"):
                _emit(logger, key, False)
            return 0

        if serial_search_workflow:
            current_stage = "smsm_wait_device_page_stable"
            serial_option_started_at = time.monotonic()
            try:
                result = handler.inspect_serial_input_dom(_trace_login)
                _emit_elapsed(logger, "smsm_serial_option", serial_option_started_at, SERIAL_OPTION_TARGET_SECONDS)
                serial_input_started_at = time.monotonic()
                input_result = handler.fill_serial_input_for_diagnostic(serial_value, _trace_login)
                _emit_elapsed(logger, "smsm_serial_input", serial_input_started_at, SERIAL_INPUT_TARGET_SECONDS)
            except RuntimeError:
                _write_serial_input_dom_schema(_serial_input_dom_schema_path(), result.get("schema", []) if "result" in locals() and isinstance(result, dict) else [])
                raise
            current_stage = "smsm_serial_input_result_validation"
            _validate_serial_input_result(input_result)
            for key, value in (
                ("custom_select_candidate_count", result["custom_select_candidate_count"]),
                ("custom_select_unique", result["custom_select_unique"]),
                ("select_backed_custom_ui_verified", result["select_backed_custom_ui_verified"]),
                ("custom_control_click_called", True),
                ("listbox_visible", result["listbox_visible"]),
                ("option_candidate_count", result["option_candidate_count"]),
                ("serial_option_candidate_count", result["serial_option_candidate_count"]),
                ("serial_option_unique", result["serial_option_unique"]),
                ("serial_option_click_called", True),
                ("serial_selection_verified", result["serial_selection_verified"]),
                ("input_count_before_selection", result["input_count_before_selection"]),
                ("input_count_after_selection", result["input_count_after_selection"]),
                ("serial_input_candidate_count", input_result.get("serial_input_candidate_count")),
                ("serial_input_unique", input_result.get("serial_input_unique")),
                ("serial_input_clear_called", input_result.get("serial_input_clear_called")),
                ("serial_input_send_keys_called", input_result.get("serial_input_send_keys_called")),
                ("search_button_click_called", False),
                ("smsm_update_called", False),
                ("certificate_action_called", False),
                ("hennge_action_called", False),
                ("excel_write_called", False),
            ):
                _emit(logger, key, value)
            for key in SERIAL_INPUT_REQUIRED_KEYS:
                if key not in {
                    "serial_input_candidate_count", "serial_input_unique",
                    "serial_input_clear_called", "serial_input_send_keys_called",
                    "search_button_click_called", "smsm_update_called", "excel_write_called",
                }:
                    _emit(logger, key, input_result.get(key))
            _write_serial_input_dom_schema(_serial_input_dom_schema_path(), result.get("schema", []))
            current_stage = "smsm_serial_input_logging"
            _emit(logger, "smsm_serial_input_completed", True)
            if trace_result_match:
                current_stage = "smsm_result_match_search"
                match_result = handler.match_serial_search_results_for_diagnostic(target, _trace_login)
                _write_result_match_schema(_smsm_result_match_schema_path(), match_result)
                result_count = match_result.get("matched_result_count", 0)
                if match_result.get("result_match_unresolved"):
                    result_code = 31
                elif result_count == 0:
                    result_code = 32
                elif result_count == 1:
                    result_code = 0
                else:
                    result_code = 33
                for key, value in (
                    ("result_row_click_called", False),
                    ("result_detail_opened", False),
                    ("smsm_update_called", False),
                    ("certificate_action_called", False),
                    ("hennge_action_called", False),
                    ("excel_write_called", False),
                ):
                    _emit(logger, key, value)
                return result_code
            if inspect_serial_search_results_dom:
                current_stage = "smsm_serial_search_results_dom"
                observation = handler.inspect_serial_search_results_dom_for_diagnostic(_trace_login)
                _write_serial_search_results_dom_schema(_serial_search_results_dom_schema_path(), observation)
                result_count = observation["result_count"]
                if result_count == 0:
                    result_code = 32
                elif result_count == 1:
                    result_code = 0
                elif result_count > 1:
                    result_code = 33
                else:
                    result_code = 31
                _emit(logger, "result_row_click_called", False)
                _emit(logger, "smsm_update_called", False)
                _emit(logger, "certificate_action_called", False)
                _emit(logger, "hennge_action_called", False)
                _emit(logger, "excel_write_called", False)
                return result_code
            if trace_serial_search:
                current_stage = "smsm_serial_search_target_loaded"
                _emit(logger, "smsm_serial_search_target_loaded", True)
                current_stage = "smsm_serial_search_target_validated"
                _emit(logger, "smsm_serial_search_target_validated", True)
                current_stage = "smsm_device_page_reached"
                _emit(logger, "smsm_device_page_reached", True)
                current_stage = "smsm_select_serial_option"
                _emit(logger, "smsm_select_serial_option", True)
                current_stage = "smsm_validate_serial_selection"
                _emit(logger, "smsm_validate_serial_selection", True)
                current_stage = "smsm_find_serial_input"
                _emit(logger, "smsm_find_serial_input", True)
                current_stage = "smsm_fill_serial_input"
                _emit(logger, "smsm_fill_serial_input", True)
                current_stage = "smsm_validate_serial_input"
                _emit(logger, "smsm_validate_serial_input", True)
                current_stage = "smsm_find_search_button"
                observation = handler.inspect_serial_search_results_dom_for_diagnostic(_trace_login)
                current_stage = "smsm_count_search_results"
                _emit(logger, "smsm_count_search_results", True)
                result_count = observation["result_count"]
                result_code = 0 if result_count == 1 else 32 if result_count == 0 else 33
                _emit(logger, "smsm_serial_search_completed", result_count == 1)
                return result_code
            current_stage = "smsm_serial_input_exit_code_resolution"
            return 0

        def _trace_search(key: str, value) -> None:
            nonlocal current_stage
            if key.startswith("smsm_") and value:
                current_stage = key
            _emit(logger, key, value)

        current_stage = "smsm_find_search_type_control"
        search_device = getattr(handler, "search_device", None)
        if callable(search_device):
            search_device(str(serial).strip(), trace=_trace_search, page_reached=True)
        else:
            handler.search_device_by_imei(str(serial).strip())
        _emit(logger, "lookup_called", True)

        current_stage = "lookup_result_wait"
        _emit(logger, "smsm_count_search_results", True)
        result_count = handler.count_visible_device_results()
        _emit(logger, "lookup_result_count", result_count)
        if result_count == 0:
            _emit(logger, "lookup_unique", False)
            return 32
        if result_count > 1:
            _emit(logger, "lookup_unique", False)
            return 33
        _emit(logger, "lookup_unique", True)
        return 0
    except KeyboardInterrupt:
        primary_failure = True
        _log_failure(logger, current_stage, KeyboardInterrupt())
        return 130
    except BrowserStartFailedError as exc:
        primary_failure = True
        _log_failure(logger, current_stage, exc)
        return 34
    except BrowserQuitFailedError as exc:
        primary_failure = True
        _log_failure(logger, "browser_quit", exc)
        return 35
    except UnlockTimeoutError as exc:
        primary_failure = True
        _log_failure(logger, current_stage, exc)
        return 13
    except (ReadOnlyWorkbookError, SaveCloseWorkbookError) as exc:
        primary_failure = True
        _log_failure(logger, current_stage, exc)
        return 12
    except ResultContractError as exc:
        primary_failure = True
        _log_failure(logger, current_stage, exc)
        return 31
    except ManifestProcessingError as exc:
        primary_failure = True
        _log_failure(logger, exc.stage, exc)
        return 31
    except RuntimeError as exc:
        primary_failure = True
        failure_observation = getattr(exc, "observation", None)
        if isinstance(failure_observation, dict) and isinstance(failure_observation.get("failed_stage"), str):
            current_stage = failure_observation["failed_stage"]
        if inspect_client_certificate_add_form_dom and isinstance(failure_observation, dict):
            for key in (
                "add_form_probe_called", "add_form_probe_completed", "add_form_probe_exception_type",
                "add_form_probe_iteration_count", "add_form_last_snapshot_available",
                "add_form_probe_failed_phase", "add_form_probe_javascript_error_name",
                "add_form_probe_snapshot_before_failure_available",
                "add_form_probe_phase", "add_form_probe_completed_phases",
                "top_document_iframe_count", "visible_iframe_count", "same_origin_iframe_count",
                "cross_origin_iframe_count", "open_shadow_root_host_count", "shadow_root_file_input_count",
                "shadow_root_password_input_count", "shadow_root_save_button_count", "top_document_file_input_count",
                "top_document_password_input_count", "top_document_text_input_count", "top_document_button_count",
                "top_document_submit_input_count", "right_side_container_candidate_count",
                "right_side_visible_container_count", "file_input_dom_count", "file_input_enabled_count",
                "password_input_dom_count", "password_input_visible_count", "save_button_dom_count",
                "save_button_visible_count", "upload_controls_common_ancestor_count", "add_form_resolution_method",
                "add_form_opened", "password_input_global_candidate_count",
                "password_input_inside_right_panel_count", "password_label_candidate_count",
                "password_label_associated_input_count", "password_input_after_type_filter_count",
                "password_input_after_exclusion_count", "password_input_after_visibility_count",
                "password_input_count", "password_input_unique", "password_input_resolution_method",
            ):
                if key in failure_observation:
                    _emit(logger, key, failure_observation[key])
        if inspect_client_certificate_upload_dom and isinstance(failure_observation, dict):
            failure_stage = failure_observation.get("failed_stage")
            if isinstance(failure_stage, str):
                try:
                    _write_certificate_workflow_diagnostic(_smsm_certificate_workflow_diagnostic_path(), failure_observation)
                except Exception:
                    _emit(logger, "navigation_failure_schema_save_failed", True)
        _log_failure(logger, current_stage, exc)
        search_stages = {
            "smsm_inspect_settings_navigation_dom",
            "smsm_navigation_route_required",
            "smsm_navigation_route_invalid",
            "smsm_capture_certificate_navigation_route",
            "smsm_manual_checkpoint",
            "smsm_client_certificate_landmark",
            "smsm_verified_route_manifest_loaded",
            "smsm_verified_route_manifest_validated",
            "smsm_verified_route_origin_resolved",
            "smsm_verified_route_target_built",
            "smsm_verified_route_navigation_started",
            "smsm_verified_route_navigation_command_completed",
            "smsm_verified_route_post_navigation_url_checked",
            "smsm_verified_route_landmark_checked",
            "smsm_verified_route_page_reached",
            "smsm_add_form_route_loaded",
            "smsm_add_form_route_validated",
            "smsm_add_form_page_reached",
            "smsm_add_form_page_landmark_verified",
            "smsm_find_certificate_add_button",
            "smsm_validate_certificate_add_button",
            "smsm_click_certificate_add_button",
            "smsm_wait_certificate_add_form",
            "smsm_inspect_certificate_file_input",
            "smsm_inspect_certificate_password_input",
            "smsm_inspect_certificate_submit_button",
            "smsm_inspect_certificate_cancel_controls",
            "smsm_client_certificate_add_form_dom_completed",
            "smsm_certificate_navigation_started",
            "smsm_find_settings_menu",
            "smsm_open_settings_menu",
            "smsm_wait_settings_page",
            "smsm_find_ios_menu",
            "smsm_open_ios_menu",
            "smsm_wait_ios_page",
            "smsm_find_certificate_management",
            "smsm_open_certificate_management",
            "smsm_wait_certificate_management_page",
            "smsm_find_client_certificate_management",
            "smsm_open_client_certificate_management",
            "smsm_wait_client_certificate_page",
            "smsm_inspect_client_certificate_upload_dom",
            "smsm_certificate_navigation_completed",
            "smsm_device_page_reached",
            "smsm_result_match_target_loaded",
            "smsm_result_match_target_validated",
            "smsm_result_match_search",
            "smsm_find_result_table",
            "smsm_inspect_result_headers",
            "smsm_map_result_columns",
            "smsm_read_result_cells_in_memory",
            "smsm_compare_result_rows",
            "smsm_resolve_unique_result",
            "smsm_result_match_completed",
            "smsm_serial_search_target_loaded",
            "smsm_serial_search_target_validated",
            "smsm_wait_device_page_stable",
            "smsm_wait_search_form_dom",
            "smsm_scan_top_document",
            "smsm_scan_iframes",
            "smsm_detect_hidden_native_select",
            "smsm_detect_custom_select_control",
            "smsm_inspect_custom_select_control",
            "smsm_validate_select_backing_relation",
            "smsm_find_custom_serial_option",
            "smsm_select_custom_serial_option",
            "smsm_find_custom_search_control",
            "smsm_open_custom_search_control",
            "smsm_wait_custom_listbox",
            "smsm_validate_custom_serial_selection",
            "smsm_wait_serial_input_dom",
            "smsm_inspect_serial_input_dom",
            "smsm_serial_input_target_loaded",
            "smsm_serial_input_target_validated",
            "smsm_clear_serial_input",
            "smsm_fill_serial_input",
            "smsm_validate_serial_input",
            "smsm_serial_input_completed",
            "smsm_detect_native_select",
            "smsm_detect_custom_select",
            "smsm_validate_search_type_control",
            "smsm_find_search_type_control",
            "smsm_open_search_type_control",
            "smsm_find_serial_option",
            "smsm_select_serial_option",
            "smsm_validate_serial_selection",
            "smsm_find_serial_input",
            "smsm_fill_serial_input",
            "smsm_validate_serial_input",
            "smsm_find_search_button",
            "smsm_submit_search",
            "smsm_wait_search_results",
            "smsm_count_search_results",
        }
        if current_stage in search_stages:
            return 31
        if current_stage == "smsm_login" or current_stage.startswith("smsm_"):
            return 30
        return 1
    except ValueError as exc:
        primary_failure = True
        _log_failure(logger, current_stage, exc)
        return 5 if current_stage == "imei_normalization" else 1
    except FileNotFoundError as exc:
        primary_failure = True
        _log_failure(logger, current_stage, exc)
        return 8
    except Exception as exc:
        primary_failure = True
        _log_failure(logger, current_stage, exc)
        return 1
    finally:
        browser_quit_failure = False
        if browser_started and browser is not None:
            try:
                quit_diagnostic = getattr(browser, "quit_diagnostic", None)
                if callable(quit_diagnostic):
                    quit_result = quit_diagnostic(lambda key, value: _emit(logger, key, value))
                    browser_quit_failure = not bool(quit_result.get("completed", False))
                    if quit_result.get("elapsed_ms", 0) > BROWSER_QUIT_TARGET_SECONDS * 1000:
                        _emit(logger, "browser_quit_slow", True)
                else:
                    _emit(logger, "browser_quit_started", True)
                    _emit(logger, "browser_quit_called", True)
                    _emit(logger, "browser_quit_command_started", True)
                    browser.quit()
                    _emit(logger, "browser_quit_command_completed", True)
                    _emit(logger, "browser_quit_command_timed_out", False)
                    _emit(logger, "browser_quit_cleanup_started", True)
                    _emit(logger, "browser_quit_cleanup_completed", True)
                    _emit(logger, "browser_quit_finished", True)
                    _emit(logger, "browser_quit_completed", True)
                    _emit(logger, "browser_quit_timed_out", False)
                    _emit(logger, "browser_session_already_closed", False)
            except Exception as exc:
                browser_quit_failure = True
                _emit(logger, "browser_quit_started", True)
                _emit(logger, "browser_quit_called", True)
                _emit(logger, "browser_quit_completed", False)
                _emit(logger, "browser_quit_timed_out", isinstance(exc, (TimeoutError, socket.timeout)))
                _emit(logger, "browser_session_already_closed", False)
                _emit(logger, "browser_quit_exception_type", type(exc).__name__)
        if reopen_required and not reopen_attempted:
            _reopen_once()
        _emit_elapsed(logger, "total", total_started_at, float("inf"))
        if browser_quit_failure and not primary_failure and result_code is None:
            raise BrowserQuitFailedError("browser quit failed")


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if "--verify-smsm-device-detail-only" in args:
        return _run_smsm_device_detail_only(args)
    if "--inspect-smsm-client-certificate-navigation-only" in args:
        return _run_smsm_client_certificate_navigation_only(args)
    if "--inspect-smsm-client-certificate-edit-form-only" in args:
        return _run_smsm_client_certificate_edit_form_only(args)
    if "--inspect-smsm-client-certificate-primary-input-only" in args:
        return _run_smsm_client_certificate_primary_input_only(args)
    if "--inspect-smsm-client-certificate-imei-input-only" in args:
        return _run_smsm_client_certificate_imei_input_only(args)
    if "--inspect-matched-device-result-links" in args:
        return _run_matched_device_result_link_inspection(args)
    if "--smsm-login-only" in args:
        return _run_smsm_login_only()
    if (
        "--help" in args
        or "--run-single-certificate-workflow" in args
        or "--prepare-smsm-certificate-upload" in args
        or "--bind-existing-smsm-certificate" in args
    ):
        return _run_single_certificate_workflow_cli(args)
    try:
        return _run_lookup(args)
    except BrowserQuitFailedError:
        return 35
    except ReopenFailedError:
        return 14


def _run_smsm_client_certificate_navigation_only(args: list[str]) -> int:
    """Inspect client-certificate navigation and stop before edit or mutation."""
    logger = AppLogger(_base_dir(), unique_log=True)
    browser = None
    observations = {
        "client_certificate_navigation_only": True,
        "client_certificate_edit_click_called": False, "client_certificate_edit_click_count": 0,
        "device_imei_send_keys_called": False, "device_imei_send_keys_count": 0,
        "device_binding_save_called": False, "device_binding_save_count": 0,
        "excel_write_called": False, "certificate_upload_called": False,
        "browser_start_called": False,
    }
    forbidden = {"--bind-existing-smsm-certificate", "--allow-device-binding", "--allow-excel-write", "--allow-certificate-upload"}
    if args != ["--inspect-smsm-client-certificate-navigation-only"] or any(item in forbidden for item in args):
        observations.update({"failed_stage": "cli_flag_validation", "exception_type": "ArgumentError"})
        for key, value in observations.items():
            _emit(logger, key, value)
            print(f"{key}={value}")
        return 2
    try:
        config = load_config()
        excel_path = str((config.get("excel", {}) or {}).get("path", ""))
        targets = ExcelReader(excel_path).read_targets(include_row_number=True)
        if not targets:
            raise RuntimeError("有効なExcel対象がありません")
        context = WorkflowContext()
        context.config = config
        context.set_target(targets[0])
        browser = Browser(_base_dir(), config)
        browser.start()
        observations["browser_start_called"] = True
        service = ProductionWorkflowService(config=config, logger=logger, browser=browser, smsm_config=resolve_smsm_config(config))
        service.smsm_login(context)
        service.smsm_open_device_list(context)
        service.smsm_search_device_by_serial(context, read_only=True)
        result = service.smsm.inspect_client_certificate_navigation_only(context.target_serial)
        observations.update(context.observations)
        observations.update(result)
        observations.pop("device_detail_panel", None)
        observations.update({"client_certificate_edit_click_called": False, "client_certificate_edit_click_count": 0, "device_imei_send_keys_called": False, "device_imei_send_keys_count": 0, "device_binding_save_called": False, "device_binding_save_count": 0, "excel_write_called": False, "certificate_upload_called": False})
        success = all((
            observations.get("device_search_identity_context_verified") is True,
            observations.get("device_result_identity_verified") is True,
            observations.get("other_settings_unique") is True,
            observations.get("other_settings_click_count") == 1,
            observations.get("device_settings_panel_unique") is True,
            observations.get("client_certificate_item_unique") is True,
            observations.get("client_certificate_item_click_count") == 1,
            observations.get("client_certificate_panel_unique") is True,
            (observations.get("client_certificate_unconfigured_state_detected") is True or observations.get("client_certificate_existing_value_detected") is True),
            observations.get("client_certificate_edit_unique") is True,
            observations.get("client_certificate_edit_displayed") is True,
            observations.get("client_certificate_edit_enabled") is True,
            observations.get("client_certificate_edit_click_called") is False,
        ))
        observations["failed_stage"] = "" if success else "client_certificate_navigation_only"
        observations["exception_type"] = ""
        for key, value in observations.items():
            _emit(logger, key, value)
            print(f"{key}={value}")
        return 0 if success else 1
    except Exception as exc:
        observations.update({"failed_stage": "client_certificate_navigation_only", "exception_type": type(exc).__name__})
        for key, value in observations.items():
            _emit(logger, key, value)
            print(f"{key}={value}")
        return 1
    finally:
        if browser is not None:
            try:
                browser.quit()
            except Exception:
                pass


def _runtime_source_diagnostics() -> dict[str, object]:
    project_root = Path(__file__).resolve().parent
    expected = {
        "diagnostic_script": project_root / "diagnose_smsm_single_target_lookup.py",
        "smsm_handler": project_root / "app" / "smsm_handler.py",
        "workflow_service": project_root / "app" / "workflow_service.py",
    }
    modules = {
        "diagnostic_script": sys.modules[__name__],
        "smsm_handler": sys.modules.get("app.smsm_handler"),
        "workflow_service": sys.modules.get("app.workflow_service"),
    }
    result = {}
    for name, expected_path in expected.items():
        module = modules[name]
        actual_path = Path(getattr(module, "__file__", "")).resolve() if module is not None and getattr(module, "__file__", None) else None
        path_key = "diagnostic_script_path_matches_expected" if name == "diagnostic_script" else f"{name}_module_path_matches_expected"
        result[path_key] = actual_path == expected_path
        try:
            digest = hashlib.sha256(expected_path.read_bytes()).hexdigest()[:12]
        except OSError:
            digest = ""
        result[f"{name}_source_fingerprint"] = digest
    return result


def _safe_name_error_name(exc: BaseException) -> tuple[bool, str]:
    name = getattr(exc, "name", None)
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return False, ""
    if any(token in name.casefold() for token in ("password", "serial", "imei", "token", "secret", "username")):
        return False, ""
    return True, name


def _name_error_diagnostics(exc: BaseException, project_root: Path) -> dict[str, object]:
    is_name_error = isinstance(exc, NameError)
    name_present, safe_name = _safe_name_error_name(exc) if is_name_error else (False, "")
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ is not None else []
    project_frames = [frame for frame in frames if Path(frame.filename).resolve().is_relative_to(project_root.resolve())]
    frame = project_frames[-1] if project_frames else None
    source_file_class = "unresolved"
    if frame is not None:
        path = Path(frame.filename).resolve()
        if path.name == "smsm_handler.py":
            source_file_class = "smsm_handler"
        elif path.name == "workflow_service.py":
            source_file_class = "workflow_service"
        elif path.name == "browser.py":
            source_file_class = "browser_helper"
        elif path.name == "diagnose_smsm_single_target_lookup.py":
            source_file_class = "diagnostic_cli"
        else:
            source_file_class = "other_project_file"
    return {
        "exception_name_present": name_present,
        "exception_name_safe": safe_name,
        "exception_name_class": "NameError" if is_name_error else "",
        "exception_source_file_class": source_file_class,
        "exception_source_function": frame.name if frame is not None else "",
        "exception_source_line_number": frame.lineno if frame is not None else 0,
        "exception_source_line_present": frame is not None,
        "exception_source_inside_project": frame is not None,
        "exception_source_inside_smsm_handler": source_file_class == "smsm_handler",
        "exception_source_inside_workflow_service": source_file_class == "workflow_service",
        "exception_source_inside_diagnostic_cli": source_file_class == "diagnostic_cli",
    }


def _key_error_diagnostics(exc: BaseException) -> dict[str, object]:
    if not isinstance(exc, KeyError):
        return {"exception_key_present": False, "exception_key_safe": "", "exception_key_class": ""}
    key = exc.args[0] if exc.args else None
    safe = key if isinstance(key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) else ""
    return {"exception_key_present": key is not None, "exception_key_safe": safe, "exception_key_class": "KeyError"}


def _client_certificate_edit_form_success(result: dict[str, object], operations: dict[str, object]) -> bool:
    return all((
        result.get("device_result_identity_verified") is True,
        result.get("other_settings_click_count") == 1,
        result.get("client_certificate_item_click_count") == 1,
        result.get("client_certificate_edit_transition_detected") is True,
        result.get("client_certificate_edit_marker_wait_completed") is True,
        result.get("client_certificate_edit_marker_last_snapshot_available") is True,
        result.get("client_certificate_after_snapshot_created") is True,
        result.get("client_certificate_after_snapshot_uses_current_classification") is True,
        result.get("client_certificate_after_snapshot_uses_before_fallback") is False,
        result.get("client_certificate_after_snapshot_metrics_consistent") is True,
        result.get("client_certificate_edit_control_presence_verified") is True,
        result.get("client_certificate_after_control_element_count", 0) >= 1,
        result.get("client_certificate_save_candidate_count") == 1,
        result.get("client_certificate_cancel_candidate_count") == 1,
        result.get("client_certificate_edit_click_count") == 1,
        operations.get("client_certificate_selection_control_click_called") is False,
        operations.get("client_certificate_option_selection_called") is False,
        operations.get("device_imei_send_keys_called") is False,
        operations.get("device_binding_save_called") is False,
        operations.get("client_certificate_cancel_click_called") is False,
        operations.get("excel_write_called") is False,
        operations.get("certificate_upload_called") is False,
    ))


def _run_smsm_client_certificate_edit_form_only(args: list[str]) -> int:
    """Inspect the certificate edit form without selecting or saving."""
    logger = AppLogger(_base_dir(), unique_log=True)
    browser = None
    current_stage = "client_certificate_edit_form_only_cli_validation"
    last_completed_stage = ""
    operations = {
        **_runtime_source_diagnostics(),
        "client_certificate_selection_control_click_called": False,
        "client_certificate_selection_control_click_count": 0,
        "client_certificate_option_selection_called": False,
        "client_certificate_option_selection_count": 0,
        "device_imei_send_keys_called": False,
        "device_imei_send_keys_count": 0,
        "device_binding_save_called": False,
        "device_binding_save_count": 0,
        "client_certificate_cancel_click_called": False,
        "excel_write_called": False,
        "certificate_upload_called": False,
        "browser_start_called": False,
    }
    forbidden = {
        "--bind-existing-smsm-certificate", "--allow-device-binding", "--allow-excel-write",
        "--allow-certificate-upload", "--inspect-smsm-client-certificate-navigation-only",
        "--verify-smsm-device-detail-only",
    }
    if args != ["--inspect-smsm-client-certificate-edit-form-only"] or any(flag in forbidden for flag in args):
        for key, value in {**operations, "failed_stage": "cli_flag_validation", "exception_type": "ArgumentError"}.items():
            _emit(logger, key, value)
            print(f"{key}={value}")
        return 2
    try:
        def advance(stage: str) -> None:
            nonlocal current_stage, last_completed_stage
            current_stage = stage

        def complete() -> None:
            nonlocal last_completed_stage
            last_completed_stage = current_stage

        advance("client_certificate_edit_form_only_load_target")
        config = load_config()
        excel_path = str((config.get("excel", {}) or {}).get("path", ""))
        targets = ExcelReader(excel_path).read_targets(include_row_number=True)
        if not targets:
            raise RuntimeError("有効なExcel対象がありません")
        context = WorkflowContext()
        context.config = config
        context.set_target(targets[0])
        context.record("excel_target_count", len(targets))
        complete()
        advance("client_certificate_edit_form_only_resolve_credentials")
        smsm_config = resolve_smsm_config(config)
        complete()
        advance("client_certificate_edit_form_only_start_browser")
        browser = Browser(_base_dir(), config)
        browser.start()
        operations["browser_start_called"] = True
        complete()
        service = ProductionWorkflowService(config=config, logger=logger, browser=browser, smsm_config=smsm_config)
        advance("client_certificate_edit_form_only_login")
        service.smsm_login(context)
        complete()
        advance("client_certificate_edit_form_only_open_device_list")
        service.smsm_open_device_list(context)
        complete()
        advance("client_certificate_edit_form_only_search_device")
        service.smsm_search_device_by_serial(context, read_only=True)
        complete()
        advance("client_certificate_edit_form_only_select_device")
        def trace_edit_form(key, value):
            nonlocal current_stage
            if value is not True:
                return
            stage_by_key = {
                "other_settings_click_called": "client_certificate_edit_form_only_open_other_settings",
                "device_client_certificate_click_called": "client_certificate_edit_form_only_open_certificate_item",
                "client_certificate_state_wait_called": "client_certificate_edit_form_only_wait_view_state",
                "client_certificate_edit_click_started": "client_certificate_edit_form_only_click_edit",
                "client_certificate_edit_click_called": "client_certificate_edit_form_only_click_edit",
                "client_certificate_edit_state_wait_called": "client_certificate_edit_form_only_wait_edit_state",
                "client_certificate_edit_marker_wait_called": "client_certificate_edit_form_only_wait_edit_state",
            }
            if key in stage_by_key:
                current_stage = stage_by_key[key]

        result = service.smsm.inspect_client_certificate_edit_form_only(context.target_serial, trace=trace_edit_form)
        if result.get("client_certificate_edit_click_completed") is True:
            last_completed_stage = "client_certificate_edit_form_only_click_edit"
            if result.get("client_certificate_edit_marker_wait_called") is True:
                current_stage = "client_certificate_edit_form_only_wait_edit_state"
                if result.get("client_certificate_edit_marker_wait_completed") is True:
                    last_completed_stage = "client_certificate_edit_form_only_wait_edit_state"
            elif result.get("client_certificate_edit_form_wait_called") is True:
                current_stage = "client_certificate_edit_form_only_wait_edit_state"
                if result.get("client_certificate_edit_form_wait_completed") is True:
                    last_completed_stage = "client_certificate_edit_form_only_wait_edit_state"
        else:
            complete()
        result.pop("client_certificate_panel", None)
        result.update(operations)
        success = _client_certificate_edit_form_success(result, operations)
        result["failed_stage"] = "" if success else current_stage
        result["last_completed_stage"] = last_completed_stage
        result["exception_type"] = ""
        internal_keys = {"panel", "device_detail_panel", "client_certificate_panel", "edit_form"}
        for key, value in result.items():
            if key in internal_keys:
                continue
            safe_value = _safe_public_diagnostic_value(value)
            if safe_value is None and value is not None:
                continue
            _emit(logger, key, safe_value)
            print(f"{key}={safe_value}")
        return 0 if success else 1
    except Exception as exc:
        result = dict(operations)
        if 'context' in locals():
            result.update(_safe_observation_scalars(getattr(context, "observations", {})))
        if 'service' in locals():
            result.update(_safe_observation_scalars(getattr(service, "device_observation", {})))
        partial = getattr(exc, "observation", None)
        result.update(_safe_observation_scalars(partial if isinstance(partial, dict) else {}))
        result.update({
            "failed_stage": current_stage,
            "last_completed_stage": last_completed_stage,
            "exception_type": type(exc).__name__,
            "exception_message_class": _classify_edit_form_exception(exc, current_stage),
            "exception_has_observation": isinstance(partial, dict),
            "exception_failed_phase": str(getattr(exc, "failed_phase", "") or ""),
        })
        result.update(_name_error_diagnostics(exc, _base_dir()))
        result.update(_key_error_diagnostics(exc))
        try:
            logger.exception(f"edit_form_failure stage={current_stage} failed_phase={getattr(exc, 'failed_phase', '') or 'none'} observation_keys={len(result)}")
        except Exception:
            pass
        internal_keys = {"panel", "device_detail_panel", "client_certificate_panel", "edit_form"}
        for key, value in result.items():
            if key in internal_keys:
                continue
            safe_value = _safe_public_diagnostic_value(value)
            if safe_value is None and value is not None:
                continue
            _emit(logger, key, safe_value)
            print(f"{key}={safe_value}")
        return 1
    finally:
        if browser is not None:
            try:
                browser.quit()
            except Exception:
                pass


def _run_smsm_client_certificate_primary_input_only(args: list[str]) -> int:
    """Inspect the resolved primary input without interacting with it."""
    logger = AppLogger(_base_dir(), unique_log=True)
    browser = None
    operations = {
        "client_certificate_primary_input_click_called": False,
        "client_certificate_primary_input_focus_called": False,
        "client_certificate_primary_input_value_write_called": False,
        "client_certificate_primary_input_expand_click_called": False,
        "client_certificate_selection_control_click_called": False,
        "client_certificate_option_selection_called": False,
        "device_imei_send_keys_called": False,
        "device_binding_save_called": False,
        "client_certificate_cancel_click_called": False,
        "excel_write_called": False,
        "certificate_upload_called": False,
        "browser_start_called": False,
    }
    forbidden = {
        "--bind-existing-smsm-certificate", "--allow-device-binding", "--allow-excel-write",
        "--allow-certificate-upload", "--allow-certificate-download", "--prepare-smsm-certificate-upload",
        "--run-single-certificate-workflow", "--inspect-smsm-client-certificate-edit-form-only",
        "--inspect-smsm-client-certificate-navigation-only", "--verify-smsm-device-detail-only",
    }
    if args != ["--inspect-smsm-client-certificate-primary-input-only"] or any(flag in forbidden for flag in args):
        for key, value in {**operations, "failed_stage": "cli_flag_validation", "exception_type": "ArgumentError"}.items():
            _emit(logger, key, value)
            print(f"{key}={value}")
        return 2
    try:
        config = load_config()
        excel_path = str((config.get("excel", {}) or {}).get("path", ""))
        targets = ExcelReader(excel_path).read_targets(include_row_number=True)
        if not targets:
            raise RuntimeError("有効なExcel対象がありません")
        context = WorkflowContext()
        context.config = config
        context.set_target(targets[0])
        smsm_config = resolve_smsm_config(config)
        browser = Browser(_base_dir(), config)
        browser.start()
        operations["browser_start_called"] = True
        service = ProductionWorkflowService(config=config, logger=logger, browser=browser, smsm_config=smsm_config)
        service.smsm_login(context)
        service.smsm_open_device_list(context)
        service.smsm_search_device_by_serial(context, read_only=True)
        result = service.smsm.inspect_client_certificate_edit_form_only(context.target_serial, keep_panel=True)
        result.update(operations)
        panel = result.get("client_certificate_panel")
        if not (
            result.get("device_result_identity_verified") is True
            and result.get("client_certificate_edit_transition_detected") is True
            and result.get("client_certificate_edit_marker_wait_completed") is True
            and panel is not None
        ):
            result["primary_input_failed_stage"] = "client_certificate_edit_form_only_wait_edit_state"
            success = False
        else:
            result.update(service.smsm.inspect_primary_client_certificate_input(panel))
            success = bool(
                result.get("client_certificate_primary_input_candidate_count") == 1
                and result.get("client_certificate_primary_input_unique") is True
                and result.get("client_certificate_primary_input_resolved") is True
                and result.get("client_certificate_after_control_element_count", 0) >= 1
                and result.get("client_certificate_save_candidate_count") == 1
                and result.get("client_certificate_cancel_candidate_count") == 1
                and not any(value for key, value in operations.items() if key != "browser_start_called")
            )
        result.pop("client_certificate_panel", None)
        result["failed_stage"] = "" if success else result.get("primary_input_failed_stage", "client_certificate_primary_input_inspection")
        result["last_completed_stage"] = "client_certificate_primary_input_inspection" if success else "client_certificate_edit_form_only_wait_edit_state"
        for key, value in result.items():
            if key in {"panel", "device_detail_panel", "client_certificate_panel", "edit_form"}:
                continue
            safe_value = _safe_public_diagnostic_value(value)
            if safe_value is not None:
                _emit(logger, key, safe_value)
                print(f"{key}={safe_value}")
        return 0 if success else 1
    except Exception as exc:
        print(f"failed_stage=client_certificate_primary_input_inspection")
        print(f"exception_type={type(exc).__name__}")
        return 1
    finally:
        if browser is not None:
            try:
                browser.quit()
            except Exception:
                pass


def _run_smsm_client_certificate_imei_input_only(args: list[str]) -> int:
    """Send the Excel IMEI once to the resolved input and inspect suggestions only."""
    logger = AppLogger(_base_dir(), unique_log=True)
    browser = None
    operations = {
        "client_certificate_primary_input_click_called": False,
        "client_certificate_primary_input_focus_called": False,
        "client_certificate_primary_input_expand_click_called": False,
        "device_imei_send_keys_called": False,
        "device_imei_send_keys_count": 0,
        "client_certificate_suggestion_click_called": False,
        "client_certificate_suggestion_click_count": 0,
        "client_certificate_option_selection_called": False,
        "client_certificate_option_selection_count": 0,
        "client_certificate_suggestion_keyboard_selection_called": False,
        "client_certificate_suggestion_keyboard_selection_count": 0,
        "device_binding_save_called": False,
        "device_binding_save_count": 0,
        "client_certificate_cancel_click_called": False,
        "excel_write_called": False,
        "certificate_upload_called": False,
    }
    forbidden = {
        "--inspect-smsm-client-certificate-edit-form-only", "--inspect-smsm-client-certificate-primary-input-only",
        "--bind-existing-smsm-certificate", "--allow-device-binding", "--allow-excel-write",
        "--allow-certificate-upload", "--allow-certificate-download", "--prepare-smsm-certificate-upload",
        "--run-single-certificate-workflow", "--verify-smsm-device-detail-only",
    }
    if args != ["--inspect-smsm-client-certificate-imei-input-only"] or any(flag in forbidden for flag in args):
        for key, value in {**operations, "failed_stage": "cli_flag_validation", "exception_type": "ArgumentError"}.items():
            _emit(logger, key, value)
            print(f"{key}={value}")
        return 2
    try:
        config = load_config()
        targets = ExcelReader(str((config.get("excel", {}) or {}).get("path", ""))).read_targets(include_row_number=True)
        if not targets:
            raise RuntimeError("有効なExcel対象がありません")
        context = WorkflowContext()
        context.config = config
        context.set_target(targets[0])
        target_imei = normalize_imei(context.target_imei)
        smsm_config = resolve_smsm_config(config)
        browser = Browser(_base_dir(), config)
        browser.start()
        service = ProductionWorkflowService(config=config, logger=logger, browser=browser, smsm_config=smsm_config)
        service.smsm_login(context)
        service.smsm_open_device_list(context)
        service.smsm_search_device_by_serial(context, read_only=True)
        result = service.smsm.inspect_client_certificate_edit_form_only(context.target_serial, keep_panel=True)
        result.update({
            "device_imei_target_present": bool(target_imei),
            "device_imei_target_nonblank": bool(target_imei.strip()),
            "device_imei_target_length_valid": len(target_imei) == 15,
            "device_imei_target_format_valid": target_imei.isdigit(),
            "device_imei_target_source_type": "workflow_context",
        })
        panel = result.get("client_certificate_panel")
        primary = service.smsm.inspect_primary_client_certificate_input(panel)
        result.update(primary)
        if not (result.get("device_result_identity_verified") is True and result.get("client_certificate_edit_transition_detected") is True and result.get("client_certificate_primary_input_resolved") is True and panel is not None):
            success = False
            result["failed_stage"] = "client_certificate_imei_input_only_inspect_primary_input"
        else:
            result.update(operations)
            result.update(service.smsm.send_imei_once_and_inspect_suggestions(panel, target_imei))
            success = bool(
                result.get("device_imei_send_keys_called") is True
                and result.get("device_imei_send_keys_count") == 1
                and result.get("device_imei_send_keys_completed") is True
                and result.get("device_imei_send_keys_retry_count") == 0
                and result.get("device_imei_input_exact_match") is True
                and result.get("device_imei_input_was_truncated") is False
                and result.get("device_imei_input_was_duplicated") is False
                and result.get("device_imei_input_was_transformed") is False
                and result.get("client_certificate_imei_suggestion_container_unique") is True
                and result.get("client_certificate_imei_suggestion_container_visible") is True
                and result.get("client_certificate_imei_suggestion_visible_option_count", 0) >= 1
                and result.get("client_certificate_imei_exact_option_unique") is True
                and not any(operations.values())
            )
            result["failed_stage"] = "" if success else "client_certificate_imei_input_only_wait_suggestions"
        result["last_completed_stage"] = "client_certificate_imei_input_only_completed" if success else "client_certificate_imei_input_only_verify_exact_suggestion"
        result.pop("client_certificate_panel", None)
        for key, value in result.items():
            if key in {"panel", "device_detail_panel", "client_certificate_panel", "edit_form"}:
                continue
            safe_value = _safe_public_diagnostic_value(value)
            if safe_value is not None:
                _emit(logger, key, safe_value)
                print(f"{key}={safe_value}")
        return 0 if success else 1
    except Exception as exc:
        print("failed_stage=client_certificate_imei_input_only")
        print(f"exception_type={type(exc).__name__}")
        return 1
    finally:
        if browser is not None:
            try:
                browser.quit()
            except Exception:
                pass


def _safe_observation_scalars(observation) -> dict[str, object]:
    if not isinstance(observation, dict):
        return {}
    internal_keys = {"panel", "device_detail_panel", "client_certificate_panel", "edit_form", "driver"}
    result = {}
    for key, value in observation.items():
        if key in internal_keys:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[key] = value
    return result


def _classify_edit_form_exception(exc: BaseException, stage: str) -> str:
    failed_phase = str(getattr(exc, "failed_phase", "") or "")
    if stage.endswith("load_target"):
        return "empty_target"
    if stage.endswith("login"):
        return "login_not_verified"
    if stage.endswith("open_device_list"):
        return "device_list_not_verified"
    if stage.endswith("search_device") or failed_phase.startswith("resolve_device_search"):
        return "search_context_unresolved"
    if stage.endswith("select_device"):
        return "result_row_unresolved"
    if "view_state" in stage:
        return "view_state_timeout"
    if "edit" in stage and "click" in stage:
        return "edit_click_failed"
    if "edit_state" in stage:
        return "edit_state_timeout"
    return "unexpected_runtime_error"


def _safe_public_diagnostic_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            clean = _safe_public_diagnostic_value(item)
            if clean is not None or item is None:
                result[str(key)] = clean
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            clean = _safe_public_diagnostic_value(item)
            if clean is not None or item is None:
                result.append(clean)
        return result
    return None


def _run_smsm_device_detail_only(args: list[str]) -> int:
    """Verify one SMSM result row and its detail-pane serial, then stop."""
    logger = AppLogger(_base_dir(), unique_log=True)
    browser = None
    observations = {
        "device_detail_only_mode": True,
        "device_search_result_total_count": None,
        "device_search_result_page_count": None,
        "device_result_candidate_count": 0,
        "device_result_candidate_unique": False,
        "device_result_click_candidate_count": 0,
        "device_result_click_unique": False,
        "device_result_click_called": False,
        "device_result_click_count": 0,
        "device_detail_panel_candidate_count": 0,
        "device_detail_serial_field_candidate_count": 0,
        "device_detail_serial_value_candidate_count": 0,
        "device_detail_serial_exact_match": False,
        "device_detail_navigation_verified": False,
        "device_result_selected": False,
        "device_result_identity_verified": False,
        "device_result_identity_verification_method": "",
        "other_settings_click_called": False,
        "other_settings_click_count": 0,
        "device_client_certificate_click_called": False,
        "device_client_certificate_click_count": 0,
        "certificate_selection_called": False,
        "uploaded_certificate_selected": False,
        "device_binding_save_called": False,
        "device_binding_save_count": 0,
        "excel_write_called": False,
        "certificate_upload_called": False,
        "browser_start_called": False,
    }

    def emit_public(key: str, value) -> None:
        _emit(logger, key, value)
        print(f"{key}={value}")

    conflict_flags = {
        "--bind-existing-smsm-certificate",
        "--allow-device-binding",
        "--allow-excel-write",
        "--allow-certificate-upload",
    }
    if args != ["--verify-smsm-device-detail-only"] or any(flag in args for flag in conflict_flags):
        observations["failed_stage"] = "cli_flag_validation"
        observations["exception_type"] = "ArgumentError"
        for key, value in observations.items():
            emit_public(key, value)
        emit_public("failed_stage", "cli_flag_validation")
        emit_public("exception_type", "ArgumentError")
        return 2

    try:
        config = load_config()
        excel_path = str((config.get("excel", {}) or {}).get("path", ""))
        if not excel_path:
            raise RuntimeError("Excelパスが設定されていません")
        targets = ExcelReader(excel_path).read_targets(include_row_number=True)
        if not targets:
            raise RuntimeError("有効なExcel対象がありません")
        context = WorkflowContext()
        context.config = config
        context.set_target(targets[0])
        context.record("excel_target_count", len(targets))
        browser = Browser(_base_dir(), config)
        browser.start()
        observations["browser_start_called"] = True
        service = ProductionWorkflowService(
            config=config,
            logger=logger,
            browser=browser,
            smsm_config=resolve_smsm_config(config),
        )
        service.smsm_login(context)
        service.smsm_open_device_list(context)
        service.smsm_search_device_by_serial(context, read_only=True)
        selection = service.smsm.select_matched_device_row(context.target_serial)
        service.device_observation.update(selection)
        for key, value in selection.items():
            context.record(key, value)
        observations.update(context.observations)
        observations.update(selection)
        observations.update({
            "other_settings_click_called": False,
            "other_settings_click_count": 0,
            "device_client_certificate_click_called": False,
            "device_client_certificate_click_count": 0,
            "certificate_selection_called": False,
            "uploaded_certificate_selected": False,
            "device_binding_save_called": False,
            "device_binding_save_count": 0,
            "excel_write_called": False,
            "certificate_upload_called": False,
        })
        count_contract = (
            observations.get("device_search_result_total_count") == 1
            and observations.get("device_search_result_page_count") == 1
        )
        structural_contract = (
            observations.get("device_search_result_total_count") is None
            and observations.get("device_search_result_page_count") is None
            and observations.get("device_search_result_structural_uniqueness_verified") is True
        )
        success = (
            observations.get("device_search_submit_count") == 1
            and observations.get("device_search_result_container_count") == 1
            and (count_contract or structural_contract)
            and observations.get("device_search_post_result_visible_row_count") == 1
            and observations.get("device_result_candidate_count") == 1
            and observations.get("device_result_candidate_unique") is True
            and observations.get("device_result_click_candidate_count") == 1
            and observations.get("device_result_click_unique") is True
            and observations.get("device_result_click_called") is True
            and observations.get("device_result_click_count") == 1
            and observations.get("device_detail_panel_candidate_count") == 1
            and observations.get("device_detail_serial_field_candidate_count") == 1
            and observations.get("device_detail_serial_value_candidate_count") == 1
            and observations.get("device_detail_serial_exact_match") is True
            and observations.get("device_detail_navigation_verified") is True
            and observations.get("device_result_selected") is True
            and observations.get("device_result_identity_verified") is True
            and observations.get("device_result_identity_verification_method") == "device_detail_panel_serial_exact_match"
        )
        observations["failed_stage"] = "" if success else "device_detail_only_verification"
        observations["exception_type"] = ""
        for key, value in observations.items():
            emit_public(key, value)
        return 0 if success else 1
    except Exception as exc:
        observations["failed_stage"] = "device_detail_only_verification"
        observations["exception_type"] = type(exc).__name__
        for key, value in observations.items():
            emit_public(key, value)
        return 1
    finally:
        if browser is not None:
            try:
                browser.quit()
            except Exception:
                pass


def _run_matched_device_result_link_inspection(args: list[str]) -> int:
    """Run only the read-only Excel/SMSM search path and inspect result links."""
    if args != ["--inspect-matched-device-result-links"]:
        return 2
    logger = AppLogger(_base_dir(), unique_log=True)
    browser = None
    observations = {
        "device_result_link_inspection_called": False,
        "device_result_link_inspection_completed": False,
        "device_result_link_candidate_count": 0,
        "device_result_link_visible_count": 0,
        "device_result_link_enabled_count": 0,
        "device_result_link_inside_matched_row_count": 0,
        "device_result_link_detail_text_count": 0,
        "device_result_link_device_detail_path_count": 0,
        "device_result_link_unique_detail_candidate_count": 0,
        "device_result_link_click_called": False,
        "device_result_link_click_count": 0,
        "device_result_link_inspection_failed_phase": "not_started",
        "device_result_link_inspection_exception_type": "",
    }
    operation_observations = {
        "device_binding_save_called": False,
        "device_binding_save_count": 0,
        "excel_write_called": False,
        "certificate_upload_called": False,
    }

    def emit_public(key: str, value) -> None:
        _emit(logger, key, value)
        print(f"{key}={value}")

    try:
        config = load_config()
        excel_path = str((config.get("excel", {}) or {}).get("path", ""))
        if not excel_path:
            raise RuntimeError("Excelパスが設定されていません")
        targets = ExcelReader(excel_path).read_targets(include_row_number=True)
        if not targets:
            raise RuntimeError("有効なExcel対象がありません")
        context = WorkflowContext()
        context.config = config
        context.set_target(targets[0])
        context.record("excel_target_count", len(targets))
        browser = Browser(_base_dir(), config)
        browser.start()
        service = ProductionWorkflowService(
            config=config,
            logger=logger,
            browser=browser,
            smsm_config=resolve_smsm_config(config),
        )
        context.services["workflow"] = service
        handlers = make_default_handlers()
        handlers["smsm_search_device_by_serial"] = lambda workflow_context: service.smsm_search_device_by_serial(
            workflow_context,
            read_only=True,
        )
        stages = (
            "excel_load_target",
            "smsm_login",
            "smsm_open_device_list",
            "smsm_search_device_by_serial",
        )
        result = run_single_certificate_workflow(
            handlers={stage: handlers[stage] for stage in stages},
            context=context,
            logger=logger,
            stages=stages,
        )
        observations.update(result.get("observations", {}) or {})
        if result.get("failed_stage"):
            observations["device_result_link_inspection_failed_phase"] = "search_workflow"
            return_code = 1
        else:
            link_result = service.smsm_inspect_matched_device_result_links(context)
            observations.update(link_result)
            return_code = 0 if link_result.get("device_result_link_inspection_completed") is True else 1
        observations.update(operation_observations)
        for key, value in observations.items():
            emit_public(key, value)
        emit_public("workflow_stages", "excel_load_target,smsm_login,smsm_open_device_list,smsm_search_device_by_serial")
        emit_public("failed_stage", "" if return_code == 0 else observations.get("device_result_link_inspection_failed_phase", "read_only_inspection"))
        emit_public("exception_type", observations.get("device_result_link_inspection_exception_type", ""))
        return return_code
    except Exception as exc:
        observations["device_result_link_inspection_exception_type"] = type(exc).__name__
        observations["device_result_link_inspection_failed_phase"] = "read_only_inspection"
        observations.update(operation_observations)
        for key, value in observations.items():
            emit_public(key, value)
        emit_public("failed_stage", "read_only_inspection")
        emit_public("exception_type", type(exc).__name__)
        return 1
    finally:
        if browser is not None:
            try:
                browser.quit()
            except Exception:
                pass


def _run_smsm_login_only() -> int:
    logger = AppLogger(_base_dir())
    browser = None
    current_stage = "config_loaded"
    login_observations = {
        "smsm_login_page_detected": False,
        "smsm_authenticated_page_detected": False,
    }

    def emit_public(key: str, value) -> None:
        _emit(logger, key, value)
        print(f"{key}={value}")

    try:
        config = load_config()
        smsm_config = resolve_smsm_config(config)
        for key, value in credential_status(smsm_config).items():
            emit_public(key, value)
        if not smsm_config.valid:
            current_stage = "smsm_config_validation"
            raise RuntimeError("SMSM認証情報の解決結果が不完全です")

        current_stage = "browser_start"
        browser = Browser(_base_dir(), config)
        browser.start()

        def _trace(key: str, value) -> None:
            nonlocal current_stage
            if key == "login_page_opened" and value is True:
                login_observations["smsm_login_page_detected"] = True
            if key == "login_completed" and value is True:
                login_observations["smsm_authenticated_page_detected"] = True
            if key.startswith("smsm_") and value is True:
                current_stage = key

        emit_public("smsm_login_called", True)
        current_stage = "smsm_login"
        SmsmHandler(
            browser=browser,
            logger=_SilentHandlerLogger(),
            smsm_config=smsm_config,
        ).login(_trace)
        emit_public("smsm_login_completed", True)
        for key, value in login_observations.items():
            emit_public(key, value)
        emit_public("failed_stage", "")
        emit_public("exception_type", "")
        return 0
    except Exception as exc:
        emit_public("smsm_login_called", current_stage == "smsm_login")
        emit_public("smsm_login_completed", False)
        for key, value in login_observations.items():
            emit_public(key, value)
        emit_public("failed_stage", current_stage)
        emit_public("exception_type", type(exc).__name__)
        return 30
    finally:
        if browser is not None:
            try:
                browser.quit()
            except Exception:
                pass


def _workflow_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="diagnose_smsm_single_target_lookup.py",
        description="1件の証明書紐づけworkflowを固定stage順で実行します。",
    )
    parser.add_argument("--run-single-certificate-workflow", action="store_true")
    parser.add_argument("--prepare-smsm-certificate-upload", action="store_true")
    parser.add_argument("--smsm-login-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-certificate-download", action="store_true")
    parser.add_argument("--resume-from-existing-certificate", action="store_true")
    parser.add_argument("--manual-checkpoint-on-front-half-failure", action="store_true")
    parser.add_argument("--allow-certificate-upload", action="store_true")
    parser.add_argument("--allow-device-binding", action="store_true")
    parser.add_argument("--bind-existing-smsm-certificate", action="store_true")
    parser.add_argument("--allow-excel-write", action="store_true")
    parser.add_argument("--inspect-matched-device-result-links", action="store_true")
    parser.add_argument("--verify-smsm-device-detail-only", action="store_true")
    parser.add_argument("--inspect-smsm-client-certificate-navigation-only", action="store_true")
    parser.add_argument("--inspect-smsm-client-certificate-edit-form-only", action="store_true")
    parser.add_argument("--inspect-smsm-client-certificate-primary-input-only", action="store_true")
    parser.add_argument("--inspect-smsm-client-certificate-imei-input-only", action="store_true")
    return parser


def _upload_only_stages() -> tuple[str, ...]:
    prepare = PREPARE_SMSM_CERTIFICATE_UPLOAD_STAGES
    workflow = WORKFLOW_STAGES
    reuse_index = prepare.index("hennge_reuse_existing_certificate")
    password_index = prepare.index("hennge_read_certificate_password")
    smsmlogin_index = workflow.index("smsm_login")
    verify_upload_index = workflow.index("smsm_verify_certificate_upload")
    hennge_stages = prepare[:reuse_index + 1] + prepare[password_index:password_index + 1]
    return hennge_stages + workflow[smsmlogin_index:verify_upload_index + 1]


def _existing_smsm_certificate_binding_stages() -> tuple[str, ...]:
    return (
        "excel_load_target",
        "smsm_login",
        "smsm_open_device_list",
        "smsm_search_device_by_serial",
        "smsm_open_device_detail",
        "smsm_open_other_settings",
        "smsm_open_device_client_certificate",
        "smsm_select_uploaded_certificate",
        "smsm_save_device_certificate_binding",
        "smsm_verify_device_certificate_binding",
    )


def _emit_binding_summary(logger, result: dict[str, object] | None = None, *, browser_start_called: bool = False) -> None:
    result = result or {}
    observations = result.get("observations", {}) or {}
    counters = result.get("operation_counters", {}) or {}
    save_called = counters.get("device_binding_save_called") is True or observations.get("device_binding_save_called") is True
    save_count = int(observations.get("device_binding_save_count", 1 if save_called else 0) or 0)
    fields = {
        "device_search_target_present": observations.get("device_search_target_present", False),
        "device_search_page_verified": observations.get("device_search_page_verified", False),
        "device_search_type_selection_called": observations.get("device_search_type_selection_called", False),
        "device_search_type_already_selected": observations.get("device_search_type_already_selected", False),
        "device_search_type_click_count": observations.get("device_search_type_click_count", 0),
        "device_search_dom_reobserve_called": observations.get("device_search_dom_reobserve_called", False),
        "device_search_dom_reobserve_completed": observations.get("device_search_dom_reobserve_completed", False),
        "device_search_type_control_candidate_count": observations.get("device_search_type_control_candidate_count", 0),
        "device_search_type_option_candidate_count": observations.get("device_search_type_option_candidate_count", 0),
        "device_search_type_target_option_found": observations.get("device_search_type_target_option_found", False),
        "device_search_type_control_displayed": observations.get("device_search_type_control_displayed", False),
        "device_search_type_control_enabled": observations.get("device_search_type_control_enabled", False),
        "device_search_input_candidate_count": observations.get("device_search_input_candidate_count", 0),
        "device_search_button_candidate_count": observations.get("device_search_button_candidate_count", 0),
        "device_search_send_keys_called": observations.get("device_search_send_keys_called", False),
        "device_search_send_keys_count": observations.get("device_search_send_keys_count", 0),
        "device_search_submit_called": observations.get("device_search_submit_called", False),
        "device_search_submit_count": observations.get("device_search_submit_count", 0),
        "device_search_wait_called": observations.get("device_search_wait_called", False),
        "device_search_wait_completed": observations.get("device_search_wait_completed", False),
        "device_search_exact_match_count": observations.get("device_search_exact_match_count"),
        "device_search_failed_phase": observations.get("device_search_failed_phase", result.get("failed_stage", "")),
        "device_search_exception_type": observations.get("device_search_exception_type", result.get("exception_type", "")),
        "device_list_navigation_called": observations.get("device_list_navigation_called", False),
        "device_list_navigation_completed": observations.get("device_list_navigation_completed", False),
        "device_list_nav_candidate_count": observations.get("device_list_nav_candidate_count", 0),
        "device_list_nav_unique": observations.get("device_list_nav_unique", False),
        "device_list_nav_click_called": observations.get("device_list_nav_click_called", False),
        "device_list_nav_click_count": observations.get("device_list_nav_click_count", 0),
        "device_list_pathname_matches": observations.get("device_list_pathname_matches", False),
        "device_list_search_input_candidate_count": observations.get("device_list_search_input_candidate_count", 0),
        "device_list_search_button_candidate_count": observations.get("device_list_search_button_candidate_count", 0),
        "device_list_condition_pathname_matches": observations.get("device_list_condition_pathname_matches", False),
        "device_list_condition_search_input_unique": observations.get("device_list_condition_search_input_unique", False),
        "device_list_condition_search_button_unique": observations.get("device_list_condition_search_button_unique", False),
        "device_list_condition_main_container_visible": observations.get("device_list_condition_main_container_visible", False),
        "device_list_page_verified": observations.get("device_list_page_verified", False),
        "device_list_failed_phase": observations.get("device_list_failed_phase", result.get("failed_stage", "")),
        "device_list_exception_type": observations.get("device_list_exception_type", result.get("exception_type", "")),
        "device_search_called": observations.get("device_search_called", False),
        "device_search_exact_match_count": observations.get("device_search_exact_match_count"),
        "device_result_selected": observations.get("device_result_selected", observations.get("device_detail_click_count", 0) == 1),
        "device_platform_ios_verified": observations.get("device_platform_ios_verified", observations.get("ios_verified", False)),
        "uploaded_certificate_exact_match_count": observations.get("uploaded_certificate_exact_match_count", observations.get("uploaded_certificate_exact_count", 0)),
        "uploaded_certificate_selected": observations.get("uploaded_certificate_selected", observations.get("uploaded_certificate_target_exact", False)),
        "device_binding_save_called": save_called,
        "device_binding_save_count": save_count,
        "device_binding_completion_verified": observations.get("device_binding_completion_verified", observations.get("device_binding_verified", False)),
        "bound_certificate_exact_match_count": observations.get("bound_certificate_exact_match_count", observations.get("bound_certificate_exact_count", 0)),
        "device_binding_verified": observations.get("device_binding_verified", False),
        "failed_stage": result.get("failed_stage", ""),
        "exception_type": result.get("exception_type", ""),
    }
    fields["browser_start_called"] = browser_start_called
    for key, value in fields.items():
        _emit(logger, key, value)
        print(f"{key}={value}")


def _run_single_certificate_workflow_cli(argv: list[str]) -> int:
    parser = _workflow_argument_parser()
    parsed = parser.parse_args(argv)
    preparation_only = bool(parsed.prepare_smsm_certificate_upload)
    binding_only = bool(parsed.bind_existing_smsm_certificate)
    logger = AppLogger(_base_dir(), unique_log=True) if binding_only else AppLogger(_base_dir())
    binding_stages = _existing_smsm_certificate_binding_stages() if binding_only else ()
    _emit(logger, "binding_cli_selected", binding_only)
    _emit(logger, "binding_allow_flag_present", bool(parsed.allow_device_binding))
    _emit(logger, "binding_stage_count", len(binding_stages))
    if binding_only and not parsed.allow_device_binding:
        _emit(logger, "binding_handler_count", 0)
        _emit(logger, "browser_start_called", False)
        _emit_binding_summary(logger, {"failed_stage": "cli_flag_validation", "exception_type": "PermissionError"})
        print("--bind-existing-smsm-certificateには--allow-device-bindingが必要です。")
        return 2
    if not parsed.run_single_certificate_workflow and not preparation_only and not binding_only:
        parser.print_help()
        return 0

    options = WorkflowOptions(
        dry_run=bool(parsed.dry_run),
        resume_from_existing_certificate=bool(parsed.resume_from_existing_certificate or preparation_only),
        front_half_only=bool(
            parsed.allow_certificate_download
            and not parsed.allow_certificate_upload
            and not parsed.allow_device_binding
            and not parsed.allow_excel_write
        ),
        allow_certificate_download=bool(parsed.allow_certificate_download),
        allow_certificate_upload=bool(parsed.allow_certificate_upload),
        allow_device_binding=bool(parsed.allow_device_binding),
        allow_excel_write=bool(parsed.allow_excel_write),
    )
    context = WorkflowContext(options=options)
    try:
        handlers = make_preparation_handlers() if preparation_only else make_default_handlers()
        workflow_stages = PREPARE_SMSM_CERTIFICATE_UPLOAD_STAGES if preparation_only else None
        if binding_only:
            default_handlers = make_default_handlers()
            workflow_stages = binding_stages
            handlers = {stage: default_handlers[stage] for stage in workflow_stages}
    except Exception as exc:
        if binding_only:
            _emit(logger, "binding_handler_count", 0)
            _emit(logger, "browser_start_called", False)
            _emit_binding_summary(logger, {"failed_stage": "workflow_handler_setup", "exception_type": type(exc).__name__})
            return 1
        raise
    if parsed.run_single_certificate_workflow and parsed.allow_certificate_upload and not parsed.allow_device_binding:
        handlers["hennge_reuse_existing_certificate"] = make_preparation_handlers()[
            "hennge_reuse_existing_certificate"
        ]
        workflow_stages = list(_upload_only_stages())
    browser = None

    def load_target(workflow_context: WorkflowContext) -> None:
        config = load_config()
        excel_path = str((config.get("excel", {}) or {}).get("path", ""))
        if not excel_path:
            raise RuntimeError("Excelパスが設定されていません")
        reader = ExcelReader(excel_path)
        try:
            targets = reader.read_targets(include_row_number=True)
        except (PermissionError, OSError, KeyError, ValueError):
            if not options.dry_run or not Path(excel_path).is_file():
                raise
            temporary_path = None
            try:
                detection = detect_target_workbook(Path(excel_path), timeout_sec=3.0, interval_sec=0.5)
                workbook = getattr(detection, "workbook", None)
                if workbook is None:
                    import win32com.client

                    application = win32com.client.GetActiveObject("Excel.Application")
                    configured_name = Path(excel_path).name.casefold()
                    workbook_candidates = []
                    for index in range(1, int(application.Workbooks.Count) + 1):
                        candidate = application.Workbooks.Item(index)
                        full_name = str(candidate.FullName or "")
                        candidate_name = full_name.replace("\\", "/").rsplit("/", 1)[-1].casefold()
                        if candidate_name == configured_name:
                            workbook_candidates.append(candidate)
                    if len(workbook_candidates) != 1:
                        raise PermissionError("開いているExcel対象を一意に確認できません")
                    workbook = workbook_candidates[0]
                sheet_name = "HENNGE登録作業必要情報"
                sheet = workbook.Worksheets(sheet_name)
                targets = []
                for row_number in range(4, 1004):
                    raw_values = tuple(sheet.Cells(row_number, column).Value for column in range(3, 6))
                    values = tuple("" if value == -2146826246 else value for value in raw_values)
                    if not is_target_row(values):
                        continue
                    alias = "" if values[0] is None else str(values[0]).strip()
                    serial = "" if values[1] is None else str(values[1]).strip()
                    imei = normalize_imei(values[2])
                    if not alias or not serial or not imei:
                        raise ValueError("Excel対象の必須項目が不足しています")
                    targets.append({"alias": alias, "serial": serial, "imei": imei, "row_number": row_number})
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
        if not targets:
            raise RuntimeError("有効なExcel対象がありません")
        workflow_context.set_target(targets[0])
        workflow_context.record("excel_target_count", len(targets))
        workflow_context.record("selected_target_count", 1)

    handlers["excel_load_target"] = load_target
    if binding_only:
        missing_handlers = [stage for stage in binding_stages if not callable(handlers.get(stage))]
        _emit(logger, "binding_handler_count", len(handlers))
        if missing_handlers:
            failure = {"failed_stage": missing_handlers[0], "exception_type": "WorkflowStageError"}
            _emit(logger, "binding_missing_handler_count", len(missing_handlers))
            _emit_binding_summary(logger, failure)
            return 1
    else:
        _emit(logger, "binding_handler_count", 0)
    browser_start_called = False
    _emit(logger, "browser_start_called", False)
    result: dict[str, object] = {}
    try:
        if not options.dry_run:
            browser_start_called = True
            _emit(logger, "browser_start_called", True)
            config = load_config()
            browser = Browser(_base_dir(), config)
            browser.start()
            smsm_config = resolve_smsm_config(config)
            service = ProductionWorkflowService(config=config, logger=logger, browser=browser, smsm_config=smsm_config)
            context.services["workflow"] = service
        run_kwargs = {"handlers": handlers, "context": context, "logger": logger}
        if workflow_stages is not None:
            run_kwargs["stages"] = workflow_stages
        result = run_single_certificate_workflow(**run_kwargs)
        if parsed.manual_checkpoint_on_front_half_failure and result.get("failed_stage") in {
            "hennge_search_certificate_by_alias",
            "hennge_wait_certificate_search_result",
            "hennge_select_certificate_result",
            "hennge_verify_certificate_detail",
            "hennge_password_reveal_candidate_resolved",
            "hennge_password_reveal_safety_verified",
            "hennge_password_reveal_click",
            "hennge_password_value_resolved",
        }:
            print("HENNGEの現在画面を確認後、PowerShellへ戻り空Enterを押してください。")
            print("待機中は結果行を手動クリックしないでください。")
            try:
                input()
            except EOFError:
                pass
    except Exception as exc:
        if binding_only:
            result = {
                "failed_stage": "browser_start" if browser_start_called and browser is None else "workflow_start",
                "exception_type": type(exc).__name__,
            }
            _emit_binding_summary(logger, result, browser_start_called=browser_start_called)
            return 1
        raise
    finally:
        if browser is not None:
            browser.quit()
    _emit_workflow_report(logger, result)
    if binding_only:
        _emit_binding_summary(logger, result, browser_start_called=browser_start_called)
        observations = result.get("observations", {}) or {}
        return 0 if not result.get("failed_stage") and observations.get("device_binding_verified") is True else 1
    if result.get("front_half_live_verification_complete"):
        return 0
    if result["workflow_implementation_complete"] and result["workflow_dry_run_completed"]:
        return 0
    if result.get("certificate_upload_ready") is True and not result.get("failed_stage") and not result.get("exception_type"):
        return 0
    if result["missing_stage_count"] or result["failed_stage"] == "excel_load_target":
        return 31
    return 1


def _emit_workflow_report(logger: AppLogger, result: dict[str, object]) -> None:
    for report in result.get("stage_reports", ()):
        report_line = (
            "stage={stage};function_name={function_name};implementation_status={implementation_status};"
            "production_connected={production_connected};dry_run_check={dry_run_check};allow_flag={allow_flag};"
            "planned={planned};live_verified={live_verified};executed={executed}"
        ).format(**report)
        print(report_line)
        logger.info(
            report_line
        )
    counters = result.get("operation_counters", {})
    observations = result.get("observations", {}) or {}
    fields = {
        "smsm_credential_source_type": observations.get("smsm_credential_source_type", "unresolved"),
        "smsm_company_code_resolved": observations.get("smsm_company_code_resolved", False),
        "smsm_username_resolved": observations.get("smsm_username_resolved", False),
        "smsm_password_resolved": observations.get("smsm_password_resolved", False),
        "smsm_credentials_complete": observations.get("smsm_credentials_complete", False),
        "smsm_login_called": observations.get("smsm_login_called", False),
        "smsm_login_completed": observations.get("smsm_login_completed", False),
        "hennge_login_live_verified": observations.get("hennge_login_live_verified", False),
        "hennge_search_live_verified": observations.get("hennge_search_live_verified", False),
        "hennge_search_result_count": observations.get("hennge_search_result_count", 0),
        "hennge_search_result_unique": observations.get("hennge_search_result_unique", False),
        "hennge_search_key_type": observations.get("hennge_search_key_type", "alias"),
        "hennge_alias_present": observations.get("hennge_alias_present", False),
        "hennge_search_called": observations.get("hennge_search_called", False),
        "hennge_alias_exact_match_count": observations.get("hennge_alias_exact_match_count", 0),
        "hennge_result_selection_verified": observations.get("hennge_result_selection_verified", False),
        "hennge_result_row_candidate_count": observations.get("hennge_result_row_candidate_count", 0),
        "hennge_result_row_click_called": observations.get("hennge_result_row_click_called", False),
        "hennge_certificate_detail_verified": observations.get("hennge_certificate_detail_verified", False),
        "hennge_detail_alias_available": observations.get("hennge_detail_alias_available", False),
        "hennge_detail_alias_exact_match": observations.get("hennge_detail_alias_exact_match", False),
        "hennge_detail_alias_label_found": observations.get("hennge_detail_alias_label_found", False),
        "hennge_detail_alias_value_found": observations.get("hennge_detail_alias_value_found", False),
        "hennge_detail_alias_value_nonblank": observations.get("hennge_detail_alias_value_nonblank", False),
        "hennge_detail_alias_field_available": observations.get("hennge_detail_alias_field_available", False),
        "hennge_detail_identity_verified_by_unique_search_context": observations.get("hennge_detail_identity_verified_by_unique_search_context", False),
        "hennge_detail_identity_verification_method": observations.get("hennge_detail_identity_verification_method", "unresolved"),
        "hennge_detail_dialog_count": observations.get("hennge_detail_dialog_count", 0),
        "hennge_detail_dialog_unique": observations.get("hennge_detail_dialog_unique", False),
        "hennge_detail_container_found": observations.get("hennge_detail_container_found", False),
        "hennge_detail_field_row_count": observations.get("hennge_detail_field_row_count", 0),
        "hennge_detail_label_count": observations.get("hennge_detail_label_count", 0),
        "hennge_detail_value_count": observations.get("hennge_detail_value_count", 0),
        "hennge_result_row_click_count": observations.get("hennge_result_row_click_count", 0),
        "hennge_download_action_candidate_count": observations.get("hennge_download_action_candidate_count", 0),
        "hennge_download_action_unique": observations.get("hennge_download_action_unique", False),
        "hennge_download_action_displayed": observations.get("hennge_download_action_displayed", False),
        "hennge_download_action_enabled": observations.get("hennge_download_action_enabled", False),
        "hennge_download_action_safe": observations.get("hennge_download_action_safe", False),
        "hennge_download_action_click_called": observations.get("hennge_download_action_click_called", False),
        "hennge_password_source_candidate_count": observations.get("hennge_password_source_candidate_count", 0),
        "password_input_candidate_count": observations.get("password_input_candidate_count", 0),
        "readonly_value_candidate_count": observations.get("readonly_value_candidate_count", 0),
        "text_input_candidate_count": observations.get("text_input_candidate_count", 0),
        "masked_value_candidate_count": observations.get("masked_value_candidate_count", 0),
        "reveal_button_candidate_count": observations.get("reveal_button_candidate_count", 0),
        "copy_button_candidate_count": observations.get("copy_button_candidate_count", 0),
        "password_label_found": observations.get("password_label_found", False),
        "password_value_container_found": observations.get("password_value_container_found", False),
        "password_source_requires_download_action": observations.get("password_source_requires_download_action", False),
        "password_source_requires_reveal_action": observations.get("password_source_requires_reveal_action", False),
        "password_source_requires_copy_action": observations.get("password_source_requires_copy_action", False),
        "hennge_imei_search_called": observations.get("hennge_imei_search_called", False),
        "download_candidate_count": observations.get("download_candidate_count", 0),
        "download_completed": observations.get("download_completed", False),
        "download_extension_valid": observations.get("download_extension_valid", False),
        "download_size_valid": observations.get("download_size_valid", False),
        "download_size_stable": observations.get("download_size_stable", False),
        "password_source_candidate_count": observations.get("password_source_candidate_count", 0),
        "password_source_unique": observations.get("password_source_unique", False),
        "password_source_type": observations.get("password_source_type", "unknown"),
        "password_value_candidate_count": observations.get("password_value_candidate_count", 0),
        "password_section_found": observations.get("password_section_found", False),
        "password_section_scrolled_into_view": observations.get("password_section_scrolled_into_view", False),
        "reveal_button_candidate_count": observations.get("reveal_button_candidate_count", 0),
        "reveal_button_click_count": observations.get("reveal_button_click_count", 0),
        "reveal_button_click_called": observations.get("reveal_button_click_called", False),
        "reveal_button_unique": observations.get("reveal_button_unique", False),
        "reveal_button_displayed": observations.get("reveal_button_displayed", False),
        "reveal_button_enabled": observations.get("reveal_button_enabled", False),
        "password_reveal_candidate_count": observations.get("password_reveal_candidate_count", 0),
        "password_reveal_unique": observations.get("password_reveal_unique", False),
        "password_reveal_displayed": observations.get("password_reveal_displayed", False),
        "password_reveal_enabled": observations.get("password_reveal_enabled", False),
        "password_reveal_disabled": observations.get("password_reveal_disabled", False),
        "password_reveal_safe": observations.get("password_reveal_safe", False),
        "password_reveal_inside_detail_dialog": observations.get("password_reveal_inside_detail_dialog", False),
        "password_reveal_click_started": observations.get("password_reveal_click_started", False),
        "password_reveal_click_completed": observations.get("password_reveal_click_completed", False),
        "password_reveal_click_exception_type": observations.get("password_reveal_click_exception_type", ""),
        "password_copy_button_candidate_count": observations.get("password_copy_button_candidate_count", 0),
        "password_copy_button_unique": observations.get("password_copy_button_unique", False),
        "password_copy_button_displayed": observations.get("password_copy_button_displayed", False),
        "password_copy_button_enabled": observations.get("password_copy_button_enabled", False),
        "password_copy_button_safe": observations.get("password_copy_button_safe", False),
        "password_copy_click_started": observations.get("password_copy_click_started", False),
        "password_copy_click_called": observations.get("password_copy_click_called", False),
        "password_copy_click_count": observations.get("password_copy_click_count", 0),
        "password_copy_click_completed": observations.get("password_copy_click_completed", False),
        "clipboard_read_called": observations.get("clipboard_read_called", False),
        "clipboard_clear_called": observations.get("clipboard_clear_called", False),
        "clipboard_clear_completed": observations.get("clipboard_clear_completed", False),
        "masked_password_field_count": observations.get("masked_password_field_count", 0),
        "password_eye_button_candidate_count": observations.get("password_eye_button_candidate_count", 0),
        "password_eye_button_click_called": observations.get("password_eye_button_click_called", False),
        "password_dom_reobserve_started": observations.get("password_dom_reobserve_started", False),
        "password_dom_reobserved": observations.get("password_dom_reobserved", False),
        "hennge_password_reveal_candidate_resolved": observations.get("hennge_password_reveal_candidate_resolved", False),
        "hennge_password_reveal_safety_verified": observations.get("hennge_password_reveal_safety_verified", False),
        "hennge_password_reveal_click_started": observations.get("hennge_password_reveal_click_started", False),
        "hennge_password_reveal_click_completed": observations.get("hennge_password_reveal_click_completed", False),
        "hennge_password_dom_reobserve_started": observations.get("hennge_password_dom_reobserve_started", False),
        "hennge_password_dom_reobserve_completed": observations.get("hennge_password_dom_reobserve_completed", False),
        "hennge_password_value_resolved": observations.get("hennge_password_value_resolved", False),
        "password_already_revealed": observations.get("password_already_revealed", False),
        "certificate_password_obtained": observations.get("certificate_password_obtained", False),
        "certificate_password_nonblank": observations.get("certificate_password_nonblank", False),
        "smsm_settings_nav_candidate_count": observations.get("smsm_settings_nav_candidate_count", 0),
        "smsm_settings_nav_unique": observations.get("smsm_settings_nav_unique", False),
        "smsm_settings_nav_click_called": observations.get("smsm_settings_nav_click_called", False),
        "smsm_settings_nav_click_count": observations.get("smsm_settings_nav_click_count", 0),
        "smsm_settings_nav_active": observations.get("smsm_settings_nav_active", False),
        "smsm_device_nav_active": observations.get("smsm_device_nav_active", False),
        "smsm_ios_settings_candidate_count": observations.get("smsm_ios_settings_candidate_count", 0),
        "smsm_ios_settings_unique": observations.get("smsm_ios_settings_unique", False),
        "smsm_ios_settings_click_called": observations.get("smsm_ios_settings_click_called", False),
        "smsm_ios_settings_click_count": observations.get("smsm_ios_settings_click_count", 0),
        "smsm_ios_settings_active": observations.get("smsm_ios_settings_active", False),
        "smsm_android_settings_active": observations.get("smsm_android_settings_active", False),
        "smsm_client_certificate_menu_candidate_count": observations.get("smsm_client_certificate_menu_candidate_count", 0),
        "smsm_client_certificate_menu_unique": observations.get("smsm_client_certificate_menu_unique", False),
        "smsm_client_certificate_menu_click_called": observations.get("smsm_client_certificate_menu_click_called", False),
        "smsm_client_certificate_menu_click_count": observations.get("smsm_client_certificate_menu_click_count", 0),
        "smsm_client_certificate_menu_active": observations.get("smsm_client_certificate_menu_active", False),
        "smsm_certificate_search_input_candidate_count": observations.get("smsm_certificate_search_input_candidate_count", 0),
        "smsm_certificate_add_icon_candidate_count": observations.get("smsm_certificate_add_icon_candidate_count", 0),
        "smsm_certificate_pathname_matches": observations.get("smsm_certificate_pathname_matches", False),
        "smsm_client_certificate_page_live_verified": observations.get("smsm_client_certificate_page_live_verified", False),
        "smsm_prepare_called": observations.get("smsm_prepare_called", False),
        "smsm_prepare_page_verified": observations.get("smsm_prepare_page_verified", False),
        "smsm_prepare_target_imei_present": observations.get("smsm_prepare_target_imei_present", False),
        "smsm_prepare_certificate_path_present": observations.get("smsm_prepare_certificate_path_present", False),
        "smsm_prepare_certificate_password_present": observations.get("smsm_prepare_certificate_password_present", False),
        "smsm_prepare_duplicate_check_called": observations.get("smsm_prepare_duplicate_check_called", False),
        "smsm_prepare_duplicate_check_completed": observations.get("smsm_prepare_duplicate_check_completed", False),
        "smsm_prepare_failed_phase": observations.get("smsm_prepare_failed_phase", ""),
        "smsm_prepare_exception_type": observations.get("smsm_prepare_exception_type", ""),
        "duplicate_search_called": observations.get("duplicate_search_called", False),
        "duplicate_check_determinate": observations.get("duplicate_check_determinate", False),
        "duplicate_exact_match_count": observations.get("duplicate_exact_match_count", 0),
        "duplicate_same_name_match_count": observations.get("duplicate_same_name_match_count", 0),
        "duplicate_upload_allowed": observations.get("duplicate_upload_allowed", False),
        "duplicate_check_failed_phase": observations.get("duplicate_check_failed_phase", ""),
        "duplicate_check_exception_type": observations.get("duplicate_check_exception_type", ""),
        "add_button_candidate_count": observations.get("add_button_candidate_count", 0),
        "add_button_unique": observations.get("add_button_unique", False),
        "add_button_click_called": observations.get("add_button_click_called", False),
        "add_form_opened": observations.get("add_form_opened", False),
        "add_form_resolution_method": observations.get("add_form_resolution_method", "unresolved"),
        "add_form_probe_called": observations.get("add_form_probe_called", False),
        "add_form_probe_completed": observations.get("add_form_probe_completed", False),
        "add_form_probe_exception_type": observations.get("add_form_probe_exception_type", ""),
        "add_form_probe_iteration_count": observations.get("add_form_probe_iteration_count", 0),
        "add_form_last_snapshot_available": observations.get("add_form_last_snapshot_available", False),
        "add_form_probe_phase": observations.get("add_form_probe_phase", ""),
        "add_form_probe_completed_phases": observations.get("add_form_probe_completed_phases", []),
        "top_document_iframe_count": observations.get("top_document_iframe_count", 0),
        "visible_iframe_count": observations.get("visible_iframe_count", 0),
        "same_origin_iframe_count": observations.get("same_origin_iframe_count", 0),
        "cross_origin_iframe_count": observations.get("cross_origin_iframe_count", 0),
        "open_shadow_root_host_count": observations.get("open_shadow_root_host_count", 0),
        "shadow_root_file_input_count": observations.get("shadow_root_file_input_count", 0),
        "shadow_root_password_input_count": observations.get("shadow_root_password_input_count", 0),
        "shadow_root_save_button_count": observations.get("shadow_root_save_button_count", 0),
        "top_document_file_input_count": observations.get("top_document_file_input_count", 0),
        "top_document_password_input_count": observations.get("top_document_password_input_count", 0),
        "top_document_text_input_count": observations.get("top_document_text_input_count", 0),
        "top_document_button_count": observations.get("top_document_button_count", 0),
        "top_document_submit_input_count": observations.get("top_document_submit_input_count", 0),
        "create_side_panel_found": observations.get("create_side_panel_found", False),
        "right_side_container_candidate_count": observations.get("right_side_container_candidate_count", 0),
        "right_side_visible_container_count": observations.get("right_side_visible_container_count", 0),
        "client_certificate_area_visible": observations.get("client_certificate_area_visible", False),
        "file_input_dom_count": observations.get("file_input_dom_count", 0),
        "file_input_enabled_count": observations.get("file_input_enabled_count", 0),
        "file_input_count": observations.get("file_input_count", 0),
        "file_input_unique": observations.get("file_input_unique", False),
        "file_input_send_keys_count": observations.get("file_input_send_keys_count", 0),
        "certificate_file_selected": observations.get("certificate_file_selected", observations.get("certificate_selected", False)),
        "selected_certificate_filename_exact_imei_match": observations.get("selected_certificate_filename_exact_imei_match", False),
        "selected_certificate_extension_valid": observations.get("selected_certificate_extension_valid", False),
        "password_input_dom_count": observations.get("password_input_dom_count", 0),
        "password_input_visible_count": observations.get("password_input_visible_count", 0),
        "password_input_global_candidate_count": observations.get("password_input_global_candidate_count", 0),
        "password_input_inside_right_panel_count": observations.get("password_input_inside_right_panel_count", 0),
        "password_label_candidate_count": observations.get("password_label_candidate_count", 0),
        "password_label_associated_input_count": observations.get("password_label_associated_input_count", 0),
        "password_input_after_type_filter_count": observations.get("password_input_after_type_filter_count", 0),
        "password_input_after_exclusion_count": observations.get("password_input_after_exclusion_count", 0),
        "password_input_after_visibility_count": observations.get("password_input_after_visibility_count", 0),
        "password_input_count": observations.get("password_input_count", 0),
        "password_input_unique": observations.get("password_input_unique", False),
        "password_input_resolution_method": observations.get("password_input_resolution_method", "unresolved"),
        "save_button_dom_count": observations.get("save_button_dom_count", 0),
        "save_button_visible_count": observations.get("save_button_visible_count", 0),
        "upload_controls_common_ancestor_count": observations.get("upload_controls_common_ancestor_count", 0),
        "submit_button_candidate_count": observations.get("submit_button_candidate_count", 0),
        "submit_button_unique": observations.get("submit_button_unique", False),
        "file_input_send_keys_called": observations.get("file_input_send_keys_called", False),
        "password_input_send_keys_called": observations.get("password_input_send_keys_called", False),
        "password_input_send_keys_count": observations.get("password_input_send_keys_count", 0),
        "password_input_nonblank_after_send_keys": observations.get("password_input_nonblank_after_send_keys", observations.get("password_input_nonblank_after_send", False)),
        "save_button_candidate_count": observations.get("save_button_candidate_count", 0),
        "save_button_unique": observations.get("save_button_unique", False),
        "save_button_displayed": observations.get("save_button_displayed", False),
        "save_button_enabled": observations.get("save_button_enabled", False),
        "save_button_click_called": observations.get("save_button_click_called", False),
        "certificate_upload_called": observations.get("certificate_upload_called", False),
        "save_button_refetch_candidate_count": observations.get("save_button_refetch_candidate_count", 0),
        "save_button_refetch_unique": observations.get("save_button_refetch_unique", False),
        "save_button_refetch_displayed": observations.get("save_button_refetch_displayed", False),
        "save_button_refetch_enabled": observations.get("save_button_refetch_enabled", False),
        "save_button_inside_current_add_form": observations.get("save_button_inside_current_add_form", False),
        "save_button_click_called": observations.get("save_button_click_called", False),
        "save_button_click_count": observations.get("save_button_click_count", 0),
        "certificate_upload_completion_wait_called": observations.get("certificate_upload_completion_wait_called", False),
        "certificate_upload_completion_verified": observations.get("certificate_upload_completion_verified", False),
        "add_form_closed_after_save": observations.get("add_form_closed_after_save", False),
        "upload_success_message_detected": observations.get("upload_success_message_detected", False),
        "certificate_list_visible_after_save": observations.get("certificate_list_visible_after_save", False),
        "post_upload_search_called": observations.get("post_upload_search_called", False),
        "post_upload_search_completed": observations.get("post_upload_search_completed", False),
        "post_upload_exact_match_count": observations.get("post_upload_exact_match_count", 0),
        "certificate_upload_verified": observations.get("certificate_upload_verified", False),
        "renamed_file_exists": observations.get("renamed_file_exists", False),
        "renamed_file_extension_valid": observations.get("renamed_file_extension_valid", False),
        "renamed_file_size_valid": observations.get("renamed_file_size_valid", False),
        "certificate_file_rename_called": observations.get("certificate_file_rename_called", False),
        "existing_certificate_file_candidate_count": observations.get("existing_certificate_file_candidate_count", 0),
        "existing_certificate_file_unique": observations.get("existing_certificate_file_unique", False),
        "existing_certificate_file_valid": observations.get("existing_certificate_file_valid", False),
        "certificate_download_skipped_existing_valid_file": observations.get("certificate_download_skipped_existing_valid_file", False),
        "certificate_rename_skipped_existing_valid_file": observations.get("certificate_rename_skipped_existing_valid_file", False),
        "validated_certificate_path_available": observations.get("validated_certificate_path_available", False),
        "stage_result": observations.get("stage_result", ""),
        "front_half_live_verification_complete": result.get("front_half_live_verification_complete", False),
        "front_half_completed_stage_count": result.get("front_half_completed_stage_count", 0),
        "certificate_upload_ready": result.get("certificate_upload_ready", False),
        "device_binding_ready": result.get("device_binding_ready", False),
        "workflow_completed": result.get("workflow_completed", False),
        "workflow_implementation_complete": result.get("workflow_implementation_complete", False),
        "workflow_dry_run_completed": result.get("workflow_dry_run_completed", False),
        "failed_stage": result.get("failed_stage", ""),
        "exception_type": result.get("exception_type", ""),
        "missing_stage_count": result.get("missing_stage_count", 0),
        "noop_stage_count": result.get("noop_stage_count", 0),
        "not_implemented_stage_count": result.get("not_implemented_stage_count", 0),
        **{name: counters.get(name, False) for name in (
            "certificate_download_called",
            "certificate_file_rename_called",
            "file_input_send_keys_called",
            "password_input_send_keys_called",
            "certificate_upload_called",
            "device_imei_send_keys_called",
            "certificate_selection_called",
            "device_binding_save_called",
            "excel_write_called",
        )},
    }
    for key, value in fields.items():
        print(f"{key}={value}")
        logger.info(f"{key}={value}")


if __name__ == "__main__":
    raise SystemExit(main())
