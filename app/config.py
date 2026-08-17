import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "settings.json"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"設定ファイルが見つかりません: {CONFIG_PATH}")

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        config = json.load(f)

    _apply_credential_overrides(config)
    _normalize_certificate_filter(config)
    return config


def _apply_credential_overrides(config: dict) -> None:
    hennge = config.setdefault("hennge", {})
    smsm = config.setdefault("smsm", {})

    env_map = [
        ("HENNGE_USERNAME", hennge, "username"),
        ("HENNGE_PASSWORD", hennge, "password"),
        ("SMSM_USERNAME", smsm, "username"),
        ("SMSM_PASSWORD", smsm, "password"),
    ]
    for env_key, section, field in env_map:
        value = os.getenv(env_key)
        if value:
            section[field] = value


def _normalize_certificate_filter(config: dict) -> None:
    cert_cfg = config.setdefault("hennge", {}).setdefault("certificate", {})
    subject = cert_cfg.setdefault("subject", {})
    issuer = cert_cfg.setdefault("issuer", {})

    subject_cn = (cert_cfg.get("subject_cn") or "").strip()
    issuer_cn = (cert_cfg.get("issuer_cn") or "").strip()
    if subject_cn and not subject.get("CN"):
        subject["CN"] = subject_cn
    if issuer_cn and not issuer.get("CN"):
        issuer["CN"] = issuer_cn


def ensure_directories(config: dict) -> None:
    base_dir = BASE_DIR
    for folder_name in ["logs", "screenshots", "downloads"]:
        path = base_dir / folder_name
        path.mkdir(exist_ok=True)

    if "paths" in config:
        for key in ["logs", "screenshots", "downloads"]:
            path_value = config["paths"].get(key)
            if path_value:
                path = base_dir / path_value
                path.mkdir(exist_ok=True)
