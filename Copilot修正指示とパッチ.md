# VS Code Copilot向け修正指示書と提案パッチ

## 目的

このプロジェクトは、SeleniumでMicrosoft Edgeを操作し、HENNGE管理画面から証明書を取得してSMSMへ登録し、IMEIを紐づける自動化ツールです。

今回の最優先課題は次のとおりです。

1. HENNGE認証中に表示されるクライアント証明書選択ダイアログを、Edgeの正式な管理ポリシーで自動選択できる構成にする。
2. HENNGEとSMSMを同じタブで上書きせず、個別タブで管理する。
3. 操作失敗を握りつぶさず、失敗時に処理を停止して診断情報を保存する。
4. ダミーPFXを作成しない。
5. SMSMへのアップロード、IMEI入力、保存が未実施でも「処理成功」になる問題を解消する。
6. `settings.json` の平文パスワード依存を減らす。

---

# Copilot Chatへ貼り付けるプロンプト

以下をそのままVS CodeのCopilot Chatへ貼り付けてください。

```text
このワークスペースのPythonプロジェクトを修正してください。

最初に関連ファイルをすべて読み、現在の呼び出し関係を確認してください。対象は少なくとも次です。

- app/browser.py
- app/config.py
- app/hennge_handler.py
- app/smsm_handler.py
- app/file_handler.py
- app/logger.py
- app/main.py
- app/excel_reader.py
- config/settings.json
- requirements.txt

重要な制約:

1. 現在のファイルを直接一括変更する前に、変更計画と対象ファイル一覧を提示すること。
2. 変更は小さな単位に分け、各段階で構文エラーがないことを確認すること。
3. 実在システムへの登録、証明書アップロード、IMEI更新は実行しないこと。
4. WindowsレジストリをPythonから自動変更しないこと。
5. EdgeのAutoSelectCertificateForUrlsは組織管理ポリシーとして外部設定される前提にすること。
6. 個人用Edgeプロファイルを自動化プロファイルとして流用しないこと。
7. ID、パスワード、Cookie、秘密鍵、PFX内容をログへ出力しないこと。
8. 既存のsettings.jsonに実パスワードがある場合、内容を表示しないこと。

修正要件:

A. app/browser.py

- `--auto-select-certificate-for-urls=...` 起動引数に依存しないこと。
- Edge正式ポリシー `AutoSelectCertificateForUrls` は端末管理側で設定する前提にすること。
- 起動時に証明書ルールの想定値をログ可能な文字列として生成できる補助メソッドは残してよいが、秘密情報は含めないこと。
- 新規タブを開く `open_new_tab(url) -> str` を追加すること。
- タブを切り替える `switch_to(handle)` を追加すること。
- `current_handle()` を追加すること。
- `capture_state()` または同等機能で current_url、title、window handleを安全に取得できるようにすること。
- Edge起動後、少なくとも1つのウィンドウハンドルがあることを確認すること。

B. app/logger.py

- `exception(message)` を追加し、スタックトレースを保存できるようにすること。
- `save_screenshot(None, ...)` の場合に0バイトPNGを作らないこと。
- `save_browser_diagnostics(driver, name)` を追加すること。
- 診断情報としてURL、タイトル、スクリーンショット、HTMLを保存すること。
- ファイル名には日時を付け、既存ファイルを上書きしないこと。
- HTMLやログへパスワードを意図的に書かないこと。

C. app/hennge_handler.py

- URLをクラス定数にすること。
- ログインボタンが見つからない場合は例外にすること。
- ID欄、パスワード欄、ログインボタンの操作結果をboolで返し、必須操作失敗時は処理を止めること。
- `except Exception: return` や `except Exception: pass` による握りつぶしをやめること。
- ドメイン画面判定から単独の曖昧な文字列 `domain` を除くこと。
- ログイン成功判定は、実画面で確認できるURLまたは固有要素に限定すること。曖昧な `user` や `certificate` の部分一致だけで成功判定しないこと。
- ログイン失敗時に `save_browser_diagnostics` を呼べる構造にすること。
- 証明書ダウンロードに失敗した場合、`placeholder` 内容の偽PFXを作らず例外にすること。
- ダウンロード開始前のファイル一覧と開始後の一覧を比較し、新規 `.pfx` または `.p12` が安定したサイズになるまで待機すること。
- ダウンロード完了を検証してからPathを返すこと。

D. app/smsm_handler.py

- ログイン成功を管理画面固有の要素またはURLで確認すること。
- 入力欄、ログインボタン、アップロード欄、検索欄、IMEI欄、保存ボタンが見つからない場合は例外にすること。
- 操作をスキップしたのに正常終了しないこと。
- `search_input` の未定義参照を解消すること。
- `//*[contains(...)]` のような広すぎるクリック候補を減らし、a、button、inputなどの操作可能要素へ限定すること。
- アップロード後、成功メッセージまたは登録結果を確認すること。
- IMEI保存後、成功メッセージまたは更新結果を確認すること。

E. app/file_handler.py

- `rename_to_imei` が実際にはコピーである点を整理すること。
- 互換性のため公開メソッド名を残してもよいが、内部では安全なコピー処理にすること。
- 入力ファイルの存在、通常ファイル、0バイトでないこと、拡張子が `.pfx` または `.p12` であることを検証すること。
- IMEIが数字だけであることを検証すること。15桁固定はプロジェクト要件に合う場合のみ有効化すること。
- 削除対象を拡張子付きの実際の出力パスにすること。
- 入力元と出力先が同じ場合はコピーせずそのまま返すこと。

F. app/main.py

- HENNGEログイン後のタブハンドルを保存すること。
- SMSMは新規タブで開いてログインし、タブハンドルを保存すること。
- HENNGE操作前にHENNGEタブへ切り替えること。
- SMSM操作前にSMSMタブへ切り替えること。
- 対象単位の失敗時に `browser.driver` を渡して診断情報を保存すること。
- 全体例外でも診断情報を保存すること。
- `logger.exception()` で行番号とスタックトレースを残すこと。
- HENNGE、SMSM、ファイル検証のどれかが失敗した場合に「処理成功」と出さないこと。
- 最後に成功件数、失敗件数を記録すること。

G. app/excel_reader.py

- workbookをfinallyで必ずcloseすること。
- 対象シートがない場合、分かりやすい例外にすること。
- alias、serial、imeiの必須項目不足を行番号付きで報告すること。
- IMEIがfloatで整数値なら末尾の `.0` を除去すること。
- IMEIに数字以外が含まれる場合は検証エラーにすること。

H. config/settings.jsonと資格情報

- `settings.json` の値は後方互換用として読み込めるようにしてよい。
- 環境変数 `HENNGE_USERNAME`、`HENNGE_PASSWORD`、`SMSM_USERNAME`、`SMSM_PASSWORD` があれば優先すること。
- パスワードをログへ出さないこと。
- 証明書フィルターは最初の切り分けではIssuer CNだけを使える構成にすること。
- 重複している `issuer_cn`、`subject_cn` は非推奨として扱い、`issuer`、`subject` に統一すること。

I. 証明書ポリシー

Windowsレジストリをコードから変更しないでください。READMEまたは新規ドキュメントへ、Edge管理ポリシーの設定例を記載してください。

設定先候補:
HKEY_CURRENT_USER\\SOFTWARE\\Policies\\Microsoft\\Edge\\AutoSelectCertificateForUrls

値名:
1

種類:
REG_SZ

切り分け用の値の例:
{"pattern":"https://ap.ssso.hdems.com","filter":{"ISSUER":{"CN":"Cybertrust DeviceiD Public CA G3h"}}}

すべてのEdgeを終了してから再起動し、edge://policy で `AutoSelectCertificateForUrls` が表示されることを確認する手順も記載してください。

J. テスト

- 外部サイトへ接続しない単体テストを追加してください。
- Browserのタブ管理はモックでテストしてください。
- FileHandlerは一時ディレクトリで正常系と異常系をテストしてください。
- ExcelReaderは一時xlsxを作り、正常行、必須項目不足、数値IMEIをテストしてください。
- ダミーPFX作成処理が残っていないことを検索で確認してください。
- `python -m compileall app` を実行してください。
- 可能なら `pytest` を追加して実行してください。

以下の提案パッチを参考にしてください。ただし、ワークスペース内の最新コードと一致しない箇所は無理に適用せず、要件を満たす形で調整してください。

最後に次を報告してください。

1. 変更したファイル一覧
2. 各ファイルの変更理由
3. 実行した検証コマンド
4. 検証結果
5. 手動確認が必要な事項
6. Edgeポリシー設定後の確認手順
7. 実システムに接続する前の安全確認項目
```

---

# 提案パッチ

> 注意: このパッチは、現在確認できたファイル内容を基準にした安全化のたたき台です。VS Code Copilotには、ワークスペースの最新版と比較して調整させてください。

```diff
*** Begin Patch
*** Update File: app/logger.py
@@
 import logging
 from pathlib import Path
 from datetime import datetime
+
 class AppLogger:
@@
     def error(self, message: str) -> None:
         self.logger.error(message)
+
+    def exception(self, message: str) -> None:
+        self.logger.exception(message)
+
     def save_screenshot(self, driver, name: str) -> None:
-        path = self.screenshots_dir / f"{name}.png"
         if driver is None:
-            path.write_bytes(b"")
-            self.info(f"スクリーンショットを保存できないため空ファイルを作成: {path}")
+            self.error(f"スクリーンショットを保存できません: driver=None, name={name}")
             return
-        driver.save_screenshot(str(path))
-        self.info(f"スクリーンショットを保存: {path}")
+        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
+        path = self.screenshots_dir / f"{name}_{timestamp}.png"
+        try:
+            driver.save_screenshot(str(path))
+            self.info(f"スクリーンショットを保存: {path}")
+        except Exception:
+            self.exception(f"スクリーンショット保存に失敗: {path}")
+
+    def save_browser_diagnostics(self, driver, name: str) -> None:
+        if driver is None:
+            self.error(f"ブラウザー診断情報を保存できません: driver=None, name={name}")
+            return
+
+        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
+
+        try:
+            self.info(f"診断時URL: {driver.current_url}")
+        except Exception:
+            self.exception("診断時URLの取得に失敗")
+
+        try:
+            self.info(f"診断時タイトル: {driver.title}")
+        except Exception:
+            self.exception("診断時タイトルの取得に失敗")
+
+        self.save_screenshot(driver, name)
+
+        html_path = self.screenshots_dir / f"{name}_{timestamp}.html"
+        try:
+            html_path.write_text(driver.page_source or "", encoding="utf-8")
+            self.info(f"HTMLを保存: {html_path}")
+        except Exception:
+            self.exception(f"HTML保存に失敗: {html_path}")
*** Update File: app/browser.py
@@
 import os
 import json
@@
     def start(self) -> None:
@@
-        cert_rule = self._build_auto_select_cert_rule()
-        if cert_rule:
-            options.add_argument(f"--auto-select-certificate-for-urls={cert_rule}")
+        # クライアント証明書の自動選択は、Edgeの正式な
+        # AutoSelectCertificateForUrls管理ポリシーで設定する。
+        # コマンドライン引数には依存しない。
         self.driver = webdriver.Edge(options=options)
+        if not self.driver.window_handles:
+            raise RuntimeError("Edgeのウィンドウハンドルを取得できませんでした")
         self.started = True
@@
     def open(self, url: str) -> None:
@@
         self.driver.get(url)
+
+    def current_handle(self) -> str:
+        if self.driver is None:
+            raise RuntimeError("ブラウザが開始されていません")
+        return self.driver.current_window_handle
+
+    def open_new_tab(self, url: str) -> str:
+        if self.driver is None or not self.started:
+            raise RuntimeError("ブラウザが開始されていません")
+        self.driver.switch_to.new_window("tab")
+        self.driver.get(url)
+        return self.driver.current_window_handle
+
+    def switch_to(self, handle: str) -> None:
+        if self.driver is None or not self.started:
+            raise RuntimeError("ブラウザが開始されていません")
+        if handle not in self.driver.window_handles:
+            raise RuntimeError(f"指定されたタブが存在しません: {handle}")
+        self.driver.switch_to.window(handle)
+
+    def capture_state(self) -> dict:
+        if self.driver is None:
+            return {"started": False}
+        state = {"started": self.started}
+        for key, getter in {
+            "url": lambda: self.driver.current_url,
+            "title": lambda: self.driver.title,
+            "handle": lambda: self.driver.current_window_handle,
+        }.items():
+            try:
+                state[key] = getter()
+            except Exception as exc:
+                state[key] = f"取得失敗: {type(exc).__name__}"
+        return state
*** Update File: app/file_handler.py
@@
 from pathlib import Path
+import shutil
+
 class FileHandler:
@@
     def rename_to_imei(self, downloaded_file: Path, imei: str) -> Path:
+        if not downloaded_file.exists():
+            raise FileNotFoundError(f"ダウンロード済みファイルが見つかりません: {downloaded_file}")
+        if not downloaded_file.is_file():
+            raise RuntimeError(f"対象が通常ファイルではありません: {downloaded_file}")
+        if downloaded_file.stat().st_size == 0:
+            raise RuntimeError(f"ダウンロードファイルが空です: {downloaded_file}")
+
+        suffix = downloaded_file.suffix.lower()
+        if suffix not in {".pfx", ".p12"}:
+            raise RuntimeError(f"想定外の証明書拡張子です: {suffix}")
+
+        imei_text = str(imei).strip()
+        if not imei_text or not imei_text.isdigit():
+            raise ValueError(f"IMEIが数字ではありません: {imei_text}")
+
         target_dir = self.base_dir / "downloads"
-        target_dir.mkdir(exist_ok=True)
-        # 既存同名ファイルがあれば削除
-        target_file = target_dir / f"{imei}"
-        if target_file.exists():
-            target_file.unlink()
-            self.logger.info(f"既存ファイルを削除: {target_file}")
-        # もし元ファイルが存在していれば、IMEI名のファイルとしてコピーする
-        if downloaded_file.exists():
-            new_path = target_dir / f"{imei}{downloaded_file.suffix}"
-            new_path.write_bytes(downloaded_file.read_bytes())
-            self.logger.info(f"ファイル名変更: {downloaded_file.name} -> {new_path.name}")
-            return new_path
-        raise FileNotFoundError(f"ダウンロード済みファイルが見つかりません: {downloaded_file}")
+        target_dir.mkdir(parents=True, exist_ok=True)
+        new_path = target_dir / f"{imei_text}{suffix}"
+
+        if downloaded_file.resolve() == new_path.resolve():
+            self.logger.info(f"証明書ファイルは既にIMEI名です: {new_path}")
+            return new_path
+
+        if new_path.exists():
+            new_path.unlink()
+            self.logger.info(f"既存ファイルを削除: {new_path}")
+
+        shutil.copy2(downloaded_file, new_path)
+        self.logger.info(f"証明書ファイルをコピー: {downloaded_file.name} -> {new_path.name}")
+        return new_path
*** Update File: app/hennge_handler.py
@@
 class HenngeHandler:
+    ADMIN_URL = "https://admin.auth.hennge.com"
@@
     def login(self) -> None:
@@
-        self.browser.open("https://admin.auth.hennge.com")
+        self.browser.open(self.ADMIN_URL)
@@
-        self._submit_login()
+        if not self._submit_login():
+            raise RuntimeError("HENNGEログイン失敗: ログインボタンを操作できませんでした")
@@
-    def _submit_login(self) -> None:
+    def _submit_login(self) -> bool:
@@
         try:
             self.browser.click_first(locators, timeout=3)
-            return
-        except Exception:
-            return
+            self.logger.info("HENNGEログインボタンをクリックしました")
+            return True
+        except Exception:
+            self.logger.exception("HENNGEログインボタンをクリックできませんでした")
+            return False
@@
         prompt_markers = [
             "ドメインを入力",
             "unknown domain",
-            "domain",
+            "enter your domain",
         ]
@@
     def download_certificate(self, alias: str, imei: str) -> Path:
@@
-        except Exception:
-            self.logger.info("ダウンロードボタンが見つからなかったため、ダウンロード済みプレースホルダーを作成します")
-        dummy_path = self.logger.base_dir / "downloads" / f"{imei}.pfx"
-        dummy_path.write_bytes(b"placeholder")
-        self.logger.info(f"ダウンロード済みファイルを作成: {dummy_path}")
-        return dummy_path
+        except Exception as exc:
+            self.logger.exception("証明書ダウンロードボタンを操作できませんでした")
+            raise RuntimeError("HENNGE証明書ダウンロード失敗") from exc
+
+        raise RuntimeError(
+            "証明書ダウンロード完了検証が未実装です。"
+            "ダウンロードフォルダー監視を実装してから本番実行してください"
+        )
*** Update File: app/excel_reader.py
@@
     def read_targets(self, sheet_name: str = "HENNGE登録作業必要情報") -> list[dict]:
@@
-        workbook = openpyxl.load_workbook(self.file_path, data_only=True)
-        sheet = workbook[sheet_name]
-        rows = []
-        for row in sheet.iter_rows(min_row=2, values_only=True):
-            alias = self._normalize_value(row[2]) if len(row) > 2 else ""
-            serial = self._normalize_value(row[3]) if len(row) > 3 else ""
-            imei = self._normalize_imei(row[4]) if len(row) > 4 else ""
-            if not alias and not serial and not imei:
-                continue
-            # Skip header-like rows that appear in some operational templates.
-            if self._is_header_row(alias, serial, imei):
-                continue
-            if self._is_placeholder_row(alias, serial, imei):
-                continue
-            rows.append({
-                "alias": alias,
-                "serial": serial,
-                "imei": imei,
-            })
-        workbook.close()
-        return rows
+        workbook = openpyxl.load_workbook(self.file_path, data_only=True)
+        try:
+            if sheet_name not in workbook.sheetnames:
+                raise KeyError(f"対象シートが見つかりません: {sheet_name}")
+            sheet = workbook[sheet_name]
+            rows = []
+            for row_number, row in enumerate(
+                sheet.iter_rows(min_row=2, values_only=True), start=2
+            ):
+                alias = self._normalize_value(row[2]) if len(row) > 2 else ""
+                serial = self._normalize_value(row[3]) if len(row) > 3 else ""
+                imei = self._normalize_imei(row[4]) if len(row) > 4 else ""
+                if not alias and not serial and not imei:
+                    continue
+                if self._is_header_row(alias, serial, imei):
+                    continue
+                if self._is_placeholder_row(alias, serial, imei):
+                    continue
+                missing = [
+                    name for name, value in {
+                        "alias": alias,
+                        "serial": serial,
+                        "imei": imei,
+                    }.items() if not value
+                ]
+                if missing:
+                    raise ValueError(
+                        f"Excel {row_number}行目の必須項目が不足: {', '.join(missing)}"
+                    )
+                rows.append({"alias": alias, "serial": serial, "imei": imei})
+            return rows
+        finally:
+            workbook.close()
@@
     def _normalize_imei(value) -> str:
-        text = ExcelReader._normalize_value(value)
-        return re.sub(r"\s+", "", text)
+        if value is None:
+            return ""
+        if isinstance(value, int):
+            text = str(value)
+        elif isinstance(value, float):
+            if not value.is_integer():
+                raise ValueError(f"IMEIが整数ではありません: {value}")
+            text = str(int(value))
+        else:
+            text = re.sub(r"\s+", "", str(value).strip())
+        if text and not text.isdigit():
+            raise ValueError(f"IMEIに数字以外が含まれています: {text}")
+        return text
*** Update File: app/main.py
@@
     try:
@@
         emit("HENNGEにログインします")
         hennge_handler.login()
+        hennge_handle = browser.current_handle()
         emit("SMSMにログインします")
+        smsm_handle = browser.open_new_tab("https://ausl.smartmanager.jp")
         smsm_handler.login()
+        success_count = 0
+        failure_count = 0
         for idx, target in enumerate(targets, start=1):
@@
             try:
+                browser.switch_to(hennge_handle)
                 emit("HENNGEでユーザーを検索します")
@@
                 renamed_file = file_handler.rename_to_imei(downloaded_file, target["imei"])
+                browser.switch_to(smsm_handle)
                 emit("SMSMへ証明書をアップロードします")
@@
                 emit("処理成功")
+                success_count += 1
             except Exception as exc:
+                failure_count += 1
                 emit(f"処理失敗: {exc}")
+                logger.exception(f"対象{idx}の処理に失敗しました")
                 try:
-                    logger.save_screenshot(None, f"error_{idx}")
-                except Exception:
-                    emit("スクリーンショット保存をスキップしました")
-        emit("処理を終了します")
+                    logger.save_browser_diagnostics(browser.driver, f"error_{idx}")
+                except Exception as diag_exc:
+                    emit(f"診断情報保存に失敗しました: {diag_exc}")
+        emit(f"処理を終了します: 成功={success_count}, 失敗={failure_count}")
     except Exception as exc:
         emit(f"処理を中断しました: {exc}")
+        logger.exception("全体処理を中断しました")
+        try:
+            logger.save_browser_diagnostics(browser.driver, "fatal_error")
+        except Exception as diag_exc:
+            emit(f"診断情報保存に失敗しました: {diag_exc}")
*** Update File: app/smsm_handler.py
@@
     def login(self) -> None:
@@
-        except Exception:
-            pass
+        except Exception as exc:
+            self.logger.exception("SMSMのログイン操作に失敗しました")
+            raise RuntimeError("SMSMログイン失敗") from exc
@@
     def upload_certificate(self, target: dict) -> None:
@@
-        # 広い範囲のXPathクリック
-        element = self.driver.find_element(By.XPATH, "//*[contains(text(), '保存')]")
+        # 操作可能な要素（button, input, a）に限定した具体的な取得
+        element = self.driver.find_element(By.XPATH, "//button[contains(text(), '保存')]")
@@
-        # 処理完了
-        return
+        # 成功メッセージの確認（例）
+        try:
+            success_msg = self.driver.find_element(By.CLASS_NAME, "alert-success")
+            self.logger.info(f"保存成功を確認: {success_msg.text}")
+        except Exception as exc:
+            self.logger.exception("SMSMでの保存成功メッセージが確認できませんでした")
+            raise RuntimeError("SMSMアップロードまたは保存失敗") from exc
*** End Patch
```

---

# 重要な補足

## 1. パッチのHENNGEダウンロード部分について

提案パッチでは、偽PFXの生成を確実に止めるため、ダウンロード完了監視が実装されるまで例外にしています。Copilotには、次の条件を満たす実装へ置き換えさせてください。

- クリック前の `downloads` フォルダー一覧を記録する。
- クリック後に新規作成された `.pfx` または `.p12` を検出する。
- `.crdownload` や一時ファイルを除外する。
- ファイルサイズが複数回連続して同じになるまで待つ。
- タイムアウト時は例外にする。
- 新規ファイルが複数ある場合は例外にする。

実装のぶれを減らすため、次のスニペットをそのまま参照させてください。

```python
import time
from pathlib import Path


def wait_for_download(download_dir: Path, timeout: int = 30) -> Path:
     end_time = time.time() + timeout
     while time.time() < end_time:
          files = list(download_dir.glob("*"))
          target_files = [
               f for f in files
               if f.suffix.lower() in {".pfx", ".p12"}
               and not f.name.endswith(".crdownload")
               and not f.name.endswith(".tmp")
          ]

          if target_files:
               latest_file = max(target_files, key=lambda p: p.stat().st_ctime)
               initial_size = latest_file.stat().st_size
               time.sleep(1)
               if initial_size > 0 and initial_size == latest_file.stat().st_size:
                    return latest_file

          time.sleep(1)

     raise TimeoutError("証明書のダウンロードがタイムアウトしました")
```

## 2. Edgeポリシーはコード修正だけでは完了しない

コードからレジストリを変更せず、組織管理者または端末管理者の承認を受けて設定してください。

設定確認手順:

1. すべてのEdgeを終了する。
2. ポリシーを設定する。
3. Edgeを起動する。
4. `edge://policy` を開く。
5. `ポリシーを再読み込み` を実行する。
6. `AutoSelectCertificateForUrls` がエラーなしで表示されることを確認する。
7. 証明書選択が発生する実際のホスト名とpatternが一致することを確認する。

## 3. 切り分け用の証明書フィルター

最初はIssuer CNだけで確認してください。

```json
{
  "pattern": "https://ap.ssso.hdems.com",
  "filter": {
    "ISSUER": {
      "CN": "Cybertrust DeviceiD Public CA G3h"
    }
  }
}
```

複数証明書が一致する場合だけSubject CNを追加します。`O` と `C` は、実証明書との完全一致を確認できるまで外します。

## 4. 実行前チェック

- [ ] `settings.json` の実パスワードを変更または削除した。
- [ ] 環境変数または資格情報管理へ移行した。
- [ ] `edge://policy` に自動選択ポリシーが表示される。
- [ ] 対象証明書が有効期限内で秘密鍵を持つ。
- [ ] 同じ条件に一致する証明書が複数ない。
- [ ] HENNGEとSMSMが別タブで開く。
- [ ] ダミーPFX作成コードが存在しない。
- [ ] アップロード失敗や保存失敗が「処理成功」にならない。
- [ ] 診断用スクリーンショットが0バイトではない。
- [ ] テスト用アカウントとテスト用端末で確認する。
- [ ] 本番登録前に1件だけ手動監視付きで実行する。

## 5. Copilotへ追加で指示するとよい確認コマンド

```powershell
python -m compileall app
```

```powershell
python -m pytest -q
```

```powershell
Get-ChildItem -Recurse -File | Select-String -Pattern "placeholder|save_screenshot\(None|except Exception:\s*(pass|return)"
```

```powershell
Get-ChildItem Cert:\CurrentUser\My |
    Select-Object Subject, Issuer, Thumbprint, NotBefore, NotAfter, HasPrivateKey |
    Format-List
```
