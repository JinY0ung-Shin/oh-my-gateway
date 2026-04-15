#!/usr/bin/env python3
"""
Unit tests for src/parameter_validator.py

Tests the ParameterValidator class.
These are pure unit tests that don't require a running server.
"""

from unittest.mock import patch

from src.constants import DEFAULT_MODEL
from src.parameter_validator import ParameterValidator


class TestParameterValidatorValidateModel:
    """Test ParameterValidator.validate_model()"""

    def test_valid_model_returns_true(self):
        """Known supported model returns True."""
        result = ParameterValidator.validate_model(DEFAULT_MODEL)
        assert result is True

    def test_unknown_model_returns_false_with_warning(self):
        """Unknown model returns False with warning logged."""
        with patch("src.parameter_validator.logger") as mock_logger:
            result = ParameterValidator.validate_model("unknown-model-xyz")
            assert result is False
            mock_logger.warning.assert_called_once()
            assert "unknown-model-xyz" in str(mock_logger.warning.call_args)

    def test_all_known_models_valid(self):
        """All models in supported models set are valid."""
        for model in ParameterValidator._get_supported_models():
            assert ParameterValidator.validate_model(model) is True


