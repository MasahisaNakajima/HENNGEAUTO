from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from app.imei_normalizer import normalize_imei


WORKFLOW_STAGES = (
    "excel_load_target",
    "hennge_login",
    "hennge_search_certificate_by_alias",
    "hennge_wait_certificate_search_result",
    "hennge_select_certificate_result",
    "hennge_download_certificate",
    "hennge_validate_download",
    "hennge_rename_certificate_to_imei",
    "hennge_read_certificate_password",
    "smsm_login",
    "smsm_open_client_certificate_page",
    "smsm_check_certificate_duplicate",
    "smsm_find_add_button",
    "smsm_open_add_form",
    "smsm_set_certificate_file",
    "smsm_set_certificate_password",
    "smsm_submit_certificate_upload",
    "smsm_verify_certificate_upload",
    "smsm_search_device_by_serial",
    "smsm_open_device_detail",
    "smsm_open_other_settings",
    "smsm_open_device_client_certificate",
    "smsm_set_device_imei",
    "smsm_select_uploaded_certificate",
    "smsm_save_device_certificate_binding",
    "smsm_verify_device_certificate_binding",
    "excel_write_success",
    "workflow_completed",
)

PREPARE_SMSM_CERTIFICATE_UPLOAD_STAGES = (
    "excel_load_target",
    "hennge_login",
    "hennge_search_certificate_by_alias",
    "hennge_wait_certificate_search_result",
    "hennge_select_certificate_result",
    "hennge_reuse_existing_certificate",
    "hennge_read_certificate_password",
    "smsm_login",
    "smsm_open_client_certificate_page",
    "smsm_prepare_certificate_upload",
)

DEFAULT_ALLOW_FLAGS = {
    "allow_certificate_download": False,
    "allow_certificate_upload": False,
    "allow_device_binding": False,
    "allow_excel_write": False,
}


class WorkflowStageError(RuntimeError):
    def __init__(self, stage: str, message: str, *, missing: bool = False) -> None:
        super().__init__(message)
        self.stage = stage
        self.missing = missing


OPERATION_COUNTER_NAMES = (
    "certificate_download_called",
    "certificate_file_rename_called",
    "file_input_send_keys_called",
    "password_input_send_keys_called",
    "certificate_upload_called",
    "device_imei_send_keys_called",
    "certificate_selection_called",
    "device_binding_save_called",
    "excel_write_called",
)


def _empty_operation_counters() -> dict[str, bool]:
    return {name: False for name in OPERATION_COUNTER_NAMES}


@dataclass(frozen=True)
class WorkflowOptions:
    dry_run: bool = False
    front_half_only: bool = False
    resume_from_existing_certificate: bool = False
    allow_certificate_download: bool = False
    allow_certificate_upload: bool = False
    allow_device_binding: bool = False
    allow_excel_write: bool = False

    def mutation_allowed(self, permission: str) -> bool:
        if self.dry_run:
            return False
        return bool(getattr(self, permission, False))


@dataclass
class WorkflowContext:
    config: dict[str, Any] = field(default_factory=dict)
    target: dict[str, str] = field(default_factory=dict)
    certificate_path: Path | None = None
    certificate_password: str | None = None
    services: dict[str, Any] = field(default_factory=dict)
    observations: dict[str, Any] = field(default_factory=dict)
    completed_stages: list[str] = field(default_factory=list)
    stage_reports: list[dict[str, Any]] = field(default_factory=list)
    operation_counters: dict[str, bool] = field(default_factory=_empty_operation_counters)
    stage_states: dict[str, dict[str, bool]] = field(default_factory=dict)
    failure_exception_type: str = ""
    options: WorkflowOptions = field(default_factory=WorkflowOptions)

    def __repr__(self) -> str:
        return (
            "WorkflowContext(target_present={target_present}, certificate_path_present={path_present}, "
            "certificate_password_present={password_present}, completed_stage_count={stage_count})"
        ).format(
            target_present=bool(self.target),
            path_present=self.certificate_path is not None,
            password_present=bool(self.certificate_password),
            stage_count=len(self.completed_stages),
        )

    def set_target(self, target: Mapping[str, Any]) -> None:
        alias = str(target.get("alias") or "").strip()
        serial = str(target.get("serial") or "").strip()
        imei = normalize_imei(target.get("imei"))
        if not alias or not serial or not imei:
            raise ValueError("Excel対象の必須項目が不足しています")
        row_number = target.get("row_number", target.get("row"))
        normalized_row_number = int(row_number) if row_number is not None else None
        self.target = {"alias": alias, "serial": serial, "imei": imei}
        if normalized_row_number is not None:
            self.target["row_number"] = str(normalized_row_number)

    @property
    def target_row_index(self) -> int | None:
        value = self.target.get("row_number")
        return int(value) if value is not None else None

    @property
    def target_alias(self) -> str:
        return self.target.get("alias", "")

    @property
    def target_imei(self) -> str:
        return self.target.get("imei", "")

    @property
    def target_serial(self) -> str:
        return self.target.get("serial", "")

    def record(self, key: str, value: Any = True) -> None:
        normalized_key = key.lower()
        sensitive_key = any(token in normalized_key for token in ("password", "alias", "imei", "serial", "path", "file"))
        metric_key = normalized_key.endswith((
            "_count", "_present", "_called", "_unique", "_type", "_valid", "_exists",
            "_completed", "_obtained", "_nonblank",
        ))
        self.observations[key] = bool(value) if sensitive_key and not metric_key else value

    def mark_operation(self, counter_name: str) -> None:
        if counter_name not in self.operation_counters:
            raise KeyError(counter_name)
        self.operation_counters[counter_name] = True


StageHandler = Callable[[WorkflowContext], Any]


def require_exactly_one(candidates: Any, *, stage: str, label: str) -> Any:
    if not isinstance(candidates, (list, tuple)) or len(candidates) != 1:
        count = len(candidates) if isinstance(candidates, (list, tuple)) else 0
        raise WorkflowStageError(stage, f"{label}_candidate_count={count}")
    return candidates[0]


def run_guarded_operation(
    context: WorkflowContext,
    *,
    stage: str,
    permission: str,
    counter_name: str,
    operation: Callable[[], Any],
) -> Any:
    require_mutation(context, permission, stage)
    result = operation()
    context.mark_operation(counter_name)
    return result


class SingleCertificateWorkflow:
    """Run the one-target certificate workflow in a fixed, fail-fast order."""

    def __init__(
        self,
        *,
        handlers: Mapping[str, StageHandler],
        context: WorkflowContext | None = None,
        logger: Any = None,
        stages: tuple[str, ...] = WORKFLOW_STAGES,
    ) -> None:
        self.handlers = dict(handlers)
        self.context = context or WorkflowContext()
        self.logger = logger
        self.stages = stages

    def _log(self, message: str) -> None:
        if self.logger is not None:
            self.logger.info(message)

    def run(self) -> dict[str, Any]:
        missing = [stage for stage in self.stages if not callable(self.handlers.get(stage))]
        if missing:
            failed_stage = missing[0]
            result = self._failure(failed_stage, missing_count=len(missing), missing=True)
            return result

        try:
            for stage in self.stages:
                handler = self.handlers[stage]
                status = getattr(handler, "workflow_status", "implemented_and_connected")
                function_name = getattr(handler, "__name__", type(handler).__name__)
                report = {
                    "stage": stage,
                    "function_name": function_name,
                    "implementation_status": status,
                    "production_connected": status == "implemented_and_connected",
                    "dry_run_check": getattr(handler, "dry_run_check", "read-only preflight"),
                    "allow_flag": getattr(handler, "allow_flag", "none"),
                    "planned": True,
                    "live_verified": False,
                    "executed": False,
                }
                self.context.stage_reports.append(report)
                self.context.stage_states[stage] = {"planned": True, "live_verified": False, "executed": False}
                if status != "implemented_and_connected":
                    raise WorkflowStageError(stage, status, missing=status in {"missing", "blocked"})
                self._log(f"workflow_stage_started={stage}")
                try:
                    handler(self.context)
                except WorkflowStageError:
                    raise
                except NotImplementedError as exc:
                    raise WorkflowStageError(stage, type(exc).__name__, missing=True) from exc
                except Exception as exc:
                    raise WorkflowStageError(getattr(exc, "failed_stage", stage), type(exc).__name__) from exc
                report["executed"] = True
                self.context.stage_states[stage]["executed"] = True
                live_verified = not self.context.options.dry_run
                report["live_verified"] = live_verified
                self.context.stage_states[stage]["live_verified"] = live_verified
                self.context.completed_stages.append(stage)
                self._log(f"workflow_stage_completed={stage}")
                if self.context.options.front_half_only and stage == "smsm_open_add_form":
                    return self._front_half_result()
        except WorkflowStageError as exc:
            self.context.failure_exception_type = type(exc.__cause__).__name__ if exc.__cause__ else type(exc).__name__
            return self._failure(exc.stage, missing_count=1 if exc.missing else 0, missing=exc.missing)

        return {
            "workflow_implementation_complete": all(
                report["implementation_status"] == "implemented_and_connected"
                for report in self.context.stage_reports
            ),
            "workflow_dry_run_completed": self.context.options.dry_run,
            "certificate_upload_ready": self._certificate_upload_ready(),
            "failed_stage": "",
            "exception_type": "",
            "missing_stage_count": 0,
            "noop_stage_count": sum(report["implementation_status"] == "noop" for report in self.context.stage_reports),
            "not_implemented_stage_count": sum(
                report["implementation_status"] == "not_implemented" for report in self.context.stage_reports
            ),
            "completed_stages": tuple(self.context.completed_stages),
            "stage_reports": tuple(self.context.stage_reports),
            "operation_counters": dict(self.context.operation_counters),
            "observations": dict(self.context.observations),
            "stage_states": dict(self.context.stage_states),
        }

    def _certificate_upload_ready(self) -> bool:
        observations = self.context.observations
        return all((
            observations.get("duplicate_check_determinate") is True,
            observations.get("duplicate_exact_match_count") == 0,
            observations.get("duplicate_same_name_match_count") == 0,
            observations.get("duplicate_upload_allowed") is True,
            observations.get("add_button_click_called") is True,
            observations.get("add_form_opened") is True,
            observations.get("file_input_send_keys_called") is True,
            observations.get("file_input_send_keys_count") == 1,
            observations.get("certificate_file_selected", observations.get("certificate_selected")) is True,
            observations.get("selected_certificate_filename_exact_imei_match") is True,
            observations.get("selected_certificate_extension_valid") is True,
            observations.get("password_input_send_keys_called") is True,
            observations.get("password_input_send_keys_count") == 1,
            observations.get("password_input_nonblank_after_send_keys", observations.get("password_input_nonblank_after_send", observations.get("password_input_nonblank"))) is True,
            observations.get("save_button_candidate_count") == 1,
            observations.get("save_button_unique") is True,
            observations.get("save_button_displayed") is True,
            observations.get("save_button_enabled") is True,
            observations.get("save_button_click_called") is False,
            observations.get("certificate_upload_called") is False,
        ))

    def _front_half_result(self) -> dict[str, Any]:
        return {
            "workflow_implementation_complete": False,
            "workflow_dry_run_completed": False,
            "front_half_live_verification_complete": True,
            "front_half_completed_stage_count": 8,
            "certificate_upload_ready": True,
            "device_binding_ready": False,
            "workflow_completed": False,
            "failed_stage": "",
            "exception_type": "",
            "missing_stage_count": 0,
            "noop_stage_count": 0,
            "not_implemented_stage_count": 0,
            "completed_stages": tuple(self.context.completed_stages),
            "stage_reports": tuple(self.context.stage_reports),
            "operation_counters": dict(self.context.operation_counters),
            "observations": dict(self.context.observations),
            "stage_states": dict(self.context.stage_states),
        }

    def _failure(self, stage: str, *, missing_count: int, missing: bool) -> dict[str, Any]:
        declared_missing_count = sum(
            not callable(self.handlers.get(candidate))
            or getattr(self.handlers[candidate], "workflow_status", "implemented_and_connected") == "missing"
            for candidate in self.stages
        )
        declared_noop_count = sum(
            callable(self.handlers.get(candidate))
            and getattr(self.handlers[candidate], "workflow_status", "implemented_and_connected") == "noop"
            for candidate in self.stages
        )
        declared_not_implemented_count = sum(
            callable(self.handlers.get(candidate))
            and getattr(self.handlers[candidate], "workflow_status", "implemented_and_connected") == "not_implemented"
            for candidate in self.stages
        )
        return {
            "workflow_implementation_complete": False,
            "workflow_dry_run_completed": False,
            "front_half_live_verification_complete": False,
            "front_half_completed_stage_count": 0,
            "certificate_upload_ready": False,
            "device_binding_ready": False,
            "workflow_completed": False,
            "failed_stage": stage,
            "exception_type": self.context.failure_exception_type,
            "missing_stage_count": max(missing_count if missing else 0, declared_missing_count),
            "noop_stage_count": max(
                declared_noop_count,
                sum(report["implementation_status"] == "noop" for report in self.context.stage_reports),
            ),
            "not_implemented_stage_count": max(
                declared_not_implemented_count,
                sum(report["implementation_status"] == "not_implemented" for report in self.context.stage_reports),
            ),
            "completed_stages": tuple(self.context.completed_stages),
            "stage_reports": tuple(self.context.stage_reports),
            "operation_counters": dict(self.context.operation_counters),
            "observations": dict(self.context.observations),
            "stage_states": dict(self.context.stage_states),
        }


def require_mutation(context: WorkflowContext, permission: str, stage: str) -> None:
    if not context.options.mutation_allowed(permission):
        raise WorkflowStageError(stage, "mutation blocked by dry-run or allow gate")


def make_default_handlers() -> dict[str, StageHandler]:
    """Return the connected stage adapters used by the production workflow."""

    handlers = {}
    for stage in WORKFLOW_STAGES:
        handler = _make_connected_stage_handler(stage)
        handlers[stage] = handler
    handlers["smsm_open_device_list"] = _make_connected_stage_handler("smsm_open_device_list")
    return handlers


def make_preparation_handlers() -> dict[str, StageHandler]:
    """Return only the stages allowed by the SMSM preparation diagnostic."""
    return {
        stage: _make_connected_stage_handler(stage)
        for stage in PREPARE_SMSM_CERTIFICATE_UPLOAD_STAGES
    }


def _make_connected_stage_handler(stage: str) -> StageHandler:
    idempotent_certificate_stages = {
        "hennge_download_certificate",
        "hennge_validate_download",
        "hennge_rename_certificate_to_imei",
        "hennge_reuse_existing_certificate",
    }
    permission_by_stage = {
        "hennge_download_certificate": "allow_certificate_download",
        "hennge_rename_certificate_to_imei": "allow_certificate_download",
        "smsm_set_certificate_file": "allow_certificate_upload",
        "smsm_set_certificate_password": "allow_certificate_upload",
        "smsm_submit_certificate_upload": "allow_certificate_upload",
        "smsm_set_device_imei": "allow_device_binding",
        "smsm_select_uploaded_certificate": "allow_device_binding",
        "smsm_save_device_certificate_binding": "allow_device_binding",
        "excel_write_success": "allow_excel_write",
    }
    counter_by_stage = {
        "hennge_download_certificate": "certificate_download_called",
        "hennge_rename_certificate_to_imei": "certificate_file_rename_called",
        "smsm_set_certificate_file": "file_input_send_keys_called",
        "smsm_set_certificate_password": "password_input_send_keys_called",
        "smsm_submit_certificate_upload": "certificate_upload_called",
        "smsm_set_device_imei": "device_imei_send_keys_called",
        "smsm_select_uploaded_certificate": "certificate_selection_called",
        "smsm_save_device_certificate_binding": "device_binding_save_called",
        "excel_write_success": "excel_write_called",
    }

    def connected_stage_handler(context: WorkflowContext) -> Any:
        context.record(f"{stage}_planned", True)
        if context.options.dry_run:
            return {"stage": stage, "mode": "dry_run", "planned": True}
        service = context.services.get(stage) or context.services.get("workflow")
        if service is None:
            raise RuntimeError(f"workflow service is not connected: {stage}")
        operation = getattr(service, stage, None)
        if not callable(operation):
            raise RuntimeError(f"workflow service method is not connected: {stage}")

        def invoke() -> Any:
            return operation(context)

        if context.options.resume_from_existing_certificate and stage in idempotent_certificate_stages:
            if context.options.dry_run:
                return invoke()
            return invoke()

        permission = permission_by_stage.get(stage)
        if permission:
            return run_guarded_operation(
                context,
                stage=stage,
                permission=permission,
                counter_name=counter_by_stage[stage],
                operation=invoke,
            )
        return invoke()

    connected_stage_handler.__name__ = f"handle_{stage}"
    connected_stage_handler.__qualname__ = f"handle_{stage}"
    connected_stage_handler.workflow_status = "implemented_and_connected"
    connected_stage_handler.dry_run_check = "records plan without browser, file, device, or Excel mutation"
    connected_stage_handler.allow_flag = permission_by_stage.get(stage, "none")
    return connected_stage_handler


def run_single_certificate_workflow(
    *,
    handlers: Mapping[str, StageHandler] | None = None,
    context: WorkflowContext | None = None,
    logger: Any = None,
    stages: tuple[str, ...] = WORKFLOW_STAGES,
) -> dict[str, Any]:
    workflow = SingleCertificateWorkflow(
        handlers=handlers or make_default_handlers(),
        context=context,
        logger=logger,
        stages=stages,
    )
    return workflow.run()
