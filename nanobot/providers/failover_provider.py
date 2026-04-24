"""Failover provider wrapper for automatic model failover."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from nanobot.providers.base import LLMProvider, LLMResponse

# Non-retryable 429 errors - quota/billing issues
_NON_RETRYABLE_429_ERROR_TOKENS = {
    "insufficient_quota",
    "quota_exceeded",
    "quota_exhausted",
    "billing_hard_limit_reached",
    "insufficient_balance",
    "credit_balance_too_low",
    "billing_not_active",
    "payment_required",
    "out_of_credits",
    "out_of_quota",
}

# Permanent error types that should NOT trigger failover
_PERMANENT_ERROR_TYPES = {
    "invalid_request",
    "invalid_api_key",
    "authentication_error",
    "permission_error",
    "not_found",
    "method_not_allowed",
}


class FailoverProvider(LLMProvider):
    """Provider that chains multiple providers and fails over between them.

    This wrapper transparently tries multiple providers/models in sequence when the
    primary model fails. Each provider gets its own retry attempts before failing
    over to the next provider in the chain.
    """

    def __init__(
        self,
        providers: list[LLMProvider],
        model_names: list[str],
        provider_retry_modes: list[str],
    ) -> None:
        """Initialize failover chain.

        Args:
            providers: List of LLMProvider instances to chain
            model_names: List of model names corresponding to each provider
            provider_retry_modes: List of retry modes ("standard" or "persistent")
                                for each provider
        """
        if not providers or not model_names or not provider_retry_modes:
            raise ValueError("Providers, model_names, and provider_retry_modes cannot be empty")
        if len(providers) != len(model_names) or len(providers) != len(provider_retry_modes):
            raise ValueError("providers, model_names, and provider_retry_modes must have same length")

        self._providers = providers
        self._model_names = model_names
        self._provider_retry_modes = provider_retry_modes

    def get_default_model(self) -> str:
        """Get the default model for this provider (always the first in chain)."""
        return self._model_names[0]

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Chat with failover support."""
        # Build attempt sequence
        attempts = list(zip(self._providers, self._model_names, self._provider_retry_modes))

        failed_attempts: list[tuple[str, str, str]] = []

        for idx, (provider, model_name, retry_mode) in enumerate(attempts):
            is_primary = (idx == 0)

            try:
                # Log attempt
                if not is_primary:
                    logger.warning(f"🔄 Model Failover: Trying {model_name}")

                # Call with retry
                result = await provider.chat_with_retry(
                    messages=messages,
                    tools=tools,
                    model=model_name,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    reasoning_effort=reasoning_effort,
                    tool_choice=tool_choice,
                    retry_mode=retry_mode,
                )

                # Check if successful
                if result.finish_reason != "error":
                    if not is_primary:
                        logger.info(f"✅ Failover successful: Using {model_name}")
                        # TODO: notify user via message bus
                    return result

                # Provider returned error response
                if self._should_failover_to_next(result):
                    # Extract error info for logging
                    error_type = result.error_type or "error"
                    error_msg = self._extract_error_message(result)
                    failed_attempts.append((model_name, error_type, error_msg))
                    logger.warning(f"  → {model_name} failed ({error_type}), trying next...")
                    continue
                else:
                    # Permanent error - don't failover
                    return result

            except Exception as e:
                error_type = type(e).__name__
                error_msg = str(e)
                failed_attempts.append((model_name, error_type, error_msg))

                if idx == len(attempts) - 1:
                    # Last provider failed
                    logger.error(f"All {len(attempts)} models failed. Last error: {e}")
                    raise
                else:
                    logger.warning(f"  → {model_name} failed ({error_type}), trying next...")
                    continue

        # This should not be reached due to raise above, but just in case
        return self._create_final_error_response(failed_attempts)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """Streaming chat with failover support."""
        # Build attempt sequence
        attempts = list(zip(self._providers, self._model_names, self._provider_retry_modes))

        failed_attempts: list[tuple[str, str, str]] = []

        for idx, (provider, model_name, retry_mode) in enumerate(attempts):
            is_primary = (idx == 0)

            try:
                # Log attempt
                if not is_primary:
                    logger.warning(f"🔄 Model Failover: Trying {model_name}")

                # For streaming, implement a stream wrapper that can capture errors
                stream_error: Exception | None = None

                async def _stream_wrapper(delta: str) -> None:
                    if on_content_delta:
                        await on_content_delta(delta)

                result = await provider.chat_stream_with_retry(
                    messages=messages,
                    tools=tools,
                    model=model_name,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    reasoning_effort=reasoning_effort,
                    tool_choice=tool_choice,
                    on_content_delta=_stream_wrapper if on_content_delta else None,
                    retry_mode=retry_mode,
                )

                if result.finish_reason != "error":
                    if not is_primary:
                        logger.info(f"✅ Failover successful: Using {model_name}")
                    return result

                # Provider returned error response
                if self._should_failover_to_next(result):
                    error_type = result.error_type or "error"
                    error_msg = self._extract_error_message(result)
                    failed_attempts.append((model_name, error_type, error_msg))
                    logger.warning(f"  → {model_name} failed ({error_type}), trying next...")
                    continue
                else:
                    return result

            except Exception as e:
                error_type = type(e).__name__
                error_msg = str(e)
                failed_attempts.append((model_name, error_type, error_msg))

                if idx == len(attempts) - 1:
                    logger.error(f"All {len(attempts)} models failed. Last error: {e}")
                    raise
                else:
                    logger.warning(f"  → {model_name} failed ({error_type}), trying next...")
                    continue

        return self._create_final_error_response(failed_attempts)

    async def _safe_chat(self, **kwargs: Any) -> LLMResponse:
        """Not used - FailoverProvider uses chat_with_retry directly."""
        raise NotImplementedError("FailoverProvider uses chat_with_retry")

    async def _safe_chat_stream(self, **kwargs: Any) -> LLMResponse:
        """Not used - FailoverProvider uses chat_stream_with_retry directly."""
        raise NotImplementedError("FailoverProvider uses chat_stream_with_retry")

    def _should_failover_to_next(self, response: LLMResponse) -> bool:
        """Determine if we should failover to next provider or stop."""
        # If not an error response at all, no need to failover
        if response.finish_reason != "error":
            return False

        # Check for non-retryable 429 (quota/billing)
        if response.error_code in _NON_RETRYABLE_429_ERROR_TOKENS:
            return False

        # Check for permanent error types
        if response.error_type in _PERMANENT_ERROR_TYPES:
            return False

        # Transient errors should trigger failover
        if response.error_status_code in {500, 502, 503, 504}:
            return True

        if response.error_kind in {"timeout", "connection"}:
            return True

        # By default, don't failover for unknown errors
        return False

    def _extract_error_message(self, response: LLMResponse) -> str:
        """Extract human-readable error message from response."""
        parts = []

        if response.error_type:
            parts.append(f"type={response.error_type}")

        if response.error_code:
            parts.append(f"code={response.error_code}")

        content = response.content or ""
        if content:
            parts.append(content[:100])

        return ",".join(parts) if parts else "unknown error"

    def _create_final_error_response(self, failed_attempts: list[tuple[str, str, str]]) -> LLMResponse:
        """Create error response when all providers failed."""
        if not failed_attempts:
            return LLMResponse(
                content="No providers available in failover chain",
                finish_reason="error",
                error_kind="failover",
            )

        error_msgs = []
        for model, error_type, error_msg in failed_attempts:
            error_msgs.append(f"- {model}: {error_type} {error_msg}")

        full_error = "All models failed\\n\\n" + "\\n".join(error_msgs)
        return LLMResponse(
            content=full_error,
            finish_reason="error",
            error_kind="failover",
        )


async def build_provider_chain(
    config: Any, primary_model: str, fallback_models: list[str]
) -> LLMProvider:
    """Build a failover chain of providers from a primary model and fallback list.

    Args:
        config: Config object with agents.defaults and provider configs
        primary_model: Primary model name (e.g., "ollama/llama3")
        fallback_models: List of fallback model names

    Returns:
        FailoverProvider if fallbacks exist, otherwise single provider
    """
    # Build models list
    all_models = [primary_model] + (fallback_models or [])

    # Build providers for each model
    providers = []
    model_names = []
    retry_modes = []

    for model in all_models:
        # Get provider and spec for this model
        provider_name = config.get_provider_name(model)
        provider_config = config.get_provider(model)

        # Create provider instance
        provider = _make_provider_from_config(config, provider_config, provider_name, model)

        # Get retry mode for this provider
        retry_mode = config.agents.defaults.provider_retry_mode

        providers.append(provider)
        model_names.append(model)
        retry_modes.append(retry_mode)

    # If only one provider, return it directly
    if len(providers) == 1:
        return providers[0]

    return FailoverProvider(providers, model_names, retry_modes)


def _make_provider_from_config(
    config: Any, provider_config: Any, provider_name: str | None, model: str
) -> LLMProvider:
    """Create a provider instance from config.

    This extracts the provider creation logic from nanobot._make_provider,
    allowing it to be reused for each model in a failover chain.
    """
    from nanobot.providers.base import GenerationSettings
    from nanobot.providers.registry import find_by_name

    spec = find_by_name(provider_name) if provider_name else None
    backend = spec.backend if spec else "openai_compat"

    if backend == "azure_openai":
        if not provider_config or not provider_config.api_key or not provider_config.api_base:
            raise ValueError("Azure OpenAI requires api_key and api_base in config.")
    elif backend == "openai_compat" and not model.startswith("bedrock/"):
        needs_key = not (provider_config and provider_config.api_key)
        exempt = spec and (spec.is_oauth or spec.is_local or spec.is_direct)
        if needs_key and not exempt:
            raise ValueError(f"No API key configured for provider '{provider_name}'.")

    if backend == "openai_codex":
        from nanobot.providers.openai_codex_provider import OpenAICodexProvider

        provider = OpenAICodexProvider(default_model=model)
    elif backend == "github_copilot":
        from nanobot.providers.github_copilot_provider import GitHubCopilotProvider

        provider = GitHubCopilotProvider(default_model=model)
    elif backend == "azure_openai":
        from nanobot.providers.azure_openai_provider import AzureOpenAIProvider

        provider = AzureOpenAIProvider(
            api_key=provider_config.api_key, api_base=provider_config.api_base, default_model=model
        )
    elif backend == "anthropic":
        from nanobot.providers.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(
            api_key=provider_config.api_key if provider_config else None,
            api_base=config.get_api_base(model),
            default_model=model,
            extra_headers=provider_config.extra_headers if provider_config else None,
        )
    else:
        from nanobot.providers.openai_compat_provider import OpenAICompatProvider

        provider = OpenAICompatProvider(
            api_key=provider_config.api_key if provider_config else None,
            api_base=config.get_api_base(model),
            default_model=model,
            extra_headers=provider_config.extra_headers if provider_config else None,
            spec=spec,
        )

    # Set generation settings
    defaults = config.agents.defaults
    provider.generation = GenerationSettings(
        temperature=defaults.temperature,
        max_tokens=defaults.max_tokens,
        reasoning_effort=defaults.reasoning_effort,
    )

    return provider
