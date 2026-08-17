from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from app.browser import Browser
from app.config import ensure_directories, load_config
from app.excel_reader import ExcelReader
from app.hennge_handler import HenngeHandler
from app.logger import AppLogger

CERTIFICATES_URL = "https://admin.auth.hennge.com/certificates/"
RESULT_TIMEOUT_SECONDS = 15
SEARCH_INPUT_TIMEOUT_SECONDS = 20
SEARCH_INPUT_PRIMARY_SELECTOR = "input[name='query'][aria-label='Search']"
SEARCH_INPUT_FALLBACK_SELECTOR = "input[name='query']"


class SearchInputError(RuntimeError):
    pass


def _base_dir() -> Path:
    return Path(__file__).resolve().parent


def _sanitize_url(raw_url: str | None) -> str:
    if not raw_url:
        return ""
    parsed = urlsplit(raw_url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _mask_query(query: str) -> str:
    text = (query or "").strip()
    if len(text) <= 3:
        return "***"
    return f"{text[:2]}***{text[-1]}"


def _is_valid_search_input_element(element, *, require_aria_label: bool):
    tag_name = (element.tag_name or "").strip().lower()
    if tag_name != "input":
        return False

    name_attr = (element.get_attribute("name") or "").strip()
    if name_attr != "query":
        return False

    aria_label_attr = (element.get_attribute("aria-label") or "").strip()
    if require_aria_label and aria_label_attr != "Search":
        return False
    if not require_aria_label and aria_label_attr not in {"", "Search"}:
        return False

    type_attr = (element.get_attribute("type") or "").strip().lower()
    if type_attr != "text":
        return False

    if not element.is_displayed():
        return False
    if not element.is_enabled():
        return False
    if element.get_attribute("disabled") is not None:
        return False
    if element.get_attribute("readonly") is not None:
        return False
    return True


def _find_single_visible_search_input(driver, selector: str = SEARCH_INPUT_FALLBACK_SELECTOR):
    require_aria_label = selector == SEARCH_INPUT_PRIMARY_SELECTOR
    candidates = []
    for element in driver.find_elements(By.CSS_SELECTOR, selector):
        if _is_valid_search_input_element(element, require_aria_label=require_aria_label):
            candidates.append(element)
    if len(candidates) != 1:
        raise SearchInputError(f"検索欄候補数が不正です: {len(candidates)}")
    return candidates[0]


def _get_page_heading_text(driver) -> str:
    heading_selectors = ["h1", "h2", "[role='heading']"]
    for selector in heading_selectors:
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            try:
                if not element.is_displayed():
                    continue
                text = (element.text or "").strip()
                if text:
                    return text
            except Exception:
                continue
    return ""


def _collect_visible_candidates(driver, selector: str):
    require_aria_label = selector == SEARCH_INPUT_PRIMARY_SELECTOR
    visible = []
    for element in driver.find_elements(By.CSS_SELECTOR, selector):
        if _is_valid_search_input_element(element, require_aria_label=require_aria_label):
            visible.append(element)
    return visible


def _wait_certificate_search_input_ready(driver, logger: AppLogger, timeout_seconds: int = SEARCH_INPUT_TIMEOUT_SECONDS):
    if driver is None:
        raise RuntimeError("ブラウザドライバーが存在しません")

    wait_start = time.monotonic()
    end_time = time.monotonic() + timeout_seconds
    visible_once = False
    search_error_logged = False
    while time.monotonic() < end_time:
        path = urlsplit(_sanitize_url(driver.current_url)).path
        visible = []
        visible_fallback = []
        try:
            visible = _collect_visible_candidates(driver, SEARCH_INPUT_PRIMARY_SELECTOR)
            if len(visible) == 1:
                input_element = visible[0]
                if input_element.is_enabled() and input_element.get_attribute("disabled") is None:
                    elapsed = time.monotonic() - wait_start
                    heading = _get_page_heading_text(driver)
                    if heading:
                        heading_l = heading.lower()
                        if "証明書一覧" in heading or "デバイス証明書" in heading or "device certificate" in heading_l:
                            logger.info(f"証明書一覧見出しを確認: {heading}")
                        else:
                            logger.info(f"証明書一覧見出し(許容外だが続行): {heading}")

                    tag_name = (input_element.tag_name or "").strip().lower()
                    id_attr = (input_element.get_attribute("id") or "").strip()
                    name_attr = (input_element.get_attribute("name") or "").strip()
                    type_attr = (input_element.get_attribute("type") or "").strip().lower()
                    logger.info(
                        "検索欄待機成功 "
                        f"elapsed={elapsed:.3f}s, "
                        f"tag={tag_name}, id={id_attr}, name={name_attr}, type={type_attr}, enabled={input_element.is_enabled()}"
                    )
                    return input_element

            if len(visible) == 0:
                visible_fallback = _collect_visible_candidates(driver, SEARCH_INPUT_FALLBACK_SELECTOR)

                if path == "/certificates/" and len(visible_fallback) == 1:
                    input_element = visible_fallback[0]
                    if input_element.is_enabled() and input_element.get_attribute("disabled") is None:
                        elapsed = time.monotonic() - wait_start
                        heading = _get_page_heading_text(driver)
                        if heading:
                            heading_l = heading.lower()
                            if "証明書一覧" in heading or "デバイス証明書" in heading or "device certificate" in heading_l:
                                logger.info(f"証明書一覧見出しを確認: {heading}")
                            else:
                                logger.info(f"証明書一覧見出し(許容外だが続行): {heading}")

                        tag_name = (input_element.tag_name or "").strip().lower()
                        id_attr = (input_element.get_attribute("id") or "").strip()
                        name_attr = (input_element.get_attribute("name") or "").strip()
                        type_attr = (input_element.get_attribute("type") or "").strip().lower()
                        logger.info(
                            "検索欄待機成功 "
                            f"elapsed={elapsed:.3f}s, "
                            f"tag={tag_name}, id={id_attr}, name={name_attr}, type={type_attr}, enabled={input_element.is_enabled()}"
                        )
                        return input_element
        except Exception as ex:
            if not search_error_logged:
                logger.error(f"検索欄要素探索時例外: {type(ex).__name__}")
                search_error_logged = True
            raise

        if len(visible) > 0 or len(visible_fallback) > 0:
            visible_once = True

        time.sleep(0.5)

    # Timeout classification
    final_path = urlsplit(_sanitize_url(driver.current_url)).path
    final_visible = _collect_visible_candidates(driver, SEARCH_INPUT_PRIMARY_SELECTOR)
    if len(final_visible) == 0:
        final_visible = _collect_visible_candidates(driver, SEARCH_INPUT_FALLBACK_SELECTOR)

    if final_path != "/certificates/":
        raise RuntimeError("証明書一覧遷移失敗: URLが/certificates/になりませんでした")
    if len(final_visible) > 1:
        raise RuntimeError("証明書検索欄判定失敗: 表示中候補が複数あります")
    if len(final_visible) == 0:
        raise RuntimeError("証明書検索欄判定失敗: 検索欄が表示されませんでした")
    if visible_once:
        raise RuntimeError("証明書検索欄判定失敗: 検索欄が有効化されませんでした")
    raise RuntimeError("証明書検索欄判定失敗: 検索欄の待機がタイムアウトしました")


def _set_query_and_submit(search_input, query: str, logger: AppLogger) -> None:
    if not search_input.is_displayed():
        raise SearchInputError("検索欄が非表示です")
    if not search_input.is_enabled():
        raise SearchInputError("検索欄が無効です")
    if search_input.get_attribute("disabled") is not None:
        raise SearchInputError("検索欄がdisabledです")
    if search_input.get_attribute("readonly") is not None:
        raise SearchInputError("検索欄がreadonlyです")

    logger.info("検索欄への入力開始")
    current_value = search_input.get_attribute("value") or ""
    if current_value:
        search_input.click()
        search_input.send_keys(Keys.CONTROL, "a")
        search_input.send_keys(Keys.BACKSPACE)

    search_input.send_keys(query)
    logger.info("検索欄への入力完了")
    search_input.send_keys(Keys.ENTER)
    logger.info("Enter送信完了")


def _is_no_data_visible(driver) -> bool:
    markers = ["データがありません", "no data", "no results", "結果がありません"]
    candidates = driver.find_elements(By.CSS_SELECTOR, "body *")
    for element in candidates:
        try:
            if not element.is_displayed():
                continue
            text = (element.text or "").strip().lower()
            if any(marker.lower() in text for marker in markers):
                return True
        except Exception:
            continue
    return False


def _count_visible(driver, selector: str) -> int:
    return len([element for element in driver.find_elements(By.CSS_SELECTOR, selector) if _is_displayed(element)])


def _count_visible_rows(driver) -> int:
    selectors = [
        "table tbody tr",
        "[data-testid='certificate-row']",
        "tr[data-testid*='certificate']",
        "li[data-testid*='certificate']",
    ]

    for selector in selectors:
        rows = []
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            try:
                text = (element.text or "").strip().lower()
                if text in {"", "no data", "no results", "結果がありません", "データがありません"}:
                    continue
                rows.append(element)
            except Exception:
                continue
        if rows:
            return len(rows)

    return 0


def _count_from_range_label(driver) -> int | None:
    selectors = [
        ".pagination",
        ".table-footer",
        ".v-data-footer",
        "[class*='page']",
        "[class*='result']",
        "body",
    ]
    pattern = re.compile(r"\b(\d+)\s*-\s*(\d+)\b")

    for selector in selectors:
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            try:
                if not element.is_displayed():
                    continue
                text = (element.text or "").strip()
                match = pattern.search(text)
                if not match:
                    continue
                start = int(match.group(1))
                end = int(match.group(2))
                if end >= start and start > 0:
                    return end - start + 1
            except Exception:
                continue
    return None


def _determine_result_count(driver) -> int:
    row_count = _count_visible_rows(driver)
    if row_count > 0:
        return row_count

    if _is_no_data_visible(driver):
        return 0

    range_count = _count_from_range_label(driver)
    if range_count is not None:
        return range_count

    return 0


def _wait_results_ready(browser: Browser, timeout_seconds: int = RESULT_TIMEOUT_SECONDS) -> int:
    if browser.driver is None:
        raise RuntimeError("ブラウザドライバーが存在しません")

    def result_state(driver):
        if driver is None:
            raise RuntimeError("検索待機中にブラウザーセッションが無効になりました")
        _ = driver.current_url
        loading_count = _count_visible(driver, "[aria-busy='true'],[data-testid*='loading'],[class*='loading'],[class*='spinner']")
        error_count = _count_visible(driver, "[role='alert'],[data-testid*='error'],.error")
        if error_count:
            raise RuntimeError("HENNGE検索結果エラー表示を検出しました")
        visible_row_count = _count_visible_rows(driver)
        no_data_visible = _is_no_data_visible(driver)
        if loading_count == 0 and (visible_row_count > 0 or no_data_visible):
            count = visible_row_count
            return count
        return False

    try:
        return WebDriverWait(browser.driver, timeout_seconds, poll_frequency=0.2).until(result_state)
    except TimeoutError:
        raise TimeoutError("検索結果の読み込みがタイムアウトしました") from None
    except Exception:
        raise


def _log_result_state(logger: AppLogger, count: int) -> None:
    if count <= 0:
        logger.info("検索結果件数: 0件")
        logger.info("検索結果判定: 0件")
        return
    if count == 1:
        logger.info("検索結果件数: 1件")
        logger.info("検索結果判定: 1件")
        return
    logger.info(f"検索結果件数: {count}件")
    logger.info("検索結果判定: 複数件")


def _save_diag_no_html(logger: AppLogger, browser: Browser, name: str) -> None:
    logger.save_browser_diagnostics(browser.driver, name, save_html=False)


def _read_single_diagnostic_target(config: dict) -> tuple[str, str]:
    excel_path = config.get("excel", {}).get("path")
    if not excel_path:
        raise RuntimeError("検証用Excelパスが設定されていません")
    targets = ExcelReader(str(excel_path)).read_targets()
    if len(targets) != 1:
        raise RuntimeError(f"検証用Excel対象件数が不正です: {len(targets)}")
    target = targets[0]
    return str(target["alias"]), str(target["imei"])


def _verify_certificate_result_without_click(handler: HenngeHandler, target_imei: str, result_count: int, logger: AppLogger) -> bool:
    selector, rows, scan_metrics = handler._scan_certificate_rows_for_imei(target_imei)
    if (
        scan_metrics["observed_exact_match_count"] == 0
        and scan_metrics["scroll_container_scrollable"]
        and not scan_metrics["scroll_container_unique"]
    ):
        logger.info("hennge_result_row_click_called=False")
        return False

    click_refetch_selector, click_refetch_rows = handler._certificate_result_rows()
    expected = handler._normalize_certificate_cell(target_imei)
    subject_found = False
    os_found = False
    subject_candidates = 0
    exact_rows = []
    ios_rows = []
    for row in click_refetch_rows:
        headers, cells = handler._result_row_headers_and_cells(row)
        subject_index = handler._find_result_column_index(headers, {"メール件名のメモ", "mail subject memo"})
        os_index = handler._find_result_column_index(headers, {"os"})
        subject_found = subject_found or subject_index is not None
        os_found = os_found or os_index is not None
        if subject_index is not None and handler._result_cell_value(cells, subject_index):
            subject_candidates += 1
        if subject_index is not None and handler._result_cell_value(cells, subject_index) == expected:
            exact_rows.append(row)
            ios_rows.append(
                os_index is not None
                and handler._normalize_certificate_cell(handler._result_cell_value(cells, os_index)).casefold() == "ios"
            )

    exact_count = len(exact_rows)
    ios_safe_count = int(exact_count == 1 and ios_rows == [True] and handler._safe_result_row(exact_rows[0]))
    safe = ios_safe_count == 1
    logger.info(f"hennge_search_result_count={result_count}")
    logger.info(f"hennge_subject_memo_column_found={subject_found}")
    logger.info(f"hennge_os_column_found={os_found}")
    logger.info(f"hennge_subject_memo_value_candidate_count={subject_candidates}")
    logger.info(f"hennge_subject_memo_exact_match_count={exact_count}")
    logger.info(f"hennge_imei_matched_row_candidate_count={exact_count}")
    logger.info(f"hennge_imei_matched_row_os_ios={exact_count == 1 and ios_rows == [True]}")
    logger.info(f"hennge_imei_matched_row_safe={safe}")
    logger.info(f"hennge_click_refetch_row_count={len(click_refetch_rows)}")
    logger.info(f"hennge_click_refetch_exact_match_count={exact_count}")
    logger.info(f"hennge_click_refetch_safe_candidate_count={ios_safe_count}")
    logger.info("hennge_result_row_click_called=False")
    logger.info("hennge_result_row_click_count=0")
    handler.last_search_observation.update({
        "row_selector": selector,
        "click_refetch_selector": click_refetch_selector,
        "result_count": result_count,
        "subject_memo_column_found": subject_found,
        "os_column_found": os_found,
        "subject_memo_value_candidate_count": subject_candidates,
        "subject_memo_exact_match_count": exact_count,
        "imei_matched_row_candidate_count": exact_count,
        "imei_matched_row_os_ios": exact_count == 1 and ios_rows == [True],
        "imei_matched_row_safe": safe,
        "click_refetch_row_count": len(click_refetch_rows),
        "click_refetch_exact_match_count": exact_count,
        "click_refetch_safe_candidate_count": ios_safe_count,
        "result_row_click_called": False,
        "result_row_click_count": 0,
    })
    return result_count == 3 and subject_found and os_found and exact_count == 1 and ios_safe_count == 1


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1 or not args[0].strip():
        print("Usage: python diagnose_hennge_certificate_search.py <TEST_ALIAS>|--verify-target")
        return 1

    base_dir = _base_dir()
    config = load_config()
    ensure_directories(config)
    logger = AppLogger(base_dir)
    browser = Browser(base_dir, config)
    verify_target = args[0].strip() == "--verify-target"
    alias = args[0].strip()
    target_imei = ""
    if verify_target:
        alias, target_imei = _read_single_diagnostic_target(config)

    logger.info("診断モード: HENNGE証明書一覧検索のみを読み取り専用で実行します")
    logger.info("/users/検索、左メニュークリック、詳細表示、ダウンロード、登録、失効、更新、削除は実行しません")
    logger.info("SMSM、詳細画面、行クリック、パスワード、証明書ファイル、Excel書込みは実行しません")

    try:
        browser.start()
        handler = HenngeHandler(config, logger, browser)
        handler.login()

        if browser.driver is None:
            raise RuntimeError("ログイン後にブラウザー状態を取得できません")

        browser.open(CERTIFICATES_URL)
        browser.wait_for_page_ready(timeout=20)
        logger.info(f"証明書一覧ページURL: {_sanitize_url(browser.driver.current_url)}")

        search_input = _wait_certificate_search_input_ready(browser.driver, logger, timeout_seconds=SEARCH_INPUT_TIMEOUT_SECONDS)
        try:
            _set_query_and_submit(search_input, alias, logger)
        except StaleElementReferenceException:
            logger.error("検索欄再取得を実行します: StaleElementReferenceException")
            refreshed = _find_single_visible_search_input(browser.driver, SEARCH_INPUT_PRIMARY_SELECTOR)
            _set_query_and_submit(refreshed, alias, logger)

        result_count = _wait_results_ready(browser, timeout_seconds=RESULT_TIMEOUT_SECONDS)
        logger.info(f"検索後URL: {_sanitize_url(browser.driver.current_url)}")
        _log_result_state(logger, result_count)

        if verify_target:
            verified = _verify_certificate_result_without_click(handler, target_imei, result_count, logger)
            return 0 if verified else 6

        _save_diag_no_html(logger, browser, "hennge_certificate_search_success")

        if result_count == 0:
            return 2
        if result_count == 1:
            return 0
        return 3
    except KeyboardInterrupt:
        logger.error("診断を中断しました: KeyboardInterrupt")
        try:
            _save_diag_no_html(logger, browser, "hennge_certificate_search_interrupted")
        except Exception:
            logger.exception("中断時の診断情報保存に失敗しました")
        return 130
    except Exception:
        logger.exception("HENNGE証明書検索診断に失敗しました")
        try:
            _save_diag_no_html(logger, browser, "hennge_certificate_search_failure")
        except Exception:
            logger.exception("失敗時の診断情報保存に失敗しました")
        return 1
    finally:
        browser.quit()


if __name__ == "__main__":
    raise SystemExit(main())
