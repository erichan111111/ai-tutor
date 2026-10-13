"""提供方注册表与统一客户端。

职责：
1) 按配置文件构造所有提供方实例（缺 Key 的标记为不可用，而不是报错）；
2) 提供带**自动降级**的调用入口：远端异常 → 回退到 mock，并在结果中标记降级原因；
3) 供 CLI / Web / 评测脚本共用，保证"同一模型、同一参数"的控制变量法可复现。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Sequence

from .anthropic import AnthropicProvider
from .base import ChatResult, LLMError, LLMProvider, Message
from .mock import MockProvider
from .openai_compat import OpenAICompatProvider
from .qianfan import QianfanProvider

# 各 provider 的默认 base_url
DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "moonshot": "https://api.moonshot.cn/v1",
    "siliconflow": "https://api.siliconflow.cn/v1",
    "ollama": "http://localhost:11434/v1",
    "vllm": "http://localhost:8000/v1",
    "openai_compat": "",
}

_OPENAI_LIKE = set(DEFAULT_BASE_URLS)


class LLMClient:
    """持有全部提供方，负责路由、降级与调用统计。"""

    def __init__(self, providers: Dict[str, LLMProvider], default: str = "mock",
                 fallback_to_mock: bool = True) -> None:
        self.providers = providers
        self.default = default if default in providers else "mock"
        self.fallback_to_mock = fallback_to_mock
        self._lock = threading.Lock()
        self.stats: Dict[str, Dict[str, int]] = {}

    # ------------------------------------------------------------ 查询
    def names(self) -> List[str]:
        return list(self.providers.keys())

    def get(self, name: Optional[str]) -> LLMProvider:
        key = name or self.default
        if key not in self.providers:
            raise KeyError(f"未配置的模型提供方: {key}（可用: {', '.join(self.names())}）")
        return self.providers[key]

    def available_names(self) -> List[str]:
        return [n for n, p in self.providers.items() if p.available()]

    def usable_names(self) -> List[str]:
        """真正可用于对比的模型（排除内置 mock，仅保留已配置凭证的提供方）。"""
        return [n for n, p in self.providers.items() if n != "mock" and p.available()]

    def vision_names(self) -> List[str]:
        """支持图片输入的模型。"""
        return [n for n, p in self.providers.items() if p.available() and p.supports_vision]

    def pick_vision_model(self, preferred: Optional[str] = None) -> Optional[str]:
        """为图片请求挑选一个支持视觉的模型。

        规则：优先用户指定（若它支持图片）→ 默认模型（若支持）→ 其它可用视觉模型 → None。
        """
        if preferred:
            provider = self.providers.get(preferred)
            if provider is not None and provider.available() and provider.supports_vision:
                return preferred
        default = self.providers.get(self.default)
        if default is not None and default.available() and default.supports_vision:
            return self.default
        candidates = self.vision_names()
        return candidates[0] if candidates else None

    def describe(self) -> List[Dict[str, Any]]:
        out = []
        for name, provider in self.providers.items():
            info = provider.describe()
            info["is_default"] = name == self.default
            out.append(info)
        return out

    # ------------------------------------------------------------ 调用
    def chat(
        self,
        messages: Sequence[Message],
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
        json_schema: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        stop: Optional[Sequence[str]] = None,
        allow_fallback: Optional[bool] = None,
    ) -> ChatResult:
        fallback = self.fallback_to_mock if allow_fallback is None else allow_fallback
        name = model or self.default
        provider = self.get(name)

        if not provider.available():
            if not fallback:
                raise LLMError(f"提供方 {name} 不可用（缺少 API Key）且已禁用降级")
            return self._mock_result(messages, temperature, max_tokens, json_schema, tools,
                                     reason=f"{name} 未配置 API Key")

        try:
            result = provider.chat(messages, temperature, max_tokens, json_schema, tools, stop)
            self._count(name, ok=True)
            return result
        except Exception as exc:  # noqa: BLE001 - 需要把任何远端异常转为降级
            self._count(name, ok=False)
            if not fallback:
                raise
            return self._mock_result(messages, temperature, max_tokens, json_schema, tools,
                                     reason=f"{name} 调用失败: {exc}")

    def _mock_result(self, messages, temperature, max_tokens, json_schema, tools, reason: str) -> ChatResult:
        mock = self.providers.get("mock") or MockProvider("mock-offline")
        result = mock.chat(messages, temperature, max_tokens, json_schema, tools)
        result.degraded = True
        result.degraded_reason = reason
        # 产出内容来自内置离线模型，必须保留 simulated 标记；
        # 否则真实模型降级后，离线内容会被当成真实模型结果统计（严重的数据失真）。
        result.simulated = True
        self._count("mock(fallback)", ok=True)
        return result

    def _count(self, name: str, ok: bool) -> None:
        with self._lock:
            slot = self.stats.setdefault(name, {"ok": 0, "fail": 0})
            slot["ok" if ok else "fail"] += 1

    def stats_snapshot(self) -> Dict[str, Dict[str, int]]:
        with self._lock:
            return {k: dict(v) for k, v in self.stats.items()}


# ----------------------------------------------------------------------
# 构造
# ----------------------------------------------------------------------

#: 允许直接写在提供方配置顶层的透传参数（会自动并入 extra 传给请求体）。
#: 这样用户不必写成嵌套的 "extra": {...}，配置更直观：
#:   "deepseek": { "model": "deepseek-flash", "reasoning_effort": "low", ... }
_EXTRA_PASSTHROUGH = ("reasoning_effort", "thinking", "top_p", "frequency_penalty",
                      "presence_penalty", "seed", "stop", "headers")


def _build_provider(name: str, cfg: Dict[str, Any]) -> LLMProvider:
    timeout = int(cfg.get("timeout", 60))
    extra = dict(cfg.get("extra") or {})
    for key in _EXTRA_PASSTHROUGH:
        if key in cfg and key not in extra:
            extra[key] = cfg[key]
    vision = bool(cfg.get("vision", False))
    if name == "mock":
        return MockProvider(cfg.get("model") or "mock-offline", extra=extra)
    if name == "anthropic":
        return AnthropicProvider(
            model=cfg.get("model", "claude-3-5-sonnet-latest"),
            api_key=cfg.get("api_key", ""),
            base_url=cfg.get("base_url") or "https://api.anthropic.com/v1",
            timeout=timeout, extra=extra, supports_vision=cfg.get("vision", True),
        )
    if name == "qianfan":
        return QianfanProvider(
            model=cfg.get("model", "ernie-4.0-8k"),
            api_key=cfg.get("api_key", ""),
            secret_key=cfg.get("secret_key", ""),
            timeout=timeout,
            extra=extra,
            endpoints=cfg.get("endpoints"),
        )
    if name in _OPENAI_LIKE:
        return OpenAICompatProvider(
            model=cfg.get("model", "gpt-4o"),
            api_key=cfg.get("api_key", ""),
            base_url=cfg.get("base_url") or DEFAULT_BASE_URLS[name],
            timeout=timeout,
            extra=extra,
            provider_name=name,
            supports_vision=vision,
        )
    raise KeyError(f"未知的提供方类型: {name}")


def get_client(config: Optional[Dict[str, Any]] = None, providers: Optional[List[str]] = None) -> LLMClient:
    """按配置构造客户端。providers 可用于只加载子集（如横向对比只加载 3 个）。"""
    config = config or {}
    llm_cfg = config.get("llm", {}) if "llm" in config else config
    models_cfg: Dict[str, Any] = llm_cfg.get("models", {})
    wanted = providers or list(models_cfg.keys())
    built: Dict[str, LLMProvider] = {}
    for name in wanted:
        cfg = models_cfg.get(name)
        if not cfg:
            continue
        try:
            built[name] = _build_provider(name, cfg)
        except KeyError:
            continue
    if "mock" not in built:
        built["mock"] = MockProvider("mock-offline")
    return LLMClient(
        built,
        default=llm_cfg.get("default", "mock"),
        fallback_to_mock=bool(llm_cfg.get("fallback_to_mock", True)),
    )


_DEFAULT_CLIENT: Optional[LLMClient] = None


def get_default_client(reload: bool = False) -> LLMClient:
    """进程级默认客户端（读取 ai_tutor 配置）。"""
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None or reload:
        from ..config import load_config
        _DEFAULT_CLIENT = get_client(load_config())
    return _DEFAULT_CLIENT


def list_models(config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    return get_client(config).describe()
