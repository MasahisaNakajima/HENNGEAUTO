import pytest
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.keys import Keys

from app.hennge_handler import HenngeHandler


USERNAME_SELECTOR = "input#login_user[name='login_user']"
PASSWORD_SELECTOR = "input#login_pwd[name='login_pwd'][type='password']"


class CaptureLogger:
    def __init__(self):
        self.messages = []

    def info(self, message: str) -> None:
        self.messages.append(message)

    def exception(self, message: str) -> None:
        self.messages.append(message)


class DummyForm:
    def __init__(self, form_id: str = ""):
        self._form_id = form_id

    def get_attribute(self, name: str):
        if name == "id":
            return self._form_id
        return None


class DummyInput:
    def __init__(
        self,
        *,
        tag_name: str = "input",
        displayed: bool = True,
        enabled: bool = True,
        attrs: dict | None = None,
        form: DummyForm | None = None,
    ):
        self.tag_name = tag_name
        self._displayed = displayed
        self._enabled = enabled
        self._attrs = attrs or {}
        self._form = form or DummyForm()
        self.sent_keys = []
        self.clicked = False

    def is_displayed(self) -> bool:
        return self._displayed

    def is_enabled(self) -> bool:
        return self._enabled

    def get_attribute(self, name: str):
        return self._attrs.get(name)

    def find_elements(self, by, value):
        if value == "ancestor::form[1]":
            return [self._form]
        return []

    def click(self) -> None:
        self.clicked = True

    def send_keys(self, *args) -> None:
        self.sent_keys.append(args)

        if args == (Keys.CONTROL, "a"):
            self._attrs["_selected_all"] = True
            return

        if args == (Keys.BACKSPACE,):
            if self._attrs.get("_selected_all"):
                self._attrs["value"] = ""
                self._attrs["_selected_all"] = False
            return

        if args == (Keys.TAB,):
            return

        for arg in args:
            if isinstance(arg, str):
                existing = self._attrs.get("value") or ""
                self._attrs["value"] = existing + arg


class DummyDriver:
    def __init__(self, selector_map: dict[str, list[DummyInput]], same_form: bool = True):
        self.selector_map = selector_map
        self.same_form = same_form
        self.queried_selectors = []

    def find_elements(self, by, selector):
        self.queried_selectors.append(selector)
        return self.selector_map.get(selector, [])

    def execute_script(self, script, first, second):
        return self.same_form


class DummyBrowser:
    def __init__(self, driver: DummyDriver):
        self.driver = driver


class ImmediateWait:
    def __init__(self, driver, timeout: int):
        self.driver = driver

    def until(self, predicate):
        if predicate(self.driver):
            return True
        raise TimeoutException("not ready")


def _build_handler(selector_map: dict[str, list[DummyInput]], same_form: bool = True):
    logger = CaptureLogger()
    driver = DummyDriver(selector_map, same_form=same_form)
    browser = DummyBrowser(driver)
    handler = HenngeHandler({}, logger, browser)
    return handler, logger, driver


def test_fill_credential_form_inputs_username_and_password(monkeypatch) -> None:
    from app import hennge_handler as mod

    monkeypatch.setattr(mod, "WebDriverWait", ImmediateWait)

    form = DummyForm("login_form")
    username = DummyInput(attrs={"id": "login_user", "name": "login_user", "type": "text", "value": ""}, form=form)
    password = DummyInput(attrs={"id": "login_pwd", "name": "login_pwd", "type": "password", "value": ""}, form=form)

    handler, logger, driver = _build_handler({
        USERNAME_SELECTOR: [username],
        PASSWORD_SELECTOR: [password],
    })

    handler._fill_credential_form("user-1", "pass-1")

    assert username.get_attribute("value") == "user-1"
    assert password.get_attribute("value") == "pass-1"
    assert (Keys.TAB,) in username.sent_keys
    assert USERNAME_SELECTOR in driver.queried_selectors
    assert PASSWORD_SELECTOR in driver.queried_selectors
    assert all("user-1" not in msg and "pass-1" not in msg for msg in logger.messages)


def test_fill_credential_form_does_not_select_submit_userpass_as_password(monkeypatch) -> None:
    from app import hennge_handler as mod

    monkeypatch.setattr(mod, "WebDriverWait", ImmediateWait)

    form = DummyForm("login_form")
    username = DummyInput(attrs={"id": "login_user", "name": "login_user", "type": "text", "value": ""}, form=form)
    password = DummyInput(attrs={"id": "login_pwd", "name": "login_pwd", "type": "password", "value": ""}, form=form)
    submit = DummyInput(attrs={"id": "login", "name": "userpass", "type": "submit", "value": "ログイン"}, form=form)

    handler, _logger, driver = _build_handler({
        USERNAME_SELECTOR: [username],
        PASSWORD_SELECTOR: [password],
        "input[name*='pass' i]": [submit],
    })

    handler._fill_credential_form("user-2", "pass-2")

    assert submit.sent_keys == []
    assert "input[name*='pass' i]" not in driver.queried_selectors


def test_fill_credential_form_ignores_hidden_xsrf_field(monkeypatch) -> None:
    from app import hennge_handler as mod

    monkeypatch.setattr(mod, "WebDriverWait", ImmediateWait)

    form = DummyForm("login_form")
    username = DummyInput(attrs={"id": "login_user", "name": "login_user", "type": "text", "value": ""}, form=form)
    password = DummyInput(attrs={"id": "login_pwd", "name": "login_pwd", "type": "password", "value": ""}, form=form)
    xsrf = DummyInput(attrs={"id": "_xsrf", "name": "_xsrf", "type": "hidden", "value": "token"}, form=form)

    handler, _logger, driver = _build_handler({
        USERNAME_SELECTOR: [username],
        PASSWORD_SELECTOR: [password],
        "input#_xsrf": [xsrf],
    })

    handler._fill_credential_form("user-3", "pass-3")

    assert xsrf.sent_keys == []
    assert "input#_xsrf" not in driver.queried_selectors


def test_fill_credential_form_raises_when_password_disabled(monkeypatch) -> None:
    from app import hennge_handler as mod

    monkeypatch.setattr(mod, "WebDriverWait", ImmediateWait)

    form = DummyForm("login_form")
    username = DummyInput(attrs={"id": "login_user", "name": "login_user", "type": "text", "value": ""}, form=form)
    password = DummyInput(
        enabled=False,
        attrs={"id": "login_pwd", "name": "login_pwd", "type": "password", "value": "", "disabled": "disabled"},
        form=form,
    )

    handler, _logger, _driver = _build_handler({
        USERNAME_SELECTOR: [username],
        PASSWORD_SELECTOR: [password],
    })

    with pytest.raises(RuntimeError, match="入力準備"):
        handler._fill_credential_form("user-4", "pass-4")


def test_fill_credential_form_raises_when_password_readonly(monkeypatch) -> None:
    from app import hennge_handler as mod

    monkeypatch.setattr(mod, "WebDriverWait", ImmediateWait)

    form = DummyForm("login_form")
    username = DummyInput(attrs={"id": "login_user", "name": "login_user", "type": "text", "value": ""}, form=form)
    password = DummyInput(
        attrs={"id": "login_pwd", "name": "login_pwd", "type": "password", "value": "", "readonly": "readonly"},
        form=form,
    )

    handler, _logger, _driver = _build_handler({
        USERNAME_SELECTOR: [username],
        PASSWORD_SELECTOR: [password],
    })

    with pytest.raises(RuntimeError, match="入力準備"):
        handler._fill_credential_form("user-5", "pass-5")


def test_fill_credential_form_raises_when_password_candidates_are_multiple(monkeypatch) -> None:
    from app import hennge_handler as mod

    monkeypatch.setattr(mod, "WebDriverWait", ImmediateWait)

    form = DummyForm("login_form")
    username = DummyInput(attrs={"id": "login_user", "name": "login_user", "type": "text", "value": ""}, form=form)
    pw1 = DummyInput(attrs={"id": "login_pwd", "name": "login_pwd", "type": "password", "value": ""}, form=form)
    pw2 = DummyInput(attrs={"id": "login_pwd", "name": "login_pwd", "type": "password", "value": ""}, form=form)

    handler, _logger, _driver = _build_handler({
        USERNAME_SELECTOR: [username],
        PASSWORD_SELECTOR: [pw1, pw2],
    })

    with pytest.raises(RuntimeError, match="パスワード欄の表示中候補数"):
        handler._fill_credential_form("user-6", "pass-6")


def test_fill_credential_form_raises_when_fields_are_in_different_forms(monkeypatch) -> None:
    from app import hennge_handler as mod

    monkeypatch.setattr(mod, "WebDriverWait", ImmediateWait)

    form_a = DummyForm("form_a")
    form_b = DummyForm("form_b")
    username = DummyInput(attrs={"id": "login_user", "name": "login_user", "type": "text", "value": ""}, form=form_a)
    password = DummyInput(attrs={"id": "login_pwd", "name": "login_pwd", "type": "password", "value": ""}, form=form_b)

    handler, _logger, _driver = _build_handler({
        USERNAME_SELECTOR: [username],
        PASSWORD_SELECTOR: [password],
    }, same_form=False)

    with pytest.raises(RuntimeError, match="同一form"):
        handler._fill_credential_form("user-7", "pass-7")
