from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from selenium.webdriver.common.by import By

from app.browser import Browser
from app.config import ensure_directories, load_config
from app.logger import AppLogger

TARGET_URL = "https://ap.ssso.hdems.com/portal/yco.co.jp/"
MAX_TEXT_LENGTH = 200


def _base_dir() -> Path:
    return Path(__file__).resolve().parent


def _safe_text(value: str | None) -> str:
    if not value:
        return ""

    text = " ".join(str(value).split())
    lowered = text.lower()

    blocked_keywords = [
        "password",
        "passwd",
        "cookie",
        "token",
        "authorization",
        "bearer",
        "session",
    ]
    if any(keyword in lowered for keyword in blocked_keywords):
        return "[redacted]"

    if len(text) > MAX_TEXT_LENGTH:
        return text[:MAX_TEXT_LENGTH] + "..."

    return text


def _safe_href_host_path(href: str | None) -> str:
    if not href:
        return ""

    parsed = urlparse(href)
    if not parsed.scheme or not parsed.netloc:
        return ""

    path = parsed.path or "/"
    return f"{parsed.netloc}{path}"


def _log_elements(logger: AppLogger, browser: Browser) -> None:
    if browser.driver is None:
        raise RuntimeError("Browser driver is not available")

    selectors = [
        "button",
        "input[type='submit']",
        "a",
    ]

    elements = []
    for selector in selectors:
        elements.extend(browser.driver.find_elements(By.CSS_SELECTOR, selector))

    logger.info(f"Visible candidate elements count={len(elements)}")

    for index, element in enumerate(elements, start=1):
        try:
            if not element.is_displayed():
                continue

            tag_name = _safe_text(element.tag_name)
            text = _safe_text(element.text)
            type_attr = _safe_text(element.get_attribute("type"))
            id_attr = _safe_text(element.get_attribute("id"))
            name_attr = _safe_text(element.get_attribute("name"))
            class_attr = _safe_text(element.get_attribute("class"))
            value_attr = _safe_text(element.get_attribute("value"))
            href_host_path = _safe_href_host_path(element.get_attribute("href"))

            logger.info(
                "Element "
                f"#{index} "
                f"tag_name={tag_name}, "
                f"text={text}, "
                f"type={type_attr}, "
                f"id={id_attr}, "
                f"name={name_attr}, "
                f"class={class_attr}, "
                f"value={value_attr}, "
                f"href_host_path={href_host_path}"
            )
        except Exception:
            logger.exception(f"Failed to inspect element #{index}")


def main() -> None:
    config = load_config()
    ensure_directories(config)

    logger = AppLogger(_base_dir())
    browser = Browser(_base_dir(), config)

    logger.info("HENNGE button diagnostics started")
    logger.info("No click, no credential input, no Excel/SMSM/download/upload/IMEI actions will be performed")

    try:
        browser.start()
        browser.open(TARGET_URL)
        browser.wait_for_page_ready(timeout=30)

        _log_elements(logger, browser)
        logger.save_browser_diagnostics(browser.driver, "hennge_buttons_end")
        logger.info("HENNGE button diagnostics finished")
    except Exception:
        logger.exception("HENNGE button diagnostics failed")
        try:
            logger.save_browser_diagnostics(browser.driver, "hennge_buttons_error")
        except Exception:
            logger.exception("Failed to save diagnostics after error")
        raise
    finally:
        browser.quit()


if __name__ == "__main__":
    main()
