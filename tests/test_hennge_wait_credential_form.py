import pytest

from app.hennge_handler import HenngeHandler


class DummyLogger:
    def info(self, message: str) -> None:
        return None

    def exception(self, message: str) -> None:
        return None


class DummyBrowser:
    def __init__(self):
        self.driver = object()


class Sequence:
    def __init__(self, values):
        self.values = list(values)
        self.index = 0

    def __call__(self, *args, **kwargs):
        if not self.values:
            return None
        if self.index < len(self.values):
            value = self.values[self.index]
            self.index += 1
            return value
        return self.values[-1]


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _handler() -> HenngeHandler:
    return HenngeHandler({}, DummyLogger(), DummyBrowser())


def _install_fake_clock(monkeypatch):
    from app import hennge_handler as mod

    clock = FakeClock()
    monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mod.time, "sleep", clock.sleep)
    return clock


def test_wait_credential_form_url_changes_then_form_appears(monkeypatch) -> None:
    _install_fake_clock(monkeypatch)
    handler = _handler()

    monkeypatch.setattr(handler, "_current_sanitized_url", Sequence([
        "https://admin.auth.hennge.com/portal/yco.co.jp/",
        "https://ap.ssso.hdems.com/portal/yco.co.jp/login/",
        "https://ap.ssso.hdems.com/portal/yco.co.jp/login/",
    ]))
    monkeypatch.setattr(handler, "_current_title", Sequence(["before", "after", "after"]))
    monkeypatch.setattr(handler, "_has_credential_form", Sequence([False, False, True]))
    monkeypatch.setattr(handler, "_is_redirecting_state", Sequence([True, False, False]))
    monkeypatch.setattr(handler, "_detect_domain_error", Sequence([False, False, False]))

    assert handler._wait_for_credential_form(timeout_sec=10) == "credential_form"


def test_wait_credential_form_ignores_initial_domain_error_during_grace(monkeypatch) -> None:
    _install_fake_clock(monkeypatch)
    handler = _handler()

    monkeypatch.setattr(handler, "_current_sanitized_url", Sequence([
        "https://ap.ssso.hdems.com/portal/yco.co.jp/login/",
    ]))
    monkeypatch.setattr(handler, "_current_title", Sequence(["HENNGE Access Control"]))
    monkeypatch.setattr(handler, "_has_credential_form", Sequence([False, False, False, True]))
    monkeypatch.setattr(handler, "_is_redirecting_state", Sequence([False, False, False, False]))
    monkeypatch.setattr(handler, "_detect_domain_error", Sequence([True, True, False, False]))

    assert handler._wait_for_credential_form(timeout_sec=10) == "credential_form"


def test_wait_credential_form_returns_domain_error_after_three_stable_checks(monkeypatch) -> None:
    _install_fake_clock(monkeypatch)
    handler = _handler()

    monkeypatch.setattr(handler, "_current_sanitized_url", Sequence([
        "https://ap.ssso.hdems.com/portal/yco.co.jp/login/",
    ]))
    monkeypatch.setattr(handler, "_current_title", Sequence(["HENNGE Access Control"]))
    monkeypatch.setattr(handler, "_has_credential_form", Sequence([False]))
    monkeypatch.setattr(handler, "_is_redirecting_state", Sequence([False]))
    monkeypatch.setattr(handler, "_detect_domain_error", Sequence([True]))

    assert handler._wait_for_credential_form(timeout_sec=10) == "domain_error"


def test_wait_credential_form_returns_timeout_when_no_form(monkeypatch) -> None:
    _install_fake_clock(monkeypatch)
    handler = _handler()

    monkeypatch.setattr(handler, "_current_sanitized_url", Sequence([
        "https://ap.ssso.hdems.com/portal/yco.co.jp/login/",
    ]))
    monkeypatch.setattr(handler, "_current_title", Sequence(["HENNGE Access Control"]))
    monkeypatch.setattr(handler, "_has_credential_form", Sequence([False]))
    monkeypatch.setattr(handler, "_is_redirecting_state", Sequence([False]))
    monkeypatch.setattr(handler, "_detect_domain_error", Sequence([False]))

    assert handler._wait_for_credential_form(timeout_sec=3) == "timeout"


def test_wait_credential_form_returns_credential_form_immediately(monkeypatch) -> None:
    _install_fake_clock(monkeypatch)
    handler = _handler()

    monkeypatch.setattr(handler, "_current_sanitized_url", Sequence([
        "https://ap.ssso.hdems.com/portal/yco.co.jp/login/",
    ]))
    monkeypatch.setattr(handler, "_current_title", Sequence(["HENNGE Access Control"]))
    monkeypatch.setattr(handler, "_has_credential_form", Sequence([True]))
    monkeypatch.setattr(handler, "_is_redirecting_state", Sequence([False]))
    monkeypatch.setattr(handler, "_detect_domain_error", Sequence([False]))

    assert handler._wait_for_credential_form(timeout_sec=10) == "credential_form"
