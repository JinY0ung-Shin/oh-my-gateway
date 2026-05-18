"""Tests for backend shared helpers."""

from src.backends.common import estimate_token_usage, parse_csv


def test_parse_csv_strips_empty_values_and_preserves_first_occurrence():
    assert parse_csv(" alpha, beta, alpha, ,gamma ,, beta ") == [
        "alpha",
        "beta",
        "gamma",
    ]


def test_estimate_token_usage_preserves_backend_length_heuristic():
    assert estimate_token_usage("a" * 40, "b" * 3) == {
        "prompt_tokens": 10,
        "completion_tokens": 1,
        "total_tokens": 11,
    }
