from pathlib import Path

from app.logger import AppLogger


class DummyDriver:
    def __init__(self):
        self.current_url = "https://ap.ssso.hdems.com/portal/yco.co.jp/login/?s=SECRET#frag"
        self.title = "HENNGE Access Control"
        self.page_source = "<html></html>"

    def save_screenshot(self, path: str) -> None:
        Path(path).write_bytes(b"png")


def test_save_browser_diagnostics_strips_query_and_fragment(tmp_path: Path) -> None:
    logger = AppLogger(tmp_path)
    driver = DummyDriver()

    logger.save_browser_diagnostics(driver, "diag")
    log_text = logger.log_path.read_text(encoding="utf-8")

    assert "https://ap.ssso.hdems.com/portal/yco.co.jp/login/" in log_text
    assert "?s=SECRET" not in log_text
    assert "SECRET" not in log_text
    assert "#frag" not in log_text


def test_sanitize_url_for_log_handles_empty_and_plain_url() -> None:
    assert AppLogger._sanitize_url_for_log("") == ""
    assert AppLogger._sanitize_url_for_log(None) == ""
    assert (
        AppLogger._sanitize_url_for_log(
            "https://example.com/path/to/page?token=abc#section"
        )
        == "https://example.com/path/to/page"
    )
