"""OpenAI 兼容协议提供方。

一次实现覆盖：OpenAI、DeepSeek、阿里通义(DashScope 兼容模式)、智谱 GLM、
月之暗面 Kimi、硅基流动、以及本地 Ollama / vLLM / LM Studio 等自建服务。
只需在配置里改 base_url 与 model。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence

from .base import ChatResult, LLMError, LLMProvider, Message, ProviderConfigError, est_tokens


class OpenAICompatProvider(LLMProvider):
    name = "openai"

    def __init__(self, model: str, api_key: str = "", base_url: str = "https://api.openai.com/v1",
                 timeout: int = 60, extra: Optional[Dict[str, Any]] = None, provider_name: str = "openai",
                 supports_vision: bool = False) -> None:
        super().__init__(model, api_key, base_url or "https://api.openai.com/v1", timeout, extra,
                         supports_vision=supports_vision)
        self.name = provider_name

    def chat(
        self,
        messages: Sequence[Message],
        temperature: float = 0.3,
        max_tokens: int = 2048,
        json_schema: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        stop: Optional[Sequence[str]] = None,
    ) -> ChatResult:
        if not self.base_url:
            raise ProviderConfigError(f"{self.name}: 缺少 base_url")
        url = f"{self.base_url}/chat/completions"
        # 含图片时使用 content parts；纯文本仍用字符串（兼容性最好）
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_openai_message() for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # 推理强度 / 思维链开关（DeepSeek-V4 等推理型模型支持）。在 config.json 对应提供方下写：
        #   "reasoning_effort": "low" | "medium" | "high"
        #   "thinking": { "type": "disabled" }      ← 直接关掉思维链，答疑场景最快
        # 实测（deepseek-flash，同一道"一句话解释导数"）：
        #   默认          0.8s / 输出 74 token / 思维链 98 字
        #   thinking 关闭 1.3s / 输出 25 token / 思维链 0 字
        # 注意：真正决定耗时的是**我的精细提示要求长输出**（六大模块契约 + 段落标记），
        # 简单问题在精细提示下也会产出 1900+ 字，因此这里只是把"额外开销"关掉。
        effort = (self.extra or {}).get("reasoning_effort")
        if effort:
            payload["reasoning_effort"] = effort
        thinking = (self.extra or {}).get("thinking")
        if thinking:
            payload["thinking"] = thinking
        # JSON 模式（response_format=json_object）默认**关闭**。
        # 实测 DeepSeek-V4 开启后会出现三种静默损坏（finish_reason 仍是 stop）：
        #   ① 正文开头被吃掉（"【题型判断】…" 变成 "{0}{0}\)…"）
        #   ② 只返回 JSON，正文整段消失
        #   ③ 返回大量前导空白 + JSON，正文被压到 JSON 里
        # 我们的输出契约本身已经要求"正文 + 末尾附 JSON"，不依赖这个参数；
        # 需要严格 JSON 的提供方（如 OpenAI）可在 config.json 里显式打开 json_mode。
        json_mode = bool((self.extra or {}).get("json_mode"))
        if json_schema and self.supports_json_mode and json_mode:
            if "json" in "\n".join(m.content for m in messages).lower():
                payload["response_format"] = {"type": "json_object"}
        if tools and self.supports_tools:
            payload["tools"] = tools
        if stop:
            payload["stop"] = list(stop)

        headers = {"Authorization": f"Bearer {self.api_key}"}
        headers.update(self.extra.get("headers", {}))

        t0 = time.perf_counter()
        data = self._post(url, payload, headers)
        elapsed = int((time.perf_counter() - t0) * 1000)

        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"{self.name}: 返回中没有 choices 字段: {str(data)[:200]}")
        choice = choices[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        tool_calls = message.get("tool_calls") or []
        usage = data.get("usage") or {}
        prompt_text = "\n".join(m.content for m in messages)

        structured = None
        if json_schema:
            structured = self.extract_json(text)
            if structured is None and tool_calls:
                args = (tool_calls[0].get("function") or {}).get("arguments") or ""
                structured = self.extract_json(args)

        # 注意：部分模型（实测 DeepSeek 在 JSON 模式下）会在正文前返回大量空白字符，
        # 直接使用会导致长度统计、相似度与质检全部失真，因此统一剥除首尾空白。
        text = (text or "").strip()
        if not text and tool_calls:
            text = tool_calls[0]["function"]["arguments"]

        return ChatResult(
            text=text,
            provider=self.name,
            model=data.get("model", self.model),
            elapsed_ms=elapsed,
            prompt_tokens=int(usage.get("prompt_tokens") or est_tokens(prompt_text)),
            completion_tokens=int(usage.get("completion_tokens") or est_tokens(text)),
            finish_reason=choice.get("finish_reason", ""),
            structured=structured,
            raw=data,
        )
