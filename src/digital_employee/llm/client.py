"""OpenAI-compatible LLM client with DLP redaction, reasoning preservation & cost tracking."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from digital_employee.security.dlp import redact_sensitive_text
from digital_employee.settings import Settings

JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class TransientLLMError(RuntimeError):
    """Raised on network timeouts, HTTP 429, or 5xx responses (`TRANSIENT_INFRA`)."""


class LLMConfigurationError(RuntimeError):
    """Raised when API credentials or endpoints are missing/invalid."""


@dataclass
class LLMCallMetrics:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_usd: float = 0.0


def extract_json_object(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    if not text:
        raise ValueError("LLM returned empty content when JSON object was expected.")
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    fence_match = JSON_FENCE_RE.search(text)
    if fence_match:
        return json.loads(fence_match.group(1))

    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last != -1 and last > first:
        return json.loads(text[first : last + 1])
    raise ValueError(f"Could not parse JSON object from LLM response: {text[:300]}")


class LLMClient:
    """Client supporting OpenAI-compatible, DeepSeek V4, and Anthropic Messages APIs."""

    def __init__(
        self,
        settings: Settings,
        *,
        provider: str = "openai_compatible",
        base_url: str | None = None,
        api_key: str | None = None,
        default_model: str | None = None,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.llm_api_key
        self.default_model = default_model or settings.reasoning_model

    def _scrub_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.settings.inference_mode != "cloud_api":
            return messages
        scrubbed: list[dict[str, Any]] = []
        for msg in messages:
            copy_msg = dict(msg)
            if isinstance(copy_msg.get("content"), str):
                copy_msg["content"] = redact_sensitive_text(copy_msg["content"])
            scrubbed.append(copy_msg)
        return scrubbed

    def _estimate_cost(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cached_prompt_tokens: int = 0,
    ) -> float:
        # Cache-aware per-1M token rates (USD) matching DeepSeek KV-cache billing
        is_pro = "pro" in model.lower() or "reasoner" in model.lower() or "gpt-4" in model.lower()
        miss_in_rate = 0.55 if is_pro else 0.15
        hit_in_rate = 0.07 if is_pro else 0.015
        out_rate = 2.19 if is_pro else 0.60
        cached = max(0, min(prompt_tokens, cached_prompt_tokens))
        uncached = max(0, prompt_tokens - cached)
        return round(
            (uncached * miss_in_rate + cached * hit_in_rate + completion_tokens * out_rate)
            / 1_000_000.0,
            6,
        )

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        json_mode: bool = False,
        max_tokens: int = 4096,
        max_retries: int = 2,
    ) -> tuple[dict[str, Any], LLMCallMetrics]:
        if not self.api_key and self.settings.inference_mode == "cloud_api":
            raise LLMConfigurationError(
                "Missing LLM API key (DE_LLM_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY)."
            )

        chosen_model = model or self.default_model
        scrubbed_msgs = self._scrub_messages(messages)

        if self.provider == "anthropic":
            return self._chat_anthropic(
                scrubbed_msgs, chosen_model=chosen_model, max_tokens=max_tokens, max_retries=max_retries
            )

        payload: dict[str, Any] = {
            "model": chosen_model,
            "messages": scrubbed_msgs,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        url = f"{self.base_url}/chat/completions"
        last_err: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                with httpx.Client(timeout=self.settings.llm_timeout_seconds) as client:
                    resp = client.post(url, headers=headers, json=payload)
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise TransientLLMError(
                        f"LLM HTTP {resp.status_code}: {resp.text[:250]}"
                    )
                if resp.status_code >= 400:
                    raise RuntimeError(
                        f"LLM non-retryable HTTP {resp.status_code}: {resp.text[:350]}"
                    )
                body = resp.json()
                raw_msg = body["choices"][0]["message"]
                usage = body.get("usage") or {}
                p_tok = int(usage.get("prompt_tokens", 0))
                c_tok = int(usage.get("completion_tokens", 0))
                cached_tok = int(
                    usage.get("prompt_cache_hit_tokens")
                    or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                    or 0
                )

                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": raw_msg.get("content") or "",
                }
                if "reasoning_content" in raw_msg and raw_msg["reasoning_content"] is not None:
                    assistant_msg["reasoning_content"] = raw_msg["reasoning_content"]
                if raw_msg.get("tool_calls"):
                    assistant_msg["tool_calls"] = raw_msg["tool_calls"]

                metrics = LLMCallMetrics(
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    estimated_cost_usd=self._estimate_cost(
                        chosen_model, p_tok, c_tok, cached_tok
                    ),
                )
                return assistant_msg, metrics
            except (httpx.TimeoutException, httpx.NetworkError, TransientLLMError) as exc:
                last_err = exc
                if attempt < max_retries:
                    time.sleep(1.5 * (2**attempt))
                    continue
                raise TransientLLMError(f"LLM request failed after retries: {exc}") from exc

        raise TransientLLMError(f"LLM request failed: {last_err}")

    def _chat_anthropic(
        self,
        messages: list[dict[str, Any]],
        *,
        chosen_model: str,
        max_tokens: int,
        max_retries: int,
    ) -> tuple[dict[str, Any], LLMCallMetrics]:
        sys_parts = [m["content"] for m in messages if m.get("role") == "system" and m.get("content")]
        chat_msgs = [
            {"role": m["role"], "content": m.get("content") or ""}
            for m in messages
            if m.get("role") in ("user", "assistant")
        ]
        payload: dict[str, Any] = {
            "model": chosen_model,
            "max_tokens": max_tokens,
            "messages": chat_msgs,
        }
        if sys_parts:
            payload["system"] = "\n\n".join(sys_parts)

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        url = f"{self.base_url}/v1/messages" if not self.base_url.endswith("/v1") else f"{self.base_url}/messages"

        for attempt in range(max_retries + 1):
            try:
                with httpx.Client(timeout=self.settings.llm_timeout_seconds) as client:
                    resp = client.post(url, headers=headers, json=payload)
                if resp.status_code in (429, 500, 502, 503, 504, 529):
                    raise TransientLLMError(f"Anthropic HTTP {resp.status_code}: {resp.text[:250]}")
                if resp.status_code >= 400:
                    raise RuntimeError(f"Anthropic HTTP {resp.status_code}: {resp.text[:350]}")
                body = resp.json()
                text_blocks = [
                    b.get("text", "")
                    for b in (body.get("content") or [])
                    if b.get("type") == "text"
                ]
                usage = body.get("usage") or {}
                p_tok = int(usage.get("input_tokens", 0))
                c_tok = int(usage.get("output_tokens", 0))
                cached = int(usage.get("cache_read_input_tokens", 0))
                metrics = LLMCallMetrics(
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    estimated_cost_usd=self._estimate_cost(chosen_model, p_tok, c_tok, cached),
                )
                return {"role": "assistant", "content": "\n".join(text_blocks)}, metrics
            except (httpx.TimeoutException, httpx.NetworkError, TransientLLMError) as exc:
                if attempt < max_retries:
                    time.sleep(1.5 * (2**attempt))
                    continue
                raise TransientLLMError(f"Anthropic request failed: {exc}") from exc
        raise TransientLLMError("Anthropic request failed")

    def complete_json(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        max_tokens: int = 8192,
    ) -> tuple[dict[str, Any], LLMCallMetrics]:
        msg, metrics = self.chat(
            messages,
            model=model,
            json_mode=True,
            max_tokens=max_tokens,
        )
        content = (msg.get("content") or "").strip()
        if not content:
            # Reasoning models may exhaust max_tokens in reasoning_content; retry with concise directive
            retry_msgs = [
                *messages,
                {
                    "role": "user",
                    "content": "Keep internal reasoning brief and output ONLY the final JSON object now.",
                },
            ]
            msg2, metrics2 = self.chat(
                retry_msgs,
                model=model,
                json_mode=True,
                max_tokens=8192,
            )
            metrics = LLMCallMetrics(
                prompt_tokens=metrics.prompt_tokens + metrics2.prompt_tokens,
                completion_tokens=metrics.completion_tokens + metrics2.completion_tokens,
                estimated_cost_usd=round(
                    metrics.estimated_cost_usd + metrics2.estimated_cost_usd, 6
                ),
            )
            content = (msg2.get("content") or "").strip()
        parsed = extract_json_object(content)
        return parsed, metrics
