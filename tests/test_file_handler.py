from pathlib import Path

import pytest

from app.file_handler import FileHandler


class DummyLogger:
    def info(self, message: str) -> None:
        return None


@pytest.fixture
def handler(tmp_path: Path) -> FileHandler:
    return FileHandler(tmp_path, DummyLogger())


def test_rename_to_imei_copies_file(handler: FileHandler, tmp_path: Path) -> None:
    source = tmp_path / "source.pfx"
    source.write_bytes(b"abc")

    output = handler.rename_to_imei(source, "123456789012345")

    assert output.exists()
    assert output.name == "123456789012345.pfx"
    assert output.read_bytes() == b"abc"


def test_rename_to_imei_rejects_invalid_imei(handler: FileHandler, tmp_path: Path) -> None:
    source = tmp_path / "source.pfx"
    source.write_bytes(b"abc")

    with pytest.raises(ValueError):
        handler.rename_to_imei(source, "12-34")


def test_rename_to_imei_normalizes_internal_whitespace(handler: FileHandler, tmp_path: Path) -> None:
    source = tmp_path / "source.pfx"
    source.write_bytes(b"abc")

    output = handler.rename_to_imei(source, "35 936730 687217 7")

    assert output.name == "359367306872177.pfx"


def test_rename_to_imei_rejects_invalid_extension(handler: FileHandler, tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_bytes(b"abc")

    with pytest.raises(RuntimeError):
        handler.rename_to_imei(source, "123456789012345")


def test_rename_to_imei_returns_same_path_when_already_named(handler: FileHandler, tmp_path: Path) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    same = downloads / "123456789012345.p12"
    same.write_bytes(b"abc")

    output = handler.rename_to_imei(same, "123456789012345")
    assert output == same
