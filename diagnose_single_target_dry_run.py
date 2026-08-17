from __future__ import annotations

import sys
import time
from pathlib import Path

from app.config import load_config
from app.excel_reader import ExcelReader
from app.excel_session import (
    ReadOnlyWorkbookError,
    SaveCloseWorkbookError,
    _resolve_web_identity_hash,
    detect_target_workbook,
    save_and_close_target_workbook,
)
from app.imei_normalizer import normalize_imei
from app.logger import AppLogger
from app.main import reopen_excel


PLANNED_STAGES = (
    "target_loaded",
    "target_validated",
    "hennge_lookup_planned",
    "certificate_download_planned",
    "filename_normalization_planned",
    "smsm_lookup_planned",
    "certificate_upload_planned",
    "result_recording_planned",
)


class UnlockTimeoutError(RuntimeError):
    pass


class ReopenFailedError(RuntimeError):
    pass


class _NoopLogger:
    def info(self, _message: str) -> None:
        return None


def _base_dir() -> Path:
    return Path(__file__).resolve().parent


def _emit(logger, key: str, value) -> None:
    logger.info(f"{key}={value}")


def _log_failure(logger, stage: str, exc: BaseException) -> None:
    _emit(logger, "failed_stage", stage)
    _emit(logger, "exception_type", type(exc).__name__)


def _wait_unlock(reader: ExcelReader, timeout_sec: float = 15.0, interval_sec: float = 0.5) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if not reader.is_file_open():
            return
        time.sleep(interval_sec)
    raise UnlockTimeoutError("unlock timeout")


def _run_dry_run(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if args not in ([], ["--dry-run"]):
        return 1

    logger = AppLogger(_base_dir())
    current_stage = "init"
    reopen_path: Path | None = None
    reopen_required = False
    reopen_attempted = False

    _emit(logger, "dry_run", True)
    _emit(logger, "external_action_called", False)

    def _reopen_once() -> None:
        nonlocal reopen_attempted
        if reopen_attempted or reopen_path is None:
            return
        reopen_attempted = True
        reopen_signal = {"failed": False}

        def _capture_reopen_result(message: str) -> None:
            if "起動に失敗" in message:
                reopen_signal["failed"] = True

        reopen_excel(reopen_path, _capture_reopen_result)
        _emit(logger, "reopen_called", True)
        if reopen_signal["failed"]:
            raise ReopenFailedError("reopen failed")

    try:
        current_stage = "config_loaded"
        config = load_config()
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
        _emit(logger, "target_was_open", target_was_open)

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
            _emit(logger, "matched_workbook_count", detection.matched_workbook_count)
            _emit(logger, "target_match_method", detection.target_match_method)
            if (
                detection.workbook is None
                or detection.application is None
                or detection.matched_workbook_count != 1
            ):
                _emit(logger, "save_called", False)
                _emit(logger, "close_called", False)
                _emit(logger, "unlock_completed", False)
                _emit(logger, "read_targets_called", False)
                return 9

            current_stage = "save_close"
            _emit(logger, "save_called", True)
            saved_and_closed = save_and_close_target_workbook(
                reopen_path.resolve(),
                _NoopLogger(),
                configured_web_identity_hash=web_identity.hash_value,
                web_identity_source=web_identity.source,
                web_identity_valid=web_identity.valid,
                web_identity_mode="test",
            )
            if not saved_and_closed:
                _emit(logger, "close_called", False)
                _emit(logger, "unlock_completed", False)
                _emit(logger, "read_targets_called", False)
                return 12
            _emit(logger, "close_called", True)
            reopen_required = True

            current_stage = "unlock_wait"
            _wait_unlock(reader)
            _emit(logger, "unlock_completed", True)
        else:
            _emit(logger, "matched_workbook_count", 0)
            _emit(logger, "target_match_method", "none")
            _emit(logger, "save_called", False)
            _emit(logger, "close_called", False)
            _emit(logger, "unlock_completed", True)

        current_stage = "read_targets"
        _emit(logger, "read_targets_called", True)
        targets = reader.read_targets()
        target_count = len(targets)
        _emit(logger, "target_count", target_count)
        if target_count == 0:
            _emit(logger, "selected_target_count", 0)
            return 2

        current_stage = "target_validation"
        target = targets[0]
        _emit(logger, "selected_target_count", 1)
        if not isinstance(target, dict):
            return 1

        alias = target.get("alias")
        serial = target.get("serial")
        imei_value = target.get("imei")
        if not str(alias or "").strip() or not str(serial or "").strip() or not str(imei_value or "").strip():
            return 10
        imei = normalize_imei(imei_value)
        if not imei:
            return 10

        _emit(logger, "alias_present", True)
        _emit(logger, "serial_present", True)
        _emit(logger, "imei_valid", True)
        _emit(logger, "planned_stage_count", len(PLANNED_STAGES))
        return 0
    except KeyboardInterrupt:
        _log_failure(logger, current_stage, KeyboardInterrupt())
        return 130
    except (ReadOnlyWorkbookError, SaveCloseWorkbookError):
        _log_failure(logger, current_stage, sys.exc_info()[1])
        return 12
    except UnlockTimeoutError as exc:
        _log_failure(logger, current_stage, exc)
        _emit(logger, "unlock_completed", False)
        _emit(logger, "read_targets_called", False)
        return 13
    except ValueError:
        _log_failure(logger, current_stage, sys.exc_info()[1])
        return 5
    except (FileNotFoundError, KeyError):
        _log_failure(logger, current_stage, sys.exc_info()[1])
        return 8
    except Exception:
        _log_failure(logger, current_stage, sys.exc_info()[1])
        return 1
    finally:
        if reopen_required and not reopen_attempted:
            _reopen_once()


def main(argv: list[str] | None = None) -> int:
    try:
        return _run_dry_run(argv)
    except ReopenFailedError:
        return 14


if __name__ == "__main__":
    raise SystemExit(main())
