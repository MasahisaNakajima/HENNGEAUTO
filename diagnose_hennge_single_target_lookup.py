from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

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
from app.hennge_handler import HenngeHandler
from app.logger import AppLogger
from app.imei_normalizer import normalize_imei
from diagnose_excel_target import UnlockTimeoutError, _wait_unlock
from diagnose_hennge_user_search import _wait_results_ready
from app.main import reopen_excel


class BrowserStartFailedError(RuntimeError):
    pass


class BrowserQuitFailedError(RuntimeError):
    pass


class ReopenFailedError(RuntimeError):
    pass


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


def _run_lookup(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if args not in ([], ["--lookup"]):
        return 1

    logger = AppLogger(_base_dir())
    config = None
    reader = None
    browser = None
    current_stage = "init"
    reopen_path: Path | None = None
    reopen_required = False
    reopen_attempted = False
    browser_started = False

    _emit(logger, "lookup_mode", "read_only")
    _emit(logger, "certificate_action_called", False)
    _emit(logger, "smsm_action_called", False)
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

        reopen_excel(reopen_path, _capture_reopen_result)
        _emit(logger, "reopen_called", True)
        if signal["failed"]:
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
        else:
            _emit(logger, "matched_workbook_count", 0)
            _emit(logger, "target_match_method", "none")

        current_stage = "read_targets"
        targets = reader.read_targets()
        target_count = len(targets)
        _emit(logger, "target_count", target_count)
        if target_count == 0:
            _emit(logger, "selected_target_count", 0)
            return 2

        target = targets[0]
        _emit(logger, "selected_target_count", 1)
        if not isinstance(target, dict):
            return 1
        alias = target.get("alias")
        serial = target.get("serial")
        imei_value = target.get("imei")
        if not str(alias or "").strip() or not str(serial or "").strip() or not str(imei_value or "").strip():
            return 1
        normalize_imei(imei_value)
        alias_value = str(alias).strip()
        _emit(logger, "alias_present", True)
        _emit(logger, "serial_present", True)
        _emit(logger, "imei_valid", True)

        current_stage = "browser_start"
        browser = Browser(_base_dir(), config)
        try:
            browser.start()
        except Exception as exc:
            raise BrowserStartFailedError("browser start failed") from exc
        browser_started = True
        _emit(logger, "browser_started", True)

        current_stage = "hennge_login"
        handler = HenngeHandler(config, _SilentHandlerLogger(), browser)
        try:
            handler.login()
        except Exception as exc:
            raise RuntimeError("HENNGE login failed") from exc
        _emit(logger, "hennge_login_completed", True)

        current_stage = "hennge_navigation"
        handler.search_user(alias_value)
        _emit(logger, "lookup_called", True)

        current_stage = "lookup_result_wait"
        result_count = _wait_results_ready(browser)
        _emit(logger, "lookup_result_count", result_count)
        if result_count == 0:
            _emit(logger, "lookup_unique", False)
            return 22
        if result_count > 1:
            _emit(logger, "lookup_unique", False)
            return 23
        _emit(logger, "lookup_unique", True)
        return 0
    except KeyboardInterrupt:
        _log_failure(logger, current_stage, KeyboardInterrupt())
        return 130
    except BrowserStartFailedError as exc:
        _log_failure(logger, current_stage, exc)
        return 24
    except BrowserQuitFailedError as exc:
        _log_failure(logger, "browser_quit", exc)
        return 25
    except (ReadOnlyWorkbookError, SaveCloseWorkbookError) as exc:
        _log_failure(logger, current_stage, exc)
        return 12
    except UnlockTimeoutError as exc:
        _log_failure(logger, current_stage, exc)
        return 13
    except RuntimeError as exc:
        _log_failure(logger, current_stage, exc)
        if current_stage == "hennge_login":
            return 20
        if current_stage == "hennge_navigation":
            return 21
        return 1
    except (FileNotFoundError, KeyError) as exc:
        _log_failure(logger, current_stage, exc)
        return 8
    except ValueError as exc:
        _log_failure(logger, current_stage, exc)
        return 1
    except Exception as exc:
        _log_failure(logger, current_stage, exc)
        return 1
    finally:
        if browser_started and browser is not None:
            try:
                browser.quit()
            except Exception as exc:
                raise BrowserQuitFailedError("browser quit failed") from exc
        if reopen_required and not reopen_attempted:
            _reopen_once()


def main(argv: list[str] | None = None) -> int:
    try:
        return _run_lookup(argv)
    except BrowserQuitFailedError:
        return 25
    except ReopenFailedError:
        return 14


if __name__ == "__main__":
    raise SystemExit(main())
