"""Tests for env_utils — boolean and integer environment variable parsing."""

import os
from unittest.mock import patch


from src.env_utils import parse_bool_env, parse_int_env


class TestParseBoolEnv:
    def test_true_values(self):
        for val in ("true", "True", "TRUE", "1", "yes", "Yes", "on", "ON"):
            with patch.dict(os.environ, {"TEST_BOOL": val}):
                assert parse_bool_env("TEST_BOOL") is True

    def test_false_values(self):
        for val in ("false", "False", "FALSE", "0", "no", "No", "off", "OFF"):
            with patch.dict(os.environ, {"TEST_BOOL": val}):
                assert parse_bool_env("TEST_BOOL") is False

    def test_unset_uses_default_false(self):
        with patch.dict(os.environ, {}, clear=True):
            assert parse_bool_env("UNSET_VAR") is False

    def test_unset_uses_default_true(self):
        with patch.dict(os.environ, {}, clear=True):
            assert parse_bool_env("UNSET_VAR", default="true") is True

    def test_unrecognized_value_treated_as_false(self):
        with patch.dict(os.environ, {"TEST_BOOL": "maybe"}):
            assert parse_bool_env("TEST_BOOL") is False

    def test_empty_string_treated_as_false(self):
        with patch.dict(os.environ, {"TEST_BOOL": ""}):
            assert parse_bool_env("TEST_BOOL") is False


class TestParseIntEnv:
    def test_valid_integer(self):
        with patch.dict(os.environ, {"TEST_INT": "42"}):
            assert parse_int_env("TEST_INT", default=0) == 42

    def test_negative_integer(self):
        with patch.dict(os.environ, {"TEST_INT": "-5"}):
            assert parse_int_env("TEST_INT", default=0) == -5

    def test_unset_uses_default(self):
        with patch.dict(os.environ, {}, clear=True):
            assert parse_int_env("UNSET_VAR", default=99) == 99

    def test_invalid_value_uses_default(self):
        with patch.dict(os.environ, {"TEST_INT": "not_a_number"}):
            assert parse_int_env("TEST_INT", default=10) == 10

    def test_empty_string_uses_default(self):
        with patch.dict(os.environ, {"TEST_INT": ""}):
            assert parse_int_env("TEST_INT", default=7) == 7

    def test_float_string_uses_default(self):
        with patch.dict(os.environ, {"TEST_INT": "3.14"}):
            assert parse_int_env("TEST_INT", default=3) == 3

    def test_zero(self):
        with patch.dict(os.environ, {"TEST_INT": "0"}):
            assert parse_int_env("TEST_INT", default=5) == 0
