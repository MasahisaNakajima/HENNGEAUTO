from __future__ import annotations

import unicodedata


IMEI_SEPARATOR_CHARS = frozenset(
    {
        " ",
        "\t",
        "\n",
        "\r",
        "\u00a0",
        "\u2009",
        "\u202f",
        "\u3000",
        "\u200b",
    }
)


def is_target_row(values: tuple[object, ...]) -> bool:
    alias, serial, imei = (str(value or "").strip() for value in values[:3])
    if not alias and not serial and not imei:
        return False

    header_values = {
        alias.replace(" ", "").upper(),
        serial.replace(" ", "").upper(),
        imei.replace(" ", "").upper(),
    }
    if header_values & {"ALIAS", "エイリアス", "SERIAL", "シリアル", "シリアル番号", "IMEI"}:
        return False

    markers = {"#N/A", "N/A", "NA", "-", ""}
    if alias.upper() in markers and serial.upper() in markers and imei.upper() in markers:
        return False
    return True


def normalize_imei(value) -> str:
    if value is None:
        return ""

    if isinstance(value, bool):
        text = str(value)
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError("IMEIが整数ではありません")
        text = str(int(value))
    else:
        text = unicodedata.normalize("NFC", str(value))

    if not isinstance(value, (int, float, bool)):
        text = "".join(char for char in text if char not in IMEI_SEPARATOR_CHARS)

    if not text:
        return ""
    if not all("0" <= char <= "9" for char in text):
        raise ValueError("IMEIに数字以外が含まれています")
    if len(text) != 15:
        raise ValueError("IMEIは15桁ではありません")
    return text
