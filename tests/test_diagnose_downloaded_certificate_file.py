from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import diagnose_downloaded_certificate_file as mod


class DummyLogger:
    def __init__(self):
        self.info_messages = []
        self.error_messages = []

    def info(self, message):
        self.info_messages.append(message)

    def error(self, message):
        self.error_messages.append(message)


@pytest.fixture
def isolated_fs(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_base_dir", lambda: tmp_path)
    source_dir = tmp_path / "downloads" / "hennge_download_diagnostic"
    dest_dir = tmp_path / "downloads" / "hennge_file_diagnostic"
    source_dir.mkdir(parents=True, exist_ok=True)
    dest_dir.mkdir(parents=True, exist_ok=True)
    logger = DummyLogger()
    monkeypatch.setattr(mod, "AppLogger", lambda _base_dir: logger)
    return tmp_path, source_dir, dest_dir, logger


def _write(path: Path, data: bytes):
    path.write_bytes(data)
    return path


def test_accepts_p12_and_copies_once(isolated_fs, monkeypatch):
    _tmp, source_dir, dest_dir, logger = isolated_fs
    source = _write(source_dir / "downloaded.p12", b"abc123")

    calls = {"copy": 0}
    original_copyfile = shutil.copyfile

    def counted_copy(src, dst, *, follow_symlinks=True):
        calls["copy"] += 1
        return original_copyfile(src, dst, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(mod.shutil, "copyfile", counted_copy)

    rc = mod.main(["123456789012345"])

    assert rc == 0
    assert calls["copy"] == 1
    copied = list(dest_dir.iterdir())
    assert len(copied) == 1
    assert copied[0].name == "123456789012345.p12"
    assert copied[0].stat().st_size == source.stat().st_size
    assert source.exists() is True
    assert "123456789012345" not in "\n".join(logger.info_messages + logger.error_messages)


def test_accepts_pfx_and_keeps_extension(isolated_fs):
    _tmp, source_dir, dest_dir, _logger = isolated_fs
    _write(source_dir / "downloaded.pfx", b"abc123")

    rc = mod.main(["123456789012345"])

    assert rc == 0
    copied = list(dest_dir.iterdir())
    assert len(copied) == 1
    assert copied[0].name.endswith(".pfx")


@pytest.mark.parametrize("suffix", [".crdownload", ".tmp", ".part"])
def test_rejects_temporary_file_suffix(isolated_fs, suffix):
    _tmp, source_dir, _dest_dir, _logger = isolated_fs
    _write(source_dir / f"downloaded{suffix}", b"abc123")

    rc = mod.main(["123456789012345"])

    assert rc == 4


def test_distinguishes_zero_files(isolated_fs):
    _tmp, _source_dir, _dest_dir, _logger = isolated_fs

    rc = mod.main(["123456789012345"])

    assert rc == 2


def test_distinguishes_multiple_files(isolated_fs):
    _tmp, source_dir, _dest_dir, _logger = isolated_fs
    _write(source_dir / "a.p12", b"a")
    _write(source_dir / "b.p12", b"b")

    rc = mod.main(["123456789012345"])

    assert rc == 3


def test_rejects_zero_size(isolated_fs):
    _tmp, source_dir, _dest_dir, _logger = isolated_fs
    _write(source_dir / "a.p12", b"")

    rc = mod.main(["123456789012345"])

    assert rc == 5


@pytest.mark.parametrize("imei", ["12345678901234", "1234567890123456", "12345ABCDE12345"])
def test_rejects_invalid_imei_formats(isolated_fs, imei):
    _tmp, source_dir, dest_dir, _logger = isolated_fs
    _write(source_dir / "a.p12", b"abc123")

    rc = mod.main([imei])

    assert rc == 6
    assert list(dest_dir.iterdir()) == []


def test_imei_15_digits_is_accepted(isolated_fs):
    _tmp, source_dir, _dest_dir, _logger = isolated_fs
    _write(source_dir / "a.p12", b"abc123")

    rc = mod.main(["123456789012345"])

    assert rc == 0


def test_source_file_is_not_modified_or_deleted(isolated_fs):
    _tmp, source_dir, _dest_dir, _logger = isolated_fs
    source = _write(source_dir / "a.p12", b"abc123")
    before = source.stat()

    rc = mod.main(["123456789012345"])

    after = source.stat()
    assert rc == 0
    assert source.exists() is True
    assert before.st_size == after.st_size
    assert before.st_mtime_ns == after.st_mtime_ns


def test_size_mismatch_returns_8(isolated_fs, monkeypatch):
    _tmp, source_dir, _dest_dir, _logger = isolated_fs
    source = _write(source_dir / "a.p12", b"abc123")

    def fake_copy(_src, dst, *, follow_symlinks=True):
        _ = follow_symlinks
        Path(dst).write_bytes(b"x")
        return str(dst)

    monkeypatch.setattr(mod.shutil, "copyfile", fake_copy)

    rc = mod.main(["123456789012345"])

    assert rc == 8
    assert source.exists() is True


def test_hash_mismatch_returns_9(isolated_fs, monkeypatch):
    _tmp, source_dir, _dest_dir, _logger = isolated_fs
    _write(source_dir / "a.p12", b"abc123")

    calls = {"count": 0}
    original_hash = mod._compute_sha256

    def fake_hash(path):
        calls["count"] += 1
        if calls["count"] == 1:
            return original_hash(path)
        return "0" * 64

    monkeypatch.setattr(mod, "_compute_sha256", fake_hash)

    rc = mod.main(["123456789012345"])

    assert rc == 9


def test_stops_when_destination_not_empty(isolated_fs):
    _tmp, source_dir, dest_dir, _logger = isolated_fs
    _write(source_dir / "a.p12", b"abc123")
    _write(dest_dir / "already.p12", b"x")

    rc = mod.main(["123456789012345"])

    assert rc == 7


def test_logs_do_not_include_full_filename_or_full_path_or_full_imei(isolated_fs):
    _tmp, source_dir, _dest_dir, logger = isolated_fs
    file_name = "very_sensitive_certificate_name.p12"
    source = _write(source_dir / file_name, b"abc123")
    imei = "123456789012345"

    rc = mod.main([imei])

    joined = "\n".join(logger.info_messages + logger.error_messages)
    assert rc == 0
    assert file_name not in joined
    assert str(source) not in joined
    assert imei not in joined
    assert "*" * 11 + imei[-4:] in joined


def test_no_external_system_usage_symbols_present():
    assert not hasattr(mod, "Browser")
    assert not hasattr(mod, "HenngeHandler")


def test_copyfile_is_called_once(isolated_fs, monkeypatch):
    _tmp, source_dir, _dest_dir, _logger = isolated_fs
    _write(source_dir / "a.p12", b"abc123")

    calls = {"count": 0}
    original_copyfile = shutil.copyfile

    def counted_copy(src, dst, *, follow_symlinks=True):
        calls["count"] += 1
        return original_copyfile(src, dst, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(mod.shutil, "copyfile", counted_copy)

    rc = mod.main(["123456789012345"])

    assert rc == 0
    assert calls["count"] == 1


def test_copy_integrity_size_and_hash_match(isolated_fs):
    _tmp, source_dir, dest_dir, _logger = isolated_fs
    source = _write(source_dir / "a.p12", b"abc123")

    rc = mod.main(["123456789012345"])

    copied = list(dest_dir.iterdir())
    assert rc == 0
    assert len(copied) == 1
    assert copied[0].stat().st_size == source.stat().st_size
    assert mod._compute_sha256(copied[0]) == mod._compute_sha256(source)
