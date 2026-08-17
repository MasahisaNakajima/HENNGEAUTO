from __future__ import annotations

import os
from dataclasses import dataclass
from collections.abc import Mapping
from urllib.parse import urlparse


SMSM_URL_ENV = "HENNGE_AUTOMATION_SMSM_URL"
SMSM_USERNAME_ENV = "SMSM_USERNAME"
SMSM_PASSWORD_ENV = "SMSM_PASSWORD"
SMSM_COMPANY_CODE_ENV = "SMSM_COMPANY_CODE"
SMSM_BASE_URL = "https://ausl.smartmanager.jp"


@dataclass(frozen=True, kw_only=True)
class SmsmConfig:
    url: str
    company_code: str
    username: str
    password: str
    source: str
    valid: bool

    @property
    def source_type(self) -> str:
        if self.source == "environment":
            return "environment_variables"
        if self.source == "settings":
            return "settings_json"
        if self.source == "mixed":
            return "mixed"
        return "unresolved"


UNSAFE_PASSWORD_MARKERS = (
    "# $env:",
    "read-host",
    "securestring",
    "networkcredential",
    "powershell",
)


def _nonblank(value) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _valid_http_url(value: str) -> bool:
    return url_validation_status(value)["url_config_valid"]


def url_validation_status(value: str) -> dict[str, bool]:
    text = value if isinstance(value, str) else ""
    lowered = text.casefold()
    parsed = urlparse(text)
    scheme_valid = parsed.scheme in {"http", "https"}
    host_present = bool(parsed.hostname)
    contains_html = "<" in text or ">" in text or "<a" in lowered or "href=" in lowered
    contains_whitespace = any(character.isspace() for character in text)
    contains_assignment = "=" in text and ("$env:" in lowered or "set-" in lowered or "powershell" in lowered)
    return {
        "url_scheme_valid": scheme_valid,
        "url_host_present": host_present,
        "url_contains_html": contains_html,
        "url_contains_whitespace": contains_whitespace,
        "url_config_valid": bool(text) and scheme_valid and host_present and not contains_html and not contains_whitespace and not contains_assignment,
    }


def password_contains_unsafe_syntax(value: str) -> bool:
    lowered = value.casefold()
    return any(marker in lowered for marker in UNSAFE_PASSWORD_MARKERS)


def password_contains_assignment_syntax(value: str) -> bool:
    return "=" in value or "$env:" in value.casefold()


def config_mapping_is_valid(config: SmsmConfig) -> bool:
    return all(isinstance(value, str) for value in (
        config.url,
        config.company_code,
        config.username,
        config.password,
        config.source,
    )) and isinstance(config.valid, bool)


def credential_status(config: SmsmConfig) -> dict[str, object]:
    return {
        "smsm_credential_source_type": config.source_type,
        "smsm_company_code_resolved": bool(config.company_code),
        "smsm_username_resolved": bool(config.username),
        "smsm_password_resolved": bool(config.password),
        "smsm_credentials_complete": bool(config.valid),
    }


def resolve_smsm_config(
    settings: Mapping[str, object] | None,
    environ: Mapping[str, str] | None = None,
) -> SmsmConfig:
    settings_smsm = settings.get("smsm", {}) if settings else {}
    if not isinstance(settings_smsm, Mapping):
        settings_smsm = {}
    env = environ if environ is not None else os.environ

    env_url = _nonblank(env.get(SMSM_URL_ENV))
    env_username = _nonblank(env.get(SMSM_USERNAME_ENV))
    env_password = _nonblank(env.get(SMSM_PASSWORD_ENV))
    env_company_code = _nonblank(env.get(SMSM_COMPANY_CODE_ENV))
    settings_url = _nonblank(settings_smsm.get("url"))
    settings_username = _nonblank(settings_smsm.get("username"))
    settings_password = _nonblank(settings_smsm.get("password"))
    settings_company_code = _nonblank(settings_smsm.get("company_code"))

    url = env_url or settings_url or SMSM_BASE_URL
    username = env_username or settings_username or ""
    password = env_password or settings_password or ""
    company_code = env_company_code or settings_company_code or ""

    credential_sources = {
        "environment" if env_company_code is not None else "settings" if settings_company_code is not None else None,
        "environment" if env_username is not None else "settings" if settings_username is not None else None,
        "environment" if env_password is not None else "settings" if settings_password is not None else None,
    }
    credential_sources.discard(None)
    if len(credential_sources) > 1:
        source = "mixed"
    elif credential_sources:
        source = next(iter(credential_sources))
    else:
        source = "unresolved"

    valid = bool(
        _valid_http_url(url)
        and _nonblank(company_code)
        and _nonblank(username)
        and _nonblank(password)
    )
    return SmsmConfig(
        url=url,
        company_code=company_code,
        username=username,
        password=password,
        source=source,
        valid=valid,
    )
