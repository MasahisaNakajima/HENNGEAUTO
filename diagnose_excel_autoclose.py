from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from app.config import load_config
from app.excel_reader import ExcelReader
from app.excel_session import (
    ReadOnlyWorkbookError,
    SaveCloseWorkbookError,
    WEB_IDENTITY_ENV_PRODUCTION,
    WEB_IDENTITY_ENV_TEST,
    WebIdentityResolution,
    detect_target_workbook,
    _inspect_web_identity_environment,
    _resolve_web_identity_hash,
    save_and_close_target_workbook,
)
from app.logger import AppLogger
from app.main import reopen_excel


class UnlockTimeoutError(RuntimeError):
    pass


def _base_dir() -> Path:
    return Path(__file__).resolve().parent


def _emit_kv(logger: AppLogger, key: str, value) -> None:
    logger.info(f"{key}={value}")


def _wait_unlock(reader: ExcelReader, timeout_sec: float = 15.0, interval_sec: float = 0.5) -> None:
    end_time = time.monotonic() + timeout_sec
    while time.monotonic() < end_time:
        if not reader.is_file_open():
            return
        time.sleep(interval_sec)
    raise UnlockTimeoutError("unlock timeout")


class _NoopLogger:
    def info(self, _message: str) -> None:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--expect-open", action="store_true", help="Fail if the target workbook is not detected as open")
    parser.add_argument("--detect-only", action="store_true", help="Run workbook detection diagnostics without any Excel operations")
    parser.add_argument(
        "--capture-web-identity",
        action="store_true",
        help="Capture web identity hash from a single URL workbook candidate (requires --detect-only)",
    )
    parser.add_argument(
        "--web-identity-mode",
        choices=["test", "production"],
        default="test",
        help="Select which web identity scope to use",
    )
    parser.add_argument(
        "--trace-web-identity",
        action="store_true",
        help="Emit non-secret web identity resolution diagnostics (requires --detect-only)",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    detect_only_mode = bool(args.detect_only)

    if args.capture_web_identity and not args.detect_only:
        parser.error("--capture-web-identity は --detect-only と併用してください")
    if args.trace_web_identity and not args.detect_only:
        parser.error("--trace-web-identity は --detect-only と併用してください")

    logger = AppLogger(_base_dir())

    reopen_attempted = False
    reopen_path: Path | None = None
    target_exists = False
    failed_stage = "init"
    target_was_open = False

    def _reopen_once() -> None:
        nonlocal reopen_attempted
        if reopen_attempted:
            return
        reopen_attempted = True
        _emit_kv(logger, "reopen_started", True)
        _emit_kv(logger, "reopen_called", True)

        signal = {"failed": False}

        def _emit_from_reopen(message: str) -> None:
            if "起動に失敗" in message:
                signal["failed"] = True

        reopen_excel(reopen_path, _emit_from_reopen)  # type: ignore[arg-type]
        if signal["failed"]:
            _emit_kv(logger, "reopen_completed", False)
            raise RuntimeError("reopen failed")

        _emit_kv(logger, "reopen_completed", True)

    try:
        failed_stage = "config_loaded"
        config = load_config()

        excel_path = (config.get("excel", {}) or {}).get("path", "")
        if not excel_path:
            _emit_kv(logger, "target_exists", False)
            return 2

        reopen_path = Path(excel_path)
        target_exists = reopen_path.exists()
        _emit_kv(logger, "target_exists", target_exists)
        if not target_exists:
            return 2

        excel_cfg = config.get("excel", {}) or {}
        web_identity = _resolve_web_identity_hash(excel_cfg, args.web_identity_mode)
        environment_diagnostics = _inspect_web_identity_environment(args.web_identity_mode)

        failed_stage = "detection"
        detection = detect_target_workbook(
            reopen_path.resolve(),
            configured_web_identity_hash=web_identity.hash_value,
            web_identity_source=web_identity.source,
            web_identity_valid=web_identity.valid,
            capture_web_identity=bool(args.capture_web_identity),
            web_identity_mode=args.web_identity_mode,
            timeout_sec=15.0,
            interval_sec=0.5,
        )

        _emit_kv(logger, "xlmain_count", detection.xlmain_count)
        _emit_kv(logger, "xldesk_count", detection.xldesk_count)
        _emit_kv(logger, "excel7_count", detection.excel7_count)
        _emit_kv(logger, "accessible_object_call_count", detection.accessible_object_call_count)
        _emit_kv(logger, "accessible_object_success_count", detection.accessible_object_success_count)
        _emit_kv(logger, "dispatch_wrap_success_count", detection.dispatch_wrap_success_count)
        _emit_kv(logger, "window_object_count", detection.window_object_count)
        _emit_kv(logger, "workbook_object_count", detection.workbook_object_count)
        _emit_kv(logger, "application_object_count", detection.application_object_count)
        _emit_kv(logger, "unknown_object_count", detection.unknown_object_count)
        _emit_kv(logger, "application_property_success_count", detection.application_property_success_count)
        _emit_kv(logger, "application_count_before_dedupe", detection.application_count_before_dedupe)
        _emit_kv(logger, "application_count_after_dedupe", detection.application_count_after_dedupe)
        _emit_kv(logger, "application_workbooks_count_success", detection.application_workbooks_count_success)
        _emit_kv(logger, "application_workbooks_count_failure", detection.application_workbooks_count_failure)
        _emit_kv(logger, "workbook_item_success_count", detection.workbook_item_success_count)
        _emit_kv(logger, "workbook_item_failure_count", detection.workbook_item_failure_count)
        _emit_kv(logger, "workbook_fullname_success_count", detection.workbook_fullname_success_count)
        _emit_kv(logger, "workbook_fullname_failure_count", detection.workbook_fullname_failure_count)
        _emit_kv(logger, "normalized_path_success_count", detection.normalized_path_success_count)
        _emit_kv(logger, "exact_path_match_count", detection.exact_path_match_count)
        _emit_kv(logger, "hwnd_unavailable_count", detection.hwnd_unavailable_count)
        _emit_kv(logger, "rot_moniker_count", detection.rot_moniker_count)
        _emit_kv(logger, "rot_get_object_success_count", detection.rot_get_object_success_count)
        _emit_kv(logger, "rot_get_object_failure_count", detection.rot_get_object_failure_count)
        _emit_kv(logger, "rot_workbook_candidate_count", detection.rot_workbook_candidate_count)
        _emit_kv(logger, "rot_fullname_success_count", detection.rot_fullname_success_count)
        _emit_kv(logger, "rot_fullname_failure_count", detection.rot_fullname_failure_count)
        _emit_kv(logger, "rot_normalized_path_success_count", detection.rot_normalized_path_success_count)
        _emit_kv(logger, "rot_exact_path_match_count", detection.rot_exact_path_match_count)
        _emit_kv(logger, "rot_duplicate_candidate_count", detection.rot_duplicate_candidate_count)
        _emit_kv(logger, "native_dispatch_pointer_success_count", detection.native_dispatch_pointer_success_count)
        _emit_kv(logger, "native_dispatch_pointer_null_count", detection.native_dispatch_pointer_null_count)
        _emit_kv(logger, "native_dispatch_wrap_success_count", detection.native_dispatch_wrap_success_count)
        _emit_kv(logger, "native_dispatch_wrap_failure_count", detection.native_dispatch_wrap_failure_count)
        _emit_kv(logger, "direct_application_success_count", detection.direct_application_success_count)
        _emit_kv(logger, "parent_application_success_count", detection.parent_application_success_count)
        _emit_kv(logger, "grandparent_application_success_count", detection.grandparent_application_success_count)
        _emit_kv(logger, "native_application_failure_count", detection.native_application_failure_count)
        _emit_kv(logger, "native_workbooks_count_success", detection.native_workbooks_count_success)
        _emit_kv(logger, "native_workbooks_count_failure", detection.native_workbooks_count_failure)

        _emit_kv(logger, "observed_excel_window_count", detection.observed_excel_window_count)
        _emit_kv(logger, "observed_application_count", detection.observed_application_count)
        _emit_kv(logger, "observed_workbook_count", detection.observed_workbook_count)
        _emit_kv(logger, "matched_workbook_count", detection.matched_workbook_count)
        _emit_kv(logger, "detection_method", detection.detection_method)
        _emit_kv(logger, "configured_path_kind", detection.configured_path_kind)
        _emit_kv(logger, "workbook_path_kind", detection.workbook_path_kind)
        _emit_kv(logger, "configured_path_exists", detection.configured_path_exists)
        _emit_kv(logger, "workbook_path_exists", detection.workbook_path_exists)
        _emit_kv(logger, "extension_equal", detection.extension_equal)
        _emit_kv(logger, "basename_equal", detection.basename_equal)
        _emit_kv(logger, "normalized_equal", detection.normalized_equal)
        _emit_kv(logger, "unicode_normalized_equal", detection.unicode_normalized_equal)
        _emit_kv(logger, "samefile_equal", detection.samefile_equal)
        _emit_kv(logger, "parent_equal", detection.parent_equal)
        _emit_kv(logger, "target_match_method", detection.target_match_method)
        _emit_kv(logger, "compared_workbook_count", detection.compared_workbook_count)
        _emit_kv(logger, "local_local_count", detection.local_local_count)
        _emit_kv(logger, "local_url_count", detection.local_url_count)
        _emit_kv(logger, "basename_equal_count", detection.basename_equal_count)
        _emit_kv(logger, "normalized_equal_count", detection.normalized_equal_count)
        _emit_kv(logger, "unicode_normalized_equal_count", detection.unicode_normalized_equal_count)
        _emit_kv(logger, "samefile_equal_count", detection.samefile_equal_count)
        _emit_kv(logger, "samefile_unavailable_count", detection.samefile_unavailable_count)
        _emit_kv(logger, "web_identity_configured", detection.web_identity_configured)
        _emit_kv(logger, "web_identity_source", detection.web_identity_source)
        _emit_kv(logger, "web_identity_valid", detection.web_identity_valid)
        _emit_kv(logger, "web_candidate_count", detection.web_candidate_count)
        _emit_kv(logger, "candidate_hash_generated_count", detection.candidate_hash_generated_count)
        _emit_kv(logger, "internal_hash_equal_count", detection.internal_hash_equal_count)
        _emit_kv(logger, "web_identity_match_count", detection.web_identity_match_count)
        _emit_kv(logger, "capture_succeeded", detection.capture_succeeded)

        if args.trace_web_identity:
            _emit_kv(logger, "selected_mode", environment_diagnostics.selected_mode)
            _emit_kv(logger, "env_name_selected", environment_diagnostics.env_name_selected)
            _emit_kv(logger, "env_present", environment_diagnostics.env_present)
            _emit_kv(logger, "env_value_length", environment_diagnostics.env_value_length)
            _emit_kv(logger, "env_length_valid", environment_diagnostics.env_length_valid)
            _emit_kv(logger, "env_hex_valid", environment_diagnostics.env_hex_valid)
            _emit_kv(logger, "resolved_configured", web_identity.valid)
            _emit_kv(logger, "resolved_valid", web_identity.valid)
            _emit_kv(logger, "capture_hash_generated", detection.capture_succeeded)
            _emit_kv(logger, "comparison_hash_generated", detection.candidate_hash_generated_count > 0)
            _emit_kv(logger, "internal_hash_equal", detection.internal_hash_equal_count > 0)

        logger.info("object_kind=window")
        _emit_kv(logger, "count", detection.window_object_count)
        logger.info("object_kind=workbook")
        _emit_kv(logger, "count", detection.workbook_object_count)
        logger.info("object_kind=application")
        _emit_kv(logger, "count", detection.application_object_count)
        logger.info("object_kind=unknown")
        _emit_kv(logger, "count", detection.unknown_object_count)

        for stage, exception_type, count in detection.detection_exceptions:
            logger.info("detection_exception")
            if stage in {
                "build_iid_idispatch",
                "configure_accessible_object_from_window",
                "call_accessible_object_from_window",
                "native_dispatch_wrap",
            }:
                _emit_kv(logger, "failed_native_stage", stage)
            else:
                _emit_kv(logger, "stage", stage)
            _emit_kv(logger, "exception_type", exception_type)
            _emit_kv(logger, "count", count)

        failed_stage = "save_close"
        target_was_open = bool(detection.workbook is not None and detection.application is not None)
        _emit_kv(logger, "target_was_open", target_was_open)

        if args.detect_only:
            _emit_kv(logger, "save_called", False)
            _emit_kv(logger, "close_called", False)
            _emit_kv(logger, "unlock_completed", False)
            _emit_kv(logger, "reopen_called", False)
            if args.capture_web_identity:
                if detection.capture_succeeded and detection.captured_web_identity_hash:
                    env_name = WEB_IDENTITY_ENV_TEST if args.web_identity_mode == "test" else WEB_IDENTITY_ENV_PRODUCTION
                    print(f"$env:{env_name}=\"{detection.captured_web_identity_hash}\"")
                    return 0
                return 8
            if target_was_open:
                return 0
            return 8

        if not target_was_open:
            _emit_kv(logger, "save_called", False)
            _emit_kv(logger, "close_called", False)
            _emit_kv(logger, "unlock_completed", False)
            _emit_kv(logger, "reopen_called", False)
            if args.expect_open:
                return 8
            return 7

        save_and_close_target_workbook(
            reopen_path.resolve(),
            _NoopLogger(),
            configured_web_identity_hash=web_identity.hash_value,
            web_identity_source=web_identity.source,
            web_identity_valid=web_identity.valid,
            web_identity_mode=args.web_identity_mode,
        )
        _emit_kv(logger, "save_called", True)
        _emit_kv(logger, "close_called", True)
        _emit_kv(logger, "save_close_completed", True)

        failed_stage = "unlock_wait"
        _emit_kv(logger, "unlock_completed", False)
        reader = ExcelReader(str(reopen_path))
        _wait_unlock(reader)
        _emit_kv(logger, "unlock_completed", True)

        failed_stage = "reopen"
        time.sleep(3.0)
        _reopen_once()
        return 0
    except KeyboardInterrupt:
        return 130
    except ReadOnlyWorkbookError as exc:
        _emit_kv(logger, "failed_stage", failed_stage)
        _emit_kv(logger, "exception_type", type(exc).__name__)
        return 3
    except SaveCloseWorkbookError as exc:
        _emit_kv(logger, "failed_stage", failed_stage)
        _emit_kv(logger, "exception_type", type(exc).__name__)
        return 4
    except UnlockTimeoutError as exc:
        _emit_kv(logger, "failed_stage", failed_stage)
        _emit_kv(logger, "exception_type", type(exc).__name__)
        return 5
    except Exception as exc:
        _emit_kv(logger, "failed_stage", failed_stage)
        _emit_kv(logger, "exception_type", type(exc).__name__)
        if failed_stage == "reopen":
            return 6
        return 1
    finally:
        if (not detect_only_mode) and target_exists and target_was_open and reopen_path is not None and not reopen_attempted:
            try:
                _reopen_once()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
