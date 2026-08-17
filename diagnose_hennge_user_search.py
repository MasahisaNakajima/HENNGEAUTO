from __future__ import annotations

import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from app.browser import Browser
from app.config import ensure_directories, load_config
from app.hennge_handler import HenngeHandler
from app.logger import AppLogger

USERS_URL = "https://admin.auth.hennge.com/users/"
RESULT_TIMEOUT_SECONDS = 15


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


def _find_single_visible_search_input(driver):
    elements = driver.find_elements(By.CSS_SELECTOR, "input[name='query'][type='text']")
    visible = [element for element in elements if element.is_displayed()]
    if len(visible) != 1:
        raise SearchInputError(f"検索欄候補数が不正です: {len(visible)}")
    return visible[0]


def _set_query_and_submit(search_input, query: str) -> None:
    if not search_input.is_enabled():
        raise SearchInputError("検索欄が無効です")
    if search_input.get_attribute("disabled") is not None:
        raise SearchInputError("検索欄がdisabledです")
    if search_input.get_attribute("readonly") is not None:
        raise SearchInputError("検索欄がreadonlyです")

    current_value = search_input.get_attribute("value") or ""
    if current_value:
        search_input.click()
        search_input.send_keys(Keys.CONTROL, "a")
        search_input.send_keys(Keys.BACKSPACE)

    search_input.send_keys(query)
    search_input.send_keys(Keys.ENTER)


def _count_visible_results(driver) -> int:
    selectors = [
        "table tbody tr",
        "[data-testid='user-row']",
        "tr[data-testid*='user']",
        "li[data-testid*='user']",
    ]

    for selector in selectors:
        rows = []
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            try:
                if element.is_displayed():
                    rows.append(element)
            except Exception:
                continue

        if rows:
            filtered = []
            for row in rows:
                text = (row.text or "").strip().lower()
                if text in {"", "no data", "no results", "該当なし", "結果がありません"}:
                    continue
                filtered.append(row)
            return len(filtered)

    return 0


def _wait_results_ready(browser: Browser, timeout_seconds: int = RESULT_TIMEOUT_SECONDS) -> int:
    if browser.driver is None:
        raise RuntimeError("ブラウザドライバーが存在しません")

    end_time = time.monotonic() + timeout_seconds
    last_count = None
    stable_count_hits = 0

    while time.monotonic() < end_time:
        try:
            browser.wait_for_page_ready(timeout=3)
        except Exception:
            pass

        count = _count_visible_results(browser.driver)
        if last_count == count:
            stable_count_hits += 1
        else:
            last_count = count
            stable_count_hits = 1

        if stable_count_hits >= 2:
            return count

        time.sleep(1)

    raise TimeoutError("検索結果の読み込みがタイムアウトしました")


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


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1 or not args[0].strip():
        print("Usage: python diagnose_hennge_user_search.py <TEST_ALIAS>")
        return 1

    query = args[0].strip()
    masked_query = _mask_query(query)

    base_dir = _base_dir()
    config = load_config()
    ensure_directories(config)
    logger = AppLogger(base_dir)
    browser = Browser(base_dir, config)

    logger.info("診断モード: HENNGEユーザー検索のみを読み取り専用で実行します")
    logger.info("ユーザー詳細、証明書画面、ダウンロード、更新系操作、SMSM、Excel、IMEI更新は実行しません")

    try:
        browser.start()
        handler = HenngeHandler(config, logger, browser)
        handler.login()

        if browser.driver is None:
            raise RuntimeError("ログイン後にブラウザー状態を取得できません")

        current_url = _sanitize_url(browser.driver.current_url)
        current_title = browser.driver.title or ""
        logger.info(f"ログイン後URL: {current_url}")
        logger.info(f"ログイン後タイトル: {current_title}")

        if current_url != USERS_URL:
            browser.open(USERS_URL)
            browser.wait_for_page_ready(timeout=20)
            logger.info(f"ユーザー一覧ページへ移動: {_sanitize_url(browser.driver.current_url)}")

        search_input = _find_single_visible_search_input(browser.driver)
        logger.info(f"検索語マスク: {masked_query}")
        _set_query_and_submit(search_input, query)

        result_count = _wait_results_ready(browser, timeout_seconds=RESULT_TIMEOUT_SECONDS)
        logger.info(f"検索後URL: {_sanitize_url(browser.driver.current_url)}")
        _log_result_state(logger, result_count)

        _save_diag_no_html(logger, browser, "hennge_user_search_success")

        if result_count == 0:
            return 2
        if result_count == 1:
            return 0
        return 3
    except KeyboardInterrupt:
        logger.error("診断を中断しました: KeyboardInterrupt")
        try:
            _save_diag_no_html(logger, browser, "hennge_user_search_interrupted")
        except Exception:
            logger.exception("中断時の診断情報保存に失敗しました")
        return 130
    except Exception:
        logger.exception("HENNGEユーザー検索診断に失敗しました")
        try:
            _save_diag_no_html(logger, browser, "hennge_user_search_failure")
        except Exception:
            logger.exception("失敗時の診断情報保存に失敗しました")
        return 1
    finally:
        browser.quit()


if __name__ == "__main__":
    raise SystemExit(main())
