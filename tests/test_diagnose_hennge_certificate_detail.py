import pytest
from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By

import diagnose_hennge_certificate_detail as mod


class Node:
    def __init__(self, *, tag="div", attrs=None, text="", displayed=True, enabled=True, children=None, parent=None):
        self.tag_name = tag
        self._attrs = attrs or {}
        self.text = text
        self._displayed = displayed
        self._enabled = enabled
        self.children = []
        self.parent = parent
        self.click_count = 0
        if children:
            for child in children:
                self.add_child(child)

    def add_child(self, child):
        child.parent = self
        self.children.append(child)

    def is_displayed(self):
        return self._displayed

    def is_enabled(self):
        return self._enabled

    def get_attribute(self, name):
        return self._attrs.get(name)

    def click(self):
        self.click_count += 1

    def _walk_descendants(self):
        stack = list(self.children)
        while stack:
            node = stack.pop(0)
            yield node
            stack[0:0] = node.children

    def _matches(self, selector):
        if selector == "a":
            return self.tag_name == "a"
        if selector == "button":
            return self.tag_name == "button"
        if selector == "input":
            return self.tag_name == "input"
        if selector == "h1":
            return self.tag_name == "h1"
        if selector == "h2":
            return self.tag_name == "h2"
        if selector == "[role='heading']":
            return (self.get_attribute("role") or "") == "heading"
        if selector == "[role='dialog']":
            return (self.get_attribute("role") or "") == "dialog"
        if selector == "[class*='drawer']":
            return "drawer" in (self.get_attribute("class") or "")
        if selector == "[data-testid*='drawer']":
            return "drawer" in (self.get_attribute("data-testid") or "")
        if selector == "[aria-modal='true']":
            return (self.get_attribute("aria-modal") or "") == "true"
        if selector == "button[aria-label='Close']":
            return self.tag_name == "button" and (self.get_attribute("aria-label") or "") == "Close"
        if selector == "input[name='note']":
            return self.tag_name == "input" and (self.get_attribute("name") or "") == "note"
        if selector == "button[data-testid='send-installation-email-toolbar']":
            return self.tag_name == "button" and (self.get_attribute("data-testid") or "") == "send-installation-email-toolbar"
        return False

    def find_elements(self, by, selector):
        if by == By.XPATH and selector == "ancestor-or-self::*":
            chain = []
            cur = self
            while cur is not None:
                chain.append(cur)
                cur = cur.parent
            return list(reversed(chain))

        if by == By.CSS_SELECTOR:
            return [node for node in self._walk_descendants() if node._matches(selector)]

        return []


class StructuredNode(Node):
    def __init__(self, *, selector_map=None, **kwargs):
        super().__init__(**kwargs)
        self.selector_map = selector_map or {}

    def find_elements(self, by, selector):
        if by == By.CSS_SELECTOR and selector in self.selector_map:
            return self.selector_map[selector]
        if by == By.XPATH and selector == "following-sibling::*[1]":
            return self.selector_map.get("following-sibling", [])
        if by == By.XPATH:
            return []
        return super().find_elements(by, selector)


class Driver:
    def __init__(self, *, root, rows, current_url="https://admin.auth.hennge.com/certificates/?q=secret#frag"):
        self.root = root
        self.rows = rows
        self.current_url = current_url
        self.execute_script_called = 0

    def find_elements(self, by, selector):
        if by == By.CSS_SELECTOR and selector == "table tbody tr":
            return self.rows
        return self.root.find_elements(by, selector)

    def execute_script(self, *_args, **_kwargs):
        self.execute_script_called += 1
        raise AssertionError("execute_script must not be used")


class DummyBrowser:
    def __init__(self, _base_dir, _config, driver=None):
        self.driver = driver

    def start(self):
        return None

    def open(self, _url):
        return None

    def wait_for_page_ready(self, timeout=20):
        _ = timeout
        return None

    def quit(self):
        return None


class DummyHandler:
    def __init__(self, _config, _logger, _browser):
        return None

    def login(self):
        return None


class DummyLogger:
    def __init__(self):
        self.info_messages = []
        self.error_messages = []
        self.exception_messages = []
        self.diag_calls = []

    def info(self, message):
        self.info_messages.append(message)

    def error(self, message):
        self.error_messages.append(message)

    def exception(self, message):
        self.exception_messages.append(message)

    def save_browser_diagnostics(self, driver, name, save_html=True):
        self.diag_calls.append((driver, name, save_html))


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class UrlSequenceDriver:
    def __init__(self, *, urls, dialog=False, drawer=False):
        self.urls = list(urls)
        self.idx = 0
        self.dialog = dialog
        self.drawer = drawer

    @property
    def current_url(self):
        if self.idx < len(self.urls):
            v = self.urls[self.idx]
            self.idx += 1
            return v
        return self.urls[-1]

    def find_elements(self, by, selector):
        if selector in {"h1", "h2", "[role='heading']"}:
            return []
        if selector == "[role='dialog']":
            return [Node(tag="div", attrs={"role": "dialog"})] if self.dialog else []
        if selector == "[class*='drawer']":
            return [Node(tag="section", attrs={"class": "right drawer"})] if self.drawer else []
        if selector in {"[data-testid*='drawer']", "[aria-modal='true']"}:
            return []
        return []


class WaitSignalDriver:
    def __init__(self, *, heading_texts=None, selector_counts=None, current_url="https://admin.auth.hennge.com/certificates/"):
        self.current_url = current_url
        self._heading_texts = list(heading_texts or [])
        self._heading_i = 0
        self._selector_counts = {k: list(v) for k, v in (selector_counts or {}).items()}
        self._selector_i = {}

    def _next_count(self, selector):
        seq = self._selector_counts.get(selector, [0])
        idx = self._selector_i.get(selector, 0)
        if idx < len(seq):
            value = seq[idx]
            self._selector_i[selector] = idx + 1
            return value
        return seq[-1]

    def find_elements(self, by, selector):
        if selector in {"h1", "h2", "[role='heading']"}:
            if self._heading_i < len(self._heading_texts):
                text = self._heading_texts[self._heading_i]
                self._heading_i += 1
                return [Node(tag="h2", text=text)] if text else []
            return []

        count = self._next_count(selector)
        items = []
        for _ in range(count):
            if selector == "button[aria-label='Close']":
                items.append(Node(tag="button", attrs={"aria-label": "Close"}))
            elif selector == "input[name='note']":
                items.append(Node(tag="input", attrs={"name": "note"}))
            elif selector == "button[data-testid='send-installation-email-toolbar']":
                items.append(Node(tag="button", attrs={"data-testid": "send-installation-email-toolbar"}))
            elif selector == "[role='dialog']":
                items.append(Node(tag="div", attrs={"role": "dialog"}))
            elif selector == "[class*='drawer']":
                items.append(Node(tag="section", attrs={"class": "drawer"}))
            elif selector == "[data-testid*='drawer']":
                items.append(Node(tag="section", attrs={"data-testid": "drawer-1"}))
            elif selector == "[aria-modal='true']":
                items.append(Node(tag="section", attrs={"aria-modal": "true"}))
            else:
                items.append(Node())
        return items


def _make_row():
    return Node(tag="tr", attrs={"class": "cursor-pointer"}, children=[])


def _build_detail_dom(*, include_dialog=False, nav_download=False, detail_actions=None):
    detail_actions = detail_actions or []

    html = Node(tag="html")
    body = Node(tag="body")
    main = Node(tag="main", attrs={"role": "main"})
    html.add_child(body)
    body.add_child(main)

    nav = Node(tag="section", attrs={"class": "left-nav"})
    main.add_child(nav)
    if nav_download:
        nav.add_child(Node(tag="a", attrs={"id": "nav-link", "href": "https://admin.auth.hennge.com/certificates/download/nav"}, text="Download"))

    panel_attrs = {"class": "detail-panel right", "style": "position: fixed; right: 0;"}
    if include_dialog:
        panel_attrs["role"] = "dialog"
    panel = Node(tag="section", attrs=panel_attrs)
    main.add_child(panel)

    close_button = Node(tag="button", attrs={"aria-label": "Close"}, text="Close")
    note_input = Node(tag="input", attrs={"name": "note"})
    mail_btn = Node(tag="button", attrs={"data-testid": "send-installation-email-toolbar"}, text="Send installation email")
    panel.add_child(close_button)
    panel.add_child(note_input)
    panel.add_child(mail_btn)

    for action in detail_actions:
        panel.add_child(action)

    heading = Node(tag="h2", text="証明書詳細")
    main.add_child(heading)

    return html, panel, close_button, note_input


def _install_main_mocks(monkeypatch, *, result_count=1, rows=None, detail_actions=None, stale_once=False, detail_ready=None, nav_download=False):
    rows = rows if rows is not None else [_make_row()]
    html, panel, close_button, note_input = _build_detail_dom(detail_actions=detail_actions, nav_download=nav_download)
    driver = Driver(root=html, rows=rows)
    browser = DummyBrowser(None, None, driver=driver)
    logger = DummyLogger()
    search_input = Node(tag="input", attrs={"name": "query", "aria-label": "Search"})

    calls = {"row_click": 0, "submit": 0, "find_single": 0}

    monkeypatch.setattr(mod, "load_config", lambda: {})
    monkeypatch.setattr(mod, "ensure_directories", lambda _config: None)
    monkeypatch.setattr(mod, "AppLogger", lambda _base_dir: logger)
    monkeypatch.setattr(mod, "Browser", lambda _base_dir, _config: browser)
    monkeypatch.setattr(mod, "HenngeHandler", DummyHandler)

    def fake_wait_input(_driver, _logger, timeout_seconds=20):
        _ = timeout_seconds
        return search_input

    def fake_submit(_search_input, _alias, _logger):
        calls["submit"] += 1
        if stale_once and calls["submit"] == 1:
            raise StaleElementReferenceException("stale")

    def fake_wait_results(_browser, timeout_seconds=15):
        _ = timeout_seconds
        return result_count

    def fake_find_single(_driver, selector):
        calls["find_single"] += 1
        assert selector == mod.cert_search.SEARCH_INPUT_PRIMARY_SELECTOR
        return search_input

    def fake_wait_detail(_driver, _before_path, _logger, timeout_seconds=20):
        _ = timeout_seconds
        if isinstance(detail_ready, Exception):
            raise detail_ready
        if detail_ready is not None:
            return detail_ready
        return {
            "path_changed": False,
            "has_heading": True,
            "has_surface": False,
            "detail_area": panel,
            "detail_method": mod.DETAIL_METHOD_CLOSE_COMMON,
            "close_count": 1,
            "note_count": 1,
            "mail_toolbar_count": 1,
            "dialog_count": 0,
            "detail_area_found": True,
        }

    original_click = Node.click

    def counted_click(self):
        if self in rows:
            calls["row_click"] += 1
        return original_click(self)

    monkeypatch.setattr(Node, "click", counted_click)
    monkeypatch.setattr(mod.cert_search, "_wait_certificate_search_input_ready", fake_wait_input)
    monkeypatch.setattr(mod.cert_search, "_set_query_and_submit", fake_submit)
    monkeypatch.setattr(mod.cert_search, "_wait_results_ready", fake_wait_results)
    monkeypatch.setattr(mod.cert_search, "_find_single_visible_search_input", fake_find_single)
    monkeypatch.setattr(mod.cert_search, "_log_result_state", lambda _logger, _count: None)
    monkeypatch.setattr(mod, "_wait_detail_ready", fake_wait_detail)

    return logger, browser, calls, panel, close_button, note_input


def test_identify_detail_area_by_close_and_note_without_dialog():
    html, panel, _close, _note = _build_detail_dom(include_dialog=False)
    driver = Driver(root=html, rows=[_make_row()])

    area, method = mod._identify_detail_area(driver)
    assert area is panel
    assert method == mod.DETAIL_METHOD_CLOSE_COMMON


def test_detail_alias_is_read_from_label_value_pair_without_page_scope():
    value = StructuredNode(tag="dd", text="target-alias")
    label = StructuredNode(tag="dt", text="Alias", selector_map={"following-sibling": [value]})
    container = StructuredNode(
        selector_map={
            "dt,th,label,[role='rowheader'],[data-testid*='label' i],[class*='label' i],[data-label]": [label],
            "dd,td,input,textarea,[role='cell'],[data-testid*='value' i],.value,[class*='value' i]": [value],
            "tr,[role='row'],fieldset,form,[data-testid*='field' i]": [],
            "[aria-labelledby]": [],
        }
    )

    observed = mod._extract_detail_field_observation(container)

    assert observed["alias_label_found"] is True
    assert observed["alias_value_found"] is True
    assert observed["alias_values"] == ["target-alias"]


def test_password_structure_identifies_password_readonly_copy_and_reveal_candidates():
    password = StructuredNode(tag="input", attrs={"type": "password", "value": "secret"})
    readonly = StructuredNode(tag="input", attrs={"readonly": "true", "value": "readonly-secret"})
    label = StructuredNode(tag="dt", text="Password", selector_map={"following-sibling": [password]})
    reveal = StructuredNode(tag="button", attrs={"data-testid": "reveal-password"})
    copy = StructuredNode(tag="button", attrs={"data-testid": "copy-password"})
    container = StructuredNode(
        selector_map={
            "input[type='password']": [password],
            "input[readonly],textarea[readonly]": [readonly],
            "[class*='mask' i],[data-testid*='mask' i],[aria-label*='password' i]": [],
            "button[aria-label*='show' i],button[aria-label*='表示' i],[data-testid*='reveal' i],[data-testid*='show' i]": [reveal],
            "button[aria-label*='copy' i],button[aria-label*='コピー' i],[data-testid*='copy' i]": [copy],
            "dt,th,label,[role='rowheader'],[data-testid*='label' i]": [label],
            "dd,td,input,textarea,[role='cell'],[data-testid*='value' i],.value,[class*='value' i]": [password, readonly],
            "tr,[role='row'],fieldset,form,[data-testid*='field' i]": [],
            "[aria-labelledby]": [],
        }
    )

    observed = mod._extract_password_structure(container)

    assert observed["password_input_candidate_count"] == 1
    assert observed["readonly_value_candidate_count"] == 1
    assert observed["reveal_button_candidate_count"] == 1
    assert observed["copy_button_candidate_count"] == 1
    assert observed["password_source_candidate_count"] == 2
    assert "secret" in observed["password_values"]


def test_detail_diagnostic_does_not_include_values_or_visible_text(tmp_path):
    value = StructuredNode(tag="dd", text="private-alias")
    label = StructuredNode(tag="dt", text="Alias", selector_map={"following-sibling": [value]})
    container = StructuredNode(selector_map={
        "dt,dd,th,td,label,input,textarea,button,a,[role='button'],[role='link']": [label, value],
    })
    class Logger:
        base_dir = tmp_path

    from app.hennge_handler import HenngeHandler
    handler = HenngeHandler({}, Logger(), object())
    handler._save_detail_dom_diagnostic(container, {"password_values": ["private-password"], "field_row_count": 1})
    diagnostic = next((tmp_path / "logs").glob("hennge_certificate_detail_dom_*.json")).read_text(encoding="utf-8")

    assert "private-alias" not in diagnostic
    assert "private-password" not in diagnostic
    assert "visible text" not in diagnostic


def test_identify_detail_area_does_not_use_body_or_main():
    html = Node(tag="html")
    body = Node(tag="body")
    main = Node(tag="main", attrs={"role": "main"})
    html.add_child(body)
    body.add_child(main)
    main.add_child(Node(tag="button", attrs={"aria-label": "Close"}))
    main.add_child(Node(tag="input", attrs={"name": "note"}))
    driver = Driver(root=html, rows=[_make_row()])

    with pytest.raises(RuntimeError, match="詳細領域を特定できませんでした"):
        mod._identify_detail_area(driver)


def test_scoped_collection_excludes_navigation_elements():
    detail_action = Node(tag="button", attrs={"id": "detail-download", "title": "Download"}, text="Download")
    html, panel, _close, _note = _build_detail_dom(nav_download=True, detail_actions=[detail_action])

    actions = mod._extract_action_elements(panel)
    ids = {item["id"] for item in actions}
    assert "detail-download" in ids
    assert "nav-link" not in ids


def test_label_classification_download_variants():
    assert mod._classify_label(mod._normalize_action_text("ダウンロード")) == mod.LABEL_CATEGORY_DOWNLOAD
    assert mod._classify_label(mod._normalize_action_text("Download")) == mod.LABEL_CATEGORY_DOWNLOAD


def test_installation_email_datatestid_has_highest_priority_over_download_text():
    action = Node(
        tag="button",
        attrs={"data-testid": "send-installation-email-toolbar"},
        text="ダウンロード",
    )
    panel = Node(tag="section", children=[action])

    extracted = mod._extract_action_elements(panel)
    assert extracted[0]["label_category"] == mod.LABEL_CATEGORY_INSTALLATION_EMAIL
    assert mod._is_download_candidate(extracted[0]) is False


def test_installation_email_not_overwritten_to_download_by_attribute_keywords():
    action = Node(
        tag="button",
        attrs={"data-testid": "foo-send-installation-email-bar", "title": "Download"},
        text="Download",
    )
    panel = Node(tag="section", children=[action])

    extracted = mod._extract_action_elements(panel)
    assert extracted[0]["label_category"] == mod.LABEL_CATEGORY_INSTALLATION_EMAIL
    assert mod._is_download_candidate(extracted[0]) is False


def test_log_does_not_emit_raw_text():
    logger = DummyLogger()
    actions = [{
        "tag": "button",
        "type": "button",
        "id": "x",
        "name": "",
        "class": "",
        "role": "",
        "aria_label": "",
        "title": "",
        "data_testid": "",
        "href_host_path": "",
        "label_category": mod.LABEL_CATEGORY_DOWNLOAD,
    }]

    mod._log_actions(logger, actions)
    joined = "\n".join(logger.info_messages)
    assert "ダウンロード" not in joined
    assert "Download" not in joined
    assert "label_category=download" in joined


def test_revoke_and_installation_email_not_download_candidate():
    revoke = {"label_category": mod.LABEL_CATEGORY_REVOKE, "aria_label": "", "title": "", "data_testid": "", "href_host_path": ""}
    mail = {"label_category": mod.LABEL_CATEGORY_INSTALLATION_EMAIL, "aria_label": "", "title": "", "data_testid": "", "href_host_path": ""}
    assert mod._is_download_candidate(revoke) is False
    assert mod._is_download_candidate(mail) is False


def test_revoke_text_with_download_word_is_not_download():
    action = Node(tag="button", attrs={"title": "失効"}, text="失効 download")
    panel = Node(tag="section", children=[action])
    extracted = mod._extract_action_elements(panel)
    assert extracted[0]["label_category"] == mod.LABEL_CATEGORY_REVOKE
    assert mod._is_download_candidate(extracted[0]) is False


def test_class_dl_alone_is_not_download_candidate():
    action = {
        "label_category": mod.LABEL_CATEGORY_UNKNOWN,
        "aria_label": "",
        "title": "",
        "data_testid": "",
        "href_host_path": "",
        "class": "dl icon",
    }
    assert mod._is_download_candidate(action) is False


def test_type_submit_and_close_are_not_download_candidate():
    submit_action = Node(tag="button", attrs={"type": "submit", "title": "Download"}, text="Download")
    close_action = Node(tag="button", attrs={"aria-label": "Close"}, text="Close")
    panel = Node(tag="section", children=[submit_action, close_action])
    extracted = mod._extract_action_elements(panel)

    submit_item = [item for item in extracted if item["type"] == "submit"][0]
    close_item = [item for item in extracted if item["label_category"] == mod.LABEL_CATEGORY_CLOSE][0]

    assert submit_item["label_category"] != mod.LABEL_CATEGORY_DOWNLOAD
    assert close_item["label_category"] == mod.LABEL_CATEGORY_CLOSE
    assert mod._is_download_candidate(submit_item) is False
    assert mod._is_download_candidate(close_item) is False


def test_download_and_installation_email_mixed_results_in_one_download_candidate():
    download_action = Node(tag="button", attrs={"title": "Download"}, text="Download")
    mail_action = Node(tag="button", attrs={"data-testid": "send-installation-email-toolbar"}, text="ダウンロード")
    panel = Node(tag="section", children=[download_action, mail_action])

    extracted = mod._extract_action_elements(panel)
    download_candidates = [item for item in extracted if mod._is_download_candidate(item)]

    assert len(download_candidates) == 1
    assert download_candidates[0]["label_category"] == mod.LABEL_CATEGORY_DOWNLOAD


def test_main_clicks_row_once_only_when_single_result(monkeypatch):
    download = Node(tag="button", attrs={"title": "Download"}, text="Download")
    _logger, _browser, calls, _panel, _close, _note = _install_main_mocks(
        monkeypatch,
        result_count=1,
        detail_actions=[download],
    )

    rc = mod.main(["TEST_ALIAS"])
    assert rc == 0
    assert calls["row_click"] == 1


@pytest.mark.parametrize("result_count, expected", [(0, 2), (2, 3)])
def test_main_does_not_click_when_result_not_single(monkeypatch, result_count, expected):
    _logger, _browser, calls, _panel, _close, _note = _install_main_mocks(monkeypatch, result_count=result_count)

    rc = mod.main(["TEST_ALIAS"])
    assert rc == expected
    assert calls["row_click"] == 0


@pytest.mark.parametrize("rows", [[], [_make_row(), _make_row()]])
def test_main_row_count_invalid(monkeypatch, rows):
    _logger, _browser, calls, _panel, _close, _note = _install_main_mocks(monkeypatch, result_count=1, rows=rows)

    rc = mod.main(["TEST_ALIAS"])
    assert rc == 4
    assert calls["row_click"] == 0


def test_main_no_javascript_click_and_no_click_in_detail(monkeypatch):
    submit_btn = Node(tag="button", attrs={"type": "submit"}, text="Save")
    download_btn = Node(tag="button", attrs={"title": "Download"}, text="Download")
    _logger, browser, calls, _panel, close_btn, _note = _install_main_mocks(
        monkeypatch,
        result_count=1,
        detail_actions=[submit_btn, download_btn],
    )

    rc = mod.main(["TEST_ALIAS"])
    assert rc == 0
    assert browser.driver.execute_script_called == 0
    assert calls["row_click"] == 1
    assert close_btn.click_count == 0
    assert submit_btn.click_count == 0
    assert download_btn.click_count == 0


def test_main_installation_email_button_is_not_clicked(monkeypatch):
    download_btn = Node(tag="button", attrs={"title": "Download"}, text="Download")
    mail_btn = Node(tag="button", attrs={"data-testid": "send-installation-email-toolbar"}, text="Download")
    _logger, _browser, calls, _panel, _close, _note = _install_main_mocks(
        monkeypatch,
        result_count=1,
        detail_actions=[download_btn, mail_btn],
    )

    rc = mod.main(["TEST_ALIAS"])
    assert rc == 0
    assert calls["row_click"] == 1
    assert download_btn.click_count == 0
    assert mail_btn.click_count == 0


@pytest.mark.parametrize("actions, expected", [
    ([], 6),
    ([Node(tag="button", attrs={"title": "Download"}, text="Download")], 0),
    ([Node(tag="button", attrs={"title": "Download"}, text="Download"), Node(tag="a", attrs={"href": "https://admin.auth.hennge.com/certificates/download"}, text="Download")], 7),
])
def test_main_download_candidate_counts(monkeypatch, actions, expected):
    _logger, _browser, _calls, _panel, _close, _note = _install_main_mocks(monkeypatch, result_count=1, detail_actions=actions)
    rc = mod.main(["TEST_ALIAS"])
    assert rc == expected


def test_main_stale_retry_once(monkeypatch):
    download = Node(tag="button", attrs={"title": "Download"}, text="Download")
    _logger, _browser, calls, _panel, _close, _note = _install_main_mocks(monkeypatch, result_count=1, detail_actions=[download], stale_once=True)

    rc = mod.main(["TEST_ALIAS"])
    assert rc == 0
    assert calls["submit"] == 2
    assert calls["find_single"] == 1


def test_main_no_alias_or_query_in_log(monkeypatch):
    download = Node(tag="a", attrs={"href": "https://admin.auth.hennge.com/certificates/download?token=secret#x"}, text="Download")
    logger, _browser, _calls, _panel, _close, _note = _install_main_mocks(monkeypatch, result_count=1, detail_actions=[download])

    rc = mod.main(["SECRET_ALIAS"])
    assert rc == 0
    joined = "\n".join(logger.info_messages + logger.error_messages + logger.exception_messages)
    assert "SECRET_ALIAS" not in joined
    assert "?token=" not in joined


def test_main_input_value_not_accessed(monkeypatch):
    class NoteInput(Node):
        def get_attribute(self, name):
            if name == "value":
                raise AssertionError("value should not be accessed")
            return super().get_attribute(name)

    note = NoteInput(tag="input", attrs={"name": "note", "id": "note-field"})
    download = Node(tag="button", attrs={"title": "Download"}, text="Download")
    html, panel, close_btn, _ = _build_detail_dom(detail_actions=[download])
    # replace note in panel
    panel.children = [c for c in panel.children if not (c.tag_name == "input" and (c.get_attribute("name") or "") == "note")]
    panel.add_child(note)

    rows = [_make_row()]
    driver = Driver(root=html, rows=rows)
    browser = DummyBrowser(None, None, driver=driver)
    logger = DummyLogger()

    monkeypatch.setattr(mod, "load_config", lambda: {})
    monkeypatch.setattr(mod, "ensure_directories", lambda _config: None)
    monkeypatch.setattr(mod, "AppLogger", lambda _base_dir: logger)
    monkeypatch.setattr(mod, "Browser", lambda _base_dir, _config: browser)
    monkeypatch.setattr(mod, "HenngeHandler", DummyHandler)
    monkeypatch.setattr(mod.cert_search, "_wait_certificate_search_input_ready", lambda *_args, **_kwargs: Node(tag="input", attrs={"name": "query", "aria-label": "Search"}))
    monkeypatch.setattr(mod.cert_search, "_set_query_and_submit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mod.cert_search, "_wait_results_ready", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(mod.cert_search, "_find_single_visible_search_input", lambda *_args, **_kwargs: Node(tag="input", attrs={"name": "query", "aria-label": "Search"}))
    monkeypatch.setattr(mod.cert_search, "_log_result_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        mod,
        "_wait_detail_ready",
        lambda *_args, **_kwargs: {
            "path_changed": False,
            "has_heading": True,
            "has_surface": False,
            "detail_area": panel,
            "detail_method": mod.DETAIL_METHOD_CLOSE_COMMON,
            "close_count": 1,
            "note_count": 1,
            "mail_toolbar_count": 1,
            "dialog_count": 0,
            "detail_area_found": True,
        },
    )

    rc = mod.main(["TEST_ALIAS"])
    assert rc == 0
    assert close_btn.click_count == 0


def test_main_no_html_saved(monkeypatch):
    download = Node(tag="button", attrs={"title": "Download"}, text="Download")
    logger, _browser, _calls, _panel, _close, _note = _install_main_mocks(monkeypatch, result_count=1, detail_actions=[download])
    rc = mod.main(["TEST_ALIAS"])
    assert rc == 0
    assert all(call[2] is False for call in logger.diag_calls)


def test_main_updates_not_executed(monkeypatch):
    update_like = Node(tag="button", attrs={"title": "更新"}, text="更新")
    _logger, _browser, calls, _panel, _close, _note = _install_main_mocks(monkeypatch, result_count=1, detail_actions=[update_like])

    rc = mod.main(["TEST_ALIAS"])
    assert rc == 6
    assert calls["row_click"] == 1
    assert update_like.click_count == 0


def test_detail_not_ready_or_area_not_found_returns_5(monkeypatch):
    _logger, _browser, _calls, _panel, _close, _note = _install_main_mocks(
        monkeypatch,
        detail_ready=RuntimeError("詳細画面へ遷移しませんでした"),
    )
    assert mod.main(["TEST_ALIAS"]) == 5

    _logger2, _browser2, _calls2, _panel2, _close2, _note2 = _install_main_mocks(
        monkeypatch,
        detail_ready=RuntimeError("詳細領域を特定できませんでした"),
    )
    assert mod.main(["TEST_ALIAS"]) == 5


def _install_fake_clock(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(mod.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(mod.time, "sleep", clock.sleep)
    return clock


def test_wait_detail_ready_does_not_succeed_with_list_heading_only(monkeypatch):
    _install_fake_clock(monkeypatch)
    driver = WaitSignalDriver(
        heading_texts=["証明書一覧"] * 20,
        selector_counts={
            "button[aria-label='Close']": [0] * 20,
            "input[name='note']": [0] * 20,
            "button[data-testid='send-installation-email-toolbar']": [0] * 20,
            "[role='dialog']": [0] * 20,
            "[class*='drawer']": [0] * 20,
            "[data-testid*='drawer']": [0] * 20,
            "[aria-modal='true']": [0] * 20,
        },
    )
    logger = DummyLogger()
    monkeypatch.setattr(mod, "_identify_detail_area", lambda _driver: (_ for _ in ()).throw(RuntimeError("詳細領域を特定できませんでした")))

    with pytest.raises(RuntimeError, match="詳細画面へ遷移しませんでした"):
        mod._wait_detail_ready(driver, "/certificates/", logger, timeout_seconds=2)


def test_wait_detail_ready_close_only_or_note_only_keeps_waiting(monkeypatch):
    _install_fake_clock(monkeypatch)
    driver = WaitSignalDriver(
        selector_counts={
            "button[aria-label='Close']": [1, 1, 0, 0, 0, 0, 0, 0],
            "input[name='note']": [0, 0, 1, 1, 0, 0, 0, 0],
            "button[data-testid='send-installation-email-toolbar']": [0] * 8,
            "[role='dialog']": [0] * 8,
            "[class*='drawer']": [0] * 8,
            "[data-testid*='drawer']": [0] * 8,
            "[aria-modal='true']": [0] * 8,
        },
    )
    logger = DummyLogger()
    monkeypatch.setattr(mod, "_identify_detail_area", lambda _driver: (_ for _ in ()).throw(RuntimeError("詳細領域を特定できませんでした")))

    with pytest.raises(RuntimeError, match="詳細画面へ遷移しませんでした"):
        mod._wait_detail_ready(driver, "/certificates/", logger, timeout_seconds=2)


def test_wait_detail_ready_succeeds_with_close_and_note(monkeypatch):
    _install_fake_clock(monkeypatch)
    driver = WaitSignalDriver(
        selector_counts={
            "button[aria-label='Close']": [0, 1],
            "input[name='note']": [0, 1],
            "button[data-testid='send-installation-email-toolbar']": [0, 0],
            "[role='dialog']": [0, 0],
            "[class*='drawer']": [0, 0],
            "[data-testid*='drawer']": [0, 0],
            "[aria-modal='true']": [0, 0],
        },
    )
    logger = DummyLogger()
    area = Node(tag="section")
    monkeypatch.setattr(mod, "_identify_detail_area", lambda _driver: (area, mod.DETAIL_METHOD_CLOSE_COMMON))

    state = mod._wait_detail_ready(driver, "/certificates/", logger, timeout_seconds=2)
    assert state["detail_area"] is area
    assert state["detail_method"] == mod.DETAIL_METHOD_CLOSE_COMMON


def test_wait_detail_ready_succeeds_with_close_and_mail_toolbar(monkeypatch):
    _install_fake_clock(monkeypatch)
    driver = WaitSignalDriver(
        selector_counts={
            "button[aria-label='Close']": [1],
            "input[name='note']": [0],
            "button[data-testid='send-installation-email-toolbar']": [1],
            "[role='dialog']": [0],
            "[class*='drawer']": [0],
            "[data-testid*='drawer']": [0],
            "[aria-modal='true']": [0],
        },
    )
    logger = DummyLogger()
    area = Node(tag="section")
    monkeypatch.setattr(mod, "_identify_detail_area", lambda _driver: (area, mod.DETAIL_METHOD_CLOSE_COMMON))

    state = mod._wait_detail_ready(driver, "/certificates/", logger, timeout_seconds=2)
    assert state["detail_area"] is area


def test_wait_detail_ready_succeeds_with_dialog(monkeypatch):
    _install_fake_clock(monkeypatch)
    driver = WaitSignalDriver(
        selector_counts={
            "button[aria-label='Close']": [1],
            "input[name='note']": [0],
            "button[data-testid='send-installation-email-toolbar']": [0],
            "[role='dialog']": [1],
            "[class*='drawer']": [0],
            "[data-testid*='drawer']": [0],
            "[aria-modal='true']": [0],
        },
    )
    logger = DummyLogger()
    area = Node(tag="div", attrs={"role": "dialog"})
    monkeypatch.setattr(mod, "_identify_detail_area", lambda _driver: (area, mod.DETAIL_METHOD_DIALOG))

    state = mod._wait_detail_ready(driver, "/certificates/", logger, timeout_seconds=2)
    assert state["detail_method"] == mod.DETAIL_METHOD_DIALOG


def test_main_uses_returned_detail_area_without_refetch(monkeypatch):
    rows = [_make_row()]
    html, panel, _close, _note = _build_detail_dom(detail_actions=[Node(tag="button", attrs={"title": "Download"}, text="Download")])
    driver = Driver(root=html, rows=rows)
    browser = DummyBrowser(None, None, driver=driver)
    logger = DummyLogger()

    monkeypatch.setattr(mod, "load_config", lambda: {})
    monkeypatch.setattr(mod, "ensure_directories", lambda _config: None)
    monkeypatch.setattr(mod, "AppLogger", lambda _base_dir: logger)
    monkeypatch.setattr(mod, "Browser", lambda _base_dir, _config: browser)
    monkeypatch.setattr(mod, "HenngeHandler", DummyHandler)
    monkeypatch.setattr(mod.cert_search, "_wait_certificate_search_input_ready", lambda *_args, **_kwargs: Node(tag="input", attrs={"name": "query", "aria-label": "Search"}))
    monkeypatch.setattr(mod.cert_search, "_set_query_and_submit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mod.cert_search, "_wait_results_ready", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(mod.cert_search, "_find_single_visible_search_input", lambda *_args, **_kwargs: Node(tag="input", attrs={"name": "query", "aria-label": "Search"}))
    monkeypatch.setattr(mod.cert_search, "_log_result_state", lambda *_args, **_kwargs: None)

    returned_area = Node(tag="section", children=[Node(tag="button", attrs={"title": "Download"}, text="Download")])
    monkeypatch.setattr(
        mod,
        "_wait_detail_ready",
        lambda *_args, **_kwargs: {
            "path_changed": False,
            "has_heading": True,
            "has_surface": False,
            "detail_area": returned_area,
            "detail_method": mod.DETAIL_METHOD_CLOSE_COMMON,
            "close_count": 1,
            "note_count": 1,
            "mail_toolbar_count": 0,
            "dialog_count": 0,
            "detail_area_found": True,
        },
    )

    def fail_identify(_driver):
        raise AssertionError("_identify_detail_area must not be called after wait success")

    monkeypatch.setattr(mod, "_identify_detail_area", fail_identify)

    rc = mod.main(["TEST_ALIAS"])
    assert rc == 0
