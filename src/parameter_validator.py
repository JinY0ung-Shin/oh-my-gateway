"""
Parameter validation and mapping utilities for OpenAI to Claude Code SDK conversion.
"""

import logging
from typing import Dict, Any
from src.models import ChatCompletionRequest
from src.constants import ALL_MODELS

logger = logging.getLogger(__name__)


class ParameterValidator:
    """Validates and maps OpenAI Chat Completions parameters to Claude Code SDK options."""

    # Use combined model list from constants (single source of truth)
    SUPPORTED_MODELS = set(ALL_MODELS)

    @classmethod
    def validate_model(cls, model: str) -> bool:
        """Validate that the model is supported.

        Supports slash-delimited patterns like ``codex/o3``: the base prefix
        (before the first ``/``) is checked against the known model list.
        Unknown models are warned but still allowed for graceful degradation.
        """
        base_model = model.split("/")[0] if "/" in model else model
        if base_model not in cls.SUPPORTED_MODELS:
            logger.warning(
                f"Model '{model}' is not in the known supported models list. "
                f"It will still be attempted but may fail. "
                f"Supported models: {sorted(cls.SUPPORTED_MODELS)}"
            )
            # Return True anyway to allow graceful degradation
        return True

    @classmethod
    def extract_claude_headers(cls, headers: Dict[str, str]) -> Dict[str, Any]:
        """
        Extract Claude-Code-specific parameters from custom HTTP headers.

        This allows clients to pass SDK-specific options via headers:
        - X-Claude-Max-Turns: 5
        - X-Claude-Allowed-Tools: tool1,tool2,tool3
        - X-Claude-Permission-Mode: acceptEdits
        """
        claude_options = {}

        # Extract max_turns
        if "x-claude-max-turns" in headers:
            try:
                claude_options["max_turns"] = int(headers["x-claude-max-turns"])
            except ValueError:
                logger.warning(
                    f"Invalid X-Claude-Max-Turns header: {headers['x-claude-max-turns']}"
                )

        # Extract allowed tools
        if "x-claude-allowed-tools" in headers:
            tools = [tool.strip() for tool in headers["x-claude-allowed-tools"].split(",")]
            if tools:
                claude_options["allowed_tools"] = tools

        # Extract disallowed tools
        if "x-claude-disallowed-tools" in headers:
            tools = [tool.strip() for tool in headers["x-claude-disallowed-tools"].split(",")]
            if tools:
                claude_options["disallowed_tools"] = tools

        # Extract permission mode
        if "x-claude-permission-mode" in headers:
            claude_options["permission_mode"] = headers["x-claude-permission-mode"]

        return claude_options


class CompatibilityReporter:
    """Reports on OpenAI API compatibility and suggests alternatives."""

    @classmethod
    def generate_compatibility_report(cls, request: ChatCompletionRequest) -> Dict[str, Any]:
        """Generate a detailed compatibility report for the request."""
        report = {
            "supported_parameters": [],
            "unsupported_parameters": [],
            "warnings": [],
            "suggestions": [],
        }

        # Check supported parameters
        if request.model:
            report["supported_parameters"].append("model")
        if request.messages:
            report["supported_parameters"].append("messages")
        if request.stream is not None:
            report["supported_parameters"].append("stream")
        if request.user:
            report["supported_parameters"].append("user (for logging)")

        # Check unsupported parameters with suggestions
        if request.temperature != 1.0:
            report["unsupported_parameters"].append("temperature")
            report["suggestions"].append(
                "Claude Code SDK does not support temperature control. Consider using different models for varied response styles (e.g., claude-3-5-haiku for more focused responses)."
            )

        if request.top_p != 1.0:
            report["unsupported_parameters"].append("top_p")
            report["suggestions"].append(
                "Claude Code SDK does not support top_p. This parameter will be ignored."
            )

        if request.max_tokens:
            report["unsupported_parameters"].append("max_tokens")
            report["suggestions"].append(
                "max_tokens is not supported by Claude Agent SDK and will be ignored. Use max_turns to limit conversation length."
            )

        if request.n > 1:
            report["unsupported_parameters"].append("n")
            report["suggestions"].append(
                "Claude Code SDK only supports single responses (n=1). For multiple variations, make separate API calls."
            )

        if request.stop:
            report["unsupported_parameters"].append("stop")
            report["suggestions"].append(
                "Stop sequences are not supported. Consider post-processing responses or using max_turns to limit output."
            )

        if request.presence_penalty != 0 or request.frequency_penalty != 0:
            report["unsupported_parameters"].extend(["presence_penalty", "frequency_penalty"])
            report["suggestions"].append(
                "Penalty parameters are not supported. Consider using different system prompts to encourage varied responses."
            )

        if request.logit_bias:
            report["unsupported_parameters"].append("logit_bias")
            report["suggestions"].append(
                "Logit bias is not supported. Consider using system prompts to guide response style."
            )

        return report
