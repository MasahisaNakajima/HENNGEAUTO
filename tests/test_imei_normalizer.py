from pathlib import Path

import pytest
from openpyxl import Workbook

import diagnose_excel_target as diagnosis
from app.excel_reader import ExcelReader
from app.file_handler import FileHandler
from app.imei_normalizer import normalize_imei


class DummyLogger:
    def info(self, _message: str) -> None:
        return None


VALID_CASES = [
    "35 936730 687217 7",
    "35\u3000936730\u3000687217\u30007",
    "35\u00a0936730\u00a0687217\u00a07",
    "35\u2009936730\u2009687217\u20097",
    "35\u202f936730\u202f687217\u202f7",
    "35\u200b936730\u200b687217\u200b7",
    "35\t936730\t687217\t7",
    "35\n936730\n687217\n7",
]

INVALID_CASES = [
    (12345678901234, "IMEIは15桁ではありません"),
    (1234567890123456, "IMEIは15桁ではありません"),
    (12345678901234.5, "IMEIが整数ではありません"),
    ("１２３４５６７８９０１２３４５", "IMEIに数字以外が含まれています"),
    ("123456789012-45", "IMEIに数字以外が含まれています"),
    ("123456789012ABC", "IMEIに数字以外が含まれています"),
    ("123456789/012345", "IMEIに数字以外が含まれています"),
]


def _excel_result(tmp_path: Path, value):
    tmp_path.mkdir(parents=True, exist_ok=True)
    workbook_path = tmp_path / "targets.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "HENNGE登録作業必要情報"
    sheet.append(["", "", "alias", "serial", "imei"])
    sheet.append(["", "", "", "", ""])
    sheet.append(["", "", "", "", ""])
    sheet.append(["", "", "target-alias", "target-serial", value])
    workbook.save(workbook_path)
    workbook.close()
    try:
        return ("ok", ExcelReader(str(workbook_path)).read_targets()[0]["imei"])
    except ValueError as exc:
        return ("error", str(exc))


def _file_result(tmp_path: Path, value):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source.pfx"
    source.write_bytes(b"certificate")
    try:
        result = FileHandler(tmp_path, DummyLogger()).rename_to_imei(source, value)
        return "ok", result.stem
    except ValueError as exc:
        return "error", str(exc)


def _diagnosis_result(value):
    normalized, category = diagnosis._normalize_imei_for_diagnosis(value)
    if category in {"valid_original_count", "valid_after_whitespace_normalization_count"}:
        return "ok", normalized
    return "error", category


@pytest.mark.parametrize("value", VALID_CASES + [123456789012345, 123456789012345.0])
def test_three_imei_routes_have_identical_success_result(tmp_path: Path, value):
    expected = ("ok", "359367306872177" if isinstance(value, str) else "123456789012345")

    assert _excel_result(tmp_path / "excel", value) == expected
    assert _file_result(tmp_path / "file", value) == expected
    assert _diagnosis_result(value) == expected


@pytest.mark.parametrize("value, expected_message", INVALID_CASES)
def test_three_imei_routes_have_identical_failure_result(tmp_path: Path, value, expected_message):
    expected = ("error", expected_message)

    assert _excel_result(tmp_path / "excel", value) == expected
    assert _file_result(tmp_path / "file", value) == expected
    with pytest.raises(ValueError) as exc:
        normalize_imei(value)
    assert ("error", str(exc.value)) == expected

    _normalized, category = diagnosis._normalize_imei_for_diagnosis(value)
    assert category in {
        "invalid_length_count",
        "non_integer_numeric_count",
        "non_ascii_digit_count",
        "non_digit_character_count",
    }
