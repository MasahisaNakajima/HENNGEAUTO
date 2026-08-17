from app.single_certificate_workflow import (
    PREPARE_SMSM_CERTIFICATE_UPLOAD_STAGES,
    WORKFLOW_STAGES,
    WorkflowContext,
    WorkflowOptions,
    make_default_handlers,
    make_preparation_handlers,
    require_exactly_one,
    run_guarded_operation,
    run_single_certificate_workflow,
)
from app.workflow_service import ProductionWorkflowService
from app.workflow_service import ProductionWorkflowService
import inspect
from pathlib import Path
import pytest

import diagnose_smsm_single_target_lookup as cli


def test_workflow_runs_all_stages_in_fixed_order():
    calls = []
    handlers = {
        stage: lambda context, stage=stage: calls.append(stage)
        for stage in WORKFLOW_STAGES
    }

    result = run_single_certificate_workflow(
        handlers=handlers,
        context=WorkflowContext(options=WorkflowOptions(dry_run=True)),
    )

    assert result["workflow_implementation_complete"] is True
    assert result["workflow_dry_run_completed"] is True
    assert calls == list(WORKFLOW_STAGES)


def test_workflow_stops_at_first_failed_stage():
    calls = []

    def fail(context):
        calls.append("failed")
        raise RuntimeError("expected")

    handlers = {
        stage: lambda context, stage=stage: calls.append(stage)
        for stage in WORKFLOW_STAGES
    }
    failed_stage = WORKFLOW_STAGES[4]
    handlers[failed_stage] = fail

    result = run_single_certificate_workflow(handlers=handlers)

    assert result["workflow_implementation_complete"] is False
    assert result["failed_stage"] == failed_stage
    assert calls == list(WORKFLOW_STAGES[:4]) + ["failed"]


def test_missing_handler_is_reported_as_code31_condition():
    handlers = {stage: lambda context: None for stage in WORKFLOW_STAGES}
    missing_stage = WORKFLOW_STAGES[6]
    del handlers[missing_stage]

    result = run_single_certificate_workflow(handlers=handlers)

    assert result["workflow_implementation_complete"] is False
    assert result["workflow_dry_run_completed"] is False
    assert result["failed_stage"] == missing_stage
    assert result["missing_stage_count"] == 1


def test_not_implemented_handler_is_reported_as_missing_stage():
    handlers = {stage: lambda context: None for stage in WORKFLOW_STAGES}

    def unimplemented(context):
        raise NotImplementedError

    handlers["smsm_set_certificate_file"] = unimplemented
    result = run_single_certificate_workflow(handlers=handlers)

    assert result["failed_stage"] == "smsm_set_certificate_file"
    assert result["missing_stage_count"] == 1


def test_dry_run_always_blocks_mutation_even_when_allow_flag_is_set():
    context = WorkflowContext(
        options=WorkflowOptions(
            dry_run=True,
            allow_certificate_download=True,
            allow_certificate_upload=True,
            allow_device_binding=True,
            allow_excel_write=True,
        )
    )

    assert context.options.mutation_allowed("allow_certificate_download") is False
    assert context.options.mutation_allowed("allow_certificate_upload") is False
    assert context.options.mutation_allowed("allow_device_binding") is False
    assert context.options.mutation_allowed("allow_excel_write") is False


def test_default_handlers_are_connected_and_dry_run_completes_all_stages():
    context = WorkflowContext(options=WorkflowOptions(dry_run=True))
    context.set_target({"alias": "alias", "serial": "serial", "imei": "123456789012345"})
    result = run_single_certificate_workflow(handlers=make_default_handlers(), context=context)

    assert result["workflow_implementation_complete"] is True
    assert result["workflow_dry_run_completed"] is True
    assert result["missing_stage_count"] == 0
    assert result["noop_stage_count"] == 0
    assert result["not_implemented_stage_count"] == 0
    assert len(result["completed_stages"]) == len(WORKFLOW_STAGES)


def test_successful_client_certificate_page_calls_prepare_handler_once():
    calls = []

    class Service:
        def smsm_open_client_certificate_page(self, context):
            context.record("smsm_client_certificate_page_live_verified", True)
            return {"smsm_client_certificate_page_live_verified": True}

        def smsm_prepare_certificate_upload(self, context):
            calls.append("prepare")
            return {"upload_ready": True}

    context = WorkflowContext()
    context.services["workflow"] = Service()
    context.set_target({"alias": "alias", "serial": "serial", "imei": "123456789012345"})
    context.certificate_path = Path("123456789012345.p12")
    context.certificate_password = "secret"
    handlers = {stage: handler for stage, handler in make_preparation_handlers().items()}

    result = run_single_certificate_workflow(
        handlers=handlers,
        context=context,
        stages=("smsm_open_client_certificate_page", "smsm_prepare_certificate_upload"),
    )

    assert result["failed_stage"] == ""
    assert calls == ["prepare"]
    assert result["stage_states"]["smsm_prepare_certificate_upload"]["executed"] is True


def test_prepare_metrics_are_emitted_from_workflow_observations(capsys):
    class Logger:
        def info(self, _message):
            return None

    observations = {
        "smsm_prepare_called": True,
        "smsm_prepare_page_verified": True,
        "smsm_prepare_target_imei_present": True,
        "smsm_prepare_certificate_path_present": True,
        "smsm_prepare_certificate_password_present": True,
        "smsm_prepare_duplicate_check_called": True,
        "smsm_prepare_duplicate_check_completed": True,
        "smsm_prepare_failed_phase": "check_certificate_duplicate",
        "smsm_prepare_exception_type": "RuntimeError",
        "duplicate_search_called": True,
        "duplicate_check_determinate": True,
        "duplicate_exact_match_count": 1,
        "duplicate_same_name_match_count": 0,
        "duplicate_upload_allowed": False,
    }

    cli._emit_workflow_report(Logger(), {"observations": observations, "operation_counters": {}, "stage_reports": []})
    output = capsys.readouterr().out

    for key, value in observations.items():
        assert f"{key}={value}" in output


def test_default_handlers_are_connected_functions_without_placeholder_patterns():
    handlers = make_default_handlers()
    source = inspect.getsource(make_default_handlers)

    assert all(callable(handler) for handler in handlers.values())
    assert "NotImplementedError" not in source
    assert "placeholder" not in source.lower()
    assert "noop" not in source.lower()
    assert all(getattr(handler, "workflow_status") == "implemented_and_connected" for handler in handlers.values())


@pytest.mark.parametrize("candidates", [[], ["first", "second"]])
def test_candidate_cardinality_zero_or_multiple_stops(candidates):
    with pytest.raises(Exception):
        require_exactly_one(candidates, stage="candidate_stage", label="candidate")


def test_candidate_cardinality_one_returns_candidate():
    assert require_exactly_one(["only"], stage="candidate_stage", label="candidate") == "only"


@pytest.mark.parametrize(
    ("stage", "label"),
    [
        ("hennge_search_certificate_by_alias", "hennge_result"),
        ("hennge_download_certificate", "download_candidate"),
        ("hennge_read_certificate_password", "password_source"),
        ("smsm_find_add_button", "add_button"),
        ("smsm_set_certificate_file", "file_input"),
        ("smsm_set_certificate_password", "password_input"),
        ("smsm_submit_certificate_upload", "submit_button"),
        ("smsm_search_device_by_serial", "serial_result"),
        ("smsm_set_device_imei", "imei_input"),
        ("smsm_select_uploaded_certificate", "certificate_option"),
        ("smsm_save_device_certificate_binding", "save_button"),
    ],
)
@pytest.mark.parametrize("candidates", [[], ["first", "second"]])
def test_workflow_candidate_stages_stop_on_zero_or_multiple(stage, label, candidates):
    with pytest.raises(Exception) as error:
        require_exactly_one(candidates, stage=stage, label=label)
    assert error.value.stage == stage


def test_single_candidates_advance_in_required_dependency_order():
    calls = []
    handlers = {
        stage: lambda context, stage=stage: calls.append(stage)
        for stage in WORKFLOW_STAGES
    }
    result = run_single_certificate_workflow(
        handlers=handlers,
        context=WorkflowContext(options=WorkflowOptions(dry_run=True)),
    )

    assert result["workflow_implementation_complete"] is True
    assert calls.index("smsm_verify_certificate_upload") < calls.index("smsm_set_device_imei")
    assert calls.index("smsm_verify_device_certificate_binding") < calls.index("excel_write_success")


def test_excel_lock_does_not_report_dry_run_success(monkeypatch, capsys):
    class Logger:
        def info(self, _message):
            return None

    class LockedReader:
        def __init__(self, _path):
            return None

        def read_targets(self):
            raise PermissionError

    monkeypatch.setattr(cli, "AppLogger", lambda _base_dir: Logger())
    monkeypatch.setattr(cli, "load_config", lambda: {"excel": {"path": "C:/locked/target.xlsm"}})
    monkeypatch.setattr(cli, "ExcelReader", LockedReader)

    result = cli.main(["--run-single-certificate-workflow", "--dry-run"])
    output = capsys.readouterr().out

    assert result == 31
    assert "workflow_implementation_complete=False" in output
    assert "workflow_dry_run_completed=False" in output
    assert "failed_stage=excel_load_target" in output


def test_context_preserves_row_number_and_string_identifiers():
    context = WorkflowContext()
    context.set_target({
        "alias": "alias",
        "serial": "00001234",
        "imei": "000123456789012",
        "row_number": 17,
    })

    assert context.target["row_number"] == "17"
    assert context.target["serial"] == "00001234"
    assert context.target["imei"] == "000123456789012"


def test_dry_run_guard_wins_over_every_allow_flag():
    context = WorkflowContext(options=WorkflowOptions(
        dry_run=True,
        allow_certificate_download=True,
        allow_certificate_upload=True,
        allow_device_binding=True,
        allow_excel_write=True,
    ))
    called = []

    with pytest.raises(Exception):
        run_guarded_operation(
            context,
            stage="download",
            permission="allow_certificate_download",
            counter_name="certificate_download_called",
            operation=lambda: called.append(True),
        )

    assert called == []
    assert context.operation_counters["certificate_download_called"] is False


def test_guarded_operation_rechecks_allow_and_records_call():
    context = WorkflowContext(options=WorkflowOptions(allow_excel_write=True))
    called = []

    run_guarded_operation(
        context,
        stage="excel_write_success",
        permission="allow_excel_write",
        counter_name="excel_write_called",
        operation=lambda: called.append(True),
    )

    assert called == [True]
    assert context.operation_counters["excel_write_called"] is True


def test_same_context_flows_between_all_connected_stages():
    context = WorkflowContext(options=WorkflowOptions(dry_run=True))
    seen = []
    handlers = {
        stage: lambda current_context, stage=stage: seen.append(id(current_context))
        for stage in WORKFLOW_STAGES
    }

    result = run_single_certificate_workflow(handlers=handlers, context=context)

    assert result["workflow_implementation_complete"] is True
    assert seen == [id(context)] * len(WORKFLOW_STAGES)


def test_resume_option_is_exposed_by_workflow_parser():
    parsed = cli._workflow_argument_parser().parse_args([
        "--run-single-certificate-workflow",
        "--resume-from-existing-certificate",
    ])

    assert parsed.resume_from_existing_certificate is True


def test_existing_smsm_binding_stages_exclude_upload_hennge_and_excel_write():
    stages = cli._existing_smsm_certificate_binding_stages()

    assert stages == (
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
    assert not any(stage.startswith("hennge_") for stage in stages)
    assert "smsm_set_device_imei" not in stages
    assert "excel_write_success" not in stages


def test_existing_smsm_binding_requires_explicit_device_binding_allow_flag(capsys):
    result = cli._run_single_certificate_workflow_cli([
        "--bind-existing-smsm-certificate",
    ])

    assert result == 2
    assert "--allow-device-binding" in capsys.readouterr().out


def test_main_routes_both_binding_flags_to_binding_workflow_once(monkeypatch):
    calls = []

    def run_binding(args):
        calls.append(args)
        return 1

    monkeypatch.setattr(cli, "_run_single_certificate_workflow_cli", run_binding)

    result = cli.main([
        "--bind-existing-smsm-certificate",
        "--allow-device-binding",
    ])

    assert result == 1
    assert calls == [["--bind-existing-smsm-certificate", "--allow-device-binding"]]


def test_existing_smsm_binding_handlers_are_all_callable():
    handlers = make_default_handlers()
    stages = cli._existing_smsm_certificate_binding_stages()

    assert all(callable(handlers.get(stage)) for stage in stages)


def test_smsm_open_device_list_accepts_none_navigation_return_when_dom_is_verified():
    service = ProductionWorkflowService.__new__(ProductionWorkflowService)

    class SmsmStub:
        def reach_device_search_page(self, trace=None):
            return None

        def inspect_device_list_page(self, trace=None):
            return {
                "device_list_navigation_completed": True,
                "device_list_page_verified": True,
                "device_list_pathname_matches": True,
                "device_list_search_input_candidate_count": 1,
                "device_list_search_button_candidate_count": 1,
                "device_list_failed_phase": "completed",
                "device_list_exception_type": "",
            }

    class ContextStub:
        def __init__(self):
            self.values = {}

        def record(self, key, value):
            self.values[key] = value

    service.smsm = SmsmStub()
    context = ContextStub()

    result = service.smsm_open_device_list(context)

    assert result["device_list_page_verified"] is True
    assert context.values["device_list_pathname_matches"] is True


def test_existing_smsm_binding_excel_lock_reports_excel_stage_and_permission_error(monkeypatch, capsys, tmp_path):
    class LockedReader:
        def __init__(self, _path):
            pass

        def read_targets(self, include_row_number=True):
            raise PermissionError("locked")

    monkeypatch.setattr(cli, "ExcelReader", LockedReader)
    monkeypatch.setattr(cli, "load_config", lambda: {"excel": {"path": str(tmp_path / "locked.xlsm")}})

    result = cli._run_single_certificate_workflow_cli([
        "--bind-existing-smsm-certificate",
        "--allow-device-binding",
        "--dry-run",
    ])

    output = capsys.readouterr().out
    assert result == 1
    assert "failed_stage=excel_load_target" in output
    assert "exception_type=PermissionError" in output


def test_existing_smsm_binding_workflow_setup_exception_reports_without_browser(monkeypatch, capsys):
    monkeypatch.setattr(cli, "make_default_handlers", lambda: (_ for _ in ()).throw(ValueError("setup")))
    browser_started = []
    monkeypatch.setattr(cli, "Browser", lambda *_args: browser_started.append(True))

    result = cli._run_single_certificate_workflow_cli([
        "--bind-existing-smsm-certificate",
        "--allow-device-binding",
    ])

    output = capsys.readouterr().out
    assert result == 1
    assert browser_started == []
    assert "failed_stage=workflow_handler_setup" in output
    assert "exception_type=ValueError" in output
    assert "browser_start_called=False" in output


def test_existing_smsm_binding_success_returns_zero_without_browser(monkeypatch):
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {
            "failed_stage": "",
            "exception_type": "",
            "observations": {"device_binding_verified": True},
            "operation_counters": {"device_binding_save_called": True},
            "stage_reports": (),
        }

    monkeypatch.setattr(cli, "run_single_certificate_workflow", fake_run)
    monkeypatch.setattr(cli, "load_config", lambda: {"excel": {"path": "unused"}})

    result = cli._run_single_certificate_workflow_cli([
        "--bind-existing-smsm-certificate",
        "--allow-device-binding",
        "--dry-run",
    ])

    assert result == 0
    assert captured["stages"] == cli._existing_smsm_certificate_binding_stages()
    assert all(callable(captured["handlers"].get(stage)) for stage in captured["stages"])


def test_resume_certificate_stages_keep_operation_counters_false():
    calls = []

    class Service:
        def hennge_download_certificate(self, context):
            calls.append("download")
            return None

        def hennge_validate_download(self, context):
            calls.append("validate")
            return None

        def hennge_rename_certificate_to_imei(self, context):
            calls.append("rename")
            return None

    context = WorkflowContext(options=WorkflowOptions(
        resume_from_existing_certificate=True,
        allow_certificate_download=True,
    ))
    context.services["workflow"] = Service()
    handlers = make_default_handlers()
    for stage in ("hennge_download_certificate", "hennge_validate_download", "hennge_rename_certificate_to_imei"):
        handlers[stage](context)

    assert calls == ["download", "validate", "rename"]
    assert context.operation_counters["certificate_download_called"] is False
    assert context.operation_counters["certificate_file_rename_called"] is False


class _ExistingCertificateHennge:
    def __init__(self, directory):
        self.directory = directory
        self.validated = []

    def _downloads_dir(self):
        return self.directory

    def validate_downloaded_certificate(self, path):
        self.validated.append(path)
        return path


def _existing_certificate_service(directory):
    service = ProductionWorkflowService.__new__(ProductionWorkflowService)
    service.hennge = _ExistingCertificateHennge(directory)
    return service


def _resume_context():
    context = WorkflowContext(options=WorkflowOptions(resume_from_existing_certificate=True))
    context.set_target({"alias": "alias", "serial": "serial", "imei": "123456789012345"})
    return context


def test_existing_p12_is_reused_without_file_mutation(tmp_path):
    candidate = tmp_path / "123456789012345.p12"
    candidate.write_bytes(b"certificate")
    service = _existing_certificate_service(tmp_path)
    context = _resume_context()

    result = service.hennge_download_certificate(context)

    assert result["stage_result"] == "idempotent_existing_state"
    assert context.certificate_path == candidate
    assert candidate.read_bytes() == b"certificate"
    assert context.observations["existing_certificate_file_candidate_count"] == 1
    assert context.observations["existing_certificate_file_valid"] is True
    assert context.operation_counters["certificate_download_called"] is False


def test_existing_pfx_is_reused(tmp_path):
    candidate = tmp_path / "123456789012345.pfx"
    candidate.write_bytes(b"certificate")
    service = _existing_certificate_service(tmp_path)
    context = _resume_context()

    service.hennge_download_certificate(context)

    assert context.certificate_path == candidate
    assert context.observations["existing_certificate_file_candidate_count"] == 1


@pytest.mark.parametrize(
    "files",
    [
        [],
        ["123456789012345.p12", "123456789012345.pfx"],
        ["123456789012345.p12"],
    ],
)
def test_existing_certificate_candidates_are_fail_fast(tmp_path, files):
    for name in files:
        path = tmp_path / name
        path.write_bytes(b"" if name.endswith(".p12") and len(files) == 1 else b"certificate")
    service = _existing_certificate_service(tmp_path)
    context = _resume_context()

    if files == ["123456789012345.p12"]:
        with pytest.raises(Exception) as error:
            service.hennge_download_certificate(context)
        assert error.value.stage == "hennge_validate_existing_certificate_file"
    elif len(files) != 1:
        with pytest.raises(Exception) as error:
            service.hennge_download_certificate(context)
        assert error.value.stage == "hennge_validate_existing_certificate_file"


def test_dry_run_has_no_side_effect_counters():
    handlers = {
        stage: lambda context, stage=stage: None
        for stage in WORKFLOW_STAGES
    }
    result = run_single_certificate_workflow(
        handlers=handlers,
        context=WorkflowContext(options=WorkflowOptions(dry_run=True)),
    )

    assert all(value is False for value in result["operation_counters"].values())


def test_sensitive_values_are_not_in_context_repr_or_observations():
    context = WorkflowContext()
    context.certificate_password = "secret-password"
    context.certificate_path = Path("C:/private/certificate.p12")
    context.record("certificate_password", "secret-password")
    context.record("device_imei", "000123456789012")
    context.record("device_serial", "00001234")
    context.record("certificate_file_path", "C:/private/certificate.p12")

    representation = repr(context)
    assert "secret-password" not in representation
    assert "000123456789012" not in representation
    assert "00001234" not in representation
    assert "certificate.p12" not in representation
    assert context.observations == {
        "certificate_password": True,
        "device_imei": True,
        "device_serial": True,
        "certificate_file_path": True,
    }
