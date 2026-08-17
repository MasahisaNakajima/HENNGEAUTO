from pathlib import Path

import pytest
from openpyxl import Workbook

from app.excel_reader import ExcelReader


def _make_workbook(path: Path, rows: list[tuple]) -> None:
    """Build a workbook that mirrors the real-file layout: header at row 1,
    two pre-data rows at rows 2-3 (skipped by min_row=4), data from row 4."""
    wb = Workbook()
    ws = wb.active
    ws.title = "HENNGE登録作業必要情報"
    ws.append(["col0", "col1", "alias", "serial", "imei"])          # row 1: column header
    ws.append(["", "", "説明A（入力不要）", "説明A", "000"])          # row 2: skipped by min_row=4
    ws.append(["", "", "説明B（記入例）", "説明B", "111"])            # row 3: skipped by min_row=4
    for row in rows:
        ws.append(list(row))                                           # row 4+: actual data
    wb.save(path)
    wb.close()


# ---------------------------------------------------------------------------
# 開始行
# ---------------------------------------------------------------------------

def test_rows_2_and_3_are_not_read(tmp_path: Path) -> None:
    xlsx = tmp_path / "t.xlsx"
    _make_workbook(xlsx, [("", "", "alias1", "serial1", "123456789012345")])

    rows = ExcelReader(str(xlsx)).read_targets()

    assert len(rows) == 1
    assert rows[0]["alias"] == "alias1"


def test_reads_from_row_4(tmp_path: Path) -> None:
    xlsx = tmp_path / "t.xlsx"
    _make_workbook(xlsx, [("", "", "first", "s1", "123456789012345")])

    rows = ExcelReader(str(xlsx)).read_targets()

    assert rows[0]["alias"] == "first"


def test_reads_multiple_rows_from_row_4(tmp_path: Path) -> None:
    xlsx = tmp_path / "t.xlsx"
    _make_workbook(
        xlsx,
        [
            ("", "", "alias1", "s1", "111111111111111"),
            ("", "", "alias2", "s2", "222222222222222"),
            ("", "", "alias3", "s3", "333333333333333"),
        ],
    )

    rows = ExcelReader(str(xlsx)).read_targets()

    assert len(rows) == 3
    assert rows[0]["alias"] == "alias1"
    assert rows[2]["alias"] == "alias3"


# ---------------------------------------------------------------------------
# 空行・継続
# ---------------------------------------------------------------------------

def test_blank_row_in_the_middle_is_skipped_without_error(tmp_path: Path) -> None:
    xlsx = tmp_path / "t.xlsx"
    _make_workbook(
        xlsx,
        [
            ("", "", "alias1", "s1", "111111111111111"),
            ("", "", None, None, None),
            ("", "", "alias2", "s2", "222222222222222"),
        ],
    )

    rows = ExcelReader(str(xlsx)).read_targets()

    assert len(rows) == 2
    assert rows[0]["alias"] == "alias1"
    assert rows[1]["alias"] == "alias2"


def test_all_blank_row_is_not_an_error(tmp_path: Path) -> None:
    xlsx = tmp_path / "t.xlsx"
    _make_workbook(xlsx, [("", "", None, None, None)])

    rows = ExcelReader(str(xlsx)).read_targets()

    assert rows == []


# ---------------------------------------------------------------------------
# 不完全行
# ---------------------------------------------------------------------------

def test_incomplete_row_raises_missing_required_field(tmp_path: Path) -> None:
    xlsx = tmp_path / "missing.xlsx"
    _make_workbook(xlsx, [("", "", "alias1", "", "123456789012345")])

    with pytest.raises(ValueError) as exc:
        ExcelReader(str(xlsx)).read_targets()

    assert "必須項目が不足" in str(exc.value)
    assert "4行目" in str(exc.value)


# ---------------------------------------------------------------------------
# IMEI 許可
# ---------------------------------------------------------------------------

def test_accepts_15_digit_string_imei(tmp_path: Path) -> None:
    xlsx = tmp_path / "t.xlsx"
    _make_workbook(xlsx, [("", "", "a1", "s1", "123456789012345")])

    assert ExcelReader(str(xlsx)).read_targets()[0]["imei"] == "123456789012345"


def test_accepts_15_digit_int_imei(tmp_path: Path) -> None:
    xlsx = tmp_path / "t.xlsx"
    _make_workbook(xlsx, [("", "", "a1", "s1", 123456789012345)])

    assert ExcelReader(str(xlsx)).read_targets()[0]["imei"] == "123456789012345"


def test_accepts_imei_with_internal_whitespace(tmp_path: Path) -> None:
    xlsx = tmp_path / "t.xlsx"
    _make_workbook(xlsx, [("", "", "a1", "s1", "35 936730 687217 7")])

    assert ExcelReader(str(xlsx)).read_targets()[0]["imei"] == "359367306872177"


def test_accepts_integer_valued_float_imei(tmp_path: Path) -> None:
    xlsx = tmp_path / "t.xlsx"
    _make_workbook(xlsx, [("", "", "a1", "s1", 123456789012345.0)])

    assert ExcelReader(str(xlsx)).read_targets()[0]["imei"] == "123456789012345"


# ---------------------------------------------------------------------------
# IMEI 拒否
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_imei, fragment", [
    ("12345678901234",   "15桁ではありません"),   # 14桁
    ("1234567890123456", "15桁ではありません"),   # 16桁
    ("1234567890ABCDE",  "数字以外"),             # 英字混在
    ("12345-789012345",  "数字以外"),             # ハイフン混在
])
def test_rejects_invalid_imei(tmp_path: Path, bad_imei: str, fragment: str) -> None:
    xlsx = tmp_path / "t.xlsx"
    _make_workbook(xlsx, [("", "", "a1", "s1", bad_imei)])

    with pytest.raises(ValueError) as exc:
        ExcelReader(str(xlsx)).read_targets()

    assert fragment in str(exc.value)


def test_rejects_float_imei_with_fractional_part(tmp_path: Path) -> None:
    xlsx = tmp_path / "t.xlsx"
    _make_workbook(xlsx, [("", "", "a1", "s1", 12345678901234.5)])

    with pytest.raises(ValueError) as exc:
        ExcelReader(str(xlsx)).read_targets()

    assert "整数ではありません" in str(exc.value)


def test_imei_15_digit_error_message_contains_no_value_or_length(tmp_path: Path) -> None:
    xlsx = tmp_path / "t.xlsx"
    _make_workbook(xlsx, [("", "", "a1", "s1", "12345678901234")])  # 14桁

    with pytest.raises(ValueError) as exc:
        ExcelReader(str(xlsx)).read_targets()

    assert str(exc.value) == "IMEIは15桁ではありません"


# ---------------------------------------------------------------------------
# 行ペアと複数行
# ---------------------------------------------------------------------------

def test_alias_imei_same_row_pair_preserved(tmp_path: Path) -> None:
    xlsx = tmp_path / "t.xlsx"
    _make_workbook(
        xlsx,
        [
            ("", "", "pairA", "sA", "111111111111111"),
            ("", "", "pairB", "sB", "222222222222222"),
        ],
    )

    rows = ExcelReader(str(xlsx)).read_targets()

    assert rows[0]["alias"] == "pairA"
    assert rows[0]["imei"] == "111111111111111"
    assert rows[1]["alias"] == "pairB"
    assert rows[1]["imei"] == "222222222222222"


# ---------------------------------------------------------------------------
# 既存テスト（後方互換）
# ---------------------------------------------------------------------------

def test_read_targets_success_and_float_imei(tmp_path: Path) -> None:
    xlsx = tmp_path / "targets.xlsx"
    _make_workbook(
        xlsx,
        [
            ("", "", "alias1", "serial1", 123456789012345.0),
            ("", "", "alias2", "serial2", "  999999999999999  "),
        ],
    )

    reader = ExcelReader(str(xlsx))
    rows = reader.read_targets()

    assert rows[0]["imei"] == "123456789012345"
    assert rows[1]["imei"] == "999999999999999"


def test_read_targets_missing_required_field(tmp_path: Path) -> None:
    xlsx = tmp_path / "targets_missing.xlsx"
    _make_workbook(xlsx, [("", "", "alias1", "", "123456789012345")])

    reader = ExcelReader(str(xlsx))
    with pytest.raises(ValueError) as exc:
        reader.read_targets()
    assert "4行目" in str(exc.value)


def test_read_targets_invalid_imei(tmp_path: Path) -> None:
    xlsx = tmp_path / "targets_invalid.xlsx"
    _make_workbook(xlsx, [("", "", "alias1", "serial1", "ABC123")])

    reader = ExcelReader(str(xlsx))
    with pytest.raises(ValueError):
        reader.read_targets()


def test_read_targets_sheet_not_found(tmp_path: Path) -> None:
    xlsx = tmp_path / "other_sheet.xlsx"
    wb = Workbook()
    wb.active.title = "Other"
    wb.save(xlsx)
    wb.close()

    reader = ExcelReader(str(xlsx))
    with pytest.raises(KeyError):
        reader.read_targets()
