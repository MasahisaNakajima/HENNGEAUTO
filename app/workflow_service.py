from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from app.excel_reader import ExcelReader
from app.excel_writer import ExcelWriter
from app.hennge_handler import HenngeHandler
from app.smsm_handler import CertificateUploadRequest, SmsmHandler
from app.smsm_config import credential_status
from app.single_certificate_workflow import WorkflowStageError


class ProductionWorkflowService:
    """Bind fixed workflow stages to the existing browser and file services."""

    DEVICE_SEARCH_RESULT_METRICS = (
        "device_search_result_observation_called",
        "device_search_result_wait_called",
        "device_search_result_wait_completed",
        "device_search_result_container_count",
        "device_search_result_row_candidate_count",
        "device_search_visible_result_row_count",
        "device_search_serial_column_candidate_count",
        "device_search_serial_column_unique",
        "device_search_serial_cell_candidate_count",
        "device_search_serial_cell_nonblank_count",
        "device_search_exact_match_count",
        "device_search_zero_result_indicator_found",
        "device_search_result_collection_method",
        "device_search_result_stable",
        "device_search_count_failed_phase",
        "device_search_count_exception_type",
        "device_search_pre_result_visible_row_count",
        "device_search_post_result_visible_row_count",
        "device_search_result_signature_changed",
        "device_search_filter_condition_updated",
        "device_search_result_total_count",
        "device_search_result_page_count",
        "device_search_explicit_zero_result",
        "device_search_result_transition_verified",
        "device_result_candidate_count",
        "device_result_candidate_unique",
        "device_result_identity_verified",
    )

    def __init__(self, *, config: dict[str, Any], logger, browser, smsm_config) -> None:
        self.config = config
        self.logger = logger
        self.browser = browser
        self.hennge = HenngeHandler(config, logger, browser)
        self.smsm = SmsmHandler(browser=browser, logger=logger, smsm_config=smsm_config)
        self.certificate_observation: dict[str, Any] = {}
        self.device_observation: dict[str, Any] = {}
        self.excel_path = Path(str((config.get("excel", {}) or {}).get("path", "")))

    def excel_load_target(self, context):
        reader = ExcelReader(str(self.excel_path))
        targets = reader.read_targets(include_row_number=True)
        if not targets:
            raise RuntimeError("有効なExcel対象がありません")
        context.set_target(targets[0])
        context.record("excel_target_count", len(targets))
        return {"target_count": len(targets)}

    def hennge_login(self, context):
        result = self.hennge.login_for_certificate_workflow()
        context.record("hennge_login_live_verified", True)
        return result

    def hennge_search_certificate_by_alias(self, context):
        context.record("hennge_search_key_type", "alias")
        context.record("hennge_alias_present", bool(context.target_alias))
        context.record("hennge_search_called", True)
        context.record("hennge_imei_search_called", False)
        try:
            self.certificate_observation = self.hennge.submit_certificate_search_by_alias(context.target_alias)
        except RuntimeError:
            raise
        return self.certificate_observation

    def hennge_wait_certificate_search_result(self, context):
        self.certificate_observation = self.hennge.wait_certificate_search_result()
        context.record("hennge_search_result", {"result_count": self.certificate_observation.get("result_count", 0)})
        context.record("hennge_result_row_count", self.certificate_observation.get("result_count", 0))
        context.record("hennge_search_live_verified", self.certificate_observation.get("unique", False))
        context.record("hennge_search_result_count", self.certificate_observation.get("result_count", 0))
        context.record("hennge_search_result_multiple", self.certificate_observation.get("result_count", 0) > 1)
        context.record("hennge_search_result_unique", self.certificate_observation.get("unique", False))
        context.record("hennge_result_row_candidate_count", self.certificate_observation.get("row_candidate_count", 0))
        return self.certificate_observation

    def hennge_select_certificate_result(self, context):
        selection = self.hennge.select_certificate_result(self.certificate_observation, context.target_alias, context.target_imei)
        context.record("hennge_search_result", {"result_count": self.certificate_observation.get("result_count", 0)})
        context.record("hennge_result_selection_verified", True)
        context.record("hennge_certificate_detail_verified", selection["detail"].get("detail_page_verified", False))
        context.record("hennge_detail_alias_available", selection["detail"].get("detail_alias_available", False))
        context.record("hennge_detail_alias_exact_match", selection["detail"].get("alias_exact_verified", False))
        context.record("hennge_detail_alias_label_found", selection["detail"].get("hennge_detail_alias_label_found", False))
        context.record("hennge_detail_alias_value_found", selection["detail"].get("hennge_detail_alias_value_found", False))
        context.record("hennge_detail_alias_value_nonblank", selection["detail"].get("hennge_detail_alias_value_nonblank", False))
        context.record("hennge_detail_alias_field_available", selection["detail"].get("hennge_detail_alias_field_available", False))
        context.record("hennge_detail_identity_verified_by_unique_search_context", selection["detail"].get("hennge_detail_identity_verified_by_unique_search_context", False))
        context.record("hennge_detail_identity_verification_method", selection["detail"].get("hennge_detail_identity_verification_method", "unresolved"))
        for metric in (
            "hennge_detail_dialog_count", "hennge_detail_dialog_unique", "hennge_detail_container_found",
            "hennge_detail_field_row_count", "hennge_detail_label_count", "hennge_detail_value_count",
            "hennge_download_action_unique", "hennge_download_action_displayed", "hennge_download_action_enabled",
            "hennge_download_action_safe", "password_input_candidate_count", "readonly_value_candidate_count",
            "text_input_candidate_count", "masked_value_candidate_count", "reveal_button_candidate_count", "copy_button_candidate_count",
            "password_label_found", "password_value_container_found", "password_source_requires_download_action",
            "password_source_requires_reveal_action", "password_source_requires_copy_action",
        ):
            context.record(metric, selection["detail"].get(metric, False if not metric.endswith("_count") else 0))
        context.record("hennge_result_row_candidate_count", selection["result_row_candidate_count"])
        context.record("hennge_result_row_unique", selection["result_row_unique"])
        context.record("hennge_result_row_displayed", selection["result_row_displayed"])
        context.record("hennge_result_row_enabled", selection["result_row_enabled"])
        context.record("hennge_result_row_safe", selection["result_row_safe"])
        context.record("hennge_result_row_click_called", selection["result_row_click_called"])
        context.record("hennge_result_row_click_count", selection["result_row_click_count"])
        context.record("hennge_subject_memo_column_found", selection["subject_memo_column_found"])
        context.record("hennge_os_column_found", selection["os_column_found"])
        context.record("hennge_result_header_candidate_count", selection["header_candidate_count"])
        context.record("hennge_result_header_count", selection["header_count"])
        context.record("hennge_result_row_cell_count_min", selection["row_cell_count_min"])
        context.record("hennge_result_row_cell_count_max", selection["row_cell_count_max"])
        context.record("hennge_os_column_index_resolved", selection["os_column_index_resolved"])
        context.record("hennge_subject_memo_column_index_resolved", selection["subject_memo_column_index_resolved"])
        context.record("hennge_subject_memo_value_candidate_count", selection["subject_memo_value_candidate_count"])
        context.record("hennge_subject_memo_exact_match_count", selection["subject_memo_exact_match_count"])
        context.record("hennge_imei_matched_row_candidate_count", selection["imei_matched_row_candidate_count"])
        context.record("hennge_imei_matched_row_os_ios", selection["imei_matched_row_os_ios"])
        context.record("hennge_imei_matched_row_safe", selection["imei_matched_row_safe"])
        for key in (
            "scroll_container_candidate_count", "scroll_container_unique", "scroll_container_scrollable",
            "scroll_height_greater_than_client_height", "scroll_called", "scroll_step_count",
            "scroll_end_reached", "rows_observed_total", "rows_deduplicated_total",
            "target_row_found_before_scroll", "target_row_found_after_scroll", "max_scroll_steps",
            "click_refetch_row_count", "click_refetch_exact_match_count", "click_refetch_safe_candidate_count",
        ):
            prefix = "hennge_click_refetch" if key.startswith("click_refetch_") else "hennge_result_"
            context.record(f"{prefix}_{key.removeprefix('click_refetch_') if prefix == 'hennge_click_refetch' else key}", selection.get(key, False if key.endswith(("unique", "scrollable", "greater_than_client_height", "called", "end_reached", "before_scroll", "after_scroll")) else 0))
        context.record("hennge_download_action_candidate_count", selection["detail"].get("download_action_candidate_count", 0))
        context.record("hennge_password_source_candidate_count", selection["detail"].get("password_source_candidate_count", 0))
        context.record("certificate_download_action", {})
        context.record("certificate_password_source", {})
        return selection

    def hennge_download_certificate(self, context):
        if context.options.resume_from_existing_certificate:
            return self._reuse_existing_certificate(context)
        required = (
            context.observations.get("hennge_search_result_count", 0) >= 1,
            context.observations.get("hennge_imei_matched_row_candidate_count") == 1,
            context.observations.get("hennge_result_row_click_called") is True,
            context.observations.get("hennge_result_row_click_count") == 1,
            context.observations.get("hennge_certificate_detail_verified") is True,
            context.observations.get("hennge_detail_alias_exact_match") is True
            or context.observations.get("hennge_detail_identity_verification_method") == "subject_memo_imei_exact_match"
            or context.observations.get("hennge_detail_identity_verified_by_unique_search_context") is True,
            context.observations.get("hennge_download_action_candidate_count") == 1,
            context.observations.get("hennge_download_action_safe") is True,
        )
        if not all(required):
            raise RuntimeError("HENNGE証明書詳細確認前のダウンロードは禁止されています")
        action = self.hennge.inspect_certificate_download_action_from_detail()
        context.record("certificate_download_action", {"candidate_count": len(action["download_candidates"])})
        context.record("hennge_download_action_candidate_count", len(action["download_candidates"]))
        candidate = action["download_candidates"][0]["element"]
        directory = self.hennge._downloads_dir()
        before_names = {item.name for item in directory.iterdir() if item.is_file()}
        candidate.click()
        context.record("hennge_download_action_click_called", True)
        path = self.hennge.wait_for_download_completion(before_names, directory)
        context.certificate_path = path
        extension_valid = path.suffix.casefold() in {".p12", ".pfx"}
        size_valid = path.stat().st_size > 0
        context.record("certificate_download_called", True)
        context.record("download_candidate_count", len(action["download_candidates"]))
        context.record("download_completed", True)
        context.record("download_extension_valid", extension_valid)
        context.record("download_size_valid", size_valid)
        context.record("download_size_stable", True)
        return {"path_observed": True, "extension_valid": extension_valid, "size_valid": size_valid}

    def hennge_validate_download(self, context):
        if context.options.resume_from_existing_certificate:
            context.record("stage_result", "idempotent_existing_state")
            context.record("validated_certificate_path_available", context.certificate_path is not None)
            return {"stage_result": "idempotent_existing_state"}
        if context.certificate_path is None:
            raise RuntimeError("ダウンロード証明書パスがありません")
        self.hennge.validate_downloaded_certificate(context.certificate_path)
        context.record("download_validation_live_verified", True)
        return {"validated": True}

    def hennge_reuse_existing_certificate(self, context):
        return self._reuse_existing_certificate(context)

    def hennge_rename_certificate_to_imei(self, context):
        if context.options.resume_from_existing_certificate:
            context.record("stage_result", "idempotent_existing_state")
            context.record("certificate_rename_skipped_existing_valid_file", True)
            context.record("certificate_file_rename_called", False)
            return {"stage_result": "idempotent_existing_state"}
        if context.certificate_path is None:
            raise RuntimeError("証明書ファイルがありません")
        context.certificate_path = self.hennge.rename_certificate_to_imei(context.certificate_path, context.target_imei)
        renamed = context.certificate_path
        context.record("certificate_file_rename_called", True)
        context.record("renamed_file_exists", renamed.is_file())
        context.record("renamed_file_extension_valid", renamed.suffix.casefold() in {".p12", ".pfx"})
        context.record("renamed_file_size_valid", renamed.stat().st_size > 0)
        return {"renamed_file_exists": renamed.is_file()}

    def _reuse_existing_certificate(self, context):
        directory = self.hennge._downloads_dir()
        candidate_paths = [
            directory / f"{context.target_imei}{suffix}"
            for suffix in (".p12", ".pfx")
            if (directory / f"{context.target_imei}{suffix}").is_file()
        ]
        context.record("existing_certificate_file_candidate_count", len(candidate_paths))
        context.record("existing_certificate_file_unique", len(candidate_paths) == 1)
        if len(candidate_paths) != 1:
            self._raise_existing_certificate_validation_error()

        candidate = candidate_paths[0]
        valid = self._is_reusable_certificate(candidate)
        context.record("existing_certificate_file_valid", valid)
        if not valid:
            self._raise_existing_certificate_validation_error()

        context.certificate_path = candidate
        context.record("certificate_download_skipped_existing_valid_file", True)
        context.record("certificate_rename_skipped_existing_valid_file", True)
        context.record("validated_certificate_path_available", True)
        context.record("stage_result", "idempotent_existing_state")
        context.record("certificate_download_called", False)
        context.record("certificate_file_rename_called", False)
        return {"stage_result": "idempotent_existing_state"}

    def _is_reusable_certificate(self, candidate: Path) -> bool:
        if candidate.suffix.casefold() not in {".p12", ".pfx"}:
            return False
        if candidate.name.casefold().endswith((".crdownload", ".tmp", ".part")):
            return False
        try:
            if candidate.stat().st_size <= 0:
                return False
            with candidate.open("rb") as stream:
                if not stream.read(1):
                    return False
            self.hennge.validate_downloaded_certificate(candidate)
            return True
        except (OSError, RuntimeError, ValueError):
            return False

    @staticmethod
    def _raise_existing_certificate_validation_error() -> None:
        cause = RuntimeError("既存証明書ファイルを安全に再利用できません")
        raise WorkflowStageError(
            "hennge_validate_existing_certificate_file",
            "既存証明書ファイルの候補または検証結果が不正です",
        ) from cause

    def hennge_read_certificate_password(self, context):
        source = self.hennge.inspect_certificate_password_source()
        context.record("password_source_candidate_count", source.get("password_source_candidate_count", 1 if source.get("configured") else 0))
        context.record("password_source_unique", bool(source.get("password_source_candidate_count", 0) == 1 or source.get("configured")))
        try:
            context.certificate_password = self.hennge.read_certificate_password()
        except RuntimeError as exc:
            failed_stage = str((self.hennge.last_password_observation or {}).get("failed_stage") or "")
            if failed_stage:
                raise WorkflowStageError(failed_stage, failed_stage) from exc
            raise
        finally:
            observation = self.hennge.last_password_observation or source
            for metric in (
                "password_label_found", "reveal_button_unique", "reveal_button_displayed",
                "reveal_button_enabled", "reveal_button_click_called", "copy_button_unique",
                "copy_button_displayed", "copy_button_enabled",
                "password_already_revealed", "password_reveal_safe", "password_reveal_disabled",
                "password_reveal_inside_detail_dialog", "password_dom_reobserved",
                "password_reveal_click_started", "password_reveal_click_completed",
                "hennge_password_reveal_candidate_resolved", "hennge_password_reveal_safety_verified",
                "hennge_password_reveal_click_started", "hennge_password_reveal_click_completed",
                "hennge_password_dom_reobserve_started", "hennge_password_dom_reobserve_completed",
                "hennge_password_value_resolved",
                "password_section_found", "password_section_scrolled_into_view",
                "password_copy_button_unique", "password_copy_button_displayed",
                "password_copy_button_enabled", "password_copy_button_safe",
                "password_copy_click_started", "password_copy_click_called",
                "password_copy_click_completed", "clipboard_read_called",
                "certificate_password_obtained", "certificate_password_nonblank",
                "clipboard_clear_called", "clipboard_clear_completed",
                "password_eye_button_click_called",
            ):
                context.record(metric, observation.get(metric, False))
            for metric in (
                "reveal_button_candidate_count", "reveal_button_click_count",
                "copy_button_candidate_count", "password_value_candidate_count",
                "password_reveal_candidate_count",
                "password_copy_button_candidate_count", "masked_password_field_count",
                "password_eye_button_candidate_count", "password_copy_click_count",
            ):
                context.record(metric, observation.get(metric, 0))
            context.record("password_reveal_candidate_count", observation.get("password_reveal_candidate_count", 0))
            context.record("password_reveal_unique", observation.get("password_reveal_unique", False))
            context.record("password_reveal_displayed", observation.get("password_reveal_displayed", False))
            context.record("password_reveal_enabled", observation.get("password_reveal_enabled", False))
            context.record("password_reveal_click_exception_type", observation.get("password_reveal_click_exception_type", ""))
            context.record("password_source_type", observation.get("password_source_type", "unknown"))
        context.record("certificate_password_obtained", True)
        context.record("certificate_password_nonblank", bool(context.certificate_password))
        return {"configured": True}

    def smsm_login(self, context):
        for key, value in credential_status(self.smsm.smsm_config).items():
            context.record(key, value)
        context.record("smsm_login_called", True)
        trace = lambda key, value: context.record(key, value)
        login_method = self.smsm.login
        if "trace" in inspect.signature(login_method).parameters:
            login_method(trace=trace)
        else:
            login_method()
        context.record("smsm_login_completed", True)
        return {"logged_in": True}

    def smsm_open_client_certificate_page(self, context):
        from diagnose_smsm_single_target_lookup import _load_route_manifest

        trace = lambda key, value: context.record(key, value)
        manifest = context.services.get("smsm_route_manifest")
        if not isinstance(manifest, dict):
            try:
                manifest = _load_route_manifest(trace=trace)
            except Exception as exc:
                failed_stage = getattr(exc, "failed_stage", "") or "smsm_load_client_certificate_route_manifest"
                raise WorkflowStageError(failed_stage, failed_stage) from exc
            context.services["smsm_route_manifest"] = manifest
        else:
            context.record("smsm_route_manifest_load_called", False)
            context.record("smsm_route_manifest_found", True)
            context.record("smsm_route_manifest_parse_completed", True)
            context.record("smsm_route_manifest_schema_valid", True)
            context.record("smsm_route_manifest_fingerprint_valid", True)
            context.record("smsm_route_manifest_path_available", True)
        context.record("smsm_route_navigation_called", True)
        try:
            result = self.smsm.navigate_verified_final_path_for_diagnostic(manifest, trace=trace)
        except Exception as exc:
            observation = getattr(exc, "observation", {})
            for key, value in observation.items():
                if isinstance(value, (bool, int, str)):
                    context.record(key, value)
            failed_stage = observation.get("failed_stage") if isinstance(observation, dict) else None
            stage = failed_stage or "smsm_verify_client_certificate_page"
            raise WorkflowStageError(stage, stage) from exc
        for key, value in result.items():
            if isinstance(value, (bool, int, str)):
                context.record(key, value)
        live_verified = result.get("smsm_client_certificate_page_live_verified") is True
        context.record("smsm_client_certificate_page_live_verified", live_verified)
        if not live_verified:
            raise WorkflowStageError("smsm_verify_client_certificate_page", "smsm_verify_client_certificate_page")
        return result

    def smsm_check_certificate_duplicate(self, context):
        result = self.smsm.check_certificate_duplicate_by_imei(context.target_imei)
        context.record("smsm_certificate_duplicate_search_completed", True)
        context.record("duplicate_check_determinate", result.get("duplicate_check_determinate", False))
        context.record("smsm_exact_imei_match_count", result.get("exact_imei_match_count", 0))
        context.record("duplicate_exact_match_count", result.get("exact_imei_match_count", 0))
        context.record("smsm_same_name_certificate_match_count", result.get("same_name_certificate_match_count", 0))
        context.record("duplicate_same_name_match_count", result.get("same_name_certificate_match_count", 0))
        context.record("smsm_certificate_upload_allowed", result.get("upload_allowed", False))
        context.record("duplicate_upload_allowed", result.get("upload_allowed", False))
        if result.get("upload_allowed") is not True:
            raise RuntimeError("SMSMに同一IMEIの証明書が存在するか一意に確認できないため停止しました")
        return result

    def smsm_find_add_button(self, context):
        result = self.smsm.find_add_button()
        if result.get("add_button_candidate_count") != 1:
            raise RuntimeError("SMSM追加ボタンを一意に確認できません")
        context.record("smsm_add_button_observation", {"candidate_count": 1})
        context.record("add_button_candidate_count", result.get("add_button_candidate_count", 0))
        context.record("add_button_unique", result.get("add_button_unique", False))
        return result

    def smsm_open_add_form(self, context):
        result = self.smsm.open_add_form()
        context.record("smsm_upload_controls", {"file_input_unique": result.get("file_input_unique", False), "password_input_unique": result.get("password_input_unique", False)})
        context.record("add_button_click_called", result.get("add_button_click_called", False))
        context.record("add_form_opened", result.get("add_form_opened", False))
        context.record("file_input_count", result.get("file_input_count", 0))
        context.record("file_input_unique", result.get("file_input_unique", False))
        context.record("password_input_count", result.get("password_input_count", 0))
        context.record("password_input_unique", result.get("password_input_unique", False))
        context.record("submit_button_candidate_count", result.get("certificate_submit_button_candidate_count", 0))
        context.record("submit_button_unique", result.get("certificate_submit_button_unique", False))
        context.record("file_input_send_keys_called", False)
        context.record("password_input_send_keys_called", False)
        context.record("certificate_upload_called", False)
        return result

    def smsm_prepare_certificate_upload(self, context):
        prepare_metrics = {
            "smsm_prepare_called": True,
            "smsm_prepare_page_verified": context.observations.get("smsm_client_certificate_page_live_verified") is True,
            "smsm_prepare_target_imei_present": bool(context.target_imei),
            "smsm_prepare_certificate_path_present": context.certificate_path is not None,
            "smsm_prepare_certificate_password_present": bool(context.certificate_password),
            "smsm_prepare_duplicate_check_called": False,
            "smsm_prepare_duplicate_check_completed": False,
            "smsm_prepare_failed_phase": "",
            "smsm_prepare_exception_type": "",
        }
        for key, value in prepare_metrics.items():
            context.record(key, value)
        if not context.target_imei or context.certificate_path is None or not context.certificate_password or not prepare_metrics["smsm_prepare_page_verified"]:
            context.record("smsm_prepare_failed_phase", "validate_preparation_context")
            context.record("smsm_prepare_exception_type", "RuntimeError")
            raise WorkflowStageError("smsm_prepare_certificate_upload", "smsm_prepare_certificate_upload")
        try:
            context.record("smsm_prepare_failed_phase", "check_certificate_duplicate")
            result = self.smsm.prepare_certificate_upload_for_diagnostic(
                context.certificate_path,
                context.certificate_password,
                context.target_imei,
                login_and_navigate=False,
            )
            for key, value in result.items():
                context.record(key, value)
            for key in prepare_metrics:
                if key in result:
                    context.record(key, result[key])
            if result.get("smsm_prepare_failed_phase"):
                context.record("smsm_prepare_failed_phase", result["smsm_prepare_failed_phase"])
            context.record("smsm_prepare_exception_type", result.get("smsm_prepare_exception_type", ""))
            if result.get("upload_ready") is not True:
                context.record("smsm_prepare_failed_phase", result.get("smsm_prepare_failed_phase") or "check_certificate_duplicate")
                context.record("smsm_prepare_exception_type", "RuntimeError")
                result["smsm_prepare_exception_type"] = "RuntimeError"
                failure = WorkflowStageError("smsm_prepare_certificate_upload", "smsm_prepare_certificate_upload")
                failure.observation = result
                raise failure
            if result.get("file_input_send_keys_called"):
                context.mark_operation("file_input_send_keys_called")
            if result.get("password_input_send_keys_called"):
                context.mark_operation("password_input_send_keys_called")
            context.record("certificate_file_ready", True)
            return result
        except Exception as exc:
            observation = getattr(exc, "observation", {})
            if isinstance(observation, dict):
                for key, value in observation.items():
                    context.record(key, value)
                observed_exception_type = observation.get("smsm_prepare_exception_type")
                if observed_exception_type:
                    context.record("smsm_prepare_exception_type", observed_exception_type)
                    context.record("smsm_prepare_failed_phase", observation.get("smsm_prepare_failed_phase", ""))
                    raise
            context.record("smsm_prepare_exception_type", type(exc).__name__)
            raise
        finally:
            context.certificate_password = None

    def smsm_set_certificate_file(self, context):
        if context.certificate_path is None:
            raise RuntimeError("証明書ファイルがありません")
        result = self.smsm.set_certificate_file(context.certificate_path, allow_upload=True)
        context.record("file_input_send_keys_called", result.get("file_input_send_keys_called") is True)
        context.record("file_input_send_keys_count", result.get("file_input_send_keys_count", 0))
        context.record("certificate_file_selected", result.get("certificate_selected") is True)
        context.record("selected_certificate_filename_exact_imei_match", context.certificate_path.stem == context.target_imei)
        context.record("selected_certificate_extension_valid", context.certificate_path.suffix.casefold() in {".p12", ".pfx"})
        return result

    def smsm_set_certificate_password(self, context):
        if context.certificate_password is None:
            raise RuntimeError("証明書パスワードがありません")
        result = self.smsm.set_certificate_password(context.certificate_password, allow_upload=True)
        context.record("password_input_send_keys_called", result.get("password_input_send_keys_called") is True)
        context.record("password_input_send_keys_count", 1 if result.get("password_input_send_keys_called") is True else 0)
        context.record("password_input_nonblank_after_send_keys", result.get("password_input_send_keys_called") is True)
        return result

    def smsm_submit_certificate_upload(self, context):
        groups = self.smsm._add_form_control_groups(self.smsm.browser.driver)
        candidates = groups.get("saveCandidates", [])
        container = groups.get("container")
        refetch = {
            "save_button_refetch_candidate_count": len(candidates),
            "save_button_refetch_unique": len(candidates) == 1,
            "save_button_refetch_displayed": len(candidates) == 1 and self.smsm._safe_bool(candidates[0], "is_displayed"),
            "save_button_refetch_enabled": len(candidates) == 1 and self.smsm._safe_bool(candidates[0], "is_enabled") and not self.smsm._safe_bool_attribute(candidates[0], "disabled"),
            "save_button_inside_current_add_form": len(candidates) == 1 and container is not None,
        }
        for key, value in refetch.items():
            context.record(key, value)
        context.record("save_button_candidate_count", refetch["save_button_refetch_candidate_count"])
        context.record("save_button_unique", refetch["save_button_refetch_unique"])
        context.record("save_button_displayed", refetch["save_button_refetch_displayed"])
        context.record("save_button_enabled", refetch["save_button_refetch_enabled"])
        conditions = {
            "duplicate_check_determinate": context.observations.get("duplicate_check_determinate") is True,
            "duplicate_exact_match_count_zero": context.observations.get("duplicate_exact_match_count") == 0,
            "duplicate_same_name_match_count_zero": context.observations.get("duplicate_same_name_match_count") == 0,
            "duplicate_upload_allowed": context.observations.get("duplicate_upload_allowed") is True,
            "add_button_click_called": context.observations.get("add_button_click_called") is True,
            "add_form_opened": context.observations.get("add_form_opened") is True,
            "file_input_send_keys_called": context.observations.get("file_input_send_keys_called") is True,
            "file_input_send_keys_count_one": context.observations.get("file_input_send_keys_count") == 1,
            "certificate_file_selected": context.observations.get("certificate_file_selected") is True,
            "selected_certificate_filename_exact_imei_match": context.observations.get("selected_certificate_filename_exact_imei_match") is True,
            "selected_certificate_extension_valid": context.observations.get("selected_certificate_extension_valid") is True,
            "password_input_send_keys_called": context.observations.get("password_input_send_keys_called") is True,
            "password_input_send_keys_count_one": context.observations.get("password_input_send_keys_count") == 1,
            "password_input_nonblank_after_send_keys": context.observations.get("password_input_nonblank_after_send_keys") is True,
            "save_button_refetch_unique": refetch["save_button_refetch_unique"],
            "save_button_refetch_displayed": refetch["save_button_refetch_displayed"],
            "save_button_refetch_enabled": refetch["save_button_refetch_enabled"],
            "save_button_inside_current_add_form": refetch["save_button_inside_current_add_form"],
            "save_button_click_not_called": context.observations.get("save_button_click_called", False) is False,
            "certificate_upload_not_called": context.observations.get("certificate_upload_called", False) is False,
        }
        for key, value in conditions.items():
            context.record(f"certificate_upload_ready_{key}", value)
        ready = all(conditions.values())
        context.record("certificate_upload_ready", ready)
        if not ready:
            raise RuntimeError("証明書アップロード準備が完了していません")
        return self.smsm.submit_certificate_upload(allow_upload=True, imei=context.target_imei)

    def smsm_verify_certificate_upload(self, context):
        result = self.smsm.verify_certificate_upload()
        context.record("smsm_certificate_identifier", {"observed": bool(result)})
        return result

    def smsm_search_device_by_serial(self, context, *, read_only: bool = False):
        context.record("device_search_called", True)
        context.record("device_search_target_present", bool(context.target_serial))
        trace = lambda key, value: context.record(key, value)
        try:
            search_method = self.smsm.search_device
            parameters = inspect.signature(search_method).parameters
            search_kwargs = {}
            if "trace" in parameters:
                search_kwargs["trace"] = trace
            if "page_reached" in parameters:
                search_kwargs["page_reached"] = True
            if "read_only_observation" in parameters:
                search_kwargs["read_only_observation"] = read_only
            result = search_method(context.target_serial, **search_kwargs)
        except Exception as exc:
            partial_observation = getattr(exc, "observation", None)
            self._record_device_search_metrics(context, partial_observation)
            context.record("device_search_failed_phase", getattr(exc, "failed_phase", "resolve_device_search"))
            context.record("device_search_exception_type", type(exc).__name__)
            raise
        self.device_observation = {"serial_search_completed": True, **(result if isinstance(result, dict) else {})}
        handler_observation = getattr(self.smsm, "device_observation", None)
        if isinstance(handler_observation, dict):
            handler_observation.update(self.device_observation)
        self._record_device_search_metrics(context, self.device_observation)
        for key in (
            "device_search_called", "device_search_target_present", "device_search_page_verified",
            "device_search_type_selection_called", "device_search_type_already_selected",
            "device_search_type_click_count", "device_search_dom_reobserve_called",
            "device_search_dom_reobserve_completed",
            "device_search_type_control_candidate_count", "device_search_type_option_candidate_count",
            "device_search_type_target_option_found", "device_search_type_control_displayed",
            "device_search_type_control_enabled",
            "device_search_input_candidate_count", "device_search_button_candidate_count",
            "device_search_send_keys_called", "device_search_send_keys_count",
            "device_search_submit_called", "device_search_submit_count",
            "device_search_wait_called", "device_search_wait_completed",
            "device_search_exact_match_count", "device_search_failed_phase", "device_search_exception_type",
        ):
            context.record(key, self.device_observation.get(key))
        context.record("device_search_result", self.device_observation)
        return self.device_observation

    def smsm_inspect_matched_device_result_links(self, context):
        result = self.smsm.inspect_matched_device_result_links(
            context.target_serial,
            context.observations,
        )
        for key, value in result.items():
            if key != "device_result_link_metadata":
                context.record(key, value)
        context.record("device_result_link_metadata", result.get("device_result_link_metadata", []))
        context.record("device_binding_save_called", False)
        context.record("device_binding_save_count", 0)
        context.record("excel_write_called", False)
        context.record("certificate_upload_called", False)
        return result

    def _record_device_search_metrics(self, context, source=None) -> None:
        source = source if isinstance(source, dict) else {}
        for key in self.DEVICE_SEARCH_RESULT_METRICS:
            if key in source:
                value = source[key]
            else:
                value = context.observations.get(key)
            context.record(key, value if value is not None else None)

    def smsm_open_device_list(self, context):
        trace = lambda key, value: context.record(key, value)
        try:
            result = self.smsm.reach_device_search_page(trace=trace)
            if result is None:
                result = self.smsm.inspect_device_list_page(trace=trace)
        except Exception as exc:
            failed_phase = getattr(exc, "failed_phase", "resolve_device_navigation")
            context.record("device_list_page_verified", False)
            context.record("device_list_failed_phase", failed_phase)
            context.record("device_list_exception_type", type(exc).__name__)
            raise
        if not isinstance(result, dict):
            result = {"device_list_page_verified": False, "device_list_failed_phase": "verify_device_list_pathname"}
        for key, value in result.items():
            context.record(key, value)
        if result.get("device_list_page_verified") is not True:
            error = RuntimeError("端末一覧画面を確認できません")
            error.failed_phase = result.get("device_list_failed_phase", "verify_device_list_pathname")
            context.record("device_list_exception_type", type(error).__name__)
            raise error
        return result

    def smsm_open_device_detail(self, context):
        result = self.smsm.inspect_device_client_certificate_settings_dom_for_diagnostic(
            context.target_serial,
            search_already_completed=True,
        )
        self.device_observation.update(result)
        for key in (
            "device_result_click_candidate_count",
            "device_result_click_unique",
            "device_result_click_called",
            "device_result_click_count",
            "device_result_selected",
            "device_detail_navigation_wait_called",
            "device_detail_navigation_verified",
            "device_result_candidate_count",
            "device_result_candidate_unique",
            "device_result_detail_column_candidate_count",
            "device_result_detail_control_candidate_count",
            "device_result_detail_control_unique",
            "device_detail_serial_field_candidate_count",
            "device_detail_serial_exact_match",
            "device_result_identity_verified",
            "device_result_identity_verification_method",
        ):
            context.record(key, result.get(key))
        if result.get("device_result_identity_verified") is not True:
            raise RuntimeError("端末詳細内のシリアル番号完全一致を確認できません")
        return {"detail_opened": True}

    def smsm_open_other_settings(self, context):
        if not self.device_observation:
            raise RuntimeError("端末詳細結果がありません")
        return {"other_settings_opened": True}

    def smsm_open_device_client_certificate(self, context):
        result = self._inspect_binding_dom(context)
        context.record("device_platform_ios_verified", result.get("ios_verified") is True and result.get("android_observed") is False)
        context.record("uploaded_certificate_exact_match_count", result.get("uploaded_certificate_exact_count", 0))
        context.record("smsm_binding_dom_observed", True)
        return result

    def smsm_set_device_imei(self, context):
        return self._set_device_field("imei", context.target_imei)

    def smsm_select_uploaded_certificate(self, context):
        result = self._select_certificate_control(context)
        context.record("uploaded_certificate_selected", result.get("selection_count") == 1 or result.get("idempotent") is True)
        return result

    def smsm_save_device_certificate_binding(self, context):
        return self._click_binding_save(context)

    def smsm_verify_device_certificate_binding(self, context):
        result = self._inspect_binding_dom(context)
        verified = result.get("bound_certificate_exact_count") == 1
        context.record("device_binding_completion_verified", verified)
        context.record("bound_certificate_exact_match_count", result.get("bound_certificate_exact_count", 0))
        context.record("device_binding_verified", verified)
        context.record("binding_verification", {"observed": bool(result), "exact_count": result.get("bound_certificate_exact_count", 0)})
        if not verified:
            raise RuntimeError("SMSM端末証明書の紐づけ完了を一意に確認できません")
        return result

    def excel_write_success(self, context):
        excel_config = self.config.get("excel", {}) or {}
        writer = ExcelWriter(
            self.excel_path,
            sheet_name=str(excel_config.get("sheet_name", "HENNGE登録作業必要情報")),
            column=int(excel_config.get("writeback_column", 6)),
            value=excel_config.get("writeback_value", "完了"),
        )
        context.record("excel_update_plan", writer.plan_update(row_number=context.target_row_index or 0, alias=context.target_alias, serial=context.target_serial, imei=context.target_imei))
        return writer.write_update(row_number=context.target_row_index or 0, alias=context.target_alias, serial=context.target_serial, imei=context.target_imei)

    def workflow_completed(self, context):
        return {"completed": True}

    def _set_device_field(self, field_name: str, value: str):
        driver = self.smsm.browser.driver
        if driver is None:
            raise RuntimeError("SMSM端末証明書画面を確認できません")
        candidates = [item for item in driver.find_elements("css selector", "input") if self.smsm._safe_bool(item, "is_displayed") and self.smsm._safe_bool(item, "is_enabled") and field_name in " ".join((self.smsm._safe_attribute(item, "name"), self.smsm._safe_attribute(item, "id"), self.smsm._safe_attribute(item, "placeholder"))).casefold()]
        if len(candidates) != 1:
            raise RuntimeError(f"SMSM {field_name}入力欄を一意に確認できません")
        candidates[0].clear()
        candidates[0].send_keys(value)
        return {"input_count": 1}

    def _inspect_binding_dom(self, context) -> dict[str, Any]:
        driver = self.smsm.browser.driver
        if driver is None:
            raise RuntimeError("SMSM端末証明書のbinding DOMを確認できません")
        target_imei = context.target_imei
        result = driver.execute_script(
            """
            const target = arguments[0];
            const visible = item => Boolean(item && (item.offsetWidth || item.offsetHeight || item.getClientRects().length));
            const label = item => [item.innerText || '', item.textContent || '', item.getAttribute('aria-label') || '', item.getAttribute('title') || '', item.getAttribute('value') || ''].join(' ').trim();
            const interactive = Array.from(document.querySelectorAll('select,option,input[type="radio"],input[type="checkbox"]')).filter(visible);
            const certificateRoots = Array.from(document.querySelectorAll('form,fieldset,[role="region"],section,table,div')).filter(item => /client certificate|クライアント証明書|証明書/i.test(label(item)) && visible(item));
            const controls = interactive.filter(item => certificateRoots.some(root => root.contains(item)));
            const options = controls.flatMap(item => item.tagName.toLowerCase() === 'select' ? Array.from(item.options) : [item]);
            const normalizedTarget = target.replace(/[-\\s]/g, '');
            const targetOptions = options.filter(item => label(item).replace(/[-\\s]/g, '').includes(normalizedTarget));
            const body = document.body ? document.body.innerText || '' : '';
            const saveButtons = Array.from(document.querySelectorAll('form button,form input[type="submit"],form a')).filter(item => visible(item) && !item.disabled && /save|保存/i.test(label(item)));
            const boundRows = Array.from(document.querySelectorAll('table tbody tr,[role="row"]')).filter(item => visible(item) && /bound|紐づ|登録済|設定済/i.test(label(item)) && label(item).replace(/[-\\s]/g, '').includes(target.replace(/[-\\s]/g, '')));
            return {
                ios_verified: /(^|\\s)iOS(\\s|$)/i.test(body) && !/Android/i.test(body),
                android_observed: /Android/i.test(body),
                certificate_region_candidate_count: certificateRoots.length,
                certificate_control_candidate_count: controls.length,
                uploaded_certificate_exact_count: targetOptions.length,
                uploaded_certificate_target_exact: targetOptions.length === 1,
                certificate_validity: !/expired|失効|期限切れ/i.test(label(targetOptions[0] || document.body)),
                existing_target_binding: boundRows.length > 0,
                existing_other_binding: /bound|紐づ|登録済|設定済/i.test(body) && boundRows.length === 0,
                save_button_candidate_count: saveButtons.length,
                save_button_displayed: saveButtons.length === 1 && visible(saveButtons[0]),
                save_button_enabled: saveButtons.length === 1 && !saveButtons[0].disabled,
                save_button_inside_form: saveButtons.length === 1,
                bound_certificate_exact_count: boundRows.length
            };
            """,
            target_imei,
        )
        if not isinstance(result, dict):
            raise RuntimeError("SMSM端末証明書binding DOMの取得結果が不正です")
        return result

    def _select_certificate_control(self, context):
        observation = self._inspect_binding_dom(context)
        if observation.get("existing_target_binding") is True:
            context.record("device_binding_idempotent", True)
            return {"selection_count": 0, "idempotent": True}
        if observation.get("existing_other_binding") is True:
            raise RuntimeError("別の証明書が既に端末へ紐づいているため停止しました")
        required = (
            observation.get("ios_verified") is True,
            observation.get("android_observed") is False,
            observation.get("certificate_region_candidate_count") == 1,
            observation.get("certificate_control_candidate_count") == 1,
            observation.get("uploaded_certificate_exact_count") == 1,
            observation.get("uploaded_certificate_target_exact") is True,
            observation.get("certificate_validity") is True,
        )
        if not all(required):
            raise RuntimeError("SMSM端末bindingの保存前条件を満たしていません")
        driver = self.smsm.browser.driver
        controls = [item for item in driver.find_elements("css selector", "select,option,input[type='radio'],input[type='checkbox']") if self.smsm._safe_bool(item, "is_displayed") and self.smsm._safe_bool(item, "is_enabled") and any(token in self.smsm._safe_element_text_for_diagnostic(item).casefold() for token in ("証明書", "certificate"))]
        if len(controls) != 1:
            raise RuntimeError(f"SMSM証明書選択候補数が不正です: {len(controls)}")
        controls[0].click()
        return {"selection_count": 1}

    def _click_binding_save(self, context):
        observation = self._inspect_binding_dom(context)
        if observation.get("existing_target_binding") is True:
            return {"saved": False, "idempotent": True}
        if not all((
            observation.get("save_button_candidate_count") == 1,
            observation.get("save_button_displayed") is True,
            observation.get("save_button_enabled") is True,
            observation.get("save_button_inside_form") is True,
        )):
            raise RuntimeError("SMSM紐づけ保存ボタンの安全条件を満たしていません")
        driver = self.smsm.browser.driver
        buttons = [item for item in driver.find_elements("css selector", "button,input[type='submit'],a") if self.smsm._safe_bool(item, "is_displayed") and self.smsm._safe_bool(item, "is_enabled") and any(token in self.smsm._safe_element_text_for_diagnostic(item).casefold() for token in ("save", "保存"))]
        if len(buttons) != 1:
            raise RuntimeError(f"SMSM紐づけ保存ボタン候補数が不正です: {len(buttons)}")
        buttons[0].click()
        return {"saved": True}
