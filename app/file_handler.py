import shutil
from pathlib import Path

from app.imei_normalizer import normalize_imei


class FileHandler:
    def __init__(self, base_dir: Path, logger):
        self.base_dir = base_dir
        self.logger = logger

    def rename_to_imei(self, downloaded_file: Path, imei: str) -> Path:
        if not downloaded_file.exists():
            raise FileNotFoundError(f"ダウンロード済みファイルが見つかりません: {downloaded_file}")
        if not downloaded_file.is_file():
            raise RuntimeError(f"対象が通常ファイルではありません: {downloaded_file}")
        if downloaded_file.stat().st_size == 0:
            raise RuntimeError(f"ダウンロードファイルが空です: {downloaded_file}")

        suffix = downloaded_file.suffix.lower()
        if suffix not in {".pfx", ".p12"}:
            raise RuntimeError(f"想定外の証明書拡張子です: {suffix}")

        imei_text = normalize_imei(imei)
        if not imei_text:
            raise ValueError("IMEIに数字以外が含まれています")

        target_dir = self.base_dir / "downloads"
        target_dir.mkdir(parents=True, exist_ok=True)
        new_path = target_dir / f"{imei_text}{suffix}"

        if downloaded_file.resolve() == new_path.resolve():
            self.logger.info(f"証明書ファイルは既にIMEI名です: {new_path}")
            return new_path

        if new_path.exists():
            raise FileExistsError("同名の証明書ファイルが既に存在します")

        shutil.copy2(downloaded_file, new_path)
        self.logger.info(f"証明書ファイルをコピー: {downloaded_file.name} -> {new_path.name}")
        return new_path
