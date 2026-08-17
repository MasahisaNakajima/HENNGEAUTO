from __future__ import annotations

import ctypes
import sys
import types
import unicodedata
from pathlib import Path

import pytest

import app.excel_session as mod


class DummyLogger:
    def __init__(self):
        self.info_messages = []

    def info(self, message):
        self.info_messages.append(message)


class FakeWorkbook:
    def __init__(self, fullname: str, *, application=None, read_only=False, save_error: Exception | None = None, close_error: Exception | None = None):
        self.FullName = fullname
        self.Application = application
        self.ReadOnly = read_only
        self._save_error = save_error
        self._close_error = close_error
        self.save_calls = 0
        self.close_calls = []

    def Save(self):
        self.save_calls += 1
        if self._save_error is not None:
            raise self._save_error

    def Close(self, SaveChanges=False):
        self.close_calls.append(SaveChanges)
        if self._close_error is not None:
            raise self._close_error


class FakeWorkbooks:
    def __init__(self, items, *, count_error: Exception | None = None, item_errors: dict[int, Exception] | None = None):
        self._items = list(items)
        self._count_error = count_error
        self._item_errors = item_errors or {}
        self.item_calls = 0

    @property
    def Count(self):
        if self._count_error is not None:
            raise self._count_error
        return len(self._items)

    def Item(self, index):
        self.item_calls += 1
        if index in self._item_errors:
            raise self._item_errors[index]
        return self._items[index - 1]


class FakeExcelApp:
    def __init__(self, hwnd, workbooks):
        self.Hwnd = hwnd
        self.Workbooks = workbooks
        self.DisplayAlerts = True
        self.quit_calls = 0

    def Quit(self):
        self.quit_calls += 1


class RaisingHwndApp:
    def __init__(self, workbooks):
        self.Workbooks = workbooks

    @property
    def Hwnd(self):
        raise RuntimeError("no hwnd")


def _install_base(monkeypatch):
    monkeypatch.setattr(mod.os, "name", "nt", raising=False)


def _install_native_modules(monkeypatch, *, hresult=0, pointer_value=1234, wrapped_object=None):
    wrapped = wrapped_object if wrapped_object is not None else object()

    class FakeAccessibleCallable:
        def __init__(self):
            self.restype = None
            self.argtypes = None

        def __call__(self, _hwnd, _objid, _riid, ppv_object):
            ptr = ctypes.cast(ppv_object, ctypes.POINTER(ctypes.c_void_p))
            ptr.contents.value = pointer_value
            return hresult

    class FakeOleAcc:
        def __init__(self):
            self.AccessibleObjectFromWindow = FakeAccessibleCallable()

    fake_pythoncom = types.SimpleNamespace(IID_IDispatch=object(), ObjectFromAddress=lambda _addr, _iid: object())
    fake_client = types.SimpleNamespace(Dispatch=lambda _raw: wrapped)
    fake_win32com = types.SimpleNamespace(client=fake_client)

    monkeypatch.setattr(ctypes, "OleDLL", lambda _name: FakeOleAcc())
    monkeypatch.setitem(sys.modules, "pythoncom", fake_pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", fake_win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", fake_client)

    return wrapped


def test_iid_idispatch_is_not_indexed(monkeypatch):
    _install_base(monkeypatch)

    class NonIndexable:
        def __getitem__(self, _index):
            raise AssertionError("IID_IDispatch must not be indexed")

    class FakeAccessibleCallable:
        def __init__(self):
            self.restype = None
            self.argtypes = None

        def __call__(self, _hwnd, _objid, _riid, ppv_object):
            ptr = ctypes.cast(ppv_object, ctypes.POINTER(ctypes.c_void_p))
            ptr.contents.value = 0
            return 0

    class FakeOleAcc:
        def __init__(self):
            self.AccessibleObjectFromWindow = FakeAccessibleCallable()

    fake_pythoncom = types.SimpleNamespace(IID_IDispatch=NonIndexable(), ObjectFromAddress=lambda _addr, _iid: object())
    fake_client = types.SimpleNamespace(Dispatch=lambda _raw: object())
    fake_win32com = types.SimpleNamespace(client=fake_client)

    monkeypatch.setattr(ctypes, "OleDLL", lambda _name: FakeOleAcc())
    monkeypatch.setitem(sys.modules, "pythoncom", fake_pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", fake_win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", fake_client)

    diag = mod._DetectionDiagnostics()
    _ = mod._get_dispatch_from_excel7_window(100, diag)

    assert diag.counters.native_dispatch_pointer_null_count == 1


def test_build_iid_idispatch_guid_has_expected_fields():
    guid = mod._build_iid_idispatch_guid()

    assert guid.Data1 == 0x00020400
    assert guid.Data2 == 0x0000
    assert guid.Data3 == 0x0000
    assert tuple(guid.Data4) == (0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46)


def test_accessible_object_returns_native_dispatch(monkeypatch):
    _install_base(monkeypatch)
    expected = _install_native_modules(monkeypatch, hresult=0, pointer_value=5678)
    diag = mod._DetectionDiagnostics()

    native = mod._get_dispatch_from_excel7_window(100, diag)

    assert native is expected
    assert diag.counters.native_dispatch_pointer_success_count == 1
    assert diag.counters.native_dispatch_wrap_success_count == 1


def test_accessible_object_uses_signed_objid_and_hwnd(monkeypatch):
    _install_base(monkeypatch)
    call_args = {}

    class FakeAccessibleCallable:
        def __init__(self):
            self.restype = None
            self.argtypes = None

        def __call__(self, hwnd, objid, _riid, ppv_object):
            call_args["hwnd"] = hwnd
            call_args["objid"] = objid
            ptr = ctypes.cast(ppv_object, ctypes.POINTER(ctypes.c_void_p))
            ptr.contents.value = 0
            return 0

    class FakeOleAcc:
        def __init__(self):
            self.AccessibleObjectFromWindow = FakeAccessibleCallable()

    fake_pythoncom = types.SimpleNamespace(IID_IDispatch=object(), ObjectFromAddress=lambda _addr, _iid: object())
    fake_client = types.SimpleNamespace(Dispatch=lambda _raw: object())
    fake_win32com = types.SimpleNamespace(client=fake_client)

    monkeypatch.setattr(ctypes, "OleDLL", lambda _name: FakeOleAcc())
    monkeypatch.setitem(sys.modules, "pythoncom", fake_pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", fake_win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", fake_client)

    diag = mod._DetectionDiagnostics()
    _ = mod._get_dispatch_from_excel7_window(123, diag)

    objid_value = call_args["objid"]
    try:
        signed_objid = int(objid_value)
    except Exception:
        raw = bytes(objid_value)
        signed_objid = int.from_bytes(raw, byteorder="little", signed=True)

    assert signed_objid == -16
    hwnd_value = call_args["hwnd"]
    try:
        numeric_hwnd = int(hwnd_value)
    except Exception:
        numeric_hwnd = int.from_bytes(bytes(hwnd_value), byteorder="little", signed=False)
    assert numeric_hwnd == 123


def test_build_iid_error_is_classified(monkeypatch):
    _install_base(monkeypatch)
    _install_native_modules(monkeypatch)
    monkeypatch.setattr(mod, "_build_iid_idispatch_guid", lambda: (_ for _ in ()).throw(TypeError("bad iid")))
    diag = mod._DetectionDiagnostics()

    native = mod._get_dispatch_from_excel7_window(100, diag)

    assert native is None
    assert ("build_iid_idispatch", "TypeError", 1) in diag.exception_rows()


def test_accessible_call_typeerror_is_classified(monkeypatch):
    _install_base(monkeypatch)

    class FakeAccessibleCallable:
        def __init__(self):
            self.restype = None
            self.argtypes = None

        def __call__(self, _hwnd, _objid, _riid, _ppv_object):
            raise TypeError("bad call")

    class FakeOleAcc:
        def __init__(self):
            self.AccessibleObjectFromWindow = FakeAccessibleCallable()

    fake_pythoncom = types.SimpleNamespace(IID_IDispatch=object(), ObjectFromAddress=lambda _addr, _iid: object())
    fake_client = types.SimpleNamespace(Dispatch=lambda _raw: object())
    fake_win32com = types.SimpleNamespace(client=fake_client)

    monkeypatch.setattr(ctypes, "OleDLL", lambda _name: FakeOleAcc())
    monkeypatch.setitem(sys.modules, "pythoncom", fake_pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", fake_win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", fake_client)

    diag = mod._DetectionDiagnostics()
    native = mod._get_dispatch_from_excel7_window(100, diag)

    assert native is None
    assert ("call_accessible_object_from_window", "TypeError", 1) in diag.exception_rows()


def test_object_from_address_receives_int_pointer(monkeypatch):
    _install_base(monkeypatch)
    call_record = {"pointer_type": None}

    class FakeAccessibleCallable:
        def __init__(self):
            self.restype = None
            self.argtypes = None

        def __call__(self, _hwnd, _objid, _riid, ppv_object):
            ptr = ctypes.cast(ppv_object, ctypes.POINTER(ctypes.c_void_p))
            ptr.contents.value = 4321
            return 0

    class FakeOleAcc:
        def __init__(self):
            self.AccessibleObjectFromWindow = FakeAccessibleCallable()

    def fake_object_from_address(pointer, _iid):
        call_record["pointer_type"] = type(pointer)
        return object()

    fake_pythoncom = types.SimpleNamespace(IID_IDispatch=object(), ObjectFromAddress=fake_object_from_address)
    fake_client = types.SimpleNamespace(Dispatch=lambda _raw: object())
    fake_win32com = types.SimpleNamespace(client=fake_client)

    monkeypatch.setattr(ctypes, "OleDLL", lambda _name: FakeOleAcc())
    monkeypatch.setitem(sys.modules, "pythoncom", fake_pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", fake_win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", fake_client)

    diag = mod._DetectionDiagnostics()
    _ = mod._get_dispatch_from_excel7_window(100, diag)

    assert call_record["pointer_type"] is int


def test_accessible_object_null_pointer_is_rejected(monkeypatch):
    _install_base(monkeypatch)
    _install_native_modules(monkeypatch, hresult=0, pointer_value=0)
    diag = mod._DetectionDiagnostics()

    native = mod._get_dispatch_from_excel7_window(100, diag)

    assert native is None
    assert diag.counters.native_dispatch_pointer_null_count == 1
    assert diag.counters.native_dispatch_wrap_success_count == 0


def test_accessible_object_hresult_failure_is_not_wrapped(monkeypatch):
    _install_base(monkeypatch)
    _install_native_modules(monkeypatch, hresult=1, pointer_value=9999)
    diag = mod._DetectionDiagnostics()

    native = mod._get_dispatch_from_excel7_window(100, diag)

    assert native is None
    assert diag.counters.native_dispatch_pointer_success_count == 0
    assert diag.counters.native_dispatch_wrap_success_count == 0


def test_direct_application_path(monkeypatch):
    _install_base(monkeypatch)
    app = FakeExcelApp(10, FakeWorkbooks([]))

    class Native:
        def __init__(self, application):
            self.Application = application

    diag = mod._DetectionDiagnostics()
    resolved, _kind = mod._get_application_from_native_object(Native(app), diag)

    assert resolved is app
    assert diag.counters.direct_application_success_count == 1


def test_parent_application_path(monkeypatch):
    _install_base(monkeypatch)
    app = FakeExcelApp(10, FakeWorkbooks([]))

    class Parent:
        def __init__(self, application):
            self.Application = application

    class Native:
        def __init__(self, parent):
            self.Parent = parent

    diag = mod._DetectionDiagnostics()
    resolved, _kind = mod._get_application_from_native_object(Native(Parent(app)), diag)

    assert resolved is app
    assert diag.counters.parent_application_success_count == 1


def test_grandparent_application_path(monkeypatch):
    _install_base(monkeypatch)
    app = FakeExcelApp(10, FakeWorkbooks([]))

    class GrandParent:
        def __init__(self, application):
            self.Application = application

    class Parent:
        def __init__(self, grandparent):
            self.Parent = grandparent

    class Native:
        def __init__(self, parent):
            self.Parent = parent

    diag = mod._DetectionDiagnostics()
    resolved, _kind = mod._get_application_from_native_object(Native(Parent(GrandParent(app))), diag)

    assert resolved is app
    assert diag.counters.grandparent_application_success_count == 1


def test_unreachable_application_is_not_candidate(monkeypatch):
    _install_base(monkeypatch)

    class Native:
        pass

    diag = mod._DetectionDiagnostics()
    resolved, kind = mod._get_application_from_native_object(Native(), diag)

    assert resolved is None
    assert kind == "unknown"
    assert diag.counters.native_application_failure_count >= 1


def test_rot_second_object_target_is_detected(monkeypatch, tmp_path):
    _install_base(monkeypatch)
    target = (tmp_path / "target.xlsm").resolve()
    other = (tmp_path / "other.xlsm").resolve()

    app = FakeExcelApp(100, FakeWorkbooks([]))
    wb1 = FakeWorkbook(str(other), application=app)
    wb2 = FakeWorkbook(str(target), application=app)

    monkeypatch.setattr(mod, "_iter_rot_running_objects", lambda _diag: [wb1, wb2])
    monkeypatch.setattr(mod, "_collect_applications_from_windows", lambda _diag: ([], 0))
    monkeypatch.setattr(mod, "_get_active_excel_application", lambda: None)

    snap = mod.detect_target_workbook(target, timeout_sec=0.1, interval_sec=0.01)

    assert snap.workbook is wb2
    assert snap.application is app
    assert snap.detection_method == "rot_workbook"


def test_fullpath_exact_match_only(monkeypatch, tmp_path):
    _install_base(monkeypatch)
    target = (tmp_path / "A" / "book.xlsm").resolve()
    different = (tmp_path / "B" / "book.xlsm").resolve()
    different.parent.mkdir(parents=True, exist_ok=True)

    app = FakeExcelApp(100, FakeWorkbooks([]))
    wb = FakeWorkbook(str(different), application=app)

    monkeypatch.setattr(mod, "_iter_rot_running_objects", lambda _diag: [wb])
    monkeypatch.setattr(mod, "_collect_applications_from_windows", lambda _diag: ([], 0))
    monkeypatch.setattr(mod, "_get_active_excel_application", lambda: None)

    snap = mod.detect_target_workbook(target, timeout_sec=0.05, interval_sec=0.01)

    assert snap.workbook is None
    assert snap.rot_exact_path_match_count == 0
    assert snap.target_match_method == "none"


def test_normalized_exact_match_detects_target(monkeypatch, tmp_path):
    _install_base(monkeypatch)
    target = (tmp_path / "target.xlsm").resolve()
    target.write_bytes(b"x")

    app = FakeExcelApp(100, FakeWorkbooks([FakeWorkbook(str(target))]))
    monkeypatch.setattr(mod, "_iter_rot_running_objects", lambda _diag: [])
    monkeypatch.setattr(mod, "_collect_applications_from_windows", lambda _diag: ([(app, "accessible_object_from_window")], 1))
    monkeypatch.setattr(mod, "_get_active_excel_application", lambda: None)

    snap = mod.detect_target_workbook(target, timeout_sec=0.1, interval_sec=0.01)

    assert snap.workbook is not None
    assert snap.target_match_method == "normalized"
    assert snap.normalized_equal_count >= 1
    assert snap.matched_workbook_count == 1


def test_unicode_nfc_match_detects_target(monkeypatch, tmp_path):
    _install_base(monkeypatch)
    base = str((tmp_path / "Cafe").resolve())
    decomposed_name = "e\u0301xample.xlsm"
    composed_name = unicodedata.normalize("NFC", decomposed_name)
    decomposed_full = f"{base}\\{decomposed_name}"
    composed_full = f"{base}\\{composed_name}"

    app = FakeExcelApp(100, FakeWorkbooks([FakeWorkbook(composed_full)]))

    def fake_norm(value):
        normalized = mod.os.path.normcase(mod.os.path.normpath(str(value)))
        if str(value) == decomposed_full:
            return unicodedata.normalize("NFD", normalized)
        if str(value) == composed_full:
            return unicodedata.normalize("NFC", normalized)
        return normalized

    monkeypatch.setattr(mod, "_normalize_windows_path", fake_norm)
    monkeypatch.setattr(mod, "_iter_rot_running_objects", lambda _diag: [])
    monkeypatch.setattr(mod, "_collect_applications_from_windows", lambda _diag: ([(app, "accessible_object_from_window")], 1))
    monkeypatch.setattr(mod, "_get_active_excel_application", lambda: None)

    snap = mod.detect_target_workbook(Path(decomposed_full), timeout_sec=0.1, interval_sec=0.01)

    assert snap.workbook is not None
    assert snap.target_match_method == "unicode_normalized"
    assert snap.unicode_normalized_equal_count >= 1


def test_samefile_match_detects_target_when_normalized_differs(monkeypatch, tmp_path):
    _install_base(monkeypatch)
    target = (tmp_path / "target.xlsm").resolve()
    alias = (tmp_path / "target_alias.xlsm").resolve()
    target.write_bytes(b"x")
    alias.write_bytes(b"y")

    app = FakeExcelApp(100, FakeWorkbooks([FakeWorkbook(str(alias))]))

    monkeypatch.setattr(mod, "_normalize_windows_path", lambda value: f"norm::{str(value)}")
    monkeypatch.setattr(mod.os.path, "samefile", lambda _a, _b: True)
    monkeypatch.setattr(mod, "_iter_rot_running_objects", lambda _diag: [])
    monkeypatch.setattr(mod, "_collect_applications_from_windows", lambda _diag: ([(app, "accessible_object_from_window")], 1))
    monkeypatch.setattr(mod, "_get_active_excel_application", lambda: None)

    snap = mod.detect_target_workbook(target, timeout_sec=0.1, interval_sec=0.01)

    assert snap.workbook is not None
    assert snap.target_match_method == "samefile"
    assert snap.samefile_equal_count == 1


def test_extension_only_match_does_not_detect_target(monkeypatch, tmp_path):
    _install_base(monkeypatch)
    target = (tmp_path / "alpha.xlsm").resolve()
    other = (tmp_path / "beta.xlsm").resolve()
    app = FakeExcelApp(100, FakeWorkbooks([FakeWorkbook(str(other))]))

    monkeypatch.setattr(mod, "_iter_rot_running_objects", lambda _diag: [])
    monkeypatch.setattr(mod, "_collect_applications_from_windows", lambda _diag: ([(app, "accessible_object_from_window")], 1))
    monkeypatch.setattr(mod, "_get_active_excel_application", lambda: None)

    snap = mod.detect_target_workbook(target, timeout_sec=0.05, interval_sec=0.01)

    assert snap.workbook is None
    assert snap.target_match_method == "none"
    assert snap.extension_equal is True


def test_url_and_local_are_not_auto_matched(monkeypatch, tmp_path):
    _install_base(monkeypatch)
    target = (tmp_path / "target.xlsm").resolve()
    target.write_bytes(b"x")

    app = FakeExcelApp(100, FakeWorkbooks([FakeWorkbook("https://example.com/path/target.xlsm")]))

    monkeypatch.setattr(mod, "_iter_rot_running_objects", lambda _diag: [])
    monkeypatch.setattr(mod, "_collect_applications_from_windows", lambda _diag: ([(app, "accessible_object_from_window")], 1))
    monkeypatch.setattr(mod, "_get_active_excel_application", lambda: None)

    snap = mod.detect_target_workbook(target, timeout_sec=0.05, interval_sec=0.01)

    assert snap.workbook is None
    assert snap.target_match_method == "none"
    assert snap.local_url_count >= 1


def test_samefile_exception_is_unavailable(monkeypatch, tmp_path):
    _install_base(monkeypatch)
    target = (tmp_path / "target.xlsm").resolve()
    other = (tmp_path / "other.xlsm").resolve()
    target.write_bytes(b"x")
    other.write_bytes(b"y")

    app = FakeExcelApp(100, FakeWorkbooks([FakeWorkbook(str(other))]))

    monkeypatch.setattr(mod, "_normalize_windows_path", lambda value: f"norm::{str(value)}")
    monkeypatch.setattr(mod.os.path, "samefile", lambda _a, _b: (_ for _ in ()).throw(PermissionError("denied")))
    monkeypatch.setattr(mod, "_iter_rot_running_objects", lambda _diag: [])
    monkeypatch.setattr(mod, "_collect_applications_from_windows", lambda _diag: ([(app, "accessible_object_from_window")], 1))
    monkeypatch.setattr(mod, "_get_active_excel_application", lambda: None)

    snap = mod.detect_target_workbook(target, timeout_sec=0.05, interval_sec=0.01)

    assert snap.workbook is None
    assert snap.samefile_equal == "unavailable"
    assert snap.samefile_unavailable_count >= 1


def test_web_identity_url_normalization_is_stable():
    a = "HTTPS://Example.COM/path/%E6%A4%9C%E8%A8%BC.xlsm/?a=1#frag"
    b = "https://example.com/path/検証.xlsm"

    assert mod._normalize_web_identity_url(a) == mod._normalize_web_identity_url(b)


def test_web_identity_hash_ignores_fragment_and_query():
    base = "https://example.com/team/tool.xlsm"
    with_fragment = "https://example.com/team/tool.xlsm?temp=123#section"

    assert mod._hash_web_identity_url(base) == mod._hash_web_identity_url(with_fragment)


def test_web_identity_hash_differs_for_different_sites():
    a = "https://site-a.example.com/team/tool.xlsm"
    b = "https://site-b.example.com/team/tool.xlsm"

    assert mod._hash_web_identity_url(a) != mod._hash_web_identity_url(b)


def test_capture_and_match_use_identical_hash_function():
    workbook_fullname = (
        "https://tenant.sharepoint.com/sites/team/Shared%20Documents/"
        "%E3%80%90%E3%83%86%E3%82%B9%E3%83%88%E7%94%A8%E3%80%91book.xlsm?web=1#fragment"
    )

    capture_hash = mod._hash_web_identity_url(workbook_fullname)
    comparison_hash = mod._hash_web_identity_url(workbook_fullname)

    assert bool(capture_hash) is True
    assert bool(comparison_hash) is True
    assert capture_hash == comparison_hash


def test_web_identity_single_match_detects_target(monkeypatch, tmp_path):
    _install_base(monkeypatch)
    target = (tmp_path / "【テスト用】HENNGE作業用Tool.ver3.1.xlsm").resolve()
    target.write_bytes(b"x")

    target_url = "https://example.com/docs/%E3%80%90%E3%83%86%E3%82%B9%E3%83%88%E7%94%A8%E3%80%91HENNGE%E4%BD%9C%E6%A5%AD%E7%94%A8Tool.ver3.1.xlsm"
    hashed = mod._hash_web_identity_url(target_url)
    app = FakeExcelApp(100, FakeWorkbooks([FakeWorkbook(target_url)]))

    monkeypatch.setattr(mod, "_iter_rot_running_objects", lambda _diag: [])
    monkeypatch.setattr(mod, "_collect_applications_from_windows", lambda _diag: ([(app, "accessible_object_from_window")], 1))
    monkeypatch.setattr(mod, "_get_active_excel_application", lambda: None)

    snap = mod.detect_target_workbook(
        target,
        configured_web_identity_hash=hashed,
        web_identity_mode="test",
        timeout_sec=0.05,
        interval_sec=0.01,
    )

    assert snap.workbook is not None
    assert snap.target_match_method == "web_identity"
    assert snap.web_identity_match_count == 1


def test_environment_web_identity_match_sets_diagnostics(monkeypatch, tmp_path):
    _install_base(monkeypatch)
    target = (tmp_path / "【テスト用】HENNGE作業用Tool.ver3.1.xlsm").resolve()
    target.write_bytes(b"x")

    target_url = "https://example.com/docs/%E3%80%90%E3%83%86%E3%82%B9%E3%83%88%E7%94%A8%E3%80%91HENNGE%E4%BD%9C%E6%A5%AD%E7%94%A8Tool.ver3.1.xlsm"
    monkeypatch.setenv(mod.WEB_IDENTITY_ENV_TEST, mod._hash_web_identity_url(target_url).upper())
    resolution = mod._resolve_web_identity_hash({}, "test")
    app = FakeExcelApp(100, FakeWorkbooks([FakeWorkbook(target_url)]))

    monkeypatch.setattr(mod, "_iter_rot_running_objects", lambda _diag: [])
    monkeypatch.setattr(mod, "_collect_applications_from_windows", lambda _diag: ([(app, "accessible_object_from_window")], 1))
    monkeypatch.setattr(mod, "_get_active_excel_application", lambda: None)

    snap = mod.detect_target_workbook(
        target,
        configured_web_identity_hash=resolution.hash_value,
        web_identity_source=resolution.source,
        web_identity_valid=resolution.valid,
        web_identity_mode="test",
        timeout_sec=0.05,
        interval_sec=0.01,
    )

    assert snap.web_identity_configured is True
    assert snap.web_identity_source == "environment"
    assert snap.web_identity_valid is True
    assert snap.web_identity_match_count == 1
    assert snap.matched_workbook_count == 1
    assert snap.target_match_method == "web_identity"
    assert snap.workbook is not None


def test_web_identity_multiple_matches_are_ambiguous(monkeypatch, tmp_path):
    _install_base(monkeypatch)
    target = (tmp_path / "【テスト用】HENNGE作業用Tool.ver3.1.xlsm").resolve()
    target.write_bytes(b"x")

    target_url = "https://example.com/docs/%E3%80%90%E3%83%86%E3%82%B9%E3%83%88%E7%94%A8%E3%80%91HENNGE%E4%BD%9C%E6%A5%AD%E7%94%A8Tool.ver3.1.xlsm"
    hashed = mod._hash_web_identity_url(target_url)
    app = FakeExcelApp(100, FakeWorkbooks([FakeWorkbook(target_url), FakeWorkbook(target_url)]))

    monkeypatch.setattr(mod, "_iter_rot_running_objects", lambda _diag: [])
    monkeypatch.setattr(mod, "_collect_applications_from_windows", lambda _diag: ([(app, "accessible_object_from_window")], 1))
    monkeypatch.setattr(mod, "_get_active_excel_application", lambda: None)

    snap = mod.detect_target_workbook(
        target,
        configured_web_identity_hash=hashed,
        web_identity_mode="test",
        timeout_sec=0.05,
        interval_sec=0.01,
    )

    assert snap.workbook is None
    assert snap.application is None
    assert snap.target_match_method == "web_identity"
    assert snap.matched_workbook_count == 2


def test_capture_web_identity_succeeds_with_single_candidate(monkeypatch, tmp_path):
    _install_base(monkeypatch)
    target = (tmp_path / "【テスト用】HENNGE作業用Tool.ver3.1.xlsm").resolve()
    target.write_bytes(b"x")

    target_url = "https://example.com/docs/%E3%80%90%E3%83%86%E3%82%B9%E3%83%88%E7%94%A8%E3%80%91HENNGE%E4%BD%9C%E6%A5%AD%E7%94%A8Tool.ver3.1.xlsm"
    app = FakeExcelApp(100, FakeWorkbooks([FakeWorkbook(target_url)]))

    monkeypatch.setattr(mod, "_iter_rot_running_objects", lambda _diag: [])
    monkeypatch.setattr(mod, "_collect_applications_from_windows", lambda _diag: ([(app, "accessible_object_from_window")], 1))
    monkeypatch.setattr(mod, "_get_active_excel_application", lambda: None)

    snap = mod.detect_target_workbook(
        target,
        capture_web_identity=True,
        web_identity_mode="test",
        timeout_sec=0.05,
        interval_sec=0.01,
    )

    assert snap.capture_succeeded is True
    assert bool(snap.captured_web_identity_hash)
    assert snap.web_candidate_count == 1


def test_capture_web_identity_fails_with_multiple_candidates(monkeypatch, tmp_path):
    _install_base(monkeypatch)
    target = (tmp_path / "【テスト用】HENNGE作業用Tool.ver3.1.xlsm").resolve()
    target.write_bytes(b"x")

    u1 = "https://example.com/docs/%E3%80%90%E3%83%86%E3%82%B9%E3%83%88%E7%94%A8%E3%80%91HENNGE%E4%BD%9C%E6%A5%AD%E7%94%A8Tool.ver3.1.xlsm"
    u2 = "https://example.com/other/%E3%80%90%E3%83%86%E3%82%B9%E3%83%88%E7%94%A8%E3%80%91HENNGE%E4%BD%9C%E6%A5%AD%E7%94%A8Tool.ver3.1.xlsm"
    app = FakeExcelApp(100, FakeWorkbooks([FakeWorkbook(u1), FakeWorkbook(u2)]))

    monkeypatch.setattr(mod, "_iter_rot_running_objects", lambda _diag: [])
    monkeypatch.setattr(mod, "_collect_applications_from_windows", lambda _diag: ([(app, "accessible_object_from_window")], 1))
    monkeypatch.setattr(mod, "_get_active_excel_application", lambda: None)

    snap = mod.detect_target_workbook(
        target,
        capture_web_identity=True,
        web_identity_mode="test",
        timeout_sec=0.05,
        interval_sec=0.01,
    )

    assert snap.capture_succeeded is False
    assert snap.captured_web_identity_hash is None
    assert snap.web_candidate_count == 2


def test_web_identity_mode_rejects_mixed_basename(monkeypatch, tmp_path):
    _install_base(monkeypatch)
    test_named = (tmp_path / "【テスト用】book.xlsm").resolve()

    with pytest.raises(ValueError):
        mod.detect_target_workbook(
            test_named,
            timeout_sec=0.01,
            interval_sec=0.01,
            web_identity_mode="production",
            capture_web_identity=True,
        )


def test_web_identity_mode_test_requires_test_basename(monkeypatch, tmp_path):
    _install_base(monkeypatch)
    prod_named = (tmp_path / "book.xlsm").resolve()

    with pytest.raises(ValueError):
        mod.detect_target_workbook(
            prod_named,
            timeout_sec=0.01,
            interval_sec=0.01,
            web_identity_mode="test",
            capture_web_identity=True,
        )


def test_resolve_web_identity_hash_prefers_mode_specific_value(monkeypatch):
    monkeypatch.delenv(mod.WEB_IDENTITY_ENV_TEST, raising=False)
    monkeypatch.delenv(mod.WEB_IDENTITY_ENV_PRODUCTION, raising=False)
    excel_cfg = {
        "web_identity_hash": "a" * 64,
        "web_identity_hash_test": "b" * 64,
        "web_identity_hash_production": "c" * 64,
    }

    test_result = mod._resolve_web_identity_hash(excel_cfg, "test")
    production_result = mod._resolve_web_identity_hash(excel_cfg, "production")

    assert test_result == mod.WebIdentityResolution("b" * 64, "mode_config", True)
    assert production_result == mod.WebIdentityResolution("c" * 64, "mode_config", True)


def test_test_mode_prefers_test_environment_and_ignores_production(monkeypatch):
    monkeypatch.setenv(mod.WEB_IDENTITY_ENV_TEST, " " + "A" * 64 + " ")
    monkeypatch.setenv(mod.WEB_IDENTITY_ENV_PRODUCTION, "b" * 64)
    config = {"web_identity_hash_test": "c" * 64, "web_identity_hash": "d" * 64}

    result = mod._resolve_web_identity_hash(config, "test")

    assert result == mod.WebIdentityResolution("a" * 64, "environment", True)


def test_production_mode_prefers_production_environment_and_ignores_test(monkeypatch):
    monkeypatch.setenv(mod.WEB_IDENTITY_ENV_TEST, "a" * 64)
    monkeypatch.setenv(mod.WEB_IDENTITY_ENV_PRODUCTION, "B" * 64)
    config = {"web_identity_hash_production": "c" * 64, "web_identity_hash": "d" * 64}

    result = mod._resolve_web_identity_hash(config, "production")

    assert result == mod.WebIdentityResolution("b" * 64, "environment", True)


def test_test_mode_does_not_use_production_environment(monkeypatch):
    monkeypatch.delenv(mod.WEB_IDENTITY_ENV_TEST, raising=False)
    monkeypatch.setenv(mod.WEB_IDENTITY_ENV_PRODUCTION, "a" * 64)

    result = mod._resolve_web_identity_hash({}, "test")

    assert result == mod.WebIdentityResolution("", "none", False)


def test_production_mode_does_not_use_test_environment(monkeypatch):
    monkeypatch.setenv(mod.WEB_IDENTITY_ENV_TEST, "a" * 64)
    monkeypatch.delenv(mod.WEB_IDENTITY_ENV_PRODUCTION, raising=False)

    result = mod._resolve_web_identity_hash({}, "production")

    assert result == mod.WebIdentityResolution("", "none", False)


def test_environment_diagnostics_do_not_expose_value(monkeypatch):
    monkeypatch.setenv(mod.WEB_IDENTITY_ENV_TEST, "A" * 64)

    diagnostics = mod._inspect_web_identity_environment("test")

    assert diagnostics.selected_mode == "test"
    assert diagnostics.env_name_selected == "test"
    assert diagnostics.env_present is True
    assert diagnostics.env_value_length == 64
    assert diagnostics.env_length_valid is True
    assert diagnostics.env_hex_valid is True
    assert not hasattr(diagnostics, "hash_value")


def test_resolver_falls_back_to_legacy_config(monkeypatch):
    monkeypatch.delenv(mod.WEB_IDENTITY_ENV_TEST, raising=False)
    result = mod._resolve_web_identity_hash({"web_identity_hash": "c" * 64}, "test")

    assert result == mod.WebIdentityResolution("c" * 64, "legacy_config", True)


@pytest.mark.parametrize("environment_value", ["", "   "])
def test_resolver_ignores_blank_environment(monkeypatch, environment_value):
    monkeypatch.setenv(mod.WEB_IDENTITY_ENV_TEST, environment_value)
    result = mod._resolve_web_identity_hash({"web_identity_hash_test": "d" * 64}, "test")

    assert result == mod.WebIdentityResolution("d" * 64, "mode_config", True)


@pytest.mark.parametrize("invalid_value", ["a" * 63, "a" * 65, "g" * 64])
def test_resolver_rejects_invalid_sha256_environment(monkeypatch, invalid_value):
    monkeypatch.setenv(mod.WEB_IDENTITY_ENV_TEST, invalid_value)

    result = mod._resolve_web_identity_hash({"web_identity_hash_test": "b" * 64}, "test")

    assert result == mod.WebIdentityResolution("", "environment", False)


def test_rot_get_object_failure_count_recorded(monkeypatch):
    _install_base(monkeypatch)

    class FakeEnum:
        def __init__(self):
            self._count = 0

        def Next(self, _n):
            self._count += 1
            if self._count == 1:
                return ["m1"]
            return []

    class FakeRot:
        def EnumRunning(self):
            return FakeEnum()

        def GetObject(self, _moniker):
            raise RuntimeError("sensitive-moniker")

    fake_pythoncom = types.SimpleNamespace(
        GetRunningObjectTable=lambda: FakeRot(),
        CreateBindCtx=lambda _v: object(),
    )
    monkeypatch.setitem(sys.modules, "pythoncom", fake_pythoncom)

    diag = mod._DetectionDiagnostics()
    objs = mod._iter_rot_running_objects(diag)

    assert objs == []
    assert diag.counters.rot_get_object_failure_count == 1


def test_workbooks_count_one_or_more_enumerates_items(monkeypatch, tmp_path):
    _install_base(monkeypatch)
    target = (tmp_path / "target.xlsm").resolve()
    workbooks = FakeWorkbooks([FakeWorkbook(str(target))])
    app = FakeExcelApp(111, workbooks)

    monkeypatch.setattr(mod, "_iter_rot_running_objects", lambda _diag: [])
    monkeypatch.setattr(mod, "_collect_applications_from_windows", lambda _diag: ([(app, "accessible_object_from_window")], 1))
    monkeypatch.setattr(mod, "_get_active_excel_application", lambda: None)

    snap = mod.detect_target_workbook(target, timeout_sec=0.1, interval_sec=0.01)

    assert snap.workbook is not None
    assert workbooks.item_calls >= 1


def test_rot_miss_falls_back_to_windows(monkeypatch, tmp_path):
    _install_base(monkeypatch)
    target = (tmp_path / "target.xlsm").resolve()
    app = FakeExcelApp(100, FakeWorkbooks([FakeWorkbook(str(target))]))

    monkeypatch.setattr(mod, "_iter_rot_running_objects", lambda _diag: [])
    monkeypatch.setattr(mod, "_collect_applications_from_windows", lambda _diag: ([(app, "accessible_object_from_window")], 1))
    monkeypatch.setattr(mod, "_get_active_excel_application", lambda: None)

    snap = mod.detect_target_workbook(target, timeout_sec=0.1, interval_sec=0.01)

    assert snap.detection_method == "accessible_object_from_window"


def test_hwnd_unavailable_does_not_over_dedupe(monkeypatch, tmp_path):
    _install_base(monkeypatch)
    target = (tmp_path / "target.xlsm").resolve()

    app1 = RaisingHwndApp(FakeWorkbooks([FakeWorkbook(str(tmp_path / "x.xlsm"))]))
    app2 = RaisingHwndApp(FakeWorkbooks([FakeWorkbook(str(target))]))

    monkeypatch.setattr(mod, "_iter_rot_running_objects", lambda _diag: [])
    monkeypatch.setattr(mod, "_collect_applications_from_windows", lambda _diag: ([(app1, "accessible_object_from_window"), (app2, "accessible_object_from_window")], 2))
    monkeypatch.setattr(mod, "_get_active_excel_application", lambda: None)

    snap = mod.detect_target_workbook(target, timeout_sec=0.1, interval_sec=0.01)

    assert snap.workbook is not None
    assert snap.observed_application_count == 2


def test_save_close_operates_only_target(monkeypatch, tmp_path):
    _install_base(monkeypatch)
    logger = DummyLogger()
    target = (tmp_path / "target.xlsm").resolve()
    other = (tmp_path / "other.xlsm").resolve()

    app = FakeExcelApp(100, FakeWorkbooks([]))
    target_wb = FakeWorkbook(str(target), application=app)
    other_wb = FakeWorkbook(str(other), application=app)

    monkeypatch.setattr(mod, "_iter_rot_running_objects", lambda _diag: [other_wb, target_wb])
    monkeypatch.setattr(mod, "_collect_applications_from_windows", lambda _diag: ([], 0))
    monkeypatch.setattr(mod, "_get_active_excel_application", lambda: None)

    result = mod.save_and_close_target_workbook(target, logger)

    assert result is True
    assert target_wb.save_calls == 1
    assert target_wb.close_calls == [False]
    assert other_wb.save_calls == 0
    assert other_wb.close_calls == []
    assert app.quit_calls == 0


def test_multiple_matches_raise_and_do_not_save(monkeypatch, tmp_path):
    _install_base(monkeypatch)
    logger = DummyLogger()
    target = (tmp_path / "target.xlsm").resolve()
    target.write_bytes(b"x")

    app = FakeExcelApp(100, FakeWorkbooks([]))
    wb1 = FakeWorkbook(str(target), application=app)
    wb2 = FakeWorkbook(str(target), application=app)

    app = FakeExcelApp(100, FakeWorkbooks([wb1, wb2]))

    monkeypatch.setattr(mod, "_iter_rot_running_objects", lambda _diag: [])
    monkeypatch.setattr(mod, "_collect_applications_from_windows", lambda _diag: ([(app, "accessible_object_from_window")], 1))
    monkeypatch.setattr(mod, "_get_active_excel_application", lambda: None)

    with pytest.raises(mod.SaveCloseWorkbookError):
        mod.save_and_close_target_workbook(target, logger)

    assert wb1.save_calls == 0
    assert wb2.save_calls == 0


def test_multiple_web_identity_matches_raise_and_do_not_save(monkeypatch, tmp_path):
    _install_base(monkeypatch)
    logger = DummyLogger()
    target = (tmp_path / "【テスト用】HENNGE作業用Tool.ver3.1.xlsm").resolve()
    target.write_bytes(b"x")

    url = "https://example.com/docs/%E3%80%90%E3%83%86%E3%82%B9%E3%83%88%E7%94%A8%E3%80%91HENNGE%E4%BD%9C%E6%A5%AD%E7%94%A8Tool.ver3.1.xlsm"
    hashed = mod._hash_web_identity_url(url)
    app = FakeExcelApp(100, FakeWorkbooks([FakeWorkbook(url), FakeWorkbook(url)]))

    monkeypatch.setattr(mod, "_iter_rot_running_objects", lambda _diag: [])
    monkeypatch.setattr(mod, "_collect_applications_from_windows", lambda _diag: ([(app, "accessible_object_from_window")], 1))
    monkeypatch.setattr(mod, "_get_active_excel_application", lambda: None)

    with pytest.raises(mod.SaveCloseWorkbookError):
        mod.save_and_close_target_workbook(
            target,
            logger,
            configured_web_identity_hash=hashed,
            web_identity_mode="test",
        )


def test_readonly_raises(monkeypatch, tmp_path):
    _install_base(monkeypatch)
    logger = DummyLogger()
    target = (tmp_path / "target.xlsm").resolve()
    app = FakeExcelApp(100, FakeWorkbooks([]))
    wb = FakeWorkbook(str(target), application=app, read_only=True)

    monkeypatch.setattr(mod, "_iter_rot_running_objects", lambda _diag: [wb])
    monkeypatch.setattr(mod, "_collect_applications_from_windows", lambda _diag: ([], 0))
    monkeypatch.setattr(mod, "_get_active_excel_application", lambda: None)

    with pytest.raises(mod.ReadOnlyWorkbookError):
        mod.save_and_close_target_workbook(target, logger)


def test_non_windows_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(mod.os, "name", "posix", raising=False)

    with pytest.raises(RuntimeError):
        mod.detect_target_workbook(tmp_path / "target.xlsm")
