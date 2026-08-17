import pytest

import diagnose_hennge_certificate_detail as detail
import app.hennge_handler as hennge_module
from app.hennge_handler import HenngeHandler


class Element:
    tag_name = "button"

    def __init__(self, *, label="", clicks=None):
        self.label = label
        self.clicks = clicks if clicks is not None else []
        self.location_once_scrolled_into_view = (0, 0)

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def get_attribute(self, name):
        return {"aria-label": self.label, "data-testid": self.label}.get(name)

    def click(self):
        self.clicks.append(self.label)


class Browser:
    driver = object()


class Logger:
    def info(self, _message):
        pass


def _patch_password_dom(monkeypatch, *, copy_safe=True):
    clicks = []
    reveal = Element(label="パスワードを表示する", clicks=clicks)
    copy = Element(label="コピー", clicks=clicks)
    section = Element(label="password-section")
    states = iter([reveal, copy])
    monkeypatch.setattr(detail, "_identify_detail_area", lambda _driver: (object(), "dialog"))
    monkeypatch.setattr(detail, "find_password_section", lambda _container: section)
    monkeypatch.setattr(detail, "password_section_scroll_target", lambda value: value)
    monkeypatch.setattr(
        detail,
        "inspect_password_reveal_candidates",
        lambda _section: {
            "candidates": [reveal], "candidate_count": 1, "unique": True,
            "displayed": True, "enabled": True, "disabled": False,
            "inside_detail_dialog": True, "safe": True,
        },
    )

    def inspect_copy(_section):
        if copy_safe:
            return {
                "candidates": [copy], "candidate_count": 1, "unique": True,
                "displayed": True, "enabled": True, "safe": True,
                "masked_password_field_count": 1,
                "password_eye_button_candidate_count": 1,
            }
        return {
            "candidates": [], "candidate_count": 0, "unique": False,
            "displayed": False, "enabled": False, "safe": False,
            "masked_password_field_count": 1,
            "password_eye_button_candidate_count": 1,
        }

    monkeypatch.setattr(detail, "inspect_password_copy_candidates", inspect_copy)
    return clicks, reveal, copy


def test_password_flow_clicks_reveal_and_copy_once_and_clears_clipboard(monkeypatch):
    clicks, reveal, copy = _patch_password_dom(monkeypatch)
    clipboard = {"reads": 0, "clears": 0}
    monkeypatch.setattr(hennge_module, "_read_windows_clipboard_once", lambda: clipboard.__setitem__("reads", clipboard["reads"] + 1) or "secret")
    monkeypatch.setattr(hennge_module, "_clear_windows_clipboard", lambda: clipboard.__setitem__("clears", clipboard["clears"] + 1))

    password = HenngeHandler({}, Logger(), Browser()).read_certificate_password()

    assert password == "secret"
    assert clicks == ["パスワードを表示する", "コピー"]
    assert reveal.location_once_scrolled_into_view == (0, 0)
    assert clipboard == {"reads": 1, "clears": 1}


def test_password_flow_does_not_click_when_copy_is_not_unique(monkeypatch):
    clicks, _reveal, _copy = _patch_password_dom(monkeypatch, copy_safe=False)
    clock = iter([0.0, 11.0])
    monkeypatch.setattr(hennge_module.time, "monotonic", lambda: next(clock, 11.0))
    monkeypatch.setattr(hennge_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="hennge_password_copy_button_resolved"):
        HenngeHandler({}, Logger(), Browser()).read_certificate_password()

    assert clicks == ["パスワードを表示する"]


def test_password_copy_observation_never_clicks_eye_button():
    eye = Element(label="目")
    copy = Element(label="コピー")
    section = Element(label="section")
    section.find_elements = lambda *_args: [eye, copy]
    result = detail.inspect_password_copy_candidates(section)
    assert result["password_eye_button_candidate_count"] >= 0
    assert eye.clicks == []


def test_clipboard_password_is_not_in_observation(monkeypatch):
    _patch_password_dom(monkeypatch)
    monkeypatch.setattr(hennge_module, "_read_windows_clipboard_once", lambda: "secret")
    monkeypatch.setattr(hennge_module, "_clear_windows_clipboard", lambda: None)

    handler = HenngeHandler({}, Logger(), Browser())
    handler.read_certificate_password()

    assert "secret" not in repr(handler.last_password_observation)
    assert handler.last_password_observation["password_source_type"] == "clipboard_copy_control"
    assert handler.last_password_observation["password_eye_button_click_called"] is False
