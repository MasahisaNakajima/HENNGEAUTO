from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from app.browser import Browser
from app.config import ensure_directories, load_config
from app.hennge_handler import HenngeHandler
from app.logger import AppLogger


def _base_dir() -> Path:
    return Path(__file__).resolve().parent


def _sanitize_url(raw_url: str) -> str:
    if not raw_url:
        return ""

    parsed = urlsplit(raw_url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def main() -> int:
    base_dir = _base_dir()
    config = load_config()
    ensure_directories(config)

    logger = AppLogger(base_dir)
    browser = Browser(base_dir, config)

    logger.info("診断モード: HENNGEログインのみ実行します")

    try:
        browser.start()
        hennge_handler = HenngeHandler(config, logger, browser)
        hennge_handler.login()

        current_url = ""
        title = ""
        if browser.driver is not None:
            current_url = _sanitize_url(browser.driver.current_url or "")
            title = browser.driver.title or ""

        logger.info(f"HENNGEログイン成功 URL={current_url}")
        logger.info(f"HENNGEログイン成功 TITLE={title}")
        logger.save_browser_diagnostics(browser.driver, "hennge_login_success")
        return 0
    except KeyboardInterrupt:
        logger.error("診断を中断しました: KeyboardInterrupt")
        try:
            logger.save_browser_diagnostics(browser.driver, "hennge_login_interrupted")
        except Exception:
            logger.exception("中断時の診断情報保存に失敗しました")
        return 130
    except Exception:
        logger.exception("HENNGEログイン診断に失敗しました")
        try:
            logger.save_browser_diagnostics(browser.driver, "hennge_login_failure")
        except Exception:
            logger.exception("失敗時の診断情報保存に失敗しました")
        return 1
    finally:
        browser.quit()


if __name__ == "__main__":
    raise SystemExit(main())
