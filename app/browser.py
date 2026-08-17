import os
import json
import socket
import time
from pathlib import Path
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.remote.command import Command
from selenium.webdriver.edge.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from urllib3.exceptions import HTTPError, ReadTimeoutError


DIAGNOSTIC_QUIT_TIMEOUT_SECONDS = 10


class Browser:
    def __init__(self, base_dir: Path, config: dict | None = None):
        self.base_dir = base_dir
        self.config = config or {}
        self.driver = None
        self.started = False

    def start(self) -> None:
        if self.started:
            return

        # Ensure local WebDriver traffic is not routed through corporate proxy.
        no_proxy_values = "localhost,127.0.0.1,::1"
        os.environ["NO_PROXY"] = no_proxy_values
        os.environ["no_proxy"] = no_proxy_values

        options = Options()
        options.use_chromium = True
        options.add_argument("--start-maximized")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1600,1200")
        downloads_setting = self.config.get("paths", {}).get("downloads", "downloads")
        download_dir = (self.base_dir / str(downloads_setting)).resolve()
        download_dir.mkdir(parents=True, exist_ok=True)
        options.add_experimental_option(
            "prefs",
            {
                "download.default_directory": str(download_dir),
                "download.prompt_for_download": False,
                "download.directory_upgrade": True,
            },
        )

        self.driver = webdriver.Edge(options=options)
        if not self.driver.window_handles:
            raise RuntimeError("Edgeのウィンドウハンドルを取得できませんでした")
        self.started = True

    def _build_auto_select_cert_rule(self) -> str | None:
        cert_cfg = self.config.get("hennge", {}).get("certificate", {})
        enabled = bool(cert_cfg.get("auto_select", False))
        if not enabled:
            return None

        pattern = cert_cfg.get("pattern", "https://ap.ssso.hdems.com/*")
        rule: dict = {"pattern": pattern, "filter": {}}

        subject_filter = self._normalize_dn_filter(cert_cfg.get("subject", {}))
        issuer_filter = self._normalize_dn_filter(cert_cfg.get("issuer", {}))

        # Backward compatible fields
        subject_cn = cert_cfg.get("subject_cn", "")
        issuer_cn = cert_cfg.get("issuer_cn", "")
        if subject_cn and "CN" not in subject_filter:
            subject_filter["CN"] = subject_cn
        if issuer_cn and "CN" not in issuer_filter:
            issuer_filter["CN"] = issuer_cn

        if subject_filter:
            rule["filter"]["SUBJECT"] = subject_filter
        if issuer_filter:
            rule["filter"]["ISSUER"] = issuer_filter

        return json.dumps([rule], ensure_ascii=False)

    def _normalize_dn_filter(self, raw_filter) -> dict:
        if not isinstance(raw_filter, dict):
            return {}

        normalized = {}
        for key in ["CN", "OU", "O", "L", "ST", "C"]:
            value = raw_filter.get(key)
            if value is None:
                continue
            if isinstance(value, list):
                filtered = [str(v).strip() for v in value if str(v).strip()]
                if filtered:
                    normalized[key] = filtered
            else:
                text = str(value).strip()
                if text:
                    normalized[key] = text
        return normalized

    def quit(self) -> None:
        if self.driver is not None:
            self.driver.quit()
            self.driver = None
            self.started = False

    def quit_diagnostic(self, emit) -> dict[str, object]:
        started_at = time.monotonic()
        emit("browser_quit_started", True)
        emit("browser_quit_called", True)
        result = {
            "completed": False,
            "timed_out": False,
            "session_already_closed": self.driver is None or not self._session_is_active(),
            "exception_type": None,
            "elapsed_ms": 0,
            "timeout_configured": False,
            "timeout_effective": False,
            "http_timed_out": False,
            "cleanup_slow": False,
            "total_over_limit": False,
        }
        if result["session_already_closed"]:
            result["completed"] = True
            emit("browser_session_already_closed", True)
            emit("browser_quit_command_completed", False)
            emit("browser_quit_command_timed_out", False)
            emit("browser_quit_cleanup_started", True)
            self.driver = None
            self.started = False
            emit("browser_quit_cleanup_completed", True)
            emit("browser_quit_finished", True)
            emit("browser_quit_elapsed_ms", int((time.monotonic() - started_at) * 1000))
            emit("browser_quit_command_elapsed_ms", 0)
            emit("browser_quit_cleanup_elapsed_ms", 0)
            emit("browser_quit_completed", True)
            emit("browser_quit_timeout_configured", False)
            emit("browser_quit_timeout_effective", False)
            result["elapsed_ms"] = int((time.monotonic() - started_at) * 1000)
            emit("browser_quit_timed_out", False)
            return result

        emit("browser_session_already_closed", False)
        command_started_at = time.monotonic()
        emit("browser_quit_command_started", True)
        prepare_started_at = time.monotonic()
        try:
            configured, effective = self._configure_diagnostic_quit_timeout()
            result["timeout_configured"] = configured
            result["timeout_effective"] = effective
            emit("browser_quit_timeout_configured", configured)
            emit("browser_quit_timeout_effective", effective)
            emit("browser_quit_pool_clear_elapsed_ms", getattr(self, "_diagnostic_last_pool_clear_elapsed_ms", 0))
            emit("browser_quit_prepare_elapsed_ms", int((time.monotonic() - prepare_started_at) * 1000))
            self._diagnostic_transport_cleanup_elapsed_ms = 0
            self._diagnostic_quit_driver(emit, result)
            result["completed"] = not result["http_timed_out"]
            emit("browser_quit_command_completed", result["completed"])
            emit("browser_quit_command_timed_out", result["http_timed_out"])
        except (TimeoutError, socket.timeout, ReadTimeoutError) as exc:
            result["timed_out"] = True
            result["http_timed_out"] = True
            result["exception_type"] = type(exc).__name__
            emit("browser_quit_command_completed", False)
            emit("browser_quit_command_timed_out", True)
            emit("browser_quit_exception_type", result["exception_type"])
        except (ConnectionError, HTTPError, WebDriverException) as exc:
            result["exception_type"] = type(exc).__name__
            emit("browser_quit_command_completed", False)
            emit("browser_quit_command_timed_out", False)
            emit("browser_quit_exception_type", result["exception_type"])
        finally:
            emit("browser_quit_command_elapsed_ms", int((time.monotonic() - command_started_at) * 1000))
            cleanup_started_at = time.monotonic()
            emit("browser_quit_wrapper_cleanup_started", True)
            self.driver = None
            self.started = False
            emit("browser_quit_wrapper_cleanup_elapsed_ms", int((time.monotonic() - cleanup_started_at) * 1000))
            emit("browser_quit_wrapper_cleanup_completed", True)
            cleanup_elapsed_ms = max(
                int((time.monotonic() - cleanup_started_at) * 1000),
                getattr(self, "_diagnostic_transport_cleanup_elapsed_ms", 0),
            )
            result["cleanup_slow"] = cleanup_elapsed_ms > DIAGNOSTIC_QUIT_TIMEOUT_SECONDS * 1000
            result["total_over_limit"] = int((time.monotonic() - started_at) * 1000) > DIAGNOSTIC_QUIT_TIMEOUT_SECONDS * 1000
            emit("browser_quit_cleanup_slow", result["cleanup_slow"])
            emit("browser_quit_total_over_limit", result["total_over_limit"])
            emit("browser_quit_http_timed_out", result["http_timed_out"])
            emit("browser_quit_cleanup_started", True)
            emit("browser_quit_cleanup_completed", True)
            emit("browser_quit_finished", True)
            emit("browser_quit_elapsed_ms", int((time.monotonic() - started_at) * 1000))
            result["elapsed_ms"] = int((time.monotonic() - started_at) * 1000)
            emit("browser_quit_completed", result["completed"])
            emit("browser_quit_timed_out", result["timed_out"])
        return result

    def _diagnostic_quit_driver(self, emit, result) -> None:
        driver = self.driver
        if driver is None:
            return
        execute = getattr(driver, "execute", None)
        executor = getattr(driver, "command_executor", None)
        if not callable(execute) or executor is None:
            http_started_at = time.monotonic()
            try:
                driver.quit()
            finally:
                emit("browser_quit_http_elapsed_ms", int((time.monotonic() - http_started_at) * 1000))
                emit("browser_quit_request_dispose_elapsed_ms", 0)
                emit("browser_quit_connection_close_elapsed_ms", 0)
            return

        http_started_at = time.monotonic()
        http_error = None
        try:
            execute(Command.QUIT)
        except (TimeoutError, socket.timeout, ReadTimeoutError):
            result["timed_out"] = True
            result["http_timed_out"] = True
            http_error = TimeoutError("diagnostic quit HTTP timeout")
        finally:
            emit("browser_quit_http_elapsed_ms", int((time.monotonic() - http_started_at) * 1000))

        request = getattr(driver, "_request", None)
        dispose_started_at = time.monotonic()
        try:
            if request is not None and hasattr(request, "dispose"):
                request.dispose()
                driver._request = None
        except Exception as exc:
            emit("browser_quit_request_dispose_exception_type", type(exc).__name__)
        emit("browser_quit_request_dispose_elapsed_ms", int((time.monotonic() - dispose_started_at) * 1000))

        transport_cleanup_started_at = time.monotonic()
        connection_close_started_at = transport_cleanup_started_at
        try:
            stop_client = getattr(driver, "stop_client", None)
            if callable(stop_client):
                stop_client()
            close = getattr(executor, "close", None)
            if callable(close):
                close()
        except Exception as exc:
            emit("browser_quit_connection_close_exception_type", type(exc).__name__)
        finally:
            emit("browser_quit_connection_close_elapsed_ms", int((time.monotonic() - connection_close_started_at) * 1000))
            self._diagnostic_transport_cleanup_elapsed_ms = int(
                (time.monotonic() - transport_cleanup_started_at) * 1000
            )
        if http_error is not None:
            raise http_error

    def _session_is_active(self) -> bool:
        if self.driver is None:
            return False
        if hasattr(self.driver, "session_id"):
            return bool(self.driver.session_id)
        return True

    def _configure_diagnostic_quit_timeout(self) -> tuple[bool, bool]:
        executor = getattr(self.driver, "command_executor", None)
        if executor is None:
            return False, False
        client_config = getattr(executor, "_client_config", None)
        configured = False
        if client_config is not None and hasattr(client_config, "timeout"):
            client_config.timeout = DIAGNOSTIC_QUIT_TIMEOUT_SECONDS
            configured = client_config.timeout == DIAGNOSTIC_QUIT_TIMEOUT_SECONDS

        old_connection = getattr(executor, "_conn", None)
        try:
            if old_connection is not None and hasattr(old_connection, "clear"):
                clear_started_at = time.monotonic()
                old_connection.clear()
                self._diagnostic_last_pool_clear_elapsed_ms = int((time.monotonic() - clear_started_at) * 1000)
            if hasattr(executor, "_get_connection_manager"):
                executor._conn = executor._get_connection_manager()
        except Exception:
            return configured, False

        effective = self._connection_timeout_matches(
            getattr(executor, "_conn", None), DIAGNOSTIC_QUIT_TIMEOUT_SECONDS
        )
        return configured, effective

    @staticmethod
    def _connection_timeout_matches(connection, expected_seconds: int) -> bool:
        pool_kwargs = getattr(connection, "connection_pool_kw", None)
        if not isinstance(pool_kwargs, dict):
            return False
        timeout = pool_kwargs.get("timeout")
        if timeout == expected_seconds:
            return True
        for attribute in ("total", "connect", "read"):
            if getattr(timeout, attribute, None) != expected_seconds:
                return False
        return timeout is not None

    def open(self, url: str) -> None:
        if self.driver is None or not self.started:
            raise RuntimeError("ブラウザが開始されていません")
        self.driver.get(url)

    def current_handle(self) -> str:
        if self.driver is None or not self.started:
            raise RuntimeError("ブラウザが開始されていません")
        return self.driver.current_window_handle

    def open_new_tab(self, url: str) -> str:
        if self.driver is None or not self.started:
            raise RuntimeError("ブラウザが開始されていません")
        self.driver.switch_to.new_window("tab")
        self.driver.get(url)
        return self.driver.current_window_handle

    def switch_to(self, handle: str) -> None:
        if self.driver is None or not self.started:
            raise RuntimeError("ブラウザが開始されていません")
        if handle not in self.driver.window_handles:
            raise RuntimeError(f"指定されたタブが存在しません: {handle}")
        self.driver.switch_to.window(handle)

    def capture_state(self) -> dict:
        if self.driver is None:
            return {"started": False}

        state = {"started": self.started}
        for key, getter in {
            "url": lambda: self.driver.current_url,
            "title": lambda: self.driver.title,
            "handle": lambda: self.driver.current_window_handle,
        }.items():
            try:
                state[key] = getter()
            except Exception as exc:
                state[key] = f"取得失敗: {type(exc).__name__}"
        return state

    def wait_for_clickable(self, by, value, timeout: int = 20):
        if self.driver is None:
            raise RuntimeError("ブラウザが開始されていません")
        return WebDriverWait(self.driver, timeout).until(EC.element_to_be_clickable((by, value)))

    def wait_for_visible(self, by, value, timeout: int = 20):
        if self.driver is None:
            raise RuntimeError("ブラウザが開始されていません")
        return WebDriverWait(self.driver, timeout).until(EC.visibility_of_element_located((by, value)))

    def wait_for_page_ready(self, timeout: int = 15):
        if self.driver is None:
            raise RuntimeError("ブラウザが開始されていません")
        WebDriverWait(self.driver, timeout).until(
            lambda driver: driver.execute_script("return document.readyState") == "complete"
        )
        return True

    def find_first(self, locators, timeout: int = 10):
        if self.driver is None:
            raise RuntimeError("ブラウザが開始されていません")

        last_error = None
        for by, value in locators:
            try:
                return self.wait_for_visible(by, value, timeout=max(2, min(5, timeout)))
            except TimeoutException as exc:
                last_error = exc
        raise last_error or TimeoutException("要素が見つかりませんでした")

    def click_first(self, locators, timeout: int = 10) -> None:
        if self.driver is None:
            raise RuntimeError("ブラウザが開始されていません")

        last_error = None
        for by, value in locators:
            try:
                element = self.wait_for_clickable(by, value, timeout=max(2, min(5, timeout)))
                element.click()
                return
            except TimeoutException as exc:
                last_error = exc
        raise last_error or TimeoutException("クリック可能な要素が見つかりませんでした")

    def find_clickable_first(self, locators, timeout: int = 10):
        if self.driver is None:
            raise RuntimeError("ブラウザが開始されていません")

        def find_clickable(_driver):
            for by, value in locators:
                for element in self.driver.find_elements(by, value):
                    if element.is_displayed() and element.is_enabled():
                        return element
            return False

        return WebDriverWait(self.driver, timeout, poll_frequency=0.1).until(find_clickable)
