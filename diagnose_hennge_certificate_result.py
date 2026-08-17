from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By

import diagnose_hennge_certificate_search as cert_search
from app.browser import Browser
from app.config import ensure_directories, load_config
from app.hennge_handler import HenngeHandler
from app.logger import AppLogger

CERTIFICATES_URL = "https://admin.auth.hennge.com/certificates/"
SEARCH_INPUT_TIMEOUT_SECONDS = 20
RESULT_TIMEOUT_SECONDS = 15

RESULT_ROW_SELECTORS = [
    "table tbody tr",
    "[data-testid='certificate-row']",
    "tr[data-testid*='certificate']",
    "li[data-testid*='certificate']",
]

DOWNLOAD_KEYWORDS = [
    "download",
    "ダウンロード",
    "export",
    "dl",
]


class ResultRowError(RuntimeError):
    pass


def _base_dir() -> Path:
    return Path(__file__).resolve().parent


def _sanitize_url(raw_url: str | None) -> str:
    if not raw_url:
        return ""
    parsed = urlsplit(raw_url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _sanitize_href_host_path(raw_href: str | None) -> str:
    if not raw_href:
        return ""
    parsed = urlsplit(raw_href)
    path = parsed.path or ""
    return f"{parsed.netloc}{path}"


def _safe_attr(element, attr_name: str) -> str:
    return (element.get_attribute(attr_name) or "").strip()


def _is_displayed(element) -> bool:
    try:
        return bool(element.is_displayed())
    except Exception:
        return False


def _collect_visible_rows(driver):
    for selector in RESULT_ROW_SELECTORS:
        rows = []
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            if _is_displayed(element):
                rows.append(element)
        if rows:
            return selector, rows
    return "", []


def _find_single_result_row(driver):
    selector, rows = _collect_visible_rows(driver)
    if len(rows) != 1:
        raise ResultRowError(f"検索結果行候補数が不正です: {len(rows)}")
    return selector, rows[0]


def _collect_row_structure(row):
    row_info = {
        "tag": (getattr(row, "tag_name", "") or "").strip().lower(),
        "role": _safe_attr(row, "role"),
        "class": _safe_attr(row, "class"),
        "data_testid": _safe_attr(row, "data-testid"),
        "aria_label": _safe_attr(row, "aria-label"),
    }

    action_elements = []
    for selector in ("a", "button", "input"):
        for element in row.find_elements(By.CSS_SELECTOR, selector):
            if not _is_displayed(element):
                continue

            tag = (getattr(element, "tag_name", "") or "").strip().lower()
            href_host_path = ""
            if tag == "a":
                href_host_path = _sanitize_href_host_path(_safe_attr(element, "href"))

            action_elements.append(
                {
                    "tag": tag,
                    "type": _safe_attr(element, "type").lower(),
                    "id": _safe_attr(element, "id"),
                    "name": _safe_attr(element, "name"),
                    "class": _safe_attr(element, "class"),
                    "role": _safe_attr(element, "role"),
                    "aria_label": _safe_attr(element, "aria-label"),
                    "title": _safe_attr(element, "title"),
                    "data_testid": _safe_attr(element, "data-testid"),
                    "href_host_path": href_host_path,
                }
            )

    row_info["a_count"] = len([e for e in action_elements if e["tag"] == "a"])
    row_info["button_count"] = len([e for e in action_elements if e["tag"] == "button"])
    row_info["input_count"] = len([e for e in action_elements if e["tag"] == "input"])

    return row_info, action_elements


def _is_download_candidate(action_element: dict[str, str]) -> bool:
    searchable = " ".join(
        [
            action_element.get("type", ""),
            action_element.get("id", ""),
            action_element.get("name", ""),
            action_element.get("class", ""),
            action_element.get("role", ""),
            action_element.get("aria_label", ""),
            action_element.get("title", ""),
            action_element.get("data_testid", ""),
            action_element.get("href_host_path", ""),
        ]
    ).lower()

    return any(keyword in searchable for keyword in DOWNLOAD_KEYWORDS)


def _has_undownloaded_flag(row_info: dict[str, object], action_elements: list[dict[str, str]]) -> bool:
    values = [
        str(row_info.get("class", "")),
        str(row_info.get("data_testid", "")),
        str(row_info.get("aria_label", "")),
    ]
    for item in action_elements:
        values.append(item.get("class", ""))
        values.append(item.get("data_testid", ""))
        values.append(item.get("aria_label", ""))
        values.append(item.get("title", ""))

    return any("未ダウンロード" in value for value in values if value)


def _log_row_structure(logger: AppLogger, selector: str, row_info: dict[str, object], action_elements: list[dict[str, str]]) -> None:
    logger.info(
        "検索結果行構造 "
        f"selector={selector}, "
        f"tag={row_info['tag']}, "
        f"role={row_info['role']}, "
        f"class={row_info['class']}, "
        f"data-testid={row_info['data_testid']}, "
        f"aria-label={row_info['aria_label']}, "
        f"a_count={row_info['a_count']}, "
        f"button_count={row_info['button_count']}, "
        f"input_count={row_info['input_count']}"
    )

    for index, item in enumerate(action_elements, start=1):
        logger.info(
            "行内操作要素 "
            f"#{index} "
            f"tag={item['tag']}, "
            f"type={item['type']}, "
            f"id={item['id']}, "
            f"name={item['name']}, "
            f"class={item['class']}, "
            f"role={item['role']}, "
            f"aria-label={item['aria_label']}, "
            f"title={item['title']}, "
            f"data-testid={item['data_testid']}, "
            f"href={item['href_host_path']}"
        )

    logger.info(f"未ダウンロード状態フラグ: {_has_undownloaded_flag(row_info, action_elements)}")


def _save_diag_no_html(logger: AppLogger, browser: Browser, name: str) -> None:
    logger.save_browser_diagnostics(browser.driver, name, save_html=False)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1 or not args[0].strip():
        print("Usage: python diagnose_hennge_certificate_result.py <TEST_ALIAS>")
        return 1

    alias = args[0].strip()

    base_dir = _base_dir()
    config = load_config()
    ensure_directories(config)
    logger = AppLogger(base_dir)
    browser = Browser(base_dir, config)

    logger.info("診断モード: HENNGE証明書検索結果1行とダウンロード入口のみを読み取り専用で調査します")
    logger.info("ダウンロード、登録、失効、更新、削除、一覧作成、行クリックは実行しません")
    logger.info("SMSM、Excel、IMEI、ファイル操作は実行しません")

    try:
        browser.start()
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
            _save_diag_no_html(logger, browser, "hennge_certificate_result_nohit")
            return 2
        if result_count > 1:
            _save_diag_no_html(logger, browser, "hennge_certificate_result_multiple")
            return 3

        selector, row = _find_single_result_row(browser.driver)
        row_info, action_elements = _collect_row_structure(row)
        _log_row_structure(logger, selector, row_info, action_elements)

        download_candidates = [item for item in action_elements if _is_download_candidate(item)]
        logger.info(f"ダウンロード候補要素数: {len(download_candidates)}")

        _save_diag_no_html(logger, browser, "hennge_certificate_result_success")

        if len(download_candidates) == 0:
            return 4
        if len(download_candidates) > 1:
            return 5
        return 0
    except KeyboardInterrupt:
        logger.error("診断を中断しました: KeyboardInterrupt")
        try:
            _save_diag_no_html(logger, browser, "hennge_certificate_result_interrupted")
        except Exception:
            logger.exception("中断時の診断情報保存に失敗しました")
        return 130
    except Exception:
        logger.exception("HENNGE証明書結果診断に失敗しました")
        try:
            _save_diag_no_html(logger, browser, "hennge_certificate_result_failure")
        except Exception:
            logger.exception("失敗時の診断情報保存に失敗しました")
        return 1
    finally:
        browser.quit()


if __name__ == "__main__":
    raise SystemExit(main())
