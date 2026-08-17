from __future__ import annotations

from dataclasses import dataclass, field
import pytest

import diagnose_excel_autoclose as mod
import app.excel_session as excel_mod


class DummyLogger:
    def __init__(self):
        self.info_messages = []

    def info(self, message):
        self.info_messages.append(message)


class FakeReader:
    def __init__(self, _path, calls, states):
        self.calls = calls
        self.states = list(states)

    def is_file_open(self):
        self.calls.append("is_file_open")
        if self.states:
            return self.states.pop(0)
        return False

    def read_targets(self):
        self.calls.append("read_targets")
        raise AssertionError("read_targets must not be called")


@dataclass
class FakeDetection:
    workbook: object | None
    application: object | None = object()
    xlmain_count: int = 2
    xldesk_count: int = 2
    excel7_count: int = 3
    accessible_object_call_count: int = 3
    accessible_object_success_count: int = 2
    dispatch_wrap_success_count: int = 2
    window_object_count: int = 1
    workbook_object_count: int = 0
    application_object_count: int = 1
    unknown_object_count: int = 0
    application_property_success_count: int = 1
    application_count_before_dedupe: int = 2
    application_count_after_dedupe: int = 2
    application_workbooks_count_success: int = 1
    application_workbooks_count_failure: int = 1
    workbook_item_success_count: int = 2
    workbook_item_failure_count: int = 1
    workbook_fullname_success_count: int = 2
    workbook_fullname_failure_count: int = 0
    normalized_path_success_count: int = 2
    exact_path_match_count: int = 1
    hwnd_unavailable_count: int = 0
    rot_moniker_count: int = 2
    rot_get_object_success_count: int = 2
    rot_get_object_failure_count: int = 0
    rot_workbook_candidate_count: int = 1
    rot_fullname_success_count: int = 1
    rot_fullname_failure_count: int = 0
    rot_normalized_path_success_count: int = 1
    rot_exact_path_match_count: int = 1
    rot_duplicate_candidate_count: int = 0
    native_dispatch_pointer_success_count: int = 2
    native_dispatch_pointer_null_count: int = 0
    native_dispatch_wrap_success_count: int = 2
    native_dispatch_wrap_failure_count: int = 0
    direct_application_success_count: int = 1
    parent_application_success_count: int = 0
    grandparent_application_success_count: int = 0
    native_application_failure_count: int = 0
    native_workbooks_count_success: int = 1
    native_workbooks_count_failure: int = 0
    observed_excel_window_count: int = 2
    observed_application_count: int = 2
    observed_workbook_count: int = 3
    matched_workbook_count: int = 0
    detection_method: str = "accessible_object_from_window"
    configured_path_kind: str = "local"
    workbook_path_kind: str = "local"
    configured_path_exists: bool = True
    workbook_path_exists: bool = True
    extension_equal: bool = True
    basename_equal: bool = True
    normalized_equal: bool = True
    unicode_normalized_equal: bool = True
    samefile_equal: bool | str = False
    parent_equal: bool = True
    target_match_method: str = "normalized"
    compared_workbook_count: int = 3
    local_local_count: int = 3
    local_url_count: int = 0
    basename_equal_count: int = 1
    normalized_equal_count: int = 1
    unicode_normalized_equal_count: int = 1
    samefile_equal_count: int = 0
    samefile_unavailable_count: int = 0
    web_identity_configured: bool = False
    web_identity_source: str = "none"
    web_identity_valid: bool = False
    web_candidate_count: int = 0
    candidate_hash_generated_count: int = 0
    internal_hash_equal_count: int = 0
    web_identity_match_count: int = 0
    capture_succeeded: bool = False
    detection_exceptions: list[tuple[str, str, int]] = field(default_factory=list)
    captured_web_identity_hash: str | None = None

    def __post_init__(self):
        if self.workbook is not None and self.matched_workbook_count == 0:
            self.matched_workbook_count = 1


def _install(
    monkeypatch,
    tmp_path,
    *,
    detection: FakeDetection | None = None,
    save_exc=None,
    unlock_states=None,
    reopen_fail=False,
):
    excel_path = tmp_path / "target.xlsm"
    excel_path.write_bytes(b"x")

    logger = DummyLogger()
    calls = {
        "save_close": 0,
        "reopen": 0,
        "detect": 0,
    }
    track = []

    monkeypatch.setattr(mod, "AppLogger", lambda _base_dir: logger)
    monkeypatch.setattr(mod, "load_config", lambda: {"excel": {"path": str(excel_path)}})

    detection_obj = detection if detection is not None else FakeDetection(workbook=object())

    def fake_detect(_path, timeout_sec=15.0, interval_sec=0.5, **_kwargs):
        _ = (_path, timeout_sec, interval_sec)
        calls["detect"] += 1
        track.append("detect")
        return detection_obj

    monkeypatch.setattr(mod, "detect_target_workbook", fake_detect)

    def fake_save_close(_path, _logger, **_kwargs):
        calls["save_close"] += 1
        track.append("save_close")
        if save_exc is not None:
            raise save_exc
        return True

    monkeypatch.setattr(mod, "save_and_close_target_workbook", fake_save_close)

    reader = FakeReader(str(excel_path), track, unlock_states or [False])

    def fake_reader_ctor(path):
        _ = path
        return reader

    monkeypatch.setattr(mod, "ExcelReader", fake_reader_ctor)

    sleep_calls = []

    def fake_sleep(sec):
        sleep_calls.append(sec)

    monkeypatch.setattr(mod.time, "sleep", fake_sleep)

    def fake_reopen(path, emit):
        _ = path
        calls["reopen"] += 1
        track.append("reopen")
        if reopen_fail:
            emit("Excelファイルの起動に失敗しました: RuntimeError")
        else:
            emit("Excelファイルを起動しました")

    monkeypatch.setattr(mod, "reopen_excel", fake_reopen)

    return excel_path, logger, calls, track, sleep_calls


def test_detect_only_detected_returns_0_without_excel_operations(monkeypatch, tmp_path):
    _excel_path, logger, calls, track, _sleep_calls = _install(
        monkeypatch,
        tmp_path,
        detection=FakeDetection(workbook=object()),
    )

    rc = mod.main(["--detect-only"])

    joined = "\n".join(logger.info_messages)
    assert rc == 0
    assert calls["save_close"] == 0
    assert calls["reopen"] == 0
    assert "is_file_open" not in track
    assert "save_called=False" in joined
    assert "close_called=False" in joined
    assert "reopen_called=False" in joined


def test_detect_only_not_found_returns_8(monkeypatch, tmp_path):
    _excel_path, logger, calls, _track, _sleep_calls = _install(
        monkeypatch,
        tmp_path,
        detection=FakeDetection(workbook=None, application=None, exact_path_match_count=0),
    )

    rc = mod.main(["--detect-only"])

    joined = "\n".join(logger.info_messages)
    assert rc == 8
    assert calls["save_close"] == 0
    assert calls["reopen"] == 0
    assert "target_was_open=False" in joined


def test_normal_not_opened_target_returns_7_without_expect_open(monkeypatch, tmp_path):
    _excel_path, logger, calls, track, _sleep_calls = _install(
        monkeypatch,
        tmp_path,
        detection=FakeDetection(workbook=None, application=None, exact_path_match_count=0),
        unlock_states=[False],
    )

    rc = mod.main([])

    joined = "\n".join(logger.info_messages)
    assert rc == 7
    assert calls["save_close"] == 0
    assert calls["reopen"] == 0
    assert "target_was_open=False" in joined
    assert "is_file_open" not in track


def test_normal_not_opened_target_returns_8_with_expect_open(monkeypatch, tmp_path):
    _excel_path, logger, calls, _track, _sleep_calls = _install(
        monkeypatch,
        tmp_path,
        detection=FakeDetection(workbook=None, application=None, exact_path_match_count=0),
    )

    rc = mod.main(["--expect-open"])

    joined = "\n".join(logger.info_messages)
    assert rc == 8
    assert calls["save_close"] == 0
    assert "target_was_open=False" in joined


def test_logs_detection_metrics(monkeypatch, tmp_path):
    detection = FakeDetection(
        workbook=object(),
        xlmain_count=6,
        xldesk_count=6,
        excel7_count=6,
        application_count_after_dedupe=1,
        observed_application_count=1,
        observed_workbook_count=0,
        detection_method="combined_scan",
    )
    _excel_path, logger, _calls, _track, _sleep_calls = _install(
        monkeypatch,
        tmp_path,
        detection=detection,
    )

    rc = mod.main(["--detect-only"])

    joined = "\n".join(logger.info_messages)
    assert rc in (0, 8)
    assert "xlmain_count=6" in joined
    assert "xldesk_count=6" in joined
    assert "excel7_count=6" in joined
    assert "accessible_object_call_count=" in joined
    assert "workbook_item_failure_count=" in joined
    assert "hwnd_unavailable_count=" in joined
    assert "rot_moniker_count=" in joined
    assert "rot_get_object_success_count=" in joined
    assert "rot_get_object_failure_count=" in joined
    assert "rot_workbook_candidate_count=" in joined
    assert "rot_exact_path_match_count=" in joined
    assert "native_dispatch_pointer_success_count=" in joined
    assert "native_dispatch_wrap_success_count=" in joined
    assert "direct_application_success_count=" in joined
    assert "native_workbooks_count_success=" in joined
    assert "observed_application_count=1" in joined
    assert "detection_method=combined_scan" in joined
    assert "configured_path_kind=" in joined
    assert "workbook_path_kind=" in joined
    assert "target_match_method=" in joined
    assert "compared_workbook_count=" in joined
    assert "local_url_count=" in joined
    assert "samefile_unavailable_count=" in joined
    assert "web_identity_configured=" in joined
    assert "web_identity_source=" in joined
    assert "web_identity_valid=" in joined
    assert "web_candidate_count=" in joined
    assert "candidate_hash_generated_count=" in joined
    assert "internal_hash_equal_count=" in joined
    assert "web_identity_match_count=" in joined
    assert "capture_succeeded=" in joined
    assert "object_kind=window" in joined
    assert "object_kind=workbook" in joined
    assert "object_kind=application" in joined
    assert "object_kind=unknown" in joined


def test_logs_detection_exceptions_only_as_stage_and_type(monkeypatch, tmp_path):
    detection = FakeDetection(
        workbook=None,
        application=None,
        exact_path_match_count=0,
        detection_exceptions=[("application_workbooks_count", "com_error", 1)],
    )
    _excel_path, logger, _calls, _track, _sleep_calls = _install(monkeypatch, tmp_path, detection=detection)

    rc = mod.main(["--detect-only"])

    joined = "\n".join(logger.info_messages)
    assert rc == 8
    assert "detection_exception" in joined
    assert "stage=application_workbooks_count" in joined
    assert "exception_type=com_error" in joined
    assert "count=1" in joined


def test_logs_failed_native_stage_for_native_errors(monkeypatch, tmp_path):
    detection = FakeDetection(
        workbook=None,
        application=None,
        exact_path_match_count=0,
        detection_exceptions=[("build_iid_idispatch", "TypeError", 1)],
    )
    _excel_path, logger, _calls, _track, _sleep_calls = _install(monkeypatch, tmp_path, detection=detection)

    rc = mod.main(["--detect-only"])

    joined = "\n".join(logger.info_messages)
    assert rc == 8
    assert "detection_exception" in joined
    assert "failed_native_stage=build_iid_idispatch" in joined
    assert "exception_type=TypeError" in joined


def test_opened_target_calls_save_close_once(monkeypatch, tmp_path):
    _excel_path, _logger, calls, track, _sleep_calls = _install(
        monkeypatch,
        tmp_path,
        detection=FakeDetection(workbook=object()),
        unlock_states=[True, False],
    )

    rc = mod.main([])

    assert rc == 0
    assert calls["detect"] == 1
    assert calls["save_close"] == 1
    assert track.count("is_file_open") >= 1


def test_reopen_called_at_most_once(monkeypatch, tmp_path):
    _excel_path, _logger, calls, _track, _sleep_calls = _install(
        monkeypatch,
        tmp_path,
        detection=FakeDetection(workbook=object()),
        unlock_states=[False],
    )

    rc = mod.main([])

    assert rc == 0
    assert calls["reopen"] == 1


def test_readonly_maps_to_code_3(monkeypatch, tmp_path):
    _excel_path, logger, calls, _track, _sleep_calls = _install(
        monkeypatch,
        tmp_path,
        detection=FakeDetection(workbook=object()),
        save_exc=mod.ReadOnlyWorkbookError("readonly"),
    )

    rc = mod.main([])

    joined = "\n".join(logger.info_messages)
    assert rc == 3
    assert calls["save_close"] == 1
    assert "failed_stage=save_close" in joined
    assert "exception_type=ReadOnlyWorkbookError" in joined


def test_save_or_close_failure_maps_to_code_4(monkeypatch, tmp_path):
    _excel_path, logger, calls, _track, _sleep_calls = _install(
        monkeypatch,
        tmp_path,
        detection=FakeDetection(workbook=object()),
        save_exc=mod.SaveCloseWorkbookError("saveclose"),
    )

    rc = mod.main([])

    joined = "\n".join(logger.info_messages)
    assert rc == 4
    assert calls["save_close"] == 1
    assert "exception_type=SaveCloseWorkbookError" in joined


def test_unlock_timeout_maps_to_code_5(monkeypatch, tmp_path):
    _excel_path, logger, _calls, _track, _sleep_calls = _install(
        monkeypatch,
        tmp_path,
        detection=FakeDetection(workbook=object()),
        unlock_states=[True] * 100,
    )

    tick = {"v": 0.0}

    def fake_monotonic():
        tick["v"] += 0.6
        return tick["v"]

    monkeypatch.setattr(mod.time, "monotonic", fake_monotonic)

    rc = mod.main([])

    joined = "\n".join(logger.info_messages)
    assert rc == 5
    assert "failed_stage=unlock_wait" in joined
    assert "exception_type=UnlockTimeoutError" in joined


def test_reopen_failure_maps_to_code_6(monkeypatch, tmp_path):
    _excel_path, logger, calls, _track, _sleep_calls = _install(
        monkeypatch,
        tmp_path,
        detection=FakeDetection(workbook=object()),
        reopen_fail=True,
    )

    rc = mod.main([])

    joined = "\n".join(logger.info_messages)
    assert rc == 6
    assert calls["reopen"] == 1
    assert "failed_stage=reopen" in joined


def test_target_missing_maps_to_code_2(monkeypatch, tmp_path):
    missing = tmp_path / "missing.xlsm"
    logger = DummyLogger()
    monkeypatch.setattr(mod, "AppLogger", lambda _base_dir: logger)
    monkeypatch.setattr(mod, "load_config", lambda: {"excel": {"path": str(missing)}})

    rc = mod.main(["--detect-only"])

    assert rc == 2


def test_detect_only_logs_do_not_leak_paths_or_identifiers(monkeypatch, tmp_path):
    excel_path, logger, _calls, _track, _sleep_calls = _install(
        monkeypatch,
        tmp_path,
        detection=FakeDetection(
            workbook=None,
            application=None,
            exact_path_match_count=0,
            detection_exceptions=[("normalize_path", "ValueError", 1)],
        ),
    )

    rc = mod.main(["--detect-only"])

    joined = "\n".join(logger.info_messages)
    assert rc == 8
    assert str(excel_path) not in joined
    assert excel_path.name not in joined
    assert "alias" not in joined
    assert "IMEI" not in joined
    assert "serial" not in joined


def test_waits_three_seconds_before_reopen(monkeypatch, tmp_path):
    _excel_path, _logger, _calls, _track, sleep_calls = _install(
        monkeypatch,
        tmp_path,
        detection=FakeDetection(workbook=object()),
        unlock_states=[False],
    )

    rc = mod.main([])

    assert rc == 0
    assert 3.0 in sleep_calls


def test_capture_web_identity_requires_detect_only(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)

    with pytest.raises(SystemExit):
        mod.main(["--capture-web-identity"])


def test_capture_web_identity_prints_env_assignment(monkeypatch, tmp_path, capsys):
    detection = FakeDetection(workbook=None, application=None, target_match_method="none")
    detection.capture_succeeded = True
    detection.captured_web_identity_hash = "a" * 64
    detection.web_candidate_count = 1

    _excel_path, logger, calls, _track, _sleep_calls = _install(monkeypatch, tmp_path, detection=detection)

    monkeypatch.setattr(
        mod,
        "_resolve_web_identity_hash",
        lambda _cfg, _mode: mod.WebIdentityResolution("", "none", False),
    )

    rc = mod.main(["--detect-only", "--capture-web-identity", "--web-identity-mode", "test"])

    out = capsys.readouterr().out
    joined = "\n".join(logger.info_messages)
    assert rc == 0
    assert calls["save_close"] == 0
    assert calls["reopen"] == 0
    assert f"$env:{mod.WEB_IDENTITY_ENV_TEST}=" in out
    assert "capture_succeeded=True" in joined


def test_capture_web_identity_failure_returns_8(monkeypatch, tmp_path):
    detection = FakeDetection(workbook=None, application=None, target_match_method="none")
    detection.capture_succeeded = False
    detection.captured_web_identity_hash = None
    detection.web_candidate_count = 2

    _excel_path, _logger, calls, _track, _sleep_calls = _install(monkeypatch, tmp_path, detection=detection)
    monkeypatch.setattr(
        mod,
        "_resolve_web_identity_hash",
        lambda _cfg, _mode: mod.WebIdentityResolution("", "none", False),
    )

    rc = mod.main(["--detect-only", "--capture-web-identity"]) 

    assert rc == 8
    assert calls["save_close"] == 0
    assert calls["reopen"] == 0


def test_cli_environment_sharepoint_match_returns_zero_without_operations(monkeypatch, tmp_path):
    excel_path = tmp_path / "【テスト用】HENNGE作業用Tool.ver3.1.xlsm"
    excel_path.write_bytes(b"x")
    workbook_url = (
        "https://tenant.sharepoint.com/sites/team/Shared%20Documents/"
        "%E3%80%90%E3%83%86%E3%82%B9%E3%83%88%E7%94%A8%E3%80%91"
        "HENNGE%E4%BD%9C%E6%A5%AD%E7%94%A8Tool.ver3.1.xlsm?web=1#section"
    )
    captured_hash = excel_mod._hash_web_identity_url(workbook_url)
    monkeypatch.setenv(excel_mod.WEB_IDENTITY_ENV_TEST, captured_hash.upper())

    class Workbook:
        FullName = workbook_url

    class Workbooks:
        Count = 1

        def Item(self, index):
            assert index == 1
            return Workbook()

    class Application:
        def __init__(self):
            self.Hwnd = 100
            self.Workbooks = Workbooks()

    logger = DummyLogger()
    calls = {"save_close": 0, "reopen": 0}
    monkeypatch.setattr(mod, "AppLogger", lambda _base_dir: logger)
    monkeypatch.setattr(mod, "load_config", lambda: {"excel": {"path": str(excel_path)}})
    monkeypatch.setattr(excel_mod, "_iter_rot_running_objects", lambda _diag: [])
    monkeypatch.setattr(
        excel_mod,
        "_collect_applications_from_windows",
        lambda _diag: ([(Application(), "accessible_object_from_window")], 1),
    )
    monkeypatch.setattr(excel_mod, "_get_active_excel_application", lambda: None)
    monkeypatch.setattr(
        mod,
        "save_and_close_target_workbook",
        lambda *_args, **_kwargs: calls.__setitem__("save_close", calls["save_close"] + 1),
    )
    monkeypatch.setattr(
        mod,
        "reopen_excel",
        lambda *_args, **_kwargs: calls.__setitem__("reopen", calls["reopen"] + 1),
    )

    rc = mod.main([
        "--expect-open",
        "--detect-only",
        "--web-identity-mode",
        "test",
        "--trace-web-identity",
    ])

    joined = "\n".join(logger.info_messages)
    assert rc == 0
    assert "web_identity_source=environment" in joined
    assert "web_identity_configured=True" in joined
    assert "web_identity_valid=True" in joined
    assert "web_candidate_count=1" in joined
    assert "candidate_hash_generated_count=1" in joined
    assert "internal_hash_equal_count=1" in joined
    assert "web_identity_match_count=1" in joined
    assert "matched_workbook_count=1" in joined
    assert "target_match_method=web_identity" in joined
    assert "target_was_open=True" in joined
    assert "env_present=True" in joined
    assert "env_length_valid=True" in joined
    assert "env_hex_valid=True" in joined
    assert "save_called=False" in joined
    assert "close_called=False" in joined
    assert "reopen_called=False" in joined
    assert captured_hash not in joined
    assert workbook_url not in joined
    assert calls == {"save_close": 0, "reopen": 0}
