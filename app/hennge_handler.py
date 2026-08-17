from pathlib import Path
import hashlib
import json
import re
import time
import shutil
import unicodedata
from math import ceil
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait


def _usable_password_values(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    masked = {"*", "•", "●", "••••", "••••••••"}
    return [
        value.strip()
        for value in values
        if isinstance(value, str) and value.strip() and value.strip() not in masked
    ]


def _read_windows_clipboard_once() -> str:
    import win32clipboard

    win32clipboard.OpenClipboard()
    try:
        value = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()
    return value if isinstance(value, str) else ""


def _clear_windows_clipboard() -> None:
    import win32clipboard

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
    finally:
        win32clipboard.CloseClipboard()


def _valid_clipboard_password(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    if not normalized or len(normalized) > 4096:
        return False
    return normalized not in {"*", "•", "●", "••••", "••••••••"}


class HenngeHandler:
    ADMIN_URL = "https://admin.auth.hennge.com"

    def __init__(self, config: dict, logger, browser):
        self.config = config
        self.logger = logger
        self.browser = browser
        self.last_search_observation: dict[str, object] = {}
        self.last_password_observation: dict[str, object] = {}

    def login(self) -> None:
        self.logger.info("HENNGEログイン処理を開始")
        try:
            self.browser.open(self.ADMIN_URL)
            self.browser.wait_for_page_ready()
            self.logger.info("HENNGEページを開きました")

            self._fill_domain_step_if_required()

            self.logger.info("HENNGE認証フォームの表示を待機します")
            form_state = self._wait_for_credential_form(timeout_sec=90)
            if form_state == "domain_error":
                raise RuntimeError("HENNGEログイン失敗: ドメインが認識されない、または管理UIが未設定です")
            if form_state == "timeout":
                raise RuntimeError("HENNGEログイン失敗: ID/PASSフォームの表示がタイムアウトしました")
            if form_state != "credential_form":
                raise RuntimeError(f"HENNGEログイン失敗: 想定外の待機状態です ({form_state})")

            username = self.config.get("hennge", {}).get("username", "")
            password = self.config.get("hennge", {}).get("password", "")
            if not username or not password:
                raise RuntimeError("HENNGEログイン失敗: 資格情報が不足しています")

            self._fill_credential_form(username, password)

            if not self._submit_login():
                raise RuntimeError("HENNGEログイン失敗: ログインボタンを操作できませんでした")

            state = self._wait_for_login_state(timeout_sec=180)
            if state == "domain_error":
                self.logger.info("ドメイン要求エラーを検知したため、ドメイン入力を再試行します")
                self._fill_domain_step_if_required(force=True)
                if not self._submit_login():
                    raise RuntimeError("HENNGEログイン失敗: 再試行時にログインボタンを操作できませんでした")
                state = self._wait_for_login_state(timeout_sec=180)

            if state == "domain_error":
                raise RuntimeError("HENNGEログイン失敗: 管理用ユーザーインタフェースが未設定、またはドメイン入力が必要です")
            if state != "success":
                raise RuntimeError("HENNGEログイン失敗: 証明書選択待ち、またはログイン遷移が完了しませんでした")

            self.logger.info("HENNGEログイン成功を確認しました")
        except Exception:
            self.logger.exception("HENNGEログイン処理に失敗しました")
            try:
                self.logger.save_browser_diagnostics(self.browser.driver, "hennge_login_error")
            except Exception:
                self.logger.exception("HENNGEログイン失敗時の診断情報保存に失敗しました")
            raise

    def _fill_domain_step_if_required(self, force: bool = False) -> None:
        domain = (self.config.get("hennge", {}).get("domain", "") or "").strip()
        if not domain:
            return

        if not (force or self._is_domain_prompt_present()):
            return

        domain_locators = [
            (By.NAME, "domain"),
            (By.ID, "domain"),
            (By.CSS_SELECTOR, "input[name*='domain' i]"),
            (By.CSS_SELECTOR, "input[id*='domain' i]"),
            (By.CSS_SELECTOR, "input[placeholder*='ドメイン' i]"),
            (By.CSS_SELECTOR, "input[placeholder*='domain' i]"),
        ]
        before_url = self._current_sanitized_url()
        element = self.browser.find_first(domain_locators, timeout=4)
        element.clear()
        element.send_keys(domain)
        self.logger.info("HENNGEドメイン入力を実行しました")
        self._submit_domain_step()

        try:
            WebDriverWait(self.browser.driver, 30).until(
                lambda _driver: self._is_post_domain_transition_ready(before_url)
            )
        except Exception:
            self.logger.info("ドメイン送信後のURL遷移待機がタイムアウトしました。継続してフォーム待機へ進みます")

        self.browser.wait_for_page_ready(timeout=20)

    def _submit_domain_step(self) -> None:
        locators = [
            (By.CSS_SELECTOR, "button[type='submit']"),
            (By.CSS_SELECTOR, "input[type='submit']"),
            (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'next')]"),
            (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'continue')]"),
            (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '次へ')]"),
            (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '続行')]"),
        ]
        self.browser.click_first(locators, timeout=4)

    def _is_domain_prompt_present(self) -> bool:
        if self.browser.driver is None:
            return False

        page_text = (self.browser.driver.page_source or "").lower()
        prompt_markers = [
            "ドメインを入力",
            "unknown domain",
            "enter your domain",
        ]
        if any(marker in page_text for marker in prompt_markers):
            return True

        domain_locators = [
            (By.NAME, "domain"),
            (By.ID, "domain"),
            (By.CSS_SELECTOR, "input[name*='domain' i]"),
            (By.CSS_SELECTOR, "input[id*='domain' i]"),
        ]
        try:
            self.browser.find_first(domain_locators, timeout=2)
            return True
        except Exception:
            return False

    def _is_post_domain_transition_ready(self, before_url: str) -> bool:
        current_url = self._current_sanitized_url()
        host = urlsplit(current_url).netloc.lower()
        if current_url and before_url and current_url != before_url:
            return True
        if host == "ap.ssso.hdems.com":
            return True
        if self._has_credential_form():
            return True
        return False

    def _fill_credential_form(self, username: str, password: str) -> None:
        if self.browser.driver is None:
            raise RuntimeError("ブラウザが開始されていません")

        username_input = self._find_single_visible_exact(
            "input#login_user[name='login_user']",
            "ユーザー名欄",
        )
        password_input = self._find_single_visible_exact(
            "input#login_pwd[name='login_pwd'][type='password']",
            "パスワード欄",
        )

        self._ensure_same_form_parent(username_input, password_input)

        self._log_input_target("ユーザー名欄", username_input)
        self._log_input_target("パスワード欄", password_input)

        if not username_input.is_enabled():
            raise RuntimeError("HENNGEログイン失敗: ユーザー名欄が無効です")
        if username_input.get_attribute("disabled") is not None:
            raise RuntimeError("HENNGEログイン失敗: ユーザー名欄がdisabledです")
        if username_input.get_attribute("readonly") is not None:
            raise RuntimeError("HENNGEログイン失敗: ユーザー名欄がreadonlyです")

        self._safe_set_secret_input(username_input, username, send_tab=True)

        try:
            WebDriverWait(self.browser.driver, 10).until(
                lambda _driver: self._is_password_input_ready(password_input)
            )
        except TimeoutException as exc:
            raise RuntimeError("HENNGEログイン失敗: パスワード欄の入力準備が整いませんでした") from exc

        self._safe_set_secret_input(password_input, password, send_tab=False)

    def _find_single_visible_exact(self, css_selector: str, label: str):
        if self.browser.driver is None:
            raise RuntimeError("ブラウザが開始されていません")

        elements = self.browser.driver.find_elements(By.CSS_SELECTOR, css_selector)
        visible = [element for element in elements if element.is_displayed()]
        if len(visible) != 1:
            raise RuntimeError(f"HENNGEログイン失敗: {label}の表示中候補数が不正です ({len(visible)})")
        return visible[0]

    def _ensure_same_form_parent(self, username_input, password_input) -> None:
        user_forms = username_input.find_elements(By.XPATH, "ancestor::form[1]")
        pass_forms = password_input.find_elements(By.XPATH, "ancestor::form[1]")
        if len(user_forms) != 1 or len(pass_forms) != 1:
            raise RuntimeError("HENNGEログイン失敗: 入力欄のform要素を一意に特定できませんでした")

        user_form = user_forms[0]
        pass_form = pass_forms[0]
        same_form = False
        try:
            same_form = bool(self.browser.driver.execute_script("return arguments[0] === arguments[1];", user_form, pass_form))
        except Exception:
            user_form_id = (user_form.get_attribute("id") or "").strip()
            pass_form_id = (pass_form.get_attribute("id") or "").strip()
            if user_form_id and pass_form_id:
                same_form = user_form_id == pass_form_id
            else:
                same_form = user_form == pass_form

        if not same_form:
            raise RuntimeError("HENNGEログイン失敗: ユーザー名欄とパスワード欄が同一form内にありません")

    def _is_password_input_ready(self, password_input) -> bool:
        if not password_input.is_displayed():
            return False
        if not password_input.is_enabled():
            return False
        if password_input.get_attribute("disabled") is not None:
            return False
        if password_input.get_attribute("readonly") is not None:
            return False
        return True

    def _safe_set_secret_input(self, input_element, secret_value: str, send_tab: bool) -> None:
        input_type = (input_element.get_attribute("type") or "").strip().lower()
        if input_type in {"submit", "hidden", "button"}:
            raise RuntimeError(f"HENNGEログイン失敗: 入力不可の要素タイプです ({input_type})")

        current_value = input_element.get_attribute("value") or ""
        if current_value:
            input_element.click()
            input_element.send_keys(Keys.CONTROL, "a")
            input_element.send_keys(Keys.BACKSPACE)

        input_element.send_keys(secret_value)
        if send_tab:
            input_element.send_keys(Keys.TAB)

        result_value = input_element.get_attribute("value") or ""
        if not result_value:
            raise RuntimeError("HENNGEログイン失敗: 入力欄への値設定に失敗しました")

    def _log_input_target(self, label: str, element) -> None:
        tag_name = (element.tag_name or "").strip().lower()
        id_attr = (element.get_attribute("id") or "").strip()
        name_attr = (element.get_attribute("name") or "").strip()
        type_attr = (element.get_attribute("type") or "").strip().lower()
        enabled = element.is_enabled()
        has_disabled = element.get_attribute("disabled") is not None
        has_readonly = element.get_attribute("readonly") is not None

        self.logger.info(
            f"{label}入力対象 "
            f"tag={tag_name}, id={id_attr}, name={name_attr}, type={type_attr}, "
            f"enabled={enabled}, has_disabled={has_disabled}, has_readonly={has_readonly}"
        )

    def _fill_field(self, value: str, hints: list[str], field_type: str) -> bool:
        if not value:
            return False

        locators = []
        for hint in hints:
            locators.append((By.NAME, hint))
            locators.append((By.ID, hint))
            locators.append((By.CSS_SELECTOR, f"input[name*='{hint}' i]"))
            locators.append((By.CSS_SELECTOR, f"input[id*='{hint}' i]"))

        if field_type == "password":
            locators.extend([
                (By.CSS_SELECTOR, "input[type='password']"),
                (By.CSS_SELECTOR, "input[name*='pass' i]"),
            ])
        else:
            locators.extend([
                (By.CSS_SELECTOR, "input[type='email']"),
                (By.CSS_SELECTOR, "input[type='text']"),
                (By.CSS_SELECTOR, "input[name*='user' i]"),
                (By.CSS_SELECTOR, "input[name*='login' i]"),
            ])

        element = self.browser.find_first(locators, timeout=5)
        element.clear()
        element.send_keys(value)
        return True

    def _submit_login(self) -> bool:
        if self.browser.driver is None:
            raise RuntimeError("ブラウザが開始されていません")

        candidates = self.browser.driver.find_elements(
            By.CSS_SELECTOR,
            "input#login[name='userpass'][type='submit']",
        )
        visible_candidates = [element for element in candidates if element.is_displayed()]

        if len(visible_candidates) != 1:
            raise RuntimeError(
                f"HENNGEログインボタン判定失敗: 表示中候補数が不正です ({len(visible_candidates)})"
            )

        target = visible_candidates[0]
        tag_name = (target.tag_name or "").strip().lower()
        id_attr = (target.get_attribute("id") or "").strip()
        name_attr = (target.get_attribute("name") or "").strip()
        type_attr = (target.get_attribute("type") or "").strip().lower()
        value_attr = (target.get_attribute("value") or "").strip()

        if "証明書" in value_attr or name_attr.lower() == "cert":
            raise RuntimeError("HENNGEログインボタン判定失敗: 証明書ログイン要素はクリック禁止です")

        if (
            tag_name != "input"
            or id_attr != "login"
            or name_attr != "userpass"
            or value_attr != "ログイン"
            or type_attr != "submit"
        ):
            raise RuntimeError(
                "HENNGEログインボタン判定失敗: tag=input, id=login, name=userpass, type=submit, value=ログイン の完全一致が必要です"
            )

        self.logger.info(
            "HENNGEログイン対象 "
            f"tag={tag_name}, id={id_attr}, name={name_attr}, type={type_attr}, value={value_attr}"
        )
        target.click()
        return True

    def login_and_navigate(self) -> None:
        self.logger.info("HENNGEのメニュー画面へ遷移します")
        self.browser.wait_for_page_ready()
        self.browser.click_first([
            (By.XPATH, "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'certificate') or contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'dashboard')]"),
            (By.XPATH, "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'certificate') or contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'dashboard')]"),
            (By.XPATH, "//a[contains(normalize-space(.), '証明書') or contains(normalize-space(.), '管理')]"),
            (By.XPATH, "//button[contains(normalize-space(.), '証明書') or contains(normalize-space(.), '管理')]"),
        ], timeout=4)

    def search_user(self, alias: str) -> None:
        if not alias:
            raise ValueError("aliasが空です")

        self.logger.info(f"HENNGEでユーザー検索: {alias}")
        self.login_and_navigate()

        search_locators = [
            (By.CSS_SELECTOR, "input[type='search']"),
            (By.CSS_SELECTOR, "input[placeholder*='検索' i]"),
            (By.CSS_SELECTOR, "input[placeholder*='Search' i]"),
            (By.CSS_SELECTOR, "input[aria-label*='検索' i]"),
            (By.CSS_SELECTOR, "input[aria-label*='Search' i]"),
            (By.CSS_SELECTOR, "input[name*='search' i]"),
        ]
        search_input = self.browser.find_first(search_locators, timeout=5)
        search_input.clear()
        search_input.send_keys(alias)

        try:
            self.browser.click_first([
                (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'search')]"),
                (By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '検索')]"),
                (By.XPATH, "//input[@type='submit' and (contains(@value, 'Search') or contains(@value, '検索'))]"),
            ], timeout=4)
        except Exception:
            search_input.send_keys(Keys.ENTER)
        self.browser.wait_for_page_ready()

    def download_certificate(self, alias: str, imei: str) -> Path:
        self.logger.info(
            "証明書ダウンロード開始: alias_fingerprint=%s, imei_fingerprint=%s",
            self.value_fingerprint(alias),
            self.value_fingerprint(imei),
        )
        download_dir = self._downloads_dir()
        before_files = {p.resolve() for p in download_dir.glob("*") if p.is_file()}

        self.browser.wait_for_page_ready()
        try:
            self.browser.click_first([
                (By.XPATH, "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'download') or contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'export')]"),
                (By.XPATH, "//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'download') or contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'export')]"),
                (By.XPATH, "//button[contains(normalize-space(.), 'ダウンロード') or contains(normalize-space(.), '出力')]"),
                (By.XPATH, "//a[contains(normalize-space(.), 'ダウンロード') or contains(normalize-space(.), '出力')]"),
            ], timeout=5)
        except Exception as exc:
            self.logger.exception("証明書ダウンロードボタンを操作できませんでした")
            raise RuntimeError("HENNGE証明書ダウンロード失敗") from exc

        downloaded = self._wait_for_new_download(download_dir, before_files, timeout=45)
        self.logger.info(f"証明書ダウンロード完了: {downloaded}")
        return downloaded

    def login_for_certificate_workflow(self) -> dict[str, object]:
        self.login()
        return {"logged_in": True, "current_url": self._current_sanitized_url()}

    def search_certificate_by_alias_exact(self, alias: str) -> dict[str, object]:
        normalized_alias = unicodedata.normalize("NFKC", str(alias or "")).strip()
        if not normalized_alias or normalized_alias.upper() == "#N/A":
            raise ValueError("HENNGE検索用エイリアスが空、またはExcelエラー値です")

        self.submit_certificate_search_by_alias(normalized_alias)
        observation = self.wait_certificate_search_result()
        selector = observation["row_selector"]
        rows = observation["rows"]
        exact_rows = [row for row in rows if self._row_has_exact_alias(row, normalized_alias)]
        exact_count = len(exact_rows)
        observation["alias_exact_match_count"] = exact_count
        if exact_count != 1:
            raise RuntimeError("HENNGEエイリアス完全一致結果が一意ではありません")
        observation["rows"] = exact_rows
        observation["unique"] = True
        return observation

    def submit_certificate_search_by_alias(self, alias: str) -> dict[str, object]:
        normalized_alias = unicodedata.normalize("NFKC", str(alias or "")).strip()
        if not normalized_alias or normalized_alias.upper() == "#N/A":
            raise ValueError("HENNGE検索用エイリアスが空、またはExcelエラー値です")

        import diagnose_hennge_certificate_search as certificate_search

        driver = self.browser.driver
        if driver is None:
            raise RuntimeError("HENNGEブラウザー状態を確認できません")
        self.browser.open("https://admin.auth.hennge.com/certificates/")
        self.browser.wait_for_page_ready(timeout=20)
        search_input = certificate_search._wait_certificate_search_input_ready(driver, self.logger)
        certificate_search._set_query_and_submit(search_input, normalized_alias, self.logger)
        return {"search_key_type": "alias", "search_submitted": True}

    @staticmethod
    def _safe_numeric_element_value(element: object, name: str) -> int:
        try:
            value = getattr(element, "get_property")(name)
        except Exception:
            try:
                value = element.get_attribute(name)
            except Exception:
                value = 0
        try:
            return max(0, int(float(value or 0)))
        except (TypeError, ValueError):
            return 0

    def _find_certificate_result_scroll_container(self, rows: list[object]) -> tuple[object | None, dict[str, object]]:
        driver = self.browser.driver
        if driver is None:
            return None, {
                "candidate_count": 0,
                "unique": False,
                "scrollable": False,
                "scroll_height_greater_than_client_height": False,
            }

        candidates = []
        selectors = ["table", "[role='table']", "[role='grid']", "[role='rowgroup']"]
        for selector in selectors:
            try:
                candidates.extend(driver.find_elements(By.CSS_SELECTOR, selector))
            except Exception:
                continue
        for row in rows:
            try:
                candidates.extend(row.find_elements(By.XPATH, "./ancestor::*"))
            except Exception:
                continue

        unique_candidates = []
        seen_ids = set()
        for candidate in candidates:
            identity = getattr(candidate, "id", None) or id(candidate)
            if identity not in seen_ids:
                seen_ids.add(identity)
                unique_candidates.append(candidate)

        scrollable = []
        for candidate in unique_candidates:
            try:
                overflow_y = (candidate.value_of_css_property("overflow-y") or "").strip().lower()
            except Exception:
                overflow_y = ""
            scroll_height = self._safe_numeric_element_value(candidate, "scrollHeight")
            client_height = self._safe_numeric_element_value(candidate, "clientHeight")
            if overflow_y in {"auto", "scroll"} or scroll_height > client_height:
                scrollable.append(candidate)

        metrics = {
            "candidate_count": len(scrollable),
            "unique": len(scrollable) == 1,
            "scrollable": bool(scrollable),
            "scroll_height_greater_than_client_height": any(
                self._safe_numeric_element_value(candidate, "scrollHeight")
                > self._safe_numeric_element_value(candidate, "clientHeight")
                for candidate in scrollable
            ),
        }
        if len(scrollable) != 1:
            return None, metrics
        return scrollable[0], metrics

    def _log_certificate_result_scroll_metrics(self, metrics: dict[str, object]) -> None:
        self.logger.info(f"hennge_result_scroll_container_candidate_count={metrics['scroll_container_candidate_count']}")
        self.logger.info(f"hennge_result_scroll_container_unique={metrics['scroll_container_unique']}")
        self.logger.info(f"hennge_result_scroll_container_scrollable={metrics['scroll_container_scrollable']}")
        self.logger.info(f"hennge_result_scroll_height_greater_than_client_height={metrics['scroll_height_greater_than_client_height']}")
        self.logger.info(f"hennge_result_scroll_called={metrics['scroll_called']}")
        self.logger.info(f"hennge_result_scroll_step_count={metrics['scroll_step_count']}")
        self.logger.info(f"hennge_result_scroll_end_reached={metrics['scroll_end_reached']}")
        self.logger.info(f"hennge_result_rows_observed_total={metrics['rows_observed_total']}")
        self.logger.info(f"hennge_result_rows_deduplicated_total={metrics['rows_deduplicated_total']}")
        self.logger.info(f"hennge_result_header_candidate_count={metrics['header_candidate_count']}")
        self.logger.info(f"hennge_result_header_count={metrics['header_count']}")
        self.logger.info(f"hennge_result_row_cell_count_min={metrics['row_cell_count_min']}")
        self.logger.info(f"hennge_result_row_cell_count_max={metrics['row_cell_count_max']}")
        self.logger.info(f"hennge_result_header_cell_count_corresponding={metrics['header_cell_count_corresponding']}")
        self.logger.info(f"hennge_os_column_found={metrics['os_column_found']}")
        self.logger.info(f"hennge_os_column_index_resolved={metrics['os_column_index_resolved']}")
        self.logger.info(f"hennge_subject_memo_column_found={metrics['subject_memo_column_found']}")
        self.logger.info(f"hennge_subject_memo_column_index_resolved={metrics['subject_memo_column_index_resolved']}")
        self.logger.info(f"hennge_subject_memo_value_candidate_count={metrics['subject_memo_value_candidate_count']}")

    def _certificate_result_scroll_position(self, container: object) -> int:
        return self._safe_numeric_element_value(container, "scrollTop")

    def _set_certificate_result_scroll_position(self, container: object, position: int) -> int:
        driver = self.browser.driver
        if driver is None:
            return self._certificate_result_scroll_position(container)
        try:
            driver.execute_script("arguments[0].scrollTop = arguments[1];", container, int(position))
        except Exception:
            return self._certificate_result_scroll_position(container)
        return self._certificate_result_scroll_position(container)

    def _scan_certificate_rows_for_imei(self, target_imei: str) -> tuple[str, list[object], dict[str, object]]:
        selector, initial_rows = self._certificate_result_rows()
        container, container_metrics = self._find_certificate_result_scroll_container(initial_rows)
        expected_imei = self._normalize_certificate_cell(target_imei)
        observed_total = 0
        observed_keys = set()
        subject_memo_column_found = False
        os_column_found = False
        header_candidate_count = 0
        header_count = 0
        row_cell_counts = []
        subject_memo_column_index_resolved = False
        os_column_index_resolved = False
        subject_value_candidates = 0
        target_found_before_scroll = False
        target_found_after_scroll = False
        target_position = None
        observed_exact_match_count = 0
        observed_ios_match_count = 0
        observed_safe_match_count = 0
        scroll_called = False
        scroll_step_count = 0
        scroll_end_reached = False
        current_rows = initial_rows
        current_position = 0
        max_scroll_steps = 0

        while True:
            selector, current_rows = self._certificate_result_rows()
            observed_total += len(current_rows)
            for row in current_rows:
                headers, cells = self._result_row_headers_and_cells(row)
                header_candidate_count = max(header_candidate_count, len(headers))
                header_count = max(header_count, len(headers))
                row_cell_counts.append(len(cells))
                subject_index = self._find_result_column_index(headers, {"メール件名のメモ", "mail subject memo"})
                os_index = self._find_result_column_index(headers, {"os"})
                subject_memo_column_found = subject_memo_column_found or subject_index is not None
                os_column_found = os_column_found or os_index is not None
                subject_memo_column_index_resolved = subject_memo_column_index_resolved or subject_index is not None
                os_column_index_resolved = os_column_index_resolved or os_index is not None
                if subject_index is not None and self._result_cell_value(cells, subject_index):
                    subject_value_candidates += 1
                key = (
                    self._result_cell_value(cells, subject_index),
                    self._result_cell_value(cells, os_index),
                    tuple(self._result_cell_value(cells, index) for index in range(len(cells))),
                )
                observed_keys.add(hashlib.sha256(repr(key).encode("utf-8")).hexdigest())
                if subject_index is not None and self._result_cell_value(cells, subject_index) == expected_imei:
                    observed_exact_match_count += 1
                    is_ios = (
                        os_index is not None
                        and self._normalize_certificate_cell(self._result_cell_value(cells, os_index)).casefold() == "ios"
                    )
                    if is_ios:
                        observed_ios_match_count += 1
                    if is_ios and self._safe_result_row(row):
                        observed_safe_match_count += 1
                    if current_position == 0:
                        target_found_before_scroll = True
                    else:
                        target_found_after_scroll = True
                    if target_position is None:
                        target_position = current_position

            if not subject_memo_column_found or not os_column_found:
                scroll_end_reached = True
                break

            # A visible exact-match row decides the result before any scroll-container ambiguity.
            if observed_exact_match_count > 0:
                scroll_end_reached = True
                break

            if container is None:
                scroll_end_reached = True
                break

            scroll_height = self._safe_numeric_element_value(container, "scrollHeight")
            client_height = self._safe_numeric_element_value(container, "clientHeight")
            current_position = self._certificate_result_scroll_position(container)
            if scroll_height <= client_height or current_position >= max(0, scroll_height - client_height):
                scroll_end_reached = True
                break
            step = max(client_height, 1)
            max_scroll_steps = min(50, max(1, ceil(scroll_height / step) + 1))
            if scroll_step_count >= max_scroll_steps:
                scroll_end_reached = True
                break
            next_position = min(scroll_height - client_height, current_position + step)
            next_actual_position = self._set_certificate_result_scroll_position(container, next_position)
            scroll_called = True
            scroll_step_count += 1
            if next_actual_position == current_position:
                scroll_end_reached = True
                break
            current_position = next_actual_position

        if container is not None and target_position is not None and current_position != target_position:
            restored_position = self._set_certificate_result_scroll_position(container, target_position)
            scroll_called = True
            current_position = restored_position
            selector, current_rows = self._certificate_result_rows()

        metrics = {
            "scroll_container_candidate_count": container_metrics["candidate_count"],
            "scroll_container_unique": container_metrics["unique"],
            "scroll_container_scrollable": container_metrics["scrollable"],
            "scroll_height_greater_than_client_height": container_metrics["scroll_height_greater_than_client_height"],
            "scroll_called": scroll_called,
            "scroll_step_count": scroll_step_count,
            "scroll_end_reached": scroll_end_reached,
            "rows_observed_total": observed_total,
            "rows_deduplicated_total": len(observed_keys),
            "target_row_found_before_scroll": target_found_before_scroll,
            "target_row_found_after_scroll": target_found_after_scroll,
            "observed_exact_match_count": observed_exact_match_count,
            "observed_ios_match_count": observed_ios_match_count,
            "observed_safe_match_count": observed_safe_match_count,
            "max_scroll_steps": max_scroll_steps,
            "subject_memo_column_found": subject_memo_column_found,
            "os_column_found": os_column_found,
            "subject_memo_value_candidate_count": subject_value_candidates,
            "header_candidate_count": header_candidate_count,
            "header_count": header_count,
            "row_cell_count_min": min(row_cell_counts, default=0),
            "row_cell_count_max": max(row_cell_counts, default=0),
            "header_cell_count_corresponding": bool(row_cell_counts) and all(count == header_count for count in row_cell_counts),
            "os_column_found": os_column_found,
            "os_column_index_resolved": os_column_index_resolved,
            "subject_memo_column_found": subject_memo_column_found,
            "subject_memo_column_index_resolved": subject_memo_column_index_resolved,
        }
        self._log_certificate_result_scroll_metrics(metrics)
        self.logger.info(f"hennge_subject_memo_exact_match_count={observed_exact_match_count}")
        self.logger.info(f"hennge_imei_matched_row_candidate_count={observed_exact_match_count}")
        self.logger.info(f"hennge_imei_matched_row_os_ios={observed_exact_match_count == 1 and observed_ios_match_count == 1}")
        self.logger.info(
            "hennge_imei_matched_row_safe="
            f"{observed_exact_match_count == 1 and observed_safe_match_count == 1}"
        )
        self.last_search_observation.update({
            "subject_memo_exact_match_count": observed_exact_match_count,
            "imei_matched_row_candidate_count": observed_exact_match_count,
            "imei_matched_row_os_ios": observed_exact_match_count == 1 and observed_ios_match_count == 1,
            "imei_matched_row_safe": observed_exact_match_count == 1 and observed_safe_match_count == 1,
            **metrics,
        })
        return selector, current_rows, metrics

    def wait_certificate_search_result(self) -> dict[str, object]:
        import diagnose_hennge_certificate_search as certificate_search

        self.logger.info("hennge_search_wait_started=True")
        try:
            try:
                count = certificate_search._wait_results_ready(self.browser, timeout_seconds=10)
            except TypeError as exc:
                if "timeout_seconds" not in str(exc):
                    raise
                count = certificate_search._wait_results_ready(self.browser)
            selector, rows = self._certificate_result_rows()
            row_candidate_count = len(rows)
            self.logger.info("hennge_search_wait_completed=True")
            self.logger.info(f"hennge_search_result_count={count}")
            self.logger.info(f"hennge_search_result_multiple={count > 1}")
            self.logger.info(f"hennge_result_row_candidate_count={row_candidate_count}")
        except Exception as wait_error:
            explicit_zero = False
            explicit_error = False
            try:
                explicit_zero = certificate_search._is_no_data_visible(self.browser.driver)
                explicit_error = certificate_search._count_visible(self.browser.driver, "[role='alert'],[data-testid*='error'],.error") > 0
            except Exception:
                pass
            final_rows = []
            final_selector = ""
            try:
                final_selector, final_rows = self._certificate_result_rows()
            except Exception:
                final_rows = []
            if final_rows:
                count = len(final_rows)
                row_candidate_count = len(final_rows)
                self.logger.info("hennge_search_timeout_final_dom_rows=True")
                self.logger.info(f"hennge_search_result_count={count}")
                self.logger.info(f"hennge_result_row_candidate_count={row_candidate_count}")
                self.last_search_observation = {
                    "result_count": count,
                    "alias_exact_match_count": 0,
                    "row_selector": final_selector,
                    "rows": final_rows,
                    "row_candidate_count": row_candidate_count,
                    "unique": count == 1,
                    "search_result_multiple": count > 1,
                    "timeout_final_dom_observed": True,
                }
                return self.last_search_observation
            if explicit_zero or explicit_error:
                self.last_search_observation = {
                    "result_count": 0,
                    "alias_exact_match_count": 0,
                    "row_selector": final_selector,
                    "rows": [],
                    "row_candidate_count": 0,
                    "unique": False,
                    "search_result_multiple": False,
                    "explicit_zero_results": explicit_zero,
                    "explicit_search_error": explicit_error,
                }
                raise RuntimeError("HENNGE検索結果が0件、または検索エラーです") from wait_error
            self.logger.info("hennge_search_wait_completed=False")
            self.logger.info("hennge_search_result_count=0")
            self.logger.info("hennge_search_result_multiple=False")
            self.logger.info("hennge_result_row_candidate_count=0")
            raise wait_error

        self.last_search_observation = {
            "result_count": count,
            "alias_exact_match_count": 0,
            "row_selector": selector,
            "rows": rows,
            "row_candidate_count": row_candidate_count,
            "unique": count == 1 and row_candidate_count == 1,
            "search_result_multiple": count > 1,
        }
        return self.last_search_observation

    def _legacy_search_observation(self, normalized_alias: str) -> dict[str, object]:
        observation = self.wait_certificate_search_result()
        count = observation["result_count"]
        selector = observation["row_selector"]
        rows = observation["rows"]
        selector, rows = self._certificate_result_rows()
        if count != len(rows):
            count = len(rows)
        exact_rows = [row for row in rows if self._row_has_exact_alias(row, normalized_alias)]
        exact_count = len(exact_rows)
        self.logger.info(f"hennge_alias_search_result_count={count}")
        self.logger.info(f"hennge_alias_exact_match_count={exact_count}")
        self.logger.info(f"hennge_alias_expected_fingerprint={self.value_fingerprint(normalized_alias)}")
        candidate_fingerprints = sorted({
            self.value_fingerprint(value)
            for row in rows
            for value in self._row_alias_candidate_values(row)
            if value
        })
        self.logger.info(f"hennge_alias_candidate_fingerprint_count={len(candidate_fingerprints)}")
        self.logger.info(f"hennge_alias_candidate_fingerprints={','.join(candidate_fingerprints)}")
        self.last_search_observation = {
            "result_count": count,
            "alias_exact_match_count": exact_count,
            "row_selector": selector,
            "rows": rows,
            "unique": count == 1 and exact_count == 1,
        }
        if count == 0 or count > 1 or exact_count != 1:
            raise RuntimeError("HENNGEエイリアス完全一致結果が一意ではありません")
        return {
            "result_count": count,
            "alias_exact_match_count": exact_count,
            "row_selector": selector,
            "rows": exact_rows,
            "unique": True,
        }

    def search_certificate_by_alias(self, alias: str) -> dict[str, object]:
        return self.search_certificate_by_alias_exact(alias)

    def search_certificate_by_imei(self, imei: str) -> dict[str, object]:
        raise RuntimeError("HENNGE検索キーはaliasです。IMEI検索は実行できません")

    @staticmethod
    def _row_has_exact_alias(row: object, alias: str) -> bool:
        expected = unicodedata.normalize("NFKC", alias).strip().casefold()
        return expected in {
            unicodedata.normalize("NFKC", value).strip().casefold()
            for value in HenngeHandler._row_alias_candidate_values(row)
        }

    @staticmethod
    def _row_alias_candidate_values(row: object) -> list[str]:
        elements = row.find_elements(
            By.CSS_SELECTOR,
            "td,th,[role='cell'],[role='gridcell'],a,span",
        )
        values = []
        for element in elements:
            values.extend(
                [
                    getattr(element, "text", ""),
                    element.get_attribute("textContent"),
                    element.get_attribute("value"),
                    element.get_attribute("title"),
                    element.get_attribute("aria-label"),
                ]
            )
        if not values:
            values = (row.text or "").splitlines()
        return [str(value or "") for value in values]

    @staticmethod
    def value_fingerprint(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]

    def resolve_unique_certificate_result(self, observation: dict[str, object]) -> object:
        rows = observation.get("rows") if isinstance(observation, dict) else None
        if not isinstance(rows, list) or len(rows) != 1 or observation.get("result_count") != 1:
            count = len(rows) if isinstance(rows, list) else 0
            raise RuntimeError(f"HENNGE証明書検索結果が一意ではありません: {count}")
        return rows[0]

    def select_certificate_result(self, observation: dict[str, object], alias: str, target_imei: str | None = None) -> dict[str, object]:
        import diagnose_hennge_certificate_detail as certificate_detail

        selector, rows, scroll_metrics = self._scan_certificate_rows_for_imei(target_imei or "")
        if (
            scroll_metrics["observed_exact_match_count"] == 0
            and scroll_metrics["scroll_container_scrollable"]
            and not scroll_metrics["scroll_container_unique"]
        ):
            error = RuntimeError("HENNGE証明書一覧スクロールコンテナを一意に解決できません")
            error.failed_stage = "hennge_resolve_certificate_row_by_imei"
            raise error
        selector, rows = self._certificate_result_rows()
        click_refetch_row_count = len(rows)
        expected_imei = self._normalize_certificate_cell(target_imei)
        subject_memo_column_found = False
        os_column_found = False
        matched_rows = []
        ios_matches = []
        subject_value_candidates = 0
        for row in rows:
            headers, cells = self._result_row_headers_and_cells(row)
            subject_index = self._find_result_column_index(headers, {"メール件名のメモ", "mail subject memo"})
            os_index = self._find_result_column_index(headers, {"os"})
            subject_memo_column_found = subject_memo_column_found or subject_index is not None
            os_column_found = os_column_found or os_index is not None
            if subject_index is not None:
                subject_value_candidates += 1
            subject_value = self._result_cell_value(cells, subject_index)
            if subject_index is not None and subject_value == expected_imei:
                matched_rows.append(row)
                ios_matches.append(os_index is not None and self._normalize_certificate_cell(self._result_cell_value(cells, os_index)).casefold() == "ios")
        exact_count = len(matched_rows)
        click_refetch_exact_match_count = exact_count
        imei_os_ios = exact_count == 1 and ios_matches == [True]
        row_safe = exact_count == 1 and imei_os_ios and self._safe_result_row(matched_rows[0])
        self.logger.info(f"hennge_subject_memo_column_found={subject_memo_column_found}")
        subject_value_candidates = max(subject_value_candidates, int(scroll_metrics["subject_memo_value_candidate_count"]))
        subject_memo_column_found = bool(subject_memo_column_found or scroll_metrics["subject_memo_column_found"])
        os_column_found = bool(os_column_found or scroll_metrics["os_column_found"])
        self.logger.info(f"hennge_os_column_found={os_column_found}")
        self.logger.info(f"hennge_subject_memo_value_candidate_count={subject_value_candidates}")
        self.logger.info(f"hennge_subject_memo_exact_match_count={exact_count}")
        self.logger.info(f"hennge_imei_matched_row_candidate_count={exact_count}")
        self.logger.info(f"hennge_imei_matched_row_os_ios={imei_os_ios}")
        self.logger.info(f"hennge_imei_matched_row_safe={row_safe}")
        self.logger.info(f"hennge_click_refetch_row_count={click_refetch_row_count}")
        self.logger.info(f"hennge_click_refetch_exact_match_count={click_refetch_exact_match_count}")
        self.logger.info(f"hennge_click_refetch_safe_candidate_count={int(row_safe)}")
        self.logger.info(f"hennge_result_scroll_called={scroll_metrics['scroll_called']}")
        self.logger.info(f"hennge_result_scroll_step_count={scroll_metrics['scroll_step_count']}")
        self.logger.info(f"hennge_result_scroll_end_reached={scroll_metrics['scroll_end_reached']}")
        self.logger.info(f"hennge_result_rows_observed_total={scroll_metrics['rows_observed_total']}")
        self.logger.info(f"hennge_result_rows_deduplicated_total={scroll_metrics['rows_deduplicated_total']}")
        self.logger.info(f"hennge_target_row_found_before_scroll={scroll_metrics['target_row_found_before_scroll']}")
        self.logger.info(f"hennge_target_row_found_after_scroll={scroll_metrics['target_row_found_after_scroll']}")
        self.last_search_observation.update({
            "subject_memo_column_found": subject_memo_column_found,
            "os_column_found": os_column_found,
            "subject_memo_value_candidate_count": subject_value_candidates,
            "subject_memo_exact_match_count": exact_count,
            "imei_matched_row_candidate_count": exact_count,
            "imei_matched_row_os_ios": imei_os_ios,
            "imei_matched_row_safe": row_safe,
            "click_refetch_row_count": click_refetch_row_count,
            "click_refetch_exact_match_count": click_refetch_exact_match_count,
            "click_refetch_safe_candidate_count": int(row_safe),
            **scroll_metrics,
        })
        if not row_safe:
            error = RuntimeError("HENNGEメール件名メモによるIMEI完全一致行を安全に解決できません")
            error.failed_stage = "hennge_resolve_certificate_row_by_imei"
            raise error
        row = matched_rows[0]
        self.last_search_observation.update({
            "result_row_click_count": 1,
        })
        certificate_detail._assert_row_click_safety(row)
        before_path = urlsplit(self._current_sanitized_url()).path
        self.logger.info("hennge_select_stage_started=True")
        self.logger.info("hennge_result_row_candidate_count=1")
        self.logger.info("hennge_result_row_unique=True")
        self.logger.info(f"hennge_result_row_displayed={bool(row.is_displayed())}")
        self.logger.info(f"hennge_result_row_enabled={bool(row.is_enabled())}")
        row.click()
        self.logger.info("hennge_result_row_click_called=True")
        self.logger.info("hennge_result_row_click_count=1")
        detail = self.verify_certificate_detail_page(alias, before_path=before_path)
        return {
            "result_selector": selector,
            "result_row_candidate_count": len(rows),
            "result_row_unique": True,
            "result_row_displayed": bool(row.is_displayed()),
            "result_row_enabled": bool(row.is_enabled()),
            "result_row_safe": True,
            "result_row_click_called": True,
            "result_row_click_count": 1,
            "subject_memo_column_found": subject_memo_column_found,
            "header_candidate_count": scroll_metrics["header_candidate_count"],
            "header_count": scroll_metrics["header_count"],
            "row_cell_count_min": scroll_metrics["row_cell_count_min"],
            "row_cell_count_max": scroll_metrics["row_cell_count_max"],
            "os_column_index_resolved": scroll_metrics["os_column_index_resolved"],
            "subject_memo_column_index_resolved": scroll_metrics["subject_memo_column_index_resolved"],
            "os_column_found": os_column_found,
            "subject_memo_value_candidate_count": subject_value_candidates,
            "subject_memo_exact_match_count": exact_count,
            "imei_matched_row_candidate_count": exact_count,
            "imei_matched_row_os_ios": imei_os_ios,
            "imei_matched_row_safe": row_safe,
            "scroll_container_candidate_count": scroll_metrics["scroll_container_candidate_count"],
            "scroll_container_unique": scroll_metrics["scroll_container_unique"],
            "scroll_container_scrollable": scroll_metrics["scroll_container_scrollable"],
            "scroll_height_greater_than_client_height": scroll_metrics["scroll_height_greater_than_client_height"],
            "scroll_called": scroll_metrics["scroll_called"],
            "scroll_step_count": scroll_metrics["scroll_step_count"],
            "scroll_end_reached": scroll_metrics["scroll_end_reached"],
            "rows_observed_total": scroll_metrics["rows_observed_total"],
            "rows_deduplicated_total": scroll_metrics["rows_deduplicated_total"],
            "target_row_found_before_scroll": scroll_metrics["target_row_found_before_scroll"],
            "target_row_found_after_scroll": scroll_metrics["target_row_found_after_scroll"],
            "max_scroll_steps": scroll_metrics["max_scroll_steps"],
            "click_refetch_row_count": click_refetch_row_count,
            "click_refetch_exact_match_count": click_refetch_exact_match_count,
            "click_refetch_safe_candidate_count": int(row_safe),
            "detail": detail,
        }

    @staticmethod
    def _normalize_certificate_cell(value: object) -> str:
        return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()

    @classmethod
    def _find_result_column_index(cls, headers: list[str], names: set[str]) -> int | None:
        expected = {cls._normalize_certificate_cell(name).casefold() for name in names}
        for index, header in enumerate(headers):
            if cls._normalize_certificate_cell(header).casefold() in expected:
                return index
        return None

    @staticmethod
    def _result_cell_value(cells: list[object], index: int | None) -> str:
        if index is None or index >= len(cells):
            return ""
        cell = cells[index]
        try:
            return HenngeHandler._normalize_certificate_cell(getattr(cell, "text", "") or cell.get_attribute("textContent"))
        except Exception:
            return ""

    @staticmethod
    def _result_row_headers_and_cells(row: object) -> tuple[list[str], list[object]]:
        try:
            cells = row.find_elements(By.CSS_SELECTOR, "td,th,[role='cell'],[role='gridcell']")
        except Exception:
            cells = []
        headers = []
        for selector in (
            "./ancestor::table[1]//thead//th",
            "./ancestor::*[@role='grid' or @role='table'][1]//*[@role='columnheader']",
            "./ancestor::*[@role='rowgroup'][1]//*[@role='columnheader']",
        ):
            try:
                headers = [HenngeHandler._normalize_certificate_cell(getattr(item, "text", "") or item.get_attribute("textContent")) for item in row.find_elements(By.XPATH, selector)]
            except Exception:
                headers = []
            if headers:
                break
        return headers, cells

    @staticmethod
    def _safe_result_row(row: object) -> bool:
        try:
            return bool(row.is_displayed()) and bool(row.is_enabled())
        except Exception:
            return False

    def verify_certificate_detail_page(self, alias: str, *, before_path: str | None = None) -> dict[str, object]:
        import diagnose_hennge_certificate_detail as certificate_detail

        driver = self.browser.driver
        if driver is None:
            raise RuntimeError("HENNGE証明書詳細画面を確認できません")
        prior_path = before_path if before_path is not None else urlsplit(self._current_sanitized_url()).path
        self.logger.info("hennge_certificate_detail_wait_started=True")
        try:
            detail = certificate_detail._wait_detail_ready(driver, prior_path, self.logger, timeout_seconds=10)
        except Exception:
            self.logger.info("hennge_certificate_detail_page_verified=False")
            self.logger.info("hennge_detail_alias_available=False")
            self.logger.info("hennge_detail_alias_exact_match=False")
            self.logger.info("hennge_download_action_candidate_count=0")
            self.logger.info("hennge_password_source_candidate_count=0")
            raise
        detail_area = detail.get("detail_area")
        if detail_area is None:
            raise RuntimeError("HENNGE証明書詳細コンテナを確認できません")

        field_observation = certificate_detail._extract_detail_field_observation(detail_area)
        password_observation = certificate_detail._extract_password_structure(detail_area)
        detail_actions = certificate_detail._extract_action_elements(detail_area)
        download_candidates = [
            item for item in detail_actions
            if item.get("label_category") == certificate_detail.LABEL_CATEGORY_DOWNLOAD
            and str(item.get("type", "")).lower() != "submit"
            and (item.get("tag") in {"a", "button", "input"} or item.get("role") in {"button", "link"})
            and bool(item["element"].is_displayed())
            and bool(item["element"].is_enabled())
            and item["element"].get_attribute("disabled") is None
        ]
        driver_dialogs = [item for item in driver.find_elements(By.CSS_SELECTOR, "[role='dialog']") if item.is_displayed()]
        dialog_count = len(driver_dialogs) or 1
        detail_dialog_unique = dialog_count == 1
        expected = unicodedata.normalize("NFKC", alias).strip().casefold()
        normalized_alias_values = {
            unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
            for value in field_observation["alias_values"] if value
        }
        alias_exact = expected in normalized_alias_values
        alias_field_available = bool(field_observation["alias_label_found"])
        subject_memo_context = (
            self.last_search_observation.get("imei_matched_row_candidate_count") == 1
            and self.last_search_observation.get("imei_matched_row_os_ios") is True
            and self.last_search_observation.get("result_row_click_count") == 1
            and detail_dialog_unique
        )
        unique_context = (
            not alias_field_available
            and self.last_search_observation.get("result_count") == 1
            and self.last_search_observation.get("row_candidate_count") == 1
            and detail_dialog_unique
            and not subject_memo_context
        )
        identity_method = "detail_alias_exact_match" if alias_exact else ("subject_memo_imei_exact_match" if subject_memo_context else ("unique_search_result_context" if unique_context else "unresolved"))
        identity_verified = (alias_exact or subject_memo_context or unique_context) and detail_dialog_unique
        action_safe = len(download_candidates) == 1
        detail_metrics = {
            "hennge_detail_dialog_count": dialog_count,
            "hennge_detail_dialog_unique": detail_dialog_unique,
            "hennge_detail_container_found": True,
            "hennge_detail_field_row_count": field_observation["field_row_count"],
            "hennge_detail_label_count": field_observation["label_count"],
            "hennge_detail_value_count": field_observation["value_count"],
            "hennge_detail_alias_label_found": field_observation["alias_label_found"],
            "hennge_detail_alias_value_found": field_observation["alias_value_found"],
            "hennge_detail_alias_value_nonblank": field_observation["alias_value_nonblank"],
            "hennge_detail_alias_field_available": alias_field_available,
            "hennge_detail_identity_verified_by_unique_search_context": unique_context,
            "hennge_detail_identity_verification_method": identity_method,
            "hennge_download_action_candidate_count": len(download_candidates),
            "hennge_download_action_unique": len(download_candidates) == 1,
            "hennge_download_action_displayed": len(download_candidates) == 1 and bool(download_candidates[0]["element"].is_displayed()),
            "hennge_download_action_enabled": len(download_candidates) == 1 and bool(download_candidates[0]["element"].is_enabled()),
            "hennge_download_action_safe": action_safe,
            **password_observation,
        }
        for key, value in detail_metrics.items():
            if key.endswith(("_count", "_found", "_nonblank", "_available", "_unique", "_displayed", "_enabled", "_safe", "_action", "_reveal_action", "_copy_action")) or key.endswith("_method"):
                self.logger.info(f"{key}={value}")
        if not alias_field_available or not field_observation["alias_value_found"] or password_observation["password_source_candidate_count"] == 0:
            self._save_detail_dom_diagnostic(detail_area, detail_metrics)
        if not identity_verified:
            self.logger.info("hennge_certificate_detail_page_verified=False")
            raise RuntimeError("HENNGE詳細画面の証明書本人確認を完了できません")
        if not action_safe:
            self.logger.info("hennge_certificate_detail_page_verified=False")
            raise RuntimeError("HENNGE詳細画面のダウンロード操作候補が一意ではありません")
        self.logger.info("hennge_certificate_detail_page_verified=True")
        return {"detail_page_verified": True, "detail_method": detail.get("detail_method", ""), "alias_exact_verified": alias_exact, "detail_alias_available": alias_field_available, "identity_verified": identity_verified, "identity_method": identity_method, "detail_area_present": True, "download_action_candidate_count": len(download_candidates), "download_action_safe": action_safe, **detail_metrics}

    def _save_detail_dom_diagnostic(self, detail_area: object, metrics: dict[str, object]) -> None:
        elements = []
        try:
            candidates = detail_area.find_elements(By.CSS_SELECTOR, "dt,dd,th,td,label,input,textarea,button,a,[role='button'],[role='link']")
        except Exception:
            candidates = []
        for index, element in enumerate(candidates, start=1):
            elements.append({
                "index": index,
                "tag": (getattr(element, "tag_name", "") or "").lower(),
                "type": (element.get_attribute("type") or "").lower(),
                "role": element.get_attribute("role") or "",
                "readonly": element.get_attribute("readonly") is not None,
                "disabled": element.get_attribute("disabled") is not None,
                "displayed": bool(element.is_displayed()),
                "enabled": bool(element.is_enabled()),
                "label_related": bool(element.get_attribute("for") or element.get_attribute("aria-labelledby")),
                "parent_tag": "",
                "dialog_index": 0,
                "field_row_index": index,
                "data_testid_present": bool(element.get_attribute("data-testid")),
                "aria_label_present": bool(element.get_attribute("aria-label")),
                "title_present": bool(element.get_attribute("title")),
                "copy_candidate": any(token in " ".join((element.get_attribute("aria-label") or "", element.get_attribute("data-testid") or "")).casefold() for token in ("copy", "コピー")),
                "reveal_candidate": any(token in " ".join((element.get_attribute("aria-label") or "", element.get_attribute("data-testid") or "")).casefold() for token in ("show", "reveal", "表示")),
                "candidate_reason": "detail_scoped_element",
                "excluded_reason": "",
            })
        payload = {key: value for key, value in metrics.items() if key != "password_values"}
        payload["elements"] = elements
        logs_dir = self.logger.base_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        path = logs_dir / f"hennge_certificate_detail_dom_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")

    def _certificate_result_rows(self) -> tuple[str, list[object]]:
        if self.browser.driver is None:
            raise RuntimeError("HENNGE検索結果を確認できません")
        import diagnose_hennge_certificate_result as certificate_result
        return certificate_result._collect_visible_rows(self.browser.driver)

    def inspect_certificate_download_action(self, result_row: object | None = None) -> dict[str, object]:
        import diagnose_hennge_certificate_detail as certificate_detail
        driver = self.browser.driver
        if driver is None:
            raise RuntimeError("HENNGE詳細画面を確認できません")
        row = result_row
        if row is None:
            _, rows = self._certificate_result_rows()
            if len(rows) != 1:
                raise RuntimeError(f"HENNGE結果行が一意ではありません: {len(rows)}")
            row = rows[0]
        certificate_detail._assert_row_click_safety(row)
        before_path = urlsplit(self._current_sanitized_url()).path
        row.click()
        detail_state = certificate_detail._wait_detail_ready(driver, before_path, self.logger)
        actions = certificate_detail._extract_action_elements(detail_state["detail_area"])
        candidates = [item for item in actions if item.get("label_category") == certificate_detail.LABEL_CATEGORY_DOWNLOAD and str(item.get("type", "")).lower() != "submit"]
        if len(candidates) != 1:
            raise RuntimeError(f"HENNGE download候補が一意ではありません: {len(candidates)}")
        return {"detail_state": detail_state, "download_candidates": candidates, "unique": True}

    def inspect_certificate_download_action_from_detail(self) -> dict[str, object]:
        import diagnose_hennge_certificate_detail as certificate_detail

        driver = self.browser.driver
        if driver is None:
            raise RuntimeError("HENNGE詳細画面を確認できません")
        detail_area, detail_method = certificate_detail._identify_detail_area(driver)
        actions = certificate_detail._extract_action_elements(detail_area)
        candidates = [item for item in actions if item.get("label_category") == certificate_detail.LABEL_CATEGORY_DOWNLOAD and str(item.get("type", "")).lower() != "submit"]
        if len(candidates) != 1:
            raise RuntimeError(f"HENNGE download候補が一意ではありません: {len(candidates)}")
        return {"detail_method": detail_method, "download_candidates": candidates, "unique": True}

    def wait_for_download_completion(self, before_names: set[str], download_dir: Path | None = None) -> Path:
        import diagnose_hennge_certificate_download as certificate_download
        directory = download_dir or self._downloads_dir()
        return certificate_download._wait_for_single_download_file(directory, before_names, self.logger)

    def validate_downloaded_certificate(self, certificate_path: Path) -> Path:
        path = Path(certificate_path)
        if not path.is_file() or path.suffix.casefold() not in {".p12", ".pfx"} or path.stat().st_size <= 0:
            raise RuntimeError("ダウンロード証明書ファイルを検証できません")
        with path.open("rb") as stream:
            if not stream.read(1):
                raise RuntimeError("ダウンロード証明書ファイルが空です")
        return path

    def plan_rename_certificate_to_imei(self, certificate_path: Path, imei: str) -> Path:
        if not imei or not imei.isdigit() or len(imei) != 15:
            raise ValueError("IMEIは15桁の数字で指定してください")
        source = self.validate_downloaded_certificate(certificate_path)
        return source.with_name(f"{imei}{source.suffix.lower()}")

    def rename_certificate_to_imei(self, certificate_path: Path, imei: str) -> Path:
        source = self.validate_downloaded_certificate(certificate_path)
        target = self.plan_rename_certificate_to_imei(source, imei)
        if source.resolve() == target.resolve():
            return source
        if target.exists():
            raise FileExistsError("IMEI名の証明書ファイルが既に存在します")
        shutil.move(str(source), str(target))
        return target

    def inspect_certificate_password_source(self) -> dict[str, object]:
        try:
            import diagnose_hennge_certificate_detail as certificate_detail

            driver = self.browser.driver
            if driver is not None:
                detail_area, _detail_method = certificate_detail._identify_detail_area(driver)
                return certificate_detail._extract_password_structure(detail_area)
        except Exception:
            pass
        certificate_config = self.config.get("hennge", {}).get("certificate", {}) or {}
        password = certificate_config.get("password") or self.config.get("certificate_password")
        return {
            "configured": isinstance(password, str) and bool(password),
            "source": "config" if password else "none",
            "password_source_candidate_count": 1 if isinstance(password, str) and bool(password) else 0,
        }

    def read_certificate_password(self) -> str:
        self.last_password_observation = {
            "password_section_found": False,
            "password_section_scrolled_into_view": False,
            "reveal_button_click_called": False,
            "reveal_button_click_count": 0,
            "password_dom_reobserved": False,
            "password_eye_button_click_called": False,
            "password_copy_click_called": False,
            "password_copy_click_count": 0,
            "clipboard_read_called": False,
            "clipboard_clear_called": False,
            "clipboard_clear_completed": False,
        }
        import diagnose_hennge_certificate_detail as certificate_detail

        driver = self.browser.driver
        if driver is None:
            self.last_password_observation["failed_stage"] = "hennge_password_section_resolved"
            raise RuntimeError("hennge_password_section_resolved")

        detail_area, _detail_method = certificate_detail._identify_detail_area(driver)
        section = certificate_detail.find_password_section(detail_area)
        self.last_password_observation["password_section_found"] = section is not None
        if section is None:
            self.last_password_observation["failed_stage"] = "hennge_password_section_resolved"
            raise RuntimeError("hennge_password_section_resolved")
        try:
            certificate_detail.password_section_scroll_target(section).location_once_scrolled_into_view
        except Exception as exc:
            self.last_password_observation["failed_stage"] = "hennge_password_section_resolved"
            raise RuntimeError("hennge_password_section_resolved") from exc
        self.last_password_observation["password_section_scrolled_into_view"] = True

        detail_area, _detail_method = certificate_detail._identify_detail_area(driver)
        section = certificate_detail.find_password_section(detail_area)
        if section is None:
            self.last_password_observation["failed_stage"] = "hennge_password_section_resolved"
            raise RuntimeError("hennge_password_section_resolved")
        self.last_password_observation["hennge_password_reveal_candidate_resolved"] = True
        if hasattr(detail_area, "find_elements"):
            resolved_reveal = certificate_detail.resolve_password_reveal_in_section(detail_area)
        else:
            resolved_reveal = (section, [])
        if resolved_reveal is None:
            self.last_password_observation["failed_stage"] = "hennge_password_reveal_safety_verified"
            raise RuntimeError("hennge_password_reveal_safety_verified")
        section, reveal_elements = resolved_reveal
        reveal = certificate_detail.inspect_password_reveal_candidates(section)
        if reveal["candidate_count"] == 0:
            reveal = certificate_detail.inspect_password_reveal_candidates(detail_area)
            reveal["candidates"] = reveal_elements
        self.last_password_observation.update({
            "password_reveal_candidate_count": reveal["candidate_count"],
            "password_reveal_unique": reveal["unique"],
            "password_reveal_displayed": reveal["displayed"],
            "password_reveal_enabled": reveal["enabled"],
            "password_reveal_disabled": reveal["disabled"],
            "password_reveal_inside_detail_dialog": reveal["inside_detail_dialog"],
            "password_reveal_safe": reveal["safe"],
            "reveal_button_candidate_count": reveal["candidate_count"],
            "reveal_button_unique": reveal["unique"],
            "reveal_button_displayed": reveal["displayed"],
            "reveal_button_enabled": reveal["enabled"],
        })
        if not reveal["safe"]:
            self.last_password_observation["failed_stage"] = "hennge_password_reveal_safety_verified"
            raise RuntimeError("hennge_password_reveal_safety_verified")

        self.last_password_observation["hennge_password_reveal_safety_verified"] = True
        self.last_password_observation["failed_stage"] = "hennge_password_reveal_clicked"
        self.last_password_observation["hennge_password_reveal_click_started"] = True
        self.last_password_observation["password_reveal_click_started"] = True
        try:
            reveal["candidates"][0].location_once_scrolled_into_view
            reveal["candidates"][0].click()
        except StaleElementReferenceException as exc:
            self.last_password_observation["failed_stage"] = "hennge_password_reveal_click"
            self.last_password_observation["password_reveal_click_exception_type"] = type(exc).__name__
            raise RuntimeError("hennge_password_reveal_click") from exc
        except Exception as exc:
            self.last_password_observation["failed_stage"] = "hennge_password_reveal_click"
            self.last_password_observation["password_reveal_click_exception_type"] = type(exc).__name__
            raise RuntimeError("hennge_password_reveal_click") from exc
        self.last_password_observation.update({
            "hennge_password_reveal_click_completed": True,
            "password_reveal_click_called": True,
            "password_reveal_click_count": 1,
            "password_reveal_click_completed": True,
            "hennge_password_dom_reobserve_started": True,
            "password_dom_reobserve_started": True,
        })
        self.last_password_observation["failed_stage"] = "hennge_password_copy_button_resolved"
        deadline = time.monotonic() + 10
        while time.monotonic() <= deadline:
            refreshed_area, _refreshed_method = certificate_detail._identify_detail_area(driver)
            refreshed_section = certificate_detail.find_password_section(refreshed_area)
            if refreshed_section is None:
                time.sleep(0.1)
                continue
            revealed = certificate_detail.inspect_password_copy_candidates(refreshed_section)
            if not revealed["safe"] and hasattr(refreshed_area, "find_elements"):
                dialog_revealed = certificate_detail.inspect_password_copy_candidates(refreshed_area)
                if dialog_revealed["candidate_count"] == 1:
                    revealed = dialog_revealed
            self.last_password_observation["password_dom_reobserved"] = True
            self.last_password_observation["hennge_password_dom_reobserve_completed"] = True
            self.last_password_observation.update({
                "masked_password_field_count": revealed["masked_password_field_count"],
                "password_eye_button_candidate_count": revealed["password_eye_button_candidate_count"],
                "password_copy_button_candidate_count": revealed["candidate_count"],
                "password_copy_button_unique": revealed["unique"],
                "password_copy_button_displayed": revealed["displayed"],
                "password_copy_button_enabled": revealed["enabled"],
                "password_copy_button_safe": revealed["safe"],
            })
            if not revealed["safe"]:
                time.sleep(0.1)
                continue
            self.last_password_observation["password_copy_click_started"] = True
            try:
                revealed["candidates"][0].click()
            except Exception as exc:
                self.last_password_observation["failed_stage"] = "hennge_password_copy_clicked"
                raise RuntimeError("hennge_password_copy_clicked") from exc
            self.last_password_observation.update({
                "password_copy_click_called": True,
                "password_copy_click_count": 1,
                "password_copy_click_completed": True,
            })
            self.last_password_observation["failed_stage"] = "hennge_password_clipboard_read"
            self.last_password_observation["clipboard_read_called"] = True
            try:
                time.sleep(1.0)
                password = _read_windows_clipboard_once()
            except Exception as exc:
                try:
                    _clear_windows_clipboard()
                    self.last_password_observation["clipboard_clear_called"] = True
                    self.last_password_observation["clipboard_clear_completed"] = True
                except Exception:
                    pass
                raise RuntimeError("hennge_password_clipboard_read") from exc
            self.last_password_observation["certificate_password_obtained"] = _valid_clipboard_password(password)
            self.last_password_observation["certificate_password_nonblank"] = bool(isinstance(password, str) and password.strip())
            if not self.last_password_observation["certificate_password_obtained"]:
                try:
                    _clear_windows_clipboard()
                    self.last_password_observation["clipboard_clear_called"] = True
                    self.last_password_observation["clipboard_clear_completed"] = True
                finally:
                    self.last_password_observation["failed_stage"] = "hennge_password_clipboard_read"
                raise RuntimeError("hennge_password_clipboard_read")
            self.last_password_observation["password_source_type"] = "clipboard_copy_control"
            self.last_password_observation["failed_stage"] = "hennge_password_clipboard_cleared"
            try:
                _clear_windows_clipboard()
            except Exception as exc:
                raise RuntimeError("hennge_password_clipboard_cleared") from exc
            self.last_password_observation["clipboard_clear_called"] = True
            self.last_password_observation["clipboard_clear_completed"] = True
            self.last_password_observation["hennge_password_value_resolved"] = True
            return password.strip()
        raise RuntimeError("hennge_password_copy_button_resolved")

    @staticmethod
    def _password_source_type(source: dict[str, object]) -> str:
        if source.get("readonly_value_candidate_count") == 1:
            return "readonly_input"
        if source.get("text_input_candidate_count") == 1:
            return "text_input"
        if source.get("password_input_candidate_count") == 1:
            return "password_input"
        if source.get("password_value_container_found") is True:
            return "value_container"
        return "unknown"

    def _downloads_dir(self) -> Path:
        downloads_setting = self.config.get("paths", {}).get("downloads", "downloads")
        path = self.logger.base_dir / downloads_setting
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _wait_for_new_download(self, download_dir: Path, before_files: set[Path], timeout: int) -> Path:
        end_time = time.time() + timeout
        while time.time() < end_time:
            candidates = []
            for path in download_dir.glob("*"):
                if not path.is_file():
                    continue
                name = path.name.lower()
                suffix = path.suffix.lower()
                if suffix not in {".pfx", ".p12"}:
                    continue
                if name.endswith(".crdownload") or name.endswith(".tmp"):
                    continue
                resolved = path.resolve()
                if resolved in before_files:
                    continue
                candidates.append(path)

            if len(candidates) > 1:
                raise RuntimeError("証明書ダウンロード失敗: 新規ファイルが複数検出されました")

            if len(candidates) == 1:
                target = candidates[0]
                size1 = target.stat().st_size
                time.sleep(1)
                size2 = target.stat().st_size
                if size1 > 0 and size1 == size2:
                    return target

            time.sleep(1)

        raise TimeoutError("証明書のダウンロードがタイムアウトしました")

    def _detect_domain_error(self) -> bool:
        if self.browser.driver is None:
            return False

        error_locators = [
            (By.CSS_SELECTOR, ".alert-danger"),
            (By.CSS_SELECTOR, ".error"),
            (By.CSS_SELECTOR, "[role='alert']"),
            (By.XPATH, "//*[contains(normalize-space(.), '不明なドメイン')]"),
            (By.XPATH, "//*[contains(normalize-space(.), '管理用ユーザーインタフェースが設定されていません')]"),
            (By.XPATH, "//*[contains(normalize-space(.), 'ドメインを入力してください')]"),
            (By.XPATH, "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'unknown domain')]"),
            (By.XPATH, "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'admin user interface is not configured')]"),
        ]
        for by, selector in error_locators:
            try:
                for element in self.browser.driver.find_elements(by, selector):
                    if element.is_displayed() and (element.text or "").strip():
                        return True
            except Exception:
                continue

        page_text = (self.browser.driver.page_source or "").lower()
        error_markers = [
            "不明なドメイン",
            "管理用ユーザーインタフェースが設定されていません",
            "ドメインを入力してください",
            "unknown domain",
            "admin user interface is not configured",
        ]
        return any(marker.lower() in page_text for marker in error_markers)

    def _is_login_success(self) -> bool:
        if self.browser.driver is None:
            return False

        current_url = (self.browser.driver.current_url or "").lower()
        if "admin.auth.hennge.com" not in current_url:
            return False
        if self._detect_domain_error():
            return False

        success_locators = [
            (By.CSS_SELECTOR, "input[type='search']"),
            (By.CSS_SELECTOR, "a[href*='certificate' i]"),
            (By.CSS_SELECTOR, "a[href*='users' i]"),
            (By.XPATH, "//h1[contains(normalize-space(.), '証明書') or contains(normalize-space(.), 'Certificate')]")
        ]
        try:
            self.browser.find_first(success_locators, timeout=4)
            return True
        except Exception:
            return False

    def _wait_for_login_state(self, timeout_sec: int = 180) -> str:
        start = time.monotonic()
        while (time.monotonic() - start) < timeout_sec:
            if self._detect_domain_error():
                return "domain_error"
            if self._is_login_success():
                return "success"

            try:
                self.browser.wait_for_page_ready(timeout=3)
            except Exception:
                pass
            time.sleep(2)
        return "timeout"

    def _wait_for_credential_form(self, timeout_sec: int = 90) -> str:
        start = time.monotonic()
        grace_seconds = 3
        stable_error_url = ""
        stable_error_count = 0
        last_url = ""
        last_title = ""

        while (time.monotonic() - start) < timeout_sec:
            current_url = self._current_sanitized_url()
            current_title = self._current_title()
            if current_url != last_url or current_title != last_title:
                self.logger.info(f"HENNGE待機状態 URL={current_url}, TITLE={current_title}")
                last_url, last_title = current_url, current_title

            # a. credential form has top priority
            if self._has_credential_form():
                return "credential_form"

            # b. keep waiting while redirect is in progress
            if self._is_redirecting_state(current_url):
                time.sleep(1)
                continue

            # c. domain error only after grace + stable consecutive detections
            elapsed = time.monotonic() - start
            if elapsed >= grace_seconds and self._detect_domain_error():
                if stable_error_url == current_url:
                    stable_error_count += 1
                else:
                    stable_error_url = current_url
                    stable_error_count = 1

                if stable_error_count >= 3:
                    return "domain_error"
            else:
                stable_error_url = ""
                stable_error_count = 0

            time.sleep(1)

        return "timeout"

    def _has_credential_form(self) -> bool:
        if self.browser.driver is None:
            return False

        user_selectors = [
            "input[type='email']",
            "input[type='text']",
            "input[name*='user' i]",
            "input[name*='login' i]",
        ]
        password_selectors = [
            "input[type='password']",
            "input[name*='pass' i]",
        ]

        user_found = any(
            element.is_displayed()
            for selector in user_selectors
            for element in self.browser.driver.find_elements(By.CSS_SELECTOR, selector)
        )
        if not user_found:
            return False

        pass_found = any(
            element.is_displayed()
            for selector in password_selectors
            for element in self.browser.driver.find_elements(By.CSS_SELECTOR, selector)
        )
        return pass_found

    def _is_redirecting_state(self, current_url: str) -> bool:
        if self.browser.driver is None:
            return True

        try:
            ready_state = self.browser.driver.execute_script("return document.readyState")
            if ready_state != "complete":
                return True
        except Exception:
            return True

        host = urlsplit(current_url).netloc.lower()
        if not host:
            return True
        return host != "ap.ssso.hdems.com"

    def _current_sanitized_url(self) -> str:
        if self.browser.driver is None:
            return ""
        raw_url = self.browser.driver.current_url or ""
        parsed = urlsplit(raw_url)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

    def _current_title(self) -> str:
        if self.browser.driver is None:
            return ""
        return self.browser.driver.title or ""
