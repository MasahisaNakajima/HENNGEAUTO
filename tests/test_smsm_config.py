from __future__ import annotations

import inspect

from app.smsm_config import (
    SMSM_COMPANY_CODE_ENV,
    SMSM_PASSWORD_ENV,
    SMSM_URL_ENV,
    SMSM_USERNAME_ENV,
    SMSM_BASE_URL,
    SmsmConfig,
    credential_status,
    resolve_smsm_config,
    url_validation_status,
)


def test_all_environment_values_use_environment_source():
    result = resolve_smsm_config(
        {"smsm": {"url": "https://settings.invalid", "username": "settings-user", "password": "settings-pass"}},
        {
            SMSM_URL_ENV: "https://environment.invalid/login",
            SMSM_COMPANY_CODE_ENV: "environment-company",
            SMSM_USERNAME_ENV: "environment-user",
            SMSM_PASSWORD_ENV: "environment-pass",
        },
    )

    assert result.source == "environment"
    assert result.valid is True
    assert result.url == "https://environment.invalid/login"
    assert result.username == "environment-user"
    assert result.password == "environment-pass"
    assert result.source_type == "environment_variables"


def test_requested_smsm_environment_variable_names_are_used():
    assert SMSM_COMPANY_CODE_ENV == "SMSM_COMPANY_CODE"
    assert SMSM_USERNAME_ENV == "SMSM_USERNAME"
    assert SMSM_PASSWORD_ENV == "SMSM_PASSWORD"


def test_environment_values_override_settings_individually():
    result = resolve_smsm_config(
        {"smsm": {"url": "https://settings.invalid", "company_code": "settings-company", "username": "settings-user", "password": "settings-pass"}},
        {SMSM_URL_ENV: "https://environment.invalid", SMSM_COMPANY_CODE_ENV: "environment-company", SMSM_USERNAME_ENV: "environment-user", SMSM_PASSWORD_ENV: "environment-pass"},
    )

    assert result.source == "environment"
    assert result.url == "https://environment.invalid"
    assert result.username == "environment-user"
    assert result.password == "environment-pass"
    assert result.source_type == "environment_variables"


def test_empty_settings_company_code_does_not_override_environment_company_code():
    result = resolve_smsm_config(
        {"smsm": {"company_code": "", "username": "settings-user", "password": "settings-pass"}},
        {SMSM_COMPANY_CODE_ENV: "environment-company", SMSM_USERNAME_ENV: "", SMSM_PASSWORD_ENV: ""},
    )

    assert result.company_code == "environment-company"
    assert result.valid is True
    assert result.source_type == "mixed"


def test_settings_company_code_resolves_when_environment_company_code_is_blank():
    result = resolve_smsm_config(
        {"smsm": {"company_code": "settings-company", "username": "settings-user", "password": "settings-pass"}},
        {SMSM_COMPANY_CODE_ENV: " ", SMSM_USERNAME_ENV: "", SMSM_PASSWORD_ENV: ""},
    )

    assert result.company_code == "settings-company"
    assert result.valid is True
    assert result.source_type == "settings_json"


def test_mixed_credentials_are_complete():
    result = resolve_smsm_config(
        {"smsm": {"company_code": "settings-company", "username": "settings-user", "password": ""}},
        {SMSM_COMPANY_CODE_ENV: "", SMSM_USERNAME_ENV: "", SMSM_PASSWORD_ENV: "environment-pass"},
    )

    assert result.valid is True
    assert result.source_type == "mixed"


def test_company_code_blank_in_both_sources_is_unresolved():
    result = resolve_smsm_config(
        {"smsm": {"company_code": " ", "username": "settings-user", "password": "settings-pass"}},
        {SMSM_COMPANY_CODE_ENV: "\t"},
    )

    assert result.company_code == ""
    assert result.valid is False
    assert result.source_type == "settings_json"


def test_whitespace_only_company_code_is_unresolved():
    result = resolve_smsm_config(
        {"smsm": {"company_code": "\t"}},
        {SMSM_COMPANY_CODE_ENV: "  "},
    )

    assert result.company_code == ""
    assert result.valid is False
    assert result.source_type == "unresolved"


def test_blank_environment_values_fall_back_to_settings():
    result = resolve_smsm_config(
        {"smsm": {"url": "https://settings.invalid", "company_code": "settings-company", "username": "settings-user", "password": "settings-pass"}},
        {SMSM_URL_ENV: " ", SMSM_COMPANY_CODE_ENV: "", SMSM_USERNAME_ENV: "", SMSM_PASSWORD_ENV: "\t"},
    )

    assert result.source == "settings"
    assert result.valid is True
    assert result.url == "https://settings.invalid"
    assert result.username == "settings-user"
    assert result.password == "settings-pass"


def test_missing_url_uses_existing_base_url():
    result = resolve_smsm_config(
        {"smsm": {"company_code": "settings-company", "username": "settings-user", "password": "settings-pass"}},
        {},
    )

    assert result.url == SMSM_BASE_URL
    assert result.source == "settings"
    assert result.valid is True


def test_missing_username_is_invalid():
    result = resolve_smsm_config({"smsm": {"url": "https://settings.invalid", "password": "settings-pass"}}, {})
    assert result.username == ""
    assert result.valid is False


def test_missing_password_is_invalid():
    result = resolve_smsm_config({"smsm": {"url": "https://settings.invalid", "username": "settings-user"}}, {})
    assert result.password == ""
    assert result.valid is False


def test_invalid_url_is_invalid():
    result = resolve_smsm_config(
        {"smsm": {"url": "ftp://settings.invalid", "username": "settings-user", "password": "settings-pass"}},
        {},
    )
    assert result.valid is False


def test_whitespace_credentials_are_not_valid():
    result = resolve_smsm_config(
        {"smsm": {"url": "https://settings.invalid", "username": " ", "password": "\t"}},
        {},
    )
    assert result.valid is False


def test_resolver_does_not_log_values():
    result = resolve_smsm_config(
        {"smsm": {"url": "https://private.invalid", "username": "private-user", "password": "private-pass"}},
        {},
    )
    allowed_log_values = {"environment", "settings", "environment+settings", "default_url", True, False}
    assert result.source in allowed_log_values
    assert result.valid in allowed_log_values


def test_credential_status_contains_no_credential_values():
    result = resolve_smsm_config(
        {"smsm": {"company_code": "private-company", "username": "private-user", "password": "private-pass"}},
        {},
    )

    status_text = repr(credential_status(result))
    assert "private-company" not in status_text
    assert "private-user" not in status_text
    assert "private-pass" not in status_text


def test_smsm_config_constructor_is_keyword_only():
    parameters = inspect.signature(SmsmConfig).parameters
    assert all(parameter.kind is inspect.Parameter.KEYWORD_ONLY for parameter in parameters.values())


def test_invalid_smsm_url_forms_are_rejected():
    invalid_values = (
        "https://example.invalid/<a href='x'>login</a>",
        "https://example.invalid/?href=x",
        "https://example.invalid/login\nnext",
        "https:///missing-host",
        "https://example.invalid/$env:SMSM_URL=https://other.invalid",
    )
    for value in invalid_values:
        assert url_validation_status(value)["url_config_valid"] is False


def test_normal_https_smsm_url_is_valid():
    assert url_validation_status("https://example.invalid/login") == {
        "url_scheme_valid": True,
        "url_host_present": True,
        "url_contains_html": False,
        "url_contains_whitespace": False,
        "url_config_valid": True,
    }
