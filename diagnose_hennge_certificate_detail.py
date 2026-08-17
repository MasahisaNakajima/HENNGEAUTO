from __future__ import annotations

import sys
import time
import json
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By

import diagnose_hennge_certificate_search as cert_search
from app.browser import Browser
from app.config import ensure_directories, load_config
from app.hennge_handler import HenngeHandler
from app.logger import AppLogger

CERTIFICATES_URL = "https://admin.auth.hennge.com/certificates/"
SEARCH_INPUT_TIMEOUT_SECONDS = 20
RESULT_TIMEOUT_SECONDS = 15
DETAIL_WAIT_TIMEOUT_SECONDS = 20

DOWNLOAD_KEYWORDS = ["download", "ダウンロード", "export"]
UPDATE_KEYWORDS = ["update", "更新", "delete", "削除", "revoke", "失効", "create", "一覧作成", "register", "登録"]
DETAIL_HEADING_KEYWORDS = ["証明書", "certificate", "detail", "詳細"]
LIST_HEADINGS_EXCLUDED = {"証明書一覧", "デバイス証明書", "certificate list", "device certificates"}
DETAIL_DRAWER_SELECTORS = [
    "[role='dialog']",
    "[class*='drawer']",
    "[data-testid*='drawer']",
    "[aria-modal='true']",
]
DETAIL_METHOD_CLOSE_COMMON = "close_button_common_ancestor"
DETAIL_METHOD_DIALOG = "dialog"
DETAIL_METHOD_DRAWER = "drawer"

LABEL_CATEGORY_DOWNLOAD = "download"
LABEL_CATEGORY_CLOSE = "close"
LABEL_CATEGORY_SAVE = "save"
LABEL_CATEGORY_CANCEL = "cancel"
LABEL_CATEGORY_REVOKE = "revoke"
LABEL_CATEGORY_INSTALLATION_EMAIL = "installation_email"
LABEL_CATEGORY_UNKNOWN = "unknown"

DOWNLOAD_LABEL_TOKENS = {"ダウンロード", "download", "証明書をダウンロード", "取得"}
CLOSE_LABEL_TOKENS = {"閉じる", "close"}
SAVE_LABEL_TOKENS = {"保存", "save"}
CANCEL_LABEL_TOKENS = {"キャンセル", "cancel"}
REVOKE_LABEL_TOKENS = {"失効", "revoke"}
INSTALLATION_EMAIL_LABEL_TOKENS = {"インストール案内メール", "send installation email"}
INSTALLATION_EMAIL_ATTR_TOKENS = {"installation-email", "installation_email", "send-installation-email"}
ALIAS_LABEL_TOKENS = {"alias", "エイリアス", "証明書エイリアス"}
PASSWORD_LABEL_TOKENS = {"password", "パスワード", "証明書パスワード"}


class ResultRowError(RuntimeError):
    pass


def _base_dir() -> Path:
    return Path(__file__).resolve().parent


def _sanitize_url(raw_url: str | None) -> str:
    if not raw_url:
        return ""
    parsed = urlsplit(raw_url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _sanitize_href_host_path(raw_href: str | None) -> str:
    if not raw_href:
        return ""
    parsed = urlsplit(raw_href)
    return f"{parsed.netloc}{parsed.path or ''}"


def _safe_attr(element, attr_name: str) -> str:
    return (element.get_attribute(attr_name) or "").strip()


def _safe_text(element) -> str:
    return ((getattr(element, "text", "") or "").strip())


def _normalize_detail_text(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).replace("\n", " ").replace("\t", " ").split()).strip().casefold()


def _detail_element_text(element) -> str:
    return _normalize_detail_text(
        _safe_text(element)
        or _safe_attr(element, "textContent")
        or _safe_attr(element, "value")
        or _safe_attr(element, "title")
        or _safe_attr(element, "aria-label")
    )


def _visible_detail_elements(container, selector: str) -> list[object]:
    return [element for element in container.find_elements(By.CSS_SELECTOR, selector) if _is_displayed(element)]


def _has_label_token(text: str, tokens: set[str]) -> bool:
    normalized = _normalize_detail_text(text)
    return any(_normalize_detail_text(token) in normalized for token in tokens)


def _password_action_elements(container, tokens: set[str], selectors: tuple[str, ...]) -> list[object]:
    candidates = []
    for selector in selectors:
        candidates.extend(container.find_elements(By.CSS_SELECTOR, selector))
    unique = []
    for element in candidates:
        if not _is_displayed(element):
            continue
        searchable = " ".join(
            _normalize_detail_text(value)
            for value in (
                _safe_text(element),
                _safe_attr(element, "aria-label"),
                _safe_attr(element, "title"),
                _safe_attr(element, "data-testid"),
            )
            if value
        )
        if not any(_normalize_detail_text(token) in searchable for token in tokens):
            continue
        if element not in unique:
            unique.append(element)
    return unique


def find_password_reveal_elements(container) -> list[object]:
    return _password_action_elements(
        container,
        {"パスワードを表示する", "password", "show", "reveal"},
        (
            "button[aria-label*='show' i],button[aria-label*='表示' i],"
            "[data-testid*='reveal' i],[data-testid*='show' i]",
            "button",
            "input",
            "[role='button']",
        ),
    )


def inspect_password_reveal_candidates(container) -> dict[str, object]:
    candidates = find_password_reveal_elements(container)
    safe_candidates = []
    for candidate in candidates:
        click_target = _password_click_target(candidate, container)
        if click_target is not None and _password_reveal_candidate_is_safe(candidate, click_target, container):
            safe_candidates.append(candidate)
    candidate_count = len(candidates)
    unique = candidate_count == 1
    displayed = unique and _is_displayed(candidates[0])
    enabled = unique and _is_enabled(candidates[0])
    disabled = unique and _is_disabled(candidates[0])
    inside_dialog = unique and _is_inside_detail_dialog(candidates[0], container)
    return {
        "candidates": candidates,
        "candidate_count": candidate_count,
        "unique": unique,
        "displayed": displayed,
        "enabled": enabled,
        "disabled": disabled,
        "inside_detail_dialog": inside_dialog,
        "safe": len(safe_candidates) == 1 and unique,
    }


def _password_click_target(candidate, container):
    tag_name = (getattr(candidate, "tag_name", "") or "").casefold()
    role = _safe_attr(candidate, "role").casefold()
    if tag_name in {"button", "input"} or role == "button":
        return candidate
    ancestors = candidate.find_elements(By.XPATH, "ancestor::*[self::button or @role='button']")
    return ancestors[0] if ancestors else None


def _password_reveal_candidate_is_safe(candidate, click_target, container) -> bool:
    searchable = " ".join(
        _normalize_detail_text(value)
        for value in (
            _safe_text(candidate),
            _safe_attr(candidate, "aria-label"),
            _safe_attr(candidate, "title"),
            _safe_attr(candidate, "data-testid"),
        )
        if value
    )
    excluded_tokens = (
        "download", "ダウンロード", "copy", "コピー", "close", "閉じる",
        "delete", "削除", "remove", "revoke", "失効",
    )
    return (
        _is_displayed(candidate)
        and _is_enabled(click_target)
        and not _is_disabled(candidate)
        and not _is_disabled(click_target)
        and _is_inside_detail_dialog(candidate, container)
        and not any(_normalize_detail_text(token) in searchable for token in excluded_tokens)
    )


def _is_enabled(element) -> bool:
    try:
        return bool(element.is_enabled())
    except Exception:
        return False


def _is_disabled(element) -> bool:
    try:
        return (
            _safe_attr(element, "aria-disabled").casefold() == "true"
            or element.get_attribute("disabled") is not None
            or not _is_enabled(element)
        )
    except Exception:
        return True


def _is_inside_detail_dialog(candidate, container) -> bool:
    try:
        return candidate in container.find_elements(By.CSS_SELECTOR, "button,input,[role='button']") or bool(
            candidate.find_elements(By.XPATH, "ancestor::*[@role='dialog' or @aria-modal='true']")
        )
    except Exception:
        return False


def find_password_copy_elements(container) -> list[object]:
    return _password_action_elements(
        container,
        {"コピー", "copy"},
        (
            "button[aria-label*='copy' i],button[aria-label*='コピー' i],"
            "[data-testid*='copy' i]",
            "button",
            "input",
            "[role='button']",
        ),
    )


def find_password_section(container) -> object | None:
    labels = _visible_detail_elements(
        container,
        "dt,th,label,[role='rowheader'],[data-testid*='label' i],[class*='label' i],[data-label]",
    )
    for label in labels:
        if not _has_label_token(_detail_element_text(label), PASSWORD_LABEL_TOKENS):
            continue
        rows = label.find_elements(By.XPATH, "ancestor::*[self::tr or @role='row' or self::fieldset][1]")
        if rows:
            return rows[0]
        parents = label.find_elements(By.XPATH, "ancestor::div[1]")
        if parents:
            return parents[0]
    text_matches = [
        element
        for element in _visible_detail_elements(container, "*")
        if _has_label_token(_detail_element_text(element), PASSWORD_LABEL_TOKENS)
    ]
    for label in text_matches:
        ancestors = label.find_elements(By.XPATH, "ancestor::*")
        for ancestor in ancestors:
            reveal_candidates = find_password_reveal_elements(ancestor)
            if len(reveal_candidates) == 1:
                return ancestor
    return None


def resolve_password_reveal_in_section(container) -> tuple[object, list[object]] | None:
    labels = [
        element
        for element in _visible_detail_elements(container, "dt,th,label,[role='rowheader'],[data-label],*")
        if _has_label_token(_detail_element_text(element), PASSWORD_LABEL_TOKENS)
    ]
    reveals = find_password_reveal_elements(container)
    if len(reveals) != 1:
        return None
    reveal = reveals[0]
    for label in labels:
        for ancestor in label.find_elements(By.XPATH, "ancestor::*"):
            descendants = ancestor.find_elements(By.CSS_SELECTOR, "button,input,[role='button']")
            if reveal in descendants:
                return ancestor, reveals
    return None


def password_section_scroll_target(section):
    ancestors = list(reversed(section.find_elements(By.XPATH, "ancestor::*")))
    candidates = [section] + ancestors
    for candidate in candidates:
        try:
            if int(candidate.get_attribute("scrollHeight") or 0) > int(candidate.get_attribute("clientHeight") or 0):
                return candidate
        except Exception:
            continue
    for candidate in candidates:
        role = _safe_attr(candidate, "role").casefold()
        aria_modal = _safe_attr(candidate, "aria-modal").casefold()
        classes = _safe_attr(candidate, "class").casefold()
        if role == "dialog" or aria_modal == "true" or "drawer" in classes:
            return candidate
    return section


def inspect_password_copy_candidates(section) -> dict[str, object]:
    copy_candidates = find_password_copy_elements(section)
    masked_fields = _visible_detail_elements(
        section,
        "input[type='password'],[class*='mask' i],[data-testid*='mask' i]",
    )
    eye_candidates = _password_action_elements(
        section,
        {"eye", "目", "show", "表示"},
        ("button", "[role='button']"),
    )
    safe_candidates = [candidate for candidate in copy_candidates if _copy_candidate_is_safe(candidate, section, masked_fields)]
    unique = len(copy_candidates) == 1
    return {
        "candidates": copy_candidates,
        "candidate_count": len(copy_candidates),
        "unique": unique,
        "displayed": unique and _is_displayed(copy_candidates[0]),
        "enabled": unique and _is_enabled(copy_candidates[0]),
        "safe": len(safe_candidates) == 1 and unique,
        "masked_password_field_count": len(masked_fields),
        "password_eye_button_candidate_count": len(eye_candidates),
    }


def _copy_candidate_is_safe(candidate, section, masked_fields) -> bool:
    if not masked_fields or _password_click_target(candidate, section) is None:
        return False
    searchable = " ".join(
        _normalize_detail_text(value)
        for value in (
            _safe_text(candidate),
            _safe_attr(candidate, "aria-label"),
            _safe_attr(candidate, "title"),
            _safe_attr(candidate, "data-testid"),
        )
        if value
    )
    excluded = ("download", "ダウンロード", "close", "閉じる", "delete", "削除", "revoke", "失効", "mail", "メール", "eye", "目", "show", "表示")
    return (
        _is_displayed(candidate)
        and _is_enabled(candidate)
        and not _is_disabled(candidate)
        and _is_inside_detail_dialog(candidate, section)
        and _same_password_row(candidate, masked_fields)
        and any(_normalize_detail_text(token) in searchable for token in ("copy", "コピー"))
        and not any(_normalize_detail_text(token) in searchable for token in excluded)
    )


def _same_password_row(candidate, masked_fields) -> bool:
    try:
        for field in masked_fields:
            candidate_rows = candidate.find_elements(
                By.XPATH,
                "ancestor::*[self::tr or @role='row' or self::fieldset or contains(@class, 'row') or contains(@data-testid, 'row')]",
            )
            field_rows = field.find_elements(
                By.XPATH,
                "ancestor::*[self::tr or @role='row' or self::fieldset or contains(@class, 'row') or contains(@data-testid, 'row')]",
            )
            if candidate_rows and field_rows and any(row == field_row for row in candidate_rows for field_row in field_rows):
                return True
            candidate_divs = candidate.find_elements(By.XPATH, "ancestor::div[1]")
            field_divs = field.find_elements(By.XPATH, "ancestor::div[1]")
            if candidate_divs and field_divs and candidate_divs[0] == field_divs[0]:
                return True
            for candidate_div in candidate.find_elements(By.XPATH, "ancestor::div"):
                for field_div in field.find_elements(By.XPATH, "ancestor::div"):
                    if candidate_div != field_div:
                        continue
                    password_fields = field_div.find_elements(
                        By.CSS_SELECTOR,
                        "input[type='password'],[class*='mask' i],[data-testid*='mask' i]",
                    )
                    if len(password_fields) == 1:
                        return True
        return False
    except Exception:
        return False


def _detail_value_candidates(label_element, container) -> list[object]:
    candidates = []
    label_for = _safe_attr(label_element, "for")
    if label_for:
        candidates.extend(container.find_elements(By.CSS_SELECTOR, f"[id='{label_for}']"))

    candidates.extend(label_element.find_elements(By.XPATH, "following-sibling::*[1]"))
    parents = label_element.find_elements(By.XPATH, "ancestor::*[self::tr or self::div or self::dd or self::dt or self::fieldset][1]")
    if parents:
        candidates.extend(parents[0].find_elements(By.CSS_SELECTOR, "dd,td,input,textarea,[role='cell'],[data-testid],.value,[class*='value' i]"))

    label_id = _safe_attr(label_element, "id")
    if label_id:
        for element in _visible_detail_elements(container, "[aria-labelledby]"):
            if label_id in set((_safe_attr(element, "aria-labelledby") or "").split()):
                candidates.append(element)
    return [candidate for candidate in candidates if candidate is not label_element and _is_displayed(candidate)]


def _extract_detail_field_observation(container) -> dict[str, object]:
    label_elements = _visible_detail_elements(container, "dt,th,label,[role='rowheader'],[data-testid*='label' i],[class*='label' i],[data-label]")
    value_elements = _visible_detail_elements(container, "dd,td,input,textarea,[role='cell'],[data-testid*='value' i],.value,[class*='value' i]")
    alias_label_found = False
    alias_values = []
    password_label_found = False
    password_values = []

    for label in label_elements:
        label_text = _detail_element_text(label)
        if _has_label_token(label_text, ALIAS_LABEL_TOKENS):
            alias_label_found = True
            alias_values.extend(_detail_element_text(item) for item in _detail_value_candidates(label, container))
        if _has_label_token(label_text, PASSWORD_LABEL_TOKENS):
            password_label_found = True
            password_values.extend(_detail_element_text(item) for item in _detail_value_candidates(label, container))

    alias_values = [value for value in alias_values if value]
    password_values = [value for value in password_values if value]
    return {
        "field_row_count": len(_visible_detail_elements(container, "tr,[role='row'],fieldset,form,[data-testid*='field' i]")),
        "label_count": len(label_elements),
        "value_count": len(value_elements),
        "alias_label_found": alias_label_found,
        "alias_value_found": bool(alias_values),
        "alias_value_nonblank": any(alias_values),
        "alias_values": alias_values,
        "password_label_found": password_label_found,
        "password_values": password_values,
    }


def _extract_password_structure(container) -> dict[str, object]:
    password_inputs = _visible_detail_elements(container, "input[type='password']")
    readonly_values = [element for element in _visible_detail_elements(container, "input[readonly],textarea[readonly]") if element not in password_inputs]
    text_inputs = _visible_detail_elements(container, "input[type='text'],textarea")
    masked_values = [element for element in _visible_detail_elements(container, "[class*='mask' i],[data-testid*='mask' i],[aria-label*='password' i]") if element not in password_inputs]
    reveal_buttons = find_password_reveal_elements(container)
    copy_buttons = find_password_copy_elements(container)
    fields = _extract_detail_field_observation(container)
    password_values = [value for value in fields["password_values"] if value]
    password_values.extend(
        _normalize_detail_text(element.get_attribute("value"))
        for element in password_inputs + readonly_values
        if element.get_attribute("value")
    )
    display_value_source_count = 1 if password_values and not password_inputs and not readonly_values else 0
    source_count = len(password_inputs) + len(readonly_values) + display_value_source_count
    return {
        "password_input_candidate_count": len(password_inputs),
        "readonly_value_candidate_count": len(readonly_values),
        "text_input_candidate_count": len(text_inputs),
        "masked_value_candidate_count": len(masked_values),
        "reveal_button_candidate_count": len(reveal_buttons),
        "copy_button_candidate_count": len(copy_buttons),
        "password_label_found": fields["password_label_found"],
        "password_value_container_found": bool(password_values),
        "password_source_candidate_count": source_count,
        "password_source_requires_download_action": source_count == 0,
        "password_source_requires_reveal_action": bool(reveal_buttons) and source_count == 0,
        "password_source_requires_copy_action": bool(copy_buttons) and source_count == 0,
        "password_values": password_values,
    }


def _is_displayed(element) -> bool:
    try:
        return bool(element.is_displayed())
    except Exception:
        return False


def _find_single_visible_table_row(driver):
    rows = [row for row in driver.find_elements(By.CSS_SELECTOR, "table tbody tr") if _is_displayed(row)]
    if len(rows) != 1:
        raise ResultRowError(f"表示中table tbody trの件数が不正です: {len(rows)}")
    return rows[0]


def _is_update_like_action(action: dict[str, str]) -> bool:
    searchable = " ".join(
        [
            action.get("type", ""),
            action.get("id", ""),
            action.get("name", ""),
            action.get("class", ""),
            action.get("role", ""),
            action.get("aria_label", ""),
            action.get("title", ""),
            action.get("data_testid", ""),
            action.get("href_host_path", ""),
        ]
    ).lower()
    return any(keyword in searchable for keyword in UPDATE_KEYWORDS)


def _extract_action_elements(container):
    actions = []
    for selector in ("a", "button", "input", "[role='button']", "[role='link']"):
        for element in container.find_elements(By.CSS_SELECTOR, selector):
            if not _is_displayed(element):
                continue
            tag = (getattr(element, "tag_name", "") or "").strip().lower()
            href_host_path = _sanitize_href_host_path(_safe_attr(element, "href")) if tag == "a" else ""
            type_attr = _safe_attr(element, "type").lower()
            id_attr = _safe_attr(element, "id")
            name_attr = _safe_attr(element, "name")
            class_attr = _safe_attr(element, "class")
            role_attr = _safe_attr(element, "role")
            aria_attr = _safe_attr(element, "aria-label")
            title_attr = _safe_attr(element, "title")
            data_testid_attr = _safe_attr(element, "data-testid")
            normalized_label = _normalize_action_text(_safe_text(element))
            label_category = _classify_action(
                normalized_text=normalized_label,
                type_attr=type_attr,
                name_attr=name_attr,
                aria_label_attr=aria_attr,
                title_attr=title_attr,
                data_testid_attr=data_testid_attr,
                href_host_path=href_host_path,
            )
            actions.append(
                {
                    "element": element,
                    "tag": tag,
                    "type": type_attr,
                    "id": id_attr,
                    "name": name_attr,
                    "class": class_attr,
                    "role": role_attr,
                    "aria_label": aria_attr,
                    "title": title_attr,
                    "data_testid": data_testid_attr,
                    "href_host_path": href_host_path,
                    "label_category": label_category,
                }
            )
    return actions


def _normalize_action_text(raw_text: str) -> str:
    if not raw_text:
        return ""
    return " ".join(raw_text.replace("\n", " ").split()).strip().lower()


def _classify_label(normalized_text: str) -> str:
    if normalized_text in {token.lower() for token in DOWNLOAD_LABEL_TOKENS}:
        return LABEL_CATEGORY_DOWNLOAD
    if normalized_text in {token.lower() for token in CLOSE_LABEL_TOKENS}:
        return LABEL_CATEGORY_CLOSE
    if normalized_text in {token.lower() for token in SAVE_LABEL_TOKENS}:
        return LABEL_CATEGORY_SAVE
    if normalized_text in {token.lower() for token in CANCEL_LABEL_TOKENS}:
        return LABEL_CATEGORY_CANCEL
    if normalized_text in {token.lower() for token in REVOKE_LABEL_TOKENS}:
        return LABEL_CATEGORY_REVOKE
    if normalized_text in {token.lower() for token in INSTALLATION_EMAIL_LABEL_TOKENS}:
        return LABEL_CATEGORY_INSTALLATION_EMAIL

    if any(token.lower() in normalized_text for token in DOWNLOAD_LABEL_TOKENS):
        return LABEL_CATEGORY_DOWNLOAD
    if any(token.lower() in normalized_text for token in CLOSE_LABEL_TOKENS):
        return LABEL_CATEGORY_CLOSE
    if any(token.lower() in normalized_text for token in SAVE_LABEL_TOKENS):
        return LABEL_CATEGORY_SAVE
    if any(token.lower() in normalized_text for token in CANCEL_LABEL_TOKENS):
        return LABEL_CATEGORY_CANCEL
    if any(token.lower() in normalized_text for token in REVOKE_LABEL_TOKENS):
        return LABEL_CATEGORY_REVOKE
    if any(token.lower() in normalized_text for token in INSTALLATION_EMAIL_LABEL_TOKENS):
        return LABEL_CATEGORY_INSTALLATION_EMAIL

    return LABEL_CATEGORY_UNKNOWN


def _contains_any_token(text: str, tokens: set[str]) -> bool:
    return any(token in text for token in tokens)


def _classify_action(
    *,
    normalized_text: str,
    type_attr: str,
    name_attr: str,
    aria_label_attr: str,
    title_attr: str,
    data_testid_attr: str,
    href_host_path: str,
) -> str:
    data_testid_l = (data_testid_attr or "").lower()
    name_l = (name_attr or "").lower()
    aria_l = (aria_label_attr or "").lower()
    title_l = (title_attr or "").lower()
    text_l = (normalized_text or "").lower()
    href_l = (href_host_path or "").lower()
    type_l = (type_attr or "").lower()

    # Priority a: installation_email (safe attributes first, before display text)
    if data_testid_l == "send-installation-email-toolbar":
        return LABEL_CATEGORY_INSTALLATION_EMAIL
    safe_join = " ".join([data_testid_l, aria_l, title_l, name_l])
    if "send-installation-email" in data_testid_l:
        return LABEL_CATEGORY_INSTALLATION_EMAIL
    if _contains_any_token(safe_join, INSTALLATION_EMAIL_ATTR_TOKENS):
        return LABEL_CATEGORY_INSTALLATION_EMAIL
    if _contains_any_token(text_l, {token.lower() for token in INSTALLATION_EMAIL_LABEL_TOKENS}):
        return LABEL_CATEGORY_INSTALLATION_EMAIL

    # Priority b: revoke
    if _contains_any_token(" ".join([text_l, aria_l, title_l, data_testid_l, name_l]), {token.lower() for token in REVOKE_LABEL_TOKENS}):
        return LABEL_CATEGORY_REVOKE

    # Priority c: close
    if _contains_any_token(" ".join([text_l, aria_l, title_l, data_testid_l, name_l]), {token.lower() for token in CLOSE_LABEL_TOKENS}):
        return LABEL_CATEGORY_CLOSE

    # Priority d: save
    if _contains_any_token(" ".join([text_l, aria_l, title_l, data_testid_l, name_l]), {token.lower() for token in SAVE_LABEL_TOKENS}):
        return LABEL_CATEGORY_SAVE

    # Priority e: cancel
    if _contains_any_token(" ".join([text_l, aria_l, title_l, data_testid_l, name_l]), {token.lower() for token in CANCEL_LABEL_TOKENS}):
        return LABEL_CATEGORY_CANCEL

    # Priority f: download
    download_signal = _contains_any_token(text_l, {token.lower() for token in DOWNLOAD_LABEL_TOKENS})
    download_signal = download_signal or _contains_any_token(" ".join([aria_l, title_l, data_testid_l]), {"download", "ダウンロード"})
    download_signal = download_signal or any(token in href_l for token in ["/download", "/export", "download", "export"])
    if download_signal and type_l != "submit":
        return LABEL_CATEGORY_DOWNLOAD

    # Priority g: unknown, including generic submit buttons.
    return LABEL_CATEGORY_UNKNOWN


def _assert_row_click_safety(row) -> None:
    if (getattr(row, "tag_name", "") or "").strip().lower() != "tr":
        raise ResultRowError("結果行クリック安全確認失敗: tag_nameがtrではありません")
    if not _is_displayed(row):
        raise ResultRowError("結果行クリック安全確認失敗: 行が非表示です")

    button_count = len([e for e in row.find_elements(By.CSS_SELECTOR, "button") if _is_displayed(e)])
    input_count = len([e for e in row.find_elements(By.CSS_SELECTOR, "input") if _is_displayed(e)])
    if button_count > 0 or input_count > 0:
        raise ResultRowError("結果行クリック安全確認失敗: 行内にbuttonまたはinputが存在します")

    actions = _extract_action_elements(row)
    if any(_is_update_like_action(item) for item in actions):
        raise ResultRowError("結果行クリック安全確認失敗: 更新系操作要素を検出しました")


def _is_detail_heading_visible(driver) -> bool:
    for selector in ("h1", "h2", "[role='heading']"):
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            try:
                if not element.is_displayed():
                    continue
                raw = (element.text or "").strip()
                if raw.lower() in {x.lower() for x in LIST_HEADINGS_EXCLUDED}:
                    continue
                text = raw.lower()
                if any(keyword in text for keyword in DETAIL_HEADING_KEYWORDS):
                    return True
            except Exception:
                continue
    return False


def _collect_ancestor_chain(element):
    chain = []
    for ancestor in element.find_elements(By.XPATH, "ancestor-or-self::*"):
        if _is_displayed(ancestor):
            chain.append(ancestor)
    return chain


def _is_global_container(element) -> bool:
    tag = (getattr(element, "tag_name", "") or "").strip().lower()
    if tag in {"html", "body", "main"}:
        return True
    role = _safe_attr(element, "role").lower()
    if role == "main":
        return True
    return False


def _contains_visible(element, selector: str) -> bool:
    return any(_is_displayed(item) for item in element.find_elements(By.CSS_SELECTOR, selector))


def _score_detail_candidate(element) -> int:
    score = 0
    tag = (getattr(element, "tag_name", "") or "").strip().lower()
    role = _safe_attr(element, "role").lower()
    klass = _safe_attr(element, "class").lower()
    style = _safe_attr(element, "style").lower().replace(" ", "")

    if tag in {"aside", "section", "dialog"}:
        score += 2
    if role == "dialog":
        score += 2
    if "position:fixed" in style or "position:absolute" in style:
        score += 2
    if any(token in klass for token in ["drawer", "panel", "detail", "right", "sidebar"]):
        score += 1
    return score


def _choose_detail_area_from_close_button(driver):
    close_buttons = [e for e in driver.find_elements(By.CSS_SELECTOR, "button[aria-label='Close']") if _is_displayed(e)]
    if len(close_buttons) != 1:
        return None

    close_button = close_buttons[0]
    note_inputs = [e for e in driver.find_elements(By.CSS_SELECTOR, "input[name='note']") if _is_displayed(e)]

    close_chain = _collect_ancestor_chain(close_button)

    common_candidates = []
    if note_inputs:
        note_chain = _collect_ancestor_chain(note_inputs[0])
        note_chain_ids = {id(item) for item in note_chain}
        for ancestor in close_chain:
            if id(ancestor) in note_chain_ids:
                common_candidates.append(ancestor)
    else:
        common_candidates = list(close_chain)

    valid = []
    for candidate in common_candidates:
        if _is_global_container(candidate):
            continue
        if not _contains_visible(candidate, "button[aria-label='Close']"):
            continue
        has_note = _contains_visible(candidate, "input[name='note']")
        has_mail_toolbar = _contains_visible(candidate, "button[data-testid='send-installation-email-toolbar']")
        if not (has_note or has_mail_toolbar):
            continue
        valid.append(candidate)

    if not valid:
        return None

    valid.sort(key=lambda item: (-_score_detail_candidate(item), -len(_collect_ancestor_chain(item))))
    return valid[0]


def _identify_detail_area(driver):
    by_close = _choose_detail_area_from_close_button(driver)
    if by_close is not None:
        return by_close, DETAIL_METHOD_CLOSE_COMMON

    visible_dialogs = [e for e in driver.find_elements(By.CSS_SELECTOR, "[role='dialog']") if _is_displayed(e) and not _is_global_container(e)]
    if visible_dialogs:
        return visible_dialogs[0], DETAIL_METHOD_DIALOG

    for selector in ["[class*='drawer']", "[data-testid*='drawer']", "[aria-modal='true']"]:
        drawers = [e for e in driver.find_elements(By.CSS_SELECTOR, selector) if _is_displayed(e) and not _is_global_container(e)]
        if drawers:
            return drawers[0], DETAIL_METHOD_DRAWER

    raise RuntimeError("詳細領域を特定できませんでした")


def _count_visible(driver, selector: str) -> int:
    return len([e for e in driver.find_elements(By.CSS_SELECTOR, selector) if _is_displayed(e)])


def _count_detail_area_candidates(driver) -> int:
    count = 0
    if _choose_detail_area_from_close_button(driver) is not None:
        count += 1
    dialogs = [e for e in driver.find_elements(By.CSS_SELECTOR, "[role='dialog']") if _is_displayed(e) and not _is_global_container(e)]
    if dialogs:
        count += 1
    for selector in ["[class*='drawer']", "[data-testid*='drawer']", "[aria-modal='true']"]:
        drawers = [e for e in driver.find_elements(By.CSS_SELECTOR, selector) if _is_displayed(e) and not _is_global_container(e)]
        if drawers:
            count += 1
            break
    return count


def _find_visible_detail_surfaces(driver):
    surfaces = []
    for selector in DETAIL_DRAWER_SELECTORS:
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            if _is_displayed(element):
                surfaces.append(element)
    return surfaces


def _wait_detail_ready(driver, before_path: str, logger: AppLogger, timeout_seconds: int = DETAIL_WAIT_TIMEOUT_SECONDS):
    end_time = time.monotonic() + timeout_seconds
    wait_start = time.monotonic()
    last_state_key = None
    final_counts = None

    while time.monotonic() < end_time:
        current_path = urlsplit(_sanitize_url(driver.current_url)).path
        path_changed = current_path != before_path
        has_heading = _is_detail_heading_visible(driver)
        close_count = _count_visible(driver, "button[aria-label='Close']")
        note_count = _count_visible(driver, "input[name='note']")
        mail_toolbar_count = _count_visible(driver, "button[data-testid='send-installation-email-toolbar']")
        dialog_count = _count_visible(driver, "[role='dialog']")
        drawer_count = _count_visible(driver, "[class*='drawer']") + _count_visible(driver, "[data-testid*='drawer']") + _count_visible(driver, "[aria-modal='true']")

        detail_area = None
        detail_method = ""
        detail_area_found = False
        try:
            detail_area, detail_method = _identify_detail_area(driver)
            detail_area_found = detail_area is not None
        except RuntimeError:
            detail_area_found = False

        state_key = (close_count, note_count, mail_toolbar_count, dialog_count + drawer_count, detail_area_found)
        if state_key != last_state_key:
            logger.info(
                "詳細表示待機状態 "
                f"close_count={close_count}, "
                f"note_count={note_count}, "
                f"mail_toolbar_count={mail_toolbar_count}, "
                f"dialog_count={dialog_count + drawer_count}, "
                f"detail_area_found={detail_area_found}"
            )
            last_state_key = state_key

        close_ready = close_count == 1
        supporting_signal = note_count == 1 or mail_toolbar_count == 1 or (dialog_count + drawer_count) > 0
        heading_only = has_heading and not supporting_signal and not close_ready

        if close_ready and supporting_signal and detail_area_found:
            elapsed = time.monotonic() - wait_start
            logger.info(
                "詳細表示待機成功 "
                f"elapsed={elapsed:.3f}s, "
                f"method={detail_method}"
            )
            return {
                "path_changed": path_changed,
                "has_heading": has_heading,
                "has_surface": (dialog_count + drawer_count) > 0,
                "detail_area": detail_area,
                "detail_method": detail_method,
                "close_count": close_count,
                "note_count": note_count,
                "mail_toolbar_count": mail_toolbar_count,
                "dialog_count": dialog_count + drawer_count,
                "detail_area_found": detail_area_found,
            }

        if heading_only:
            pass

        final_counts = {
            "close_count": close_count,
            "note_count": note_count,
            "mail_toolbar_count": mail_toolbar_count,
            "dialog_count": dialog_count + drawer_count,
            "detail_area_candidate_count": _count_detail_area_candidates(driver),
        }

        time.sleep(0.25)

    timeout_counts = final_counts or {
        "close_count": 0,
        "note_count": 0,
        "mail_toolbar_count": 0,
        "dialog_count": 0,
        "detail_area_candidate_count": 0,
    }
    logger.error(
        "詳細表示待機タイムアウト最終件数 "
        f"close_count={timeout_counts['close_count']}, "
        f"note_count={timeout_counts['note_count']}, "
        f"mail_toolbar_count={timeout_counts['mail_toolbar_count']}, "
        f"dialog_count={timeout_counts['dialog_count']}, "
        f"detail_area_candidate_count={timeout_counts['detail_area_candidate_count']}"
    )
    raise RuntimeError("詳細画面へ遷移しませんでした")


def _collect_detail_actions(driver, surfaces):
    _ = surfaces
    raise RuntimeError("詳細領域は _identify_detail_area を使用して特定してください")


def _is_download_candidate(action: dict[str, str]) -> bool:
    return action.get("label_category") == LABEL_CATEGORY_DOWNLOAD


def _log_actions(logger: AppLogger, actions):
    logger.info(f"詳細画面操作要素数: {len(actions)}")
    for index, item in enumerate(actions, start=1):
        logger.info(
            "詳細画面要素 "
            f"#{index} "
            f"tag={item['tag']}, "
            f"type={item['type']}, "
            f"id={item['id']}, "
            f"name={item['name']}, "
            f"class={item['class']}, "
            f"role={item['role']}, "
            f"aria-label={item['aria_label']}, "
            f"title={item['title']}, "
            f"data-testid={item['data_testid']}, "
            f"href={item['href_host_path']}, "
            f"label_category={item['label_category']}"
        )


def _save_diag_no_html_with_notice(logger: AppLogger, browser: Browser, name: str) -> None:
    logger.info("スクリーンショットには個人情報が含まれる可能性があります")
    logger.save_browser_diagnostics(browser.driver, name, save_html=False)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1 or not args[0].strip():
        print("Usage: python diagnose_hennge_certificate_detail.py <TEST_ALIAS>")
        return 1

    alias = args[0].strip()
    _ = alias

    base_dir = _base_dir()
    config = load_config()
    ensure_directories(config)
    logger = AppLogger(base_dir)
    browser = Browser(base_dir, config)

    logger.info("診断モード: 証明書詳細画面とダウンロード入口を読み取り専用で調査します")
    logger.info("結果行クリック1回以外のクリック、ダウンロード、登録、失効、更新、削除、一覧作成は実行しません")
    logger.info("SMSM、Excel、IMEI、ファイル操作は実行しません")

    try:
        browser.start()
        handler = HenngeHandler(config, logger, browser)
        handler.login()

        if browser.driver is None:
            raise RuntimeError("ログイン後にブラウザー状態を取得できません")

        browser.open(CERTIFICATES_URL)
        browser.wait_for_page_ready(timeout=20)

        search_input = cert_search._wait_certificate_search_input_ready(
            browser.driver,
            logger,
            timeout_seconds=SEARCH_INPUT_TIMEOUT_SECONDS,
        )
        try:
            cert_search._set_query_and_submit(search_input, alias, logger)
        except StaleElementReferenceException:
            logger.error("検索欄再取得を実行します: StaleElementReferenceException")
            refreshed = cert_search._find_single_visible_search_input(
                browser.driver,
                cert_search.SEARCH_INPUT_PRIMARY_SELECTOR,
            )
            cert_search._set_query_and_submit(refreshed, alias, logger)

        result_count = cert_search._wait_results_ready(browser, timeout_seconds=RESULT_TIMEOUT_SECONDS)
        cert_search._log_result_state(logger, result_count)

        if result_count == 0:
            return 2
        if result_count > 1:
            return 3

        rows = [row for row in browser.driver.find_elements(By.CSS_SELECTOR, "table tbody tr") if _is_displayed(row)]
        if len(rows) != 1:
            return 4

        row = rows[0]
        _assert_row_click_safety(row)

        before_url = _sanitize_url(browser.driver.current_url)
        before_path = urlsplit(before_url).path
        logger.info(f"クリック前URL: {before_url}")

        logger.info("結果行クリック開始")
        row.click()
        logger.info("結果行クリック完了")

        detail_state = _wait_detail_ready(browser.driver, before_path, logger, timeout_seconds=DETAIL_WAIT_TIMEOUT_SECONDS)

        current_url = _sanitize_url(browser.driver.current_url)
        logger.info(f"遷移後URL: {current_url}")
        detail_area = detail_state["detail_area"]
        detail_method = detail_state["detail_method"]

        actions = _extract_action_elements(detail_area)
        _log_actions(logger, actions)

        download_candidates = [item for item in actions if _is_download_candidate(item)]
        logger.info(f"ダウンロード候補要素数: {len(download_candidates)}")

        if len(download_candidates) == 0:
            return 6
        if len(download_candidates) > 1:
            return 7
        return 0
    except KeyboardInterrupt:
        logger.error("診断を中断しました: KeyboardInterrupt")
        try:
            _save_diag_no_html_with_notice(logger, browser, "hennge_certificate_detail_interrupted")
        except Exception:
            logger.exception("中断時の診断情報保存に失敗しました")
        return 130
    except ResultRowError:
        logger.exception("結果行判定に失敗しました")
        return 4
    except RuntimeError as ex:
        if str(ex) in {"詳細画面へ遷移しませんでした", "詳細領域を特定できませんでした"}:
            logger.error("詳細画面表示の待機に失敗しました")
            return 5
        logger.exception("HENNGE証明書詳細診断に失敗しました")
        try:
            _save_diag_no_html_with_notice(logger, browser, "hennge_certificate_detail_failure")
        except Exception:
            logger.exception("失敗時の診断情報保存に失敗しました")
        return 1
    except Exception:
        logger.exception("HENNGE証明書詳細診断に失敗しました")
        try:
            _save_diag_no_html_with_notice(logger, browser, "hennge_certificate_detail_failure")
        except Exception:
            logger.exception("失敗時の診断情報保存に失敗しました")
        return 1
    finally:
        browser.quit()


if __name__ == "__main__":
    raise SystemExit(main())
