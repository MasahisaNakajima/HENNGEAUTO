from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from selenium.webdriver.common.by import By

from app.browser import Browser
from app.config import ensure_directories, load_config
from app.hennge_handler import HenngeHandler
from app.logger import AppLogger

ADMIN_KEYWORDS = ["ユーザー", "証明書", "検索", "管理", "administration"]


def _base_dir() -> Path:
    return Path(__file__).resolve().parent


def _sanitize_url(raw_url: str | None) -> str:
    if not raw_url:
        return ""
    parsed = urlsplit(raw_url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _safe_text(raw_text: str | None) -> str:
    if not raw_text:
        return ""
    return " ".join(str(raw_text).split())


def _href_host_path(href: str | None) -> str:
    if not href:
        return ""

    parsed = urlsplit(href)
    if not parsed.netloc:
        return ""
    return f"{parsed.netloc}{parsed.path or '/'}"


def _element_summary(element) -> dict[str, str]:
    return {
        "tag": _safe_text(element.tag_name),
        "text": _safe_text(element.text),
        "id": _safe_text(element.get_attribute("id")),
        "name": _safe_text(element.get_attribute("name")),
        "type": _safe_text(element.get_attribute("type")),
        "class": _safe_text(element.get_attribute("class")),
        "href_host_path": _href_host_path(element.get_attribute("href")),
    }


def _is_admin_related(summary: dict[str, str]) -> bool:
    source = " ".join(
        [
            summary.get("text", ""),
            summary.get("id", ""),
            summary.get("name", ""),
            summary.get("class", ""),
            summary.get("href_host_path", ""),
        ]
    ).lower()
    return any(keyword.lower() in source for keyword in ADMIN_KEYWORDS)


def _log_elements(logger: AppLogger, browser: Browser) -> None:
    if browser.driver is None:
        raise RuntimeError("ブラウザドライバーが初期化されていません")

    selector = "a, button, input"
    elements = browser.driver.find_elements(By.CSS_SELECTOR, selector)

    visible_elements = []
    for element in elements:
        try:
            if element.is_displayed():
                visible_elements.append(element)
        except Exception:
            continue

    logger.info(f"表示中要素数(a/button/input): {len(visible_elements)}")

    for index, element in enumerate(visible_elements, start=1):
        summary = _element_summary(element)
        logger.info(
            "要素 "
            f"#{index} "
            f"tag={summary['tag']}, "
            f"text={summary['text']}, "
            f"id={summary['id']}, "
            f"name={summary['name']}, "
            f"type={summary['type']}, "
            f"class={summary['class']}, "
            f"href_host_path={summary['href_host_path']}"
        )

    admin_related = []
    for element in visible_elements:
        summary = _element_summary(element)
        if _is_admin_related(summary):
            admin_related.append(summary)

    logger.info(f"管理関連候補要素数: {len(admin_related)}")
    for index, summary in enumerate(admin_related, start=1):
        logger.info(
            "管理関連候補 "
            f"#{index} "
            f"tag={summary['tag']}, "
            f"text={summary['text']}, "
            f"id={summary['id']}, "
            f"name={summary['name']}, "
            f"type={summary['type']}, "
            f"class={summary['class']}, "
            f"href_host_path={summary['href_host_path']}"
        )


def main() -> int:
    base_dir = _base_dir()
    config = load_config()
    ensure_directories(config)

    logger = AppLogger(base_dir)
    browser = Browser(base_dir, config)

    logger.info("診断モード: HENNGE管理画面の読み取り専用診断を実行します")
    logger.info("ユーザー検索、証明書ダウンロード、SMSMアクセス、Excel読込、IMEI更新、クリック操作は実行しません")

    try:
        browser.start()
        handler = HenngeHandler(config, logger, browser)
        handler.login()

        if browser.driver is None:
            raise RuntimeError("ログイン後にブラウザー状態を取得できません")

        current_url = _sanitize_url(browser.driver.current_url)
        current_title = _safe_text(browser.driver.title)
        logger.info(f"ログイン後URL: {current_url}")
        logger.info(f"ログイン後タイトル: {current_title}")

        _log_elements(logger, browser)
        logger.save_browser_diagnostics(browser.driver, "hennge_admin_success")
        return 0
    except Exception:
        logger.exception("HENNGE管理画面診断に失敗しました")
        try:
            logger.save_browser_diagnostics(browser.driver, "hennge_admin_failure")
        except Exception:
            logger.exception("失敗時の診断情報保存にも失敗しました")
        return 1
    finally:
        browser.quit()


if __name__ == "__main__":
    raise SystemExit(main())
