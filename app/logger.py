import logging
from pathlib import Path
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit


class AppLogger:
    def __init__(self, base_dir: Path, *, unique_log: bool = False):
        self.base_dir = base_dir
        self.logs_dir = base_dir / "logs"
        self.screenshots_dir = base_dir / "screenshots"
        self.logs_dir.mkdir(exist_ok=True)
        self.screenshots_dir.mkdir(exist_ok=True)

        timestamp_format = "%Y%m%d_%H%M%S_%f" if unique_log else "%Y%m%d_%H%M%S"
        timestamp = datetime.now().strftime(timestamp_format)
        self.log_path = self.logs_dir / f"run_{timestamp}.log"

        self.logger = logging.getLogger("hennge_auto")
        self.logger.setLevel(logging.INFO)
        self.logger.handlers.clear()

        handler = logging.FileHandler(self.log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        self.logger.addHandler(handler)

    def info(self, message: str) -> None:
        self.logger.info(message)

    def error(self, message: str) -> None:
        self.logger.error(message)

    def exception(self, message: str) -> None:
        self.logger.exception(message)

    def save_screenshot(self, driver, name: str) -> None:
        if driver is None:
            self.error(f"スクリーンショットを保存できません: driver=None, name={name}")
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = self.screenshots_dir / f"{name}_{timestamp}.png"
        try:
            driver.save_screenshot(str(path))
            self.info(f"スクリーンショットを保存: {path}")
        except Exception:
            self.exception(f"スクリーンショット保存に失敗: {path}")

    def save_browser_diagnostics(self, driver, name: str, save_html: bool = True) -> None:
        if driver is None:
            self.error(f"ブラウザー診断情報を保存できません: driver=None, name={name}")
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        try:
            self.info(f"診断時URL: {self._sanitize_url_for_log(driver.current_url)}")
        except Exception:
            self.exception("診断時URLの取得に失敗")

        try:
            self.info(f"診断時タイトル: {driver.title}")
        except Exception:
            self.exception("診断時タイトルの取得に失敗")

        self.save_screenshot(driver, name)

        if not save_html:
            self.info("HTML保存は無効化されています")
            return

        html_path = self.screenshots_dir / f"{name}_{timestamp}.html"
        try:
            html_path.write_text(driver.page_source or "", encoding="utf-8")
            self.info(f"HTMLを保存: {html_path}")
        except Exception:
            self.exception(f"HTML保存に失敗: {html_path}")

    @staticmethod
    def _sanitize_url_for_log(raw_url: str | None) -> str:
        if not raw_url:
            return ""

        parsed = urlsplit(raw_url)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
