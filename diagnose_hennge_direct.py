from __future__ import annotations

import time

from app.browser import Browser
from app.config import ensure_directories, load_config
from app.logger import AppLogger

TARGET_URL = "https://ap.ssso.hdems.com/portal/yco.co.jp/"
OBSERVE_SECONDS = 120
POLL_INTERVAL_SECONDS = 2


def _safe_state(browser: Browser) -> tuple[str, str]:
    if browser.driver is None:
        return "", ""

    current_url = ""
    title = ""
    try:
        current_url = browser.driver.current_url or ""
    except Exception:
        current_url = "<unavailable>"

    try:
        title = browser.driver.title or ""
    except Exception:
        title = "<unavailable>"

    return current_url, title


def main() -> None:
    config = load_config()
    ensure_directories(config)

    logger = AppLogger(browser_base_dir())
    browser = Browser(logger.base_dir, config)

    logger.info("Direct HENNGE diagnostics started")
    logger.info("No login, Excel read, SMSM access, download, upload, or IMEI update will be performed")

    try:
        browser.start()
        browser.open(TARGET_URL)
        browser.wait_for_page_ready(timeout=30)

        logger.save_browser_diagnostics(browser.driver, "hennge_direct_start")

        last_url, last_title = _safe_state(browser)
        logger.info(f"Initial state url={last_url}, title={last_title}")

        deadline = time.monotonic() + OBSERVE_SECONDS
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_SECONDS)
            current_url, current_title = _safe_state(browser)
            if current_url != last_url or current_title != last_title:
                logger.info(
                    "State changed "
                    f"url={last_url} -> {current_url}, "
                    f"title={last_title} -> {current_title}"
                )
                last_url, last_title = current_url, current_title

        logger.save_browser_diagnostics(browser.driver, "hennge_direct_end")
        logger.info("Direct HENNGE diagnostics finished")
    except Exception:
        logger.exception("Direct HENNGE diagnostics failed")
        try:
            logger.save_browser_diagnostics(browser.driver, "hennge_direct_error")
        except Exception:
            logger.exception("Failed to save diagnostics after error")
        raise
    finally:
        browser.quit()


def browser_base_dir():
    # Keep this script self-contained at the project root.
    from pathlib import Path

    return Path(__file__).resolve().parent


if __name__ == "__main__":
    main()
