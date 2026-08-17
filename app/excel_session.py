from __future__ import annotations

import os
import time
import uuid
import hashlib
import re
from ctypes import wintypes
import ctypes
from dataclasses import dataclass
from pathlib import Path
import unicodedata
from urllib.parse import quote, unquote, urlsplit


class ReadOnlyWorkbookError(RuntimeError):
    pass


class SaveCloseWorkbookError(RuntimeError):
    pass


OBJID_NATIVEOM = 0xFFFFFFF0
WINDOW_CLASS_XLMAIN = "XLMAIN"
WINDOW_CLASS_XLDESK = "XLDESK"
WINDOW_CLASS_EXCEL7 = "EXCEL7"
WEB_IDENTITY_ENV_TEST = "HENNGE_EXCEL_WEB_IDENTITY_HASH_TEST"
WEB_IDENTITY_ENV_PRODUCTION = "HENNGE_EXCEL_WEB_IDENTITY_HASH_PRODUCTION"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


@dataclass(frozen=True)
class WebIdentityResolution:
    hash_value: str
    source: str
    valid: bool


@dataclass(frozen=True)
class WebIdentityEnvironmentDiagnostics:
    selected_mode: str
    env_name_selected: str
    env_present: bool
    env_value_length: int
    env_length_valid: bool
    env_hex_valid: bool


def _build_iid_idispatch_guid() -> GUID:
    guid_value = uuid.UUID("{00020400-0000-0000-C000-000000000046}")
    data = guid_value.bytes
    data1 = int.from_bytes(data[0:4], byteorder="big", signed=False)
    data2 = int.from_bytes(data[4:6], byteorder="big", signed=False)
    data3 = int.from_bytes(data[6:8], byteorder="big", signed=False)
    data4 = (ctypes.c_ubyte * 8)(*data[8:16])
    return GUID(data1, data2, data3, data4)


@dataclass
class WorkbookDetection:
    xlmain_count: int
    xldesk_count: int
    excel7_count: int
    accessible_object_call_count: int
    accessible_object_success_count: int
    dispatch_wrap_success_count: int
    window_object_count: int
    workbook_object_count: int
    application_object_count: int
    unknown_object_count: int
    application_property_success_count: int
    application_count_before_dedupe: int
    application_count_after_dedupe: int
    application_workbooks_count_success: int
    application_workbooks_count_failure: int
    workbook_item_success_count: int
    workbook_item_failure_count: int
    workbook_fullname_success_count: int
    workbook_fullname_failure_count: int
    normalized_path_success_count: int
    exact_path_match_count: int
    hwnd_unavailable_count: int
    rot_moniker_count: int
    rot_get_object_success_count: int
    rot_get_object_failure_count: int
    rot_workbook_candidate_count: int
    rot_fullname_success_count: int
    rot_fullname_failure_count: int
    rot_normalized_path_success_count: int
    rot_exact_path_match_count: int
    rot_duplicate_candidate_count: int
    native_dispatch_pointer_success_count: int
    native_dispatch_pointer_null_count: int
    native_dispatch_wrap_success_count: int
    native_dispatch_wrap_failure_count: int
    direct_application_success_count: int
    parent_application_success_count: int
    grandparent_application_success_count: int
    native_application_failure_count: int
    native_workbooks_count_success: int
    native_workbooks_count_failure: int
    observed_excel_window_count: int
    observed_application_count: int
    observed_workbook_count: int
    matched_workbook_count: int
    detection_method: str
    configured_path_kind: str
    workbook_path_kind: str
    configured_path_exists: bool
    workbook_path_exists: bool
    extension_equal: bool
    basename_equal: bool
    normalized_equal: bool
    unicode_normalized_equal: bool
    samefile_equal: bool | str
    parent_equal: bool
    target_match_method: str
    compared_workbook_count: int
    local_local_count: int
    local_url_count: int
    basename_equal_count: int
    normalized_equal_count: int
    unicode_normalized_equal_count: int
    samefile_equal_count: int
    samefile_unavailable_count: int
    web_identity_configured: bool
    web_identity_source: str
    web_identity_valid: bool
    web_candidate_count: int
    candidate_hash_generated_count: int
    internal_hash_equal_count: int
    web_identity_match_count: int
    capture_succeeded: bool
    detection_exceptions: list[tuple[str, str, int]]
    application: object | None
    workbook: object | None
    captured_web_identity_hash: str | None


@dataclass
class _DetectionCounters:
    xlmain_count: int = 0
    xldesk_count: int = 0
    excel7_count: int = 0
    accessible_object_call_count: int = 0
    accessible_object_success_count: int = 0
    dispatch_wrap_success_count: int = 0
    window_object_count: int = 0
    workbook_object_count: int = 0
    application_object_count: int = 0
    unknown_object_count: int = 0
    application_property_success_count: int = 0
    application_count_before_dedupe: int = 0
    application_count_after_dedupe: int = 0
    application_workbooks_count_success: int = 0
    application_workbooks_count_failure: int = 0
    workbook_item_success_count: int = 0
    workbook_item_failure_count: int = 0
    workbook_fullname_success_count: int = 0
    workbook_fullname_failure_count: int = 0
    normalized_path_success_count: int = 0
    exact_path_match_count: int = 0
    hwnd_unavailable_count: int = 0
    rot_moniker_count: int = 0
    rot_get_object_success_count: int = 0
    rot_get_object_failure_count: int = 0
    rot_workbook_candidate_count: int = 0
    rot_fullname_success_count: int = 0
    rot_fullname_failure_count: int = 0
    rot_normalized_path_success_count: int = 0
    rot_exact_path_match_count: int = 0
    rot_duplicate_candidate_count: int = 0
    native_dispatch_pointer_success_count: int = 0
    native_dispatch_pointer_null_count: int = 0
    native_dispatch_wrap_success_count: int = 0
    native_dispatch_wrap_failure_count: int = 0
    direct_application_success_count: int = 0
    parent_application_success_count: int = 0
    grandparent_application_success_count: int = 0
    native_application_failure_count: int = 0
    native_workbooks_count_success: int = 0
    native_workbooks_count_failure: int = 0
    matched_workbook_count: int = 0
    configured_path_kind: str = "unknown"
    workbook_path_kind: str = "unknown"
    configured_path_exists: bool = False
    workbook_path_exists: bool = False
    extension_equal: bool = False
    basename_equal: bool = False
    normalized_equal: bool = False
    unicode_normalized_equal: bool = False
    samefile_equal: bool | str = False
    parent_equal: bool = False
    target_match_method: str = "none"
    compared_workbook_count: int = 0
    local_local_count: int = 0
    local_url_count: int = 0
    basename_equal_count: int = 0
    normalized_equal_count: int = 0
    unicode_normalized_equal_count: int = 0
    samefile_equal_count: int = 0
    samefile_unavailable_count: int = 0
    web_identity_configured: bool = False
    web_identity_source: str = "none"
    web_identity_valid: bool = False
    web_candidate_count: int = 0
    candidate_hash_generated_count: int = 0
    internal_hash_equal_count: int = 0
    web_identity_match_count: int = 0
    capture_succeeded: bool = False


class _DetectionDiagnostics:
    def __init__(self) -> None:
        self.counters = _DetectionCounters()
        self._exceptions: dict[tuple[str, str], int] = {}

    def record_exception(self, stage: str, exc: Exception) -> None:
        key = (stage, type(exc).__name__)
        self._exceptions[key] = self._exceptions.get(key, 0) + 1

    def exception_rows(self) -> list[tuple[str, str, int]]:
        rows: list[tuple[str, str, int]] = []
        for (stage, exception_type), count in sorted(self._exceptions.items()):
            rows.append((stage, exception_type, count))
        return rows


def _normalize_windows_path(path_value: str | Path) -> str:
    path = Path(path_value).resolve(strict=False)
    return os.path.normcase(os.path.normpath(str(path)))


def _classify_path_kind(path_value: str | Path) -> str:
    raw = str(path_value).strip()
    lowered = raw.lower()
    if lowered.startswith("http://"):
        return "url_http"
    if lowered.startswith("https://"):
        return "url_https"
    if lowered.startswith("file://"):
        return "url_file"
    if raw:
        drive, _tail = os.path.splitdrive(raw)
        if drive or raw.startswith("\\\\") or raw.startswith("/") or raw.startswith("."):
            return "local"
    return "unknown"


def _coarse_path_kind(kind: str) -> str:
    if kind == "local":
        return "local"
    if kind.startswith("url_"):
        return "url"
    return "unknown"


def _normalize_non_local_path(path_value: str | Path) -> str:
    raw = str(path_value).strip()
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    normalized_path = unicodedata.normalize("NFC", parts.path).rstrip("/")
    return f"{scheme}://{netloc}{normalized_path}".casefold()


def _normalize_web_identity_url(url_value: str) -> str:
    parts = urlsplit(url_value.strip())
    scheme = parts.scheme.lower()
    host = parts.hostname.lower() if parts.hostname else ""
    if parts.port:
        host = f"{host}:{parts.port}"

    path_decoded = unquote(parts.path or "")
    path_nfc = unicodedata.normalize("NFC", path_decoded)
    path_quoted = quote(path_nfc, safe="/-._~")
    if path_quoted and path_quoted != "/":
        path_quoted = path_quoted.rstrip("/")

    return f"{scheme}://{host}{path_quoted}"


def _hash_web_identity_url(url_value: str) -> str:
    normalized = _normalize_web_identity_url(url_value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _is_test_basename(basename: str) -> bool:
    return "【テスト用】" in basename


def _validate_web_identity_mode_for_path(excel_path: Path, web_identity_mode: str) -> None:
    basename = excel_path.name
    if web_identity_mode == "test" and not _is_test_basename(basename):
        raise ValueError("web_identity_mode=test はテスト用Workbook名が必要です")
    if web_identity_mode == "production" and _is_test_basename(basename):
        raise ValueError("web_identity_mode=production ではテスト用Workbook名を使用できません")


def _normalize_hash_value(value) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def _build_web_identity_resolution(value, source: str) -> WebIdentityResolution:
    normalized = _normalize_hash_value(value)
    if not normalized:
        return WebIdentityResolution(hash_value="", source="none", valid=False)
    if not _SHA256_PATTERN.fullmatch(normalized):
        return WebIdentityResolution(hash_value="", source=source, valid=False)
    return WebIdentityResolution(hash_value=normalized, source=source, valid=True)


def _resolve_web_identity_hash(excel_config: dict, web_identity_mode: str) -> WebIdentityResolution:
    if web_identity_mode == "test":
        environment_value = os.getenv(WEB_IDENTITY_ENV_TEST)
        mode_config_value = excel_config.get("web_identity_hash_test")
    elif web_identity_mode == "production":
        environment_value = os.getenv(WEB_IDENTITY_ENV_PRODUCTION)
        mode_config_value = excel_config.get("web_identity_hash_production")
    else:
        return WebIdentityResolution(hash_value="", source="none", valid=False)

    if _normalize_hash_value(environment_value):
        return _build_web_identity_resolution(environment_value, "environment")
    if _normalize_hash_value(mode_config_value):
        return _build_web_identity_resolution(mode_config_value, "mode_config")

    legacy_config_value = excel_config.get("web_identity_hash")
    if _normalize_hash_value(legacy_config_value):
        return _build_web_identity_resolution(legacy_config_value, "legacy_config")
    return WebIdentityResolution(hash_value="", source="none", valid=False)


def _inspect_web_identity_environment(web_identity_mode: str) -> WebIdentityEnvironmentDiagnostics:
    if web_identity_mode == "test":
        env_name = WEB_IDENTITY_ENV_TEST
        env_name_selected = "test"
    elif web_identity_mode == "production":
        env_name = WEB_IDENTITY_ENV_PRODUCTION
        env_name_selected = "production"
    else:
        return WebIdentityEnvironmentDiagnostics(web_identity_mode, "none", False, 0, False, False)

    raw_value = os.getenv(env_name)
    normalized = _normalize_hash_value(raw_value)
    return WebIdentityEnvironmentDiagnostics(
        selected_mode=web_identity_mode,
        env_name_selected=env_name_selected,
        env_present=raw_value is not None,
        env_value_length=len(normalized),
        env_length_valid=len(normalized) == 64,
        env_hex_valid=bool(normalized) and bool(re.fullmatch(r"[0-9a-f]+", normalized)),
    )


def _extract_name_and_extension(path_value: str | Path, kind: str) -> tuple[str, str]:
    raw = str(path_value).strip()
    if kind.startswith("url_"):
        candidate = unquote(urlsplit(raw).path)
        candidate = unicodedata.normalize("NFC", candidate)
    else:
        candidate = raw

    basename = os.path.basename(candidate)
    extension = os.path.splitext(basename)[1]
    return basename.casefold(), extension.casefold()


def _extract_parent(path_value: str | Path, kind: str) -> str:
    raw = str(path_value).strip()
    if kind.startswith("url_"):
        parts = urlsplit(raw)
        parent = os.path.dirname(parts.path.rstrip("/"))
        return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parent}".casefold()

    normalized_local = _normalize_windows_path(raw)
    return os.path.dirname(normalized_local)


def _try_samefile(local_a: str, local_b: str) -> bool | str:
    try:
        return bool(os.path.samefile(local_a, local_b))
    except (FileNotFoundError, OSError, PermissionError):
        return "unavailable"


def _evaluate_path_match(
    configured_path_raw: str,
    configured_path_normalized: str,
    workbook_fullname_raw: str,
    diag: _DetectionDiagnostics,
) -> str:
    c = diag.counters

    configured_kind_detailed = _classify_path_kind(configured_path_raw)
    workbook_kind_detailed = _classify_path_kind(workbook_fullname_raw)
    configured_kind = _coarse_path_kind(configured_kind_detailed)
    workbook_kind = _coarse_path_kind(workbook_kind_detailed)

    c.compared_workbook_count += 1

    if c.configured_path_kind == "unknown":
        c.configured_path_kind = configured_kind

    if c.workbook_path_kind == "unknown":
        c.workbook_path_kind = workbook_kind
    elif c.workbook_path_kind != workbook_kind:
        c.workbook_path_kind = "unknown"

    configured_exists = False
    if configured_kind_detailed == "local":
        try:
            configured_exists = os.path.exists(configured_path_raw)
        except Exception:
            configured_exists = False

    workbook_exists = False
    if workbook_kind_detailed == "local":
        try:
            workbook_exists = os.path.exists(workbook_fullname_raw)
        except Exception:
            workbook_exists = False

    c.configured_path_exists = c.configured_path_exists or configured_exists
    c.workbook_path_exists = c.workbook_path_exists or workbook_exists

    configured_basename, configured_ext = _extract_name_and_extension(configured_path_raw, configured_kind_detailed)
    workbook_basename, workbook_ext = _extract_name_and_extension(workbook_fullname_raw, workbook_kind_detailed)
    basename_equal = configured_basename == workbook_basename and configured_basename != ""
    extension_equal = configured_ext == workbook_ext and configured_ext != ""

    if basename_equal:
        c.basename_equal_count += 1
        c.basename_equal = True
    if extension_equal:
        c.extension_equal = True

    if configured_kind == "local" and workbook_kind == "local":
        c.local_local_count += 1
    elif {configured_kind, workbook_kind} == {"local", "url"}:
        c.local_url_count += 1

    normalized_equal = False
    unicode_normalized_equal = False

    if {configured_kind, workbook_kind} != {"local", "url"}:
        if workbook_kind_detailed == "local":
            try:
                workbook_normalized = _normalize_windows_path(workbook_fullname_raw)
                c.normalized_path_success_count += 1
            except Exception as exc:
                diag.record_exception("normalize_path", exc)
                return "none"
        elif workbook_kind_detailed.startswith("url_"):
            workbook_normalized = _normalize_non_local_path(workbook_fullname_raw)
            c.normalized_path_success_count += 1
        else:
            workbook_normalized = os.path.normcase(os.path.normpath(str(workbook_fullname_raw))).casefold()
            c.normalized_path_success_count += 1

        configured_normalized = configured_path_normalized
        if configured_kind_detailed.startswith("url_"):
            configured_normalized = _normalize_non_local_path(configured_path_raw)

        normalized_equal = configured_normalized == workbook_normalized
        if normalized_equal:
            c.normalized_equal_count += 1
            c.normalized_equal = True
            c.exact_path_match_count += 1

        configured_nfc = unicodedata.normalize("NFC", configured_normalized)
        workbook_nfc = unicodedata.normalize("NFC", workbook_normalized)
        unicode_normalized_equal = configured_nfc == workbook_nfc
        if unicode_normalized_equal:
            c.unicode_normalized_equal_count += 1
            c.unicode_normalized_equal = True

    configured_parent = _extract_parent(configured_path_raw, configured_kind_detailed)
    workbook_parent = _extract_parent(workbook_fullname_raw, workbook_kind_detailed)
    parent_equal = configured_parent == workbook_parent and configured_parent != ""
    if parent_equal:
        c.parent_equal = True

    samefile_state: bool | str = False
    if configured_kind_detailed == "local" and workbook_kind_detailed == "local" and configured_exists and workbook_exists:
        samefile_state = _try_samefile(configured_path_raw, workbook_fullname_raw)
        if samefile_state is True:
            c.samefile_equal_count += 1
            c.samefile_equal = True
        elif samefile_state == "unavailable":
            c.samefile_unavailable_count += 1
            if c.samefile_equal is not True:
                c.samefile_equal = "unavailable"

    if normalized_equal:
        return "normalized"
    if unicode_normalized_equal:
        return "unicode_normalized"
    if samefile_state is True:
        return "samefile"
    return "none"


def _init_path_diagnostics(configured_path_raw: str, configured_path_normalized: str, diag: _DetectionDiagnostics) -> None:
    kind_detailed = _classify_path_kind(configured_path_raw)
    diag.counters.configured_path_kind = _coarse_path_kind(kind_detailed)
    if kind_detailed == "local":
        try:
            diag.counters.configured_path_exists = os.path.exists(configured_path_raw)
        except Exception:
            diag.counters.configured_path_exists = False

    if kind_detailed.startswith("url_"):
        _ = _normalize_non_local_path(configured_path_raw)
    else:
        _ = configured_path_normalized


def _evaluate_web_identity_candidate(
    configured_path_raw: str,
    workbook_fullname_raw: str,
    configured_web_identity_hash: str,
    diag: _DetectionDiagnostics,
) -> tuple[bool, str | None]:
    c = diag.counters
    configured_kind = _classify_path_kind(configured_path_raw)
    workbook_kind = _classify_path_kind(workbook_fullname_raw)

    if configured_kind != "local":
        return False, None
    if not workbook_kind.startswith("url_"):
        return False, None

    configured_basename, configured_ext = _extract_name_and_extension(configured_path_raw, configured_kind)
    workbook_basename, workbook_ext = _extract_name_and_extension(workbook_fullname_raw, workbook_kind)
    if configured_basename == "" or configured_ext == "":
        return False, None
    if configured_basename != workbook_basename:
        return False, None
    if configured_ext != workbook_ext:
        return False, None

    c.web_candidate_count += 1

    try:
        workbook_hash = _hash_web_identity_url(workbook_fullname_raw)
        c.candidate_hash_generated_count += 1
    except Exception as exc:
        diag.record_exception("web_identity_hash", exc)
        return False, None

    if configured_web_identity_hash and workbook_hash == configured_web_identity_hash.lower():
        c.internal_hash_equal_count += 1
        c.web_identity_match_count += 1
        return True, workbook_hash

    return False, workbook_hash


def _safe_get_attr(obj, attr_name: str, diag: _DetectionDiagnostics, stage: str):
    try:
        return getattr(obj, attr_name), True
    except Exception as exc:
        diag.record_exception(stage, exc)
        return None, False


def _is_application_object(obj, diag: _DetectionDiagnostics) -> bool:
    _value, ok = _safe_get_attr(obj, "Workbooks", diag, "application_candidate")
    return bool(ok)


def _is_workbook_object(obj, diag: _DetectionDiagnostics) -> bool:
    _fullname, fullname_ok = _safe_get_attr(obj, "FullName", diag, "workbook_candidate")
    _application, application_ok = _safe_get_attr(obj, "Application", diag, "workbook_candidate")
    return bool(fullname_ok and application_ok)


def _get_active_excel_application():
    import win32com.client

    try:
        return win32com.client.GetActiveObject("Excel.Application")
    except Exception:
        return None


def _iter_top_level_excel_windows(diag: _DetectionDiagnostics) -> list[int]:
    import win32gui

    hwnds: list[int] = []

    def _callback(hwnd, _lparam):
        try:
            if win32gui.GetClassName(hwnd) == WINDOW_CLASS_XLMAIN:
                hwnds.append(hwnd)
        except Exception as exc:
            diag.record_exception("enum_xlmain", exc)
        return True

    try:
        win32gui.EnumWindows(_callback, 0)
    except Exception as exc:
        diag.record_exception("enum_xlmain", exc)
    return hwnds


def _iter_child_windows_by_class(parent_hwnd: int, class_name: str, diag: _DetectionDiagnostics) -> list[int]:
    import win32gui

    children: list[int] = []
    try:
        child = win32gui.FindWindowEx(parent_hwnd, 0, class_name, None)
        while child:
            children.append(child)
            child = win32gui.FindWindowEx(parent_hwnd, child, class_name, None)
    except Exception as exc:
        diag.record_exception("enum_child_windows", exc)
    return children


def _get_dispatch_from_excel7_window(hwnd: int, diag: _DetectionDiagnostics):
    import pythoncom
    import win32com.client

    oleacc = ctypes.OleDLL("oleacc")
    try:
        iid_dispatch = _build_iid_idispatch_guid()
    except Exception as exc:
        diag.record_exception("build_iid_idispatch", exc)
        return None

    ppv_object = ctypes.c_void_p()

    try:
        accessible = oleacc.AccessibleObjectFromWindow
        accessible.restype = ctypes.c_long
        accessible.argtypes = [
            wintypes.HWND,
            ctypes.c_long,
            ctypes.POINTER(GUID),
            ctypes.POINTER(ctypes.c_void_p),
        ]
    except Exception as exc:
        diag.record_exception("configure_accessible_object_from_window", exc)
        return None

    try:
        hr = accessible(
            wintypes.HWND(hwnd),
            ctypes.c_long(-16),
            ctypes.byref(iid_dispatch),
            ctypes.byref(ppv_object),
        )
    except Exception as exc:
        diag.record_exception("call_accessible_object_from_window", exc)
        return None

    if hr != 0:
        diag.record_exception("accessible_object_hresult", RuntimeError("hresult_failure"))
        return None

    if not ppv_object.value:
        diag.counters.native_dispatch_pointer_null_count += 1
        return None

    diag.counters.native_dispatch_pointer_success_count += 1
    diag.counters.accessible_object_success_count += 1

    if not hasattr(pythoncom, "ObjectFromAddress"):
        diag.counters.native_dispatch_wrap_failure_count += 1
        diag.record_exception("native_dispatch_wrap", RuntimeError("ObjectFromAddressUnavailable"))
        return None

    try:
        pointer_value = int(ppv_object.value)
        raw_dispatch = pythoncom.ObjectFromAddress(pointer_value, pythoncom.IID_IDispatch)
    except Exception as exc:
        diag.counters.native_dispatch_wrap_failure_count += 1
        diag.record_exception("native_dispatch_wrap", exc)
        return None

    try:
        native = win32com.client.Dispatch(raw_dispatch)
        diag.counters.native_dispatch_wrap_success_count += 1
        diag.counters.dispatch_wrap_success_count += 1
        return native
    except Exception as exc:
        diag.counters.native_dispatch_wrap_failure_count += 1
        diag.record_exception("native_dispatch_wrap", exc)
        return None


def _get_application_from_native_object(native_obj, diag: _DetectionDiagnostics):
    if native_obj is None:
        diag.counters.unknown_object_count += 1
        diag.counters.native_application_failure_count += 1
        return None, "unknown"

    if _is_application_object(native_obj, diag):
        diag.counters.application_object_count += 1
        return native_obj, "application"

    if _is_workbook_object(native_obj, diag):
        diag.counters.workbook_object_count += 1
    else:
        _tmp, app_visible = _safe_get_attr(native_obj, "Application", diag, "native_direct_application")
        if app_visible:
            diag.counters.window_object_count += 1
        else:
            diag.counters.unknown_object_count += 1

    direct_application, direct_ok = _safe_get_attr(native_obj, "Application", diag, "native_direct_application")
    if direct_ok and direct_application is not None and _is_application_object(direct_application, diag):
        diag.counters.application_property_success_count += 1
        diag.counters.direct_application_success_count += 1
        return direct_application, "native"

    parent, parent_ok = _safe_get_attr(native_obj, "Parent", diag, "native_parent")
    if parent_ok and parent is not None:
        parent_application, parent_application_ok = _safe_get_attr(parent, "Application", diag, "native_parent_application")
        if parent_application_ok and parent_application is not None and _is_application_object(parent_application, diag):
            diag.counters.application_property_success_count += 1
            diag.counters.parent_application_success_count += 1
            return parent_application, "native"

        grandparent, grandparent_ok = _safe_get_attr(parent, "Parent", diag, "native_grandparent")
        if grandparent_ok and grandparent is not None:
            grandparent_application, grandparent_application_ok = _safe_get_attr(grandparent, "Application", diag, "native_grandparent_application")
            if grandparent_application_ok and grandparent_application is not None and _is_application_object(grandparent_application, diag):
                diag.counters.application_property_success_count += 1
                diag.counters.grandparent_application_success_count += 1
                return grandparent_application, "native"

    diag.counters.native_application_failure_count += 1
    return None, "unknown"


def _get_application_hwnd(app) -> int | None:
    try:
        hwnd = int(app.Hwnd)
    except Exception:
        return None

    if hwnd == 0:
        return None

    return hwnd


def _collect_applications_from_windows(diag: _DetectionDiagnostics) -> tuple[list[tuple[object, str]], int]:
    apps: list[tuple[object, str]] = []
    xlmain_hwnds = _iter_top_level_excel_windows(diag)
    diag.counters.xlmain_count = len(xlmain_hwnds)

    for xlmain_hwnd in xlmain_hwnds:
        xldesk_hwnds = _iter_child_windows_by_class(xlmain_hwnd, WINDOW_CLASS_XLDESK, diag)
        diag.counters.xldesk_count += len(xldesk_hwnds)
        for xldesk_hwnd in xldesk_hwnds:
            excel7_hwnds = _iter_child_windows_by_class(xldesk_hwnd, WINDOW_CLASS_EXCEL7, diag)
            diag.counters.excel7_count += len(excel7_hwnds)
            for excel7_hwnd in excel7_hwnds:
                diag.counters.accessible_object_call_count += 1
                native_obj = _get_dispatch_from_excel7_window(excel7_hwnd, diag)
                app, _kind = _get_application_from_native_object(native_obj, diag)
                if app is None:
                    continue

                _hwnd = _get_application_hwnd(app)
                if _hwnd is None:
                    diag.counters.hwnd_unavailable_count += 1

                try:
                    _ = int(app.Workbooks.Count)
                    diag.counters.native_workbooks_count_success += 1
                except Exception as exc:
                    diag.counters.native_workbooks_count_failure += 1
                    diag.record_exception("native_workbooks_count", exc)

                apps.append((app, "accessible_object_from_window"))

    return apps, len(xlmain_hwnds)


def _iter_rot_running_objects(diag: _DetectionDiagnostics) -> list[object]:
    import pythoncom

    objects: list[object] = []

    try:
        rot = pythoncom.GetRunningObjectTable()
        bind_ctx = pythoncom.CreateBindCtx(0)
        enum_running = rot.EnumRunning()
    except Exception as exc:
        diag.record_exception("rot_initialize", exc)
        return objects

    _ = bind_ctx

    while True:
        try:
            monikers = enum_running.Next(1)
        except Exception as exc:
            diag.record_exception("rot_enum_next", exc)
            break

        if not monikers:
            break

        diag.counters.rot_moniker_count += 1

        try:
            obj = rot.GetObject(monikers[0])
            diag.counters.rot_get_object_success_count += 1
            objects.append(obj)
        except Exception as exc:
            diag.counters.rot_get_object_failure_count += 1
            diag.record_exception("rot_get_object", exc)

    return objects


def _collect_workbooks_from_rot(
    target_path: str,
    diag: _DetectionDiagnostics,
) -> tuple[object | None, object | None, list[tuple[object, str]], int]:
    app_candidates: list[tuple[object, str]] = []
    seen_workbook_ids: set[int] = set()

    for obj in _iter_rot_running_objects(diag):
        application, application_ok = _safe_get_attr(obj, "Application", diag, "rot_application_property")
        _save_method, save_ok = _safe_get_attr(obj, "Save", diag, "rot_save_property")
        _close_method, close_ok = _safe_get_attr(obj, "Close", diag, "rot_close_property")

        if not (application_ok and save_ok and close_ok):
            continue

        diag.counters.rot_workbook_candidate_count += 1

        fullname, fullname_ok = _safe_get_attr(obj, "FullName", diag, "rot_fullname")
        if not fullname_ok or fullname is None:
            diag.counters.rot_fullname_failure_count += 1
            continue

        diag.counters.rot_fullname_success_count += 1

        workbook_id = id(obj)
        if workbook_id in seen_workbook_ids:
            diag.counters.rot_duplicate_candidate_count += 1
            continue
        seen_workbook_ids.add(workbook_id)

        if application is not None:
            app_candidates.append((application, "rot_workbook"))

        try:
            normalized = _normalize_windows_path(fullname)
            diag.counters.rot_normalized_path_success_count += 1
        except Exception as exc:
            diag.record_exception("rot_normalize_path", exc)
            continue

        if normalized == target_path:
            diag.counters.rot_exact_path_match_count += 1
            return application, obj, app_candidates, len(seen_workbook_ids)

    return None, None, app_candidates, len(seen_workbook_ids)


def _dedupe_applications(candidates: list[tuple[object, str]], diag: _DetectionDiagnostics) -> list[tuple[object, str]]:
    deduped: list[tuple[object, str]] = []
    seen_keys: set[tuple[str, int]] = set()

    diag.counters.application_count_before_dedupe = len(candidates)

    for app, source in candidates:
        hwnd = _get_application_hwnd(app)
        if hwnd is None:
            key = ("obj", id(app))
        else:
            key = ("hwnd", hwnd)

        if key in seen_keys:
            continue

        seen_keys.add(key)
        deduped.append((app, source))

    diag.counters.application_count_after_dedupe = len(deduped)
    return deduped


def _build_detection_result(
    diag: _DetectionDiagnostics,
    *,
    observed_workbook_count: int,
    detection_method: str,
    application,
    workbook,
) -> WorkbookDetection:
    c = diag.counters
    return WorkbookDetection(
        xlmain_count=c.xlmain_count,
        xldesk_count=c.xldesk_count,
        excel7_count=c.excel7_count,
        accessible_object_call_count=c.accessible_object_call_count,
        accessible_object_success_count=c.accessible_object_success_count,
        dispatch_wrap_success_count=c.dispatch_wrap_success_count,
        window_object_count=c.window_object_count,
        workbook_object_count=c.workbook_object_count,
        application_object_count=c.application_object_count,
        unknown_object_count=c.unknown_object_count,
        application_property_success_count=c.application_property_success_count,
        application_count_before_dedupe=c.application_count_before_dedupe,
        application_count_after_dedupe=c.application_count_after_dedupe,
        application_workbooks_count_success=c.application_workbooks_count_success,
        application_workbooks_count_failure=c.application_workbooks_count_failure,
        workbook_item_success_count=c.workbook_item_success_count,
        workbook_item_failure_count=c.workbook_item_failure_count,
        workbook_fullname_success_count=c.workbook_fullname_success_count,
        workbook_fullname_failure_count=c.workbook_fullname_failure_count,
        normalized_path_success_count=c.normalized_path_success_count,
        exact_path_match_count=c.exact_path_match_count,
        hwnd_unavailable_count=c.hwnd_unavailable_count,
        rot_moniker_count=c.rot_moniker_count,
        rot_get_object_success_count=c.rot_get_object_success_count,
        rot_get_object_failure_count=c.rot_get_object_failure_count,
        rot_workbook_candidate_count=c.rot_workbook_candidate_count,
        rot_fullname_success_count=c.rot_fullname_success_count,
        rot_fullname_failure_count=c.rot_fullname_failure_count,
        rot_normalized_path_success_count=c.rot_normalized_path_success_count,
        rot_exact_path_match_count=c.rot_exact_path_match_count,
        rot_duplicate_candidate_count=c.rot_duplicate_candidate_count,
        native_dispatch_pointer_success_count=c.native_dispatch_pointer_success_count,
        native_dispatch_pointer_null_count=c.native_dispatch_pointer_null_count,
        native_dispatch_wrap_success_count=c.native_dispatch_wrap_success_count,
        native_dispatch_wrap_failure_count=c.native_dispatch_wrap_failure_count,
        direct_application_success_count=c.direct_application_success_count,
        parent_application_success_count=c.parent_application_success_count,
        grandparent_application_success_count=c.grandparent_application_success_count,
        native_application_failure_count=c.native_application_failure_count,
        native_workbooks_count_success=c.native_workbooks_count_success,
        native_workbooks_count_failure=c.native_workbooks_count_failure,
        observed_excel_window_count=c.xlmain_count,
        observed_application_count=c.application_count_after_dedupe,
        observed_workbook_count=observed_workbook_count,
        matched_workbook_count=c.matched_workbook_count,
        detection_method=detection_method,
        configured_path_kind=c.configured_path_kind,
        workbook_path_kind=c.workbook_path_kind,
        configured_path_exists=c.configured_path_exists,
        workbook_path_exists=c.workbook_path_exists,
        extension_equal=c.extension_equal,
        basename_equal=c.basename_equal,
        normalized_equal=c.normalized_equal,
        unicode_normalized_equal=c.unicode_normalized_equal,
        samefile_equal=c.samefile_equal,
        parent_equal=c.parent_equal,
        target_match_method=c.target_match_method,
        compared_workbook_count=c.compared_workbook_count,
        local_local_count=c.local_local_count,
        local_url_count=c.local_url_count,
        basename_equal_count=c.basename_equal_count,
        normalized_equal_count=c.normalized_equal_count,
        unicode_normalized_equal_count=c.unicode_normalized_equal_count,
        samefile_equal_count=c.samefile_equal_count,
        samefile_unavailable_count=c.samefile_unavailable_count,
        web_identity_configured=c.web_identity_configured,
        web_identity_source=c.web_identity_source,
        web_identity_valid=c.web_identity_valid,
        web_candidate_count=c.web_candidate_count,
        candidate_hash_generated_count=c.candidate_hash_generated_count,
        internal_hash_equal_count=c.internal_hash_equal_count,
        web_identity_match_count=c.web_identity_match_count,
        capture_succeeded=c.capture_succeeded,
        detection_exceptions=diag.exception_rows(),
        application=application,
        workbook=workbook,
        captured_web_identity_hash=None,
    )


def detect_target_workbook(
    excel_path: Path,
    *,
    configured_web_identity_hash: str = "",
    web_identity_source: str = "none",
    web_identity_valid: bool | None = None,
    capture_web_identity: bool = False,
    web_identity_mode: str = "production",
    timeout_sec: float = 15.0,
    interval_sec: float = 0.5,
) -> WorkbookDetection:
    if os.name != "nt":
        raise RuntimeError("Excel COM操作はWindowsでのみ利用できます")

    normalized_web_identity_hash = _normalize_hash_value(configured_web_identity_hash)
    if web_identity_valid is None:
        web_identity_valid = bool(_SHA256_PATTERN.fullmatch(normalized_web_identity_hash))
    if not web_identity_valid:
        normalized_web_identity_hash = ""

    if capture_web_identity or web_identity_valid:
        _validate_web_identity_mode_for_path(excel_path, web_identity_mode)

    target_path = _normalize_windows_path(excel_path)
    target_path_raw = str(excel_path)
    deadline = time.monotonic() + timeout_sec

    last_snapshot = _build_detection_result(
        _DetectionDiagnostics(),
        observed_workbook_count=0,
        detection_method="none",
        application=None,
        workbook=None,
    )

    while time.monotonic() < deadline:
        diag = _DetectionDiagnostics()
        _init_path_diagnostics(target_path_raw, target_path, diag)
        diag.counters.web_identity_configured = bool(web_identity_valid)
        diag.counters.web_identity_source = web_identity_source if web_identity_source in {
            "environment",
            "mode_config",
            "legacy_config",
            "none",
        } else "none"
        diag.counters.web_identity_valid = bool(web_identity_valid)

        # 1) ROT diagnostics-first path.
        rot_app, rot_workbook, rot_app_candidates, rot_workbook_count = _collect_workbooks_from_rot(target_path, diag)
        if rot_workbook is not None and rot_app is not None:
            return _build_detection_result(
                diag,
                observed_workbook_count=rot_workbook_count,
                detection_method="rot_workbook",
                application=rot_app,
                workbook=rot_workbook,
            )

        # 2) Native object path via AccessibleObjectFromWindow.
        window_candidates, _window_count = _collect_applications_from_windows(diag)

        candidates: list[tuple[object, str]] = []
        candidates.extend(rot_app_candidates)
        candidates.extend(window_candidates)

        # 3) Fallback active application.
        try:
            active_app = _get_active_excel_application()
        except Exception as exc:
            diag.record_exception("get_active_object", exc)
            active_app = None

        if active_app is not None:
            candidates.append((active_app, "get_active_object"))

        apps = _dedupe_applications(candidates, diag)
        workbook_count = 0

        method_rank = {"normalized": 0, "unicode_normalized": 1, "samefile": 2, "web_identity": 3}
        matches: list[tuple[int, object, object, str, str]] = []
        capture_candidates: list[str] = []

        for app, source in apps:
            try:
                count = int(app.Workbooks.Count)
                diag.counters.application_workbooks_count_success += 1
            except Exception as exc:
                diag.counters.application_workbooks_count_failure += 1
                diag.record_exception("application_workbooks_count", exc)
                continue

            if count <= 0:
                continue

            workbook_count += count
            for index in range(1, count + 1):
                try:
                    wb = app.Workbooks.Item(index)
                    diag.counters.workbook_item_success_count += 1
                except Exception as exc:
                    diag.counters.workbook_item_failure_count += 1
                    diag.record_exception("workbook_item", exc)
                    continue

                try:
                    wb_fullname = wb.FullName
                    diag.counters.workbook_fullname_success_count += 1
                except Exception as exc:
                    diag.counters.workbook_fullname_failure_count += 1
                    diag.record_exception("workbook_fullname", exc)
                    continue

                match_method = _evaluate_path_match(target_path_raw, target_path, str(wb_fullname), diag)
                if match_method in method_rank:
                    rank = method_rank[match_method]
                    matches.append((rank, wb, app, source, match_method))

                web_identity_match, web_hash = _evaluate_web_identity_candidate(
                    target_path_raw,
                    str(wb_fullname),
                    normalized_web_identity_hash,
                    diag,
                )
                if web_hash is not None:
                    capture_candidates.append(web_hash)

                if web_identity_match:
                    matches.append((method_rank["web_identity"], wb, app, source, "web_identity"))

        if capture_web_identity:
            unique_capture_candidates = sorted(set(capture_candidates))
            result = _build_detection_result(
                diag,
                observed_workbook_count=workbook_count,
                detection_method="capture_web_identity",
                application=None,
                workbook=None,
            )
            if len(unique_capture_candidates) == 1:
                diag.counters.capture_succeeded = True
                result.capture_succeeded = True
                result.captured_web_identity_hash = unique_capture_candidates[0]
                return result

            result.capture_succeeded = False
            result.captured_web_identity_hash = None
            return result

        if matches:
            diag.counters.matched_workbook_count = len(matches)
            matches.sort(key=lambda row: row[0])
            best_rank = matches[0][0]
            best_method = matches[0][4]
            best_matches = [row for row in matches if row[0] == best_rank]
            diag.counters.target_match_method = best_method

            if len(best_matches) == 1:
                _rank, best_wb, best_app, best_source, _method = best_matches[0]
                return _build_detection_result(
                    diag,
                    observed_workbook_count=workbook_count,
                    detection_method=best_source,
                    application=best_app,
                    workbook=best_wb,
                )

            return _build_detection_result(
                diag,
                observed_workbook_count=workbook_count,
                detection_method="ambiguous_match",
                application=None,
                workbook=None,
            )

        last_snapshot = _build_detection_result(
            diag,
            observed_workbook_count=workbook_count,
            detection_method="combined_scan",
            application=None,
            workbook=None,
        )
        time.sleep(interval_sec)

    return last_snapshot


def _save_and_close_workbook(application, workbook) -> None:
    if bool(workbook.ReadOnly):
        raise ReadOnlyWorkbookError("対象WorkbookがReadOnlyのため保存できません")

    original_display_alerts = None
    display_alerts_changed = False

    try:
        try:
            original_display_alerts = application.DisplayAlerts
            application.DisplayAlerts = False
            display_alerts_changed = True
        except Exception:
            display_alerts_changed = False

        workbook.Save()
        workbook.Close(SaveChanges=False)
    except Exception as exc:
        raise SaveCloseWorkbookError("対象Workbookの保存または終了に失敗しました") from exc
    finally:
        if display_alerts_changed:
            try:
                application.DisplayAlerts = original_display_alerts
            except Exception:
                pass


def save_and_close_target_workbook(
    excel_path: Path,
    logger,
    *,
    configured_web_identity_hash: str = "",
    web_identity_source: str = "none",
    web_identity_valid: bool | None = None,
    web_identity_mode: str = "production",
) -> bool:
    detection = detect_target_workbook(
        excel_path,
        configured_web_identity_hash=configured_web_identity_hash,
        web_identity_source=web_identity_source,
        web_identity_valid=web_identity_valid,
        web_identity_mode=web_identity_mode,
        timeout_sec=15.0,
        interval_sec=0.5,
    )
    if detection.matched_workbook_count > 1:
        raise SaveCloseWorkbookError("対象Workbookの一致候補が複数のため保存・終了を中止しました")

    if detection.workbook is None or detection.application is None:
        logger.info("対象Excelが開いているか: False")
        return False

    logger.info("対象Excelが開いているか: True")
    logger.info("保存開始")
    _save_and_close_workbook(detection.application, detection.workbook)
    logger.info("保存完了")
    logger.info("対象ブック終了開始")
    logger.info("対象ブック終了完了")
    return True
