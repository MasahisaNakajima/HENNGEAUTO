from __future__ import annotations

import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from selenium.webdriver.common.by import By

from app.browser import Browser
from app.config import ensure_directories, load_config
from app.hennge_handler import HenngeHandler
from app.logger import AppLogger

CERTIFICATES_URL = "https://admin.auth.hennge.com/certificates/"
WAIT_TIMEOUT_SECONDS = 20


def _base_dir() -> Path:
    return Path(__file__).resolve().parent


def _sanitize_url(raw_url: str | None) -> str:
    if not raw_url:
        return ""
    parsed = urlsplit(raw_url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _wait_certificates_page_ready(browser: Browser, timeout_seconds: int = WAIT_TIMEOUT_SECONDS) -> None:
    if browser.driver is None:
        raise RuntimeError("ブラウザドライバーが存在しません")

    end_time = time.monotonic() + timeout_seconds
    while time.monotonic() < end_time:
        try:
            browser.wait_for_page_ready(timeout=3)
        except Exception:
            pass

        current_path = urlsplit(_sanitize_url(browser.driver.current_url)).path
        current_title = (browser.driver.title or "").strip()
        if current_path == "/certificates/" and current_title == "証明書一覧":
            return
        time.sleep(0.5)

    raise RuntimeError("証明書一覧画面の準備待機がタイムアウトしました")


def _wait_visible_inputs(driver, timeout_seconds: int = WAIT_TIMEOUT_SECONDS):
    end_time = time.monotonic() + timeout_seconds
    while time.monotonic() < end_time:
        visible_inputs = []
        for element in driver.find_elements(By.CSS_SELECTOR, "input"):
            try:
                if element.is_displayed():
                    visible_inputs.append(element)
            except Exception:
                continue
        if len(visible_inputs) > 0:
            return visible_inputs
        time.sleep(0.5)

    raise RuntimeError("表示中input要素の待機がタイムアウトしました")


def _collect_parent_attrs(input_element) -> dict[str, str]:
    parent_candidates = input_element.find_elements(By.XPATH, "ancestor::*[1]")
    if not parent_candidates:
        return {
            "parent_tag": "",
            "parent_role": "",
            "parent_aria_label": "",
            "parent_data_testid": "",
        }

    parent = parent_candidates[0]
    return {
        "parent_tag": (getattr(parent, "tag_name", "") or "").strip().lower(),
        "parent_role": (parent.get_attribute("role") or "").strip(),
        "parent_aria_label": (parent.get_attribute("aria-label") or "").strip(),
        "parent_data_testid": (parent.get_attribute("data-testid") or "").strip(),
    }


def _inspect_visible_inputs(visible_inputs) -> tuple[int, list[dict[str, str | bool]]]:
    hidden_count = 0
    details = []

    for element in visible_inputs:
        type_attr = (element.get_attribute("type") or "").strip().lower()
        if type_attr == "hidden":
            hidden_count += 1
            continue

        parent_attrs = _collect_parent_attrs(element)
        details.append(
            {
                "type": type_attr,
                "id": (element.get_attribute("id") or "").strip(),
                "name": (element.get_attribute("name") or "").strip(),
                "class": (element.get_attribute("class") or "").strip(),
                "placeholder": (element.get_attribute("placeholder") or "").strip(),
                "aria_label": (element.get_attribute("aria-label") or "").strip(),
                "role": (element.get_attribute("role") or "").strip(),
                "autocomplete": (element.get_attribute("autocomplete") or "").strip(),
                "enabled": bool(element.is_enabled()),
                "has_disabled": element.get_attribute("disabled") is not None,
                "has_readonly": element.get_attribute("readonly") is not None,
                **parent_attrs,
            }
        )

    return hidden_count, details


def _save_diag_no_html(logger: AppLogger, browser: Browser, name: str) -> None:
    logger.save_browser_diagnostics(browser.driver, name, save_html=False)


def main(argv: list[str] | None = None) -> int:
    _ = argv  # This script takes no CLI parameters.

    base_dir = _base_dir()
    config = load_config()
    ensure_directories(config)

    logger = AppLogger(base_dir)
    browser = Browser(base_dir, config)

    logger.info("診断モード: 証明書一覧画面のinput属性を読み取り専用で調査します")
    logger.info("入力、Enter送信、クリック、ダウンロード、更新、削除、SMSM、Excel、IMEI、ファイル操作は実行しません")

    try:
        browser.start()
        handler = HenngeHandler(config, logger, browser)
        handler.login()

        browser.open(CERTIFICATES_URL)
        _wait_certificates_page_ready(browser, timeout_seconds=WAIT_TIMEOUT_SECONDS)

        if browser.driver is None:
            raise RuntimeError("ブラウザドライバーが存在しません")

        safe_url = _sanitize_url(browser.driver.current_url)
        logger.info(f"証明書一覧画面URL: {safe_url}")
        logger.info(f"証明書一覧画面タイトル: {(browser.driver.title or '').strip()}")

        visible_inputs = _wait_visible_inputs(browser.driver, timeout_seconds=WAIT_TIMEOUT_SECONDS)
        hidden_count, details = _inspect_visible_inputs(visible_inputs)

        logger.info(f"表示中input要素数: {len(visible_inputs)}")
        logger.info(f"hidden input件数: {hidden_count}")
        logger.info(f"記録対象input件数(hidden除外): {len(details)}")

        for index, item in enumerate(details, start=1):
            logger.info(
                "input属性 "
                f"#{index} "
                f"type={item['type']}, "
                f"id={item['id']}, "
                f"name={item['name']}, "
                f"class={item['class']}, "
                f"placeholder={item['placeholder']}, "
                f"aria-label={item['aria_label']}, "
                f"role={item['role']}, "
                f"autocomplete={item['autocomplete']}, "
                f"enabled={item['enabled']}, "
                f"has_disabled={item['has_disabled']}, "
                f"has_readonly={item['has_readonly']}, "
                f"parent_tag={item['parent_tag']}, "
                f"parent_role={item['parent_role']}, "
                f"parent_aria_label={item['parent_aria_label']}, "
                f"parent_data_testid={item['parent_data_testid']}"
            )

        _save_diag_no_html(logger, browser, "hennge_certificate_inputs_success")
        return 0
    except KeyboardInterrupt:
        logger.error("診断を中断しました: KeyboardInterrupt")
        try:
            _save_diag_no_html(logger, browser, "hennge_certificate_inputs_interrupted")
        except Exception:
            logger.exception("中断時の診断情報保存に失敗しました")
        return 130
    except Exception:
        logger.exception("証明書一覧input診断に失敗しました")
        try:
            _save_diag_no_html(logger, browser, "hennge_certificate_inputs_failure")
        except Exception:
            logger.exception("失敗時の診断情報保存に失敗しました")
        return 1
    finally:
        browser.quit()


if __name__ == "__main__":
    raise SystemExit(main())
