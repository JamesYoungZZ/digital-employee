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
    """Client supporting OpenAI / DeepSeek V4 / local vLLM chat & tool calling."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.llm_base_url.rstrip("/")

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
        if not self.settings.llm_api_key and self.settings.inference_mode == "cloud_api":
            raise LLMConfigurationError(
                "Missing DE_LLM_API_KEY (or DEEPSEEK_API_KEY / OPENAI_API_KEY)."
            )

        chosen_model = model or self.settings.reasoning_model
        payload: dict[str, Any] = {
            "model": chosen_model,
            "messages": self._scrub_messages(messages),
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"

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

    def complete_json(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        max_tokens: int = 4096,
    ) -> tuple[dict[str, Any], LLMCallMetrics]:
        msg, metrics = self.chat(
            messages,
            model=model,
            json_mode=True,
            max_tokens=max_tokens,
        )
        parsed = extract_json_object(msg.get("content") or "")
        return parsed, metrics
