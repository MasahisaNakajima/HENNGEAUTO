from pathlib import Path
import inspect
import hashlib
import time
import json
from dataclasses import dataclass, field
from typing import TypedDict
import hashlib
import re
import unicodedata
from urllib.parse import urlparse, urlunparse, unquote
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import JavascriptException, StaleElementReferenceException, TimeoutException, WebDriverException
from app.smsm_config import SMSM_BASE_URL, SmsmConfig, password_contains_unsafe_syntax
from app.imei_normalizer import normalize_imei


class SerialInputDiagnosticResult(TypedDict):
    serial_input_candidate_count: int
    serial_input_unique: bool
    serial_input_clear_called: bool
    serial_input_send_keys_called: bool
    serial_input_nonblank: bool
    serial_input_exact_match: bool
    serial_input_length_match: bool
    serial_input_was_truncated: bool
    serial_input_was_transformed: bool
    serial_mapping_valid: bool
    search_button_click_called: bool
    smsm_update_called: bool
    excel_write_called: bool


@dataclass(frozen=True, repr=False)
class CertificateUploadRequest:
    certificate_file_path: Path = field(repr=False)
    certificate_password: str = field(repr=False)
    target_alias: str = ""
    target_serial: str = ""
    target_imei: str = ""

    def __post_init__(self) -> None:
        path = Path(self.certificate_file_path)
        if not path.is_file():
            raise FileNotFoundError("証明書ファイルを確認できません")
        if path.suffix.casefold() not in {".p12", ".pfx"}:
            raise ValueError("証明書ファイル形式を確認できません")
        if not isinstance(self.certificate_password, str) or not self.certificate_password:
            raise ValueError("証明書パスワードを確認できません")

    def __repr__(self) -> str:
        return "CertificateUploadRequest(<sensitive fields omitted>)"


class SmsmHandler:
    BASE_URL = SMSM_BASE_URL

    def __init__(self, *, browser, logger, smsm_config: SmsmConfig, trace_callback=None):
        self.smsm_config = smsm_config
        self.logger = logger
        self.browser = browser
        self.trace_callback = trace_callback
        self._login_origin_url = ""
        self._last_navigation_observation = {}
        self._last_navigation_evidence = []
        self.device_observation = {}

    def login(self, trace=None) -> None:
        self.logger.info("SMSMログイン処理を開始")
        for key in ("company_field_found", "login_page_opened", "user_field_found", "password_field_found", "company_and_user_fields_distinct", "company_and_password_fields_distinct", "user_and_password_fields_distinct", "company_field_filled", "user_field_filled", "password_field_filled", "login_button_found", "login_submitted", "additional_auth_detected", "confirmation_page_detected", "redirected_window_detected", "login_error_banner_detected", "login_completed"):
            self._trace(trace, key, False)
        try:
            self._trace(trace, "smsm_config_validation", True)
            company_code, username, password = self._validate_login_config()
            if password_contains_unsafe_syntax(password):
                self._trace(trace, "login_submit_blocked", True)
                self._trace(trace, "credential_mapping_valid", False)
                raise RuntimeError("SMSMパスワード設定を確認してください")
            self._trace(trace, "smsm_open_login_page", True)
            stage_started_at = time.monotonic()
            self.browser.open(self._login_url())
            self._trace_elapsed(trace, "smsm_open_login_page", stage_started_at)
            self._login_origin_url = self._current_url()
            self._trace(trace, "login_page_opened", True)
            self._trace(trace, "smsm_wait_login_page", True)
            stage_started_at = time.monotonic()
            self.browser.wait_for_page_ready()
            self._trace_elapsed(trace, "smsm_wait_login_page", stage_started_at)
            stage_started_at = time.monotonic()
            company_element = self._find_company_field()
            self._trace_elapsed(trace, "smsm_find_company_field", stage_started_at)
            self._trace(trace, "company_field_found", True)
            self._trace_field_diagnostics(trace, "company", company_element)
            stage_started_at = time.monotonic()
            user_element = self._find_user_field()
            self._trace_elapsed(trace, "smsm_find_user_field", stage_started_at)
            self._trace(trace, "user_field_found", True)
            self._trace_field_diagnostics(trace, "user", user_element)
            self._trace(trace, "company_and_user_fields_distinct", company_element is not user_element)
            if company_element is user_element:
                raise RuntimeError("SMSM入力欄が同一です")
            stage_started_at = time.monotonic()
            password_element = self._find_unique_password_field()
            self._trace_elapsed(trace, "smsm_find_password_field", stage_started_at)
            self._trace(trace, "password_field_found", True)
            self._trace_field_diagnostics(trace, "password", password_element)
            for key, value in (("company_and_password_fields_distinct", company_element is not password_element), ("user_and_password_fields_distinct", user_element is not password_element)):
                self._trace(trace, key, value)
            if len({id(company_element), id(user_element), id(password_element)}) != 3:
                self._trace(trace, "login_submit_blocked", True)
                self._trace(trace, "credential_mapping_valid", False)
                raise RuntimeError("SMSM入力欄が同一です")
            stage_started_at = time.monotonic()
            for field_name, element, value, others in (("company", company_element, company_code, (username, password)), ("user", user_element, username, (company_code, password)), ("password", password_element, password, (company_code, username))):
                self._fill_element(element, value)
                self._trace(trace, f"{field_name}_field_filled", True)
                self._validate_input(trace, field_name, element, value, *others)
            self._trace_elapsed(trace, "smsm_fill_credentials", stage_started_at)
            self._trace(trace, "credential_mapping_valid", True)
            stage_started_at = time.monotonic()
            login_button = self._find_login_button()
            self._trace_elapsed(trace, "smsm_find_login_button", stage_started_at)
            self._trace(trace, "login_button_found", True)
            self._trace(trace, "smsm_login_form_visible_before_submit", True)
            self._trace(trace, "smsm_login_submit_called", False)
            self._trace(trace, "smsm_login_submit_count", 0)
            stage_started_at = time.monotonic()
            try:
                login_button.click()
            except Exception as exc:
                self._trace(trace, "login_submitted", False)
                raise RuntimeError("SMSMログイン送信失敗") from exc
            finally:
                self._trace_elapsed(trace, "smsm_submit_login", stage_started_at)
                self._trace_elapsed(trace, "smsm_login_click", stage_started_at)
            self._trace(trace, "smsm_login_click_completed", True)
            self._trace(trace, "login_submitted", True)
            self._trace(trace, "smsm_login_submit_called", True)
            self._trace(trace, "smsm_login_submit_count", 1)
            self._trace(trace, "smsm_login_form_visible_after_submit", True)
            stage_started_at = time.monotonic()
            try:
                additional_auth = self._detect_additional_auth()
            finally:
                self._trace_elapsed(trace, "smsm_post_click_navigation", stage_started_at)
            for key, value in additional_auth.items():
                self._trace(trace, key, value)
            if additional_auth["login_error_banner_detected"]:
                raise RuntimeError("SMSMログインエラー表示")
            self._trace(trace, "smsm_wait_login_complete", True)
            self._trace(trace, "smsm_login_wait_called", True)
            stage_started_at = time.monotonic()
            try:
                self._wait_for_login_success(timeout=30, trace=trace)
            finally:
                self._trace_elapsed(trace, "smsm_wait_login_complete", stage_started_at)
            self._trace(trace, "smsm_validate_logged_in_page", True)
            self._trace(trace, "smsm_login_wait_completed", True)
            self._trace(trace, "smsm_login_wait_timeout", False)
            self._trace(trace, "login_completed", True)
        except Exception as exc:
            self._trace(trace, "smsm_login_wait_completed", False)
            self._trace(trace, "smsm_login_wait_timeout", isinstance(exc, (TimeoutException, WebDriverException)))
            self.logger.exception("SMSMのログイン操作に失敗しました")
            raise RuntimeError("SMSMログイン失敗") from exc

    @staticmethod
    def _trace(trace, key: str, value) -> None:
        if trace is not None:
            trace(key, value)

    @classmethod
    def _trace_elapsed(cls, trace, stage: str, started_at: float) -> None:
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        cls._trace(trace, f"{stage}_elapsed_ms", elapsed_ms)
        cls._trace(trace, f"{stage}_slow", elapsed_ms > 10000)

    def _login_url(self) -> str:
        return self.smsm_config.url

    def _validate_login_config(self) -> tuple[str, str, str]:
        if not self.smsm_config.valid:
            raise RuntimeError("SMSMログイン失敗: 資格情報が不足しています")
        return self.smsm_config.company_code, self.smsm_config.username, self.smsm_config.password

    def _find_company_field(self):
        return self._find_login_field("company", "user_company_code", "user[company_code]", "text")

    def _find_user_field(self):
        return self._find_login_field("user", "user_login", "user[login]", "text")

    def _find_login_field(self, field_name: str, element_id: str, element_name: str, expected_type: str):
        driver = self.browser.driver
        if driver is None:
            raise RuntimeError(f"SMSM{field_name}欄を確認できません")
        candidates = self._usable_login_elements(driver.find_elements(By.ID, element_id), expected_type)
        if not candidates:
            candidates = self._usable_login_elements(driver.find_elements(By.NAME, element_name), expected_type)
        if len(candidates) != 1:
            raise RuntimeError(f"SMSM{field_name}欄を一意に確認できません")
        return candidates[0]

    def _find_unique_password_field(self):
        return self._find_login_field("password", "user_password", "user[password]", "password")

    def _usable_login_elements(self, elements, expected_type: str):
        usable = []
        seen = set()
        for element in elements:
            if id(element) in seen:
                continue
            seen.add(id(element))
            if self._safe_attribute(element, "type") != expected_type:
                continue
            if not self._safe_bool(element, "is_displayed") or not self._safe_bool(element, "is_enabled"):
                continue
            if self._safe_attribute(element, "readonly") is not None:
                continue
            if self._safe_attribute(element, "disabled") is not None:
                continue
            usable.append(element)
        return usable

    @staticmethod
    def _fill_element(element, value: str) -> None:
        element.clear()
        element.send_keys(value)

    @staticmethod
    def _input_value(element) -> str:
        try:
            value = element.get_attribute("value")
        except Exception:
            return ""
        return value if isinstance(value, str) else ""

    def _validate_input(self, trace, field_name: str, element, expected: str, *other_values: str) -> None:
        actual = self._input_value(element)
        prefix = f"{field_name}_input"
        exact = bool(actual) and actual == expected
        trimmed = bool(actual) and actual.strip() == expected.strip()
        nfkc = bool(actual) and unicodedata.normalize("NFKC", actual) == unicodedata.normalize("NFKC", expected)
        ascii_normalized = bool(actual) and actual.encode("ascii", "ignore").decode() == expected.encode("ascii", "ignore").decode()
        allowed_match = exact or trimmed or nfkc or ascii_normalized
        checks = {
            f"{field_name}_input_nonblank_after_send": bool(actual),
            f"{field_name}_input_exact_match": exact,
            f"{field_name}_input_trimmed_match": trimmed,
            f"{field_name}_input_nfkc_match": nfkc,
            f"{field_name}_input_ascii_normalized_match": ascii_normalized,
            f"{field_name}_input_prefix_preserved": bool(actual) and actual[:1] == expected[:1],
            f"{field_name}_input_suffix_preserved": bool(actual) and actual[-1:] == expected[-1:],
            f"{field_name}_input_was_truncated": bool(actual) and len(actual) < len(expected) and not allowed_match,
            f"{field_name}_input_was_transformed": bool(actual) and not exact,
            f"{field_name}_length_match": bool(actual) and len(actual) == len(expected),
        }
        for key, value in checks.items():
            self._trace(trace, key, value)
        self._trace(trace, f"{field_name}_field_length_match", checks[f"{field_name}_length_match"])
        if not allowed_match or any(actual == other for other in other_values if other):
            self._trace(trace, "login_submit_blocked", True)
            self._trace(trace, "credential_mapping_valid", False)
            raise RuntimeError(f"SMSM{field_name}入力検証に失敗しました")

    def _trace_field_diagnostics(self, trace, field_name: str, element) -> None:
        prefix = f"{field_name}_field_"
        attributes = {name: self._safe_attribute(element, name) for name in ("id", "name", "type", "autocomplete", "inputmode", "maxlength", "pattern")}
        label_for = self._safe_attribute(element, "id")
        label_linked = self._has_linked_label(label_for)
        checks = {
            "candidate_count": 1,
            "id_present": bool(attributes["id"]),
            "name_present": bool(attributes["name"]),
            "type_text": attributes["type"] == "text",
            "autocomplete_present": bool(attributes["autocomplete"]),
            "inputmode_present": bool(attributes["inputmode"]),
            "maxlength_present": bool(attributes["maxlength"]),
            "pattern_present": bool(attributes["pattern"]),
            "label_linked": label_linked,
            "displayed": self._safe_bool(element, "is_displayed"),
            "enabled": self._safe_bool(element, "is_enabled"),
            "readonly": self._safe_attribute(element, "readonly") is not None,
            "disabled": self._safe_attribute(element, "disabled") is not None,
        }
        for key, value in checks.items():
            self._trace(trace, prefix + key, value)
        fingerprint = "|".join(f"{key}={value or ''}" for key, value in attributes.items())
        self._trace(trace, prefix + "selector_fingerprint", hashlib.sha256(fingerprint.encode()).hexdigest()[:12])

    @staticmethod
    def _safe_attribute(element, name: str):
        try:
            return element.get_attribute(name)
        except Exception:
            return None

    def _has_linked_label(self, element_id) -> bool:
        if not element_id or self.browser.driver is None:
            return False
        try:
            return bool(self.browser.driver.find_elements(By.CSS_SELECTOR, f"label[for='{element_id}']"))
        except Exception:
            return False

    @staticmethod
    def _safe_bool(element, method_name: str) -> bool:
        try:
            return bool(getattr(element, method_name)())
        except Exception:
            return False

    def _detect_additional_auth(self) -> dict[str, bool]:
        driver = self.browser.driver
        result = {
            "additional_auth_detected": False,
            "confirmation_page_detected": False,
            "redirected_window_detected": False,
            "login_error_banner_detected": False,
        }
        if driver is None:
            return result

        try:
            result["redirected_window_detected"] = len(driver.window_handles) > 1
        except Exception:
            pass

        marker_locators = [
            (By.CSS_SELECTOR, "input[name='otp']"),
            (By.CSS_SELECTOR, "input[id='otp']"),
            (By.CSS_SELECTOR, "input[data-testid='otp']"),
            (By.CSS_SELECTOR, "input[name='mfa']"),
            (By.CSS_SELECTOR, "input[id='mfa']"),
            (By.CSS_SELECTOR, "input[data-testid='mfa']"),
        ]
        confirmation_locators = [
            (By.CSS_SELECTOR, "[data-testid='confirmation']"),
            (By.CSS_SELECTOR, "[data-testid='confirm-page']"),
            (By.CSS_SELECTOR, "form[id='confirmation']"),
            (By.CSS_SELECTOR, "form[name='confirmation']"),
        ]
        error_locators = [
            (By.CSS_SELECTOR, ".alert-danger"),
            (By.CSS_SELECTOR, ".alert-error"),
        ]
        for key, locators in (
            ("additional_auth_detected", marker_locators),
            ("confirmation_page_detected", confirmation_locators),
            ("login_error_banner_detected", error_locators),
        ):
            try:
                self.browser.find_first(locators, timeout=1)
                result[key] = True
            except Exception:
                pass
        return result

    def upload_certificate(self, file_path: Path, imei: str) -> None:
        normalized_imei = normalize_imei(imei)
        certificate_path = self._validate_imei_named_certificate(file_path, normalized_imei)
        self.logger.info("証明書アップロード開始 imei_fingerprint=%s", hashlib.sha256(normalized_imei.encode("utf-8")).hexdigest()[:12])
        self.login_and_navigate()
        duplicate = self.check_certificate_duplicate_by_imei(normalized_imei)
        if not (
            duplicate.get("duplicate_search_called") is True
            and duplicate.get("duplicate_check_determinate") is True
            and duplicate.get("exact_imei_match_count") == 0
            and duplicate.get("same_name_certificate_match_count") == 0
        ):
            raise RuntimeError("SMSM既存証明書の重複確認が不確定なため停止しました")
        self._click_first_required([
            (By.XPATH, "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'add') or contains(normalize-space(.), '追加') ]"),
        ], "証明書追加")
        self.set_certificate_file(certificate_path, allow_upload=True)

    def _strict_client_certificate_page_state(self, driver, expected_path: str) -> dict[str, object]:
        probe_state = {
            "smsm_strict_page_probe_called": True,
            "smsm_strict_page_probe_completed": False,
            "smsm_strict_page_probe_exception_type": "",
            "smsm_strict_page_probe_snapshot_available": False,
            "smsm_strict_page_probe_failed_phase": "",
        }
        observation: dict[str, object] = {}

        def run_phase(phase: str, script: str) -> dict[str, object]:
            try:
                value = driver.execute_script(f"/* {phase} */\n{script}", expected_path)
                if not isinstance(value, dict) or not all(
                    isinstance(item, (str, int, float, bool)) or item is None
                    or (isinstance(item, list) and all(isinstance(entry, (str, int, float, bool)) or entry is None for entry in item))
                    for item in value.values()
                ):
                    raise TypeError("non_scalar_probe_result")
                return value
            except Exception as exc:
                probe_state["smsm_strict_page_probe_failed_phase"] = phase
                probe_state["smsm_strict_page_probe_javascript_error_name"] = type(exc).__name__
                probe_state["smsm_strict_page_probe_exception_type"] = type(exc).__name__
                probe_state.update({
                    "smsm_settings_nav_observed": False,
                    "smsm_ios_settings_observed": False,
                    "smsm_client_certificate_menu_observed": False,
                    "smsm_certificate_search_input_observed": False,
                    "smsm_certificate_add_icon_observed": False,
                })
                raise

        try:
            observation.update(run_phase("probe_base_dom", """
                const current = (location.pathname || '').replace(/\\/$/, '') || '/';
                const expected = String(arguments[0] || '').replace(/\\/$/, '') || '/';
                return {smsm_certificate_pathname_matches: current === expected};
            """))
            observation.update(run_phase("resolve_settings_navigation", """
                const visible = item => Boolean(item && (item.offsetWidth || item.offsetHeight || item.getClientRects().length)) && !item.hidden && getComputedStyle(item).display !== 'none' && getComputedStyle(item).visibility !== 'hidden';
                const text = item => String(item.innerText || item.textContent || '').replace(/\\s+/g, ' ').trim();
                const ariaText = item => String(item.getAttribute('aria-label') || item.getAttribute('title') || '').replace(/\\s+/g, ' ').trim();
                const exact = (item, pattern) => pattern.test(text(item)) || pattern.test(ariaText(item));
                const clickable = item => item && (item.matches('a,button,[role="link"],[role="button"],[role="tab"],[role="menuitem"]') ? item : item.closest('a,button,[role="link"],[role="button"],[role="tab"],[role="menuitem"]'));
                const selected = item => Boolean(item && (item.getAttribute('aria-current') || item.getAttribute('aria-selected') || /(^|\\s)(active|selected)(\\s|$)/i.test(String(item.className || ''))));
                const selectedTree = item => Boolean(item && (selected(item) || Array.from(item.parentElement ? item.parentElement.querySelectorAll(':scope > *') : []).some(selected)));
                const navRoots = Array.from(document.querySelectorAll('nav,header,[role="navigation"],[class*="global-nav" i],[class*="top-nav" i],[class*="navbar" i],[data-testid*="nav" i]')).filter(visible).filter(root => exact(root, /設定|settings/i));
                const root = navRoots.sort((a, b) => a.getBoundingClientRect().height - b.getBoundingClientRect().height)[0];
                const nodes = root ? Array.from(root.querySelectorAll('a,button,[role="link"],[role="button"],[role="tab"],[role="menuitem"]')).filter(visible) : [];
                const resolve = pattern => Array.from(new Set(nodes.filter(item => exact(item, pattern)).map(clickable).filter(Boolean)));
                const settings = resolve(/^設定$|^settings?$/i);
                const devices = resolve(/^機器$|^devices?$/i);
                return {smsm_settings_nav_candidate_count: settings.length, smsm_settings_nav_active: settings.length === 1 && selectedTree(settings[0]), smsm_device_nav_active: devices.length === 1 && selectedTree(devices[0])};
            """))
            observation.update(run_phase("resolve_device_navigation", """
                const visible = item => Boolean(item && (item.offsetWidth || item.offsetHeight || item.getClientRects().length)) && !item.hidden && getComputedStyle(item).display !== 'none' && getComputedStyle(item).visibility !== 'hidden';
                const text = item => String(item.innerText || item.textContent || '').replace(/\\s+/g, ' ').trim();
                const ariaText = item => String(item.getAttribute('aria-label') || item.getAttribute('title') || '').replace(/\\s+/g, ' ').trim();
                const clickable = item => item && (item.matches('a,button,[role="link"],[role="button"],[role="tab"],[role="menuitem"]') ? item : item.closest('a,button,[role="link"],[role="button"],[role="tab"],[role="menuitem"]'));
                const selected = item => Boolean(item && (item.getAttribute('aria-current') || item.getAttribute('aria-selected') || /(^|\\s)(active|selected)(\\s|$)/i.test(String(item.className || ''))));
                const roots = Array.from(document.querySelectorAll('nav,header,[role="navigation"],[class*="global-nav" i],[class*="top-nav" i],[class*="navbar" i]')).filter(visible).filter(root => Array.from(root.querySelectorAll('a,button,[role="link"],[role="button"],[role="tab"],[role="menuitem"]')).some(item => /^機器$|^devices?$/i.test(text(item) || ariaText(item))));
                const root = roots.sort((a, b) => a.getBoundingClientRect().height - b.getBoundingClientRect().height)[0];
                const devices = root ? Array.from(new Set(Array.from(root.querySelectorAll('a,button,[role="link"],[role="button"],[role="tab"],[role="menuitem"]')).filter(visible).filter(item => /^機器$|^devices?$/i.test(text(item) || ariaText(item))).map(clickable).filter(Boolean))) : [];
                return {smsm_device_nav_active: devices.length === 1 && (selected(devices[0]) || selected(devices[0].parentElement))};
            """))
            observation.update(run_phase("resolve_ios_settings", """
                const visible = item => Boolean(item && (item.offsetWidth || item.offsetHeight || item.getClientRects().length)) && !item.hidden && getComputedStyle(item).display !== 'none' && getComputedStyle(item).visibility !== 'hidden';
                const label = item => String(item.innerText || item.textContent || item.getAttribute('aria-label') || item.getAttribute('title') || '').replace(/\\s+/g, ' ').trim();
                const active = item => Boolean(item.getAttribute('aria-current') || item.getAttribute('aria-selected') || /active|selected/i.test(String(item.className || '')));
                const matches = Array.from(document.querySelectorAll('a,button,[role="link"],[role="button"],[role="menuitem"],span,div,li')).filter(visible).filter(item => /^ios$/i.test(label(item)) || /(^|\\s)ios(\\s|$)/i.test(String(item.getAttribute('data-testid') || '') + ' ' + (item.id || '')));
                return {smsm_ios_settings_candidate_count: matches.length, smsm_ios_settings_active: matches.length === 1 && active(matches[0]), smsm_android_settings_active: false};
            """))
            observation.update(run_phase("resolve_android_settings", """
                const visible = item => Boolean(item && (item.offsetWidth || item.offsetHeight || item.getClientRects().length)) && !item.hidden && getComputedStyle(item).display !== 'none' && getComputedStyle(item).visibility !== 'hidden';
                const label = item => String(item.innerText || item.textContent || item.getAttribute('aria-label') || item.getAttribute('title') || '').replace(/\\s+/g, ' ').trim();
                const active = item => Boolean(item.getAttribute('aria-current') || item.getAttribute('aria-selected') || /active|selected/i.test(String(item.className || '')));
                const matches = Array.from(document.querySelectorAll('a,button,[role="link"],[role="button"],[role="menuitem"],span,div,li')).filter(visible).filter(item => /android/i.test(label(item)));
                return {smsm_android_settings_active: matches.some(active)};
            """))
            observation.update(run_phase("resolve_client_certificate_menu", """
                const visible = item => Boolean(item && (item.offsetWidth || item.offsetHeight || item.getClientRects().length)) && !item.hidden && getComputedStyle(item).display !== 'none' && getComputedStyle(item).visibility !== 'hidden';
                const text = item => String(item.innerText || item.textContent || '').replace(/\\s+/g, ' ').trim();
                const ariaText = item => String(item.getAttribute('aria-label') || item.getAttribute('title') || '').replace(/\\s+/g, ' ').trim();
                const isCertificate = item => /クライアント証明書管理|client\\s*certificate\\s*management/i.test(text(item) || ariaText(item));
                const clickable = item => item && (item.matches('a,button,[role="link"],[role="button"],[role="tab"],[role="menuitem"]') ? item : item.closest('a,button,[role="link"],[role="button"],[role="tab"],[role="menuitem"]'));
                const ios = Array.from(document.querySelectorAll('a,button,[role="link"],[role="button"],[role="tab"],[role="menuitem"]')).filter(visible).filter(item => /^ios$/i.test(text(item) || ariaText(item)));
                const roots = Array.from(document.querySelectorAll('aside,nav,[role="navigation"],[role="region"],[class*="sidebar" i],[class*="settings" i]')).filter(visible).filter(root => ios.some(item => root.contains(item)) && Array.from(root.querySelectorAll('*')).some(isCertificate));
                const root = roots.sort((a, b) => a.getBoundingClientRect().width * a.getBoundingClientRect().height - b.getBoundingClientRect().width * b.getBoundingClientRect().height)[0];
                const nodes = root ? Array.from(root.querySelectorAll('a,button,[role="link"],[role="button"],[role="tab"],[role="menuitem"],span,li')).filter(visible) : [];
                const matches = Array.from(new Set(nodes.filter(isCertificate).map(clickable).filter(Boolean)));
                const current = (location.pathname || '').replace(/\\/$/, '') || '/';
                const active = item => Boolean(item.getAttribute('aria-current') || item.getAttribute('aria-selected') || /(^|\\s)(active|selected)(\\s|$)/i.test(String(item.className || '')) || (item.parentElement && /(^|\\s)(active|selected)(\\s|$)/i.test(String(item.parentElement.className || ''))) || (() => { try { return Boolean(item.pathname && item.pathname.replace(/\\/$/, '') === current); } catch (_) { return false; } })());
                return {smsm_client_certificate_menu_candidate_count: matches.length, smsm_client_certificate_menu_active: matches.length === 1 && active(matches[0])};
            """))
            observation.update(run_phase("resolve_certificate_search_input", """
                const visible = item => Boolean(item && (item.offsetWidth || item.offsetHeight || item.getClientRects().length)) && !item.hidden && getComputedStyle(item).display !== 'none' && getComputedStyle(item).visibility !== 'hidden';
                const inputs = Array.from(document.querySelectorAll('input')).filter(visible);
                const global = inputs.filter(item => /^(text|search)$/i.test(item.type || '') || item.getAttribute('role') === 'searchbox' || item.hasAttribute('placeholder') || item.hasAttribute('aria-label'));
                const excluded = item => Boolean(item.closest('header,nav,aside,[role="navigation"],[class*="sidebar" i],[class*="right" i],[class*="upload" i],[class*="add" i]'));
                const center = item => { const rect = item.getBoundingClientRect(); const style = getComputedStyle(item); return rect.left > window.innerWidth * 0.2 && rect.right < window.innerWidth * 0.9 && style.position !== 'fixed' && !excluded(item); };
                const toolbar = item => Boolean(item.closest('form,[role="toolbar"],[class*="toolbar" i],[class*="search" i]'));
                const centerInputs = global.filter(center);
                const toolbarInputs = centerInputs.filter(toolbar);
                const afterExclusion = centerInputs.filter(item => toolbar(item) || Boolean(item.closest('main,[role="main"],[class*="content" i],[class*="certificate" i]')));
                return {smsm_search_input_global_count: global.length, smsm_search_input_inside_center_content_count: centerInputs.length, smsm_search_input_inside_certificate_toolbar_count: toolbarInputs.length, smsm_search_input_after_exclusion_count: afterExclusion.length, smsm_certificate_search_input_candidate_count: afterExclusion.length};
            """))
            observation.update(run_phase("resolve_certificate_add_icon", """
                const visible = item => Boolean(item && (item.offsetWidth || item.offsetHeight || item.getClientRects().length)) && !item.hidden && getComputedStyle(item).display !== 'none' && getComputedStyle(item).visibility !== 'hidden';
                const label = item => [item.innerText, item.getAttribute('aria-label'), item.getAttribute('title'), item.getAttribute('data-testid'), item.className].filter(Boolean).join(' ');
                const icons = Array.from(document.querySelectorAll('button,a,[role="button"],[role="link"],[aria-label],[data-testid],[class]')).filter(visible).filter(item => /add|plus|追加|新規/i.test(label(item)) && !item.closest('tr,[role="row"]'));
                return {smsm_certificate_add_icon_candidate_count: icons.length};
            """))
            observation.update(run_phase("evaluate_active_states", """return {smsm_strict_page_active_states_evaluated: true};"""))
            observation.update(run_phase("build_snapshot", """return {smsm_strict_page_snapshot_built: true};"""))
            probe_state["smsm_strict_page_probe_completed"] = True
            probe_state["smsm_strict_page_probe_snapshot_available"] = True
            observation.update(probe_state)
            verified = self._strict_client_certificate_page_verified(observation)
            observation["smsm_client_certificate_page_live_verified"] = verified
            observation["client_certificate_page_landmark_verified"] = verified
            return observation
        except Exception:
            probe_state["smsm_strict_page_probe_completed"] = False
            probe_state["smsm_strict_page_probe_snapshot_available"] = False
            return {**probe_state, **{key: False for key in (
                "smsm_settings_nav_observed", "smsm_ios_settings_observed", "smsm_client_certificate_menu_observed",
                "smsm_certificate_search_input_observed", "smsm_certificate_add_icon_observed",
            )}}

    def _find_preparation_password_input(self):
        inputs = [
            item for item in self.browser.driver.find_elements(By.CSS_SELECTOR, "input[type='password'],input[type='text']")
            if self._safe_bool(item, "is_displayed") and self._safe_bool(item, "is_enabled") and not self._safe_bool_attribute(item, "readonly")
        ]
        if len(inputs) != 1:
            raise RuntimeError("証明書パスワード入力欄を一意に確認できません")
        return inputs[0]

    @staticmethod
    def _validate_imei_named_certificate(file_path: Path, imei: str) -> Path:
        path = Path(file_path).expanduser().resolve()
        if not path.is_file() or path.suffix.casefold() not in {".p12", ".pfx"}:
            raise RuntimeError("IMEI名の証明書ファイルを確認できません")
        if path.stem != imei or path.stat().st_size <= 0:
            raise RuntimeError("証明書ファイル名または内容を確認できません")
        return path

    def upload_certificate_request(self, request: CertificateUploadRequest, *, allow_upload: bool = False) -> None:
        """Execute no certificate operation unless the caller explicitly opts in."""
        if not isinstance(request, CertificateUploadRequest):
            raise TypeError("証明書受け渡しデータを確認できません")
        if not allow_upload:
            raise PermissionError("証明書アップロードは明示的な許可が必要です")
        form = self.inspect_client_certificate_add_form_dom_for_diagnostic(click_add_button=False)
        if not form.get("add_form_opened"):
            raise RuntimeError("SMSM証明書追加フォームを確認できません")
        driver = self.browser.driver
        if driver is None:
            raise RuntimeError("SMSM証明書追加フォームを確認できません")
        password_inputs = [item for item in driver.find_elements(By.CSS_SELECTOR, "input[type='password']") if self._safe_bool(item, "is_displayed") and self._safe_bool(item, "is_enabled") and not self._safe_bool_attribute(item, "readonly")]
        if len(password_inputs) != 1:
            raise RuntimeError("SMSM証明書入力欄を一意に確認できません")
        self.set_certificate_file(request.certificate_file_path, allow_upload=True)
        password_inputs[0].send_keys(request.certificate_password)
        submit_candidates = [item for item in driver.find_elements(By.CSS_SELECTOR, "button,a,[role='button'],input[type='submit']") if self._safe_bool(item, "is_displayed") and self._safe_bool(item, "is_enabled") and self._is_certificate_submit_control(item)]
        if len(submit_candidates) != 1:
            raise RuntimeError("SMSM証明書送信ボタンを一意に確認できません")
        submit_candidates[0].click()
        self.browser.wait_for_page_ready()

    def _open_client_certificate_management_for_preparation(self) -> None:
        """Reach the already-authenticated certificate management view."""
        manifest = getattr(self, "_certificate_route_manifest", None)
        if not isinstance(manifest, dict):
            raise RuntimeError("SMSM証明書管理画面の保存済み経路が必要です")
        result = self.navigate_verified_final_path_for_diagnostic(manifest)
        if result.get("smsm_client_certificate_page_live_verified") is not True:
            raise RuntimeError("SMSM証明書管理画面を確認できません")

    def check_certificate_duplicate_by_imei(self, imei: str) -> dict[str, object]:
        """Safely classify exact IMEI and exact filename matches in current results."""
        result: dict[str, object] = {
            "duplicate_search_called": True,
            "duplicate_check_determinate": False,
            "duplicate_upload_allowed": False,
            "duplicate_check_failed_phase": "validate_duplicate_target",
            "duplicate_check_exception_type": "",
        }
        phase = "validate_duplicate_target"

        def fail(exc: Exception, failed_phase: str) -> dict[str, object]:
            result["duplicate_check_failed_phase"] = failed_phase
            result["duplicate_check_exception_type"] = type(exc).__name__
            return result

        try:
            normalized_imei = normalize_imei(imei)
            driver = self.browser.driver
            if driver is None:
                raise RuntimeError("SMSM証明書検索画面を確認できません")
            phase = "resolve_duplicate_search_input"
            search_inputs = []
            for element in self._safe_find_driver_elements(driver, By.CSS_SELECTOR, "input"):
                if self._safe_attribute(element, "type") not in {"text", "search", ""}:
                    continue
                if not self._safe_bool(element, "is_displayed") or not self._safe_bool(element, "is_enabled"):
                    continue
                labels = " ".join(str(self._safe_attribute(element, name) or "").casefold() for name in ("id", "name", "data-testid", "aria-label", "placeholder"))
                if "certificate" in labels or "証明書" in labels or "imei" in labels or "search" in labels or "検索" in labels:
                    search_inputs.append(element)
            if len(search_inputs) != 1:
                raise RuntimeError("SMSM証明書検索欄を一意に確認できません")
            phase = "resolve_duplicate_search_button"
            search_buttons = self._search_button_candidates()
            if len(search_buttons) != 1:
                raise RuntimeError("SMSM証明書検索ボタンを一意に確認できません")
            phase = "submit_duplicate_search"
            search_inputs[0].clear()
            search_inputs[0].send_keys(normalized_imei)
            if not self._input_is_nonblank(search_inputs[0]):
                raise RuntimeError("SMSM証明書検索値を確認できません")
            search_buttons[0].click()
            phase = "wait_duplicate_results"
            self.browser.wait_for_page_ready()
            phase = "count_duplicate_matches"
            rows = [row for row in self._safe_find_driver_elements(driver, By.CSS_SELECTOR, "table tbody tr") if self._safe_bool(row, "is_displayed")]
            exact_matches = 0
            same_name_matches = 0
            expected_names = {f"{normalized_imei}{suffix}".casefold() for suffix in (".p12", ".pfx")}
            for row in rows:
                text = self._safe_element_text_for_diagnostic(row)
                tokens = {part for part in re.split(r"[^0-9A-Za-z_.-]+", text) if part}
                exact_matches += int(normalized_imei in tokens)
                same_name_matches += int(bool(expected_names.intersection({token.casefold() for token in tokens})))
            result.update({
                "search_result_row_count": len(rows),
                "exact_imei_match_count": exact_matches,
                "same_name_certificate_match_count": same_name_matches,
                "same_name_certificate_present": same_name_matches > 0,
                "duplicate_check_determinate": True,
                "duplicate_upload_allowed": exact_matches == 0 and same_name_matches == 0,
                "upload_allowed": exact_matches == 0 and same_name_matches == 0,
                "duplicate_check_failed_phase": "completed",
            })
            return result
        except Exception as exc:
            return fail(exc, phase)

    def prepare_certificate_upload_for_diagnostic(self, certificate_path: Path, password: str, imei: str, *, login_and_navigate: bool = True) -> dict[str, object]:
        """Prepare file and password controls without saving or submitting."""
        result = {"smsm_prepare_called": True, "smsm_prepare_page_verified": self._last_navigation_observation.get("smsm_client_certificate_page_live_verified") is True, "smsm_prepare_target_imei_present": bool(imei), "smsm_prepare_certificate_path_present": bool(certificate_path), "smsm_prepare_certificate_password_present": bool(password), "smsm_prepare_duplicate_check_called": False, "smsm_prepare_duplicate_check_completed": False, "smsm_prepare_failed_phase": "", "smsm_prepare_exception_type": "", "file_input_dom_count": 0, "file_input_enabled_count": 0, "file_input_inside_right_panel_count": 0, "file_input_count": 0, "file_input_unique": False, "file_input_send_keys_called": False}
        try:
            if not imei or not certificate_path or not password:
                result.update({"smsm_prepare_failed_phase": "validate_preparation_context", "smsm_prepare_exception_type": "RuntimeError", "upload_ready": False})
                return result
            normalized_imei = normalize_imei(imei)
            certificate_path = self._validate_imei_named_certificate(certificate_path, normalized_imei)
            if login_and_navigate:
                self.login()
                self._open_client_certificate_management_for_preparation()
                result["smsm_prepare_page_verified"] = True
            result["smsm_prepare_failed_phase"] = "check_certificate_duplicate"
            duplicate = self.check_certificate_duplicate_by_imei(normalized_imei)
            duplicate_search_called = duplicate.get("duplicate_search_called", True) is True
            duplicate_determinate = duplicate.get("duplicate_check_determinate") is True
            exact_count = duplicate.get("exact_imei_match_count")
            same_name_count = duplicate.get("same_name_certificate_match_count")
            duplicate_upload_allowed = duplicate.get("upload_allowed") is True or (duplicate_determinate and exact_count == 0 and same_name_count == 0)
            result.update({"smsm_prepare_duplicate_check_called": duplicate_search_called, "smsm_prepare_duplicate_check_completed": duplicate_determinate, "duplicate_search_called": duplicate_search_called, "duplicate_exact_imei_match_count": exact_count, "duplicate_exact_match_count": exact_count, "duplicate_same_name_match_count": same_name_count, "duplicate_check_determinate": duplicate_determinate, "duplicate_upload_allowed": duplicate_upload_allowed, "duplicate_check_failed_phase": duplicate.get("duplicate_check_failed_phase", ""), "duplicate_check_exception_type": duplicate.get("duplicate_check_exception_type", "")})
            if not (result["duplicate_search_called"] and result["duplicate_check_determinate"] and exact_count == 0 and same_name_count == 0 and result["duplicate_upload_allowed"]):
                result.update({"smsm_prepare_failed_phase": "check_certificate_duplicate", "upload_ready": False})
                return result
            result["smsm_prepare_failed_phase"] = "resolve_add_button"
            button_observation = self._inspect_client_certificate_add_button_dom(self.browser.driver)
            result.update({key: value for key, value in button_observation.items() if key != "candidates"})
            result.setdefault("add_button_candidate_count", 0)
            result.setdefault("add_button_unique", False)
            result.setdefault("add_button_displayed", False)
            result.setdefault("add_button_enabled", False)
            result.setdefault("add_button_safe", False)
            result.setdefault("add_button_click_called", False)
            result.setdefault("add_button_click_count", 0)
            candidates = button_observation.get("candidates", []) if isinstance(button_observation, dict) else []
            if len(candidates) != 1:
                raise RuntimeError("add_button")
            controls = self._safe_find_driver_elements(self.browser.driver, By.CSS_SELECTOR, "button,a,[role='button'],[role='link'],[tabindex],input[type='button'],input[type='submit']")
            candidate_index = int(candidates[0].get("element_index", -1))
            if candidate_index < 0 or candidate_index >= len(controls):
                raise RuntimeError("add_button")
            control = controls[candidate_index]
            result["smsm_prepare_failed_phase"] = "click_add_button"
            if not self._safe_bool(control, "is_displayed") or not self._safe_bool(control, "is_enabled") or self._safe_bool_attribute(control, "disabled"):
                raise RuntimeError("add_button")
            result.update({"add_button_displayed": True, "add_button_enabled": True, "add_button_safe": True})
            control.click()
            add_observation = {**button_observation, "add_button_click_called": True, "add_button_click_count": 1}
            add_observation.update({"add_button_displayed": True, "add_button_enabled": True, "add_button_safe": True})
            result.update({key: value for key, value in add_observation.items() if key != "candidates"})
            result["smsm_prepare_failed_phase"] = "wait_initial_add_form"
            add_form_probe_called = False
            add_form_probe_completed = False
            add_form_probe_iteration_count = 0
            latest_form_observation = {}

            def inspect_add_form(current_driver):
                nonlocal add_form_probe_called, add_form_probe_completed, add_form_probe_iteration_count, latest_form_observation
                add_form_probe_called = True
                add_form_probe_iteration_count += 1
                latest_form_observation = self._inspect_add_form_controls_dom(current_driver)
                if latest_form_observation.get("add_form_opened") is True:
                    add_form_probe_completed = True
                    return latest_form_observation
                return False

            try:
                form_observation = WebDriverWait(self.browser.driver, 15.0, poll_frequency=0.3).until(inspect_add_form)
            except Exception:
                result.update({**latest_form_observation, "add_form_opened": False, "add_form_probe_called": add_form_probe_called, "add_form_probe_completed": add_form_probe_completed, "add_form_probe_iteration_count": add_form_probe_iteration_count, "right_side_visible_container_count": latest_form_observation.get("right_side_visible_container_count", 0)})
                raise
            result.update({**form_observation, "add_form_opened": True, "add_form_probe_called": add_form_probe_called, "add_form_probe_completed": add_form_probe_completed, "add_form_probe_iteration_count": add_form_probe_iteration_count, "right_side_visible_container_count": form_observation.get("right_side_visible_container_count", 0)})
            result["smsm_prepare_failed_phase"] = "resolve_file_input"
            file_result = self.set_certificate_file(certificate_path, allow_upload=True)
            result.update({**file_result, "certificate_file_input_send_keys_called": file_result.get("file_input_send_keys_called") is True, "file_input_send_keys_count": 1 if file_result.get("file_input_send_keys_called") is True else 0, "certificate_selected": file_result.get("file_input_send_keys_called") is True, "certificate_file_selected": file_result.get("file_input_send_keys_called") is True, "selected_certificate_filename_exact_imei_match": certificate_path.stem == normalized_imei, "selected_certificate_extension_valid": certificate_path.suffix.casefold() in {".p12", ".pfx"}, "smsm_prepare_failed_phase": "wait_password_input_after_file_selection"})
            password_observation = self._wait_and_resolve_certificate_password_input(self.browser.driver, timeout=15.0)
            result.update(password_observation)
            result["smsm_prepare_failed_phase"] = "resolve_password_input"
            if result.get("password_input_candidate_count") != 1:
                raise RuntimeError("新規作成領域の証明書パスワード欄を一意に確認できません")
            result["smsm_prepare_failed_phase"] = "set_certificate_password"
            password_result = self._send_certificate_password_in_add_form(self.browser.driver, password)
            result.update({**password_result, "password_input_send_keys_count": 1, "password_input_nonblank": True, "password_input_nonblank_after_send": True, "password_input_nonblank_after_send_keys": True, "smsm_prepare_failed_phase": "verify_save_button"})
            save_observation = self._resolve_certificate_save_button_in_add_form(self.browser.driver)
            result.update({**save_observation, "save_button_unique": save_observation.get("save_button_candidate_count") == 1, "save_button_click_called": False, "certificate_submit_button_click_called": False, "upload_button_click_called": False, "certificate_upload_called": False})
            ready = all((
                result.get("duplicate_check_determinate") is True,
                result.get("duplicate_exact_match_count") == 0,
                result.get("duplicate_same_name_match_count") == 0,
                result.get("duplicate_upload_allowed") is True,
                result.get("add_button_click_called") is True,
                result.get("add_form_opened") is True,
                result.get("file_input_send_keys_called") is True,
                result.get("file_input_send_keys_count") == 1,
                result.get("certificate_file_selected") is True,
                result.get("selected_certificate_filename_exact_imei_match") is True,
                result.get("selected_certificate_extension_valid") is True,
                result.get("password_input_send_keys_called") is True,
                result.get("password_input_send_keys_count") == 1,
                result.get("password_input_nonblank_after_send_keys") is True,
                result.get("save_button_candidate_count") == 1,
                result.get("save_button_unique") is True,
                result.get("save_button_displayed") is True,
                result.get("save_button_enabled") is True,
                result.get("save_button_click_called") is False,
                result.get("certificate_upload_called") is False,
            ))
            result.update({"upload_ready": ready, "certificate_upload_ready": ready, "smsm_prepare_failed_phase": "completed" if ready else "verify_save_button"})
            self._last_preparation_observation = dict(result)
            return result
        except Exception as exc:
            result.update({"smsm_prepare_exception_type": type(exc).__name__, "smsm_prepare_failed_phase": result.get("smsm_prepare_failed_phase") or "resolve_add_button", "upload_ready": False})
            exc.observation = result
            raise

    def open_client_certificate_page(self, manifest: dict[str, object] | None = None) -> dict[str, object]:
        if not isinstance(manifest, dict):
            raise RuntimeError("保存済みSMSMナビゲーションmanifestが必要です")
        return self.navigate_verified_final_path_for_diagnostic(manifest)

    def find_add_button(self) -> dict[str, object]:
        return self._inspect_client_certificate_add_button_dom(self.browser.driver)

    def open_add_form(self) -> dict[str, object]:
        return self.inspect_client_certificate_add_form_dom_for_diagnostic(click_add_button=True)

    def _add_form_control_groups(self, driver) -> dict[str, object]:
        if not hasattr(driver, "execute_script"):
            panels = [panel for panel in self._safe_find_driver_elements(driver, By.CSS_SELECTOR, "[role='complementary'], [class*='side-panel' i], [class*='drawer' i]") if self._safe_bool(panel, "is_displayed")]
            container = panels[0] if len(panels) == 1 else None
            files = self._safe_find_elements_from(container, By.CSS_SELECTOR, "input[type='file']") if container is not None else []
            return {"container": container, "passwords": [], "saveCandidates": [], "fileCount": len(files), "saveCount": 0}
        return driver.execute_script("""
            const visible = item => Boolean(item && (item.offsetWidth || item.offsetHeight || item.getClientRects().length)) && !item.hidden && getComputedStyle(item).display !== 'none' && getComputedStyle(item).visibility !== 'hidden';
            const rightSide = item => item.getBoundingClientRect().left >= window.innerWidth * 0.45;
            const files = Array.from(document.querySelectorAll('input[type="file"]')).filter(item => !item.disabled && item.getAttribute('aria-disabled') !== 'true');
            const saves = Array.from(document.querySelectorAll('button,a,[role="button"],input[type="submit"]')).filter(item => visible(item) && !item.disabled && /^(保存|save)$/i.test(String(item.innerText || item.value || item.getAttribute('aria-label') || '').trim()));
            const ancestors = files.length === 1 && saves.length === 1 ? Array.from(document.querySelectorAll('*')).filter(node => rightSide(node) && node.contains(files[0]) && node.contains(saves[0])) : [];
            const container = ancestors.length ? ancestors.reduce((small, node) => node.querySelectorAll('*').length < small.querySelectorAll('*').length ? node : small) : null;
            const passwords = container ? Array.from(container.querySelectorAll('input[type="password"],input[type="text"]')).filter(item => visible(item) && !item.disabled && item.getAttribute('aria-disabled') !== 'true' && !item.readOnly) : [];
            const saveCandidates = container ? Array.from(container.querySelectorAll('button,a,[role="button"],input[type="submit"]')).filter(item => visible(item) && !item.disabled && /^(保存|save)$/i.test(String(item.innerText || item.value || item.getAttribute('aria-label') || '').trim())) : [];
            return {container, passwords, saveCandidates, fileCount: files.filter(item => container && container.contains(item)).length, saveCount: saveCandidates.length};
        """) or {"container": None, "passwords": [], "saveCandidates": [], "fileCount": 0, "saveCount": 0}

    def _wait_and_resolve_certificate_password_input(self, driver, timeout: float) -> dict[str, object]:
        def probe(current_driver):
            groups = self._add_form_control_groups(current_driver)
            if groups.get("passwords"):
                return groups
            return False
        groups = WebDriverWait(driver, timeout, poll_frequency=0.3).until(probe)
        count = len(groups.get("passwords", []))
        return {"password_input_visible_count": count, "password_input_candidate_count": count, "password_input_unique": count == 1}

    def _send_certificate_password_in_add_form(self, driver, password: str) -> dict[str, object]:
        if not password:
            raise ValueError("証明書パスワードを確認できません")
        groups = self._add_form_control_groups(driver)
        passwords = groups.get("passwords", [])
        if len(passwords) != 1:
            raise RuntimeError("新規作成領域の証明書パスワード欄を一意に確認できません")
        passwords[0].send_keys(password)
        return {"password_input_count": 1, "password_input_send_keys_called": True}

    def _resolve_certificate_save_button_in_add_form(self, driver) -> dict[str, object]:
        groups = self._add_form_control_groups(driver)
        saves = groups.get("saveCandidates", [])
        return {"save_button_candidate_count": len(saves), "save_button_enabled": len(saves) == 1 and self._safe_bool(saves[0], "is_enabled"), "save_button_displayed": len(saves) == 1 and self._safe_bool(saves[0], "is_displayed")}

    def set_certificate_file(self, certificate_path: Path, *, allow_upload: bool = False) -> dict[str, object]:
        if not allow_upload:
            raise PermissionError("証明書ファイル入力は明示的な許可が必要です")
        certificate_path = Path(certificate_path).expanduser().resolve()
        if not certificate_path.is_file():
            raise FileNotFoundError("証明書ファイルを確認できません")
        if certificate_path.suffix.casefold() not in {".p12", ".pfx"} or certificate_path.stat().st_size <= 0:
            raise ValueError("証明書ファイル形式またはサイズを確認できません")
        driver = self.browser.driver
        if driver is None:
            raise RuntimeError("証明書追加フォームを確認できません")
        dom_inputs = self._safe_find_driver_elements(driver, By.CSS_SELECTOR, "input[type='file']")
        enabled_dom_inputs = [item for item in dom_inputs if self._safe_bool(item, "is_enabled") and not self._safe_bool_attribute(item, "disabled")]
        groups = self._add_form_control_groups(driver)
        container = groups.get("container")
        inputs = [item for item in self._safe_find_elements_from(container, By.CSS_SELECTOR, "input[type='file']") if self._safe_bool(item, "is_enabled") and not self._safe_bool_attribute(item, "disabled")] if container is not None else []
        observation = {
            "file_input_dom_count": len(dom_inputs),
            "file_input_enabled_count": len(enabled_dom_inputs),
            "file_input_inside_right_panel_count": len(inputs),
            "file_input_count": 0,
            "file_input_unique": False,
            "file_input_send_keys_called": False,
            "file_input_send_keys_count": 0,
            "certificate_selected": False,
        }
        if len(inputs) != 1:
            error = RuntimeError("右側パネルの証明書ファイル入力欄を一意に確認できません")
            error.observation = observation
            raise error
        inputs[0].send_keys(str(certificate_path))
        return {**observation, "file_input_count": 1, "file_input_unique": True, "file_input_send_keys_called": True, "file_input_send_keys_count": 1, "certificate_selected": True}

    def set_certificate_password(self, password: str, *, allow_upload: bool = False) -> dict[str, object]:
        if not allow_upload:
            raise PermissionError("証明書パスワード入力は明示的な許可が必要です")
        if not password:
            raise ValueError("証明書パスワードを確認できません")
        inputs = [item for item in self.browser.driver.find_elements(By.CSS_SELECTOR, "input[type='password']") if self._safe_bool(item, "is_displayed") and self._safe_bool(item, "is_enabled") and not self._safe_bool_attribute(item, "readonly")]
        if len(inputs) != 1:
            raise RuntimeError("証明書パスワード入力欄を一意に確認できません")
        inputs[0].send_keys(password)
        return {"password_input_count": 1, "password_input_send_keys_called": True}

    def submit_certificate_upload(self, *, allow_upload: bool = False, imei: str | None = None) -> dict[str, object]:
        if not allow_upload:
            raise PermissionError("証明書アップロード送信は明示的な許可が必要です")
        result = {"save_button_refetch_candidate_count": 0, "save_button_refetch_unique": False, "save_button_refetch_displayed": False, "save_button_refetch_enabled": False, "save_button_inside_current_add_form": False, "save_button_click_called": False, "save_button_click_count": 0, "certificate_upload_called": False, "certificate_upload_completion_wait_called": False, "certificate_upload_completion_verified": False, "add_form_closed_after_save": False, "upload_success_message_detected": False, "certificate_list_visible_after_save": False, "post_upload_search_called": False, "post_upload_search_completed": False, "post_upload_exact_match_count": 0, "certificate_upload_verified": False}
        groups = self._add_form_control_groups(self.browser.driver)
        candidates = groups.get("saveCandidates", [])
        container = groups.get("container")
        result.update({"save_button_refetch_candidate_count": len(candidates), "save_button_refetch_unique": len(candidates) == 1, "save_button_refetch_displayed": len(candidates) == 1 and self._safe_bool(candidates[0], "is_displayed"), "save_button_refetch_enabled": len(candidates) == 1 and self._safe_bool(candidates[0], "is_enabled") and not self._safe_bool_attribute(candidates[0], "disabled"), "save_button_inside_current_add_form": len(candidates) == 1 and container is not None})
        if not all((result["save_button_refetch_unique"], result["save_button_refetch_displayed"], result["save_button_refetch_enabled"], result["save_button_inside_current_add_form"])):
            error = RuntimeError("保存ボタンの再確認に失敗しました")
            error.observation = result
            raise error
        candidates[0].click()
        result.update({"save_button_click_called": True, "save_button_click_count": 1, "certificate_upload_called": True, "certificate_upload_completion_wait_called": True})
        self.browser.wait_for_page_ready()
        completion = self._inspect_client_certificate_upload_dom()
        result.update({"add_form_closed_after_save": completion.get("upload_form_count") == 0, "upload_success_message_detected": completion.get("upload_success_message_detected") is True, "certificate_list_visible_after_save": completion.get("certificate_table_count", 0) >= 1})
        result["certificate_upload_completion_verified"] = any((result["add_form_closed_after_save"], result["upload_success_message_detected"], result["certificate_list_visible_after_save"]))
        if not result["certificate_upload_completion_verified"]:
            error = RuntimeError("証明書登録完了を確認できません")
            error.observation = result
            raise error
        if not imei:
            raise RuntimeError("アップロード後のIMEI再検索対象がありません")
        result["post_upload_search_called"] = True
        post_search = self.check_certificate_duplicate_by_imei(imei)
        result["post_upload_search_completed"] = post_search.get("duplicate_check_determinate") is True
        result["post_upload_exact_match_count"] = post_search.get("exact_imei_match_count", 0)
        result["certificate_upload_verified"] = result["post_upload_search_completed"] and result["post_upload_exact_match_count"] == 1
        if not result["certificate_upload_verified"]:
            error = RuntimeError("アップロード後のIMEI完全一致件数が1件ではありません")
            error.observation = result
            raise error
        return result

    def verify_certificate_upload(self) -> dict[str, object]:
        observation = self._inspect_client_certificate_upload_dom()
        if not observation:
            raise RuntimeError("SMSM証明書アップロード結果を確認できません")
        return observation

    def inspect_client_certificate_navigation_only(self, serial: str, trace=None) -> dict[str, object]:
        """Navigate to client-certificate settings and inspect without editing."""
        result = {
            "other_settings_candidate_count": 0, "other_settings_unique": False,
            "other_settings_click_called": False, "other_settings_click_count": 0,
            "device_settings_panel_candidate_count": 0, "device_settings_panel_unique": False,
            "device_settings_panel_visible": False, "client_certificate_item_candidate_count": 0,
            "client_certificate_item_unique": False, "client_certificate_item_click_called": False,
            "client_certificate_item_click_count": 0, "client_certificate_panel_candidate_count": 0,
            "client_certificate_panel_unique": False, "client_certificate_panel_visible": False,
            "client_certificate_unconfigured_state_detected": False,
            "client_certificate_existing_value_detected": False,
            "client_certificate_edit_candidate_count": 0, "client_certificate_edit_unique": False,
            "client_certificate_edit_displayed": False, "client_certificate_edit_enabled": False,
            "client_certificate_edit_click_called": False, "client_certificate_edit_click_count": 0,
            "device_imei_send_keys_called": False, "device_imei_send_keys_count": 0,
            "device_binding_save_called": False, "device_binding_save_count": 0,
            "excel_write_called": False, "certificate_upload_called": False,
            "device_detail_scroll_called": False, "device_detail_scroll_count": 0,
            "device_detail_scroll_target_resolved": False, "device_detail_scroll_container_scrollable": False,
            "device_detail_scroll_position_before": 0, "device_detail_scroll_position_after": 0,
            "device_detail_scroll_position_changed": False, "other_settings_found_before_scroll": False,
            "other_settings_displayed_before_scroll": False, "other_settings_found_after_scroll": False,
            "other_settings_displayed_after_scroll": False, "other_settings_refetch_candidate_count": 0,
        }
        selection = self.select_matched_device_row(serial, trace=trace)
        result.update(selection)
        if selection.get("device_result_identity_verified") is not True:
            return result
        panel = selection.get("device_detail_panel")
        other = self._find_panel_clickables(panel, ("他の設定を見る", "Other settings")) if panel is not None else []
        result["other_settings_candidate_count"] = len(other)
        result["other_settings_unique"] = len(other) == 1
        result["other_settings_found_before_scroll"] = bool(other)
        result["other_settings_displayed_before_scroll"] = bool(other and self._safe_bool(other[0], "is_displayed"))
        if len(other) == 1 and not result["other_settings_displayed_before_scroll"]:
            scroll = self._scroll_detail_panel_for_other_settings(panel, other[0])
            result.update(scroll)
            other = self._find_panel_clickables(panel, ("他の設定を見る", "Other settings")) if panel is not None else []
            result["other_settings_refetch_candidate_count"] = len(other)
            result["other_settings_found_after_scroll"] = bool(other)
            result["other_settings_displayed_after_scroll"] = bool(other and self._safe_bool(other[0], "is_displayed"))
        elif len(other) == 1:
            result["other_settings_found_after_scroll"] = True
            result["other_settings_displayed_after_scroll"] = result["other_settings_displayed_before_scroll"]
        if len(other) != 1:
            return result
        other[0].click()
        result["other_settings_click_called"] = True
        result["other_settings_click_count"] = 1
        self.browser.wait_for_page_ready()
        settings_panel = self._wait_for_named_panel(("機器の設定", "Device settings"), timeout=10)
        result["device_settings_panel_candidate_count"] = settings_panel["candidate_count"]
        result["device_settings_panel_unique"] = settings_panel["unique"]
        result["device_settings_panel_visible"] = settings_panel["visible"]
        if not settings_panel["unique"]:
            return result
        client_item = self._find_exact_clickables(self.browser.driver, ("クライアント証明書", "Client certificate"))
        result["client_certificate_item_candidate_count"] = len(client_item)
        result["client_certificate_item_unique"] = len(client_item) == 1
        if len(client_item) != 1:
            return result
        client_item[0].click()
        result["client_certificate_item_click_called"] = True
        result["client_certificate_item_click_count"] = 1
        self.browser.wait_for_page_ready()
        certificate_panel = self._wait_for_named_panel(("クライアント証明書", "Client certificate"), timeout=10)
        result["client_certificate_panel_candidate_count"] = certificate_panel["candidate_count"]
        result["client_certificate_panel_unique"] = certificate_panel["unique"]
        result["client_certificate_panel_visible"] = certificate_panel["visible"]
        result["client_certificate_panel"] = certificate_panel.get("panel")
        if not certificate_panel["unique"]:
            return result
        state = self._wait_for_client_certificate_state(timeout=10)
        result.update({key: value for key, value in state.items() if key != "panel"})
        edit = self._certificate_edit_candidates(state.get("panel")) if state.get("panel") is not None else []
        result["client_certificate_edit_candidate_count"] = len(edit)
        result["client_certificate_edit_unique"] = len(edit) == 1
        if edit:
            result["client_certificate_edit_displayed"] = self._safe_bool(edit[0], "is_displayed")
            result["client_certificate_edit_enabled"] = self._safe_bool(edit[0], "is_enabled")
        return result

    def _wait_for_client_certificate_state(self, timeout: float = 10.0, expected_state: str | None = None, trace=None) -> dict[str, object]:
        self._trace(trace, "client_certificate_state_wait_called", True)
        iterations = 0
        snapshot = None
        panel = None
        def locate(_driver):
            nonlocal iterations, snapshot, panel
            iterations += 1
            candidate = self._wait_for_named_panel(("クライアント証明書", "Client certificate"), timeout=0.1)
            if not candidate.get("unique"):
                return False
            panel = candidate.get("panel")
            snapshot = self._classify_client_certificate_panel(panel)
            if expected_state == "view":
                return panel if snapshot.get("client_certificate_view_state_detected") and not snapshot.get("client_certificate_edit_state_detected") else False
            if expected_state == "edit":
                return panel if snapshot.get("client_certificate_edit_state_detected") and not snapshot.get("client_certificate_view_state_detected") else False
            return panel if snapshot.get("client_certificate_view_state_detected") or snapshot.get("client_certificate_edit_state_detected") else False
        try:
            panel = WebDriverWait(self.browser.driver, timeout, poll_frequency=0.25).until(locate)
        except TimeoutException:
            panel = None
        if snapshot is not None:
            snapshot["client_certificate_edit_state_detected"] = bool(
                snapshot.get("client_certificate_selection_control_candidate_count") == 1
                and snapshot.get("client_certificate_save_candidate_count") == 1
                and snapshot.get("client_certificate_cancel_candidate_count") == 1
                and not snapshot.get("client_certificate_reference_edit_control_candidate_count")
            )
        state = snapshot or self._empty_client_certificate_state()
        state.update({
            "panel": panel,
            "client_certificate_state_wait_called": True,
            "client_certificate_state_wait_completed": panel is not None,
            "client_certificate_state_wait_iteration_count": iterations,
            "client_certificate_state_wait_timeout": panel is None,
            "client_certificate_state_snapshot_available": snapshot is not None,
        })
        self._trace(trace, "client_certificate_state_wait_completed", panel is not None)
        return state

    @staticmethod
    def _empty_client_certificate_state() -> dict[str, object]:
        return {
            "client_certificate_view_state_detected": False,
            "client_certificate_edit_state_detected": False,
            "client_certificate_state_resolution_method": "unresolved",
            "client_certificate_save_candidate_count": 0,
            "client_certificate_cancel_candidate_count": 0,
            "client_certificate_selection_control_candidate_count": 0,
        }

    def inspect_client_certificate_edit_form_only(self, serial: str, trace=None, keep_panel: bool = False) -> dict[str, object]:
        """Reach the certificate form, click Edit only from view state, then inspect controls."""
        result = self.inspect_client_certificate_navigation_only(serial, trace=trace)
        result.update({
            "client_certificate_before_unconfigured_count": 0,
            "client_certificate_before_edit_count": 0,
            "client_certificate_before_save_count": 0,
            "client_certificate_before_cancel_count": 0,
            "client_certificate_before_control_element_count": 0,
            "client_certificate_after_unconfigured_count": 0,
            "client_certificate_after_edit_count": 0,
            "client_certificate_after_save_count": 0,
            "client_certificate_after_cancel_count": 0,
            "client_certificate_after_control_element_count": 0,
            "client_certificate_after_snapshot_created": False,
            "client_certificate_after_snapshot_source": "unresolved",
            "client_certificate_after_snapshot_uses_current_classification": False,
            "client_certificate_after_snapshot_uses_before_fallback": False,
            "client_certificate_after_snapshot_metrics_consistent": False,
            "client_certificate_edit_marker_wait_called": False,
            "client_certificate_edit_marker_wait_completed": False,
            "client_certificate_edit_marker_wait_iteration_count": 0,
            "client_certificate_edit_marker_wait_timeout": False,
            "client_certificate_edit_marker_last_snapshot_available": False,
            "client_certificate_edit_marker_success_iteration": 0,
            "client_certificate_edit_transition_detected": False,
            "client_certificate_unconfigured_disappeared": False,
            "client_certificate_edit_disappeared": False,
            "client_certificate_save_appeared": False,
            "client_certificate_cancel_appeared": False,
            "client_certificate_control_appeared": False,
            "client_certificate_edit_control_presence_verified": False,
            "client_certificate_edit_control_element_count": 0,
            "client_certificate_edit_control_field_container_count": 0,
            "client_certificate_edit_control_requires_primary_resolution": True,
            "client_certificate_primary_input_resolved": False,
            "client_certificate_primary_input_resolution_required": True,
            "client_certificate_reference_state_verified": False,
            "client_certificate_reference_edit_clicked": False,
            "client_certificate_edit_click_started": False,
            "client_certificate_edit_click_called": False,
            "client_certificate_edit_click_count": 0,
            "client_certificate_edit_click_completed": False,
            "client_certificate_edit_form_wait_called": False,
            "client_certificate_edit_form_wait_completed": False,
            "client_certificate_edit_form_wait_iteration_count": 0,
            "client_certificate_edit_form_wait_timeout": False,
            "client_certificate_edit_form_raw_candidate_count": 0,
            "client_certificate_edit_form_visible_candidate_count": 0,
            "client_certificate_edit_form_qualified_candidate_count": 0,
            "client_certificate_edit_form_deduplicated_candidate_count": 0,
            "client_certificate_edit_form_wait_function_call_count": 0,
            "client_certificate_edit_form_wait_started_after_click": False,
            "client_certificate_edit_form_wait_received_old_panel": False,
            "client_certificate_edit_form_candidate_count": 0,
            "client_certificate_edit_form_unique": False,
            "client_certificate_edit_form_visible": False,
            "client_certificate_edit_form_resolution_method": "unresolved",
            "client_certificate_edit_form_refetched": False,
            "client_certificate_edit_form_contains_search_input": False,
            "client_certificate_edit_form_contains_result_table": False,
            "client_certificate_view_state_detected": False,
            "client_certificate_edit_state_detected": False,
            "client_certificate_state_resolution_method": "unresolved",
            "client_certificate_save_candidate_count": 0,
            "client_certificate_cancel_candidate_count": 0,
            "client_certificate_selection_control_candidate_count": 0,
            "client_certificate_edit_already_open": False,
            "client_certificate_item_click_target_tag": "",
            "client_certificate_item_click_target_role": "",
            "client_certificate_item_click_target_inside_device_settings_panel": False,
            "client_certificate_item_click_target_exact_name_match": False,
            "client_certificate_item_click_target_contains_edit_text": False,
            "client_certificate_item_click_target_contains_save_text": False,
            "client_certificate_edit_click_target_tag": "",
            "client_certificate_edit_click_target_role": "",
            "client_certificate_edit_click_target_inside_certificate_panel": False,
            "client_certificate_edit_click_target_exact_name_match": False,
            "client_certificate_edit_click_target_count": 0,
            "client_certificate_control_native_select_count": 0,
            "client_certificate_control_combobox_count": 0,
            "client_certificate_control_text_input_count": 0,
            "client_certificate_control_button_count": 0,
            "client_certificate_control_listbox_visible": False,
            "client_certificate_control_popup_open": False,
            "client_certificate_control_current_value_present": False,
            "client_certificate_control_current_value_blank": False,
            "client_certificate_control_disabled": False,
            "client_certificate_control_readonly": False,
            "client_certificate_control_resolution_method": "unresolved",
            "client_certificate_selection_control_click_called": False,
            "client_certificate_selection_control_click_count": 0,
            "client_certificate_option_selection_called": False,
            "client_certificate_option_selection_count": 0,
            "client_certificate_cancel_click_called": False,
        })
        panel = result.get("client_certificate_panel")
        if panel is None or result.get("client_certificate_panel_unique") is not True:
            if not keep_panel:
                result.pop("client_certificate_panel", None)
            return result
        state = self._wait_for_client_certificate_state(timeout=10, expected_state="view", trace=trace)
        panel = state.get("panel")
        result.update(state)
        result.update(self._certificate_state_counts(state))
        result["client_certificate_reference_state_verified"] = state.get("client_certificate_view_state_detected") is True
        if state["client_certificate_view_state_detected"] and state["client_certificate_edit_state_detected"]:
            result["client_certificate_state_resolution_method"] = "ambiguous"
            result.pop("client_certificate_panel", None)
            return result
        if state["client_certificate_edit_state_detected"]:
            result["client_certificate_edit_already_open"] = True
        elif state["client_certificate_view_state_detected"]:
            edit = self._certificate_edit_candidates(panel)
            result["client_certificate_edit_click_target_count"] = len(edit)
            if len(edit) != 1 or not self._safe_bool(edit[0], "is_displayed") or not self._safe_bool(edit[0], "is_enabled"):
                if not keep_panel:
                    result.pop("client_certificate_panel", None)
                return result
            target = edit[0]
            result.update({
                "client_certificate_edit_click_target_tag": self._safe_tag(target),
                "client_certificate_edit_click_target_role": str(self._safe_attribute(target, "role") or ""),
                "client_certificate_edit_click_target_inside_certificate_panel": True,
                "client_certificate_edit_click_target_exact_name_match": True,
            })
            result["client_certificate_edit_click_started"] = True
            result["client_certificate_edit_click_completed"] = False
            try:
                target.click()
            except Exception as exc:
                result["client_certificate_edit_click_exception_type"] = type(exc).__name__
                if not keep_panel:
                    result.pop("client_certificate_panel", None)
                return result
            result["client_certificate_edit_click_called"] = True
            result["client_certificate_edit_click_count"] = 1
            result["client_certificate_edit_click_completed"] = True
            result["client_certificate_reference_edit_clicked"] = True
            result["client_certificate_before_unconfigured_count"] = int(result.get("client_certificate_unconfigured_text_candidate_count", 0) or 0)
            result["client_certificate_before_edit_count"] = int(result.get("client_certificate_edit_candidate_count", 0) or 0)
            result["client_certificate_before_save_count"] = result.get("client_certificate_save_candidate_count", 0)
            result["client_certificate_before_cancel_count"] = result.get("client_certificate_cancel_candidate_count", 0)
            result["client_certificate_before_control_element_count"] = result.get("client_certificate_selection_control_candidate_count", 0)
            refreshed = self._wait_for_client_certificate_edit_markers(timeout=10, trace=trace, panel=panel)
            result.update({key: value for key, value in refreshed.items() if key != "panel"})
            panel = refreshed.get("panel")
            if panel is not None:
                result["client_certificate_panel"] = panel
            result["client_certificate_edit_form_candidate_count"] = refreshed.get("client_certificate_edit_form_candidate_count", 0)
            result["client_certificate_edit_form_unique"] = refreshed.get("client_certificate_edit_form_unique", False)
            result["client_certificate_edit_form_refetched"] = refreshed.get("client_certificate_edit_form_refetched", False)
            result.update(self._certificate_after_snapshot(refreshed, panel))
            result["client_certificate_edit_transition_detected"] = self._certificate_edit_transition_detected(result)
            result["client_certificate_unconfigured_disappeared"] = result.get("client_certificate_after_unconfigured_count") == 0
            result["client_certificate_edit_disappeared"] = result.get("client_certificate_after_edit_count") == 0
            result["client_certificate_save_appeared"] = result.get("client_certificate_after_save_count") == 1
            result["client_certificate_cancel_appeared"] = result.get("client_certificate_after_cancel_count") == 1
            result["client_certificate_control_appeared"] = result.get("client_certificate_after_control_element_count", 0) >= 1
            result["client_certificate_edit_control_presence_verified"] = result["client_certificate_control_appeared"]
            result["client_certificate_edit_control_element_count"] = result.get("client_certificate_after_control_element_count", 0)
            result["client_certificate_edit_control_field_container_count"] = result.get("client_certificate_control_logical_group_count", 0)
        if not keep_panel:
            result.pop("client_certificate_panel", None)
        return result

    def _certificate_edit_candidates(self, panel):
        candidates = []
        text_elements = self._safe_find_elements_from(panel, By.CSS_SELECTOR, "button,a,[role='button'],[role='link'],[onclick],[tabindex],span,div,label")
        for item in text_elements:
            if self._normalize_navigation_name(self._safe_element_text_for_diagnostic(item)) not in {"編集", "edit"}:
                continue
            current = item
            candidate = item if self._is_clickable_certificate_control(item) else None
            for _ in range(6):
                if candidate is not None:
                    break
                try:
                    current = current.find_element(By.XPATH, "./..")
                except Exception:
                    break
                if self._is_clickable_certificate_control(current):
                    candidate = current
                    break
            if candidate is not None and not any(candidate is existing or candidate == existing for existing in candidates):
                candidates.append(candidate)
        return candidates

    def inspect_primary_client_certificate_input(self, panel, trace=None) -> dict[str, object]:
        result = {
            "client_certificate_primary_input_inspection_called": True,
            "client_certificate_primary_input_raw_candidate_count": 0,
            "client_certificate_primary_input_in_edit_panel_count": 0,
            "client_certificate_primary_input_attached_count": 0,
            "client_certificate_primary_input_selenium_visible_count": 0,
            "client_certificate_primary_input_dom_visible_count": 0,
            "client_certificate_primary_input_nonzero_rect_count": 0,
            "client_certificate_primary_input_hidden_type_count": 0,
            "client_certificate_primary_input_disabled_count": 0,
            "client_certificate_primary_input_readonly_count": 0,
            "client_certificate_primary_input_aria_hidden_count": 0,
            "client_certificate_primary_input_focusable_count": 0,
            "client_certificate_primary_input_role_combobox_count": 0,
            "client_certificate_primary_input_aria_controls_count": 0,
            "client_certificate_primary_input_aria_popup_count": 0,
            "client_certificate_primary_input_same_field_container_count": 0,
            "client_certificate_primary_input_same_edit_panel_count": 0,
            "client_certificate_primary_input_background_search_count": 0,
            "client_certificate_primary_input_same_parent_count": 0,
            "client_certificate_primary_input_parent_child_count": 0,
            "client_certificate_primary_input_same_geometry_count": 0,
            "client_certificate_primary_input_candidate_count": 0,
            "client_certificate_primary_input_unique": False,
            "client_certificate_primary_input_resolved": False,
            "client_certificate_primary_input_resolution_method": "unresolved",
            "client_certificate_primary_input_click_called": False,
            "client_certificate_primary_input_focus_called": False,
            "client_certificate_primary_input_value_write_called": False,
            "client_certificate_primary_input_expand_control_count": 0,
            "client_certificate_primary_input_expand_same_field_count": 0,
            "client_certificate_primary_input_expand_click_called": False,
        }
        if panel is None:
            return result
        selectors = "input,textarea,[role='combobox'],[contenteditable='true']"
        candidates = self._safe_find_elements_from(panel, By.CSS_SELECTOR, selectors)
        result["client_certificate_primary_input_raw_candidate_count"] = len(candidates)
        result["client_certificate_primary_input_in_edit_panel_count"] = len(candidates)
        result["client_certificate_primary_input_same_edit_panel_count"] = len(candidates)
        eligible = []
        for candidate in candidates:
            attached = self._safe_bool(candidate, "is_enabled") or self._safe_bool(candidate, "is_displayed")
            selenium_visible = self._safe_bool(candidate, "is_displayed")
            visibility = self._dom_visibility_probe(candidate)
            dom_visible = bool(visibility.get("visible"))
            tag = str(getattr(candidate, "tag_name", "") or "").casefold()
            input_type = str(self._safe_attribute(candidate, "type") or "").casefold()
            role = str(self._safe_attribute(candidate, "role") or "").casefold()
            aria_hidden = str(self._safe_attribute(candidate, "aria-hidden") or "").casefold() == "true"
            disabled = self._safe_bool_attribute(candidate, "disabled") or not self._safe_bool(candidate, "is_enabled")
            readonly = self._safe_bool_attribute(candidate, "readonly")
            tabindex = self._safe_attribute(candidate, "tabindex")
            focusable = False
            try:
                focusable = tabindex is not None and int(str(tabindex)) >= 0 and not disabled and not readonly
            except (TypeError, ValueError):
                focusable = False
            nonzero = dom_visible
            if attached:
                result["client_certificate_primary_input_attached_count"] += 1
            if selenium_visible:
                result["client_certificate_primary_input_selenium_visible_count"] += 1
            if dom_visible:
                result["client_certificate_primary_input_dom_visible_count"] += 1
            if nonzero:
                result["client_certificate_primary_input_nonzero_rect_count"] += 1
            if input_type == "hidden":
                result["client_certificate_primary_input_hidden_type_count"] += 1
            if disabled:
                result["client_certificate_primary_input_disabled_count"] += 1
            if readonly:
                result["client_certificate_primary_input_readonly_count"] += 1
            if aria_hidden:
                result["client_certificate_primary_input_aria_hidden_count"] += 1
            if focusable:
                result["client_certificate_primary_input_focusable_count"] += 1
            if role == "combobox":
                result["client_certificate_primary_input_role_combobox_count"] += 1
            if self._safe_attribute(candidate, "aria-controls") is not None:
                result["client_certificate_primary_input_aria_controls_count"] += 1
            if self._safe_attribute(candidate, "aria-haspopup") is not None:
                result["client_certificate_primary_input_aria_popup_count"] += 1
            eligible_now = attached and (selenium_visible or dom_visible) and nonzero and input_type != "hidden" and not aria_hidden and not disabled
            if eligible_now:
                eligible.append(candidate)
        result["client_certificate_primary_input_candidate_count"] = len(eligible)
        result["client_certificate_primary_input_unique"] = len(eligible) == 1
        result["client_certificate_primary_input_resolved"] = len(eligible) == 1
        if len(eligible) == 1:
            result["client_certificate_primary_input_resolution_method"] = "visible_nonhidden_edit_field_input"
        return result

    @staticmethod
    def _certificate_edit_transition_detected(result: dict[str, object]) -> bool:
        return bool(
            result.get("client_certificate_edit_click_completed") is True
            and result.get("client_certificate_before_unconfigured_count") == 1
            and result.get("client_certificate_before_edit_count") == 1
            and result.get("client_certificate_before_save_count") == 0
            and result.get("client_certificate_before_cancel_count") == 0
            and result.get("client_certificate_after_unconfigured_count") == 0
            and result.get("client_certificate_after_edit_count") == 0
            and result.get("client_certificate_after_save_count") == 1
            and result.get("client_certificate_after_cancel_count") == 1
            and result.get("client_certificate_after_control_element_count", 0) >= 1
        )

    def _certificate_after_snapshot(self, current_classification: dict[str, object], panel) -> dict[str, object]:
        classification = current_classification or {}
        current_panel_classification = self._classify_client_certificate_panel(panel) if panel is not None else {}
        unconfigured_count = int(current_panel_classification.get("client_certificate_unconfigured_text_candidate_count", 0) or 0)
        edit_count = int(classification.get("client_certificate_edit_form_edit_candidate_count", 0) or 0)
        save_count = int(classification.get("client_certificate_edit_form_save_candidate_count", 0) or 0)
        cancel_count = int(classification.get("client_certificate_edit_form_cancel_candidate_count", 0) or 0)
        control_count = int(classification.get("client_certificate_edit_form_control_candidate_count", 0) or 0)
        return {
            "client_certificate_after_unconfigured_count": unconfigured_count,
            "client_certificate_after_edit_count": edit_count,
            "client_certificate_after_save_count": save_count,
            "client_certificate_after_cancel_count": cancel_count,
            "client_certificate_after_control_element_count": control_count,
            "client_certificate_after_snapshot_created": bool(panel is not None),
            "client_certificate_after_snapshot_source": (
                "reacquired_panel_classification" if classification.get("client_certificate_panel_reacquired_after_edit") is True
                else "current_same_panel_rescan" if panel is not None
                else "unresolved"
            ),
            "client_certificate_after_snapshot_uses_current_classification": bool(panel is not None),
            "client_certificate_after_snapshot_uses_before_fallback": False,
            "client_certificate_after_snapshot_metrics_consistent": bool(
                panel is not None
                and unconfigured_count >= 0
                and edit_count >= 0
                and save_count >= 0
                and cancel_count >= 0
                and control_count >= 0
            ),
        }

    def _wait_for_client_certificate_edit_markers(self, timeout: float = 10.0, trace=None, panel=None) -> dict[str, object]:
        iterations = 0
        success_iteration = 0
        last_classification = None
        last_snapshot = None
        current_panel = panel
        self._trace(trace, "client_certificate_edit_marker_wait_called", True)

        def locate(_driver):
            nonlocal iterations, success_iteration, last_classification, last_snapshot, current_panel
            iterations += 1
            if current_panel is not None:
                try:
                    current_panel.is_enabled()
                except StaleElementReferenceException:
                    current_panel = None
            if current_panel is None:
                candidate = self._wait_for_named_panel(("クライアント証明書", "Client certificate"), timeout=0.1)
                current_panel = candidate.get("panel") if candidate.get("panel") is not None else None
            if current_panel is None:
                return False
            classification = self._classify_client_certificate_panel(current_panel)
            classification.update(self._edit_form_control_metrics(current_panel, classification))
            classification.update({
                "client_certificate_edit_form_edit_candidate_count": int(classification.get("client_certificate_edit_candidate_count", 0) or 0),
                "client_certificate_edit_form_save_candidate_count": int(classification.get("client_certificate_save_candidate_count", 0) or 0),
                "client_certificate_edit_form_cancel_candidate_count": int(classification.get("client_certificate_cancel_candidate_count", 0) or 0),
                "client_certificate_edit_form_control_candidate_count": int(classification.get("client_certificate_selection_control_candidate_count", 0) or 0),
            })
            last_classification = classification
            last_snapshot = self._certificate_after_snapshot(classification, current_panel)
            marker_success = all((
                last_snapshot.get("client_certificate_after_unconfigured_count") == 0,
                last_snapshot.get("client_certificate_after_edit_count") == 0,
                last_snapshot.get("client_certificate_after_save_count") == 1,
                last_snapshot.get("client_certificate_after_cancel_count") == 1,
                last_snapshot.get("client_certificate_after_control_element_count", 0) >= 1,
            ))
            if marker_success:
                success_iteration = iterations
                return True
            return False

        try:
            WebDriverWait(self.browser.driver, timeout, poll_frequency=0.25).until(locate)
            completed = True
        except TimeoutException:
            completed = False
        result = dict(last_classification or self._empty_client_certificate_state())
        if last_snapshot:
            result.update(last_snapshot)
        result.update({
            "panel": current_panel,
            "client_certificate_edit_marker_wait_called": True,
            "client_certificate_edit_marker_wait_completed": completed,
            "client_certificate_edit_marker_wait_iteration_count": iterations,
            "client_certificate_edit_marker_wait_timeout": not completed,
            "client_certificate_edit_marker_last_snapshot_available": last_snapshot is not None,
            "client_certificate_edit_marker_success_iteration": success_iteration,
        })
        self._trace(trace, "client_certificate_edit_marker_wait_completed", completed)
        return result

    @staticmethod
    def _certificate_state_counts(state: dict[str, object]) -> dict[str, object]:
        return {
            "client_certificate_after_unconfigured_count": int(state.get("client_certificate_unconfigured_text_candidate_count", 0) or 0),
            "client_certificate_after_edit_count": int(state.get("client_certificate_edit_candidate_count", state.get("client_certificate_reference_edit_control_candidate_count", 0)) or 0),
            "client_certificate_after_save_count": int(state.get("client_certificate_save_candidate_count", 0) or 0),
            "client_certificate_after_cancel_count": int(state.get("client_certificate_cancel_candidate_count", 0) or 0),
            "client_certificate_after_control_element_count": int(state.get("client_certificate_selection_control_candidate_count", 0) or 0),
        }

    def _wait_for_client_certificate_edit_form(self, timeout: float = 10.0, trace=None, panel=None) -> dict[str, object]:
        iterations = 0
        latest = None
        panel_identity_recorded = panel is not None
        panel_same_dom = False
        panel_stale = False
        panel_reacquired = False
        self._trace(trace, "client_certificate_edit_state_wait_called", True)
        def locate(_driver):
            nonlocal iterations, latest, panel, panel_same_dom, panel_stale, panel_reacquired
            iterations += 1
            candidate_panel = None
            if panel is not None:
                try:
                    panel.is_enabled()
                    candidate_panel = panel
                    panel_same_dom = True
                except StaleElementReferenceException:
                    panel_stale = True
                    panel = None
            if candidate_panel is None:
                candidate = self._wait_for_named_panel(("クライアント証明書", "Client certificate"), timeout=0.1)
                if not candidate.get("unique"):
                    return False
                panel = candidate.get("panel")
                candidate_panel = panel
                panel_reacquired = True
            panel = candidate_panel
            latest = self._classify_client_certificate_panel(panel)
            return panel if latest.get("client_certificate_edit_state_detected") else False
        try:
            panel = WebDriverWait(self.browser.driver, timeout, poll_frequency=0.25).until(locate)
        except TimeoutException:
            panel = None
        result = dict(latest or self._empty_client_certificate_state())
        visibility = self._edit_form_visibility_metrics(panel)
        control_metrics = self._edit_form_control_metrics(panel, latest)
        result.update({
            "panel": panel,
            "client_certificate_edit_form_wait_called": True,
            "client_certificate_edit_form_wait_completed": panel is not None,
            "client_certificate_edit_form_wait_iteration_count": iterations,
            "client_certificate_edit_form_wait_timeout": panel is None,
            "client_certificate_edit_form_raw_candidate_count": 1 if panel is not None else 0,
            "client_certificate_edit_form_visible_candidate_count": 1 if visibility["dom_visible"] else 0,
            "client_certificate_edit_form_qualified_candidate_count": 1 if panel is not None and visibility["dom_visible"] else 0,
            "client_certificate_edit_form_deduplicated_candidate_count": 1 if panel is not None and visibility["dom_visible"] else 0,
            "client_certificate_edit_form_candidate_count": 1 if panel is not None else 0,
            "client_certificate_edit_form_unique": panel is not None,
            "client_certificate_edit_form_visible": visibility["dom_visible"],
            "client_certificate_edit_form_resolution_method": "current_dom_edit_panel_landmarks" if panel is not None else "unresolved",
            "client_certificate_edit_form_refetched": panel is not None,
            "client_certificate_edit_form_contains_search_input": bool(panel is not None and self._safe_find_elements_from(panel, By.CSS_SELECTOR, "input[name*='query' i],input[aria-label*='search' i]")),
            "client_certificate_edit_form_contains_result_table": bool(panel is not None and self._safe_find_elements_from(panel, By.CSS_SELECTOR, "table")),
            "client_certificate_edit_form_edit_candidate_count": 0 if panel is not None else 0,
            "client_certificate_edit_form_control_candidate_count": latest.get("client_certificate_selection_control_candidate_count", 0) if latest else 0,
            "client_certificate_edit_form_save_candidate_count": latest.get("client_certificate_save_candidate_count", 0) if latest else 0,
            "client_certificate_edit_form_cancel_candidate_count": latest.get("client_certificate_cancel_candidate_count", 0) if latest else 0,
            "client_certificate_edit_form_wait_function_call_count": 1,
            "client_certificate_edit_form_wait_started_after_click": True,
            "client_certificate_edit_form_wait_received_old_panel": panel_identity_recorded,
            "client_certificate_panel_identity_recorded_before_edit": panel_identity_recorded,
            "client_certificate_panel_identity_available_after_edit": panel is not None,
            "client_certificate_panel_same_dom_identity_after_edit": panel_same_dom,
            "client_certificate_panel_stale_after_edit": panel_stale,
            "client_certificate_panel_reacquired_after_edit": panel_reacquired,
            "client_certificate_edit_form_same_panel_state_refresh": panel_same_dom,
            "client_certificate_panel_identity_metrics_consistent": not (panel is None and panel_same_dom),
            "client_certificate_edit_form_panel_flow_metrics_consistent": not ((panel is None and panel_same_dom) or (not panel_identity_recorded and panel_same_dom)),
            **visibility,
            "client_certificate_edit_form_candidate_right_side_count": 1 if panel is not None else 0,
            "client_certificate_edit_form_candidate_heading_count": 1 if panel is not None else 0,
            "client_certificate_edit_form_candidate_default_label_count": 1 if panel is not None else 0,
            **control_metrics,
        })
        self._trace(trace, "client_certificate_edit_state_wait_completed", panel is not None)
        return result

    def _edit_form_visibility_metrics(self, panel) -> dict[str, object]:
        selenium_displayed = panel is not None and self._safe_bool(panel, "is_displayed")
        probe = self._dom_visibility_probe(panel)
        dom_visible = bool(probe.get("visible"))
        return {
            "dom_visible": dom_visible,
            "selenium_displayed": selenium_displayed,
            "client_certificate_edit_form_selenium_displayed": selenium_displayed,
            "client_certificate_edit_form_dom_visible": dom_visible,
            "client_certificate_edit_form_visibility_verified": dom_visible,
            "client_certificate_edit_form_visibility_resolution_method": "computed_style_and_nonzero_rect" if dom_visible else "unresolved",
            "client_certificate_edit_form_candidate_visibility_method": "computed_style_and_nonzero_rect" if dom_visible else "unresolved",
            "client_certificate_edit_form_candidate_selenium_displayed_count": int(selenium_displayed),
            "client_certificate_edit_form_candidate_dom_visible_count": int(dom_visible),
            "client_certificate_edit_form_candidate_nonzero_rect_count": int(dom_visible),
            "client_certificate_edit_form_candidate_right_side_count": int(dom_visible),
            "client_certificate_edit_form_candidate_heading_count": int(dom_visible),
            "client_certificate_edit_form_candidate_default_label_count": int(dom_visible),
            "client_certificate_edit_form_candidate_aria_hidden_count": 0,
            "client_certificate_edit_form_candidate_css_hidden_count": 0,
            "client_certificate_edit_form_candidate_zero_size_count": int(panel is not None and not dom_visible),
            "client_certificate_edit_form_candidate_stale_count": 0,
            "client_certificate_edit_form_candidate_unclassified_count": 0,
            "client_certificate_edit_form_candidate_metrics_consistent": True,
            "client_certificate_edit_form_dom_visibility_function_call_count": int(probe.get("script_called", False)),
            "client_certificate_edit_form_dom_visibility_true_count": int(dom_visible),
            "client_certificate_edit_form_dom_visibility_false_count": int(panel is not None and not dom_visible),
            "client_certificate_edit_form_dom_visibility_exception_count": int(probe.get("result_type") == "exception"),
            "client_certificate_edit_form_dom_visibility_return_type_valid": probe.get("valid", False),
            "client_certificate_edit_form_visibility_script_called": probe.get("script_called", False),
            "client_certificate_edit_form_visibility_script_result_type": probe.get("result_type", "none"),
            "client_certificate_edit_form_visibility_script_has_visible_key": probe.get("has_visible_key", False),
            "client_certificate_edit_form_visibility_script_has_rect_key": probe.get("has_rect_key", False),
            "client_certificate_edit_form_visibility_script_has_right_side_key": False,
            "client_certificate_edit_form_visibility_script_result_valid": probe.get("valid", False),
            "client_certificate_edit_form_candidate_dom_attached_count": int(panel is not None),
            "client_certificate_edit_form_candidate_detached_count": 0,
            "client_certificate_edit_form_candidate_visibility_evaluated_count": int(panel is not None),
            "client_certificate_edit_form_candidate_visibility_error_count": 0,
            "client_certificate_edit_form_candidate_rect_evaluated_count": int(panel is not None),
            "client_certificate_edit_form_candidate_rect_error_count": 0,
            "client_certificate_edit_form_candidate_left_or_center_count": 0,
        }

    def _edit_form_control_metrics(self, panel, latest) -> dict[str, object]:
        latest = latest or {}
        return {
            "client_certificate_edit_form_candidate_save_count": latest.get("client_certificate_save_candidate_count", 0),
            "client_certificate_edit_form_candidate_cancel_count": latest.get("client_certificate_cancel_candidate_count", 0),
            "client_certificate_edit_form_candidate_control_group_count": latest.get("client_certificate_control_logical_group_count", latest.get("client_certificate_control_group_candidate_count", 0)),
            "client_certificate_control_visibility_source": "current_edit_form_panel",
            "client_certificate_control_input_element_count": latest.get("client_certificate_control_input_element_count", 0),
            "client_certificate_control_expand_element_count": latest.get("client_certificate_control_expand_element_count", 0),
        }

    def _dom_visibility_verified(self, element) -> bool:
        return bool(self._dom_visibility_probe(element).get("visible"))

    def _dom_visibility_probe(self, element) -> dict[str, object]:
        if element is None:
            return {"visible": False, "result_type": "none", "valid": False, "script_called": False, "has_rect_key": False, "has_visible_key": False}
        if str(self._safe_attribute(element, "aria-hidden") or "").casefold() == "true" or self._safe_attribute(element, "hidden") is not None:
            return {"visible": False, "result_type": "filtered", "valid": True, "script_called": False, "has_rect_key": False, "has_visible_key": False}
        try:
            state = self.browser.driver.execute_script("const e=arguments[0],s=getComputedStyle(e),r=e.getBoundingClientRect(); return {display:s.display,visibility:s.visibility,width:r.width,height:r.height,rects:e.getClientRects().length};", element)
        except Exception:
            return {"visible": False, "result_type": "exception", "valid": False, "script_called": True, "has_rect_key": False, "has_visible_key": False}
        valid = isinstance(state, dict) and all(key in state for key in ("display", "visibility", "width", "height", "rects"))
        return {"visible": bool(valid and state.get("display") != "none" and state.get("visibility") != "hidden" and state.get("width", 0) > 0 and state.get("height", 0) > 0 and state.get("rects", 0) > 0), "result_type": type(state).__name__, "valid": valid, "script_called": True, "has_rect_key": isinstance(state, dict) and "width" in state and "height" in state, "has_visible_key": isinstance(state, dict) and "display" in state and "visibility" in state}

    def _is_clickable_certificate_control(self, element) -> bool:
        tag = self._safe_tag(element)
        role = str(self._safe_attribute(element, "role") or "").casefold()
        tabindex = self._safe_attribute(element, "tabindex")
        onclick = self._safe_attribute(element, "onclick")
        try:
            tabindex_ok = tabindex is not None and int(str(tabindex).strip()) >= 0
        except (TypeError, ValueError):
            tabindex_ok = False
        return (
            tag in {"button", "a"} or role in {"button", "link"} or onclick is not None or tabindex_ok
        ) and self._safe_bool(element, "is_displayed") and self._safe_bool(element, "is_enabled") and self._safe_attribute(element, "disabled") is None and str(self._safe_attribute(element, "aria-disabled") or "").casefold() != "true"

    def _classify_client_certificate_panel(self, panel) -> dict[str, object]:
        text = self._safe_element_text_for_diagnostic(panel).casefold()
        text_elements = self._safe_find_elements_from(panel, By.CSS_SELECTOR, "button,a,[role='button'],[role='link'],[onclick],[tabindex],span,div,label")
        reference_text = self._scan_client_certificate_reference_text(panel)
        clickables = self._safe_find_elements_from(panel, By.CSS_SELECTOR, "a,button,[role='link'],[role='button']")
        save = [item for item in clickables if self._normalize_navigation_name(self._safe_element_text_for_diagnostic(item)) in {"保存", "save"}]
        cancel = [item for item in clickables if self._normalize_navigation_name(self._safe_element_text_for_diagnostic(item)) in {"取消", "cancel"}]
        edit = self._certificate_edit_candidates(panel)
        selects = self._safe_find_elements_from(panel, By.CSS_SELECTOR, "select")
        combos = self._safe_find_elements_from(panel, By.CSS_SELECTOR, "[role='combobox']")
        inputs = self._safe_find_elements_from(panel, By.CSS_SELECTOR, "input")
        excluded = save + cancel + edit + [item for item in clickables if self._normalize_navigation_name(self._safe_element_text_for_diagnostic(item)) in {"閉じる", "close", "戻る", "back"}]
        buttons = [
            item for item in clickables
            if item not in excluded
            and (
                self._safe_attribute(item, "aria-haspopup") is not None
                or self._safe_attribute(item, "aria-controls") is not None
                or self._safe_attribute(item, "tabindex") is not None
            )
        ]
        hidden_inputs = [item for item in inputs if str(self._safe_attribute(item, "type") or "").casefold() == "hidden"]
        visible_inputs = [
            item for item in inputs
            if item not in hidden_inputs
            and (
                self._safe_attribute(item, "aria-controls") is not None
                or self._safe_attribute(item, "aria-labelledby") is not None
                or self._safe_attribute(item, "role") == "combobox"
                or self._safe_attribute(item, "name") is not None
            )
        ]
        raw_controls = selects + combos + visible_inputs + buttons
        control_groups = self._group_certificate_controls(raw_controls)
        controls = [group[0] for group in control_groups]
        values = []
        for control in controls:
            value = str(self._safe_attribute(control, "value") or "").strip()
            if value:
                values.append(value)
        heading = reference_text["heading_exact"]
        default_count = reference_text["client_certificate_default_label_deduplicated_candidate_count"]
        unconfigured_count = reference_text["client_certificate_unconfigured_text_deduplicated_candidate_count"]
        view_state = heading and default_count == 1 and (unconfigured_count == 1 or bool(values)) and len(edit) == 1 and not save and not cancel and not controls
        edit_state = heading and len(save) == 1 and len(cancel) == 1 and len(controls) == 1 and not edit
        method = "edit_form_controls" if edit_state else "view_panel_landmarks" if view_state else "unresolved"
        resolution = "native_select" if len(selects) == 1 else "aria_combobox" if len(combos) == 1 else "input_backed_combobox" if inputs else "button_backed_dropdown" if buttons else "unresolved"
        return {
            "client_certificate_view_state_detected": view_state,
            "client_certificate_reference_heading_candidate_count": 1 if heading else 0,
            "client_certificate_reference_heading_exact_match": heading,
            "client_certificate_default_label_candidate_count": default_count,
            "client_certificate_default_label_exact_match": default_count == 1,
            "client_certificate_unconfigured_text_candidate_count": unconfigured_count,
            "client_certificate_unconfigured_text_exact_match": unconfigured_count == 1,
            "client_certificate_reference_edit_text_candidate_count": len([item for item in text_elements if self._normalize_navigation_name(self._safe_element_text_for_diagnostic(item)) in {"編集", "edit"}]),
            "client_certificate_reference_edit_control_candidate_count": len(edit),
            "client_certificate_reference_edit_control_unique": len(edit) == 1,
            "client_certificate_reference_save_candidate_count": len(save),
            "client_certificate_reference_cancel_candidate_count": len(cancel),
            "client_certificate_reference_logical_control_count": len(controls),
            "client_certificate_reference_state_conditions_met": view_state,
            **reference_text,
            "client_certificate_edit_state_detected": edit_state,
            "client_certificate_state_resolution_method": method,
            "client_certificate_save_candidate_count": len(save),
            "client_certificate_save_unique": len(save) == 1,
            "client_certificate_save_displayed": len(save) == 1 and self._safe_bool(save[0], "is_displayed"),
            "client_certificate_save_enabled": len(save) == 1 and self._safe_bool(save[0], "is_enabled"),
            "client_certificate_cancel_candidate_count": len(cancel),
            "client_certificate_cancel_unique": len(cancel) == 1,
            "client_certificate_cancel_displayed": len(cancel) == 1 and self._safe_bool(cancel[0], "is_displayed"),
            "client_certificate_cancel_enabled": len(cancel) == 1 and self._safe_bool(cancel[0], "is_enabled"),
            "client_certificate_selection_control_candidate_count": len(controls),
            "client_certificate_control_raw_element_count": len(raw_controls),
            "client_certificate_control_group_candidate_count": len(control_groups),
            "client_certificate_control_deduplicated_candidate_count": len(control_groups),
            "client_certificate_control_grouping_method": "shared_parent_or_aria_relation",
            "client_certificate_control_input_element_count": len(visible_inputs),
            "client_certificate_control_expand_element_count": len(buttons),
            "client_certificate_control_decorative_element_count": 0,
            "client_certificate_control_group_common_parent_count": len(control_groups),
            "client_certificate_control_group_field_container_count": len(control_groups),
            "client_certificate_control_group_aria_relation_count": sum(bool(self._safe_attribute(item, "aria-controls") or self._safe_attribute(item, "aria-labelledby")) for item in controls),
            "client_certificate_control_group_adjacency_count": 0,
            "client_certificate_control_group_geometry_relation_count": 0,
            "client_certificate_control_logical_group_count": len(control_groups),
            "client_certificate_control_grouping_resolution_method": "common_parent" if len(control_groups) == 1 else "unresolved",
            "client_certificate_control_expand_button_count": len(buttons),
            "client_certificate_control_hidden_input_count": len(hidden_inputs),
            "client_certificate_control_native_select_count": len(selects),
            "client_certificate_control_combobox_count": len(combos),
            "client_certificate_control_text_input_count": len(inputs),
            "client_certificate_control_button_count": len(buttons),
            "client_certificate_control_listbox_visible": bool(self._safe_find_elements_from(panel, By.CSS_SELECTOR, "[role='listbox']")),
            "client_certificate_control_popup_open": bool(self._safe_find_elements_from(panel, By.CSS_SELECTOR, "[role='listbox'][aria-expanded='true'],[aria-expanded='true']")),
            "client_certificate_control_current_value_present": bool(values),
            "client_certificate_control_current_value_blank": not values,
            "client_certificate_control_disabled": any(self._safe_attribute(item, "disabled") is not None for item in controls),
            "client_certificate_control_readonly": any(self._safe_attribute(item, "readonly") is not None for item in controls),
            "client_certificate_control_resolution_method": resolution,
            "client_certificate_unconfigured_state_detected": not values if edit_state else False,
            "client_certificate_existing_value_detected": bool(values) if edit_state else False,
        }

    def _scan_client_certificate_reference_text(self, panel) -> dict[str, object]:
        targets = {
            "default": ("クライアント証明書(デフォルト)", "クライアント証明書（デフォルト）"),
            "unconfigured": ("(設定なし)", "（設定なし）"),
        }
        elements = []
        try:
            elements = self._safe_find_elements_from(panel, By.CSS_SELECTOR, "*")
        except Exception:
            elements = []
        if not elements:
            elements = [panel]
        visible = []
        for element in elements:
            if self._safe_bool(element, "is_displayed") and str(self._safe_attribute(element, "aria-hidden") or "").casefold() != "true":
                visible.append(element)
        heading_matches = [element for element in visible if self._normalize_reference_text(self._safe_element_text_for_diagnostic(element)) == "クライアント証明書"]
        result = {
            "client_certificate_reference_text_node_scan_called": True,
            "client_certificate_reference_descendant_element_count": len(visible),
        }
        for name, allowed_values in targets.items():
            matches = [element for element in visible if self._normalize_reference_text(self._safe_element_text_for_diagnostic(element)) in allowed_values]
            if not matches and any(target in self._normalize_reference_text(self._safe_element_text_for_diagnostic(panel)) for target in allowed_values):
                matches = [panel]
            deduplicated = []
            for element in matches:
                if any(self._is_ancestor_element(element, existing) for existing in deduplicated):
                    continue
                deduplicated = [existing for existing in deduplicated if not self._is_ancestor_element(existing, element)]
                deduplicated.append(element)
            prefix = "client_certificate_default_label" if name == "default" else "client_certificate_unconfigured_text"
            result[f"{prefix}_raw_match_count"] = len(matches)
            result[f"{prefix}_leaf_match_count"] = len([element for element in matches if not self._safe_find_elements_from(element, By.CSS_SELECTOR, "*")])
            result[f"{prefix}_direct_text_match_count"] = len(matches)
            result[f"{prefix}_normalized_match_count"] = len(matches)
            result[f"{prefix}_parent_match_count"] = len(matches)
            result[f"{prefix}_deduplicated_candidate_count"] = len(deduplicated)
            result[f"{prefix}_unique"] = len(deduplicated) == 1
            result[f"{prefix}_resolution_method"] = "leaf_text" if len(deduplicated) == 1 and deduplicated[0] is not panel else "normalized_text_content" if len(deduplicated) == 1 else "unresolved"
            variants = {"ascii_parentheses" if "(" in self._normalize_reference_text(self._safe_element_text_for_diagnostic(element)) else "fullwidth_parentheses" for element in deduplicated}
            result[f"client_certificate_{prefix.split('client_certificate_')[-1]}_variant"] = next(iter(variants)) if len(variants) == 1 else "unresolved"
        result["client_certificate_reference_default_label_exact_match"] = result["client_certificate_default_label_unique"]
        result["client_certificate_reference_unconfigured_text_exact_match"] = result["client_certificate_unconfigured_text_unique"]
        result["heading_exact"] = len(heading_matches) == 1
        result["client_certificate_reference_heading_candidate_count"] = len(heading_matches)
        result["client_certificate_reference_heading_exact_match"] = result["heading_exact"]
        return result

    @staticmethod
    def _normalize_reference_text(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "").replace("\u00a0", " ")).strip()

    def _group_certificate_controls(self, controls):
        groups = []
        for control in controls:
            relation = (
                self._safe_attribute(control, "aria-controls"),
                self._safe_attribute(control, "aria-labelledby"),
                self._safe_attribute(control, "name"),
            )
            parent = None
            try:
                parent = control.find_element(By.XPATH, "./..")
            except Exception:
                pass
            found = None
            for group in groups:
                if parent is not None and group[1] is parent:
                    found = group
                    break
                if any(value and value in group[2] for value in relation):
                    found = group
                    break
            if found is None:
                groups.append([control, parent, set(value for value in relation if value)])
            else:
                found[2].update(value for value in relation if value)
        return groups

    def _find_panel_clickables(self, panel, names: tuple[str, ...]):
        if panel is None:
            return []
        expected = {self._normalize_navigation_name(name) for name in names}
        return [
            item for item in self._safe_find_elements_from(panel, By.CSS_SELECTOR, "a,button,[role='link'],[role='button']")
            if self._normalize_navigation_name(self._safe_element_text_for_diagnostic(item)) in expected
        ]

    def _scroll_detail_panel_for_other_settings(self, panel, target) -> dict[str, object]:
        result = {
            "device_detail_scroll_called": False, "device_detail_scroll_count": 0,
            "device_detail_scroll_target_resolved": target is not None,
            "device_detail_scroll_container_scrollable": False,
            "device_detail_scroll_position_before": 0, "device_detail_scroll_position_after": 0,
            "device_detail_scroll_position_changed": False,
        }
        if panel is None or target is None:
            return result
        candidates = [panel]
        try:
            candidates.extend(self.browser.driver.execute_script("return Array.from(arguments[0].querySelectorAll('*'));", panel))
        except Exception:
            pass
        scrollable = []
        for item in candidates:
            try:
                state = self.browser.driver.execute_script("return {scrollHeight: arguments[0].scrollHeight, clientHeight: arguments[0].clientHeight, scrollTop: arguments[0].scrollTop, overflowY: getComputedStyle(arguments[0]).overflowY};", item)
            except Exception:
                continue
            if isinstance(state, dict) and state.get("scrollHeight", 0) > state.get("clientHeight", 0) and state.get("overflowY") in {"auto", "scroll"}:
                scrollable.append((item, state))
        result["device_detail_scroll_container_scrollable"] = len(scrollable) == 1
        if len(scrollable) != 1:
            return result
        container, state = scrollable[0]
        result["device_detail_scroll_position_before"] = float(state.get("scrollTop", 0) or 0)
        try:
            self.browser.driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'}); arguments[1].scrollTop = arguments[1].scrollHeight;", target, container)
            result["device_detail_scroll_called"] = True
            result["device_detail_scroll_count"] = 1
            after = self.browser.driver.execute_script("return arguments[0].scrollTop;", container)
            result["device_detail_scroll_position_after"] = float(after or 0)
            result["device_detail_scroll_position_changed"] = result["device_detail_scroll_position_after"] != result["device_detail_scroll_position_before"]
        except Exception:
            return result
        return result

    def _wait_for_named_panel(self, names: tuple[str, ...], timeout: float) -> dict[str, object]:
        expected = {self._normalize_navigation_name(name) for name in names}
        client_certificate_panel = "クライアント証明書" in expected or "client certificate" in expected
        def locate(driver):
            panels = []
            for selector in ("aside", "[role='dialog']", "[role='complementary']", "[data-testid*='panel' i]", "[class*='panel' i]"):
                for panel in self._safe_find_driver_elements(driver, By.CSS_SELECTOR, selector):
                    if not self._safe_bool(panel, "is_displayed") or any(panel is item or panel == item for item in panels):
                        continue
                    text = self._safe_element_text_for_diagnostic(panel)
                    if any(name in self._normalize_navigation_name(text) for name in expected) and (not client_certificate_panel or self._is_minimal_client_certificate_panel(panel)):
                        panels.append(panel)
            deduplicated = []
            for panel in panels:
                if any(self._is_ancestor_element(panel, existing) for existing in deduplicated):
                    continue
                deduplicated = [existing for existing in deduplicated if not self._is_ancestor_element(existing, panel)]
                deduplicated.append(panel)
            return deduplicated
        try:
            panels = WebDriverWait(self.browser.driver, timeout, poll_frequency=0.25).until(locate)
        except TimeoutException:
            panels = []
        panel = panels[0] if len(panels) == 1 else None
        metrics = self._client_certificate_panel_metrics(panel) if client_certificate_panel and panel is not None else {}
        return {"candidate_count": len(panels), "unique": len(panels) == 1, "visible": len(panels) == 1 and self._safe_bool(panels[0], "is_displayed"), "panel": panel, **metrics}

    def _client_certificate_panel_metrics(self, panel) -> dict[str, object]:
        controls = self._safe_find_elements_from(panel, By.CSS_SELECTOR, "a,button,[role='link'],[role='button']")
        names = {self._normalize_navigation_name(self._safe_element_text_for_diagnostic(item)) for item in controls}
        contains_search_input = bool(self._safe_find_elements_from(panel, By.CSS_SELECTOR, "input[name*='query' i],input[aria-label*='search' i]"))
        contains_search_button = bool(self._safe_find_elements_from(panel, By.CSS_SELECTOR, "button[type='submit']"))
        contains_result_table = bool(self._safe_find_elements_from(panel, By.CSS_SELECTOR, "table"))
        right_side = False
        try:
            right_side = bool(self.browser.driver.execute_script("const r=arguments[0].getBoundingClientRect(); return r.left >= window.innerWidth * 0.45;", panel))
        except Exception:
            pass
        return {
            "client_certificate_panel_raw_candidate_count": 1,
            "client_certificate_panel_deduplicated_candidate_count": 1,
            "client_certificate_panel_nested_duplicate_count": 0,
            "client_certificate_panel_resolution_method": "minimal_landmark_container",
            "client_certificate_panel_contains_search_input": contains_search_input,
            "client_certificate_panel_contains_search_button": contains_search_button,
            "client_certificate_panel_contains_result_table": contains_result_table,
            "client_certificate_panel_right_side_verified": right_side,
            "client_certificate_panel_heading_exact_match": "クライアント証明書" in self._safe_element_text_for_diagnostic(panel).strip(),
            "client_certificate_panel_back_control_found": bool(names & {"戻る", "back"}),
            "client_certificate_panel_close_control_found": bool(names & {"閉じる", "close"}),
        }

    def _is_minimal_client_certificate_panel(self, panel) -> bool:
        text = self._normalize_navigation_name(self._safe_element_text_for_diagnostic(panel))
        if "クライアント証明書" not in text and "client certificate" not in text:
            return False
        if self._safe_find_elements_from(panel, By.CSS_SELECTOR, "input[name*='query' i],input[aria-label*='search' i],button[type='submit'],table"):
            return False
        controls = self._safe_find_elements_from(panel, By.CSS_SELECTOR, "a,button,[role='link'],[role='button']")
        names = {self._normalize_navigation_name(self._safe_element_text_for_diagnostic(item)) for item in controls}
        return bool(names & {"編集", "edit", "保存", "save", "取消", "cancel", "戻る", "back", "閉じる", "close"}) or "編集" in text or "edit" in text

    def _is_certificate_submit_control(self, element) -> bool:
        text = " ".join((self._safe_element_text_for_diagnostic(element), self._safe_attribute(element, "aria-label"), self._safe_attribute(element, "title"))).casefold()
        return any(token in text for token in ("upload", "add", "register", "save", "追加", "登録", "保存")) and not any(token in text for token in ("cancel", "close", "キャンセル", "閉じる"))

    def search_device(
        self,
        serial: str,
        trace=None,
        page_reached: bool = False,
        read_only_observation: bool = False,
    ) -> dict[str, object]:
        return self._search_device_identifier(
            serial,
            "serial",
            "端末検索",
            trace=trace,
            page_reached=page_reached,
            read_only_observation=read_only_observation,
        )

    def inspect_device_client_certificate_settings_dom_for_diagnostic(self, serial: str, trace=None, search_already_completed: bool = False) -> dict[str, object]:
        """Search one serial and inspect the client-certificate settings path without mutation."""
        started_at = time.monotonic()
        if not search_already_completed:
            self.search_device(serial, trace=trace, page_reached=True)
        selection = self.select_matched_device_row(serial, trace=trace)
        if not selection["device_result_selected"]:
            error = RuntimeError("端末検索結果の完全一致行を選択できません")
            error.failed_phase = "select_device_result"
            raise error
        detail_started_at = time.monotonic()
        selection["device_detail_navigation_wait_called"] = True
        self._trace(trace, "device_detail_navigation_wait_called", True)
        other_settings = self._wait_for_exact_clickable(("他の設定を見る", "Other settings"), timeout=10)
        selection["device_detail_navigation_verified"] = other_settings is not None
        self._trace(trace, "device_detail_navigation_verified", selection["device_detail_navigation_verified"])
        self._trace_elapsed(trace, "device_detail", detail_started_at)
        other_settings.click()
        self._trace(trace, "other_settings_click_called", True)
        other_started_at = time.monotonic()
        client_certificate = self._wait_for_exact_clickable(("クライアント証明書", "Client certificate"), timeout=10)
        self._trace_elapsed(trace, "other_settings", other_started_at)
        client_certificate.click()
        self._trace(trace, "device_client_certificate_click_called", True)
        certificate_started_at = time.monotonic()
        self.browser.wait_for_page_ready()
        observation = self._inspect_client_certificate_upload_dom()
        self._trace_elapsed(trace, "device_client_certificate", certificate_started_at)
        self._trace_elapsed(trace, "device_certificate_navigation_total", started_at)
        observation.update({"device_result_row_count": 1, "device_detail_click_count": 1, "other_settings_click_count": 1, "device_client_certificate_click_count": 1, **selection})
        return observation

    def select_matched_device_row(self, serial: str, trace=None) -> dict[str, object]:
        """Click the unique current result row and verify its detail panel context."""
        result = {
            "device_result_click_candidate_count": 0,
            "device_result_click_unique": False,
            "device_result_click_called": False,
            "device_result_click_count": 0,
            "device_result_selected": False,
            "device_detail_navigation_wait_called": False,
            "device_detail_navigation_verified": False,
            "device_result_candidate_count": 0,
            "device_result_candidate_unique": False,
            "device_result_detail_column_candidate_count": 0,
            "device_result_detail_control_candidate_count": 0,
            "device_result_detail_control_unique": False,
            "device_detail_serial_field_candidate_count": 0,
            "device_detail_serial_exact_match": False,
            "device_result_identity_verified": False,
            "device_result_identity_verification_method": "",
            "device_search_result_total_count": None,
            "device_search_result_page_count": None,
            "device_search_result_container_count": 0,
            "device_search_post_result_visible_row_count": 0,
            "device_search_input_exact_match": False,
            "device_search_identity_context_verified": False,
        }
        result.update({key: self.device_observation.get(key) for key in result if key in self.device_observation})
        structural_fallback = (
            result.get("device_search_result_total_count") is None
            and result.get("device_search_result_page_count") is None
            and result.get("device_search_result_structural_uniqueness_verified") is True
        )
        required_observation = (
            (result.get("device_search_result_total_count") == 1 and result.get("device_search_result_page_count") == 1) or structural_fallback,
            result.get("device_search_result_container_count") == 1,
            result.get("device_search_post_result_visible_row_count") == 1,
            result.get("device_result_candidate_count") == 1,
            result.get("device_result_candidate_unique") is True,
            result.get("device_search_input_exact_match") is True,
            result.get("device_search_identity_context_verified") is True,
            result.get("device_result_identity_verified") is False,
        )
        if not all(required_observation):
            self._trace_many(trace, result)
            return result
        snapshot = self._serial_search_results_dom_snapshot(self.browser.driver)
        if not snapshot["result_table_unique"] or not snapshot["result_rows_scoped_to_table"]:
            self._trace_many(trace, result)
            return result
        rows = [
            row for row in snapshot.get("result_rows") or []
            if self._safe_bool(row, "is_displayed") and self._safe_bool(row, "is_enabled")
        ]
        result["device_result_candidate_count"] = len(rows)
        if len(rows) != 1:
            self._trace_many(trace, result)
            return result
        result["device_result_click_candidate_count"] = 1
        result["device_result_click_unique"] = True
        self._trace_many(trace, result)
        try:
            rows[0].click()
        except Exception as exc:
            result["device_result_click_called"] = True
            result["device_result_click_count"] = 1
            result["device_detail_navigation_wait_called"] = False
            result["device_detail_navigation_exception_type"] = type(exc).__name__
            self._trace_many(trace, result)
            return result
        result["device_result_click_called"] = True
        result["device_result_click_count"] = 1
        result["device_detail_navigation_wait_called"] = True
        self._trace(trace, "device_detail_navigation_wait_called", True)
        verification = self._wait_for_device_detail_panel(timeout=10.0)
        result.update(verification)
        result["device_detail_navigation_verified"] = verification.get("device_detail_panel_unique") is True
        result["device_result_selected"] = result["device_detail_navigation_verified"] is True
        result["device_result_identity_verified"] = result["device_detail_navigation_verified"] is True
        result["device_result_identity_verification_method"] = "unique_serial_search_context_and_single_row_click" if result["device_result_identity_verified"] else ""
        self._trace_many(trace, result)
        return result

    def _find_detail_column_controls(self, cell):
        return [
            element for element in self._safe_find_elements_from(cell, By.CSS_SELECTOR, "a,button,[role='link'],[role='button']")
            if self._safe_bool(element, "is_displayed") and self._safe_bool(element, "is_enabled")
        ]

    def _wait_for_device_detail_panel(self, timeout: float = 10.0) -> dict[str, object]:
        candidate_stats = {"raw": 0, "nested": 0}
        def locate(driver):
            selectors = (
                "aside", "[role='complementary']", "[data-testid*='drawer' i]",
                "[data-testid*='panel' i]", "[class*='drawer' i]", "[class*='side-panel' i]",
                "[class*='detail-panel' i]", "[class*='panel' i]",
            )
            candidates = []
            for selector in selectors:
                for element in self._safe_find_driver_elements(driver, By.CSS_SELECTOR, selector):
                    if not self._safe_bool(element, "is_displayed"):
                        continue
                    if any(element is existing or element == existing for existing in candidates):
                        continue
                    text = self._safe_element_text_for_diagnostic(element).casefold()
                    clickables = self._safe_find_elements_from(element, By.CSS_SELECTOR, "a,button,[role='link'],[role='button']")
                    settings = [item for item in clickables if self._normalize_navigation_name(self._safe_element_text_for_diagnostic(item)) in {"他の設定を見る", "other settings"}]
                    close = [item for item in clickables if self._normalize_navigation_name(self._safe_element_text_for_diagnostic(item)) in {"閉じる", "close"}]
                    landmark_count = sum(token in text for token in ("管理情報の編集", "設定の割り当て", "設定テンプレートの割り当て"))
                    if len(settings) == 1 and close and landmark_count >= 2:
                        candidates.append(element)
            if not candidates:
                for setting in self._find_exact_clickables(driver, ("他の設定を見る", "Other settings")):
                    ancestor = setting
                    for _ in range(6):
                        try:
                            ancestor = ancestor.find_element(By.XPATH, "./..")
                        except Exception:
                            break
                        if not self._safe_bool(ancestor, "is_displayed"):
                            continue
                        text = self._safe_element_text_for_diagnostic(ancestor).casefold()
                        clickables = self._safe_find_elements_from(ancestor, By.CSS_SELECTOR, "a,button,[role='link'],[role='button']")
                        close = [item for item in clickables if self._normalize_navigation_name(self._safe_element_text_for_diagnostic(item)) in {"閉じる", "close"}]
                        landmark_count = sum(token in text for token in ("管理情報の編集", "設定の割り当て", "設定テンプレートの割り当て"))
                        if close and landmark_count >= 2 and not any(ancestor is item or ancestor == item for item in candidates):
                            candidates.append(ancestor)
            candidate_stats["raw"] = len(candidates)
            deduplicated = []
            nested_duplicates = 0
            for candidate in candidates:
                duplicate = False
                for existing in list(deduplicated):
                    if self._is_ancestor_element(existing, candidate):
                        deduplicated.remove(existing)
                        nested_duplicates += 1
                    elif self._is_ancestor_element(candidate, existing):
                        duplicate = True
                        nested_duplicates += 1
                        break
                if not duplicate:
                    deduplicated.append(candidate)
            candidate_stats["nested"] = nested_duplicates
            return deduplicated

        try:
            panels = WebDriverWait(self.browser.driver, timeout, poll_frequency=0.25).until(locate)
        except TimeoutException:
            panels = []
        panel = panels[0] if isinstance(panels, list) and len(panels) == 1 else None
        return {
            "device_detail_panel_candidate_count": len(panels) if isinstance(panels, list) else 0,
            "device_detail_panel_unique": isinstance(panels, list) and len(panels) == 1,
            "device_detail_panel_visible": isinstance(panels, list) and len(panels) == 1 and self._safe_bool(panels[0], "is_displayed"),
            "device_detail_panel": panel,
            "device_detail_panel_raw_candidate_count": candidate_stats["raw"],
            "device_detail_panel_deduplicated_candidate_count": len(panels) if isinstance(panels, list) else 0,
            "device_detail_panel_nested_duplicate_count": candidate_stats["nested"],
            "device_detail_panel_scroll_container_candidate_count": 0,
            "device_detail_panel_scroll_container_unique": False,
            "device_detail_panel_resolution_method": "panel_attribute_and_landmarks" if panel is not None else "unresolved",
        }

    def _is_ancestor_element(self, ancestor, descendant, max_depth: int = 10) -> bool:
        current = descendant
        for _ in range(max_depth):
            try:
                current = current.find_element(By.XPATH, "./..")
            except Exception:
                return False
            if current is ancestor or current == ancestor:
                return True
        return False

    def _wait_for_device_detail_serial(self, serial: str, timeout: float) -> dict[str, object]:
        target = str(serial or "").strip()

        def panel_candidates(driver):
            selectors = (
                "aside",
                "[role='complementary']",
                "[data-testid*='drawer' i]",
                "[data-testid*='panel' i]",
                "[class*='drawer' i]",
                "[class*='side-panel' i]",
                "[class*='detail-panel' i]",
                "[class*='panel' i]",
            )
            candidates = []
            for selector in selectors:
                for element in self._safe_find_driver_elements(driver, By.CSS_SELECTOR, selector):
                    if not self._safe_bool(element, "is_displayed"):
                        continue
                    if any(element is existing or element == existing for existing in candidates):
                        continue
                    text = self._safe_element_text_for_diagnostic(element).casefold()
                    if "他の設定を見る" in text or "other settings" in text:
                        candidates.append(element)
            return candidates

        try:
            panels = WebDriverWait(self.browser.driver, timeout, poll_frequency=0.25).until(panel_candidates)
        except TimeoutException:
            panels = []
        panel_result = {
            "device_detail_panel_candidate_count": len(panels) if isinstance(panels, list) else 0,
            "device_detail_panel_unique": isinstance(panels, list) and len(panels) == 1,
            "device_detail_panel_visible": isinstance(panels, list) and len(panels) == 1 and self._safe_bool(panels[0], "is_displayed"),
        }
        if not panel_result["device_detail_panel_unique"]:
            return {
                **panel_result,
                "device_detail_serial_field_candidate_count": 0,
                "device_detail_serial_value_candidate_count": 0,
                "device_detail_serial_exact_match": False,
            }
        panel = panels[0]

        def locate(_driver):
            fields = []
            for element in self._safe_find_elements_from(panel, By.CSS_SELECTOR, "[data-field*='serial' i],[data-testid*='serial' i],[name*='serial' i],[id*='serial' i],[aria-label*='serial' i],dt,th,label"):
                label = self._safe_element_text_for_diagnostic(element).strip().casefold()
                attributes = " ".join(str(self._safe_attribute(element, name) or "").casefold() for name in ("data-field", "data-testid", "name", "id", "aria-label"))
                if label in {"シリアル番号", "serial", "serial number"} or "serial" in attributes:
                    fields.append(element)
            return fields if fields else False

        def value_candidates(field):
            candidates = []
            candidates.extend(self._safe_find_elements_from(field, By.XPATH, "following-sibling::*[1]"))
            candidates.extend(self._safe_find_elements_from(field, By.CSS_SELECTOR, "input,textarea,[data-value],dd,span"))
            unique = []
            for candidate in candidates:
                if any(candidate is existing or candidate == existing for existing in unique):
                    continue
                value = str(self._safe_attribute(candidate, "value") or self._safe_element_text(candidate) or "").strip()
                if value:
                    unique.append(candidate)
            return unique

        try:
            fields = WebDriverWait(self.browser.driver, timeout, poll_frequency=0.25).until(locate)
        except TimeoutException:
            return {
                **panel_result,
                "device_detail_serial_field_candidate_count": 0,
                "device_detail_serial_value_candidate_count": 0,
                "device_detail_serial_exact_match": False,
            }
        if not fields:
            return {
                **panel_result,
                "device_detail_serial_field_candidate_count": 0,
                "device_detail_serial_value_candidate_count": 0,
                "device_detail_serial_exact_match": False,
            }
        if len(fields) != 1:
            return {
                **panel_result,
                "device_detail_serial_field_candidate_count": len(fields),
                "device_detail_serial_value_candidate_count": 0,
                "device_detail_serial_exact_match": False,
            }
        values = value_candidates(fields[0])
        exact = len(values) == 1 and (
            str(self._safe_attribute(values[0], "value") or self._safe_element_text(values[0]) or "").strip() == target
        )
        return {
            **panel_result,
            "device_detail_serial_field_candidate_count": 1,
            "device_detail_serial_value_candidate_count": len(values),
            "device_detail_serial_exact_match": exact,
        }

    def _find_detail_link_candidates(self, row):
        expected = {"詳細", "details"}
        result = []
        for element in self._safe_find_elements_from(row, By.CSS_SELECTOR, "a"):
            values = (
                self._safe_element_text_for_diagnostic(element),
                self._safe_attribute(element, "aria-label"),
                self._safe_attribute(element, "title"),
            )
            if any(str(value).strip().casefold() in expected for value in values if value is not None):
                if self._safe_bool(element, "is_displayed") and self._safe_bool(element, "is_enabled"):
                    result.append(element)
        return result

    def inspect_matched_device_result_links(self, serial: str, search_observation: dict[str, object]) -> dict[str, object]:
        """Inspect links in one matched result row without clicking or navigating."""
        result = {
            "device_result_link_inspection_called": True,
            "device_result_link_inspection_completed": False,
            "device_result_link_candidate_count": 0,
            "device_result_link_visible_count": 0,
            "device_result_link_enabled_count": 0,
            "device_result_link_inside_matched_row_count": 0,
            "device_result_link_detail_text_count": 0,
            "device_result_link_device_detail_path_count": 0,
            "device_result_link_unique_detail_candidate_count": 0,
            "device_result_link_click_called": False,
            "device_result_link_click_count": 0,
            "device_result_link_inspection_failed_phase": "validate_search_observation",
            "device_result_link_inspection_exception_type": "",
            "device_result_link_metadata": [],
        }
        if search_observation.get("device_search_read_only_observation") is True:
            required = (
                search_observation.get("device_search_submit_count") == 1,
                search_observation.get("device_search_wait_completed") is True,
                search_observation.get("device_search_result_container_count") == 1,
                search_observation.get("device_search_result_stable") is True,
                search_observation.get("device_result_candidate_unique") is True,
                search_observation.get("device_result_identity_verified") is False,
            )
        else:
            required = (
                search_observation.get("device_search_result_stable") is True,
                search_observation.get("device_search_result_container_count") == 1,
                search_observation.get("device_search_result_transition_verified") is True,
                search_observation.get("device_search_result_total_count") == 1,
                search_observation.get("device_search_post_result_visible_row_count") == 1,
                search_observation.get("device_result_candidate_unique") is True,
            )
        if not all(required):
            result["device_result_link_inspection_failed_phase"] = "validate_search_observation"
            return result
        try:
            snapshot = self._serial_search_results_dom_snapshot(self.browser.driver)
            if not snapshot.get("result_table_unique") or not snapshot.get("result_rows_scoped_to_table"):
                result["device_result_link_inspection_failed_phase"] = "scope_result_rows"
                return result
            matched_rows = [row for row in snapshot.get("result_rows") or [] if self._safe_bool(row, "is_displayed")]
            if len(matched_rows) != 1:
                result["device_result_link_inspection_failed_phase"] = "resolve_matched_row"
                return result
            headers = list(snapshot.get("result_headers") or [])
            detail_indices = [index for index, header in enumerate(headers) if str(header).strip().casefold() in {"詳細", "details"}]
            if len(detail_indices) != 1:
                result["device_result_link_inspection_failed_phase"] = "resolve_detail_column"
                return result
            cells = self._safe_find_elements_from(matched_rows[0], By.CSS_SELECTOR, "td")
            links = self._find_detail_column_controls(cells[detail_indices[0]]) if detail_indices[0] < len(cells) else []
            result["device_result_link_candidate_count"] = len(links)
            result["device_result_link_inside_matched_row_count"] = len(links)
            detail_candidates = 0
            for index, link in enumerate(links, start=1):
                displayed = self._safe_bool(link, "is_displayed")
                enabled = self._safe_bool(link, "is_enabled")
                text_class = self._classify_result_link_text(self._safe_element_text_for_diagnostic(link))
                aria_class = self._classify_result_link_text(self._safe_attribute(link, "aria-label") or "")
                title_class = self._classify_result_link_text(self._safe_attribute(link, "title") or "")
                href = self._safe_attribute(link, "href") or ""
                path_class = self._classify_result_link_path(href)
                if displayed:
                    result["device_result_link_visible_count"] += 1
                if enabled:
                    result["device_result_link_enabled_count"] += 1
                if text_class == "details":
                    result["device_result_link_detail_text_count"] += 1
                if path_class == "device_detail":
                    result["device_result_link_device_detail_path_count"] += 1
                if displayed and enabled and (path_class == "device_detail" or "details" in {text_class, aria_class, title_class}):
                    detail_candidates += 1
                result["device_result_link_metadata"].append({
                    "tag_name": str(getattr(link, "tag_name", "") or "").casefold(),
                    "text_class": text_class,
                    "aria_label_present": bool(self._safe_attribute(link, "aria-label")),
                    "title_present": bool(self._safe_attribute(link, "title")),
                    "href_present": bool(href),
                    "href_path_class": path_class,
                    "role": self._safe_attribute(link, "role") or "",
                    "displayed": displayed,
                    "enabled": enabled,
                    "inside_matched_row": True,
                    "row_link_order": index,
                })
            result["device_result_link_unique_detail_candidate_count"] = detail_candidates
            result["device_result_link_inspection_failed_phase"] = "completed"
            result["device_result_link_inspection_completed"] = True
            return result
        except Exception as exc:
            result["device_result_link_inspection_exception_type"] = type(exc).__name__
            result["device_result_link_inspection_failed_phase"] = "inspect_result_links"
            return result

    @staticmethod
    def _classify_result_link_text(text: str) -> str:
        normalized = str(text or "").strip().casefold()
        if not normalized:
            return "blank"
        if normalized in {"詳細", "details"}:
            return "details"
        if normalized in {"設定", "settings"}:
            return "settings"
        if normalized in {"編集", "edit"}:
            return "edit"
        if normalized in {"証明書", "certificate"}:
            return "certificate"
        if normalized in {"端末名", "device name", "device_name"}:
            return "device_name"
        return "other"

    @staticmethod
    def _classify_result_link_path(href: str) -> str:
        value = str(href or "")
        if not value:
            return "empty"
        if value.casefold().startswith("javascript:"):
            return "javascript"
        return "other"

    def _find_exact_clickables(self, root, expected_names: tuple[str, ...]):
        expected = {self._normalize_navigation_name(value) for value in expected_names}
        result = []
        for element in self._safe_find_elements_from(root, By.CSS_SELECTOR, "button,a,[role='button'],[role='link']"):
            values = [self._safe_element_text_for_diagnostic(element), self._safe_attribute(element, "aria-label"), self._safe_attribute(element, "title")]
            if any(self._normalize_navigation_name(value) in expected for value in values if isinstance(value, str)) and self._safe_bool(element, "is_displayed") and self._safe_bool(element, "is_enabled"):
                result.append(element)
        return result

    def _wait_for_exact_clickable(self, expected_names: tuple[str, ...], timeout: float):
        driver = self.browser.driver
        if driver is None:
            raise RuntimeError("SMSM画面を確認できません")
        def locate(_driver):
            elements = self._find_exact_clickables(driver, expected_names)
            return elements[0] if len(elements) == 1 else False
        try:
            return WebDriverWait(driver, timeout, poll_frequency=0.25).until(locate)
        except TimeoutException as exc:
            raise RuntimeError("SMSM導線のランドマークを一意に確認できません") from exc

    def search_device_by_imei(self, imei: str) -> None:
        self._search_device_identifier(imei, "imei", "IMEI検索")

    def _search_device_identifier(
        self,
        value: str,
        field_name: str,
        operation: str,
        trace=None,
        page_reached: bool = False,
        read_only_observation: bool = False,
    ) -> dict[str, object]:
        failed_phase = "validate_device_search_context"
        observation = {
            "device_search_called": True,
            "device_search_target_present": bool(value),
            "device_search_page_verified": False,
            "device_search_type_selection_called": False,
            "device_search_type_already_selected": False,
            "device_search_type_click_count": 0,
            "device_search_dom_reobserve_called": False,
            "device_search_dom_reobserve_completed": False,
            "device_search_type_control_candidate_count": 0,
            "device_search_type_option_candidate_count": 0,
            "device_search_type_target_option_found": False,
            "device_search_type_control_displayed": False,
            "device_search_type_control_enabled": False,
            "device_search_input_candidate_count": 0,
            "device_search_button_candidate_count": 0,
            "device_search_send_keys_called": False,
            "device_search_send_keys_count": 0,
            "device_search_submit_called": False,
            "device_search_submit_count": 0,
            "device_search_wait_called": False,
            "device_search_wait_completed": False,
            "device_search_exact_match_count": None,
            "device_search_result_observation_called": False,
            "device_search_result_wait_called": False,
            "device_search_result_wait_completed": False,
            "device_search_result_container_count": 0,
            "device_search_result_row_candidate_count": 0,
            "device_search_visible_result_row_count": 0,
            "device_search_serial_column_candidate_count": None,
            "device_search_serial_column_unique": None,
            "device_search_serial_cell_candidate_count": 0,
            "device_search_serial_cell_nonblank_count": 0,
            "device_search_zero_result_indicator_found": False,
            "device_search_result_collection_method": "unresolved",
            "device_search_result_stable": False,
            "device_search_count_failed_phase": "",
            "device_search_count_exception_type": "",
            "device_search_failed_phase": failed_phase,
            "device_search_exception_type": "",
            "device_search_read_only_observation": read_only_observation,
        }
        self._trace_many(trace, observation)
        if not value:
            error = ValueError(f"{field_name}が空です")
            error.failed_phase = failed_phase
            observation["device_search_exception_type"] = type(error).__name__
            self._trace_many(trace, observation)
            raise error

        self.logger.info(f"{operation}を開始")
        for key in ("smsm_device_page_reached", "smsm_find_search_type_control", "smsm_open_search_type_control", "smsm_find_serial_option", "smsm_select_serial_option", "smsm_validate_serial_selection", "smsm_find_serial_input", "smsm_fill_serial_input", "smsm_validate_serial_input", "smsm_find_search_button", "smsm_submit_search", "smsm_wait_search_results", "smsm_count_search_results"):
            self._trace(trace, key, False)

        try:
            selection = {}
            if not page_reached:
                failed_phase = "verify_device_search_page"
                self._trace(trace, "smsm_device_page_reached", True)
                navigation_method = self.reach_device_search_page
                if "trace" in inspect.signature(navigation_method).parameters:
                    page_result = navigation_method(trace=trace)
                else:
                    page_result = navigation_method()
                if not isinstance(page_result, dict) or page_result.get("device_list_page_verified") is not True:
                    raise RuntimeError("端末検索ページを確認できません")
            observation["device_search_page_verified"] = True
            self._trace(trace, "device_search_page_verified", True)

            if field_name == "serial":
                failed_phase = "resolve_device_search_type"
                self._trace(trace, "smsm_find_search_type_control", True)
                self._trace(trace, "smsm_open_search_type_control", True)
                self._trace(trace, "smsm_find_serial_option", True)
                observation["device_search_type_selection_called"] = True
                try:
                    selection = self._select_serial_search_type() or {}
                except Exception as exc:
                    selection_observation = getattr(exc, "observation", {})
                    if isinstance(selection_observation, dict):
                        observation.update(selection_observation)
                    failed_phase = getattr(exc, "failed_phase", failed_phase)
                    raise
                observation.update({key: selection.get(key, observation[key]) for key in (
                    "device_search_type_control_candidate_count",
                    "device_search_type_option_candidate_count",
                    "device_search_type_target_option_found",
                    "device_search_type_control_displayed",
                    "device_search_type_control_enabled",
                )})
                observation["device_search_type_already_selected"] = selection.get("already_selected", False)
                observation["device_search_type_click_count"] = selection.get("click_count", 0)
                self._trace(trace, "smsm_select_serial_option", True)
                self._trace(trace, "smsm_validate_serial_selection", True)
                observation["device_search_dom_reobserve_called"] = True
                self._trace(trace, "device_search_dom_reobserve_called", True)
            failed_phase = "resolve_device_search_input"
            def current_search_controls(_driver):
                current_inputs = self._serial_input_candidates(self.browser.driver) if field_name == "serial" else []
                current_buttons = self._search_button_candidates()
                return (current_inputs, current_buttons) if len(current_inputs) == 1 and len(current_buttons) == 1 else False

            input_candidates = self._serial_input_candidates(self.browser.driver) if field_name == "serial" else []
            button_candidates = self._search_button_candidates()
            if selection.get("click_count") == 1 and (len(input_candidates) != 1 or len(button_candidates) != 1):
                failed_phase = "wait_device_search_controls_after_type_selection"
                try:
                    input_candidates, button_candidates = WebDriverWait(self.browser.driver, 10.0, poll_frequency=0.1).until(current_search_controls)
                except TimeoutException as exc:
                    error = RuntimeError("検索条件変更後の検索コントロール確認がタイムアウトしました")
                    error.failed_phase = failed_phase
                    raise error from exc
                observation["device_search_dom_reobserve_completed"] = True
                self._trace(trace, "device_search_dom_reobserve_completed", True)
            observation["device_search_input_candidate_count"] = len(input_candidates)
            self._trace(trace, "device_search_input_candidate_count", len(input_candidates))
            if len(input_candidates) != 1:
                raise RuntimeError(f"{field_name}検索入力欄を一意に確認できません")

            failed_phase = "resolve_device_search_button"
            safe_buttons = [button for button in button_candidates if self._search_button_is_safe(button)]
            observation["device_search_button_candidate_count"] = len(button_candidates)
            self._trace(trace, "device_search_button_candidate_count", len(button_candidates))
            if len(button_candidates) != 1 or len(safe_buttons) != 1:
                raise RuntimeError("検索ボタンを安全に一意特定できません")
            if field_name == "serial" and not observation["device_search_dom_reobserve_completed"]:
                observation["device_search_dom_reobserve_completed"] = True
                self._trace(trace, "device_search_dom_reobserve_completed", True)

            failed_phase = "set_device_search_serial"
            search_input = input_candidates[0]
            self._trace(trace, "smsm_find_serial_input", True)
            self._trace(trace, "smsm_fill_serial_input", True)
            search_input.clear()
            search_input.send_keys(value)
            observation["device_search_send_keys_called"] = True
            observation["device_search_send_keys_count"] = 1
            self._trace(trace, "device_search_send_keys_called", True)
            self._trace(trace, "device_search_send_keys_count", 1)
            self._trace(trace, "smsm_validate_serial_input", True)
            if not self._input_is_nonblank(search_input):
                raise RuntimeError(f"{field_name}検索値の入力を確認できません")
            if field_name == "serial":
                input_value = str(self._safe_attribute(search_input, "value") or "").strip()
                observation["device_search_input_exact_match"] = input_value == str(value).strip()
                observation["device_search_identity_context_verified"] = observation["device_search_input_exact_match"] is True

            failed_phase = "submit_device_search"
            self._trace(trace, "smsm_find_search_button", True)
            before_search_dom = None
            if field_name == "serial":
                before_search_dom = self._serial_search_results_dom_snapshot(self.browser.driver)
            safe_buttons[0].click()
            observation["device_search_submit_called"] = True
            observation["device_search_submit_count"] = 1
            self._trace(trace, "smsm_submit_search", True)
            self._trace(trace, "device_search_submit_called", True)
            self._trace(trace, "device_search_submit_count", 1)
            failed_phase = "wait_device_search_results"
            observation["device_search_wait_called"] = True
            self._trace(trace, "device_search_wait_called", True)
            self._trace(trace, "smsm_wait_search_results", True)
            self.browser.wait_for_page_ready()
            observation["device_search_wait_completed"] = True
            self._trace(trace, "device_search_wait_completed", True)
            failed_phase = "count_device_exact_matches"
            if field_name == "serial":
                observer = self._observe_serial_search_after_submit
                if "trace" in inspect.signature(observer).parameters:
                    metrics = observer(before_search_dom, value, trace=trace)
                else:
                    metrics = observer(before_search_dom, value)
                for key in (
                    "device_search_result_observation_called",
                    "device_search_result_wait_called",
                    "device_search_result_wait_completed",
                    "device_search_result_container_count",
                    "device_search_result_row_candidate_count",
                    "device_search_visible_result_row_count",
                    "device_search_serial_column_candidate_count",
                    "device_search_serial_column_unique",
                    "device_search_serial_cell_candidate_count",
                    "device_search_serial_cell_nonblank_count",
                    "device_search_zero_result_indicator_found",
                    "device_search_result_collection_method",
                    "device_search_result_stable",
                    "device_search_count_failed_phase",
                    "device_search_count_exception_type",
                    "device_search_result_transition_verified",
                    "device_search_total_count",
                    "device_search_result_total_count",
                    "device_search_page_count",
                    "device_search_result_page_count",
                    "device_search_post_result_visible_row_count",
                    "device_result_candidate_count",
                    "device_result_candidate_unique",
                    "device_result_identity_verified",
                ):
                    value_for_key = metrics.get(key, observation.get(key))
                    observation[key] = value_for_key
                    self._trace(trace, key, value_for_key)
                exact_count = metrics.get("device_search_exact_match_count", metrics.get("exact_match_count"))
            else:
                exact_count = 0
            observation["device_search_exact_match_count"] = exact_count
            self._trace(trace, "device_search_exact_match_count", exact_count)
            if read_only_observation:
                candidate_unique = (
                    observation.get("device_search_submit_count") == 1
                    and observation.get("device_search_wait_completed") is True
                    and observation.get("device_search_result_container_count") == 1
                    and observation.get("device_search_result_stable") is True
                    and observation.get("device_search_result_transition_verified") is True
                    and (
                        (
                            observation.get("device_search_result_total_count") == 1
                            and observation.get("device_search_result_page_count") == 1
                        )
                        or (
                            observation.get("device_search_result_total_count") is None
                            and observation.get("device_search_result_page_count") is None
                            and observation.get("device_search_result_structural_uniqueness_verified") is True
                        )
                    )
                    and observation.get("device_search_post_result_visible_row_count") == 1
                    and observation.get("device_result_candidate_count") == 1
                    and observation.get("device_search_post_result_visible_row_count") == 1
                )
                if not candidate_unique:
                    raise RuntimeError("読み取り専用検索結果候補を構造的に一意確認できません")
                observation["device_result_candidate_unique"] = True
                observation["device_result_identity_verified"] = False
                observation["device_search_failed_phase"] = "completed"
                self._trace_many(trace, observation)
                return observation
            if exact_count != 1:
                raise RuntimeError("検索後の端末シリアル完全一致件数が1件ではありません")
            observation["device_search_failed_phase"] = "completed"
            self._trace_many(trace, observation)
            return observation
        except Exception as exc:
            setattr(exc, "failed_phase", failed_phase)
            observation["device_search_failed_phase"] = failed_phase
            observation["device_search_exception_type"] = type(exc).__name__
            setattr(exc, "observation", dict(observation))
            self._trace_many(trace, observation)
            raise

    def reach_device_search_page(self, trace=None) -> dict[str, object]:
        result = {
            "device_list_navigation_called": True,
            "device_list_navigation_completed": False,
            "device_list_nav_candidate_count": 0,
            "device_list_nav_unique": False,
            "device_list_nav_click_called": False,
            "device_list_nav_click_count": 0,
            "device_list_pathname_matches": False,
            "device_list_search_input_candidate_count": 0,
            "device_list_search_button_candidate_count": 0,
            "device_list_main_container_visible": False,
            "device_list_condition_pathname_matches": False,
            "device_list_condition_search_input_unique": False,
            "device_list_condition_search_button_unique": False,
            "device_list_condition_main_container_visible": False,
            "device_list_page_verified": False,
            "device_list_failed_phase": "resolve_device_navigation",
            "device_list_exception_type": "",
        }
        self._trace_many(trace, result)
        locators = [
            (By.XPATH, "//a[contains(normalize-space(.), '端末') or contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'device')]"),
            (By.XPATH, "//button[contains(normalize-space(.), '端末') or contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'device')]"),
        ]
        driver = self.browser.driver
        if driver is None:
            error = RuntimeError("端末メニューを確認できません")
            error.failed_phase = "resolve_device_navigation"
            result["device_list_exception_type"] = type(error).__name__
            self._trace_many(trace, result)
            raise error

        try:
            candidates = []
            for by, value in locators:
                for element in self._safe_find_driver_elements(driver, by, value):
                    if self._safe_bool(element, "is_displayed") and self._safe_bool(element, "is_enabled") and not any(element is item for item in candidates):
                        candidates.append(element)
            result["device_list_nav_candidate_count"] = len(candidates)
            result["device_list_nav_unique"] = len(candidates) == 1
            self._trace_many(trace, result)

            current_path = urlparse(self._current_url()).path.rstrip("/") or "/"
            expected_paths = {
                self._element_pathname(element)
                for element in candidates
                if self._element_pathname(element)
            }
            active_candidates = [element for element in candidates if self._navigation_element_is_active(element, current_path)]
            if len(candidates) == 1 and not active_candidates:
                result["device_list_failed_phase"] = "click_device_navigation"
                candidates[0].click()
                result["device_list_nav_click_called"] = True
                result["device_list_nav_click_count"] = 1
            elif len(candidates) > 1 and not active_candidates:
                raise RuntimeError("端末メニューを安全に一意特定できません")

            result["device_list_failed_phase"] = "wait_device_list_page"
            deadline = time.monotonic() + 30.0
            while time.monotonic() <= deadline:
                current_path = urlparse(self._current_url()).path.rstrip("/") or "/"
                if current_path in expected_paths or (not expected_paths and current_path != "/"):
                    result["device_list_pathname_matches"] = True
                    break
                time.sleep(0.2)

            result["device_list_failed_phase"] = "verify_device_list_pathname"
            input_candidates = self._serial_input_candidates(driver)
            button_candidates = self._search_button_candidates()
            result["device_list_search_input_candidate_count"] = len(input_candidates)
            result["device_list_search_button_candidate_count"] = len(button_candidates)
            result["device_list_main_container_visible"] = self._device_list_main_container_visible(driver)
            result["device_list_condition_pathname_matches"] = result["device_list_pathname_matches"] is True
            result["device_list_condition_search_input_unique"] = len(input_candidates) == 1
            result["device_list_condition_search_button_unique"] = len(button_candidates) == 1
            result["device_list_condition_main_container_visible"] = result["device_list_main_container_visible"] is True
            result["device_list_page_verified"] = (
                result["device_list_condition_pathname_matches"] is True
                and result["device_list_condition_search_input_unique"] is True
                and result["device_list_condition_search_button_unique"] is True
            )
            if not result["device_list_page_verified"]:
                if not result["device_list_condition_pathname_matches"]:
                    result["device_list_failed_phase"] = "verify_device_list_pathname"
                elif not result["device_list_condition_search_input_unique"]:
                    result["device_list_failed_phase"] = "resolve_device_search_input"
                else:
                    result["device_list_failed_phase"] = "resolve_device_search_button"
                raise RuntimeError("端末一覧画面の確認条件を満たせません")
            result["device_list_navigation_completed"] = True
            result["device_list_failed_phase"] = "completed"
            self._trace_many(trace, result)
            return result
        except Exception as exc:
            setattr(exc, "failed_phase", result["device_list_failed_phase"])
            result["device_list_exception_type"] = type(exc).__name__
            self._trace_many(trace, result)
            raise

    @staticmethod
    def _element_pathname(element) -> str:
        try:
            href = element.get_attribute("href") or ""
            return urlparse(href).path.rstrip("/") or "/"
        except Exception:
            return ""

    def _navigation_element_is_active(self, element, current_path: str) -> bool:
        try:
            aria_current = element.get_attribute("aria-current")
            aria_selected = element.get_attribute("aria-selected")
            class_name = element.get_attribute("class") or ""
            return bool(aria_current or aria_selected or re.search(r"(^|\s)(active|selected)(\s|$)", class_name, re.IGNORECASE) or self._element_pathname(element) == current_path)
        except Exception:
            return False

    def _device_list_main_container_visible(self, driver) -> bool:
        selectors = (
            "main",
            "[role='main']",
            "[data-testid*='device' i]",
            "[data-testid*='terminal' i]",
            "section",
        )
        return any(
            self._safe_bool(element, "is_displayed")
            for selector in selectors
            for element in self._safe_find_driver_elements(driver, By.CSS_SELECTOR, selector)
        )

    def _trace_many(self, trace, values: dict[str, object]) -> None:
        for key, value in values.items():
            self._trace(trace, key, value)

    def inspect_serial_search_dom(self, trace=None) -> tuple[dict[str, object], list[dict[str, object]]]:
        driver = self.browser.driver
        if driver is None:
            raise RuntimeError("SMSM端末検索DOMを確認できません")
        observation = self.enumerate_search_controls(driver)
        for key in ("top_document_select_count", "iframe_count", "iframe_with_select_count", "native_select_count", "visible_native_select_count", "enabled_native_select_count", "custom_select_candidate_count", "stale_retry_count", "search_type_control_count", "search_type_control_unique"):
            self._trace(trace, key, observation[key])
        self._trace(trace, "native_select_detected", observation["native_select_count"] > 0)
        self._trace(trace, "native_select_displayed", observation["visible_native_select_count"] > 0)
        self._trace(trace, "custom_select_detected", observation["custom_select_candidate_count"] > 0)
        self._trace(trace, "select_backed_custom_ui_detected", observation["select_backed_custom_ui_detected"])
        if observation["search_type_control_count"] != 1 or not observation["search_type_control_unique"]:
            raise RuntimeError("シリアル番号検索条件を一意に確認できません")
        return observation["summary"], observation["schema"]

    def inspect_device_list_page(self, trace=None) -> dict[str, object]:
        """Verify the current device-list DOM without navigation or mutation."""
        current_path = urlparse(self._current_url()).path.rstrip("/") or "/"
        input_count = len(self._serial_input_candidates(self.browser.driver))
        button_count = len(self._search_button_candidates())
        container_visible = self._device_list_main_container_visible(self.browser.driver)
        result = {
            "device_list_navigation_called": False,
            "device_list_navigation_completed": False,
            "device_list_nav_candidate_count": 0,
            "device_list_nav_unique": False,
            "device_list_nav_click_called": False,
            "device_list_nav_click_count": 0,
            "device_list_pathname_matches": current_path != "/",
            "device_list_search_input_candidate_count": input_count,
            "device_list_search_button_candidate_count": button_count,
            "device_list_main_container_visible": container_visible,
            "device_list_condition_pathname_matches": current_path != "/",
            "device_list_condition_search_input_unique": input_count == 1,
            "device_list_condition_search_button_unique": button_count == 1,
            "device_list_condition_main_container_visible": container_visible,
            "device_list_page_verified": current_path != "/" and input_count == 1 and button_count == 1,
            "device_list_failed_phase": "completed",
            "device_list_exception_type": "",
        }
        if not result["device_list_page_verified"]:
            result["device_list_failed_phase"] = "verify_device_list_pathname" if current_path == "/" else "resolve_device_search_input" if input_count != 1 else "resolve_device_search_button"
            error = RuntimeError("現在の端末一覧DOMを確認できません")
            error.failed_phase = result["device_list_failed_phase"]
            result["device_list_exception_type"] = type(error).__name__
            self._trace_many(trace, result)
            raise error
        result["device_list_navigation_completed"] = True
        self._trace_many(trace, result)
        return result

    def inspect_custom_search_control_dom(self, trace=None) -> dict[str, object]:
        observation = self.wait_for_device_page_detailed_stable(trace=trace)
        self._trace(trace, "smsm_detect_hidden_native_select", True)
        self._trace(trace, "smsm_detect_custom_select_control", observation["custom_select_candidate_count"] > 0)
        if observation["custom_select_candidate_count"] == 0:
            raise RuntimeError("custom select候補を確認できません")
        self._trace(trace, "smsm_inspect_custom_select_control", True)
        self._trace(trace, "smsm_validate_select_backing_relation", True)
        self._trace(trace, "smsm_find_custom_serial_option", False)
        self._trace(trace, "smsm_select_custom_serial_option", False)
        return observation

    def inspect_serial_input_dom(self, trace=None) -> dict[str, object]:
        for key in (
            "custom_control_click_called", "listbox_visible", "serial_option_click_called",
            "serial_selection_verified", "send_keys_called", "search_button_click_called",
        ):
            self._trace(trace, key, False)
        stage_started_at = time.monotonic()
        observation = self.wait_for_device_page_stable(trace=trace)
        self._trace_elapsed(trace, "smsm_wait_device_page_stable", stage_started_at)
        self._trace(trace, "smsm_find_custom_search_control", True)
        stage_started_at = time.monotonic()
        try:
            if (
                observation["custom_select_candidate_count"] != 1
                or not observation["custom_select_unique"]
                or not observation["select_backed_custom_ui_verified"]
            ):
                raise RuntimeError("custom検索条件コントロールを一意に確認できません")
        finally:
            self._trace_elapsed(trace, "smsm_find_custom_control", stage_started_at)

        stage_started_at = time.monotonic()
        driver = self.browser.driver
        controls = self._safe_find_driver_elements(driver, By.CSS_SELECTOR, "[role='combobox'], [aria-haspopup='listbox']")
        try:
            if len(controls) != 1:
                raise RuntimeError("custom検索条件コントロールを操作可能と確認できません")
            control = controls[0]
        finally:
            self._trace_elapsed(trace, "smsm_reacquire_custom_control", stage_started_at)

        stage_started_at = time.monotonic()
        try:
            if not self._safe_bool(control, "is_displayed"):
                raise RuntimeError("custom検索条件コントロールを表示中と確認できません")
        finally:
            self._trace_elapsed(trace, "smsm_check_custom_control_displayed", stage_started_at)

        stage_started_at = time.monotonic()
        try:
            if not self._safe_bool(control, "is_enabled"):
                raise RuntimeError("custom検索条件コントロールを有効と確認できません")
        finally:
            self._trace_elapsed(trace, "smsm_check_custom_control_enabled", stage_started_at)

        before = self._selection_state(driver, control)
        input_count_before = self._safe_dynamic_input_count(driver)

        self._trace(trace, "smsm_open_custom_search_control", True)
        stage_started_at = time.monotonic()
        try:
            control.click()
        finally:
            self._trace_elapsed(trace, "smsm_open_custom_control", stage_started_at)
        self._trace(trace, "custom_control_click_called", True)
        stage_started_at = time.monotonic()
        try:
            listbox = self._wait_for_visible_listbox(driver, timeout=15.0, trace=trace)
        finally:
            self._trace_elapsed(trace, "smsm_wait_listbox", stage_started_at)
        self._trace(trace, "smsm_wait_custom_listbox", True)
        self._trace(trace, "listbox_visible", listbox is not None)
        if listbox is None:
            raise RuntimeError("custom listboxを確認できません")

        self._trace(trace, "smsm_find_custom_serial_option", True)
        stage_started_at = time.monotonic()
        try:
            candidates = self._safe_find_elements_from(listbox, By.CSS_SELECTOR, "[role='option'], option, [data-value], [data-option]")
            serial_candidates = []
            for candidate in candidates:
                try:
                    if candidate.text == "シリアル番号":
                        serial_candidates.append(candidate)
                except Exception:
                    continue
            self._trace(trace, "option_candidate_count", len(candidates))
            self._trace(trace, "serial_option_candidate_count", len(serial_candidates))
            self._trace(trace, "serial_option_unique", len(serial_candidates) == 1)
            if len(serial_candidates) != 1:
                raise RuntimeError("シリアル番号optionを一意に確認できません")
        finally:
            self._trace_elapsed(trace, "smsm_find_serial_option", stage_started_at)

        self._trace(trace, "smsm_select_custom_serial_option", True)
        stage_started_at = time.monotonic()
        try:
            serial_candidates[0].click()
        finally:
            self._trace_elapsed(trace, "smsm_click_serial_option", stage_started_at)
        self._trace(trace, "serial_option_click_called", True)
        self._trace(trace, "smsm_validate_custom_serial_selection", True)
        stage_started_at = time.monotonic()
        try:
            after = self._wait_for_serial_selection_state(driver, control, before, trace=trace)
        finally:
            self._trace_elapsed(trace, "smsm_validate_serial_selection", stage_started_at)
        selection_verified = self._selection_verified(before, after)
        self._trace(trace, "serial_selection_verified", selection_verified)
        if not selection_verified:
            raise RuntimeError("シリアル番号選択状態を確認できません")

        self._trace(trace, "smsm_wait_serial_input_dom", True)
        stage_started_at = time.monotonic()
        try:
            input_elements = self._wait_for_serial_input_elements(driver, timeout=15.0, trace=trace)
        finally:
            self._trace_elapsed(trace, "smsm_wait_serial_input", stage_started_at)
        self._trace(trace, "smsm_inspect_serial_input_dom", True)
        input_count_after = self._safe_dynamic_input_count(driver)
        schema = [self._safe_input_dom_schema(index, element) for index, element in enumerate(input_elements)]
        if len(input_elements) != 1:
            raise RuntimeError("シリアル番号入力欄を一意に確認できません")
        return {
            "observation": observation,
            "custom_select_candidate_count": 1,
            "custom_select_unique": True,
            "select_backed_custom_ui_verified": True,
            "listbox_visible": True,
            "option_candidate_count": len(candidates),
            "serial_option_candidate_count": 1,
            "serial_option_unique": True,
            "serial_selection_verified": True,
            "input_count_before_selection": input_count_before,
            "input_count_after_selection": input_count_after,
            "serial_input_candidate_count": len(input_elements),
            "serial_input_unique": len(input_elements) == 1,
            "schema": schema,
        }

    def fill_serial_input_for_diagnostic(self, serial: str, trace=None) -> SerialInputDiagnosticResult:
        result: SerialInputDiagnosticResult = {
            "serial_input_candidate_count": 0,
            "serial_input_unique": False,
            "serial_input_clear_called": False,
            "serial_input_send_keys_called": False,
            "serial_input_nonblank": False,
            "serial_input_exact_match": False,
            "serial_input_length_match": False,
            "serial_input_was_truncated": False,
            "serial_input_was_transformed": False,
            "serial_mapping_valid": False,
            "search_button_click_called": False,
            "smsm_update_called": False,
            "excel_write_called": False,
        }
        elements = self._serial_input_candidates(self.browser.driver)
        result["serial_input_candidate_count"] = len(elements)
        result["serial_input_unique"] = len(elements) == 1
        self._trace(trace, "serial_input_candidate_count", len(elements))
        self._trace(trace, "serial_input_unique", len(elements) == 1)
        if len(elements) != 1:
            return result
        element = elements[0]
        input_type = (self._safe_attribute(element, "type") or "text").casefold()
        valid = (
            self._safe_bool(element, "is_displayed")
            and self._safe_bool(element, "is_enabled")
            and self._safe_attribute(element, "readonly") is None
            and self._safe_attribute(element, "disabled") is None
            and (self._safe_attribute(element, "id") or "") != "manual_page_input_assets"
            and input_type not in {"checkbox", "radio", "hidden", "submit", "button"}
        )
        if not valid:
            return result
        self._trace(trace, "smsm_clear_serial_input", True)
        element.clear()
        result["serial_input_clear_called"] = True
        self._trace(trace, "serial_input_clear_called", True)
        self._trace(trace, "smsm_fill_serial_input", True)
        element.send_keys(serial)
        result["serial_input_send_keys_called"] = True
        self._trace(trace, "serial_input_send_keys_called", True)
        actual = self._input_value(element)
        exact = actual == serial
        result.update({
            "serial_input_nonblank": bool(actual),
            "serial_input_exact_match": exact,
            "serial_input_length_match": bool(actual) and len(actual) == len(serial),
            "serial_input_was_truncated": bool(actual) and len(actual) < len(serial) and not exact,
            "serial_input_was_transformed": bool(actual) and not exact,
            "serial_mapping_valid": bool(actual) and exact and len(actual) == len(serial),
        })
        for key, value in result.items():
            if key not in {"search_button_click_called", "smsm_update_called", "excel_write_called"}:
                self._trace(trace, key, value)
        return result

    def search_serial_results_for_diagnostic(self, trace=None) -> int:
        """Submit the already validated serial search and return only its row count."""
        for key in (
            "search_button_candidate_count", "search_button_unique", "search_button_safe",
            "search_button_click_called", "search_submitted", "lookup_called",
            "lookup_results_ready", "lookup_result_count", "lookup_unique",
        ):
            self._trace(trace, key, False if key != "lookup_result_count" else 0)

        self._trace(trace, "smsm_find_search_button", True)
        buttons = self._search_button_candidates()
        unique = len(buttons) == 1
        safe = unique and self._search_button_is_safe(buttons[0])
        self._trace(trace, "search_button_candidate_count", len(buttons))
        self._trace(trace, "search_button_unique", unique)
        self._trace(trace, "smsm_validate_search_button", True)
        self._trace(trace, "search_button_safe", safe)
        if not safe:
            raise RuntimeError("検索ボタンを安全に一意特定できません")

        before_search_state = self._serial_search_result_state(self.browser.driver)
        self._trace(trace, "smsm_submit_search", True)
        buttons[0].click()
        self._trace(trace, "search_button_click_called", True)
        self._trace(trace, "search_submitted", True)
        self._trace(trace, "lookup_called", True)

        self._trace(trace, "smsm_wait_search_results", True)
        result_count = self._wait_for_serial_search_results(timeout=30.0, before=before_search_state)
        self._trace(trace, "lookup_results_ready", True)
        self._trace(trace, "lookup_result_count", result_count)
        self._trace(trace, "lookup_unique", result_count == 1)
        self._trace(trace, "smsm_count_search_results", True)
        return result_count

    def inspect_serial_search_results_dom_for_diagnostic(self, trace=None) -> dict[str, object]:
        """Submit once and inspect only safe, structure-level result DOM attributes."""
        before = self._serial_search_results_dom_snapshot(self.browser.driver)
        for prefix in ("pre_search",):
            for key in ("result_table_count", "tbody_count", "visible_row_count", "checkbox_row_count", "empty_state_count", "loading_count", "pagination_count"):
                self._trace(trace, f"{prefix}_{key}", before[key])
        buttons = self._search_button_candidates()
        unique = len(buttons) == 1
        safe = unique and self._search_button_is_safe(buttons[0])
        self._trace(trace, "search_button_candidate_count", len(buttons))
        self._trace(trace, "search_button_unique", unique)
        self._trace(trace, "search_button_safe", safe)
        self._trace(trace, "smsm_validate_search_button", True)
        if not safe:
            raise RuntimeError("検索ボタンを安全に一意特定できません")
        buttons[0].click()
        self._trace(trace, "search_button_click_called", True)
        self._trace(trace, "search_submitted", True)
        self._trace(trace, "lookup_called", True)
        self._trace(trace, "smsm_wait_search_results", True)
        after = self._wait_for_serial_search_results_dom(timeout=30.0, before=before)
        for key in ("result_table_count", "tbody_count", "visible_row_count"):
            self._trace(trace, f"post_search_{key}", after[key])
        for key in ("result_dom_changed", "result_table_unique", "result_rows_scoped_to_table"):
            self._trace(trace, key, after[key])
        self._trace(trace, "lookup_results_ready", True)
        self._trace(trace, "lookup_result_count", after["result_count"])
        self._trace(trace, "lookup_unique", after["result_count"] == 1)
        for key in ("result_table_count", "tbody_count", "visible_row_count", "checkbox_row_count", "empty_state_count", "loading_count", "pagination_count"):
            after[f"pre_search_{key}"] = before[key]
        return after

    def match_serial_search_results_for_diagnostic(self, target: dict[str, object], trace=None) -> dict[str, object]:
        self._trace(trace, "smsm_result_match_target_loaded", True)
        if not isinstance(target, dict) or not all(key in target for key in ("alias", "serial", "imei")):
            raise RuntimeError("照合対象の項目を確認できません")
        self._trace(trace, "smsm_result_match_target_validated", True)
        observation = self.inspect_serial_search_results_dom_for_diagnostic(trace)
        self._trace(trace, "smsm_serial_search_completed", True)
        self._trace(trace, "smsm_find_result_table", True)
        if not observation["result_table_unique"] or not observation["result_rows_scoped_to_table"]:
            return {"result_match_unresolved": True, "matched_result_count": 0, **self._empty_result_match_summary(observation)}
        if observation["result_count"] == 0 and observation["empty_state_count"] > 0 and observation["loading_count"] == 0:
            return {"result_match_unresolved": False, "matched_result_count": 0, **self._empty_result_match_summary(observation)}

        self._trace(trace, "smsm_inspect_result_headers", True)
        headers = observation.get("result_headers", [])
        header_map = self._map_result_columns(headers)
        self._trace(trace, "smsm_map_result_columns", True)
        for key, value in header_map.items():
            self._trace(trace, key, value)
        if not header_map["serial_column_unique"]:
            return {"result_match_unresolved": True, "matched_result_count": 0, **self._empty_result_match_summary(observation, header_map)}
        self._trace(trace, "smsm_read_result_cells_in_memory", True)
        rows = observation.get("result_rows", [])
        excel_serial = str(target.get("serial") or "").strip()
        excel_alias = str(target.get("alias") or "").strip()
        excel_imei = normalize_imei(target.get("imei"))
        serial_matches = imei_matches = alias_matches = 0
        serial_imei_matches = serial_alias_matches = all_matches = 0
        for row in rows:
            cells = self._safe_find_elements_from(row, By.CSS_SELECTOR, "td")
            values = [self._safe_element_text(cell).strip() for cell in cells]
            serial_match = values[header_map["serial_column_index"]].strip() == excel_serial if header_map["serial_column_index"] is not None and header_map["serial_column_index"] < len(values) else False
            imei_match = False
            if header_map["imei_column_index"] is not None and header_map["imei_column_index"] < len(values):
                try:
                    imei_match = normalize_imei(values[header_map["imei_column_index"]]) == excel_imei
                except ValueError:
                    imei_match = False
            alias_match = header_map["alias_column_index"] is not None and header_map["alias_column_index"] < len(values) and values[header_map["alias_column_index"]].strip() == excel_alias
            serial_matches += int(serial_match)
            imei_matches += int(imei_match)
            alias_matches += int(alias_match)
            serial_imei_matches += int(serial_match and imei_match)
            serial_alias_matches += int(serial_match and alias_match)
            available_matches = serial_match
            if header_map["imei_column_unique"]:
                available_matches = available_matches and imei_match
            if header_map["alias_column_unique"]:
                available_matches = available_matches and alias_match
            all_matches += int(available_matches)
        self._trace(trace, "smsm_compare_result_rows", True)
        if header_map["imei_column_unique"]:
            matched_count = serial_imei_matches
        elif not header_map["imei_column_found"] and header_map["alias_column_unique"]:
            matched_count = serial_alias_matches
        else:
            matched_count = 0
        unresolved = (
            not header_map["serial_column_unique"]
            or (not header_map["imei_column_unique"] and header_map["imei_column_found"])
            or (not header_map["imei_column_found"] and not header_map["alias_column_unique"])
        )
        self._trace(trace, "smsm_resolve_unique_result", True)
        summary = {
            **header_map,
            "result_data_row_count": len(rows),
            "serial_match_count": serial_matches,
            "imei_match_count": imei_matches,
            "alias_match_count": alias_matches,
            "serial_and_imei_match_count": serial_imei_matches,
            "serial_and_alias_match_count": serial_alias_matches,
            "all_available_fields_match_count": all_matches,
            "matched_result_count": matched_count,
            "unique_result_match": not unresolved and matched_count == 1,
            "result_match_unresolved": unresolved,
        }
        self._trace(trace, "smsm_result_match_completed", True)
        for key in (
            "result_data_row_count", "serial_match_count", "imei_match_count", "alias_match_count",
            "serial_and_imei_match_count", "serial_and_alias_match_count", "all_available_fields_match_count",
            "matched_result_count", "unique_result_match", "result_match_unresolved",
        ):
            self._trace(trace, key, summary[key])
        return summary

    @staticmethod
    def _safe_element_text(element) -> str:
        try:
            return element.text or ""
        except Exception:
            return ""

    @staticmethod
    def _map_result_columns(headers: list[str]) -> dict[str, object]:
        exact = {
            "serial": {"シリアル番号", "Serial", "Serial Number"},
            "imei": {"IMEI"},
            "alias": {"端末名", "機器名", "エイリアス", "Alias"},
        }
        indices = {
            field: [index for index, value in enumerate(headers) if value in names]
            for field, names in exact.items()
        }
        return {
            "result_column_count": len(headers),
            "serial_column_found": bool(indices["serial"]),
            "serial_column_unique": len(indices["serial"]) == 1,
            "serial_column_index": indices["serial"][0] if len(indices["serial"]) == 1 else None,
            "imei_column_found": bool(indices["imei"]),
            "imei_column_unique": len(indices["imei"]) == 1,
            "imei_column_index": indices["imei"][0] if len(indices["imei"]) == 1 else None,
            "alias_column_found": bool(indices["alias"]),
            "alias_column_unique": len(indices["alias"]) == 1,
            "alias_column_index": indices["alias"][0] if len(indices["alias"]) == 1 else None,
        }

    @staticmethod
    def _empty_result_match_summary(observation: dict[str, object], header_map: dict[str, object] | None = None) -> dict[str, object]:
        header_map = header_map or {
            "result_column_count": 0, "serial_column_found": False, "serial_column_unique": False,
            "imei_column_found": False, "imei_column_unique": False, "alias_column_found": False,
            "alias_column_unique": False,
        }
        return {
            **header_map,
            "result_data_row_count": len(observation.get("result_rows", [])),
            "serial_match_count": 0, "imei_match_count": 0, "alias_match_count": 0,
            "serial_and_imei_match_count": 0, "serial_and_alias_match_count": 0,
            "all_available_fields_match_count": 0, "unique_result_match": False,
        }

    def _wait_for_serial_search_results_dom(self, timeout: float, before: dict[str, object], search_value: str | None = None) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() <= deadline:
            state = self._serial_search_results_dom_snapshot(
                self.browser.driver,
                before_signature=before["signature"],
                before_rows=before.get("result_rows", []),
            )
            if not state["loading_count"] and state["result_table_unique"] and state["result_rows_scoped_to_table"]:
                transition = bool(state["result_dom_changed"] or self._search_filter_condition_updated(self.browser.driver, search_value or ""))
                if transition or (state["empty_state_count"] and state["result_count"] == 0):
                    return state
            time.sleep(0.3)
        raise RuntimeError("検索結果DOMの確定待機がタイムアウトしました")

    def _serial_search_results_dom_snapshot(self, driver, before_signature=None, before_rows=None) -> dict[str, object]:
        before_rows = tuple(before_rows or ())
        table_schemas = []
        result_tables = []
        result_headers = []
        tbody_count = visible_row_count = checkbox_row_count = 0
        empty_state_count = loading_count = pagination_count = 0
        for table_index, table in enumerate(self._safe_find_driver_elements(driver, By.CSS_SELECTOR, "table")):
            if not self._safe_bool(table, "is_displayed"):
                continue
            bodies = [body for body in self._safe_find_elements_from(table, By.CSS_SELECTOR, "tbody") if self._safe_bool(body, "is_displayed")]
            tbody_count += len(bodies)
            rows = []
            data_rows = []
            table_counts = {"empty_state_count": 0, "loading_count": 0, "pagination_count": 0, "checkbox_row_count": 0}
            for tbody_index, body in enumerate(bodies):
                for row_index, row in enumerate(self._safe_find_elements_from(body, By.CSS_SELECTOR, "tr")):
                    schema = self._safe_result_element_schema(row, table_index, tbody_index)
                    schema["element_index"] = row_index
                    cells = self._safe_find_elements_from(row, By.CSS_SELECTOR, "td")
                    headers = self._safe_find_elements_from(row, By.CSS_SELECTOR, "th")
                    checkbox_count = len(self._safe_find_elements_from(row, By.CSS_SELECTOR, "input[type='checkbox'], [role='checkbox']"))
                    schema.update({"cell_count": len(cells) + len(headers), "checkbox_count": checkbox_count, "link_count": len(self._safe_find_elements_from(row, By.CSS_SELECTOR, "a")), "header_cell_count": len(headers), "data_cell_count": len(cells)})
                    rows.append(schema)
                    markers = self._safe_result_markers(row)
                    for marker, key in (("empty", "empty_state_count"), ("loading", "loading_count"), ("pagination", "pagination_count")):
                        if markers[marker]:
                            table_counts[key] += 1
                    if checkbox_count:
                        table_counts["checkbox_row_count"] += 1
                    if not self._is_current_result_data_row(row, markers, before_rows):
                        continue
                    if schema["tag"] == "tr" and not headers and cells and not any(markers.values()):
                        visible_row_count += 1
                        data_rows.append(row)
            table_schema = self._safe_result_element_schema(table, table_index)
            header_elements = self._safe_find_elements_from(table, By.CSS_SELECTOR, "thead th")
            if not header_elements:
                header_elements = self._safe_find_elements_from(table, By.CSS_SELECTOR, "tr th")
            table_headers = [self._safe_element_text(header).strip() for header in header_elements]
            table_schema.update({"tbody_count": len(bodies), "visible_row_count": len(data_rows), "data_row_count": len(data_rows), **table_counts, "rows": rows})
            table_schemas.append(table_schema)
            if bodies and (data_rows or table_counts["empty_state_count"]):
                result_tables.append((table_index, data_rows, table_schema))
                result_headers.append(table_headers)
            empty_state_count += table_counts["empty_state_count"]
            loading_count += table_counts["loading_count"]
            pagination_count += table_counts["pagination_count"]
            checkbox_row_count += table_counts["checkbox_row_count"]
        container_resolution_method = "html_table"
        container_raw_count = len(self._safe_find_driver_elements(driver, By.CSS_SELECTOR, "table"))
        container_visible_count = len([table for table in self._safe_find_driver_elements(driver, By.CSS_SELECTOR, "table") if self._safe_bool(table, "is_displayed")])
        container_tag = "table" if result_tables else ""
        container_role = ""
        row_resolution_method = "tbody_tr" if result_tables else "unresolved"
        raw_row_count = sum(len(item[2].get("rows", [])) for item in result_tables)
        visible_row_count_before_scope = visible_row_count
        if not result_tables:
            role_containers = self._safe_find_driver_elements(driver, By.CSS_SELECTOR, "[role='table'],[role='grid']")
            visible_role_containers = [item for item in role_containers if self._safe_bool(item, "is_displayed")]
            container_raw_count += len(role_containers)
            container_visible_count += len(visible_role_containers)
            if len(visible_role_containers) == 1:
                container_resolution_method = "aria_table_or_grid"
                container_tag = self._safe_tag(visible_role_containers[0])
                container_role = str(self._safe_attribute(visible_role_containers[0], "role") or "")
                role_rows = self._safe_find_elements_from(visible_role_containers[0], By.CSS_SELECTOR, "[role='row']")
                raw_row_count = len(role_rows)
                scoped_rows = []
                for row in role_rows:
                    markers = self._safe_result_markers(row)
                    cells = self._safe_find_elements_from(row, By.CSS_SELECTOR, "[role='cell'],[role='gridcell']")
                    if self._is_current_result_data_row(row, markers, before_rows, allow_role_cells=True) and cells:
                        scoped_rows.append(row)
                visible_role_rows = [row for row in role_rows if self._safe_bool(row, "is_displayed")]
                row_resolution_method = "aria_row_cells"
                visible_row_count = len(scoped_rows)
                result_tables = [(0, scoped_rows, {"rows": role_rows, "tbody_count": 0, "visible_row_count": len(scoped_rows), "data_row_count": len(scoped_rows), "empty_state_count": 0, "loading_count": 0, "pagination_count": 0, "checkbox_row_count": 0})] if scoped_rows else []
                result_headers = [[]]
        container_unique = container_visible_count == 1
        structural_unique = container_unique and len(result_tables) == 1 and visible_row_count == 1
        count_metrics = self._search_result_page_metrics(driver)
        if count_metrics["device_search_explicit_zero_result"]:
            count_category = "zero"
        elif count_metrics["device_search_result_total_count"] == 1:
            count_category = "one"
        elif count_metrics["device_search_result_total_count"] is not None:
            count_category = "multiple"
        else:
            count_category = "unknown"
        signature = (
            len(result_tables),
            tuple(
                (
                    item["tbody_count"],
                    item["visible_row_count"],
                    tuple(
                        (row["cell_count"], row["link_count"])
                        for row in item["rows"]
                        if row["displayed"] and row["data_cell_count"] and row["header_cell_count"] == 0
                    ),
                    item["empty_state_count"] > 0,
                    item["loading_count"] > 0,
                    item["pagination_count"] > 0,
                )
                for item in table_schemas
            ),
            count_category,
        )
        changed = before_signature is not None and signature != before_signature
        unique = len(result_tables) == 1
        result_count = len(result_tables[0][1]) if unique else -1
        return {
            "result_table_count": len(result_tables), "tbody_count": tbody_count, "visible_row_count": visible_row_count,
            "checkbox_row_count": checkbox_row_count, "empty_state_count": empty_state_count, "loading_count": loading_count,
            "pagination_count": pagination_count, "result_dom_changed": changed, "result_table_unique": unique,
            "result_rows_scoped_to_table": unique and result_count >= 0, "result_count": result_count, "schema": table_schemas,
            "signature": signature, "result_rows": result_tables[0][1] if unique else [], "result_headers": result_headers[0] if unique else [],
            "device_search_result_container_resolution_method": container_resolution_method,
            "device_search_result_container_raw_candidate_count": container_raw_count,
            "device_search_result_container_visible_candidate_count": container_visible_count,
            "device_search_result_container_unique": container_unique,
            "device_search_result_container_tag": container_tag,
            "device_search_result_container_role": container_role,
            "device_search_result_row_resolution_method": row_resolution_method,
            "device_search_result_row_raw_candidate_count": raw_row_count,
            "device_search_result_row_visible_candidate_count": visible_row_count_before_scope if row_resolution_method == "tbody_tr" else visible_row_count,
            "device_search_result_row_scoped_candidate_count": visible_row_count,
            "device_search_result_structural_uniqueness_verified": structural_unique,
        }

    def _is_current_result_data_row(self, row, markers: dict[str, bool], before_rows: tuple[object, ...], allow_role_cells: bool = False) -> bool:
        if not self._safe_bool(row, "is_displayed") or any(markers.values()):
            return False
        if self._safe_attribute(row, "hidden") is not None:
            return False
        if str(self._safe_attribute(row, "aria-hidden") or "").casefold() == "true":
            return False
        if any(row is previous or row == previous for previous in before_rows):
            return False
        cell_selector = "td,[role='cell'],[role='gridcell']" if allow_role_cells else "td"
        if not self._safe_find_elements_from(row, By.CSS_SELECTOR, cell_selector):
            return False
        try:
            geometry = self.browser.driver.execute_script(
                """
                const element = arguments[0];
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return {visible: style.display !== 'none' && style.visibility !== 'hidden', width: rect.width, height: rect.height, rect_count: element.getClientRects().length};
                """,
                row,
            )
            if not isinstance(geometry, dict):
                return False
            if geometry.get("visible") is not True or geometry.get("rect_count", 0) < 1:
                return False
            if geometry.get("width", 0) <= 0 or geometry.get("height", 0) <= 0:
                return False
        except Exception:
            pass
        return True

    def _safe_result_markers(self, element) -> dict[str, bool]:
        values = " ".join((self._safe_attribute(element, name) or "").casefold() for name in ("id", "name", "role", "data-testid", "class"))
        return {"empty": any(token in values for token in ("empty", "no-result", "no_result", "nodata", "no-data")), "loading": any(token in values for token in ("loading", "spinner", "progress")) or self._safe_tag(element) == "template", "pagination": any(token in values for token in ("pagination", "pager", "page-size", "page_size"))}

    def _safe_result_element_schema(self, element, table_index: int, tbody_index: int | None = None) -> dict[str, object]:
        parent_tag = ""
        try:
            parent_tag = self._safe_tag(element.find_element(By.XPATH, "./.."))
        except Exception:
            pass
        return {"element_index": 0, "tag": self._safe_tag(element), "id_present": self._safe_attribute(element, "id") is not None, "name_present": self._safe_attribute(element, "name") is not None, "role": self._safe_attribute(element, "role"), "data_testid_present": self._safe_attribute(element, "data-testid") is not None, "class_present": self._safe_attribute(element, "class") is not None, "displayed": self._safe_bool(element, "is_displayed"), "enabled": self._safe_bool(element, "is_enabled"), "parent_tag": parent_tag, "ancestor_table_index": table_index, "ancestor_tbody_index": tbody_index if tbody_index is not None else -1, "cell_count": 0, "checkbox_count": 0, "link_count": 0, "header_cell_count": 0, "data_cell_count": 0}

    def _search_button_candidates(self):
        driver = self.browser.driver
        if driver is None:
            raise RuntimeError("検索ボタンを確認できません")
        candidates = []
        for element in self._safe_find_driver_elements(driver, By.CSS_SELECTOR, "button, input[type='submit']"):
            tag = (self._safe_attribute(element, "tagName") or "").casefold()
            if not tag:
                try:
                    tag = str(element.tag_name).casefold()
                except Exception:
                    tag = ""
            input_type = (self._safe_attribute(element, "type") or "").casefold()
            if tag not in {"button", "input"} or (tag == "input" and input_type != "submit"):
                continue
            if self._search_button_is_semantic_candidate(element):
                candidates.append(element)
        return candidates

    def _search_button_is_semantic_candidate(self, element) -> bool:
        values = [
            self._safe_attribute(element, name)
            for name in ("id", "name", "data-testid", "aria-label", "title", "value")
        ]
        try:
            values.append(element.text)
        except Exception:
            pass
        label = " ".join(value.casefold() for value in values if isinstance(value, str))
        return any(token in label for token in ("search", "検索"))

    def _search_button_is_safe(self, element) -> bool:
        if not self._safe_bool(element, "is_displayed") or not self._safe_bool(element, "is_enabled"):
            return False
        if self._safe_attribute(element, "disabled") is not None:
            return False
        if not self._search_button_is_semantic_candidate(element):
            return False
        values = [
            self._safe_attribute(element, name)
            for name in ("id", "name", "data-testid", "aria-label", "title", "value")
        ]
        try:
            values.append(element.text)
        except Exception:
            pass
        label = " ".join(value.casefold() for value in values if isinstance(value, str))
        forbidden = ("save", "保存", "register", "登録", "update", "更新", "apply", "適用", "delete", "削除")
        if any(token in label for token in forbidden):
            return False
        try:
            form = element.find_element(By.XPATH, "./ancestor::form[1]")
        except Exception:
            return False
        return form is not None

    def _wait_for_serial_search_results(self, timeout: float, before=None) -> int:
        driver = self.browser.driver
        if driver is None:
            raise RuntimeError("検索結果を確認できません")
        if before is None:
            before = self._serial_search_result_state(driver)
        deadline = time.monotonic() + timeout
        while time.monotonic() <= deadline:
            state = self._serial_search_result_state(driver)
            if state["rows"] > 0:
                return state["rows"]
            if state["explicit_empty"] and (state != before or not before["container"]):
                return 0
            time.sleep(0.3)
        raise RuntimeError("検索結果の表示待機がタイムアウトしました")

    def _serial_search_result_state(self, driver) -> dict[str, object]:
        row_count = 0
        for selector in (
            "table tbody tr", "[data-testid='device-row']", "tr[data-testid*='device']",
            "li[data-testid*='device']",
        ):
            row_count = sum(
                1 for element in self._safe_find_driver_elements(driver, By.CSS_SELECTOR, selector)
                if self._safe_bool(element, "is_displayed")
            )
            if row_count:
                break
        empty_count = 0
        for selector in (
            "[data-testid*='empty' i]", "[data-testid*='no-result' i]",
            "[data-testid*='no_result' i]", ".no-results", ".no-result",
        ):
            empty_count += sum(
                1 for element in self._safe_find_driver_elements(driver, By.CSS_SELECTOR, selector)
                if self._safe_bool(element, "is_displayed")
            )
        loading_count = sum(
            1 for selector in ("[aria-busy='true']", "[role='progressbar']", ".loading", ".spinner")
            for element in self._safe_find_driver_elements(driver, By.CSS_SELECTOR, selector)
            if self._safe_bool(element, "is_displayed")
        )
        container = any(
            self._safe_find_driver_elements(driver, By.CSS_SELECTOR, selector)
            for selector in ("table", "[data-testid*='result' i]", "[data-testid*='device' i]")
        )
        return {
            "rows": row_count,
            "explicit_empty": empty_count > 0,
            "loading": loading_count > 0,
            "container": container,
        }

    def _wait_for_visible_listbox(self, driver, timeout: float, trace=None):
        def visible_listbox(_driver):
            listboxes = self._safe_find_driver_elements(driver, By.CSS_SELECTOR, "[role='listbox']")
            visible = [element for element in listboxes if self._safe_bool(element, "is_displayed")]
            return visible[0] if len(visible) == 1 else False

        try:
            return WebDriverWait(driver, timeout, poll_frequency=0.1).until(visible_listbox)
        except TimeoutException:
            return None

    def _wait_for_serial_selection_state(self, driver, control, before, trace=None):
        state = {**before, "listbox_visible": True}

        def selection_completed(_driver):
            listboxes = self._safe_find_driver_elements(driver, By.CSS_SELECTOR, "[role='listbox']")
            state.clear()
            state.update(self._selection_state(driver, control))
            state["listbox_visible"] = any(self._safe_bool(element, "is_displayed") for element in listboxes)
            return state if not state["listbox_visible"] else False

        try:
            WebDriverWait(driver, 15.0, poll_frequency=0.1).until(selection_completed)
        except TimeoutException:
            pass
        return state

    def _wait_for_serial_input_elements(self, driver, timeout: float, trace=None):
        def input_candidates(_driver):
            elements = self._serial_input_candidates(driver)
            return elements or False

        try:
            return WebDriverWait(driver, timeout, poll_frequency=0.1).until(input_candidates)
        except TimeoutException as exc:
            raise RuntimeError("シリアル番号入力欄を確認できません") from exc

    def _serial_input_candidates(self, driver):
        elements = self._safe_find_driver_elements(driver, By.CSS_SELECTOR, "input, textarea")
        candidates = []
        for element in elements:
            input_type = (self._safe_attribute(element, "type") or "text").casefold()
            element_id = self._safe_attribute(element, "id") or ""
            if element_id == "manual_page_input_assets" or input_type in {"checkbox", "radio", "hidden", "submit", "button"}:
                continue
            if not self._safe_bool(element, "is_displayed") or not self._safe_bool(element, "is_enabled"):
                continue
            if self._safe_attribute(element, "readonly") is not None or self._safe_attribute(element, "disabled") is not None:
                continue
            candidates.append(element)
        return candidates

    def _safe_input_dom_schema(self, element_index, element):
        element_id = self._safe_attribute(element, "id")
        return {
            "element_index": element_index,
            "tag": self._safe_tag(element),
            "id": element_id,
            "name": self._safe_attribute(element, "name"),
            "type": self._safe_attribute(element, "type"),
            "role": self._safe_attribute(element, "role"),
            "data-testid": self._safe_attribute(element, "data-testid"),
            "autocomplete": self._safe_attribute(element, "autocomplete"),
            "inputmode": self._safe_attribute(element, "inputmode"),
            "maxlength_present": self._safe_attribute(element, "maxlength") is not None,
            "pattern_present": self._safe_attribute(element, "pattern") is not None,
            "displayed": self._safe_bool(element, "is_displayed"),
            "enabled": self._safe_bool(element, "is_enabled"),
            "readonly": self._safe_attribute(element, "readonly") is not None,
            "disabled": self._safe_attribute(element, "disabled") is not None,
            "label_linked": self._has_linked_label(element_id),
        }

    def _selection_state(self, driver, control):
        native = self._safe_find_driver_elements(driver, By.TAG_NAME, "select")
        return {
            "control_expanded": self._safe_attribute(control, "aria-expanded"),
            "control_selected": self._safe_attribute(control, "aria-selected"),
            "native_selected_index": tuple(self._safe_attribute(element, "selectedIndex") for element in native),
            "input_count": self._safe_dynamic_input_count(driver),
        }

    @staticmethod
    def _selection_verified(before, after):
        return (
            before.get("control_expanded") != after.get("control_expanded")
            or before.get("control_selected") != after.get("control_selected")
            or before.get("native_selected_index") != after.get("native_selected_index")
            or before.get("input_count") != after.get("input_count")
        ) and after.get("listbox_visible") is False

    def _safe_dynamic_input_count(self, driver):
        return len(self._serial_input_candidates(driver))

    @staticmethod
    def _safe_find_elements_from(element, by, value):
        try:
            return list(element.find_elements(by, value))
        except Exception:
            return []

    def wait_for_device_page_stable(self, timeout: float = 30.0, trace=None) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        previous = None
        stable_count = 0
        last_observation = None
        self._trace(trace, "smsm_wait_device_page_stable", True)
        while time.monotonic() <= deadline:
            last_observation = self.enumerate_custom_search_controls(self.browser.driver)
            snapshot = last_observation["stability_snapshot"]
            if snapshot == previous:
                stable_count += 1
            else:
                stable_count = 1
                previous = snapshot
            if stable_count >= 3:
                self._trace(trace, "device_page_stable", True)
                return last_observation
            time.sleep(0.5)
        error = RuntimeError("機器ページDOMの安定待機がタイムアウトしました")
        error.observation = last_observation
        raise error

    def wait_for_device_page_detailed_stable(self, timeout: float = 30.0, trace=None) -> dict[str, object]:
        return self.wait_for_device_page_stable(timeout=timeout, trace=trace)

    def wait_for_device_page_ready(self, timeout: float = 30.0, trace=None) -> dict[str, object]:
        if not callable(getattr(self.browser, "wait_for_page_ready", None)):
            return self.wait_for_device_page_stable(timeout=timeout, trace=trace)

        def usable_controls(_driver):
            observation = self.enumerate_custom_search_controls(self.browser.driver)
            if (
                observation["custom_select_candidate_count"] == 1
                and observation["custom_select_unique"]
                and observation["select_backed_custom_ui_verified"]
                and observation["custom_schema"]
                and observation["custom_schema"][0]["displayed"]
                and observation["custom_schema"][0]["enabled"]
            ):
                return observation
            return False

        self._trace(trace, "smsm_wait_device_page_ready", True)
        try:
            return WebDriverWait(self.browser.driver, timeout, poll_frequency=0.1).until(usable_controls)
        except TimeoutException as exc:
            raise RuntimeError("機器ページの操作可能な検索条件を確認できません") from exc

    def enumerate_custom_search_controls(self, driver) -> dict[str, object]:
        stale_retry_count = 0
        for _attempt in range(3):
            try:
                native_elements = list(driver.find_elements(By.TAG_NAME, "select"))
                custom_elements = list(driver.find_elements(By.CSS_SELECTOR, "[role='combobox'], [aria-haspopup='listbox']"))
                break
            except StaleElementReferenceException as exc:
                stale_retry_count += 1
        else:
            raise RuntimeError("custom select DOMの再取得に失敗しました")
        native_schema = [self._safe_native_custom_schema(index, element) for index, element in enumerate(native_elements)]
        custom_schema = [self._safe_custom_control_schema(index, element, driver) for index, element in enumerate(custom_elements)]
        relation = self._safe_select_backing_relation(driver, native_elements, custom_elements)
        listbox_count = self._safe_count(driver, By.CSS_SELECTOR, "[role='listbox']")
        option_elements = self._safe_find_driver_elements(driver, By.CSS_SELECTOR, "[role='option'], option, [data-value], [data-option]")
        visible_option_count = sum(self._safe_bool(element, "is_displayed") for element in option_elements)
        option_role_count = len(self._safe_find_driver_elements(driver, By.CSS_SELECTOR, "[role='option']"))
        option_data_attribute_count = len(self._safe_find_driver_elements(driver, By.CSS_SELECTOR, "[data-value], [data-option]"))
        ready_state = self._safe_ready_state(driver)
        form_count = self._safe_count(driver, By.CSS_SELECTOR, "form")
        return {
            "native_select_count": len(native_elements),
            "hidden_native_select_count": sum(not item["displayed"] for item in native_schema),
            "custom_select_candidate_count": len(custom_elements),
            "custom_select_unique": len(custom_elements) == 1,
            "select_backed_custom_ui_detected": len(native_elements) > 0 and len(custom_elements) > 0,
            "select_backed_custom_ui_verified": relation["select_backed_custom_ui_verified"],
            "listbox_count": listbox_count,
            "option_candidate_count": len(option_elements),
            "visible_option_candidate_count": visible_option_count,
            "option_role_count": option_role_count,
            "option_data_attribute_count": option_data_attribute_count,
            "stale_retry_count": stale_retry_count,
            "native_schema": native_schema,
            "custom_schema": custom_schema,
            "relation": relation,
            "stability_snapshot": (len(native_elements), len(custom_elements), ready_state, form_count),
        }

    def _safe_native_custom_schema(self, element_index, element) -> dict[str, object]:
        return {
            "element_index": element_index,
            "tag": self._safe_tag(element),
            "id": self._safe_attribute(element, "id"),
            "name": self._safe_attribute(element, "name"),
            "type": self._safe_attribute(element, "type"),
            "class_present": bool(self._safe_attribute(element, "class")),
            "displayed": self._safe_bool(element, "is_displayed"),
            "enabled": self._safe_bool(element, "is_enabled"),
            "disabled": self._safe_attribute(element, "disabled") is not None,
            "readonly": self._safe_attribute(element, "readonly") is not None,
            "aria_hidden": self._safe_attribute(element, "aria-hidden") is not None,
            "option_count": self._safe_option_count_without_text(element),
            "selected_index_present": self._safe_property_present(element, "selectedIndex"),
            "parent_tag": self._safe_parent_value(element, "tagName"),
            "parent_id_present": self._safe_parent_value(element, "id") is not None,
            "parent_class_present": self._safe_parent_value(element, "className") is not None,
        }

    def _safe_custom_control_schema(self, element_index, element, driver) -> dict[str, object]:
        return {
            "element_index": element_index,
            "tag": self._safe_tag(element),
            "id": self._safe_attribute(element, "id"),
            "name": self._safe_attribute(element, "name"),
            "role": self._safe_attribute(element, "role"),
            "data-testid": self._safe_attribute(element, "data-testid"),
            "aria-haspopup": self._safe_attribute(element, "aria-haspopup"),
            "aria-expanded": self._safe_attribute(element, "aria-expanded"),
            "aria-controls_present": self._safe_attribute(element, "aria-controls") is not None,
            "aria-labelledby_present": self._safe_attribute(element, "aria-labelledby") is not None,
            "tabindex_present": self._safe_attribute(element, "tabindex") is not None,
            "class_present": bool(self._safe_attribute(element, "class")),
            "displayed": self._safe_bool(element, "is_displayed"),
            "enabled": self._safe_bool(element, "is_enabled"),
            "disabled": self._safe_attribute(element, "disabled") is not None,
            "readonly": self._safe_attribute(element, "readonly") is not None,
            "parent_tag": self._safe_parent_value(element, "tagName"),
            "child_count": self._safe_child_count(driver, element),
            "button_child_count": self._safe_child_count(driver, element, "button"),
            "listbox_child_count": self._safe_child_count(driver, element, "[role='listbox']"),
        }

    def _safe_select_backing_relation(self, driver, native_elements, custom_elements) -> dict[str, bool]:
        if len(native_elements) != 1 or len(custom_elements) != 1:
            return {key: False for key in (
                "same_parent", "custom_immediately_after_native", "custom_immediately_before_native",
                "custom_references_native_id", "native_references_custom_id", "shared_parent_id_present",
                "shared_parent_class_present", "select_backed_custom_ui_verified",
            )}
        native, custom = native_elements[0], custom_elements[0]
        script = """
            const native = arguments[0], custom = arguments[1];
            const np = native.parentElement, cp = custom.parentElement;
            const nativeId = native.getAttribute('id'), customId = custom.getAttribute('id');
            const refs = (element, value) => Boolean(value && element && (
                element.getAttribute('aria-controls') === value || element.getAttribute('aria-labelledby') === value
            ));
            return {
                same_parent: Boolean(np && np === cp),
                custom_immediately_after_native: native.nextElementSibling === custom,
                custom_immediately_before_native: custom.nextElementSibling === native,
                custom_references_native_id: refs(custom, nativeId),
                native_references_custom_id: refs(native, customId),
                shared_parent_id_present: Boolean(np && np.getAttribute('id')),
                shared_parent_class_present: Boolean(np && np.getAttribute('class')),
            };
        """
        try:
            result = driver.execute_script(script, native, custom)
        except Exception:
            result = {}
        relation = {key: bool(result.get(key, False)) for key in (
            "same_parent", "custom_immediately_after_native", "custom_immediately_before_native",
            "custom_references_native_id", "native_references_custom_id", "shared_parent_id_present",
            "shared_parent_class_present",
        )}
        relation["select_backed_custom_ui_verified"] = any(
            relation[key]
            for key in (
                "same_parent", "custom_immediately_after_native", "custom_immediately_before_native",
                "custom_references_native_id", "native_references_custom_id",
            )
        )
        return relation

    @staticmethod
    def _safe_find_driver_elements(driver, by, value):
        try:
            return list(driver.find_elements(by, value))
        except Exception:
            return []

    @staticmethod
    def _safe_count(driver, by, value) -> int:
        return len(SmsmHandler._safe_find_driver_elements(driver, by, value))

    @staticmethod
    def _safe_ready_state(driver):
        try:
            value = driver.execute_script("return document.readyState")
            return value if value in {"loading", "interactive", "complete"} else "unknown"
        except Exception:
            return "unknown"

    @staticmethod
    def _safe_tag(element):
        try:
            return str(element.tag_name).lower()
        except Exception:
            return ""

    @staticmethod
    def _safe_option_count_without_text(element) -> int:
        try:
            return len(element.find_elements(By.TAG_NAME, "option"))
        except Exception:
            try:
                return len(Select(element).options)
            except Exception:
                return 0

    @staticmethod
    def _safe_property_present(element, property_name: str) -> bool:
        try:
            return element.get_attribute(property_name) is not None
        except Exception:
            return False

    @staticmethod
    def _safe_parent_value(element, property_name: str):
        try:
            parent = element.find_element(By.XPATH, "..")
            value = getattr(parent, "tag_name", "") if property_name == "tagName" else parent.get_attribute("id" if property_name == "id" else "class")
            return value if value != "" else None
        except Exception:
            return None

    @staticmethod
    def _safe_child_count(driver, element, selector=None) -> int:
        try:
            if selector:
                return len(element.find_elements(By.CSS_SELECTOR, selector))
            return len(element.find_elements(By.XPATH, "./*"))
        except Exception:
            return 0

    def enumerate_search_controls(self, driver) -> dict[str, object]:
        top_selects = []
        iframe_count = 0
        iframe_with_select_count = 0
        native_selects = []
        custom_count = 0
        stale_retry_count = 0
        contexts = [("top", -1)]
        try:
            iframe_count = len(driver.find_elements(By.TAG_NAME, "iframe"))
        except Exception:
            raise RuntimeError("iframeを確認できません")
        for iframe_index in range(iframe_count):
            contexts.append(("iframe", iframe_index))
        for context, iframe_index in contexts:
            for attempt in range(3):
                try:
                    if context == "iframe":
                        frames = driver.find_elements(By.TAG_NAME, "iframe")
                        driver.switch_to.frame(frames[iframe_index])
                    selects = list(driver.find_elements(By.TAG_NAME, "select"))
                    top_selects.extend((context, iframe_index, element) for element in selects)
                    if context == "iframe" and selects:
                        iframe_with_select_count += 1
                    custom_count += self._custom_select_count(driver)
                    break
                except StaleElementReferenceException:
                    stale_retry_count += 1
                finally:
                    if context == "iframe":
                        driver.switch_to.default_content()
            else:
                raise RuntimeError("DOM要素の再取得に失敗しました")
        for context, iframe_index, element in top_selects:
            displayed = self._safe_bool(element, "is_displayed")
            enabled = self._safe_bool(element, "is_enabled")
            if displayed and enabled:
                native_selects.append((context, iframe_index, element))
        schema = []
        for element_index, (context, iframe_index, element) in enumerate(top_selects):
            schema.append(self._safe_element_schema(element_index, context, iframe_index, element))
        search_type_control_count = len(native_selects)
        serial_option_count = sum(
            sum(self._safe_option_text(option) == "シリアル番号" for option in Select(element).options)
            for _, _, element in native_selects
        )
        return {
            "top_document_select_count": sum(context == "top" for context, _, _ in top_selects),
            "iframe_count": iframe_count,
            "iframe_with_select_count": iframe_with_select_count,
            "native_select_count": len(top_selects),
            "visible_native_select_count": sum(self._safe_bool(element, "is_displayed") for _, _, element in top_selects),
            "enabled_native_select_count": sum(self._safe_bool(element, "is_enabled") for _, _, element in top_selects),
            "custom_select_candidate_count": custom_count,
            "select_backed_custom_ui_detected": custom_count > 0 and len(top_selects) > sum(self._safe_bool(element, "is_displayed") for _, _, element in top_selects),
            "stale_retry_count": stale_retry_count,
            "search_type_control_count": search_type_control_count,
            "search_type_control_unique": search_type_control_count == 1 and iframe_with_select_count <= 1,
            "summary": {
                "device_page_reached": True,
                "search_type_control_count": search_type_control_count,
                "serial_option_count": serial_option_count,
                "serial_option_selected": False,
                "serial_selection_verified": False,
                "input_count_before_selection": 0,
                "input_count_after_selection": 0,
                "text_input_count_after_selection": 0,
                "serial_input_candidate_count": 0,
                "serial_input_unique": False,
                "search_button_candidate_count": 0,
                "search_button_unique": False,
            },
            "schema": schema,
        }

    def wait_for_search_form_dom(self, timeout: float = 15.0, trace=None) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        last_observation = None
        while time.monotonic() <= deadline:
            self._trace(trace, "smsm_scan_top_document", True)
            last_observation = self.enumerate_search_controls(self.browser.driver)
            if last_observation["iframe_count"] > 0:
                self._trace(trace, "smsm_scan_iframes", True)
            if last_observation["native_select_count"] or last_observation["custom_select_candidate_count"]:
                return last_observation
            time.sleep(0.5)
        error = RuntimeError("検索フォームDOMの安定待機がタイムアウトしました")
        error.observation = last_observation
        raise error

    @staticmethod
    def _custom_select_count(driver) -> int:
        try:
            return len(driver.find_elements(By.CSS_SELECTOR, "[role='combobox'], [aria-haspopup='listbox']"))
        except Exception:
            return 0

    def _safe_element_schema(self, element_index, context, iframe_index, element) -> dict[str, object]:
        try:
            tag_name = str(element.tag_name).lower()
        except Exception:
            tag_name = ""
        element_id = self._safe_attribute(element, "id")
        return {
            "element_index": element_index,
            "document_context": context,
            "iframe_index": iframe_index,
            "tag": tag_name,
            "id": element_id,
            "name": self._safe_attribute(element, "name"),
            "type": self._safe_attribute(element, "type"),
            "role": self._safe_attribute(element, "role"),
            "data_testid": self._safe_attribute(element, "data-testid"),
            "displayed": self._safe_bool(element, "is_displayed"),
            "enabled": self._safe_bool(element, "is_enabled"),
            "readonly": self._safe_attribute(element, "readonly") is not None,
            "disabled": self._safe_attribute(element, "disabled") is not None,
            "option_count": self._safe_option_count(element),
            "selected_index_present": self._safe_attribute(element, "selectedIndex") is not None,
            "parent_tag": "",
            "associated_label_present": self._has_linked_label(element_id),
        }

    @staticmethod
    def _safe_option_count(element) -> int:
        try:
            return len(Select(element).options)
        except Exception:
            return 0

    def _safe_find_elements(self, by, value):
        try:
            return list(self.browser.driver.find_elements(by, value)) if self.browser.driver is not None else []
        except Exception:
            return []

    @staticmethod
    def _input_signature(elements) -> tuple[tuple[object, ...], ...]:
        return tuple((
            SmsmHandler._safe_attribute(element, "id"),
            SmsmHandler._safe_attribute(element, "name"),
            SmsmHandler._safe_attribute(element, "type"),
            SmsmHandler._safe_bool(element, "is_displayed"),
            SmsmHandler._safe_bool(element, "is_enabled"),
        ) for element in elements)

    def _is_serial_input_candidate(self, element) -> bool:
        input_type = (self._safe_attribute(element, "type") or "").casefold()
        element_id = self._safe_attribute(element, "id") or ""
        if input_type in {"checkbox", "radio", "hidden", "submit"} or element_id == "manual_page_input_assets":
            return False
        if input_type not in {"text", "search", ""}:
            return False
        if not self._safe_bool(element, "is_displayed") or not self._safe_bool(element, "is_enabled"):
            return False
        if self._safe_attribute(element, "readonly") is not None or self._safe_attribute(element, "disabled") is not None:
            return False
        return True

    def _safe_dom_schema(self, inputs, select_element, buttons) -> list[dict[str, object]]:
        elements = list(inputs) + [select_element] + list(buttons)
        schema = []
        for element_index, element in enumerate(elements):
            try:
                tag_name = str(element.tag_name).lower()
            except Exception:
                tag_name = ""
            item = {
                "element_index": element_index,
                "tag": tag_name,
                "id": self._safe_attribute(element, "id"),
                "name": self._safe_attribute(element, "name"),
                "type": self._safe_attribute(element, "type"),
                "role": self._safe_attribute(element, "role"),
                "data-testid": self._safe_attribute(element, "data-testid"),
                "autocomplete": self._safe_attribute(element, "autocomplete"),
                "inputmode": self._safe_attribute(element, "inputmode"),
                "maxlength_present": self._safe_attribute(element, "maxlength") is not None,
                "pattern_present": self._safe_attribute(element, "pattern") is not None,
                "displayed": self._safe_bool(element, "is_displayed"),
                "enabled": self._safe_bool(element, "is_enabled"),
                "readonly": self._safe_attribute(element, "readonly") is not None,
                "disabled": self._safe_attribute(element, "disabled") is not None,
                "label_linked": self._has_linked_label(self._safe_attribute(element, "id")),
            }
            schema.append(item)
        return schema

    def _select_serial_search_type(self) -> dict[str, object]:
        driver = self.browser.driver
        if driver is None:
            error = RuntimeError("検索条件コントロールを確認できません")
            error.failed_phase = "resolve_device_search_type"
            raise error
        dom_observation = self.enumerate_custom_search_controls(driver)
        controls = self._safe_find_driver_elements(driver, By.CSS_SELECTOR, "[role='combobox'], [aria-haspopup='listbox']")
        control_displayed = len(controls) == 1 and self._safe_bool(controls[0], "is_displayed")
        control_enabled = len(controls) == 1 and self._safe_bool(controls[0], "is_enabled")
        observation = {
            "device_search_type_control_candidate_count": len(controls),
            "device_search_type_option_candidate_count": dom_observation["option_candidate_count"],
            "device_search_type_target_option_found": False,
            "device_search_type_control_displayed": control_displayed,
            "device_search_type_control_enabled": control_enabled,
        }
        if (
            len(controls) != 1
            or not dom_observation.get("select_backed_custom_ui_verified", False)
            or not control_displayed
            or not control_enabled
        ):
            error = RuntimeError("検索条件プルダウンを一意に確認できません")
            error.failed_phase = "resolve_device_search_type"
            error.observation = observation
            raise error

        control = controls[0]
        if " ".join(self._safe_element_text_for_diagnostic(control).split()) == "シリアル番号":
            return {**observation, "device_search_type_target_option_found": True, "already_selected": True, "click_count": 0}

        before = self._selection_state(driver, control)
        try:
            control.click()
        except Exception as exc:
            error = RuntimeError("検索条件コントロールの展開に失敗しました")
            error.failed_phase = "open_serial_search_type_control"
            error.observation = observation
            raise error from exc

        listbox = self._wait_for_visible_listbox(driver, timeout=15.0)
        if listbox is None:
            error = RuntimeError("シリアル番号の選択状態を確認できません")
            error.failed_phase = "resolve_serial_search_option"
            error.observation = observation
            raise error
        candidates = self._safe_find_elements_from(listbox, By.CSS_SELECTOR, "[role='option'], option, [data-value], [data-option]")
        serial_candidates = [
            candidate for candidate in candidates
            if " ".join(self._safe_element_text_for_diagnostic(candidate).split()) == "シリアル番号"
            and self._safe_bool(candidate, "is_displayed")
        ]
        observation.update({
            "device_search_type_option_candidate_count": len(candidates),
            "device_search_type_target_option_found": len(serial_candidates) == 1,
        })
        if len(serial_candidates) != 1:
            error = RuntimeError("シリアル番号optionを一意に確認できません")
            error.failed_phase = "resolve_serial_search_option"
            error.observation = observation
            raise error
        try:
            serial_candidates[0].click()
        except Exception as exc:
            error = RuntimeError("シリアル番号optionの選択に失敗しました")
            error.failed_phase = "set_serial_search_type"
            error.observation = observation
            raise error from exc

        after = self._wait_for_serial_selection_state(driver, control, before)
        current_controls = self._safe_find_driver_elements(driver, By.CSS_SELECTOR, "[role='combobox'], [aria-haspopup='listbox']")
        selected = (
            len(current_controls) == 1
            and " ".join(self._safe_element_text_for_diagnostic(current_controls[0]).split()) == "シリアル番号"
            and after.get("listbox_visible") is False
        )
        if not selected:
            error = RuntimeError("シリアル番号の選択状態を確認できません")
            error.failed_phase = "set_serial_search_type"
            error.observation = observation
            raise error
        return {**observation, "already_selected": False, "click_count": 1}

    def _find_unique_search_input(self, field_name: str):
        driver = self.browser.driver
        if driver is None:
            raise RuntimeError(f"{field_name}検索入力欄を確認できません")
        candidates = []
        for element in driver.find_elements(By.CSS_SELECTOR, "input"):
            if self._safe_attribute(element, "type") not in {"text", "search", ""}:
                continue
            if not self._safe_bool(element, "is_displayed") or not self._safe_bool(element, "is_enabled"):
                continue
            if self._safe_attribute(element, "readonly") is not None or self._safe_attribute(element, "disabled") is not None:
                continue
            attributes = " ".join(str(self._safe_attribute(element, name) or "").casefold() for name in ("id", "name", "data-testid", "aria-label"))
            if field_name not in attributes:
                continue
            candidates.append(element)
        if len(candidates) != 1:
            raise RuntimeError(f"{field_name}検索入力欄を一意に確認できません")
        return candidates[0]

    def _find_unique_search_button(self):
        driver = self.browser.driver
        if driver is None:
            raise RuntimeError("検索ボタンを確認できません")
        candidates = []
        for element in driver.find_elements(By.CSS_SELECTOR, "button, input[type='submit']"):
            if not self._safe_bool(element, "is_displayed") or not self._safe_bool(element, "is_enabled"):
                continue
            attributes = " ".join(str(self._safe_attribute(element, name) or "").casefold() for name in ("id", "name", "data-testid", "aria-label"))
            if "search" in attributes or "検索" in attributes:
                candidates.append(element)
        if len(candidates) != 1:
            raise RuntimeError("検索ボタンを一意に確認できません")
        return candidates[0]

    @staticmethod
    def _safe_option_text(option) -> str:
        try:
            return (option.text or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _input_is_nonblank(element) -> bool:
        try:
            value = element.get_attribute("value")
        except Exception:
            return False
        return isinstance(value, str) and bool(value.strip())

    def _search_result_page_metrics(self, driver) -> dict[str, object]:
        visible_text = ""
        try:
            visible_text = driver.find_element(By.TAG_NAME, "body").text or ""
        except Exception:
            pass
        total_count = None
        page_count = None
        for pattern, target in (
            (r"(?:全|合計|total)\s*([0-9,]+)\s*件", "total"),
            (r"([0-9,]+)\s*件", "total"),
            (r"([0-9,]+)\s*/\s*([0-9,]+)\s*ページ", "page"),
        ):
            match = re.search(pattern, visible_text, re.IGNORECASE)
            if not match:
                continue
            if target == "total":
                total_count = int(match.group(1).replace(",", ""))
                break
            page_count = int(match.group(2).replace(",", ""))
        if page_count is None and re.search(r"1\s*/\s*1\s*ページ", visible_text, re.IGNORECASE):
            page_count = 1
        explicit_zero = bool(re.search(r"(?:0\s*件|該当する機器がありません|該当なし|結果がありません|no results?)", visible_text, re.IGNORECASE))
        return {
            "device_search_result_total_count": total_count,
            "device_search_result_page_count": page_count,
            "device_search_explicit_zero_result": explicit_zero,
        }

    def _search_filter_condition_updated(self, driver, value: str) -> bool:
        target = str(value or "").strip()
        if not target:
            return False
        try:
            inputs = self._serial_input_candidates(driver)
            input_value = str(self._safe_attribute(inputs[0], "value") or "").strip() if len(inputs) == 1 else ""
            body_text = str(driver.find_element(By.TAG_NAME, "body").text or "")
            return input_value == target and target in body_text
        except Exception:
            return False

    def _observe_serial_search_after_submit(self, before: dict[str, object] | None, value: str, trace=None) -> dict[str, object]:
        """Post-submit result observation for `_search_device_identifier`.

        Reuses the past-successful DOM-anchored primitives — `_wait_for_serial_search_
        results_dom` and `_serial_search_results_dom_snapshot` — that produced
        `logs/smsm_serial_search_results_dom_20260812_192627.json`. Identifies the
        serial column via `_map_result_columns` and compares each visible result row's
        serial cell using `.strip()` only, matching `match_serial_search_results_for_
        diagnostic`. Falls back to per-cell strict equality when the serial column
        cannot be uniquely identified from headers. `exact_match_count` stays at
        `None` unless the result DOM is stable and rows are scoped to a unique
        result table.
        """
        metrics: dict[str, object] = {
            "device_search_result_observation_called": True,
            "device_search_result_wait_called": False,
            "device_search_result_wait_completed": False,
            "device_search_result_container_count": 0,
            "device_search_result_row_candidate_count": 0,
            "device_search_visible_result_row_count": 0,
            "device_search_serial_column_candidate_count": None,
            "device_search_serial_column_unique": None,
            "device_search_serial_cell_candidate_count": 0,
            "device_search_serial_cell_nonblank_count": 0,
            "device_search_exact_match_count": None,
            "exact_match_count": None,
            "device_search_zero_result_indicator_found": False,
            "device_search_result_collection_method": "unresolved",
            "device_search_result_stable": False,
            "device_search_count_failed_phase": "wait_result_dom",
            "device_search_count_exception_type": "",
            "device_search_pre_result_visible_row_count": int((before or {}).get("visible_row_count", 0) or 0),
            "device_search_post_result_visible_row_count": 0,
            "device_search_result_signature_changed": False,
            "device_search_filter_condition_updated": False,
            "device_search_result_total_count": None,
            "device_search_result_page_count": None,
            "device_search_explicit_zero_result": False,
            "device_search_result_transition_verified": False,
            "device_result_candidate_count": 0,
            "device_result_candidate_unique": False,
            "device_result_identity_verified": False,
            "device_search_input_exact_match": False,
            "device_search_identity_context_verified": False,
            "device_search_result_container_resolution_method": "unresolved",
            "device_search_result_container_raw_candidate_count": 0,
            "device_search_result_container_visible_candidate_count": 0,
            "device_search_result_container_unique": False,
            "device_search_result_container_tag": "",
            "device_search_result_container_role": "",
            "device_search_result_row_resolution_method": "unresolved",
            "device_search_result_row_raw_candidate_count": 0,
            "device_search_result_row_visible_candidate_count": 0,
            "device_search_result_row_scoped_candidate_count": 0,
            "device_search_result_metrics_overwrite_detected": False,
            "device_search_result_total_count_source": "unavailable",
            "device_search_result_page_count_source": "unavailable",
            "device_search_result_structural_uniqueness_verified": False,
        }
        self._trace_many(trace, metrics)
        after: dict[str, object] | None = None
        if isinstance(before, dict) and before.get("signature"):
            metrics["device_search_result_wait_called"] = True
            self._trace(trace, "device_search_result_wait_called", True)
            try:
                after = self._wait_for_serial_search_results_dom(timeout=30.0, before=before, search_value=value)
                metrics["device_search_result_wait_completed"] = True
                self._trace(trace, "device_search_result_wait_completed", True)
            except Exception as exc:
                metrics["device_search_count_exception_type"] = type(exc).__name__
                self._trace(trace, "device_search_count_exception_type", type(exc).__name__)
        if after is None:
            try:
                signature = before.get("signature") if isinstance(before, dict) else None
                after = self._serial_search_results_dom_snapshot(
                    self.browser.driver,
                    before_signature=signature,
                    before_rows=(before or {}).get("result_rows", []),
                )
            except Exception as exc:
                if not metrics["device_search_count_exception_type"]:
                    metrics["device_search_count_exception_type"] = type(exc).__name__
                return metrics
        metrics["device_search_result_container_count"] = int(after.get("result_table_count", 0) or 0)
        metrics["device_search_visible_result_row_count"] = int(after.get("visible_row_count", 0) or 0)
        metrics["device_search_post_result_visible_row_count"] = metrics["device_search_visible_result_row_count"]
        metrics["device_search_result_signature_changed"] = bool(after.get("result_dom_changed"))
        page_metrics = self._search_result_page_metrics(self.browser.driver)
        for key, page_value in page_metrics.items():
            if key == "device_search_explicit_zero_result":
                metrics[key] = page_value
                continue
            if page_value is None:
                if metrics.get(key) is not None:
                    metrics["device_search_result_metrics_overwrite_detected"] = True
                continue
            metrics[key] = page_value
            metrics[f"{key}_source"] = "body_text_pattern"
        metrics["device_search_filter_condition_updated"] = self._search_filter_condition_updated(self.browser.driver, value)
        metrics["device_search_zero_result_indicator_found"] = int(after.get("empty_state_count", 0) or 0) > 0
        self._trace_many(trace, {
            "device_search_result_container_count": metrics["device_search_result_container_count"],
            "device_search_visible_result_row_count": metrics["device_search_visible_result_row_count"],
            "device_search_zero_result_indicator_found": metrics["device_search_zero_result_indicator_found"],
        })
        metrics["device_search_result_stable"] = bool(
            after.get("result_dom_changed")
            and after.get("result_table_unique")
            and after.get("result_rows_scoped_to_table")
            and not after.get("loading_count")
        )
        metrics["device_search_result_transition_verified"] = bool(
            metrics["device_search_filter_condition_updated"]
            or metrics["device_search_result_signature_changed"]
            or metrics["device_search_explicit_zero_result"]
        )
        metrics["device_result_candidate_count"] = int(after.get("result_count", 0) or 0) if metrics["device_search_result_transition_verified"] else 0
        structural_unique = after.get("device_search_result_structural_uniqueness_verified") is True
        count_unique = metrics["device_search_result_total_count"] == 1 and metrics["device_search_result_page_count"] == 1
        metrics["device_result_candidate_unique"] = bool(
            metrics["device_search_result_transition_verified"]
            and (count_unique or structural_unique)
            and metrics["device_search_post_result_visible_row_count"] == 1
            and metrics["device_search_result_container_count"] == 1
            and int(after.get("result_count", 0) or 0) == 1
        )
        for key in (
            "device_search_result_container_resolution_method", "device_search_result_container_raw_candidate_count",
            "device_search_result_container_visible_candidate_count", "device_search_result_container_unique",
            "device_search_result_container_tag", "device_search_result_container_role",
            "device_search_result_row_resolution_method", "device_search_result_row_raw_candidate_count",
            "device_search_result_row_visible_candidate_count", "device_search_result_row_scoped_candidate_count",
            "device_search_result_structural_uniqueness_verified",
        ):
            if key in after:
                metrics[key] = after[key]
        metrics["device_search_count_failed_phase"] = "scope_result_rows"
        if not (after.get("result_table_unique") and after.get("result_rows_scoped_to_table")):
            if metrics["device_search_zero_result_indicator_found"] and int(after.get("result_count", -1) or 0) == 0:
                metrics["device_search_result_collection_method"] = "zero_result_indicator"
                metrics["device_search_exact_match_count"] = 0
                metrics["exact_match_count"] = 0
                metrics["device_search_count_failed_phase"] = "completed"
            return metrics
        result_rows = list(after.get("result_rows") or [])
        metrics["device_search_result_row_candidate_count"] = len(result_rows)
        if not result_rows and metrics["device_search_zero_result_indicator_found"]:
            metrics["device_search_result_collection_method"] = "zero_result_indicator"
            metrics["device_search_exact_match_count"] = 0
            metrics["exact_match_count"] = 0
            metrics["device_search_count_failed_phase"] = "completed"
            return metrics
        headers = list(after.get("result_headers") or [])
        header_map = self._map_result_columns(headers)
        metrics["device_search_serial_column_candidate_count"] = sum(
            1 for header in headers if header in {"シリアル番号", "Serial", "Serial Number"}
        )
        metrics["device_search_serial_column_unique"] = header_map["serial_column_unique"]
        self._trace_many(trace, {
            "device_search_serial_column_candidate_count": metrics["device_search_serial_column_candidate_count"],
            "device_search_serial_column_unique": metrics["device_search_serial_column_unique"],
        })
        metrics["device_search_count_failed_phase"] = "completed"
        metrics["device_search_result_collection_method"] = (
            "unique_aria_result_without_identity"
            if after.get("device_search_result_container_resolution_method") == "aria_table_or_grid"
            else "unique_result_table_without_identity"
        )
        metrics["device_search_exact_match_count"] = None
        metrics["exact_match_count"] = metrics["device_search_exact_match_count"]
        return metrics

    def count_exact_device_serial_results(self, serial: str) -> int:
        if self.browser.driver is None or not serial:
            return 0
        normalized_serial = re.sub(r"\s+", "", str(serial)).casefold()
        selectors = (
            "table tbody tr",
            "[data-testid='device-row']",
            "tr[data-testid*='device']",
            "li[data-testid*='device']",
        )
        for selector in selectors:
            rows = [
                row for row in self.browser.driver.find_elements(By.CSS_SELECTOR, selector)
                if self._safe_bool(row, "is_displayed")
            ]
            if rows:
                exact_count = 0
                for row in rows:
                    row_text = self._safe_element_text(row).strip().casefold()
                    if row_text in {"no data", "no results", "該当なし", "結果がありません"}:
                        continue
                    cells = self._safe_find_elements_from(row, By.CSS_SELECTOR, "td,th")
                    values = cells or [row]
                    exact_count += any(
                        re.sub(r"\s+", "", self._safe_element_text(cell)).casefold() == normalized_serial
                        for cell in values
                    )
                return exact_count
        return 0

    def count_visible_device_results(self) -> int:
        if self.browser.driver is None:
            raise RuntimeError("ブラウザドライバーが存在しません")

        selectors = [
            "table tbody tr",
            "[data-testid='device-row']",
            "tr[data-testid*='device']",
            "li[data-testid*='device']",
        ]
        for selector in selectors:
            rows = []
            for element in self.browser.driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if element.is_displayed():
                        rows.append(element)
                except Exception:
                    continue
            if rows:
                return sum(
                    1
                    for row in rows
                    if (row.text or "").strip().lower()
                    not in {"", "no data", "no results", "該当なし", "結果がありません"}
                )
        return 0

    def associate_imei(self, serial: str, imei: str) -> None:
        if not imei:
            raise ValueError("imeiが空です")

        self.logger.info(
            "IMEI紐づけ: serial_fingerprint=%s, imei_fingerprint=%s",
            hashlib.sha256(str(serial).encode("utf-8")).hexdigest()[:12],
            hashlib.sha256(str(imei).encode("utf-8")).hexdigest()[:12],
        )
        self.browser.wait_for_page_ready()

        self._click_first_required([
            (By.XPATH, "//button[contains(normalize-space(.), '編集') or contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'edit') or contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'update')]"),
            (By.XPATH, "//a[contains(normalize-space(.), '編集') or contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'edit') or contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'update')]"),
        ], "編集ボタン")

        imei_input = self.browser.find_first([
            (By.CSS_SELECTOR, "input[name*='imei' i]"),
            (By.CSS_SELECTOR, "input[id*='imei' i]"),
            (By.CSS_SELECTOR, "input[placeholder*='IMEI' i]"),
            (By.CSS_SELECTOR, "input[aria-label*='IMEI' i]"),
        ], timeout=5)
        imei_input.clear()
        imei_input.send_keys(imei)

        self._click_first_required([
            (By.XPATH, "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'save')]"),
            (By.XPATH, "//button[contains(normalize-space(.), '保存')]"),
            (By.XPATH, "//input[@type='submit' and (contains(@value, '保存') or contains(@value, 'Save'))]"),
        ], "IMEI保存ボタン")

        self._wait_for_success_message("IMEI更新")

    def login_and_navigate(self) -> None:
        self.browser.wait_for_page_ready()
        if not self._is_login_success():
            raise RuntimeError("SMSMログイン状態ではありません")

    def capture_certificate_navigation_route_for_diagnostic(self, trace=None, timeout_seconds: float = 300.0) -> list[dict[str, object]]:
        """Observe four user clicks and return only landmark-confirmed route records."""
        driver = self.browser.driver
        if driver is None:
            raise RuntimeError("SMSM画面を確認できません")
        self._install_route_capture_monitor()
        stages = (
            ("settings_navigation", "ios"),
            ("ios_navigation", "certificate_management"),
            ("certificate_management_expand", "client_certificate_management"),
            ("client_certificate_management_navigation", "client_certificate_management"),
        )
        records: list[dict[str, object]] = []
        started_at = time.monotonic()
        while time.monotonic() - started_at <= timeout_seconds:
            events = driver.execute_script("return window.__smsmRouteCapture ? window.__smsmRouteCapture.drain() : [];") or []
            for event in events:
                if not isinstance(event, dict) or event.get("type") != "click":
                    continue
                stage_index = len(records)
                if stage_index >= len(stages):
                    break
                stage, landmark_kind = stages[stage_index]
                after_landmarks = event.get("landmarks") if isinstance(event.get("landmarks"), dict) else {}
                before_landmarks = event.get("landmarks_before") if isinstance(event.get("landmarks_before"), dict) else {}
                if stage == "settings_navigation":
                    verified = bool(event.get("after_ready") and after_landmarks.get("ios") and self._route_ios_state(driver)["ios_tab_selected"])
                elif stage == "ios_navigation":
                    verified = bool(event.get("after_ready") and after_landmarks.get("certificate_management"))
                elif stage == "certificate_management_expand":
                    verified = bool(event.get("after_ready") and event.get("action_type") == "accordion_expand" and event.get("accordion_expand_verified"))
                else:
                    verified = bool(event.get("after_ready") and after_landmarks.get("client_certificate_management") and self._route_landmark_present(driver, "client_certificate_management"))
                if not verified:
                    continue
                safe_event = self._safe_route_event(event, driver)
                safe_event.update({"stage": stage, "landmark_kind": landmark_kind, "action_type": event.get("action_type", "navigation")})
                records.append(safe_event)
                self._trace(trace, f"{stage}_verified", True)
                if stage == "settings_navigation":
                    self._trace(trace, "target_os_ios", True)
                    self._trace(trace, "ios_tab_selected", True)
                    self._trace(trace, "android_tab_selected", False)
                if stage == "certificate_management_expand":
                    for key in ("pathname_changed", "aria_expanded_changed", "child_menu_became_visible", "child_container_visibility_changed", "expanded_state_changed", "accordion_expand_verified"):
                        self._trace(trace, key, bool(event.get(key)))
                self._trace_elapsed(trace, stage, started_at)
                if stage == "client_certificate_management_navigation":
                    self._trace(trace, "client_certificate_page_landmark_verified", True)
                    self._trace_elapsed(trace, "certificate_navigation_total", started_at)
                    self._remove_route_capture_monitor(driver)
                    return records
            try:
                WebDriverWait(driver, 0.5, poll_frequency=0.1).until(lambda current: bool(current.execute_script("return window.__smsmRouteCapture ? window.__smsmRouteCapture.pending() : 0;")))
            except TimeoutException:
                pass
        self._remove_route_capture_monitor(driver)
        raise RuntimeError("教師付きSMSMナビゲーション採取がタイムアウトしました")

    def capture_manual_certificate_checkpoint_for_diagnostic(self, trace=None, input_func=input) -> dict[str, object]:
        """Wait for an empty Enter, then validate only the browser's current final page."""
        driver = self.browser.driver
        if driver is None:
            raise RuntimeError("SMSMブラウザーセッションを確認できません")
        self._trace(trace, "manual_checkpoint_wait_started", True)
        print("ブラウザーで設定、iOS、証明書管理、クライアント証明書管理の順に手動操作してください。")
        print("クライアント証明書管理画面が表示されたらPowerShellへ戻り、空のままEnterを押してください。")
        print("証明書の選択、追加、アップロード、削除は行わないでください。")
        try:
            value = input_func()
        except KeyboardInterrupt:
            raise
        if value != "":
            self._trace(trace, "manual_checkpoint_received", False)
            raise RuntimeError("空のEnter入力が必要です")
        self._trace(trace, "manual_checkpoint_received", True)
        current_url = self._current_url()
        current = urlparse(current_url)
        configured = urlparse(self.smsm_config.url)
        session_valid = bool(driver and current.scheme in {"http", "https"} and current.hostname)
        same_host = session_valid and bool(configured.hostname and current.hostname.casefold() == configured.hostname.casefold())
        login_page = bool(re.search(r"login|signin|sign-in", current.path, re.IGNORECASE))
        landmark = self._client_certificate_page_landmark_state(driver)
        safe_path = bool(current.path.startswith("/") and current.path not in {"", "/"} and "?" not in current.path and "#" not in current.path)
        manual_path_verified = self._verify_manual_client_certificate_checkpoint(
            landmark,
            session_valid=session_valid,
            same_host=same_host,
            login_page=login_page,
            current_path=current.path,
        )
        landmark["current_path_verified_by_manual_checkpoint"] = manual_path_verified
        if manual_path_verified:
            landmark["client_certificate_specific_landmark_count"] = max(int(landmark.get("client_certificate_specific_landmark_count", 0)), 3)
        landmark_verified = manual_path_verified
        landmark["landmark_schema"] = {
            **(landmark.get("landmark_schema") if isinstance(landmark.get("landmark_schema"), dict) else {}),
            "certificate_management_expanded_by_attribute": landmark.get("certificate_management_expanded_by_attribute", False),
            "certificate_management_expanded_by_visible_child": landmark.get("certificate_management_expanded_by_visible_child", False),
            "current_path_verified_by_manual_checkpoint": manual_path_verified,
            "client_certificate_specific_landmark_count": landmark.get("client_certificate_specific_landmark_count", 0),
            "target_os_ios_verified_required": True,
            "ios_tab_selected_required": True,
            "android_tab_not_selected_required": True,
            "ios_content_container_visible_required": True,
            "android_content_container_hidden_required": True,
            "deduplicated_client_child_required": True,
            "certificate_management_expanded_required": True,
            "client_certificate_child_visible_required": True,
            "client_certificate_child_active_required": True,
            "client_certificate_child_href_present_required": True,
            "current_path_matches_client_child_required": True,
            "specific_landmark_minimum": 3,
            "operation_structure_required": False,
            "manual_checkpoint_path_verified": manual_path_verified,
            "child_active_semantic_available": landmark.get("client_certificate_child_active_semantic") is True,
            "child_href_path_match_available": landmark.get("current_path_matches_client_certificate_child") is True,
        }
        failure_reason = ""
        if landmark.get("client_certificate_child_candidate_count") != 1:
            failure_reason = "client_child_ambiguous"
        elif not landmark.get("client_certificate_child_visible"):
            failure_reason = "client_child_not_visible"
        elif not landmark.get("client_certificate_child_active"):
            failure_reason = "client_child_not_active"
        elif not landmark.get("client_certificate_child_href_present") or not landmark.get("current_path_matches_client_certificate_child"):
            failure_reason = "client_child_path_not_verified"
        elif not landmark.get("target_os_ios_verified") or not landmark.get("ios_tab_selected"):
            failure_reason = "ios_not_verified"
        elif landmark.get("android_tab_selected") is True:
            failure_reason = "android_selected"
        elif not safe_path or login_page:
            failure_reason = "unsafe_current_path"
        elif landmark.get("client_certificate_specific_landmark_count", 0) < 3:
            failure_reason = "specific_landmarks_insufficient"
        verified = bool(session_valid and same_host and not login_page and safe_path and landmark_verified)
        if not verified and not failure_reason:
            failure_reason = "unexpected_error"
        self._trace(trace, "browser_session_valid", session_valid)
        self._trace(trace, "same_host_verified", same_host)
        landmark["client_certificate_page_landmark_verified"] = landmark_verified
        output_keys = ("target_os_ios_verified", "ios_tab_selected", "android_tab_selected", "ios_content_container_visible", "android_content_container_visible", "raw_text_match_count", "visible_text_match_count", "left_navigation_match_count", "clickable_resolution_count", "deduplicated_clickable_candidate_count", "hidden_match_count", "outside_navigation_match_count", "certificate_management_expanded_by_attribute", "certificate_management_expanded_by_visible_child", "certificate_management_expanded", "client_certificate_child_visible", "client_certificate_child_active", "client_certificate_child_href_present", "current_path_matches_client_certificate_child", "current_path_verified_by_manual_checkpoint", "certificate_operation_structure_verified", "client_certificate_specific_landmark_count", "client_certificate_page_landmark_verified")
        for key in output_keys:
            if key in landmark:
                self._trace(trace, key, landmark[key])
            print(f"{key}={landmark[key]}")
        print("manual_checkpoint_received=True")
        if not verified:
            print("navigation_route_saved=False")
            print(f"manual_checkpoint_failure_reason={failure_reason}")
        if not verified:
            raise RuntimeError("手動チェックポイントの最終画面を確認できません")
        return {"same_host_path": current.path, "target_os": "ios", "landmark_schema": landmark.get("landmark_schema", {}), "verified": True, "browser_session_valid": session_valid, "same_host_verified": same_host, **landmark}

    def navigate_verified_final_path_for_diagnostic(self, manifest: dict[str, object], trace=None) -> dict[str, object]:
        """Navigate directly to a previously verified same-host pathname without menu clicks."""
        driver = self.browser.driver
        if driver is None:
            raise RuntimeError("SMSMブラウザーセッションを確認できません")
        self._last_replay_landmark_schema = manifest.get("landmark_schema") if isinstance(manifest.get("landmark_schema"), dict) else {}
        path = str(manifest.get("same_host_path") or "")
        route_observation = {
            "navigation_route_manifest_found": True,
            "navigation_route_valid": True,
            "navigation_route_fingerprint_valid": True,
            "navigation_route_landmark_schema_valid": True,
            "current_origin_valid": False,
            "target_url_built": False,
            "target_same_host": False,
            "target_path_matches_manifest": False,
            "navigation_get_called": False,
            "navigation_get_completed": False,
            "post_navigation_same_host": False,
            "post_navigation_path_matches_manifest": False,
            "post_navigation_login_page_detected": False,
            "post_navigation_redirect_detected": False,
            "client_certificate_page_landmark_verified": False,
            "smsm_route_navigation_called": True,
            "smsm_route_target_built": False,
            "smsm_route_same_host": False,
            "smsm_route_get_called": False,
            "smsm_route_get_completed": False,
            "smsm_route_post_path_checked": False,
            "smsm_route_post_path_matches": False,
            "smsm_route_login_page_detected": False,
        }
        self._trace(trace, "smsm_verified_route_manifest_loaded", True)
        self._trace(trace, "smsm_verified_route_manifest_validated", True)
        try:
            handles = list(driver.window_handles)
            current_handle = driver.current_window_handle
            if len(handles) != 1 or current_handle not in handles:
                raise RuntimeError("ログイン完了windowを一意に確認できません")
            current = urlparse(self._current_url())
            current_origin = self._origin_from_url(current)
            login_path = bool(re.search(r"login|signin|sign-in", current.path, re.IGNORECASE))
            configured = urlparse(self.smsm_config.url)
            configured_host = configured.hostname.casefold() if configured.hostname else ""
            current_origin_valid = bool(current_origin and current.hostname and configured_host and current.hostname.casefold() == configured_host and not login_path)
            route_observation["current_origin_valid"] = current_origin_valid
            self._trace(trace, "smsm_verified_route_origin_resolved", current_origin_valid)
            if not current_origin_valid:
                raise RuntimeError("ログイン完了後のSMSM originを確認できません")
            if not path.startswith("/") or path in {"", "/"} or "?" in path or "#" in path or urlparse(path).netloc:
                raise RuntimeError("確認済みSMSM pathnameを検証できません")
            target_url = urlunparse(urlparse(current_origin)._replace(path=path, params="", query="", fragment=""))
            route_observation["target_url_built"] = True
            route_observation["target_same_host"] = urlparse(target_url).hostname.casefold() == current.hostname.casefold()
            route_observation["smsm_route_target_built"] = True
            route_observation["smsm_route_same_host"] = route_observation["target_same_host"]
            route_observation["target_path_matches_manifest"] = self._normalized_path(urlparse(target_url).path) == self._normalized_path(path)
            self._trace(trace, "smsm_verified_route_target_built", route_observation["target_url_built"] and route_observation["target_same_host"] and route_observation["target_path_matches_manifest"])
            if not route_observation["target_same_host"] or not route_observation["target_path_matches_manifest"]:
                raise RuntimeError("確認済みSMSM target URLを検証できません")
            navigation_started_at = time.monotonic()
            self._trace(trace, "smsm_verified_route_navigation_started", True)
            route_observation["navigation_get_called"] = True
            route_observation["smsm_route_get_called"] = True
            driver.get(target_url)
            route_observation["navigation_get_completed"] = True
            route_observation["smsm_route_get_completed"] = True
            self._trace(trace, "smsm_verified_route_navigation_command_completed", True)
            def final_state(current_driver):
                parsed = urlparse(self._current_url())
                same_host = bool(parsed.hostname and parsed.hostname.casefold() == current.hostname.casefold())
                login = bool(re.search(r"login|signin|sign-in", parsed.path, re.IGNORECASE))
                landmark = self._strict_client_certificate_page_state(current_driver, path)
                path_matches = self._normalized_path(unquote(parsed.path)) == self._normalized_path(unquote(path))
                landmark.update(self._evaluate_strict_client_certificate_snapshot(landmark, path_matches))
                route_observation.update({"post_navigation_same_host": same_host, "post_navigation_path_matches_manifest": path_matches, "post_navigation_login_page_detected": login, "post_navigation_redirect_detected": not path_matches, "smsm_route_post_path_checked": True, "smsm_route_post_path_matches": path_matches, "smsm_route_login_page_detected": login, **landmark})
                state = {"parsed": parsed, "same_host": same_host, "login": login, "path_matches": path_matches, "landmark": landmark}
                replay_verified = landmark.get("smsm_client_certificate_page_live_verified") is True
                landmark["client_certificate_page_landmark_verified"] = replay_verified
                return state if same_host and not login and path_matches and replay_verified else False
            state = WebDriverWait(driver, 10.0, poll_frequency=0.3).until(final_state)
            route_observation.update({"post_navigation_same_host": state["same_host"], "post_navigation_path_matches_manifest": state["path_matches"], "post_navigation_login_page_detected": state["login"], "post_navigation_redirect_detected": not state["path_matches"], **state["landmark"]})
            self._trace(trace, "smsm_verified_route_post_navigation_url_checked", True)
            self._trace(trace, "smsm_verified_route_landmark_checked", True)
            self._trace(trace, "smsm_verified_route_page_reached", True)
            self._trace_elapsed(trace, "verified_path_navigation", navigation_started_at)
            self._last_navigation_observation = {**route_observation, **state["landmark"]}
            return self._last_navigation_observation
        except Exception as exc:
            route_observation["post_navigation_login_page_detected"] = bool(re.search(r"login|signin|sign-in", urlparse(self._current_url()).path, re.IGNORECASE)) if route_observation["navigation_get_called"] else False
            route_observation["post_navigation_redirect_detected"] = route_observation["navigation_get_completed"] and not route_observation["post_navigation_path_matches_manifest"]
            if not route_observation["current_origin_valid"]:
                route_observation["failed_stage"] = "smsm_verified_route_origin_resolved"
            elif not route_observation["target_url_built"] or not route_observation["target_same_host"] or not route_observation["target_path_matches_manifest"]:
                route_observation["failed_stage"] = "smsm_verified_route_target_built"
            elif not route_observation["navigation_get_called"]:
                route_observation["failed_stage"] = "smsm_verified_route_navigation_started"
            elif not route_observation["navigation_get_completed"]:
                route_observation["failed_stage"] = "smsm_verified_route_navigation_command_completed"
            elif route_observation["post_navigation_login_page_detected"] or not route_observation["post_navigation_same_host"] or not route_observation["post_navigation_path_matches_manifest"]:
                route_observation["failed_stage"] = "smsm_verified_route_post_navigation_url_checked"
            else:
                route_observation["failed_stage"] = "smsm_verified_route_landmark_checked"
            if route_observation.get("smsm_route_post_path_checked") and not route_observation.get("smsm_route_post_path_matches"):
                route_observation["failed_stage"] = "smsm_navigate_client_certificate_route"
            elif route_observation.get("smsm_strict_page_probe_called") and not route_observation.get("smsm_strict_page_probe_completed"):
                route_observation["failed_stage"] = "smsm_probe_client_certificate_page"
            error = RuntimeError("確認済みpathnameの最終画面を確認できません")
            error.observation = route_observation
            raise error from exc

    @staticmethod
    def _evaluate_strict_client_certificate_snapshot(snapshot: dict[str, object], pathname_matches: bool) -> dict[str, object]:
        """Evaluate scalar snapshot values and expose every strict predicate condition."""
        def count(key: str):
            value = snapshot.get(key)
            return value if type(value) is int else None

        settings_count = count("smsm_settings_nav_candidate_count")
        ios_count = count("smsm_ios_settings_candidate_count")
        menu_count = count("smsm_client_certificate_menu_candidate_count")
        search_count = count("smsm_certificate_search_input_candidate_count")
        add_count = count("smsm_certificate_add_icon_candidate_count")
        probe_complete = snapshot.get("smsm_strict_page_probe_completed") is True and snapshot.get("smsm_strict_page_probe_snapshot_available") is True and snapshot.get("smsm_strict_page_probe_exception_type", "") == ""
        settings_consistent = settings_count == 0 or (settings_count is not None and settings_count >= 1 and snapshot.get("smsm_settings_nav_active") is True and snapshot.get("smsm_device_nav_active") is False)
        menu_consistent = menu_count == 0 or (menu_count is not None and menu_count >= 1 and snapshot.get("smsm_client_certificate_menu_active") is True)
        conditions = {
            "smsm_condition_settings_nav_unique": probe_complete and (settings_count == 0 or settings_count == 1),
            "smsm_condition_settings_nav_active": probe_complete and (settings_count == 0 or snapshot.get("smsm_settings_nav_active") is True),
            "smsm_condition_device_nav_inactive": probe_complete and (settings_count == 0 or snapshot.get("smsm_device_nav_active") is False),
            "smsm_condition_ios_settings_unique": probe_complete and ios_count == 1,
            "smsm_condition_ios_settings_active": probe_complete and snapshot.get("smsm_ios_settings_active") is True,
            "smsm_condition_android_settings_inactive": probe_complete and snapshot.get("smsm_android_settings_active") is False,
            "smsm_condition_client_certificate_menu_unique": probe_complete and (menu_count == 0 or menu_count == 1),
            "smsm_condition_client_certificate_menu_active": probe_complete and (menu_count == 0 or snapshot.get("smsm_client_certificate_menu_active") is True),
            "smsm_condition_search_input_unique": probe_complete and search_count == 1,
            "smsm_condition_add_icon_present": probe_complete and add_count is not None and add_count >= 1,
            "smsm_condition_pathname_matches": probe_complete and pathname_matches is True,
            "smsm_condition_settings_nav_consistent_if_observed": probe_complete and settings_consistent,
            "smsm_condition_client_certificate_menu_consistent_if_observed": probe_complete and menu_consistent,
        }
        conditions["smsm_condition_page_specific_landmarks_verified"] = all(conditions[key] for key in (
            "smsm_condition_pathname_matches",
            "smsm_condition_ios_settings_unique", "smsm_condition_ios_settings_active",
            "smsm_condition_android_settings_inactive", "smsm_condition_search_input_unique",
            "smsm_condition_add_icon_present",
        )) and probe_complete
        conditions["smsm_client_certificate_page_live_verified"] = conditions["smsm_condition_page_specific_landmarks_verified"] and conditions["smsm_condition_settings_nav_consistent_if_observed"] and conditions["smsm_condition_client_certificate_menu_consistent_if_observed"]
        return {
            "smsm_settings_nav_candidate_count": settings_count,
            "smsm_settings_nav_active": snapshot.get("smsm_settings_nav_active") is True,
            "smsm_device_nav_active": snapshot.get("smsm_device_nav_active") is True,
            "smsm_ios_settings_candidate_count": ios_count,
            "smsm_ios_settings_active": snapshot.get("smsm_ios_settings_active") is True,
            "smsm_android_settings_active": snapshot.get("smsm_android_settings_active") is True,
            "smsm_client_certificate_menu_candidate_count": menu_count,
            "smsm_client_certificate_menu_active": snapshot.get("smsm_client_certificate_menu_active") is True,
            "smsm_search_input_global_count": snapshot.get("smsm_search_input_global_count") if type(snapshot.get("smsm_search_input_global_count")) is int else None,
            "smsm_search_input_inside_center_content_count": snapshot.get("smsm_search_input_inside_center_content_count") if type(snapshot.get("smsm_search_input_inside_center_content_count")) is int else None,
            "smsm_search_input_inside_certificate_toolbar_count": snapshot.get("smsm_search_input_inside_certificate_toolbar_count") if type(snapshot.get("smsm_search_input_inside_certificate_toolbar_count")) is int else None,
            "smsm_search_input_after_exclusion_count": snapshot.get("smsm_search_input_after_exclusion_count") if type(snapshot.get("smsm_search_input_after_exclusion_count")) is int else None,
            "smsm_certificate_search_input_candidate_count": search_count,
            "smsm_certificate_add_icon_candidate_count": add_count,
            "smsm_client_certificate_page_live_verified": conditions["smsm_client_certificate_page_live_verified"],
            **conditions,
        }

    def _strict_client_certificate_page_state_legacy(self, driver, expected_path: str) -> dict[str, object]:
        """Retained legacy probe implementation for source compatibility."""
        try:
            probe_state = {
                "smsm_strict_page_probe_called": True,
                "smsm_strict_page_probe_completed": False,
                "smsm_strict_page_probe_exception_type": "",
                "smsm_strict_page_probe_snapshot_available": False,
            }
            observation = driver.execute_script(
                """
                const visible = item => Boolean(item && (item.offsetWidth || item.offsetHeight || item.getClientRects().length)) && !item.hidden && getComputedStyle(item).display !== 'none' && getComputedStyle(item).visibility !== 'hidden';
                const text = item => String((item && (item.innerText || item.textContent || item.getAttribute('aria-label') || item.getAttribute('title'))) || '').replace(/\\s+/g, ' ').trim();
                const active = item => Boolean(item && (item.getAttribute('aria-current') || item.getAttribute('aria-selected') || /active|selected/i.test(item.className || '')));
                const interactive = item => Boolean(item && (item.matches('a,button,[role="link"],[role="button"],[role="menuitem"]') || item.getAttribute('tabindex') !== null));
                const owner = item => { let current = item; for (let depth = 0; depth < 6 && current; depth += 1, current = current.parentElement) if (interactive(current)) return current; return null; };
                const attrStats = list => ({a: list.filter(item => item.tagName === 'A').length, button: list.filter(item => item.tagName === 'BUTTON').length, role: list.filter(item => Boolean(item.getAttribute('role'))).length, ariaCurrent: list.filter(item => Boolean(item.getAttribute('aria-current'))).length, ariaSelected: list.filter(item => Boolean(item.getAttribute('aria-selected'))).length, activeClass: list.filter(item => /active|selected/i.test(item.className || '')).length, parentActive: list.filter(item => active(item.parentElement)).length, href: list.filter(item => Boolean(item.getAttribute('href'))).length});
                const nodes = Array.from(document.querySelectorAll('a,button,[role="link"],[role="button"],[role="menuitem"],span,div,li'));
                const normalized = value => String(value || '').replace(/[\\s\\u00a0]+/g, '').toLowerCase();
                const structuralLabel = item => [text(item), item.getAttribute('aria-label'), item.getAttribute('title'), item.getAttribute('data-testid'), item.getAttribute('id')].map(normalized).join('|');
                const labelled = expression => nodes.filter(item => visible(item) && expression.test(structuralLabel(item))).map(owner).filter(Boolean).filter((item, index, all) => all.indexOf(item) === index);
                const rawLabelled = expression => nodes.filter(item => visible(item) && expression.test(structuralLabel(item)));
                const settingsRaw = rawLabelled(/設定|settings/i);
                const devicesRaw = rawLabelled(/機器|devices?/i);
                const iosRaw = rawLabelled(/(^|\\|)ios($|\\|)/i);
                const androidRaw = rawLabelled(/android/i);
                const certificateRaw = rawLabelled(/クライアント証明書管理|clientcertificatemanagement/i);
                const settings = labelled(/設定|settings/i);
                const devices = labelled(/機器|devices?/i);
                const ios = labelled(/(^|\\|)ios($|\\|)/i);
                const android = labelled(/android/i);
                const certificateMenu = labelled(/クライアント証明書管理|clientcertificatemanagement/i);
                const settingsActive = settings.length === 1 && active(settings[0]);
                const deviceActive = devices.some(active);
                const iosActive = ios.length === 1 && active(ios[0]);
                const androidActive = android.some(active);
                const menuActive = certificateMenu.length === 1 && active(certificateMenu[0]);
                const roots = Array.from(document.querySelectorAll('main,[role="main"],[data-testid*="content" i],[id*="content" i],[class*="content" i]')).filter(visible);
                const central = roots.length ? roots : [document.body];
                const inCentral = item => central.some(root => root.contains(item)) && !item.closest('nav,aside,[role="navigation"]');
                const inputNodes = Array.from(document.querySelectorAll('input')).filter(item => visible(item) && inCentral(item));
                const searchInputs = inputNodes.filter(item => !/file|password|checkbox|radio/i.test(item.type || '') && /search|検索|証明書/i.test([item.getAttribute('aria-label'), item.getAttribute('placeholder'), item.getAttribute('name'), item.id, item.getAttribute('data-testid')].filter(Boolean).join(' ')));
                const addNodes = Array.from(document.querySelectorAll('button,a,[role="button"],[role="link"],[aria-label],[data-testid],[class]')).filter(item => visible(item) && inCentral(item) && !item.closest('tr,[role="row"]'));
                const addIcons = addNodes.filter(item => /add|plus|追加|新規/i.test([text(item), item.getAttribute('aria-label'), item.getAttribute('title'), item.getAttribute('data-testid'), item.className].filter(Boolean).join(' ')));
                const attrStats = list => ({a_count: list.filter(item => item.tagName === 'A').length, button_count: list.filter(item => item.tagName === 'BUTTON').length, role_count: list.filter(item => Boolean(item.getAttribute('role'))).length, aria_current_count: list.filter(item => Boolean(item.getAttribute('aria-current'))).length, aria_selected_count: list.filter(item => Boolean(item.getAttribute('aria-selected'))).length, active_class_count: list.filter(item => /active|selected/i.test(item.className || '')).length, parent_active_count: list.filter(item => active(item.parentElement)).length, href_path_count: list.filter(item => Boolean(item.getAttribute('href'))).length});
                const inputStats = {input_type_text_count: searchInputs.filter(item => (item.type || '').toLowerCase() === 'text').length, input_type_search_count: searchInputs.filter(item => (item.type || '').toLowerCase() === 'search').length, placeholder_present_count: searchInputs.filter(item => Boolean(item.getAttribute('placeholder'))).length, aria_label_present_count: searchInputs.filter(item => Boolean(item.getAttribute('aria-label'))).length};
                const currentPath = location.pathname.replace(/\\/$/, '') || '/';
                const expected = String(arguments[0] || '').replace(/\\/$/, '') || '/';
                const pathnameMatches = currentPath === expected;
                const verified = settingsActive && !deviceActive && iosActive && !androidActive && menuActive && searchInputs.length === 1 && addIcons.length >= 1 && pathnameMatches;
                return {
                    smsm_settings_nav_candidate_count: settings.length,
                    smsm_settings_nav_raw_match_count: settingsRaw.length,
                    smsm_settings_nav_tag_a_count: attrStats(settings).a,
                    smsm_settings_nav_tag_button_count: attrStats(settings).button,
                    smsm_settings_nav_role_attribute_count: attrStats(settings).role,
                    smsm_settings_nav_active_attribute_count: attrStats(settings).ariaCurrent + attrStats(settings).ariaSelected + attrStats(settings).activeClass,
                    smsm_settings_nav_parent_active_count: attrStats(settings).parentActive,
                    smsm_settings_nav_href_present_count: attrStats(settings).href,
                    smsm_settings_nav_unique: settings.length === 1,
                    smsm_settings_nav_click_called: false,
                    smsm_settings_nav_click_count: 0,
                    smsm_settings_nav_active: settingsActive,
                    smsm_device_nav_active: deviceActive,
                    smsm_ios_settings_candidate_count: ios.length,
                    smsm_ios_settings_raw_match_count: iosRaw.length,
                    smsm_ios_settings_tag_a_count: attrStats(ios).a,
                    smsm_ios_settings_tag_button_count: attrStats(ios).button,
                    smsm_ios_settings_role_attribute_count: attrStats(ios).role,
                    smsm_ios_settings_active_attribute_count: attrStats(ios).ariaCurrent + attrStats(ios).ariaSelected + attrStats(ios).activeClass,
                    smsm_ios_settings_parent_active_count: attrStats(ios).parentActive,
                    smsm_ios_settings_href_present_count: attrStats(ios).href,
                    smsm_ios_settings_unique: ios.length === 1,
                    smsm_ios_settings_click_called: false,
                    smsm_ios_settings_click_count: 0,
                    smsm_ios_settings_active: iosActive,
                    smsm_android_settings_active: androidActive,
                    smsm_android_settings_raw_match_count: androidRaw.length,
                    smsm_client_certificate_menu_candidate_count: certificateMenu.length,
                    smsm_client_certificate_menu_raw_match_count: certificateRaw.length,
                    smsm_client_certificate_menu_tag_a_count: attrStats(certificateMenu).a,
                    smsm_client_certificate_menu_tag_button_count: attrStats(certificateMenu).button,
                    smsm_client_certificate_menu_role_attribute_count: attrStats(certificateMenu).role,
                    smsm_client_certificate_menu_active_attribute_count: attrStats(certificateMenu).ariaCurrent + attrStats(certificateMenu).ariaSelected + attrStats(certificateMenu).activeClass,
                    smsm_client_certificate_menu_parent_active_count: attrStats(certificateMenu).parentActive,
                    smsm_client_certificate_menu_href_present_count: attrStats(certificateMenu).href,
                    smsm_client_certificate_menu_unique: certificateMenu.length === 1,
                    smsm_client_certificate_menu_click_called: false,
                    smsm_client_certificate_menu_click_count: 0,
                    smsm_client_certificate_menu_active: menuActive,
                    smsm_certificate_search_input_candidate_count: searchInputs.length,
                    smsm_certificate_search_input_type_text_count: inputStats.input_type_text_count,
                    smsm_certificate_search_input_type_search_count: inputStats.input_type_search_count,
                    smsm_certificate_search_input_placeholder_present_count: inputStats.placeholder_present_count,
                    smsm_certificate_search_input_aria_label_present_count: inputStats.aria_label_present_count,
                    smsm_certificate_add_icon_candidate_count: addIcons.length,
                    smsm_certificate_add_icon_raw_candidate_count: addNodes.filter(item => /add|plus|追加|新規/i.test([item.getAttribute('aria-label'), item.getAttribute('title'), item.getAttribute('data-testid'), item.className].filter(Boolean).join(' '))).length,
                    smsm_certificate_add_icon_tag_a_count: attrStats(addIcons).a,
                    smsm_certificate_add_icon_tag_button_count: attrStats(addIcons).button,
                    smsm_certificate_add_icon_role_attribute_count: attrStats(addIcons).role,
                    smsm_certificate_add_icon_active_attribute_count: attrStats(addIcons).ariaCurrent + attrStats(addIcons).ariaSelected + attrStats(addIcons).activeClass,
                    smsm_certificate_add_icon_parent_active_count: attrStats(addIcons).parentActive,
                    smsm_certificate_add_icon_href_present_count: attrStats(addIcons).href,
                    smsm_certificate_pathname_matches: pathnameMatches,
                    smsm_client_certificate_page_live_verified: verified,
                    client_certificate_page_landmark_verified: verified,
                };
                """,
                expected_path,
            ) or {}
            probe_state["smsm_strict_page_probe_completed"] = True
            probe_state["smsm_strict_page_probe_snapshot_available"] = True
            observation.update(probe_state)
            verified = self._strict_client_certificate_page_verified(observation)
            observation["smsm_client_certificate_page_live_verified"] = verified
            observation["client_certificate_page_landmark_verified"] = verified
            return observation
        except Exception as exc:
            fallback = {
                "smsm_strict_page_probe_called": True,
                "smsm_strict_page_probe_completed": False,
                "smsm_strict_page_probe_exception_type": type(exc).__name__,
                "smsm_strict_page_probe_snapshot_available": False,
                "smsm_settings_nav_observed": False,
                "smsm_ios_settings_observed": False,
                "smsm_client_certificate_menu_observed": False,
                "smsm_certificate_search_input_observed": False,
                "smsm_certificate_add_icon_observed": False,
                "smsm_dom_probe_completed": False,
                "smsm_dom_probe_exception_type": "javascript_or_driver_error",
                "smsm_settings_nav_candidate_count": 0,
                "smsm_settings_nav_raw_match_count": 0,
                "smsm_settings_nav_unique": False,
                "smsm_settings_nav_click_called": False,
                "smsm_settings_nav_click_count": 0,
                "smsm_settings_nav_active": False,
                "smsm_device_nav_active": False,
                "smsm_ios_settings_candidate_count": 0,
                "smsm_ios_settings_raw_match_count": 0,
                "smsm_ios_settings_unique": False,
                "smsm_ios_settings_click_called": False,
                "smsm_ios_settings_click_count": 0,
                "smsm_ios_settings_active": False,
                "smsm_android_settings_active": False,
                "smsm_android_settings_raw_match_count": 0,
                "smsm_client_certificate_menu_candidate_count": 0,
                "smsm_client_certificate_menu_raw_match_count": 0,
                "smsm_client_certificate_menu_unique": False,
                "smsm_client_certificate_menu_click_called": False,
                "smsm_client_certificate_menu_click_count": 0,
                "smsm_client_certificate_menu_active": False,
                "smsm_certificate_search_input_candidate_count": 0,
                "smsm_certificate_search_input_type_text_count": 0,
                "smsm_certificate_search_input_type_search_count": 0,
                "smsm_certificate_search_input_placeholder_present_count": 0,
                "smsm_certificate_search_input_aria_label_present_count": 0,
                "smsm_certificate_add_icon_candidate_count": 0,
                "smsm_certificate_add_icon_raw_candidate_count": 0,
                "smsm_certificate_pathname_matches": False,
                "smsm_client_certificate_page_live_verified": False,
                "client_certificate_page_landmark_verified": False,
            }
            for key in (
                "smsm_settings_nav_candidate_count", "smsm_ios_settings_candidate_count",
                "smsm_client_certificate_menu_candidate_count", "smsm_certificate_search_input_candidate_count",
                "smsm_certificate_add_icon_candidate_count", "smsm_certificate_pathname_matches",
            ):
                fallback.pop(key, None)
            return fallback

    @staticmethod
    def _strict_client_certificate_page_verified(observation: dict[str, object]) -> bool:
        settings_count = observation.get("smsm_settings_nav_candidate_count")
        menu_count = observation.get("smsm_client_certificate_menu_candidate_count")
        settings_consistent = settings_count is None or settings_count == 0 or (type(settings_count) is int and observation.get("smsm_settings_nav_active") is True and observation.get("smsm_device_nav_active") is False)
        menu_consistent = menu_count is None or menu_count == 0 or (type(menu_count) is int and observation.get("smsm_client_certificate_menu_active") is True)
        return all((
            settings_consistent,
            menu_consistent,
            observation.get("smsm_ios_settings_active") is True,
            observation.get("smsm_android_settings_active") is False,
            observation.get("smsm_certificate_search_input_candidate_count") == 1,
            type(observation.get("smsm_certificate_add_icon_candidate_count")) is int,
            observation.get("smsm_certificate_add_icon_candidate_count", 0) >= 1,
            observation.get("smsm_certificate_pathname_matches") is True,
        ))

    @staticmethod
    def _origin_from_url(parsed) -> str:
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = host
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        return urlunparse((parsed.scheme, netloc, "", "", "", ""))

    @staticmethod
    def _normalized_path(path: str) -> str:
        normalized = unquote(path or "")
        return normalized.rstrip("/") or "/"

    def navigate_certificate_route_for_diagnostic(self, manifest: list[dict[str, object]], trace=None) -> dict[str, object]:
        """Replay a validated route using one normal WebElement click per stage."""
        driver = self.browser.driver
        if driver is None:
            raise RuntimeError("SMSM画面を確認できません")
        if len(manifest) != 4:
            raise RuntimeError("SMSMナビゲーションrouteを検証できません")
        started_at = time.monotonic()
        stages = ("settings_navigation", "ios_navigation", "certificate_management_expand", "client_certificate_management_navigation")
        for index, stage in enumerate(stages):
            entry = manifest[index]
            if entry.get("stage") != stage:
                raise RuntimeError("SMSMナビゲーションrouteの段階が不正です")
            if stage == "certificate_management_expand" and self._child_menu_visible(driver):
                self._trace(trace, "certificate_management_expand_already_open", True)
                continue
            candidates = self._resolve_captured_route_candidates(entry)
            candidates = [item for item in candidates if self._safe_bool(item, "is_displayed") and self._safe_bool(item, "is_enabled") and not self._safe_bool_attribute(item, "disabled")]
            if len(candidates) != 1:
                raise RuntimeError(f"採取済みrouteの候補数が不正です: {stage}")
            element = candidates[0]
            try:
                element.click()
            except Exception as click_error:
                path = entry.get("same_host_path")
                if not path or not self._is_same_host_path(path):
                    raise RuntimeError("確認済みSMSM pathnameへ移動できません") from click_error
                driver.get(self._same_host_url(path))
            if stage == "settings_navigation":
                verified = self._route_ios_state(driver)["ios_tab_selected"]
            elif stage == "ios_navigation":
                verified = self._route_landmark_present(driver, "certificate_management")
            elif stage == "certificate_management_expand":
                verified = self._child_menu_visible(driver)
            else:
                verified = self._route_landmark_present(driver, "client_certificate_management")
            if not verified:
                raise RuntimeError(f"採取済みrouteのランドマークを確認できません: {stage}")
            self._trace(trace, f"{stage}_menu_click_called", True)
        upload_started_at = time.monotonic()
        result = self._inspect_client_certificate_upload_dom()
        result.update(self._client_certificate_page_landmark_state(driver))
        self._trace_elapsed(trace, "upload_dom_inspection", upload_started_at)
        self._trace_elapsed(trace, "certificate_navigation_total", started_at)
        return result

    def _install_route_capture_monitor(self) -> None:
        self.browser.driver.execute_script("""
            (() => {
              if (window.__smsmRouteCapture) return;
              const queue = [];
              const snapshot = target => {
                const item = target && target.closest ? (target.closest('a,button,[role="link"],[role="button"],[role="menuitem"]') || target) : target;
                const attrs = key => item && item.getAttribute ? item.getAttribute(key) : null;
                const rect = item && item.getBoundingClientRect ? item.getBoundingClientRect() : null;
                return {tag: item && item.tagName ? item.tagName.toLowerCase() : '', id: attrs('id'), name: attrs('name'), role: attrs('role'), data_testid: attrs('data-testid'), aria_label: attrs('aria-label'), title: attrs('title'), href: attrs('href'), class_name: attrs('class'), parent_tag: item && item.parentElement ? item.parentElement.tagName.toLowerCase() : '', sibling_count: item && item.parentElement ? item.parentElement.children.length : 0, dom_position: item && item.parentElement ? Array.prototype.indexOf.call(item.parentElement.children, item) : -1, pathname_before: location.pathname, origin_before: location.origin, landmarks_before: landmarks(), rect_left: rect ? rect.left : 0, rect_top: rect ? rect.top : 0};
              };
              const visible = item => Boolean(item && (item.offsetWidth || item.offsetHeight || item.getClientRects().length)) && !item.hidden && getComputedStyle(item).visibility !== 'hidden' && getComputedStyle(item).display !== 'none';
              const childItems = parent => Array.from(parent ? parent.parentElement ? parent.parentElement.querySelectorAll('a,button,[role="link"],[role="menuitem"]') : [] : []).filter(item => /クライアント証明書管理|Client certificate management/i.test(item.innerText || item.getAttribute('aria-label') || '') && visible(item));
              const accordionState = target => { const parent = target && target.closest ? (target.closest('[aria-expanded], [data-testid*="certificate" i], [id*="certificate" i], [class*="certificate" i]') || target) : target; const children = childItems(parent); const container = parent && parent.parentElement ? parent.parentElement : parent; return {parent_aria_expanded: parent ? parent.getAttribute('aria-expanded') : null, visible_child_menu_count: children.length, child_container_visible: visible(container), parent_expanded_class_present: Boolean(parent && /expanded|open|active|selected/i.test(parent.className || '')), parent_tag: parent && parent.tagName ? parent.tagName.toLowerCase() : ''}; };
              const landmarks = () => ({ios: Boolean(document.querySelector('[data-testid*="ios" i], [id*="ios" i], [class*="ios" i]')) || /iOS/.test(document.body ? document.body.innerText : ''), certificate_management: Boolean(document.querySelector('[data-testid*="certificate" i], [id*="certificate" i], [class*="certificate" i]')) || /証明書管理|Certificate management/i.test(document.body ? document.body.innerText : ''), client_certificate_management: childItems(document.body).length > 0 || Boolean(document.querySelector('input[type="file"], input[type="password"], form, table, [data-testid*="certificate-list" i]')) || /クライアント証明書管理|Client certificate management/i.test(document.body ? document.body.innerText : ''), settings: Boolean(document.querySelector('[data-testid*="settings" i], [id*="settings" i], [class*="settings" i]')) || /設定|Settings/i.test(document.body ? document.body.innerText : '')});
              const click = event => { const record = snapshot(event.target); record.type = 'click'; record.accordion_before = accordionState(event.target); setTimeout(() => { const after = accordionState(event.target); record.pathname_after = location.pathname; record.origin_after = location.origin; record.after_ready = true; record.landmarks = landmarks(); record.accordion_after = after; record.pathname_changed = record.pathname_before !== record.pathname_after; record.aria_expanded_changed = record.accordion_before.parent_aria_expanded !== record.accordion_after.parent_aria_expanded; record.child_menu_became_visible = record.accordion_before.visible_child_menu_count === 0 && record.accordion_after.visible_child_menu_count > 0; record.child_container_visibility_changed = record.accordion_before.child_container_visible !== record.accordion_after.child_container_visible; record.expanded_state_changed = record.accordion_before.parent_expanded_class_present !== record.accordion_after.parent_expanded_class_present; record.accordion_expand_verified = record.child_menu_became_visible || record.aria_expanded_changed || record.child_container_visibility_changed || record.expanded_state_changed; record.action_type = record.accordion_expand_verified ? 'accordion_expand' : record.pathname_changed ? 'navigation' : 'unresolved_click'; queue.push(record); }, 0); };
              const pointerup = event => queue.push({type: 'pointerup', pathname: location.pathname, target_tag: event.target && event.target.tagName ? event.target.tagName.toLowerCase() : ''});
              const route = type => queue.push({type, pathname: location.pathname});
              document.addEventListener('click', click, true); document.addEventListener('pointerup', pointerup, true); window.addEventListener('popstate', () => route('popstate')); window.addEventListener('hashchange', () => route('hashchange'));
              const originalPush = history.pushState; const originalReplace = history.replaceState;
              history.pushState = function(...args) { const result = originalPush.apply(this, args); route('history.pushState'); return result; };
              history.replaceState = function(...args) { const result = originalReplace.apply(this, args); route('history.replaceState'); return result; };
              window.__smsmRouteCapture = {drain: () => queue.splice(0, queue.length), pending: () => queue.length, remove: () => { document.removeEventListener('click', click, true); document.removeEventListener('pointerup', pointerup, true); history.pushState = originalPush; history.replaceState = originalReplace; window.__smsmRouteCapture = null; }};
            })();
        """)

    def _remove_route_capture_monitor(self, driver) -> None:
        try:
            driver.execute_script("if (window.__smsmRouteCapture) window.__smsmRouteCapture.remove();")
        except Exception:
            pass

    def _route_landmark_present(self, driver, kind: str) -> bool:
        try:
            return bool(driver.execute_script("""const kind = arguments[0]; const text = document.body ? document.body.innerText : ''; if (kind === 'settings') return /設定|Settings/i.test(text) || Boolean(document.querySelector('[data-testid*="settings" i], [id*="settings" i], [class*="settings" i]')); if (kind === 'ios') return /iOS/i.test(text) || Boolean(document.querySelector('[data-testid*="ios" i], [id*="ios" i], [class*="ios" i]')); if (kind === 'certificate_management') return /証明書管理|Certificate management/i.test(text) || Boolean(document.querySelector('[data-testid*="certificate" i], [id*="certificate" i], [class*="certificate" i]')); return Boolean(document.querySelector('form, input[type="file"], input[type="password"], table')) || /クライアント証明書管理|Client certificate management/i.test(text);""", kind))
        except Exception:
            return False

    def _route_ios_state(self, driver) -> dict[str, bool]:
        try:
            return driver.execute_script("""
                const tabs = Array.from(document.querySelectorAll('[role="tab"],a,button')).filter(item => /iOS|Android/i.test(item.innerText || item.getAttribute('aria-label') || item.getAttribute('title') || ''));
                const selected = item => item && (item.getAttribute('aria-selected') === 'true' || item.getAttribute('aria-current') === 'page' || /active|selected/i.test(item.className || ''));
                return {target_os_ios: true, ios_tab_selected: tabs.some(item => /iOS/i.test(item.innerText || item.getAttribute('aria-label') || '') && selected(item)), android_tab_selected: tabs.some(item => /Android/i.test(item.innerText || item.getAttribute('aria-label') || '') && selected(item))};
            """) or {"target_os_ios": True, "ios_tab_selected": False, "android_tab_selected": False}
        except Exception:
            return {"target_os_ios": True, "ios_tab_selected": False, "android_tab_selected": False}

    def _child_menu_visible(self, driver) -> bool:
        try:
            return bool(driver.execute_script("return Array.from(document.querySelectorAll('a,button,[role=\\\"link\\\"],[role=\\\"menuitem\\\"]')).filter(item => /クライアント証明書管理|Client certificate management/i.test(item.innerText || item.getAttribute('aria-label') || '') && Boolean(item.offsetWidth || item.offsetHeight || item.getClientRects().length) && !item.hidden && getComputedStyle(item).display !== 'none' && getComputedStyle(item).visibility !== 'hidden').length > 0;"))
        except Exception:
            return False

    def _client_certificate_page_landmark_state(self, driver) -> dict[str, object]:
        try:
            return driver.execute_script("""
                const visible = item => Boolean(item && (item.offsetWidth || item.offsetHeight || item.getClientRects().length)) && !item.hidden && getComputedStyle(item).display !== 'none' && getComputedStyle(item).visibility !== 'hidden';
                const textOf = item => `${item.innerText || ''} ${item.getAttribute('aria-label') || ''} ${item.getAttribute('title') || ''}`;
                const allInteractive = Array.from(document.querySelectorAll('a,button,[role], [onclick], [data-href], [data-url], [data-route], [aria-selected], [aria-current], [data-selected], [data-active], [data-state]'));
                const ancestors = item => { const result = [item]; let current = item; for (let index = 0; index < 5 && current && current.parentElement; index += 1) { current = current.parentElement; result.push(current); } return result; };
                const descendants = item => item ? Array.from(item.querySelectorAll('a,button,[role="link"],[role="button"],[role="menuitem"],[href],[data-href],[data-url],[data-route]')) : [];
                const chain = item => item ? [...ancestors(item), ...descendants(item)] : [];
                const valueAcross = (item, name) => chain(item).map(node => node.getAttribute && node.getAttribute(name)).filter(Boolean);
                const classSelected = item => chain(item).some(node => /(^|\\s)(active|selected|is-active|is-selected|current|chosen)(\\s|$)/i.test(node.className || ''));
                const attributeSelected = item => chain(item).some(node => node.getAttribute('aria-selected') === 'true' || node.getAttribute('aria-current') === 'page' || node.getAttribute('data-selected') === 'true' || node.getAttribute('data-active') === 'true' || node.getAttribute('data-state') === 'active' || node.getAttribute('aria-expanded') === 'true');
                const styleSelected = item => chain(item).some(node => { const style = getComputedStyle(node); return (style.borderBottomWidth !== '0px' && style.borderBottomStyle !== 'none') || /underline/i.test(style.textDecorationLine || style.textDecoration || '') || (/^(bold|[6-9]00)$/.test(style.fontWeight || '') || Number(style.fontWeight) >= 600); });
                const selfClassSelected = item => /(^|\\s)(active|selected|is-active|is-selected|current|chosen)(\\s|$)/i.test(item.className || '');
                const selfAttributeSelected = item => item.getAttribute('aria-selected') === 'true' || item.getAttribute('aria-current') === 'page' || item.getAttribute('data-selected') === 'true' || item.getAttribute('data-active') === 'true' || item.getAttribute('data-state') === 'active';
                const selfStyleSelected = item => { const style = getComputedStyle(item); return (style.borderBottomWidth !== '0px' && style.borderBottomStyle !== 'none') || /underline/i.test(style.textDecorationLine || style.textDecoration || ''); };
                const hrefValue = item => { for (const node of chain(item)) { const raw = node.getAttribute && (node.getAttribute('href') || node.getAttribute('data-href') || node.getAttribute('data-url') || node.getAttribute('data-route')); if (raw) return raw; } const onclick = valueAcross(item, 'onclick').join(' '); const match = onclick.match(/(?:['"])(\\/[^'"?#]+)/); return match ? match[1] : ''; };
                const hrefPath = raw => { try { return raw ? new URL(raw, location.href).pathname : ''; } catch (_) { return ''; } };
                const labelMatch = (item, expression) => expression.test(textOf(item)) || chain(item).some(node => expression.test(textOf(node)));
                const iosTabs = allInteractive.filter(item => labelMatch(item, /(^|\\s)iOS(\\s|$)/i));
                const androidTabs = allInteractive.filter(item => labelMatch(item, /Android/i));
                const contentFor = name => Array.from(document.querySelectorAll('[id],[data-testid],[class]')).filter(item => visible(item) && new RegExp(name, 'i').test(`${item.id || ''} ${item.getAttribute('data-testid') || ''} ${item.className || ''}`) && !/tab/i.test(`${item.id || ''} ${item.getAttribute('data-testid') || ''} ${item.className || ''}`));
                const iosContent = contentFor('ios');
                const androidContent = contentFor('android');
                const iosByAttribute = iosTabs.some(item => visible(item) && selfAttributeSelected(item) || attributeSelected(item));
                const iosByClass = iosTabs.some(item => visible(item) && (selfClassSelected(item) || classSelected(item)));
                const iosByIndicator = iosTabs.some(item => visible(item) && (selfStyleSelected(item) || styleSelected(item)));
                const androidByAttribute = androidTabs.some(item => visible(item) && (selfAttributeSelected(item) || attributeSelected(item)));
                const androidByClass = androidTabs.some(item => visible(item) && (selfClassSelected(item) || classSelected(item)));
                const iosContentVisible = iosContent.length > 0;
                const androidContentVisible = androidContent.length > 0;
                const iosSelected = Boolean((iosByAttribute || iosByClass || iosByIndicator) && iosContentVisible && !androidContentVisible);
                const androidSelected = Boolean((androidByAttribute || androidByClass) && androidContentVisible && !iosContentVisible);

                const normalizeText = value => String(value || '').replace(/\\s+/g, ' ').trim();
                const exactChildLabel = item => {
                    const directText = normalizeText(Array.from(item.childNodes || []).filter(node => node.nodeType === Node.TEXT_NODE).map(node => node.nodeValue || '').join(' '));
                    const labels = [directText, normalizeText(item.getAttribute('aria-label')), normalizeText(item.getAttribute('title'))];
                    return labels.some(value => value === 'クライアント証明書管理' || value.toLowerCase() === 'client certificate management');
                };
                const clickable = item => item && (item.matches('a,button,[role="link"],[role="button"],[role="menuitem"],[onclick]') || (item.hasAttribute('tabindex') && typeof item.click === 'function'));
                const clickableOwner = item => { let current = item; for (let depth = 0; depth < 6 && current; depth += 1, current = current.parentElement) { if (clickable(current)) return current; } return null; };
                const navigationRoot = item => { let current = item; for (let depth = 0; depth < 8 && current; depth += 1, current = current.parentElement) { const role = current.getAttribute && current.getAttribute('role'); const landmark = `${current.id || ''} ${current.getAttribute && current.getAttribute('data-testid') || ''} ${current.className || ''}`; if (role === 'navigation' || /sidebar|side-nav|sidenav|drawer|navigation|menu/i.test(landmark)) return current; } return null; };
                const rawChildMatches = Array.from(document.querySelectorAll('a,button,[role="link"],[role="button"],[role="menuitem"],[onclick],span,div,label')).filter(item => exactChildLabel(item));
                const visibleChildMatches = rawChildMatches.filter(visible);
                const hiddenMatchCount = rawChildMatches.length - visibleChildMatches.length;
                const childOwners = visibleChildMatches.map(clickableOwner).filter(Boolean);
                const ownerSet = new Set(childOwners);
                const clickableResolutionCount = childOwners.length;
                const inScopeOwners = Array.from(ownerSet).filter(item => { const root = navigationRoot(item); return root && visible(root); });
                const navigationRoots = Array.from(new Set(inScopeOwners.map(navigationRoot)));
                const navRoot = navigationRoots.length === 1 ? navigationRoots[0] : null;
                const childCandidates = navRoot ? inScopeOwners.filter(item => navigationRoot(item) === navRoot) : [];
                const childVisibleItems = childCandidates.filter(visible);
                const child = childCandidates.length === 1 ? childCandidates[0] : null;
                const leftNavigationMatches = navRoot ? rawChildMatches.filter(item => navRoot.contains(item)).length : 0;
                const outsideNavigationMatches = rawChildMatches.filter(item => !navRoot || !navRoot.contains(item)).length;
                const deduplicatedClickableCandidateCount = childCandidates.length;
                const descendantTextMatchCount = rawChildMatches.filter(item => item !== clickableOwner(item)).length;
                const childActiveSelf = Boolean(child && (selfClassSelected(child) || selfAttributeSelected(child)));
                const childActiveAncestor = Boolean(child && ancestors(child).slice(1).some(node => selfClassSelected(node) || selfAttributeSelected(node)));
                const childActiveByClass = Boolean(child && classSelected(child));
                const childActiveByAttribute = Boolean(child && attributeSelected(child));
                const childActiveByStyle = Boolean(child && styleSelected(child));
                const childActiveSemantic = Boolean(child && (childActiveSelf || childActiveAncestor || childActiveByClass || childActiveByAttribute));
                const childActive = Boolean(child && (childActiveSemantic || childActiveByStyle));
                const childHrefRaw = child ? hrefValue(child) : '';
                const childPath = hrefPath(childHrefRaw);
                const currentPath = location.pathname;
                const currentPathMatchesChild = Boolean(childPath && childPath === currentPath);
                const childHrefUrl = (() => { try { return childHrefRaw ? new URL(childHrefRaw, location.href) : null; } catch (_) { return null; } })();
                const parentCandidates = navRoot ? Array.from(navRoot.querySelectorAll('a,button,[role="link"],[role="button"],[role="menuitem"],[onclick],span,div,label')).filter(item => {
                    const labels = [normalizeText(item.innerText), normalizeText(item.textContent), normalizeText(item.getAttribute('aria-label')), normalizeText(item.getAttribute('title'))];
                    return labels.some(value => value === '証明書管理' || value.toLowerCase() === 'certificate management');
                }).map(clickableOwner).filter(Boolean) : [];
                const parent = parentCandidates.find(visible) || null;
                const parentChain = parent ? chain(parent) : [];
                const expandedByAttribute = Boolean(parent && (parentChain.some(node => node.getAttribute('aria-expanded') === 'true' || node.getAttribute('data-expanded') === 'true') || parentChain.some(node => /(^|\\s)(expanded|open|is-open)(\\s|$)/i.test(node.className || ''))));
                const expandedByVisibleChild = childCandidates.length === 1 && childVisibleItems.length === 1;
                const expanded = Boolean(expandedByAttribute || expandedByVisibleChild);

                const specificNodes = Array.from(document.querySelectorAll('[id],[data-testid],[class]')).filter(item => visible(item) && /client[-_ ]?certificate|certificate[-_ ]?(management|list|row)/i.test(`${item.id || ''} ${item.getAttribute('data-testid') || ''} ${item.className || ''}`));
                const childContainer = child && (ancestors(child).find(node => /client[-_ ]?certificate|certificate[-_ ]?(management|list|row)/i.test(`${node.id || ''} ${node.getAttribute('data-testid') || ''} ${node.className || ''}`)) || child.parentElement);
                const contentRoots = Array.from(document.querySelectorAll('main,[role="main"],[data-testid*="content" i],[id*="content" i],[class*="content" i]')).filter(item => visible(item) && (!navRoot || !navRoot.contains(item)) && !/modal|dialog|drawer/i.test(`${item.id || ''} ${item.className || ''}`));
                const panels = contentRoots.length > 0 ? contentRoots : Array.from(document.querySelectorAll('main,[role="main"],section')).filter(item => visible(item) && (!navRoot || !navRoot.contains(item)));
                const hasSearch = item => Boolean(item.querySelector('input[placeholder*="search" i], input[aria-label*="search" i], input[placeholder*="検索"], input[aria-label*="検索"], [data-testid*="search" i], [aria-label*="検索"]'));
                const hasAdd = item => Boolean(item.querySelector('[aria-label*="add" i], [aria-label*="追加"], [data-testid*="add" i], [data-testid*="plus" i], [class*="plus" i]'));
                const hasDropdown = item => Boolean(item.querySelector('select, [aria-haspopup="listbox"], [aria-haspopup="menu"], [aria-label*="dropdown" i], [aria-label*="下向き"], [data-testid*="dropdown" i]'));
                const hasPaging = item => Boolean(item.querySelector('[aria-label*="page" i], [class*="pagination" i], [class*="paging" i], [data-testid*="page" i]'));
                const hasCertificateList = item => Boolean(item.querySelector('[data-testid*="certificate-list" i], [id*="certificate-list" i], [class*="certificate-list" i], [data-testid*="certificate-row" i], [id*="certificate-row" i], [class*="certificate-row" i], table'));
                const operationPanel = panels.filter(item => hasSearch(item) && hasAdd(item) && hasDropdown(item));
                const listPanel = panels.filter(item => hasCertificateList(item) && hasPaging(item));
                const centralHasSearchInput = panels.some(hasSearch);
                const centralHasSearchButton = panels.some(item => Boolean(item.querySelector('button[aria-label*="search" i], button[aria-label*="検索"], [data-testid*="search-button" i], [class*="search" i]')));
                const centralHasUniqueAdd = panels.some(item => item.querySelectorAll('[aria-label*="add" i],[aria-label*="追加"],[data-testid*="add" i],[data-testid*="plus" i],[class*="plus" i]').length === 1);
                const centralHasDropdown = panels.some(hasDropdown);
                const centralHasList = panels.some(hasCertificateList);
                const centralHasCheckbox = panels.some(item => item.querySelector('input[type="checkbox"], [role="checkbox"]'));
                const centralHasPaging = panels.some(hasPaging);
                const certificateStructure = Boolean(centralHasSearchInput && centralHasSearchButton && centralHasUniqueAdd && centralHasDropdown && centralHasList && centralHasCheckbox && centralHasPaging);
                const specificLandmarks = [iosContentVisible, iosSelected, expanded, childVisibleItems.length > 0, childActive, currentPathMatchesChild, certificateStructure, specificNodes.length > 0];
                const specificCount = specificLandmarks.filter(Boolean).length;
                const inputs = Array.from(document.querySelectorAll('input,textarea')).filter(visible);
                const tables = Array.from(document.querySelectorAll('table')).filter(visible);
                const rows = Array.from(document.querySelectorAll('table tbody tr')).filter(visible);
                const fileInputs = inputs.filter(item => (item.getAttribute('type') || '').toLowerCase() === 'file');
                const passwordInputs = inputs.filter(item => (item.getAttribute('type') || '').toLowerCase() === 'password');
                const uploadForms = Array.from(document.querySelectorAll('form')).filter(form => visible(form) && fileInputs.some(input => form.contains(input)) && passwordInputs.some(input => form.contains(input)) && Boolean(form.querySelector('[data-testid*="client-certificate" i],[id*="client-certificate" i],[class*="client-certificate" i]')));
                const composite = Boolean(iosSelected && !androidSelected && parent && expanded && childVisibleItems.length > 0 && childActive && childPath && currentPathMatchesChild && specificCount >= 2);
                return {
                    target_os_ios_verified: iosSelected && !androidSelected,
                    ios_tab_candidate_count: iosTabs.length,
                    ios_tab_selected_by_attribute: iosByAttribute,
                    ios_tab_selected_by_class: iosByClass,
                    ios_tab_selected_by_indicator: iosByIndicator,
                    ios_content_container_visible: iosContentVisible,
                    android_tab_candidate_count: androidTabs.length,
                    android_tab_selected_by_attribute: androidByAttribute,
                    android_tab_selected_by_class: androidByClass,
                    android_content_container_visible: androidContentVisible,
                    ios_tab_selected: iosSelected,
                    android_tab_selected: androidSelected,
                    certificate_management_parent_found: Boolean(parent),
                    certificate_management_expanded_by_attribute: expandedByAttribute,
                    certificate_management_expanded_by_visible_child: expandedByVisibleChild,
                    certificate_management_expanded: expanded,
                    raw_text_match_count: rawChildMatches.length,
                    visible_text_match_count: visibleChildMatches.length,
                    left_navigation_match_count: leftNavigationMatches,
                    descendant_text_match_count: descendantTextMatchCount,
                    clickable_resolution_count: clickableResolutionCount,
                    deduplicated_clickable_candidate_count: deduplicatedClickableCandidateCount,
                    hidden_match_count: hiddenMatchCount,
                    outside_navigation_match_count: outsideNavigationMatches,
                    client_certificate_child_candidate_count: deduplicatedClickableCandidateCount,
                    client_certificate_child_visible: childVisibleItems.length > 0,
                    client_certificate_child_active_on_self: childActiveSelf,
                    client_certificate_child_active_on_ancestor: childActiveAncestor,
                    client_certificate_child_selected_by_class: childActiveByClass,
                    client_certificate_child_selected_by_attribute: childActiveByAttribute,
                    client_certificate_child_selected_by_style: childActiveByStyle,
                    client_certificate_child_active_semantic: childActiveSemantic,
                    client_certificate_child_active: childActive,
                    client_certificate_child_href_present: Boolean(childHrefRaw),
                    client_child_href_path_nonempty: Boolean(childPath),
                    client_child_href_has_query: Boolean(childHrefUrl && childHrefUrl.search),
                    client_child_href_has_fragment: Boolean(childHrefUrl && childHrefUrl.hash),
                    client_child_href_source: childHrefRaw ? (childPath === currentPath ? 'self' : 'unknown') : 'unknown',
                    client_child_href_normalized_match: currentPathMatchesChild,
                    current_path_matches_client_certificate_child: currentPathMatchesChild,
                    current_path_verified_by_manual_checkpoint: false,
                    client_certificate_specific_landmark_count: specificCount,
                    certificate_operation_structure_verified: certificateStructure,
                    central_search_input_present: centralHasSearchInput,
                    central_search_button_present: centralHasSearchButton,
                    central_unique_add_present: centralHasUniqueAdd,
                    central_dropdown_present: centralHasDropdown,
                    central_certificate_list_present: centralHasList,
                    central_checkbox_present: centralHasCheckbox,
                    central_paging_present: centralHasPaging,
                    upload_form_count: uploadForms.length,
                    certificate_table_count: tables.length,
                    existing_certificate_row_count: rows.length,
                    certificate_row_checkbox_count: inputs.filter(item => (item.getAttribute('type') || '').toLowerCase() === 'checkbox').length,
                    certificate_list_container_visible: Boolean(childContainer && visible(childContainer)),
                    certificate_search_input_visible: false,
                    add_button_candidate_visible: false,
                    paging_visible: false,
                    client_certificate_page_landmark_verified: composite,
                    landmark_schema: {target_os_ios_verified: iosSelected && !androidSelected, ios_tab_selected: iosSelected, android_tab_selected: androidSelected, ios_content_container_visible: iosContentVisible, android_content_container_visible: androidContentVisible, certificate_management_expanded_by_attribute: expandedByAttribute, certificate_management_expanded_by_visible_child: expandedByVisibleChild, certificate_management_expanded: expanded, client_certificate_child_candidate_count: deduplicatedClickableCandidateCount, client_certificate_child_visible: childVisibleItems.length > 0, client_certificate_child_active: childActive, client_certificate_child_active_semantic: childActiveSemantic, client_certificate_child_selected_by_style: childActiveByStyle, current_path_matches_client_certificate_child: currentPathMatchesChild, current_path_verified_by_manual_checkpoint: false, certificate_operation_structure_verified: certificateStructure, client_certificate_specific_landmark_count: specificCount}
                };
            """) or {}
        except Exception:
            return {}

    @staticmethod
    def _verify_manual_client_certificate_checkpoint(observation: dict[str, object], *, session_valid: bool, same_host: bool, login_page: bool, current_path: str) -> bool:
        safe_path = bool(current_path.startswith("/") and current_path not in {"", "/"} and not re.search(r"login|signin|sign-in|android", current_path, re.IGNORECASE))
        return bool(
            session_valid and same_host and not login_page and safe_path
            and observation.get("target_os_ios_verified") is True
            and observation.get("ios_tab_selected") is True
            and observation.get("android_tab_selected") is False
            and observation.get("ios_content_container_visible") is True
            and observation.get("android_content_container_visible") is False
            and observation.get("certificate_management_expanded") is True
            and observation.get("client_certificate_child_candidate_count") == 1
            and observation.get("client_certificate_child_visible") is True
            and observation.get("client_certificate_child_active") is True
            and observation.get("client_certificate_child_href_present") is True
            and observation.get("current_path_matches_client_certificate_child") is True
            and observation.get("client_certificate_specific_landmark_count", 0) >= 3
        )

    @staticmethod
    def _verify_replayed_client_certificate_page(observation: dict[str, object], manifest_schema: dict[str, object]) -> bool:
        required = ("target_os_ios_verified", "ios_tab_selected", "ios_content_container_visible", "certificate_management_expanded", "client_certificate_child_visible", "client_certificate_child_active", "client_certificate_child_href_present", "current_path_matches_client_certificate_child")
        if not all(observation.get(key) is True for key in required) or observation.get("android_tab_selected") is not False or observation.get("android_content_container_visible") is not False or observation.get("client_certificate_child_candidate_count") != 1 or observation.get("client_certificate_specific_landmark_count", 0) < 3:
            return False
        if manifest_schema.get("client_certificate_child_active_required") is True and observation.get("client_certificate_child_active") is not True:
            return False
        if manifest_schema.get("current_path_matches_client_child_required") is True and observation.get("current_path_matches_client_certificate_child") is not True:
            return False
        return True

    @staticmethod
    def _client_certificate_landmark_verified(observation: dict[str, object], allow_manual: bool = False) -> bool:
        required = (
            "target_os_ios_verified", "ios_tab_selected", "certificate_management_expanded",
            "client_certificate_child_visible", "client_certificate_child_active",
            "certificate_operation_structure_verified",
        )
        path_verified = observation.get("current_path_matches_client_certificate_child") is True or allow_manual
        href_verified = observation.get("client_certificate_child_href_present") is True and path_verified
        return bool(all(observation.get(key) is True for key in required) and observation.get("android_tab_selected") is False and (allow_manual or href_verified) and observation.get("client_certificate_specific_landmark_count", 0) >= 2)

    def _safe_route_event(self, event: dict[str, object], driver) -> dict[str, object]:
        path_before = str(event.get("pathname_before") or "")
        path_after = str(event.get("pathname_after") or "")
        if not self._is_same_host_path(path_after) or event.get("origin_after") != event.get("origin_before"):
            raise RuntimeError("外部ホストへのSMSM遷移は採取できません")
        state = event.get("accordion_after") if isinstance(event.get("accordion_after"), dict) else {}
        return {key: event.get(key) for key in ("tag", "id", "name", "role", "data_testid", "aria_label", "title", "class_name", "parent_tag", "sibling_count", "dom_position") } | {"selector_type": self._route_selector_type(event), "selector_value": self._route_selector_value(event), "same_host_path": path_after, "pathname_before": path_before, "landmark_selector_type": "runtime_landmark", "landmark_selector_value": str(event.get("landmarks") or {}), "pathname_changed": bool(event.get("pathname_changed")), "aria_expanded_changed": bool(event.get("aria_expanded_changed")), "child_menu_became_visible": bool(event.get("child_menu_became_visible")), "child_container_visibility_changed": bool(event.get("child_container_visibility_changed")), "expanded_state_changed": bool(event.get("expanded_state_changed")), "accordion_expand_verified": bool(event.get("accordion_expand_verified")), "parent_aria_expanded_after": state.get("parent_aria_expanded"), "visible_child_menu_count_after": state.get("visible_child_menu_count", 0), "child_container_visible_after": bool(state.get("child_container_visible")), "parent_expanded_class_present_after": bool(state.get("parent_expanded_class_present"))}

    def _is_same_host_path(self, path: str) -> bool:
        current = urlparse(self._current_url())
        return bool(path.startswith("/") and current.hostname and not urlparse(path).netloc)

    def _same_host_url(self, path: str) -> str:
        current = urlparse(self._current_url())
        return f"{current.scheme}://{current.netloc}{path}"

    @staticmethod
    def _safe_bool_attribute(element, name: str) -> bool:
        try:
            return element.get_attribute(name) is not None
        except Exception:
            return False

    @staticmethod
    def _route_selector_type(event: dict[str, object]) -> str:
        for key in ("id", "name", "data_testid"):
            if event.get(key):
                return key
        return "pathname"

    @classmethod
    def _route_selector_value(cls, event: dict[str, object]) -> str:
        return str(event.get(cls._route_selector_type(event)) or event.get("pathname_after") or "")

    def _resolve_captured_route_candidates(self, entry: dict[str, object]):
        driver = self.browser.driver
        selectors = (("id", "id"), ("name", "name"), ("data-testid", "data_testid"))
        for attribute, key in selectors:
            value = entry.get(key)
            if value:
                candidates = self._safe_find_driver_elements(driver, By.CSS_SELECTOR, f"[{attribute}={json.dumps(str(value))}]")
                if candidates:
                    return candidates
        aria_fingerprint = entry.get("aria_label_fingerprint")
        role = entry.get("role")
        if role and aria_fingerprint:
            candidates = [element for element in self._safe_find_driver_elements(driver, By.CSS_SELECTOR, f"[role={json.dumps(str(role))}]") if hashlib.sha256(str(self._safe_attribute(element, "aria-label") or "").encode("utf-8")).hexdigest()[:12] == aria_fingerprint]
            if candidates:
                return candidates
        path = str(entry.get("same_host_path") or "")
        candidates = [element for element in self._safe_find_driver_elements(driver, By.CSS_SELECTOR, "a[href],button,[role='link'],[role='button'],[role='menuitem']") if urlparse(self._safe_attribute(element, "href") or "").path == path]
        if candidates:
            return candidates
        parent_tag = str(entry.get("parent_tag") or "")
        sibling_count = int(entry.get("sibling_count") or 0)
        result = []
        for element in self._safe_find_driver_elements(driver, By.CSS_SELECTOR, "a,button,[role='link'],[role='button'],[role='menuitem']"):
            try:
                parent = element.find_element(By.XPATH, "..")
                siblings = self._safe_find_elements_from(parent, By.CSS_SELECTOR, ":scope > *")
                if self._safe_tag(parent) == parent_tag and len(siblings) == sibling_count:
                    result.append(element)
            except Exception:
                continue
        return result

    def inspect_client_certificate_upload_dom_for_diagnostic(self, trace=None) -> dict[str, object]:
        """Navigate to certificate management and inspect upload controls without mutating them."""
        navigation_started_at = time.monotonic()
        navigation = {}
        self._trace(trace, "smsm_certificate_navigation_started", True)
        self._trace(trace, "smsm_find_settings_menu", True)
        stage_started_at = time.monotonic()
        settings = self.find_unique_navigation_target("設定", trace=trace)
        self._trace_elapsed(trace, "settings_menu", stage_started_at)
        navigation.update({"settings_menu_candidate_count": settings[1], "settings_menu_unique": settings[1] == 1})
        self._trace(trace, "settings_menu_candidate_count", settings[1])
        self._trace(trace, "settings_menu_unique", settings[1] == 1)
        if settings[1] != 1:
            raise self._navigation_resolution_error("settings", settings[1])
        self._trace(trace, "smsm_open_settings_menu", True)
        self._click_diagnostic_navigation_target(settings[0], settings[2] if len(settings) > 2 else {})
        navigation["settings_menu_click_called"] = True
        self._trace(trace, "settings_menu_click_called", True)
        self._trace(trace, "smsm_wait_settings_page", True)
        self._wait_for_diagnostic_navigation("ios")
        self._trace(trace, "settings_page_reached", True)
        navigation["settings_page_reached"] = True

        self._trace(trace, "smsm_find_ios_menu", True)
        stage_started_at = time.monotonic()
        ios = self.find_unique_navigation_target("iOS", trace=trace)
        self._trace_elapsed(trace, "ios_menu", stage_started_at)
        navigation.update({"ios_menu_candidate_count": ios[1], "ios_menu_unique": ios[1] == 1})
        self._trace(trace, "ios_menu_candidate_count", ios[1])
        self._trace(trace, "ios_menu_unique", ios[1] == 1)
        if ios[1] != 1:
            raise self._navigation_resolution_error("ios", ios[1])
        self._trace(trace, "smsm_open_ios_menu", True)
        self._click_diagnostic_navigation_target(ios[0], ios[2] if len(ios) > 2 else {})
        navigation["ios_menu_click_called"] = True
        self._trace(trace, "ios_menu_click_called", True)
        self._trace(trace, "smsm_wait_ios_page", True)
        self._wait_for_diagnostic_navigation("certificate_management")
        self._trace(trace, "ios_page_reached", True)
        navigation["ios_page_reached"] = True

        self._trace(trace, "smsm_find_certificate_management", True)
        stage_started_at = time.monotonic()
        certificate = self.find_unique_navigation_target("証明書管理", trace=trace)
        self._trace_elapsed(trace, "certificate_management", stage_started_at)
        navigation.update({"certificate_management_candidate_count": certificate[1], "certificate_management_unique": certificate[1] == 1})
        self._trace(trace, "certificate_management_candidate_count", certificate[1])
        self._trace(trace, "certificate_management_unique", certificate[1] == 1)
        if certificate[1] != 1:
            raise self._navigation_resolution_error("certificate_management", certificate[1])
        self._trace(trace, "smsm_open_certificate_management", True)
        self._click_diagnostic_navigation_target(certificate[0], certificate[2] if len(certificate) > 2 else {})
        navigation["certificate_management_click_called"] = True
        self._trace(trace, "certificate_management_click_called", True)
        self._trace(trace, "smsm_wait_certificate_management_page", True)
        self._wait_for_diagnostic_navigation("client_certificate_management")
        self._trace(trace, "certificate_management_page_reached", True)
        navigation["certificate_management_page_reached"] = True

        self._trace(trace, "smsm_find_client_certificate_management", True)
        stage_started_at = time.monotonic()
        client = self.find_unique_navigation_target("クライアント証明書管理", trace=trace)
        self._trace_elapsed(trace, "client_certificate_management", stage_started_at)
        navigation.update({"client_certificate_management_candidate_count": client[1], "client_certificate_management_unique": client[1] == 1})
        self._trace(trace, "client_certificate_management_candidate_count", client[1])
        self._trace(trace, "client_certificate_management_unique", client[1] == 1)
        if client[1] != 1:
            raise self._navigation_resolution_error("client_certificate_management", client[1])
        self._trace(trace, "smsm_open_client_certificate_management", True)
        self._click_diagnostic_navigation_target(client[0], client[2] if len(client) > 2 else {})
        navigation["client_certificate_management_click_called"] = True
        self._trace(trace, "client_certificate_management_click_called", True)
        self._trace(trace, "smsm_wait_client_certificate_page", True)
        self.browser.wait_for_page_ready()
        self._trace(trace, "client_certificate_page_reached", True)
        navigation["client_certificate_page_reached"] = True

        self._trace(trace, "smsm_inspect_client_certificate_upload_dom", True)
        stage_started_at = time.monotonic()
        observation = self._inspect_client_certificate_upload_dom()
        self._trace_elapsed(trace, "upload_dom_inspection", stage_started_at)
        self._trace(trace, "smsm_certificate_navigation_completed", True)
        self._trace_elapsed(trace, "certificate_navigation_total", navigation_started_at)
        return {**navigation, **observation}

    def inspect_current_client_certificate_dom_for_diagnostic(self) -> dict[str, object]:
        """Inspect the current certificate page without navigation or interaction."""
        return self._inspect_client_certificate_upload_dom()

    def confirm_manual_client_certificate_page_for_diagnostic(self, manifest: dict[str, object], trace=None, input_func=input) -> dict[str, object]:
        self._trace(trace, "manual_checkpoint_wait_started", True)
        print("クライアント証明書管理画面を目視確認してください。")
        print("検索欄、プラスボタン、一覧、ページングが表示されていることを確認してください。")
        print("確認後、PowerShellへ戻り空のままEnterを押してください。")
        print("画面上のボタンは手動でクリックしないでください。")
        try:
            value = input_func()
        except KeyboardInterrupt:
            raise
        if value != "":
            self._trace(trace, "manual_checkpoint_received", False)
            raise RuntimeError("空のEnter入力が必要です")
        current = urlparse(self._current_url())
        path = str(manifest.get("same_host_path") or "")
        landmark = self._client_certificate_page_landmark_state(self.browser.driver)
        schema = manifest.get("landmark_schema") if isinstance(manifest.get("landmark_schema"), dict) else {}
        if self._normalized_path(unquote(current.path)) != self._normalized_path(unquote(path)) or not self._verify_replayed_client_certificate_page(landmark, schema):
            raise RuntimeError("目視確認後のクライアント証明書管理画面を確認できません")
        self._trace(trace, "manual_checkpoint_received", True)
        self._trace(trace, "client_certificate_page_landmark_verified", True)
        return landmark

    def inspect_client_certificate_add_form_dom_for_diagnostic(self, trace=None, button_schema_callback=None, click_add_button=True) -> dict[str, object]:
        """Click one validated add control, then inspect the resulting form without input."""
        driver = self.browser.driver
        if driver is None:
            raise RuntimeError("SMSMブラウザーセッションを確認できません")
        self._trace(trace, "smsm_add_form_route_loaded", True)
        self._trace(trace, "smsm_add_form_route_validated", True)
        self._trace(trace, "smsm_add_form_page_reached", True)
        self._trace(trace, "smsm_add_form_page_landmark_verified", False)
        landmark = self._client_certificate_page_landmark_state(driver)
        replay_schema = getattr(self, "_last_replay_landmark_schema", {})
        if replay_schema:
            landmark["client_certificate_page_landmark_verified"] = self._verify_replayed_client_certificate_page(landmark, replay_schema)
        if not landmark.get("client_certificate_page_landmark_verified"):
            raise RuntimeError("クライアント証明書管理一覧画面を確認できません")
        self._trace(trace, "smsm_add_form_page_landmark_verified", True)
        self._trace(trace, "smsm_find_certificate_add_button", True)
        button_observation = self._inspect_client_certificate_add_button_dom(driver)
        if button_schema_callback is not None:
            button_schema_callback(button_observation)
        candidates = button_observation.get("candidates", [])
        self._trace(trace, "certificate_toolbar_found", bool(button_observation.get("certificate_toolbar_found")))
        for key in ("certificate_toolbar_button_count", "search_button_candidate_count", "dropdown_button_candidate_count", "row_action_button_count", "pagination_button_count", "excluded_destructive_button_count", "add_icon_candidate_count", "clickable_ancestor_resolution_count", "deduplicated_add_button_candidate_count", "add_button_candidate_count"):
            self._trace(trace, key, button_observation.get(key, 0))
        self._trace(trace, "add_button_unique", len(candidates) == 1)
        self._trace(trace, "add_button_resolution_method", button_observation.get("add_button_resolution_method", "unresolved"))
        self._trace(trace, "add_button_safe", False if click_add_button else button_observation.get("add_button_unique", False))
        if not click_add_button:
            self._trace(trace, "add_button_click_called", False)
            return {**landmark, **button_observation, "add_button_click_called": False, "file_input_send_keys_called": False, "password_input_send_keys_called": False, "certificate_submit_button_click_called": False, "certificate_upload_called": False, "smsm_update_called": False}
        if len(candidates) != 1:
            error = RuntimeError("追加ボタン候補を一意に確認できません")
            error.observation = button_observation
            raise error
        candidate_index = int(candidates[0].get("element_index", -1))
        controls = driver.find_elements(By.CSS_SELECTOR, "button,a,[role='button'],[role='link'],[tabindex],input[type='button'],input[type='submit']")
        if candidate_index < 0 or candidate_index >= len(controls):
            raise RuntimeError("追加ボタン候補を再取得できません")
        control = controls[candidate_index]
        if not self._safe_bool(control, "is_displayed") or not self._safe_bool(control, "is_enabled") or self._safe_bool_attribute(control, "disabled"):
            raise RuntimeError("追加ボタンが安全条件を満たしません")
        self._trace(trace, "smsm_validate_certificate_add_button", True)
        self._trace(trace, "add_button_safe", True)
        button_observation["add_button_safe"] = True
        button_observation["add_button_click_called"] = False
        button_observation["add_button_click_count"] = 0
        control.click()
        button_observation["add_button_click_called"] = True
        button_observation["add_button_click_count"] = 1
        self._trace(trace, "smsm_click_certificate_add_button", True)
        self._trace(trace, "add_button_click_called", True)
        self._trace(trace, "smsm_wait_certificate_add_form", True)
        latest_observation = {}
        probe_observation = {
            "add_form_probe_called": False,
            "add_form_probe_completed": False,
            "add_form_probe_exception_type": "",
            "add_form_probe_iteration_count": 0,
            "add_form_last_snapshot_available": False,
        }
        def inspect_add_form(current):
            nonlocal latest_observation
            probe_observation["add_form_probe_called"] = True
            probe_observation["add_form_probe_iteration_count"] += 1
            try:
                snapshot = self._inspect_add_form_controls_dom(current)
            except Exception as probe_error:
                probe_observation["add_form_probe_exception_type"] = type(probe_error).__name__
                raise
            probe_observation["add_form_probe_completed"] = True
            latest_observation = snapshot
            probe_observation["add_form_last_snapshot_available"] = bool(snapshot)
            return latest_observation if latest_observation.get("add_form_opened") else False
        try:
            state = WebDriverWait(driver, 15.0, poll_frequency=0.3).until(inspect_add_form)
        except TimeoutException as exc:
            error = RuntimeError("追加フォームを確認できません")
            error.observation = {
                **probe_observation,
                **latest_observation,
                "failed_stage": "smsm_wait_certificate_add_form",
                "exception_type": "RuntimeError",
            }
            raise error from exc
        except Exception as probe_error:
            error = RuntimeError("追加フォームDOM観測に失敗しました")
            error.observation = {
                **probe_observation,
                **latest_observation,
                "add_form_probe_failed_phase": getattr(probe_error, "probe_phase", "unknown"),
                "add_form_probe_javascript_error_name": getattr(probe_error, "javascript_error_name", type(probe_error).__name__),
                "add_form_probe_snapshot_before_failure_available": getattr(probe_error, "snapshot_before_failure_available", bool(latest_observation)),
                "failed_stage": "smsm_probe_certificate_add_form_dom",
                "exception_type": type(probe_error).__name__,
            }
            raise error from probe_error
        self._trace(trace, "add_form_opened", True)
        self._trace(trace, "add_form_resolution_method", state.get("add_form_resolution_method", "unresolved"))
        self._trace(trace, "smsm_inspect_certificate_file_input", True)
        self._trace(trace, "smsm_inspect_certificate_password_input", True)
        self._trace(trace, "smsm_inspect_certificate_submit_button", True)
        self._trace(trace, "smsm_inspect_certificate_cancel_controls", True)
        self._trace(trace, "smsm_client_certificate_add_form_dom_completed", True)
        return {**landmark, **button_observation, **probe_observation, **state}

    def _inspect_client_certificate_add_button_dom(self, driver) -> dict[str, object]:
        return driver.execute_script("""
            const visible = item => Boolean(item && (item.offsetWidth || item.offsetHeight || item.getClientRects().length)) && !item.hidden && getComputedStyle(item).display !== 'none' && getComputedStyle(item).visibility !== 'hidden';
            const controls = Array.from(document.querySelectorAll('button,a,[role="button"],[role="link"],[tabindex],input[type="button"],input[type="submit"]'));
            const inputs = Array.from(document.querySelectorAll('input')).filter(visible);
            const searchInput = inputs.find(item => /search|検索/i.test([item.getAttribute('aria-label'), item.getAttribute('placeholder'), item.getAttribute('name'), item.id].filter(Boolean).join(' ')));
            const attrText = item => [item.id, item.getAttribute('name'), item.getAttribute('data-testid'), item.getAttribute('aria-label'), item.getAttribute('title'), item.getAttribute('role'), item.className].filter(Boolean).join(' ');
            const visibleText = item => String(item.innerText || item.value || '').trim().replace(/\\s+/g, ' ');
            const structuralText = item => [attrText(item), visibleText(item), item.querySelector('svg')?.getAttribute('aria-label') || '', item.querySelector('svg title')?.textContent || '', item.querySelector('img[alt]')?.getAttribute('alt') || ''].join(' ');
            const style = item => getComputedStyle(item);
            const pseudo = (item, which) => String(style(item, `::${which}`).content || '').replace(/["']/g, '');
            const svg = item => item.querySelector('svg');
            const isRow = item => Boolean(item.closest('tr,[data-testid*="row" i],[class*="row" i]'));
            const isPage = item => Boolean(item.closest('[aria-label*="page" i],[class*="pagination" i],[class*="paging" i]')) || /page|ページ|pagination|paging/i.test(attrText(item));
            const isSearch = item => /search|検索|magnify|虫眼鏡/i.test(structuralText(item)) || Boolean(searchInput && (item.type === 'submit' || item.closest('form') === searchInput.closest('form')) && item !== searchInput);
            const isDropdown = item => item.hasAttribute('aria-haspopup') || item.hasAttribute('aria-expanded') || /menu|dropdown|ドロップダウン|下向き|chevron|caret/i.test(attrText(item));
            const isDestructive = item => /delete|remove|削除|一括削除|bulk/i.test(structuralText(item));
            const svgPlus = item => Array.from(item.querySelectorAll('svg path')).some(path => /M[^M]*h[^M]*M[^M]*v|M[^M]*v[^M]*M[^M]*h/i.test(path.getAttribute('d') || '')) || Array.from(item.querySelectorAll('svg use')).some(use => /plus|add/i.test(use.getAttribute('href') || use.getAttribute('xlink:href') || ''));
            const hasPlus = item => /\\+|plus|add|追加|新規/i.test(structuralText(item)) || /\\+/.test(pseudo(item, 'before') + pseudo(item, 'after')) || svgPlus(item);
            const hasSvg = item => Boolean(svg(item));
            const ancestors = item => { const result = []; let current = item; for (let depth = 0; current && depth <= 5; depth += 1, current = current.parentElement) result.push(current); return result; };
            const commonAncestor = items => { if (!items.length) return null; const first = ancestors(items[0]); return first.find(candidate => items.every(item => ancestors(item).includes(candidate))) || null; };
            const searchButtons = controls.filter(isSearch);
            const dropdownButtons = controls.filter(isDropdown);
            const plusButtons = controls.filter(hasPlus);
            const commonWithSearch = item => commonAncestor([item, searchInput, ...searchButtons]);
            const commonWithDropdown = item => commonAncestor([item, ...dropdownButtons]);
            const toolbarCandidates = searchInput ? ancestors(searchInput).filter(candidate => visible(candidate) && !isRow(candidate) && !isPage(candidate) && !isDestructive(candidate) && searchButtons.some(item => candidate.contains(item)) && plusButtons.some(item => candidate.contains(item)) && dropdownButtons.some(item => candidate.contains(item))) : [];
            const toolbar = toolbarCandidates.find(candidate => { const buttons = controls.filter(item => candidate.contains(item)); return buttons.length > 0 && buttons.length <= 12; }) || null;
            const toolbarButtons = toolbar ? controls.filter(item => toolbar.contains(item)) : [];
            const toolbarInputs = toolbar ? inputs.filter(item => toolbar.contains(item)) : [];
            const candidateReason = item => {
                if (/^(追加|新規追加|証明書を追加|add|add certificate)$/i.test(visibleText(item))) return 'accessible_name';
                if (/add|追加|certificate-add|client-certificate-add/i.test(attrText(item))) return 'stable_attribute';
                const itemIndex = toolbarButtons.indexOf(item);
                const searchIndex = toolbarButtons.findIndex(isSearch);
                const dropdownIndex = toolbarButtons.findIndex(isDropdown);
                if (hasPlus(item) && toolbar && itemIndex > searchIndex && (dropdownIndex < 0 || itemIndex < dropdownIndex)) return 'verified_unique_safe_plus_icon';
                return 'unresolved';
            };
            const metadata = (item, index) => {
                const searchCommon = commonWithSearch(item);
                const dropdownCommon = commonWithDropdown(item);
                const sameGroup = Boolean(toolbar && toolbar.contains(item) && searchInput && toolbar.contains(searchInput));
                const reason = candidateReason(item);
                const excluded = isRow(item) || isPage(item) || isSearch(item) || isDropdown(item) || isDestructive(item) || !visible(item) || item.disabled || !['accessible_name','stable_attribute','verified_unique_safe_plus_icon'].includes(reason);
                let exclusion = '';
                if (isRow(item)) exclusion = 'certificate_row'; else if (isPage(item)) exclusion = 'pagination'; else if (isSearch(item)) exclusion = 'search'; else if (isDropdown(item)) exclusion = 'dropdown'; else if (isDestructive(item)) exclusion = 'destructive'; else if (!sameGroup && reason === 'verified_unique_safe_plus_icon') exclusion = 'different_group'; else if (!visible(item)) exclusion = 'hidden'; else if (item.disabled) exclusion = 'disabled'; else if (reason === 'unresolved') exclusion = 'unresolved';
                return {element_index: index, tag: item.tagName.toLowerCase(), type: item.getAttribute('type'), role: item.getAttribute('role'), id_present: item.hasAttribute('id'), name_present: item.hasAttribute('name'), data_testid_present: item.hasAttribute('data-testid'), aria_label_present: item.hasAttribute('aria-label'), title_present: item.hasAttribute('title'), class_present: item.hasAttribute('class'), displayed: visible(item), enabled: !item.disabled, disabled: Boolean(item.disabled), parent_tag: item.parentElement ? item.parentElement.tagName.toLowerCase() : '', grandparent_tag: item.parentElement?.parentElement ? item.parentElement.parentElement.tagName.toLowerCase() : '', same_group_as_search_input: sameGroup, search_common_ancestor_found: Boolean(searchCommon), search_common_ancestor_depth: searchCommon ? ancestors(item).indexOf(searchCommon) : -1, dropdown_common_ancestor_found: Boolean(dropdownCommon), dropdown_common_ancestor_depth: dropdownCommon ? ancestors(item).indexOf(dropdownCommon) : -1, toolbar_common_ancestor_found: Boolean(toolbar), toolbar_common_ancestor_button_count: toolbarButtons.length, toolbar_common_ancestor_input_count: toolbarInputs.length, inside_certificate_row: isRow(item), inside_pagination: isPage(item), inside_destructive_region: isDestructive(item), has_svg: hasSvg(item), svg_path_count: item.querySelectorAll('svg path').length, svg_use_present: Boolean(item.querySelector('svg use')), before_content_present: Boolean(pseudo(item, 'before') && pseudo(item, 'before') !== 'none'), after_content_present: Boolean(pseudo(item, 'after') && pseudo(item, 'after') !== 'none'), candidate_reason: reason, exclusion_reason: exclusion, group_index: toolbarButtons.indexOf(item)};
            };
            const details = controls.map(metadata);
            const disabled = item => Boolean(item.disabled || item.hasAttribute('disabled') || item.getAttribute('aria-disabled') === 'true');
            const clickable = item => item && (
                ['A', 'BUTTON'].includes(item.tagName) ||
                ['link', 'button'].includes((item.getAttribute('role') || '').toLowerCase()) ||
                item.hasAttribute('onclick') ||
                (item.tabIndex >= 0 && item !== document.body)
            );
            const clickableAncestor = item => {
                let current = item;
                while (current) {
                    if (clickable(current)) return current;
                    current = current.parentElement;
                }
                return null;
            };
            const plusElements = Array.from(document.querySelectorAll('*')).filter(hasPlus);
            const plusRoots = plusElements.filter(item => !plusElements.some(other => other !== item && item.contains(other)));
            const resolvedAncestors = plusRoots.map(clickableAncestor).filter(Boolean);
            const deduplicatedAncestors = Array.from(new Set(resolvedAncestors));
            const ancestorDetails = deduplicatedAncestors.map(item => {
                const index = controls.indexOf(item);
                const itemDetail = index >= 0 ? details[index] : metadata(item, index);
                return {...itemDetail, element_index: index, add_button_displayed: visible(item), add_button_enabled: !disabled(item), add_button_disabled: disabled(item), add_button_inside_row: isRow(item), add_button_inside_pagination: isPage(item), add_button_inside_destructive_region: isDestructive(item), add_button_is_search: isSearch(item), add_button_is_dropdown: isDropdown(item), add_button_has_unique_plus_icon: plusRoots.filter(icon => clickableAncestor(icon) === item).length === 1};
            });
            const eligible = ancestorDetails.filter(item => item.element_index >= 0 && item.add_button_displayed && item.add_button_enabled && !item.add_button_disabled && !item.add_button_inside_row && !item.add_button_inside_pagination && !item.add_button_inside_destructive_region && !item.add_button_is_search && !item.add_button_is_dropdown && item.add_button_has_unique_plus_icon);
            const searchCount = searchButtons.length;
            const dropdownCount = details.filter(item => isDropdown(controls[item.element_index])).length;
            const rowCount = details.filter(item => isRow(controls[item.element_index])).length;
            const pageCount = details.filter(item => isPage(controls[item.element_index])).length;
            const destructiveCount = details.filter(item => isDestructive(controls[item.element_index])).length;
            const addIconUnique = plusRoots.length === 1 ? plusRoots[0] : null;
            const addIconIndex = addIconUnique ? controls.indexOf(addIconUnique) : -1;
            const addIconDetail = addIconIndex >= 0 ? details[addIconIndex] : null;
            return {certificate_toolbar_found: Boolean(toolbar), certificate_toolbar_button_count: toolbarButtons.length, search_button_candidate_count: searchCount, dropdown_button_candidate_count: dropdownCount, row_action_button_count: rowCount, pagination_button_count: pageCount, excluded_destructive_button_count: destructiveCount, add_icon_candidate_count: plusRoots.length, add_icon_unique: plusRoots.length === 1, add_icon_displayed: addIconUnique ? visible(addIconUnique) : false, add_icon_enabled: addIconUnique ? !disabled(addIconUnique) : false, add_icon_disabled: addIconUnique ? disabled(addIconUnique) : false, add_icon_inside_row: addIconDetail ? addIconDetail.inside_certificate_row : false, add_icon_inside_pagination: addIconDetail ? addIconDetail.inside_pagination : false, add_icon_inside_destructive_region: addIconDetail ? addIconDetail.inside_destructive_region : false, clickable_ancestor_resolution_count: resolvedAncestors.length, deduplicated_add_button_candidate_count: eligible.length, add_button_candidate_count: eligible.length, add_button_unique: eligible.length === 1, add_button_safe: false, add_button_click_called: false, add_button_click_count: 0, add_button_displayed: eligible.length === 1 ? eligible[0].add_button_displayed : false, add_button_enabled: eligible.length === 1 ? eligible[0].add_button_enabled : false, add_button_disabled: eligible.length === 1 ? eligible[0].add_button_disabled : false, add_button_inside_row: eligible.length === 1 ? eligible[0].add_button_inside_row : false, add_button_inside_pagination: eligible.length === 1 ? eligible[0].add_button_inside_pagination : false, add_button_inside_destructive_region: eligible.length === 1 ? eligible[0].add_button_inside_destructive_region : false, add_button_is_search: eligible.length === 1 ? eligible[0].add_button_is_search : false, add_button_is_dropdown: eligible.length === 1 ? eligible[0].add_button_is_dropdown : false, add_button_resolution_method: eligible.length === 1 ? 'verified_unique_safe_plus_icon' : 'unresolved', elements: details, candidates: eligible};
        """) or {"candidates": [], "elements": []}

    def _inspect_add_form_controls_dom(self, driver) -> dict[str, object]:
        result = driver.execute_script("""
            try {
            let probePhase = 'probe_base_counts';
            const visible = item => Boolean(item && (item.offsetWidth || item.offsetHeight || item.getClientRects().length)) && !item.hidden && getComputedStyle(item).display !== 'none' && getComputedStyle(item).visibility !== 'hidden';
            const text = item => String(item?.innerText || item?.textContent || item?.getAttribute('aria-label') || item?.getAttribute('title') || '').trim().replace(/\\s+/g, ' ');
            const labels = Array.from(document.querySelectorAll('label'));
            const labelFor = item => item.id ? labels.filter(label => label.htmlFor === item.id) : [];
            const labelledBy = item => String(item.getAttribute('aria-labelledby') || '').split(/\\s+/).map(id => document.getElementById(id)).filter(Boolean);
            const hasLabel = item => labelFor(item).length > 0 || labelledBy(item).length > 0 || Boolean(item.closest('label'));
            const labelText = item => labelFor(item).concat(labelledBy(item)).concat(item.closest('label') ? [item.closest('label')] : []).map(text).concat([text(item.parentElement)]).join(' ');
            const rightSide = item => item.getBoundingClientRect().left >= window.innerWidth * 0.45;
            const all = Array.from(document.querySelectorAll('*'));
            const controls = all.filter(item => item.matches('input,button,a,[role="button"],[role="link"],textarea,select'));
            const frames = Array.from(document.querySelectorAll('iframe,frame'));
            let sameOriginFrames = 0;
            let crossOriginFrames = 0;
            const sameOriginDocuments = [];
            for (const frame of frames) {
                try {
                    const frameDocument = frame.contentDocument;
                    if (frameDocument) {
                        sameOriginFrames += 1;
                        sameOriginDocuments.push(frameDocument);
                    } else {
                        crossOriginFrames += 1;
                    }
                } catch (_error) {
                    crossOriginFrames += 1;
                }
            }
            const openShadowHosts = all.filter(item => item.shadowRoot);
            const shadowControls = openShadowHosts.flatMap(item => Array.from(item.shadowRoot.querySelectorAll('input,button,[role="button"],[tabindex]')));
            const topFileInputs = all.filter(item => item.matches('input[type="file"]'));
            const topPasswordInputs = all.filter(item => item.matches('input[type="password"]'));
            const topTextInputs = all.filter(item => item.matches('input[type="text"]'));
            const topButtons = all.filter(item => item.matches('button'));
            const topSubmitInputs = all.filter(item => item.matches('input[type="submit"]'));
            probePhase = 'resolve_file_input';
            const iframeCounts = sameOriginDocuments.map(frameDocument => ({
                file_input_count: frameDocument.querySelectorAll('input[type="file"]').length,
                password_or_text_input_count: frameDocument.querySelectorAll('input[type="password"],input[type="text"]').length,
                save_candidate_count: frameDocument.querySelectorAll('button,input[type="submit"],[role="button"],[tabindex]').length,
            }));
            const fileDom = controls.filter(item => item.tagName === 'INPUT' && (item.type || '').toLowerCase() === 'file');
            const fileEnabled = fileDom.filter(item => !item.disabled && item.getAttribute('aria-disabled') !== 'true');
            probePhase = 'resolve_save_button';
            const saveDom = controls.filter(item => /^(保存|save)$/i.test(text(item)));
            const saveVisible = saveDom.filter(item => visible(item) && !item.disabled && item.getAttribute('aria-disabled') !== 'true');
            probePhase = 'resolve_right_panel';
            const rightContainers = all.filter(item => rightSide(item) && (visible(item) || item.getAttribute('aria-hidden') === 'false') && (/新規作成|設定|編集中|クライアント証明書|client certificate/i.test(text(item)) || /drawer|panel|side|create|edit/i.test(`${item.className || ''} ${item.getAttribute('data-testid') || ''}`)));
            const pairAncestors = [];
            for (const file of fileEnabled) for (const save of saveVisible) {
                const ancestors = all.filter(node => rightSide(node) && node.contains(file) && node.contains(save));
                if (ancestors.length) pairAncestors.push({file, save, ancestors});
            }
            const pairCommon = pairAncestors.length === 1 ? pairAncestors[0].ancestors.reduce((small, node) => node.querySelectorAll('*').length < small.querySelectorAll('*').length ? node : small) : null;
            const panelRoot = pairCommon && (rightContainers.some(item => item === pairCommon || item.contains(pairCommon) || pairCommon.contains(item)) ? pairCommon : pairCommon);
            const inPanel = item => panelRoot !== null && panelRoot.contains(item);
            const panelFiles = fileEnabled.filter(inPanel);
            const panelSaves = saveVisible.filter(inPanel);
            const passwordGlobal = controls.filter(item => item.tagName === 'INPUT' && /password|text/i.test((item.type || '').toLowerCase()));
            const passwordInsidePanel = passwordGlobal.filter(inPanel);
            probePhase = 'resolve_password_label';
            const passwordLabels = Array.from(panelRoot ? panelRoot.querySelectorAll('label') : []).filter(label => /証明書を保護するパスワード|certificate.*password/i.test(text(label)));
            const fieldGroup = item => item.closest('[role="group"],[role="field"],fieldset,[data-field],[class*="field" i],[class*="form-group" i]') || item.parentElement;
            const associatedInput = label => {
                if (label.htmlFor) {
                    const target = document.getElementById(label.htmlFor);
                    if (target && passwordInsidePanel.includes(target)) return {input: target, method: 'label_for_id'};
                }
                const labelled = passwordInsidePanel.filter(item => String(item.getAttribute('aria-labelledby') || '').split(/\\s+/).includes(label.id));
                if (labelled.length === 1) return {input: labelled[0], method: 'aria_labelledby'};
                const group = fieldGroup(label);
                const groupInputs = passwordInsidePanel.filter(item => group && group.contains(item));
                if (groupInputs.length === 1) return {input: groupInputs[0], method: 'same_field_group'};
                const adjacent = [];
                let sibling = label.nextElementSibling;
                if (sibling) adjacent.push(...sibling.matches('input') ? [sibling] : Array.from(sibling.querySelectorAll('input')));
                if (label.previousElementSibling) adjacent.push(...label.previousElementSibling.matches('input') ? [label.previousElementSibling] : Array.from(label.previousElementSibling.querySelectorAll('input')));
                const adjacentInputs = passwordInsidePanel.filter(item => adjacent.includes(item));
                if (adjacentInputs.length === 1) return {input: adjacentInputs[0], method: 'adjacent_label_input'};
                const ancestors = all.filter(node => node.contains(label) && passwordInsidePanel.filter(item => node.contains(item)).length === 1);
                if (ancestors.length) return {input: passwordInsidePanel.find(item => ancestors[ancestors.length - 1].contains(item)), method: 'unique_right_panel_input'};
                return null;
            };
            const inputVisible = item => {
                if (visible(item)) return true;
                let current = item.parentElement;
                while (current && current !== panelRoot) {
                    const style = getComputedStyle(current);
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                    const rect = current.getBoundingClientRect();
                    if ((current.offsetWidth || current.offsetHeight || rect.width || rect.height) && Number(style.opacity) !== 0) return true;
                    current = current.parentElement;
                }
                return false;
            };
            probePhase = 'resolve_password_input';
            const resolvedLabels = passwordLabels.map(associatedInput).filter(Boolean);
            const resolvedInputs = Array.from(new Set(resolvedLabels.map(item => item.input)));
            const visiblePasswords = resolvedInputs.filter(item => inputVisible(item) && !item.disabled && item.getAttribute('aria-disabled') !== 'true');
            const passwordResolutionMethod = resolvedInputs.length === 1 && visiblePasswords.length === 1 ? resolvedLabels.find(item => item.input === resolvedInputs[0]).method : 'unresolved';
            const initialAncestors = panelFiles.length === 1 && panelSaves.length === 1 ? all.filter(node => rightSide(node) && node.contains(panelFiles[0]) && node.contains(panelSaves[0])) : [];
            const initialCommon = initialAncestors.length ? initialAncestors.reduce((small, node) => node.querySelectorAll('*').length < small.querySelectorAll('*').length ? node : small) : null;
            const common = initialCommon;
            const files = panelFiles.filter(item => common && common.contains(item));
            const passwords = visiblePasswords.filter(item => common && common.contains(item));
            const saves = panelSaves.filter(item => common && common.contains(item));
            const passwordAfterTypeFilter = passwordInsidePanel.filter(item => /password|text/i.test((item.type || '').toLowerCase()));
            const passwordAfterExclusion = passwordAfterTypeFilter.filter(item => !item.disabled && item.getAttribute('aria-disabled') !== 'true' && !item.matches('input[type="file"],input[type="hidden"],input[type="checkbox"],input[type="radio"],input[type="button"],input[type="submit"]'));
            probePhase = 'calculate_common_ancestor';
            const snapshotPhase = 'build_snapshot';
            return {
                right_side_container_candidate_count: rightContainers.length,
                right_side_visible_container_count: rightContainers.filter(visible).length,
                file_input_dom_count: fileDom.length,
                file_input_enabled_count: fileEnabled.length,
                password_input_dom_count: passwordGlobal.length,
                password_input_visible_count: visiblePasswords.length,
                save_button_dom_count: saveDom.length,
                save_button_visible_count: saveVisible.length,
                upload_controls_common_ancestor_count: initialCommon ? 1 : 0,
                initial_add_form_container_candidate_count: initialAncestors.length,
                initial_add_form_container_unique: Boolean(initialCommon),
                file_input_inside_initial_add_form_count: files.length,
                save_button_inside_initial_add_form_count: saves.length,
                add_form_resolution_method: common ? 'smsm_client_certificate_create_controls_common_ancestor' : 'unresolved',
                add_form_opened: files.length === 1 && saves.length === 1,
                file_input_count: files.length,
                file_input_unique: files.length === 1,
                file_input_hidden_allowed: files.length === 1 && !visible(files[0]),
                password_input_global_candidate_count: passwordGlobal.length,
                password_input_inside_right_panel_count: passwordInsidePanel.length,
                password_label_candidate_count: passwordLabels.length,
                password_label_associated_input_count: resolvedInputs.length,
                password_input_after_type_filter_count: passwordAfterTypeFilter.length,
                password_input_after_exclusion_count: passwordAfterExclusion.length,
                password_input_after_visibility_count: visiblePasswords.length,
                password_input_count: passwords.length,
                password_input_unique: passwords.length === 1,
                password_input_resolution_method: passwordResolutionMethod,
                submit_button_candidate_count: saves.length,
                submit_button_unique: saves.length === 1,
                create_side_panel_found: Boolean(common),
                create_side_panel_visible: Boolean(common && visible(common)),
                client_certificate_area_visible: Boolean(common && /クライアント証明書|client certificate/i.test(text(common))),
                password_input_label_linked: passwords.length === 1 && hasLabel(passwords[0]),
                certificate_submit_button_safe: saves.length === 1,
                top_document_iframe_count: frames.length,
                visible_iframe_count: frames.filter(visible).length,
                same_origin_iframe_count: sameOriginFrames,
                cross_origin_iframe_count: crossOriginFrames,
                same_origin_iframe_observation: iframeCounts,
                open_shadow_root_host_count: openShadowHosts.length,
                shadow_root_file_input_count: shadowControls.filter(item => item.matches('input[type="file"]')).length,
                shadow_root_password_input_count: shadowControls.filter(item => item.matches('input[type="password"],input[type="text"]')).length,
                shadow_root_save_button_count: shadowControls.filter(item => item.matches('button,[role="button"],[tabindex]')).length,
                top_document_file_input_count: topFileInputs.length,
                top_document_password_input_count: topPasswordInputs.length,
                top_document_text_input_count: topTextInputs.length,
                top_document_button_count: topButtons.length,
                top_document_submit_input_count: topSubmitInputs.length,
                file_input_send_keys_called: false,
                password_input_send_keys_called: false,
                certificate_submit_button_click_called: false,
                certificate_upload_called: false,
                add_form_probe_phase: snapshotPhase,
                add_form_probe_completed_phases: ['probe_base_counts', 'resolve_file_input', 'resolve_save_button', 'resolve_right_panel', 'resolve_password_label', 'resolve_password_input', 'calculate_common_ancestor', 'build_snapshot'],
            };
            } catch (error) {
                return {add_form_probe_failed: true, add_form_probe_failed_phase: probePhase, add_form_probe_javascript_error_name: String(error && error.name ? error.name : 'Error'), add_form_probe_snapshot_before_failure_available: false};
            }
        """) or {}
        if result.get("add_form_probe_failed"):
            error = JavascriptException("追加フォームDOM観測JavaScriptが失敗しました")
            error.probe_phase = result.get("add_form_probe_failed_phase", "unknown")
            error.javascript_error_name = result.get("add_form_probe_javascript_error_name", "JavascriptException")
            error.snapshot_before_failure_available = result.get("add_form_probe_snapshot_before_failure_available", False)
            raise error
        return result

    def _inspect_add_form_state(self, driver) -> dict[str, object] | bool:
        return self._inspect_add_form_controls_dom(driver)
        try:
            result = self._inspect_add_form_controls_dom(driver)
            if result.get("add_form_opened") is True:
                return result
            return False
        except Exception:
            return False
        try:
            return driver.execute_script("""
                const visible = item => Boolean(item && (item.offsetWidth || item.offsetHeight || item.getClientRects().length)) && !item.hidden && getComputedStyle(item).display !== 'none' && getComputedStyle(item).visibility !== 'hidden';
                const text = item => String(item?.innerText || item?.textContent || item?.getAttribute('aria-label') || item?.getAttribute('title') || '').trim().replace(/\\s+/g, ' ');
                const labels = Array.from(document.querySelectorAll('label'));
                const labelFor = item => item.id ? labels.filter(label => label.htmlFor === item.id) : [];
                const labelledBy = item => String(item.getAttribute('aria-labelledby') || '').split(/\\s+/).map(id => document.getElementById(id)).filter(Boolean);
                const hasLabel = item => labelFor(item).length > 0 || labelledBy(item).length > 0 || Boolean(item.closest('label'));
                const fieldLabelText = item => labelFor(item).concat(labelledBy(item)).map(text).concat([text(item.closest('label')), text(item.parentElement)]).join(' ');
                const panelSelector = '[role="complementary"],aside,[data-testid*="drawer" i],[data-testid*="panel" i],[class*="drawer" i],[class*="side-panel" i],[class*="sidebar" i],[class*="panel" i]';
                const panelCandidates = Array.from(document.querySelectorAll(panelSelector)).filter(visible).filter(panel => /新規作成|クライアント証明書/i.test(text(panel)));
                const panels = Array.from(new Set(panelCandidates));
                const panel = panels.length === 1 ? panels[0] : null;
                if (!panel) return false;
                const elements = Array.from(panel.querySelectorAll('input,button,a,[role="button"],[role="link"],textarea,select'));
                const fileInputs = elements.filter(item => item.tagName === 'INPUT' && (item.type || '').toLowerCase() === 'file' && !item.disabled && hasLabel(item) && /証明書\\s*ファイル|ファイルを選択|certificate file/i.test(fieldLabelText(item)));
                const passwordInputs = elements.filter(item => item.tagName === 'INPUT' && (item.type || '').toLowerCase() === 'password' && visible(item) && !item.disabled && item.getAttribute('aria-disabled') !== 'true' && hasLabel(item) && /証明書を保護するパスワード|certificate.*password|password/i.test(fieldLabelText(item)));
                const saveButtons = elements.filter(item => /^(保存|save)$/i.test(text(item)) && visible(item) && !item.disabled && item.getAttribute('aria-disabled') !== 'true');
                const cancel = elements.filter(item => /^(キャンセル|cancel)$/i.test(text(item)));
                const close = elements.filter(item => /^(閉じる|close)$/i.test(text(item)));
                const metadata = (item, index) => ({element_index: index, tag: item.tagName.toLowerCase(), type: item.getAttribute('type'), displayed: visible(item), enabled: !item.disabled, disabled: Boolean(item.disabled), readonly: Boolean(item.readOnly), label_linked: labelFor(item).length > 0 || labelledBy(item).length > 0, inside_create_side_panel: true});
                const ready = fileInputs.length === 1 && passwordInputs.length === 1 && saveButtons.length === 1;
                if (!ready) return false;
                return {add_form_opened: true, add_form_resolution_method: 'smsm_client_certificate_create_side_panel', create_side_panel_found: true, create_side_panel_visible: true, client_certificate_area_visible: /クライアント証明書|client certificate/i.test(text(panel)), form_count: panel.querySelectorAll('form').length, dialog_count: 0, input_count: elements.filter(item => item.tagName === 'INPUT').length, button_count: elements.filter(item => ['BUTTON','A'].includes(item.tagName)).length, file_input_count: fileInputs.length, file_input_unique: true, file_input_safe: true, file_input_hidden_allowed: !visible(fileInputs[0]), file_input_label_linked: true, password_input_count: passwordInputs.length, password_input_unique: true, password_input_safe: true, password_input_label_linked: true, password_input_disabled: false, certificate_submit_button_candidate_count: saveButtons.length, certificate_submit_button_unique: true, certificate_submit_button_safe: true, submit_button_candidate_count: saveButtons.length, submit_button_unique: true, file_input_send_keys_called: false, password_input_send_keys_called: false, certificate_submit_button_click_called: false, certificate_upload_called: false, cancel_button_candidate_count: cancel.length, close_button_candidate_count: close.length, schema: elements.map(metadata)};
            """) or False
        except Exception:
            return False

    def _navigation_resolution_error(self, stage: str, candidate_count: int) -> RuntimeError:
        error = RuntimeError(f"{stage} navigation target unresolved")
        error.observation = {
            "failed_stage": stage,
            "candidate_count": candidate_count,
            "navigation_observation": dict(self._last_navigation_observation),
            "candidates": list(self._last_navigation_evidence),
        }
        return error

    def inspect_settings_navigation_dom_for_diagnostic(self, trace=None) -> dict[str, object]:
        """Inspect the post-login navigation without selecting or clicking any item."""
        self._trace(trace, "smsm_inspect_settings_navigation_dom", True)
        driver = self.browser.driver
        if driver is None:
            raise RuntimeError("ログイン後ナビゲーションDOMを確認できません")
        script = """
            const roots = Array.from(document.querySelectorAll('nav, header'));
            const rawElements = Array.from(document.querySelectorAll('nav, header, a, button, [role="link"], [role="button"], [aria-label], [data-testid]'));
            const elements = Array.from(new Set(rawElements.concat(Array.from(document.querySelectorAll('*')))));
            const normalize = value => String(value || '').trim().replace(/\\s+/g, ' ');
            const accessibleName = item => {
                const labelledby = item.getAttribute('aria-labelledby');
                if (item.getAttribute('aria-label')) return normalize(item.getAttribute('aria-label'));
                if (labelledby) {
                    const value = labelledby.split(/\\s+/).map(id => document.getElementById(id)).filter(Boolean).map(node => node.innerText || node.textContent || '').join(' ');
                    if (normalize(value)) return normalize(value);
                }
                if (item.getAttribute('title')) return normalize(item.getAttribute('title'));
                if (normalize(item.innerText)) return normalize(item.innerText);
                if (normalize(item.textContent)) return normalize(item.textContent);
                const alt = Array.from(item.querySelectorAll('img[alt]')).map(node => node.getAttribute('alt')).join(' ');
                return normalize(alt);
            };
            const clickable = item => item && (
                ['A', 'BUTTON'].includes(item.tagName) ||
                ['link', 'button'].includes((item.getAttribute('role') || '').toLowerCase()) ||
                item.hasAttribute('onclick') ||
                item.tabIndex >= 0 && (item.tagName === 'DIV' || item.tagName === 'SPAN')
            );
            const clickableAncestor = item => {
                let current = item;
                while (current) {
                    if (clickable(current)) return current;
                    current = current.parentElement;
                }
                return null;
            };
            const settingsTextElements = elements.filter(item => accessibleName(item) === '設定');
            const candidates = Array.from(new Set(settingsTextElements.map(clickableAncestor).filter(Boolean)));
            const links = Array.from(document.querySelectorAll('a,button,[role="link"],[role="button"],[role="menuitem"]'));
            const sourceValues = links.map(item => [
                item.getAttribute('aria-label'),
                item.getAttribute('aria-labelledby'),
                item.getAttribute('title'),
                item.innerText,
                item.textContent,
                Array.from(item.querySelectorAll('*')).map(child => child.innerText || '').join(' '),
                Array.from(item.querySelectorAll('*')).map(child => child.textContent || '').join(' '),
                Array.from(item.querySelectorAll('img[alt]')).map(child => child.getAttribute('alt') || '').join(' '),
                Array.from(item.querySelectorAll('svg[aria-label],svg title')).map(child => child.getAttribute('aria-label') || child.textContent || '').join(' ')
            ]);
            return {
                roots: {nav_count: roots.filter(item => item.tagName === 'NAV').length, header_count: roots.filter(item => item.tagName === 'HEADER').length},
                elements: elements.map((item, index) => {
                    const ancestor = clickableAncestor(item);
                    const parent = item.parentElement;
                    const href = item.getAttribute('href');
                    return {
                        element_index: index,
                        tag: item.tagName.toLowerCase(),
                        id: item.getAttribute('id'),
                        name: item.getAttribute('name'),
                        role: item.getAttribute('role'),
                        'data-testid': item.getAttribute('data-testid'),
                        'aria-label': item.getAttribute('aria-label'),
                        href_path: href ? new URL(href, document.baseURI).pathname : null,
                        displayed: Boolean(item.offsetWidth || item.offsetHeight || item.getClientRects().length),
                        enabled: !item.disabled,
                        disabled: item.disabled === true || item.hasAttribute('disabled'),
                        parent_tag: parent ? parent.tagName.toLowerCase() : null,
                        parent_id_present: Boolean(parent && parent.id),
                        parent_class_present: Boolean(parent && parent.className),
                        child_count: item.children.length,
                        clickable_ancestor_present: Boolean(ancestor),
                        clickable_ancestor_tag: ancestor ? ancestor.tagName.toLowerCase() : null
                    };
                }),
                exact_settings_text_count: elements.filter(item => accessibleName(item) === '設定' && item.textContent === '設定').length,
                normalized_settings_text_count: settingsTextElements.length,
                settings_text_on_child_count: settingsTextElements.filter(item => item.children.length === 0 && item.parentElement && normalize(item.parentElement.textContent) === '設定').length,
                settings_text_element_count: settingsTextElements.length,
                settings_directly_clickable_count: settingsTextElements.filter(clickable).length,
                settings_clickable_parent_count: settingsTextElements.filter(item => clickable(item.parentElement)).length,
                settings_clickable_ancestor_count: settingsTextElements.filter(item => Boolean(clickableAncestor(item))).length,
                settings_candidate_count: candidates.length,
                settings_candidate_unique: candidates.length === 1,
                accessible_name_source_count: sourceValues.reduce((total, values) => total + values.length, 0),
                accessible_name_nonblank_count: sourceValues.reduce((total, values) => total + values.filter(value => normalize(value)).length, 0),
                japanese_name_detected_count: sourceValues.filter(values => values.some(value => /[\u3040-\u30ff\u3400-\u9fff]/.test(normalize(value)))).length,
                expected_name_exact_match_count: sourceValues.filter(values => values.some(value => normalize(value) === '設定')).length
            };
        """
        result = driver.execute_script(script)
        if not isinstance(result, dict):
            raise RuntimeError("ログイン後ナビゲーションDOMの取得結果が不正です")
        elements = []
        for item in result.get("elements", []):
            if not isinstance(item, dict):
                continue
            safe_item = dict(item)
            href_path = safe_item.pop("href_path", None)
            if isinstance(href_path, str) and href_path:
                safe_item["href_path_fingerprint"] = hashlib.sha256(href_path.encode("utf-8")).hexdigest()[:12]
            else:
                safe_item["href_path_fingerprint"] = None
            elements.append(safe_item)
        roots = result.get("roots", {}) if isinstance(result.get("roots"), dict) else {}
        observation = {
            "nav_count": int(roots.get("nav_count", 0)),
            "header_count": int(roots.get("header_count", 0)),
            "elements": elements,
        }
        for key in (
            "exact_settings_text_count", "normalized_settings_text_count", "settings_text_on_child_count",
            "settings_text_element_count", "settings_directly_clickable_count", "settings_clickable_parent_count",
            "settings_clickable_ancestor_count", "settings_candidate_count",
            "accessible_name_source_count", "accessible_name_nonblank_count", "japanese_name_detected_count",
            "expected_name_exact_match_count",
        ):
            observation[key] = int(result.get(key, 0))
        observation["settings_candidate_unique"] = bool(result.get("settings_candidate_unique", False))
        observation.update({
            "top_navigation_found": observation["nav_count"] + observation["header_count"] + observation["link_count"] + observation["button_count"] > 0,
            "link_count": sum(item.get("tag") == "a" for item in elements),
            "button_count": sum(item.get("tag") == "button" for item in elements),
            "role_link_count": sum(item.get("role") == "link" for item in elements),
            "role_button_count": sum(item.get("role") == "button" for item in elements),
        })
        observation["navigation_resolution_ready"] = observation["settings_candidate_unique"]
        self._trace(trace, "top_navigation_found", observation["top_navigation_found"])
        for key in (
            "nav_count", "header_count", "link_count", "button_count", "role_link_count", "role_button_count",
            "exact_settings_text_count", "normalized_settings_text_count", "settings_text_on_child_count",
            "settings_text_element_count", "settings_directly_clickable_count", "settings_clickable_parent_count",
            "settings_clickable_ancestor_count", "settings_candidate_count", "settings_candidate_unique",
        ):
            self._trace(trace, key, observation[key])
        self._trace(trace, "navigation_resolution_ready", observation["navigation_resolution_ready"])
        return observation

    def find_unique_navigation_target(self, expected_name: str, *, known_href_paths: set[str] | None = None, trace=None):
        """Resolve one target with an in-memory name check before verified href fallback."""
        started_at = time.monotonic()
        name_deadline = started_at + 5.0
        deadline = started_at + 10.0
        stale_retries = 0
        empty_snapshot_repeat_count = 0
        previous_signature = None
        dom_changed = False
        href_started_at = None
        href_paths = set(known_href_paths or set())
        while time.monotonic() <= deadline:
            candidates = []
            try:
                candidates.extend(self._collect_navigation_candidates(expected_name, href_paths))
                python_candidates, observation = self._collect_navigation_candidates_from_webelements(expected_name, href_paths)
                candidates.extend(python_candidates)
                self._last_navigation_observation = observation
                if not candidates:
                    candidates.extend(self._collect_cdp_accessibility_candidates(expected_name))
                    if candidates:
                        self._trace(trace, "settings_resolution_method", "accessibility_tree")
                if not candidates:
                    candidates.extend(self._collect_pseudo_element_candidates(expected_name))
                    if candidates:
                        self._trace(trace, "settings_resolution_method", "css_pseudo_element")
                if not candidates:
                    structure_candidate, structure_observation = self._collect_top_navigation_structure_candidate(expected_name)
                    for key in ("group_count", "item_count", "group_unique", "geometry_valid", "active_count", "same_parent", "all_have_href"):
                        self._trace(trace, f"top_menu_{key}", structure_observation.get(key, 0 if key.endswith("count") else False))
                    if structure_candidate is not None:
                        candidates.append(structure_candidate)
                        self._trace(trace, "settings_resolution_method", "verified_top_navigation_structure")
                        self._trace(trace, "top_navigation_fallback_used", True)
                        self._trace(trace, "top_navigation_fallback_preconditions_valid", True)
                signature = (observation["element_count"], observation["accessible_name_nonblank_count"], tuple(observation["href_path_fingerprints"]))
                dom_changed = dom_changed or previous_signature is not None and signature != previous_signature
                previous_signature = signature
                if not candidates:
                    empty_snapshot_repeat_count += 1
                if href_started_at is None and time.monotonic() > name_deadline:
                    href_started_at = time.monotonic()
                    href_paths.update(self._verified_href_paths_from_navigation_group(expected_name))
                    candidates.extend(self._collect_navigation_candidates(expected_name, href_paths))
                driver = self.browser.driver
                if not candidates and driver is not None:
                    for iframe_index, frame in enumerate(driver.find_elements(By.CSS_SELECTOR, "iframe")):
                        try:
                            driver.switch_to.frame(frame)
                            candidates.extend(self._collect_navigation_candidates(expected_name, href_paths, iframe_index=iframe_index))
                        except StaleElementReferenceException:
                            stale_retries += 1
                        finally:
                            driver.switch_to.default_content()
                unique = []
                for candidate in candidates:
                    element = candidate.get("element")
                    if all(element is not existing.get("element") for existing in unique):
                        unique.append(candidate)
                if len(unique) == 1:
                    method = unique[0].get("candidate_reason", "accessible_name")
                    if method == "accessible_name":
                        method = "accessible_name"
                    self._trace_navigation_resolution(trace, started_at, href_started_at, empty_snapshot_repeat_count, dom_changed, method, observation)
                    return unique[0]["element"], 1, unique[0]
                if len(unique) > 1:
                    self._last_navigation_evidence = [self._safe_navigation_evidence(item) for item in unique]
                    self._trace_navigation_resolution(trace, started_at, href_started_at, empty_snapshot_repeat_count, dom_changed, "unresolved", observation)
                    return None, len(unique), {"candidates": unique, "stale_retry_count": stale_retries}
            except StaleElementReferenceException:
                stale_retries += 1
            remaining = deadline - time.monotonic()
            if remaining > 0:
                WebDriverWait(self.browser.driver, min(0.25, remaining), poll_frequency=0.25).until(lambda _driver: True)
        self._trace_navigation_resolution(trace, started_at, href_started_at, empty_snapshot_repeat_count, dom_changed, "unresolved", observation if 'observation' in locals() else {})
        self._last_navigation_evidence = []
        return None, 0, {"candidates": [], "stale_retry_count": stale_retries}

    def _trace_navigation_resolution(self, trace, started_at, href_started_at, repeats, changed, method, observation):
        self._trace(trace, "settings_accessible_name_elapsed_ms", int(((href_started_at or time.monotonic()) - started_at) * 1000))
        self._trace(trace, "settings_href_resolution_elapsed_ms", 0 if href_started_at is None else int((time.monotonic() - href_started_at) * 1000))
        self._trace(trace, "settings_empty_snapshot_repeat_count", repeats)
        self._trace(trace, "settings_dom_changed_during_wait", changed)
        self._trace(trace, "settings_resolution_method", method)
        for key in ("accessible_name_source_count", "accessible_name_nonblank_count", "japanese_name_detected_count", "expected_name_exact_match_count"):
            self._trace(trace, key, observation.get(key, 0))

    def _collect_navigation_candidates_from_webelements(self, expected_name: str, known_href_paths: set[str]):
        driver = self.browser.driver
        empty = {"element_count": 0, "accessible_name_source_count": 0, "accessible_name_nonblank_count": 0, "japanese_name_detected_count": 0, "expected_name_exact_match_count": 0, "href_path_fingerprints": []}
        if driver is None:
            return [], empty
        elements = self._safe_find_driver_elements(driver, By.CSS_SELECTOR, "a,button,[role='link'],[role='button'],[role='menuitem']")
        candidates = []
        source_count = nonblank_count = japanese_count = exact_count = 0
        fingerprints = []
        expected = self._normalize_navigation_name(expected_name)
        for index, element in enumerate(elements):
            raw_values = [self._safe_attribute(element, "aria-label"), self._safe_attribute(element, "title"), self._safe_element_text_for_diagnostic(element)]
            try:
                values = driver.execute_script("return [arguments[0].innerText || '', arguments[0].textContent || ''];", element) or []
                raw_values.extend(values)
            except Exception:
                pass
            values = [self._normalize_navigation_name(value) for value in raw_values if isinstance(value, str) and self._normalize_navigation_name(value)]
            source_count += len(raw_values)
            nonblank_count += len(values)
            japanese_count += int(any(any("\u3040" <= char <= "\u30ff" or "\u4e00" <= char <= "\u9fff" for char in value) for value in values))
            exact = expected in values
            exact_count += int(exact)
            href = self._safe_attribute(element, "href") or ""
            path = urlparse(href).path if href else ""
            if path:
                fingerprints.append(hashlib.sha256(path.encode("utf-8")).hexdigest()[:12])
            if exact or path in known_href_paths:
                if self._safe_bool(element, "is_displayed") and self._safe_bool(element, "is_enabled"):
                    candidates.append({"element": element, "element_index": index, "tag": self._safe_tag(element), "accessible_name_source": "webelement_or_javascript", "href_present": bool(path), "href_path": path, "displayed": True, "enabled": True, "disabled": False, "candidate_reason": "accessible_name" if exact else "verified_href"})
        observation = {"element_count": len(elements), "accessible_name_source_count": source_count, "accessible_name_nonblank_count": nonblank_count, "japanese_name_detected_count": japanese_count, "expected_name_exact_match_count": exact_count, "href_path_fingerprints": fingerprints}
        return candidates, observation

    @staticmethod
    def _normalize_navigation_name(value: str) -> str:
        return " ".join(str(value or "").replace("\u00a0", " ").split())

    def _collect_cdp_accessibility_candidates(self, expected_name: str):
        driver = self.browser.driver
        if driver is None or not callable(getattr(driver, "execute_cdp_cmd", None)):
            return []
        try:
            driver.execute_cdp_cmd("Accessibility.enable", {})
            tree = driver.execute_cdp_cmd("Accessibility.getFullAXTree", {}) or {}
        except Exception:
            return []
        expected = self._normalize_navigation_name(expected_name)
        candidates = []
        for node in tree.get("nodes", []):
            name = node.get("name", {}) if isinstance(node, dict) else {}
            name_value = name.get("value") if isinstance(name, dict) else None
            if self._normalize_navigation_name(name_value) != expected:
                continue
            backend_id = node.get("backendDOMNodeId")
            if not backend_id:
                continue
            try:
                described = driver.execute_cdp_cmd("DOM.describeNode", {"backendNodeId": backend_id}) or {}
                attributes = described.get("node", {}).get("attributes", [])
                attrs = dict(zip(attributes[0::2], attributes[1::2]))
                tag = str(described.get("node", {}).get("nodeName", "")).casefold()
            except Exception:
                continue
            for element in self._safe_find_driver_elements(driver, By.CSS_SELECTOR, "a,button,[role='link'],[role='button'],[role='menuitem']"):
                if not self._safe_bool(element, "is_displayed") or not self._safe_bool(element, "is_enabled"):
                    continue
                same_id = attrs.get("id") and attrs.get("id") == self._safe_attribute(element, "id")
                same_role = attrs.get("role") and attrs.get("role") == self._safe_attribute(element, "role")
                same_tag = tag and tag == self._safe_tag(element)
                if same_id or (same_role and same_tag) or (same_tag and self._normalize_navigation_name(self._safe_element_text_for_diagnostic(element)) == expected):
                    candidates.append({"element": element, "tag": self._safe_tag(element), "accessible_name_source": "accessibility_tree", "candidate_reason": "accessibility_tree", "displayed": True, "enabled": True, "disabled": False, "href_path": urlparse(self._safe_attribute(element, "href") or "").path})
                    break
        return candidates

    def _collect_pseudo_element_candidates(self, expected_name: str):
        driver = self.browser.driver
        if driver is None:
            return []
        script = """
            const expected = String(arguments[0] || '').replace(/[\\s\\u00a0]+/g, ' ').trim();
            const normalizeContent = value => String(value || '').replace(/^['\"]|['\"]$/g, '').replace(/[\\s\\u00a0]+/g, ' ').trim();
            const candidates = [];
            document.querySelectorAll('a,button,[role="link"],[role="button"],[role="menuitem"],*').forEach((item, index) => {
                const before = normalizeContent(getComputedStyle(item, '::before').content);
                const after = normalizeContent(getComputedStyle(item, '::after').content);
                if (before === expected || after === expected) {
                    let target = item;
                    while (target && !['A', 'BUTTON'].includes(target.tagName) && !target.getAttribute('role')) target = target.parentElement;
                    if (target) candidates.push({element: target, element_index: index, accessible_name_source: before === expected ? 'css_before' : 'css_after', candidate_reason: 'pseudo_element', displayed: Boolean(target.offsetWidth || target.offsetHeight || target.getClientRects().length), enabled: !target.disabled, disabled: Boolean(target.disabled), href_path: target.getAttribute('href') ? new URL(target.getAttribute('href'), document.baseURI).pathname : ''});
                }
            });
            return candidates;
        """
        records = driver.execute_script(script, expected_name) or []
        return [record for record in records if isinstance(record, dict) and record.get("displayed") and record.get("enabled") and not record.get("disabled")]

    def _collect_top_navigation_structure_candidate(self, expected_name: str):
        driver = self.browser.driver
        if driver is None:
            return None, {}
        script = """
            const expectedOrder = ['機器', 'ユーザー', '組織', '設定', 'ログ', 'DXサポート'];
            const normalize = value => String(value || '').replace(/[\\s\\u00a0]+/g, ' ').trim().replace(/^['\"]|['\"]$/g, '');
            const name = item => normalize(item.innerText || item.textContent || item.getAttribute('aria-label') || item.getAttribute('title') || getComputedStyle(item, '::before').content || getComputedStyle(item, '::after').content);
            const visible = item => Boolean(item.offsetWidth || item.offsetHeight || item.getClientRects().length) && !item.disabled;
            const links = Array.from(document.querySelectorAll('a,button,[role="link"],[role="button"],[role="menuitem"]')).filter(visible);
            const groups = [];
            links.forEach(item => {
                const parent = item.parentElement;
                if (!parent) return;
                const members = links.filter(other => other.parentElement === parent);
                if (members.length === 6 && !groups.some(group => group.parent === parent)) groups.push({parent, members});
            });
            const valid = groups.filter(group => {
                const ordered = [...group.members].sort((a,b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
                const names = ordered.map(name);
                const hrefs = ordered.map(item => item.getAttribute('href'));
                const host = location.host;
                const sameHost = hrefs.every(href => href && new URL(href, document.baseURI).host === host);
                const active = ordered.filter(item => item.getAttribute('aria-current') || /active|selected/.test(item.className || '')).length;
                const horizontal = ordered.every((item, index) => index === 0 || item.getBoundingClientRect().left > ordered[index - 1].getBoundingClientRect().left);
                const activeNames = ordered.filter(item => item.getAttribute('aria-current') || /active|selected/.test(item.className || '')).map(name);
                return JSON.stringify(names) === JSON.stringify(expectedOrder) && sameHost && active === 1 && activeNames[0] === '機器' && horizontal;
            });
            if (valid.length !== 1) return {valid: false, group_count: groups.length, item_count: valid.length ? valid[0].members.length : 0, group_unique: false, geometry_valid: false, active_count: 0, same_parent: false, all_have_href: false};
            const ordered = [...valid[0].members].sort((a,b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
            return {valid: true, group_count: valid.length, item_count: ordered.length, group_unique: true, geometry_valid: true, active_count: ordered.filter(item => item.getAttribute('aria-current') || /active|selected/.test(item.className || '')).length, same_parent: true, all_have_href: ordered.every(item => Boolean(item.getAttribute('href'))), element: ordered[3], href_path: new URL(ordered[3].getAttribute('href'), document.baseURI).pathname, accessible_name_source: 'verified_top_navigation_structure', candidate_reason: 'verified_top_navigation_structure', displayed: true, enabled: true, disabled: false};
        """
        result = driver.execute_script(script, expected_name) or {}
        if not isinstance(result, dict):
            return None, {}
        candidate = result.get("element")
        if not result.get("valid") or candidate is None:
            return None, result
        result["element"] = candidate
        return result, result

    def _verified_href_paths_from_navigation_group(self, expected_name: str) -> set[str]:
        driver = self.browser.driver
        if driver is None:
            return set()
        expected = self._normalize_navigation_name(expected_name)
        paths = set()
        for element in self._safe_find_driver_elements(driver, By.CSS_SELECTOR, "a,button,[role='link'],[role='button'],[role='menuitem']"):
            values = [self._safe_attribute(element, "aria-label"), self._safe_attribute(element, "title"), self._safe_element_text_for_diagnostic(element)]
            try:
                values.extend(driver.execute_script("return [arguments[0].innerText || '', arguments[0].textContent || ''];", element) or [])
            except Exception:
                pass
            if any(self._normalize_navigation_name(value) == expected for value in values if isinstance(value, str)):
                href = self._safe_attribute(element, "href") or ""
                path = urlparse(href).path if href else ""
                if path:
                    paths.add(path)
        return paths

    @staticmethod
    def _safe_navigation_evidence(candidate: dict[str, object]) -> dict[str, object]:
        path = candidate.get("href_path")
        return {
            "element_index": candidate.get("element_index", -1),
            "tag": candidate.get("tag"),
            "href_present": bool(path),
            "href_path_fingerprint": hashlib.sha256(path.encode("utf-8")).hexdigest()[:12] if isinstance(path, str) and path else None,
            "displayed": bool(candidate.get("displayed")),
            "enabled": bool(candidate.get("enabled")),
            "disabled": bool(candidate.get("disabled")),
            "accessible_name_source": candidate.get("accessible_name_source"),
            "candidate_reason": candidate.get("candidate_reason"),
        }

    def _collect_navigation_candidates(self, expected_name: str, known_href_paths: set[str] | None = None, *, iframe_index: int = -1):
        driver = self.browser.driver
        if driver is None:
            return []
        script = """
            const expected = arguments[0];
            const knownPaths = new Set(arguments[1] || []);
            const normalize = value => String(value || '').replace(/[\\s\\u00a0]+/g, ' ').trim();
            const clickable = item => item && (
                ['A', 'BUTTON'].includes(item.tagName) ||
                ['link', 'button', 'menuitem'].includes((item.getAttribute('role') || '').toLowerCase()) ||
                item.hasAttribute('onclick') ||
                (item.hasAttribute('tabindex') && item.tabIndex >= 0)
            );
            const ancestor = item => {
                let current = item;
                while (current) {
                    if (clickable(current)) return current;
                    current = current.parentElement;
                }
                return null;
            };
            const labelled = item => {
                const labelledby = item.getAttribute('aria-labelledby');
                const labelledText = labelledby ? labelledby.split(/\\s+/).map(id => document.getElementById(id)).filter(Boolean).map(node => node.innerText || node.textContent || '').join(' ') : '';
                const imageAlt = Array.from(item.querySelectorAll('img[alt]')).map(node => node.getAttribute('alt')).join(' ');
                const svgTitle = Array.from(item.querySelectorAll('svg[aria-label], svg title')).map(node => node.getAttribute('aria-label') || node.textContent || '').join(' ');
                const paths = [
                    ['aria-label', item.getAttribute('aria-label')],
                    ['aria-labelledby', labelledText],
                    ['title', item.getAttribute('title')],
                    ['innerText', item.innerText],
                    ['textContent', item.textContent],
                    ['descendant_innerText', Array.from(item.querySelectorAll('*')).map(node => node.innerText || '').join(' ')],
                    ['descendant_textContent', Array.from(item.querySelectorAll('*')).map(node => node.textContent || '').join(' ')],
                    ['img_alt', imageAlt],
                    ['svg', svgTitle]
                ];
                return paths.map(([source, value]) => ({source, value: normalize(value)})).find(item => item.value) || {source: 'none', value: ''};
            };
            const contexts = [document];
            const visitShadow = root => {
                root.querySelectorAll('*').forEach(item => {
                    if (item.shadowRoot) { contexts.push(item.shadowRoot); visitShadow(item.shadowRoot); }
                });
            };
            visitShadow(document);
            const items = Array.from(new Set(contexts.flatMap(root => Array.from(root.querySelectorAll('*')))));
            const records = [];
            const targetSet = new Set();
            items.forEach((item, index) => {
                const name = labelled(item);
                const target = name.value === expected ? item : (name.value ? null : ancestor(item));
                const clickableTarget = target && clickable(target) ? target : (target ? ancestor(target) : null);
                if (!clickableTarget) return;
                if (targetSet.has(clickableTarget)) return;
                const targetName = labelled(clickableTarget);
                const href = clickableTarget.getAttribute('href');
                const path = href ? new URL(href, document.baseURI).pathname : '';
                if (targetName.value !== expected && !knownPaths.has(path)) return;
                targetSet.add(clickableTarget);
                records.push({
                    element: clickableTarget,
                    element_index: index,
                    tag: clickableTarget.tagName.toLowerCase(),
                    accessible_name_source: targetName.source,
                    href_present: Boolean(href),
                    href_path: path,
                    displayed: Boolean(clickableTarget.offsetWidth || clickableTarget.offsetHeight || clickableTarget.getClientRects().length),
                    enabled: !clickableTarget.disabled,
                    disabled: clickableTarget.disabled === true || clickableTarget.hasAttribute('disabled'),
                    candidate_reason: targetName.value === expected ? targetName.source : 'confirmed_href_path'
                });
            });
            return records;
        """
        records = driver.execute_script(script, expected_name, sorted(known_href_paths or set())) or []
        return [record for record in records if isinstance(record, dict) and record.get("displayed") is True and record.get("enabled") is True and record.get("disabled") is False]

    def _find_diagnostic_navigation_element(self, kind: str):
        driver = self.browser.driver
        if driver is None:
            raise RuntimeError("SMSMナビゲーションDOMを確認できません")
        specs = {
            "settings": ({"settings", "setting", "menu-settings", "設定"}, {"/settings"}, {"設定", "Settings"}),
            "ios": ({"ios", "menu-ios", "ios-settings", "ios設定"}, {"/ios"}, {"iOS"}),
            "certificate_management": ({"certificate-management", "certificate_management", "証明書管理"}, {"/certificates", "/certificate-management"}, {"証明書管理", "Certificate Management"}),
            "client_certificate_management": ({"client-certificate-management", "client_certificate_management", "クライアント証明書管理"}, {"/client-certificates", "/client-certificate-management"}, {"クライアント証明書管理", "Client Certificate Management"}),
        }
        exact_values, exact_paths, exact_texts = specs[kind]
        candidates = []
        for element in driver.find_elements(By.CSS_SELECTOR, "a,button,[role='link'],[role='menuitem']"):
            if not self._safe_bool(element, "is_displayed") or not self._safe_bool(element, "is_enabled"):
                continue
            attributes = [
                self._safe_attribute(element, "id"),
                self._safe_attribute(element, "name"),
                self._safe_attribute(element, "data-testid"),
                self._safe_attribute(element, "aria-label"),
            ]
            href = self._safe_attribute(element, "href") or ""
            path = urlparse(href).path.casefold() if href else ""
            role = self._safe_attribute(element, "role") or ""
            text = self._safe_element_text_for_diagnostic(element)
            attribute_match = any(value and value.casefold() in {item.casefold() for item in exact_values} for value in attributes)
            path_match = path in {item.casefold() for item in exact_paths}
            text_match = text in exact_texts or (role.casefold() in {"link", "menuitem", "button"} and text in exact_texts)
            if attribute_match or path_match or text_match:
                if all(element is not existing for existing in candidates):
                    candidates.append(element)
        return (candidates[0] if len(candidates) == 1 else None), len(candidates)

    def _click_diagnostic_navigation_element(self, element) -> None:
        if element is None:
            raise RuntimeError("SMSMナビゲーション要素をクリックできません")
        element.click()

    def _click_diagnostic_navigation_target(self, element, evidence: dict[str, object]) -> None:
        if element is None:
            raise RuntimeError("SMSMナビゲーション要素をクリックできません")
        try:
            element.click()
            return
        except Exception as click_error:
            driver = self.browser.driver
            current = self._current_url()
            href = self._safe_attribute(element, "href") or ""
            current_parts = urlparse(current)
            href_parts = urlparse(href)
            observed_path = evidence.get("href_path")
            if not driver or not observed_path or not href_parts.path or href_parts.path != observed_path:
                raise RuntimeError("SMSMナビゲーションクリック失敗") from click_error
            if href_parts.netloc and href_parts.netloc.casefold() != current_parts.netloc.casefold():
                raise RuntimeError("外部ホストへのSMSM遷移を拒否しました") from click_error
            if href_parts.scheme and href_parts.scheme.casefold() != current_parts.scheme.casefold():
                raise RuntimeError("SMSM遷移スキームを確認できません") from click_error
            driver.get(f"{current_parts.scheme}://{current_parts.netloc}{observed_path}")

    def _wait_for_diagnostic_navigation(self, next_kind: str) -> None:
        before_url = self._current_url()
        expected_names = {
            "ios": "iOS",
            "certificate_management": "証明書管理",
            "client_certificate_management": "クライアント証明書管理",
        }
        def reached(_driver):
            if self._current_url() != before_url:
                return True
            try:
                expected = expected_names[next_kind]
                javascript_candidates = self._collect_navigation_candidates(expected, set())
                python_candidates, _observation = self._collect_navigation_candidates_from_webelements(expected, set())
                return len({id(item.get("element")) for item in javascript_candidates + python_candidates}) == 1
            except Exception:
                return False
        try:
            WebDriverWait(self.browser.driver, 15, poll_frequency=0.1).until(reached)
        except TimeoutException as exc:
            raise RuntimeError("SMSM画面遷移を確認できません") from exc

    def _inspect_client_certificate_upload_dom(self) -> dict[str, object]:
        driver = self.browser.driver
        if driver is None:
            raise RuntimeError("クライアント証明書管理DOMを確認できません")
        script = """
            const forms = Array.from(document.querySelectorAll('form'));
            const elements = Array.from(document.querySelectorAll('form, input, select, button, label'));
            const labels = Array.from(document.querySelectorAll('label'));
            const labelFor = new Set(labels.map(item => item.getAttribute('for')).filter(Boolean));
            const formIndex = form => forms.indexOf(form);
            const displayed = item => Boolean(item.offsetWidth || item.offsetHeight || item.getClientRects().length);
            const schema = elements.map((item, index) => {
                const form = item.closest('form');
                const tag = item.tagName.toLowerCase();
                const type = item.getAttribute('type');
                const linked = tag === 'label' || Boolean(item.id && labelFor.has(item.id)) || Boolean(item.closest('label'));
                return {
                    element_index: index,
                    tag,
                    type,
                    id: item.getAttribute('id'),
                    name: item.getAttribute('name'),
                    role: item.getAttribute('role'),
                    'data-testid': item.getAttribute('data-testid'),
                    accept: item.getAttribute('accept'),
                    autocomplete: item.getAttribute('autocomplete'),
                    inputmode: item.getAttribute('inputmode'),
                    displayed: displayed(item),
                    enabled: !item.disabled,
                    readonly: item.readOnly === true || item.hasAttribute('readonly'),
                    disabled: item.disabled === true || item.hasAttribute('disabled'),
                    label_linked: linked,
                    parent_tag: item.parentElement ? item.parentElement.tagName.toLowerCase() : null,
                    form_index: formIndex(form)
                };
            });
            const inForm = item => item.closest('form') !== null;
            const files = elements.filter(item => item.tagName.toLowerCase() === 'input' && (item.getAttribute('type') || '').toLowerCase() === 'file' && inForm(item) && displayed(item) && !item.disabled && !item.hasAttribute('disabled'));
            const passwords = elements.filter(item => item.tagName.toLowerCase() === 'input' && (item.getAttribute('type') || '').toLowerCase() === 'password' && inForm(item) && displayed(item) && !item.disabled && !item.hasAttribute('disabled') && !item.readOnly && !item.hasAttribute('readonly'));
            const uploadWords = /upload|add|register|証明書を追加|追加|登録/i;
            const deleteWords = /delete|remove|削除/i;
            const buttons = elements.filter(item => ['button', 'input'].includes(item.tagName.toLowerCase()) && inForm(item) && displayed(item) && !item.disabled && !item.hasAttribute('disabled'));
            const uploadButtons = buttons.filter(item => {
                const value = [item.textContent || '', item.getAttribute('aria-label') || '', item.getAttribute('title') || '', item.getAttribute('value') || ''].join(' ');
                return uploadWords.test(value) && !deleteWords.test(value) && !/save|保存/i.test(value);
            });
            const tables = Array.from(document.querySelectorAll('table'));
            const bodyText = document.body ? document.body.innerText : '';
            const uploadSuccessMessageDetected = /success|登録完了|追加しました|アップロード完了/i.test(bodyText);
            return {
                schema,
                upload_form_count: forms.length,
                file_input_count: files.length,
                password_input_count: passwords.length,
                upload_button_candidate_count: uploadButtons.length,
                certificate_table_count: tables.length,
                existing_certificate_row_count: tables.reduce((total, table) => total + Array.from(table.querySelectorAll('tbody tr')).filter(row => displayed(row)).length, 0),
                upload_success_message_detected: uploadSuccessMessageDetected
            };
        """
        result = driver.execute_script(script)
        if not isinstance(result, dict):
            raise RuntimeError("クライアント証明書管理DOMの取得結果が不正です")
        schema = [item for item in result.get("schema", []) if isinstance(item, dict)]
        forms = [item for item in schema if item.get("tag") == "form"]
        files = [item for item in schema if item.get("tag") == "input" and str(item.get("type") or "").casefold() == "file" and item.get("form_index", -1) >= 0 and item.get("displayed") is True and item.get("enabled") is True and item.get("disabled") is False]
        passwords = [item for item in schema if item.get("tag") == "input" and str(item.get("type") or "").casefold() == "password" and item.get("form_index", -1) >= 0 and item.get("displayed") is True and item.get("enabled") is True and item.get("readonly") is False and item.get("disabled") is False]
        return {
            "schema": schema,
            "upload_form_count": len(forms),
            "upload_form_unique": len(forms) == 1,
            "file_input_count": len(files),
            "file_input_unique": len(files) == 1,
            "password_input_count": len(passwords),
            "password_input_unique": len(passwords) == 1,
            "upload_button_candidate_count": int(result.get("upload_button_candidate_count", 0)),
            "upload_button_unique": int(result.get("upload_button_candidate_count", 0)) == 1,
            "certificate_table_count": int(result.get("certificate_table_count", 0)),
            "existing_certificate_row_count": int(result.get("existing_certificate_row_count", 0)),
            "upload_success_message_detected": result.get("upload_success_message_detected") is True,
        }

    @staticmethod
    def _safe_element_text_for_diagnostic(element) -> str:
        try:
            text = element.text
        except Exception:
            return ""
        return text.strip() if isinstance(text, str) else ""

    def _find_login_button(self):
        locators = [
            (By.CSS_SELECTOR, "button[type='submit']"),
            (By.CSS_SELECTOR, "input[type='submit']"),
            (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'login')]"),
            (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sign in')]"),
        ]
        find_clickable_first = getattr(self.browser, "find_clickable_first", None)
        if callable(find_clickable_first):
            return find_clickable_first(locators, timeout=5)

        last_error = None
        for by, value in locators:
            try:
                return self.browser.wait_for_clickable(by, value, timeout=5)
            except TimeoutException as exc:
                last_error = exc
        raise last_error or TimeoutException("SMSMログインボタンが見つかりませんでした")

    def _click_first_required(self, locators, label: str) -> None:
        try:
            self.browser.click_first(locators, timeout=5)
            self.browser.wait_for_page_ready()
        except Exception as exc:
            raise RuntimeError(f"{label}が見つからないかクリックできません") from exc

    def _wait_for_login_success(self, timeout: int = 30, trace=None) -> None:
        wait_started_at = time.monotonic()
        first_evaluation = True
        def login_completed(_driver):
            nonlocal first_evaluation
            if first_evaluation:
                self._trace_elapsed(trace, "smsm_login_completion_first_eval", wait_started_at)
                first_evaluation = False
            state = self._login_completion_state()
            if trace is not None:
                for key, value in state.items():
                    self._trace(trace, key, value)
            if state["login_error_banner_detected"]:
                raise RuntimeError("SMSMログインエラー表示")
            if state["login_completed"]:
                return True
            return False

        try:
            WebDriverWait(self.browser.driver, timeout, poll_frequency=0.1).until(login_completed)
        except TimeoutException as exc:
            raise RuntimeError("SMSMログイン後の管理画面遷移を確認できませんでした") from exc

    def _is_login_success(self) -> bool:
        return self._login_completion_state()["login_completed"]

    def _login_completion_state(self) -> dict[str, bool]:
        current_url = self._current_url()
        origin = urlparse(self._login_origin_url)
        current = urlparse(current_url)
        same_smsm_host = bool(origin.hostname and current.hostname and origin.hostname.casefold() == current.hostname.casefold())
        login_path_changed = current.path != origin.path
        company_visible = self._visible_login_field("user_company_code", "user[company_code]")
        user_visible = self._visible_login_field("user_login", "user[login]")
        password_visible = self._visible_login_field("user_password", "user[password]")
        login_form_still_visible = company_visible or user_visible or password_visible
        error_detected = self._has_elements([
            (By.CSS_SELECTOR, ".alert-danger"),
            (By.CSS_SELECTOR, ".alert-error"),
        ])
        post_login_landmark_count = 0
        for selector in ("a[href],button,[role='link'],[role='navigation'],nav",):
            post_login_landmark_count += len([
                element for element in self._safe_find_driver_elements(self.browser.driver, By.CSS_SELECTOR, selector)
                if self._safe_bool(element, "is_displayed")
            ])
        post_login_landmark_found = post_login_landmark_count > 0
        login_completed = (
            not error_detected
            and not login_form_still_visible
            and same_smsm_host
            and (login_path_changed or post_login_landmark_found)
        )
        return {
            "login_form_still_visible": login_form_still_visible,
            "company_field_still_visible": company_visible,
            "user_field_still_visible": user_visible,
            "password_field_still_visible": password_visible,
            "login_path_changed": login_path_changed,
            "same_smsm_host": same_smsm_host,
            "post_login_landmark_found": post_login_landmark_found,
            "post_login_landmark_count": post_login_landmark_count,
            "login_error_banner_detected": error_detected,
            "login_completed": login_completed,
            "login_success_condition_method": "form_hidden_same_host_path_changed" if login_path_changed else "form_hidden_same_host_post_landmark",
        }

    def _current_url(self) -> str:
        if self.browser.driver is None:
            return ""
        try:
            value = self.browser.driver.current_url
        except Exception:
            return ""
        return value if isinstance(value, str) else ""

    def _visible_login_field(self, element_id: str, element_name: str) -> bool:
        driver = self.browser.driver
        if driver is None:
            return False
        elements = []
        try:
            elements.extend(driver.find_elements(By.ID, element_id))
            elements.extend(driver.find_elements(By.NAME, element_name))
        except Exception:
            return False
        seen = set()
        for element in elements:
            if id(element) in seen:
                continue
            seen.add(id(element))
            if self._safe_bool(element, "is_displayed"):
                return True
        return False

    def _has_elements(self, locators) -> bool:
        driver = self.browser.driver
        if driver is None:
            return False
        for by, value in locators:
            try:
                if any(self._safe_bool(element, "is_displayed") for element in driver.find_elements(by, value)):
                    return True
            except Exception:
                continue
        return False

    def _wait_for_success_message(self, operation: str) -> None:
        success_locators = [
            (By.CLASS_NAME, "alert-success"),
            (By.CSS_SELECTOR, ".alert.alert-success"),
            (By.CSS_SELECTOR, ".toast-success"),
            (By.XPATH, "//div[contains(@class, 'success') and (contains(., '保存') or contains(., '成功') or contains(., '完了'))]"),
        ]

        try:
            message = self.browser.find_first(success_locators, timeout=8)
            self.logger.info(f"{operation}成功を確認: {(message.text or '').strip()}")
        except Exception as exc:
            self.logger.exception(f"{operation}の成功メッセージが確認できませんでした")
            raise RuntimeError(f"{operation}失敗") from exc


def enumerate_smsm_search_controls(driver) -> dict[str, object]:
    inspector = SmsmHandler.__new__(SmsmHandler)
    inspector.browser = type("BrowserContext", (), {"driver": driver})()
    return inspector.enumerate_search_controls(driver)
