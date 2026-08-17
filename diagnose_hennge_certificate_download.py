from __future__ import annotations

import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By

import diagnose_hennge_certificate_detail as cert_detail
import diagnose_hennge_certificate_result as cert_result
import diagnose_hennge_certificate_search as cert_search
from app.browser import Browser
from app.config import ensure_directories, load_config
from app.hennge_handler import HenngeHandler
from app.logger import AppLogger

CERTIFICATES_URL = "https://admin.auth.hennge.com/certificates/"
SEARCH_INPUT_TIMEOUT_SECONDS = 20
RESULT_TIMEOUT_SECONDS = 15
DETAIL_WAIT_TIMEOUT_SECONDS = 20
DOWNLOAD_WAIT_TIMEOUT_SECONDS = 60
DOWNLOAD_STABLE_SECONDS = 2.0
DOWNLOAD_POLL_SECONDS = 0.5
DOWNLOAD_DIAGNOSTIC_DIRNAME = "hennge_download_diagnostic"
TEMP_DOWNLOAD_SUFFIXES = {".crdownload", ".tmp", ".part"}
BLOCKED_DATA_TESTID_TOKENS = {
    "send-installation-email",
    "revoke",
    "delete",
    "save",
    "register",
}


class ResultRowError(RuntimeError):
    pass


class DownloadFolderNotEmptyError(RuntimeError):
    pass


class DownloadTimeoutError(RuntimeError):
    pass


class DownloadMultipleFilesError(RuntimeError):
    pass


def _base_dir() -> Path:
    return Path(__file__).resolve().parent


def _sanitize_url(raw_url: str | None) -> str:
    if not raw_url:
        return ""
    parsed = urlsplit(raw_url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _download_dir() -> Path:
    return _base_dir() / "downloads" / DOWNLOAD_DIAGNOSTIC_DIRNAME


def _safe_attr(element, attr_name: str) -> str:
    return (element.get_attribute(attr_name) or "").strip()


def _is_displayed(element) -> bool:
    try:
        return bool(element.is_displayed())
    except Exception:
        return False


def _ensure_download_dir_ready(download_dir: Path, logger: AppLogger) -> list[Path]:
    download_dir.mkdir(parents=True, exist_ok=True)
    existing = [item for item in download_dir.iterdir() if item.is_file()]
    logger.info(f"専用ダウンロードフォルダー初期件数: {len(existing)}")
    if existing:
        raise DownloadFolderNotEmptyError("専用ダウンロードフォルダーが空ではありません")
    return existing


def _configure_download_directory(driver, download_dir: Path, logger: AppLogger) -> None:
    if driver is None:
        raise RuntimeError("ブラウザードライバーが初期化されていません")

    payload = {"behavior": "allow", "downloadPath": str(download_dir)}
    for command in ("Browser.setDownloadBehavior", "Page.setDownloadBehavior"):
        try:
            driver.execute_cdp_cmd(command, payload)
            logger.info(f"ダウンロード保存先を設定しました: {download_dir.name}")
            return
        except Exception:
            continue
    raise RuntimeError("ダウンロード保存先の設定に失敗しました")


def _is_safe_download_candidate(action: dict[str, object]) -> bool:
    if action.get("label_category") != cert_detail.LABEL_CATEGORY_DOWNLOAD:
        return False

    if str(action.get("type", "")).strip().lower() == "submit":
        return False

    data_testid = str(action.get("data_testid", "")).strip().lower()
    if any(token in data_testid for token in BLOCKED_DATA_TESTID_TOKENS):
        return False

    return True


def _log_download_candidate_preclick(logger: AppLogger, candidate: dict[str, object], candidate_count: int) -> None:
    enabled = True
    element = candidate.get("element")
    if element is not None:
        try:
            enabled = bool(element.is_enabled())
        except Exception:
            enabled = False

    logger.info(
        "download候補確認 "
        f"count={candidate_count}, "
        f"tag={candidate.get('tag', '')}, "
        f"type={candidate.get('type', '')}, "
        f"label_category={candidate.get('label_category', '')}, "
        f"disabled={not enabled}, "
        f"enabled={enabled}"
    )


def _list_new_download_files(download_dir: Path, before_names: set[str]) -> list[Path]:
    current_files = [item for item in download_dir.iterdir() if item.is_file()]
    return [item for item in current_files if item.name not in before_names]


def _wait_for_single_download_file(
    download_dir: Path,
    before_names: set[str],
    logger: AppLogger,
    timeout_seconds: float = DOWNLOAD_WAIT_TIMEOUT_SECONDS,
) -> Path:
    deadline = time.monotonic() + timeout_seconds
    stable_since: float | None = None
    last_file_name = ""
    last_size = -1

    while time.monotonic() < deadline:
        new_files = _list_new_download_files(download_dir, before_names)
        if len(new_files) == 0:
            time.sleep(DOWNLOAD_POLL_SECONDS)
            continue

        if len(new_files) > 1:
            logger.error(f"新規ファイル件数が複数です: {len(new_files)}")
            raise DownloadMultipleFilesError("新規ファイルが複数件検出されました")

        candidate = new_files[0]
        suffix = candidate.suffix.lower()
        if suffix in TEMP_DOWNLOAD_SUFFIXES:
            time.sleep(DOWNLOAD_POLL_SECONDS)
            continue

        try:
            current_size = candidate.stat().st_size
        except FileNotFoundError:
            stable_since = None
            time.sleep(DOWNLOAD_POLL_SECONDS)
            continue

        if current_size <= 0:
            stable_since = None
            last_file_name = candidate.name
            last_size = current_size
            time.sleep(DOWNLOAD_POLL_SECONDS)
            continue

        now = time.monotonic()
        if candidate.name != last_file_name or current_size != last_size:
            last_file_name = candidate.name
            last_size = current_size
            stable_since = now

        if stable_since is not None and (now - stable_since) >= DOWNLOAD_STABLE_SECONDS:
            elapsed = now - (deadline - timeout_seconds)
            logger.info(
                "ダウンロード完了確認 "
                f"extension={suffix or '(none)'}, "
                f"size_gt_zero={current_size > 0}, "
                f"new_file_count=1, "
                f"elapsed={elapsed:.3f}s"
            )
            return candidate

        time.sleep(DOWNLOAD_POLL_SECONDS)

    logger.error("ダウンロード待機がタイムアウトしました")
    raise DownloadTimeoutError("ダウンロード完了がタイムアウトしました")


def _save_diag_no_html_with_notice(logger: AppLogger, browser: Browser, name: str) -> None:
    logger.info("スクリーンショットには個人情報が含まれる可能性があります")
    logger.save_browser_diagnostics(browser.driver, name, save_html=False)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1 or not args[0].strip():
        print("Usage: python diagnose_hennge_certificate_download.py <TEST_ALIAS>")
        return 1

    alias = args[0].strip()

    base_dir = _base_dir()
    config = load_config()
    ensure_directories(config)
    logger = AppLogger(base_dir)
    browser = Browser(base_dir, config)
    download_dir = _download_dir()

    logger.info("診断モード: HENNGE証明書詳細のdownload候補1件のみを読み取り専用で調査します")
    logger.info("結果行クリック1回とdownload候補1回以外のクリック、登録、失効、更新、削除は実行しません")
    logger.info("SMSM、Excel、IMEI、ファイル操作は実行しません")

    try:
        _ensure_download_dir_ready(download_dir, logger)

        browser.start()
        _configure_download_directory(browser.driver, download_dir, logger)

        handler = HenngeHandler(config, logger, browser)
        handler.login()

        if browser.driver is None:
            raise RuntimeError("ログイン後にブラウザー状態を取得できません")

        browser.open(CERTIFICATES_URL)
        browser.wait_for_page_ready(timeout=20)
        logger.info(f"証明書一覧ページURL: {_sanitize_url(browser.driver.current_url)}")

        search_input = cert_search._wait_certificate_search_input_ready(
            browser.driver,
            logger,
            timeout_seconds=SEARCH_INPUT_TIMEOUT_SECONDS,
        )
        try:
            cert_search._set_query_and_submit(search_input, alias, logger)
        except StaleElementReferenceException:
            logger.error("検索欄再取得を実行します: StaleElementReferenceException")
            refreshed = cert_search._find_single_visible_search_input(
                browser.driver,
                cert_search.SEARCH_INPUT_PRIMARY_SELECTOR,
            )
            cert_search._set_query_and_submit(refreshed, alias, logger)

        result_count = cert_search._wait_results_ready(browser, timeout_seconds=RESULT_TIMEOUT_SECONDS)
        cert_search._log_result_state(logger, result_count)

        if result_count == 0:
            return 2
        if result_count > 1:
            return 3

        selector, rows = cert_result._collect_visible_rows(browser.driver)
        logger.info(f"表示中検索結果行件数: {len(rows)}")
        if len(rows) != 1:
            return 4

        row = rows[0]
        cert_detail._assert_row_click_safety(row)

        before_url = _sanitize_url(browser.driver.current_url)
        before_path = urlsplit(before_url).path
        logger.info(f"クリック前URL: {before_url}")
        logger.info(f"結果行選択方式: {selector}")

        logger.info("結果行クリック開始")
        row.click()
        logger.info("結果行クリック完了")

        detail_state = cert_detail._wait_detail_ready(
            browser.driver,
            before_path,
            logger,
            timeout_seconds=DETAIL_WAIT_TIMEOUT_SECONDS,
        )
        if detail_state.get("detail_method") != cert_detail.DETAIL_METHOD_DIALOG:
            raise RuntimeError("詳細領域をdialogとして特定できませんでした")

        detail_area = detail_state["detail_area"]
        actions = cert_detail._extract_action_elements(detail_area)
        download_candidates = [item for item in actions if _is_safe_download_candidate(item)]
        logger.info(f"download候補要素数: {len(download_candidates)}")

        if len(download_candidates) == 0:
            return 6
        if len(download_candidates) > 1:
            return 7

        candidate = download_candidates[0]
        _log_download_candidate_preclick(logger, candidate, len(download_candidates))

        before_files = _ensure_download_dir_ready(download_dir, logger)
        before_names = {item.name for item in before_files}

        logger.info("download候補クリック開始")
        candidate["element"].click()
        logger.info("download候補クリック完了")

        _wait_for_single_download_file(
            download_dir,
            before_names,
            logger,
            timeout_seconds=DOWNLOAD_WAIT_TIMEOUT_SECONDS,
        )
        return 0
    except KeyboardInterrupt:
        logger.error("診断を中断しました: KeyboardInterrupt")
        try:
            _save_diag_no_html_with_notice(logger, browser, "hennge_certificate_download_interrupted")
        except Exception:
            logger.exception("中断時の診断情報保存に失敗しました")
        return 130
    except DownloadFolderNotEmptyError:
        logger.error("専用ダウンロードフォルダーが空ではありません")
        return 10
    except ResultRowError:
        logger.exception("結果行判定に失敗しました")
        return 4
    except cert_detail.ResultRowError:
        logger.exception("結果行判定に失敗しました")
        return 4
    except DownloadTimeoutError:
        return 8
    except DownloadMultipleFilesError:
        return 9
    except RuntimeError as ex:
        if str(ex) in {"詳細画面へ遷移しませんでした", "詳細領域を特定できませんでした", "詳細領域をdialogとして特定できませんでした"}:
            logger.error("詳細画面表示の待機に失敗しました")
            return 5
        logger.exception("HENNGE証明書ダウンロード診断に失敗しました")
        try:
            _save_diag_no_html_with_notice(logger, browser, "hennge_certificate_download_failure")
        except Exception:
            logger.exception("失敗時の診断情報保存に失敗しました")
        return 1
    except Exception:
        logger.exception("HENNGE証明書ダウンロード診断に失敗しました")
        try:
            _save_diag_no_html_with_notice(logger, browser, "hennge_certificate_download_failure")
        except Exception:
            logger.exception("失敗時の診断情報保存に失敗しました")
        return 1
    finally:
        browser.quit()


if __name__ == "__main__":
    raise SystemExit(main())