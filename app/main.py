import os
import time
from pathlib import Path
import hashlib

from app.config import load_config, ensure_directories
from app.excel_reader import ExcelReader
from app.excel_session import save_and_close_target_workbook
from app.logger import AppLogger
from app.hennge_handler import HenngeHandler
from app.smsm_handler import SmsmHandler
from app.smsm_config import resolve_smsm_config
from app.file_handler import FileHandler
from app.browser import Browser
from app.progress import ProgressWindow


def reopen_excel(path: Path, emit) -> None:
    if not path.exists():
        emit("Excel再起動をスキップしました(ファイルなし)")
        return

    try:
        os.startfile(str(path))
        emit("Excelファイルを起動しました")
    except Exception as exc:
        emit(f"Excelファイルの起動に失敗しました: {type(exc).__name__}")


def wait_excel_unlock(reader: ExcelReader, emit, timeout_sec: float = 15.0, interval_sec: float = 0.5) -> None:
    emit("ロック解除待機開始")
    end_time = time.monotonic() + timeout_sec
    while time.monotonic() < end_time:
        if not reader.is_file_open():
            emit("ロック解除完了")
            return
        time.sleep(interval_sec)
    raise RuntimeError("Excelファイルのロック解除がタイムアウトしました")


def main() -> None:
    base_dir = Path(__file__).resolve().parent.parent
    logger = AppLogger(base_dir)
    progress = ProgressWindow()
    progress.start()

    def emit(message: str) -> None:
        logger.info(message)
        progress.update(message)

    browser = None
    reopen_path = None
    should_reopen_excel = False

    try:
        config = load_config()
        ensure_directories(config)

        browser = Browser(base_dir, config)
        emit("処理を開始します")

        excel_path = config.get("excel", {}).get("path", "")
        reopen_path = Path(excel_path) if excel_path else None
        should_reopen_excel = bool(reopen_path)
        targets: list[dict] = []

        if not excel_path:
            emit("Excelパスが設定されていないため、画面確認モードで実行します")
            emit("Edgeブラウザを起動します")
            browser.start()
            emit("Edgeブラウザを起動しました")
            try:
                browser.open("https://admin.auth.hennge.com")
                browser.wait_for_page_ready()
                emit("HENNGEログイン画面を表示しました")
            except Exception as exc:
                emit(f"HENNGE画面表示失敗: {exc}")

            try:
                browser.open("https://ausl.smartmanager.jp")
                browser.wait_for_page_ready()
                emit("SMSMログイン画面を表示しました")
            except Exception as exc:
                emit(f"SMSM画面表示失敗: {exc}")

            emit("画面確認モードを終了します")
            return

        if reopen_path is None:
            raise RuntimeError("Excelパスの解決に失敗しました")
        if not reopen_path.exists():
            raise FileNotFoundError("対象Excelファイルが見つかりません")

        reader = ExcelReader(excel_path)
        try:
            save_and_close_target_workbook(reopen_path.resolve(), logger)
        except Exception as exc:
            logger.error(f"失敗段階=save_and_close_target_workbook, 例外型={type(exc).__name__}")
            raise

        wait_excel_unlock(reader, emit)

        emit("Excel読み込み開始")
        targets = reader.read_targets()
        emit(f"対象件数: {len(targets)}")

        emit("Edgeブラウザを起動します")
        browser.start()
        emit("Edgeブラウザを起動しました")

        hennge_handler = HenngeHandler(config, logger, browser)
        smsm_config = resolve_smsm_config(config)
        smsm_handler = SmsmHandler(browser=browser, logger=logger, smsm_config=smsm_config)
        file_handler = FileHandler(base_dir, logger)

        emit("HENNGEにログインします")
        hennge_handler.login()
        hennge_handle = browser.current_handle()
        emit("SMSMにログインします")
        smsm_handle = browser.open_new_tab("https://ausl.smartmanager.jp")
        smsm_handler.login()

        success_count = 0
        failure_count = 0

        for idx, target in enumerate(targets, start=1):
            alias_fingerprint = hashlib.sha256(str(target["alias"]).encode("utf-8")).hexdigest()[:12]
            emit(f"対象{idx}/{len(targets)}を処理開始: alias_fingerprint={alias_fingerprint}")
            try:
                browser.switch_to(hennge_handle)
                emit("HENNGEでユーザーを検索します")
                hennge_handler.search_user(target["alias"])
                emit("証明書を取得します")
                downloaded_file = hennge_handler.download_certificate(target["alias"], target["imei"])
                emit("証明書ファイル名をIMEIに合わせて整理します")
                renamed_file = file_handler.rename_to_imei(downloaded_file, target["imei"])
                browser.switch_to(smsm_handle)
                emit("SMSMへ証明書をアップロードします")
                smsm_handler.upload_certificate(renamed_file, target["imei"])
                emit("端末を検索します")
                smsm_handler.search_device(target["serial"])
                emit("IMEIを紐づけます")
                smsm_handler.associate_imei(target["serial"], target["imei"])
                emit("処理成功")
                success_count += 1
            except Exception as exc:
                failure_count += 1
                emit(f"処理失敗: {exc}")
                logger.exception(f"対象{idx}の処理に失敗しました")
                try:
                    logger.save_browser_diagnostics(browser.driver, f"error_{idx}")
                except Exception as diag_exc:
                    emit(f"診断情報保存に失敗しました: {diag_exc}")

        emit(f"処理を終了します: 成功={success_count}, 失敗={failure_count}")
    except Exception as exc:
        emit(f"処理を中断しました: {type(exc).__name__}")
        logger.error(f"失敗段階=main_execution, 例外型={type(exc).__name__}")
        try:
            driver = browser.driver if browser is not None else None
            logger.save_browser_diagnostics(driver, "fatal_error")
        except Exception as diag_exc:
            emit(f"診断情報保存に失敗しました: {type(diag_exc).__name__}")
    finally:
        if browser is not None:
            browser.quit()
        if should_reopen_excel and reopen_path is not None:
            reopen_excel(reopen_path, emit)
        progress.close()


if __name__ == "__main__":
    main()
