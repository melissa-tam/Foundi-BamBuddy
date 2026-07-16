"""Unit tests for the ERP directory authentication service.

The PyMySQL driver is mocked at the ``_fetch_user_row`` boundary so no real
DB connection is made. These tests pin the tri-state classifier (OK /
REJECTED / UNREACHABLE), config parsing, and bcrypt verification.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import bcrypt
import pytest

from backend.app.core.auth import get_password_hash, verify_bcrypt_password, verify_password
from backend.app.services import erp_directory
from backend.app.services.erp_directory import (
    DEFAULT_ROLE_GROUP_MAPPING,
    ErpAuthStatus,
    authenticate_erp_user,
    load_erp_env_defaults,
    parse_erp_config,
)

# A real bcrypt $2a$ hash for the password "s3cret" (cost 10 for test speed).
_PW = "s3cret"
_HASH = bcrypt.hashpw(_PW.encode(), bcrypt.gensalt(rounds=10, prefix=b"2a")).decode()


def _config():
    return parse_erp_config(
        {
            "erp_db_host": "db.example",
            "erp_db_name": "Foundi_management_system",
            "erp_db_user": "reader",
            "erp_db_password": "x",  # noqa: S106 - test fixture
        }
    )


def _row(**overrides):
    row = {
        "username": "melissa.tam1",
        "password_hash": _HASH,
        "role": "ADMIN",
        "active": 1,
        "locked_until": None,
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------------------
# parse_erp_config
# --------------------------------------------------------------------------


class TestParseErpConfig:
    def test_no_connection_config_returns_none(self):
        # No enable flag exists anymore — absence of host/name/user is the
        # "off" state (empty dict, or an orphan flag row, both resolve to None).
        assert parse_erp_config({}) is None
        assert parse_erp_config({"erp_login_enabled": "false"}) is None

    def test_missing_host_returns_none(self):
        assert parse_erp_config({"erp_db_name": "d", "erp_db_user": "u"}) is None

    def test_active_without_any_flag(self):
        # Presence of host/name/user => active, with NO erp_login_enabled key.
        cfg = parse_erp_config({"erp_db_host": "h", "erp_db_name": "d", "erp_db_user": "u"})
        assert cfg is not None
        assert cfg.host == "h"

    def test_valid_config(self):
        cfg = _config()
        assert cfg is not None
        assert cfg.host == "db.example"
        assert cfg.port == 3306
        assert cfg.role_group_mapping == DEFAULT_ROLE_GROUP_MAPPING

    def test_default_mapping_applied_when_empty(self):
        cfg = parse_erp_config(
            {
                "erp_db_host": "h",
                "erp_db_name": "d",
                "erp_db_user": "u",
                "erp_role_group_mapping": "",
            }
        )
        assert cfg.role_group_mapping == DEFAULT_ROLE_GROUP_MAPPING

    def test_custom_mapping_parsed(self):
        cfg = parse_erp_config(
            {
                "erp_db_host": "h",
                "erp_db_name": "d",
                "erp_db_user": "u",
                "erp_role_group_mapping": '{"ADMIN": "Ops"}',
            }
        )
        assert cfg.role_group_mapping == {"ADMIN": "Ops"}

    def test_bad_port_falls_back_to_default(self):
        cfg = parse_erp_config(
            {
                "erp_db_host": "h",
                "erp_db_name": "d",
                "erp_db_user": "u",
                "erp_db_port": "not-a-number",
            }
        )
        assert cfg.port == 3306


class TestLoadErpEnvDefaults:
    def test_missing_or_empty_path_returns_empty(self, tmp_path):
        assert load_erp_env_defaults(None) == {}
        assert load_erp_env_defaults(tmp_path / "does-not-exist.env") == {}

    def test_parses_known_keys_and_filters_unknown(self, tmp_path):
        env = tmp_path / "erp.env"
        env.write_text(
            "erp_db_host=db.example\n"
            "erp_db_port=3307\n"
            "erp_db_name=Foundi_management_system\n"
            "erp_db_user=farm_reader\n"
            "erp_db_password=secret\n"
            "erp_db_ssl=true\n"
            "# a comment\n"
            "SOMETHING_ELSE=ignored\n"
        )
        loaded = load_erp_env_defaults(env)
        assert loaded["erp_db_host"] == "db.example"
        assert loaded["erp_db_port"] == "3307"
        assert loaded["erp_db_user"] == "farm_reader"
        assert loaded["erp_db_ssl"] == "true"
        assert "SOMETHING_ELSE" not in loaded
        # A file alone (no DB rows) yields a valid config — proves zero-config.
        assert parse_erp_config(loaded) is not None

    def test_malformed_file_does_not_raise(self, tmp_path):
        env = tmp_path / "erp.env"
        env.write_bytes(b"\x00\xff not valid utf8 \x80")
        # Never raises — returns whatever dotenv salvages (possibly {}).
        assert isinstance(load_erp_env_defaults(env), dict)


# --------------------------------------------------------------------------
# verify_bcrypt_password (canonical bcrypt verifier in core/auth.py)
# --------------------------------------------------------------------------


class TestVerifyBcryptPassword:
    def test_correct_password(self):
        assert verify_bcrypt_password(_PW, _HASH) is True

    def test_wrong_password(self):
        assert verify_bcrypt_password("nope", _HASH) is False

    def test_empty_inputs(self):
        assert verify_bcrypt_password("", _HASH) is False
        assert verify_bcrypt_password(_PW, None) is False

    def test_malformed_hash_returns_false(self):
        assert verify_bcrypt_password(_PW, "not-a-bcrypt-hash") is False

    def test_over_72_bytes_does_not_raise(self):
        # bcrypt>=4 raises on >72 bytes; the verifier truncates instead.
        assert verify_bcrypt_password("x" * 200, _HASH) is False


# --------------------------------------------------------------------------
# verify_password — hash-agnostic dispatch (pbkdf2 local vs bcrypt ERP mirror)
# --------------------------------------------------------------------------


class TestVerifyPasswordHashAgnostic:
    def test_bcrypt_hash_correct_password(self):
        # Would raise passlib UnknownHashError (-> HTTP 500) before the dispatch fix.
        assert verify_password(_PW, _HASH) is True

    def test_bcrypt_hash_wrong_password(self):
        assert verify_password("nope", _HASH) is False

    def test_pbkdf2_hash_still_verifies(self):
        # Regression: local users' pbkdf2 hashes go through the passlib path.
        pbkdf2_hash = get_password_hash(_PW)
        assert pbkdf2_hash.startswith("$pbkdf2-sha256$")
        assert verify_password(_PW, pbkdf2_hash) is True
        assert verify_password("nope", pbkdf2_hash) is False


# --------------------------------------------------------------------------
# authenticate_erp_user — tri-state classification
# --------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAuthenticateErpUser:
    async def test_ok(self):
        with patch.object(erp_directory, "_fetch_user_row", return_value=_row()):
            result = await authenticate_erp_user(_config(), "melissa.tam1", _PW)
        assert result.status == ErpAuthStatus.OK
        assert result.erp_user is not None
        assert result.erp_user.username == "melissa.tam1"
        assert result.erp_user.role == "ADMIN"
        assert result.erp_user.active is True
        assert result.erp_user.password_hash == _HASH

    async def test_rejected_wrong_password(self):
        with patch.object(erp_directory, "_fetch_user_row", return_value=_row()):
            result = await authenticate_erp_user(_config(), "melissa.tam1", "wrong")
        assert result.status == ErpAuthStatus.REJECTED
        assert result.erp_user is None

    async def test_rejected_row_not_found(self):
        with patch.object(erp_directory, "_fetch_user_row", return_value=None):
            result = await authenticate_erp_user(_config(), "ghost", _PW)
        assert result.status == ErpAuthStatus.REJECTED

    async def test_rejected_inactive(self):
        with patch.object(erp_directory, "_fetch_user_row", return_value=_row(active=0)):
            result = await authenticate_erp_user(_config(), "melissa.tam1", _PW)
        assert result.status == ErpAuthStatus.REJECTED

    async def test_rejected_locked_in_future(self):
        future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
        with patch.object(erp_directory, "_fetch_user_row", return_value=_row(locked_until=future)):
            result = await authenticate_erp_user(_config(), "melissa.tam1", _PW)
        assert result.status == ErpAuthStatus.REJECTED

    async def test_ok_when_lock_expired(self):
        past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        with patch.object(erp_directory, "_fetch_user_row", return_value=_row(locked_until=past)):
            result = await authenticate_erp_user(_config(), "melissa.tam1", _PW)
        assert result.status == ErpAuthStatus.OK

    async def test_unreachable_on_connection_error(self):
        def _boom(*_args, **_kwargs):
            raise OSError("connection refused")

        with patch.object(erp_directory, "_fetch_user_row", side_effect=_boom):
            result = await authenticate_erp_user(_config(), "melissa.tam1", _PW)
        assert result.status == ErpAuthStatus.UNREACHABLE
        assert result.erp_user is None

    async def test_empty_credentials_rejected_without_query(self):
        with patch.object(erp_directory, "_fetch_user_row") as fetch:
            result = await authenticate_erp_user(_config(), "melissa.tam1", "")
        assert result.status == ErpAuthStatus.REJECTED
        fetch.assert_not_called()
