from __future__ import annotations

import hashlib
import re
import shutil
import sys
from pathlib import Path

from app.logger import AppLogger

SOURCE_DIRNAME = "hennge_download_diagnostic"
DEST_DIRNAME = "hennge_file_diagnostic"
TEMP_SUFFIXES = {".crdownload", ".tmp", ".part"}
ALLOWED_SUFFIXES = {".p12", ".pfx"}
IMEI_PATTERN = re.compile(r"^\d{15}$")


def _base_dir() -> Path:
    return Path(__file__).resolve().parent


def _source_dir() -> Path:
    return _base_dir() / "downloads" / SOURCE_DIRNAME


def _dest_dir() -> Path:
    return _base_dir() / "downloads" / DEST_DIRNAME


def _mask_imei(imei: str) -> str:
    if len(imei) <= 4:
        return "*" * len(imei)
    return "*" * (len(imei) - 4) + imei[-4:]


def _is_valid_imei(imei: str) -> bool:
    return bool(IMEI_PATTERN.fullmatch(imei))


def _list_regular_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return [item for item in directory.iterdir() if item.is_file()]


def _compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _can_read_file(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            _ = f.read(1)
        return True
    except Exception:
        return False


def _has_temp_files(files: list[Path]) -> bool:
    return any(item.suffix.lower() in TEMP_SUFFIXES for item in files)


def _safe_ext(path: Path) -> str:
    return path.suffix.lower()


def _log_summary(
    logger: AppLogger,
    *,
    source_count: int,
    extension: str,
    size_gt_zero: bool,
    readable: bool,
    imei_valid: bool,
    imei_masked: str,
    dest_count: int,
    size_match: bool,
    hash_match: bool,
    source_preserved: bool,
    digest_prefix: str,
) -> None:
    logger.info(
        "診断結果 "
        f"source_file_count={source_count}, "
        f"extension={extension}, "
        f"size_gt_zero={size_gt_zero}, "
        f"readable={readable}, "
        f"imei_format_valid={imei_valid}, "
        f"imei_masked={imei_masked}, "
        f"dest_file_count={dest_count}, "
        f"size_match={size_match}, "
        f"hash_match={hash_match}, "
        f"source_preserved={source_preserved}, "
        f"sha256_prefix8={digest_prefix}"
    )


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1 or not args[0].strip():
        print("Usage: python diagnose_downloaded_certificate_file.py <TEST_IMEI>")
        return 1

    imei = args[0].strip()
    imei_valid = _is_valid_imei(imei)
    imei_masked = _mask_imei(imei)

    logger = AppLogger(_base_dir())
    logger.info("診断モード: ローカル証明書ファイルの形式確認と隔離コピー試験のみを実行します")
    logger.info("SMSM/HENNGE/Excel/ブラウザー操作は実行しません")

    try:
        source_dir = _source_dir()
        source_files = _list_regular_files(source_dir)
        source_count = len(source_files)
        if source_count == 0:
            _log_summary(
                logger,
                source_count=0,
                extension="",
                size_gt_zero=False,
                readable=False,
                imei_valid=imei_valid,
                imei_masked=imei_masked,
                dest_count=0,
                size_match=False,
                hash_match=False,
                source_preserved=False,
                digest_prefix="",
            )
            return 2
        if source_count > 1:
            _log_summary(
                logger,
                source_count=source_count,
                extension="",
                size_gt_zero=False,
                readable=False,
                imei_valid=imei_valid,
                imei_masked=imei_masked,
                dest_count=0,
                size_match=False,
                hash_match=False,
                source_preserved=False,
                digest_prefix="",
            )
            return 3

        source_file = source_files[0]
        extension = _safe_ext(source_file)
        if extension in TEMP_SUFFIXES:
            _log_summary(
                logger,
                source_count=source_count,
                extension=extension,
                size_gt_zero=False,
                readable=False,
                imei_valid=imei_valid,
                imei_masked=imei_masked,
                dest_count=0,
                size_match=False,
                hash_match=False,
                source_preserved=False,
                digest_prefix="",
            )
            return 4

        if extension not in ALLOWED_SUFFIXES:
            _log_summary(
                logger,
                source_count=source_count,
                extension=extension,
                size_gt_zero=False,
                readable=False,
                imei_valid=imei_valid,
                imei_masked=imei_masked,
                dest_count=0,
                size_match=False,
                hash_match=False,
                source_preserved=False,
                digest_prefix="",
            )
            return 4

        source_stat_before = source_file.stat()
        size_gt_zero = source_stat_before.st_size > 0
        if not size_gt_zero:
            _log_summary(
                logger,
                source_count=source_count,
                extension=extension,
                size_gt_zero=False,
                readable=False,
                imei_valid=imei_valid,
                imei_masked=imei_masked,
                dest_count=0,
                size_match=False,
                hash_match=False,
                source_preserved=True,
                digest_prefix="",
            )
            return 5

        readable = _can_read_file(source_file)
        source_hash = _compute_sha256(source_file)
        digest_prefix = source_hash[:8]

        if not imei_valid:
            _log_summary(
                logger,
                source_count=source_count,
                extension=extension,
                size_gt_zero=size_gt_zero,
                readable=readable,
                imei_valid=False,
                imei_masked=imei_masked,
                dest_count=0,
                size_match=False,
                hash_match=False,
                source_preserved=True,
                digest_prefix=digest_prefix,
            )
            return 6

        dest_dir = _dest_dir()
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_files_before = _list_regular_files(dest_dir)
        if dest_files_before:
            _log_summary(
                logger,
                source_count=source_count,
                extension=extension,
                size_gt_zero=size_gt_zero,
                readable=readable,
                imei_valid=True,
                imei_masked=imei_masked,
                dest_count=len(dest_files_before),
                size_match=False,
                hash_match=False,
                source_preserved=True,
                digest_prefix=digest_prefix,
            )
            return 7

        dest_file = dest_dir / f"{imei}{extension}"
        shutil.copyfile(source_file, dest_file)

        dest_files_after = _list_regular_files(dest_dir)
        if len(dest_files_after) != 1:
            _log_summary(
                logger,
                source_count=source_count,
                extension=extension,
                size_gt_zero=size_gt_zero,
                readable=readable,
                imei_valid=True,
                imei_masked=imei_masked,
                dest_count=len(dest_files_after),
                size_match=False,
                hash_match=False,
                source_preserved=source_file.exists(),
                digest_prefix=digest_prefix,
            )
            return 1

        if _has_temp_files(dest_files_after):
            _log_summary(
                logger,
                source_count=source_count,
                extension=extension,
                size_gt_zero=size_gt_zero,
                readable=readable,
                imei_valid=True,
                imei_masked=imei_masked,
                dest_count=len(dest_files_after),
                size_match=False,
                hash_match=False,
                source_preserved=source_file.exists(),
                digest_prefix=digest_prefix,
            )
            return 1

        dest_stat = dest_file.stat()
        size_match = dest_stat.st_size == source_stat_before.st_size
        if not size_match:
            _log_summary(
                logger,
                source_count=source_count,
                extension=extension,
                size_gt_zero=size_gt_zero,
                readable=readable,
                imei_valid=True,
                imei_masked=imei_masked,
                dest_count=len(dest_files_after),
                size_match=False,
                hash_match=False,
                source_preserved=source_file.exists(),
                digest_prefix=digest_prefix,
            )
            return 8

        dest_hash = _compute_sha256(dest_file)
        hash_match = dest_hash == source_hash
        if not hash_match:
            _log_summary(
                logger,
                source_count=source_count,
                extension=extension,
                size_gt_zero=size_gt_zero,
                readable=readable,
                imei_valid=True,
                imei_masked=imei_masked,
                dest_count=len(dest_files_after),
                size_match=True,
                hash_match=False,
                source_preserved=source_file.exists(),
                digest_prefix=digest_prefix,
            )
            return 9

        source_exists_after = source_file.exists()
        source_unchanged = False
        if source_exists_after:
            source_stat_after = source_file.stat()
            source_unchanged = (
                source_stat_after.st_size == source_stat_before.st_size
                and source_stat_after.st_mtime_ns == source_stat_before.st_mtime_ns
            )

        source_preserved = source_exists_after and source_unchanged
        if not source_preserved:
            _log_summary(
                logger,
                source_count=source_count,
                extension=extension,
                size_gt_zero=size_gt_zero,
                readable=readable,
                imei_valid=True,
                imei_masked=imei_masked,
                dest_count=len(dest_files_after),
                size_match=True,
                hash_match=True,
                source_preserved=False,
                digest_prefix=digest_prefix,
            )
            return 10

        _log_summary(
            logger,
            source_count=source_count,
            extension=extension,
            size_gt_zero=size_gt_zero,
            readable=readable,
            imei_valid=True,
            imei_masked=imei_masked,
            dest_count=len(dest_files_after),
            size_match=True,
            hash_match=True,
            source_preserved=True,
            digest_prefix=digest_prefix,
        )
        return 0
    except KeyboardInterrupt:
        logger.error("診断を中断しました: KeyboardInterrupt")
        return 130
    except Exception:
        logger.error("ダウンロード済み証明書ファイル診断に失敗しました")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
