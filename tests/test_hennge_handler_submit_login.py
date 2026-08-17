import pytest

from app.hennge_handler import HenngeHandler


class DummyElement:
    def __init__(self, *, tag_name: str = "input", displayed: bool = True, attrs: dict | None = None):
        self.tag_name = tag_name
        self._displayed = displayed
        self._attrs = attrs or {}
        self.clicked = False

    def is_displayed(self) -> bool:
        return self._displayed

    def get_attribute(self, name: str):
        return self._attrs.get(name)

    def click(self) -> None:
        self.clicked = True


class DummyDriver:
    def __init__(self, elements):
        self._elements = elements

    def find_elements(self, by, selector):
        if selector == "input#login[name='userpass'][type='submit']":
            return self._elements
        return []


class DummyBrowser:
    def __init__(self, elements):
        self.driver = DummyDriver(elements)


class DummyLogger:
    def info(self, message: str) -> None:
        return None

    def exception(self, message: str) -> None:
        return None


def _handler(elements):
    return HenngeHandler({}, DummyLogger(), DummyBrowser(elements))


def test_submit_login_selects_only_normal_login_when_both_present() -> None:
    # Selector must isolate only id=login,name=userpass candidate.
    normal = DummyElement(attrs={"id": "login", "name": "userpass", "type": "submit", "value": "ログイン"})
    handler = _handler([normal])

    assert handler._submit_login() is True
    assert normal.clicked is True


def test_submit_login_raises_when_only_certificate_login_exists() -> None:
    # No matching selector candidate should result in 0 visible candidates.
    handler = _handler([])
    with pytest.raises(RuntimeError, match="表示中候補数"):
        handler._submit_login()


def test_submit_login_raises_when_multiple_normal_candidates_exist() -> None:
    first = DummyElement(attrs={"id": "login", "name": "userpass", "type": "submit", "value": "ログイン"})
    second = DummyElement(attrs={"id": "login", "name": "userpass", "type": "submit", "value": "ログイン"})
    handler = _handler([first, second])

    with pytest.raises(RuntimeError, match="表示中候補数"):
        handler._submit_login()


def test_submit_login_raises_when_value_contains_certificate() -> None:
    wrong = DummyElement(attrs={"id": "login", "name": "userpass", "type": "submit", "value": "証明書ログイン"})
    handler = _handler([wrong])

    with pytest.raises(RuntimeError, match="証明書ログイン要素"):
        handler._submit_login()
