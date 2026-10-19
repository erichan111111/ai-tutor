"""配置加载：config.json + 环境变量覆盖。

优先级：环境变量 > 配置文件 > 代码默认值。
环境变量约定（与各家 SDK 习惯一致）：
    OPENAI_API_KEY / DEEPSEEK_API_KEY / DASHSCOPE_API_KEY / ZHIPU_API_KEY /
    MOONSHOT_API_KEY / SILICONFLOW_API_KEY / ANTHROPIC_API_KEY /
    QIANFAN_API_KEY / QIANFAN_SECRET_KEY
    AI_TUTOR_MODEL      覆盖默认模型名（如 deepseek / openai）
    AI_TUTOR_DATA_DIR   覆盖数据目录
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"

ENV_KEY_MAP = {
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
    "zhipu": "ZHIPU_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "siliconflow": "SILICONFLOW_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "qianfan": "QIANFAN_API_KEY",
}

DEFAULT_CONFIG: Dict[str, Any] = {
    "app": {
        "name": "大学生期末复习 AI 答疑助手",
        "team": "第二组：李明轩　韩武晋　郑志",
        "version": "1.3.0",
        "data_dir": "data",
        "log_dir": "data/logs",
        "max_history_turns": 8,
    },
    "llm": {
        "default": "deepseek",
        "fallback_to_mock": True,
        "models": {
            "mock": {"model": "mock-offline", "timeout": 60},
            "openai": {"model": "gpt-4o", "base_url": "https://api.openai.com/v1", "api_key": "", "timeout": 90},
            # DeepSeek：deepseek-flash (DeepSeek-V4.1-Flash) 同时支持文本与图片输入
            "deepseek": {"model": "deepseek-flash", "base_url": "https://api.deepseek.com/v1",
                         "api_key": "", "timeout": 180, "vision": True},
            "dashscope": {"model": "qwen-plus", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "api_key": "", "timeout": 90},
            "zhipu": {"model": "glm-4-plus", "base_url": "https://open.bigmodel.cn/api/paas/v4", "api_key": "", "timeout": 90},
            "moonshot": {"model": "moonshot-v1-8k", "base_url": "https://api.moonshot.cn/v1", "api_key": "", "timeout": 90},
            "siliconflow": {"model": "Qwen/Qwen2.5-7B-Instruct", "base_url": "https://api.siliconflow.cn/v1", "api_key": "", "timeout": 90},
            "anthropic": {"model": "claude-3-5-sonnet-latest", "base_url": "https://api.anthropic.com/v1", "api_key": "", "timeout": 90, "vision": True},
            "qianfan": {"model": "ernie-4.0-8k", "api_key": "", "secret_key": "", "timeout": 90},
            "ollama": {"model": "qwen2.5:7b", "base_url": "http://localhost:11434/v1", "api_key": "", "timeout": 180},
        },
    },
    "image": {
        "enabled": True,
        "max_images": 4,
        "max_bytes": 8388608,
        "allowed_types": ["png", "jpg", "jpeg", "webp", "gif"],
        "detail": "auto",
    },
    "web": {"host": "127.0.0.1", "port": 8848},
    "risk": {
        "pass_criteria": {"hallucination_rate": 0.20, "logic_errors": 1, "repetition": 0.30},
        "repetition_rounds": 3,
        "comparison_models": ["deepseek", "openai", "anthropic", "qianfan"],
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _apply_env(cfg: Dict[str, Any]) -> Dict[str, Any]:
    models = cfg.setdefault("llm", {}).setdefault("models", {})
    for name, env in ENV_KEY_MAP.items():
        key = os.environ.get(env, "").strip()
        if key:
            models.setdefault(name, {})["api_key"] = key
    secret = os.environ.get("QIANFAN_SECRET_KEY", "").strip()
    if secret:
        models.setdefault("qianfan", {})["secret_key"] = secret
    forced = os.environ.get("AI_TUTOR_MODEL", "").strip()
    if forced:
        cfg["llm"]["default"] = forced
    data_dir = os.environ.get("AI_TUTOR_DATA_DIR", "").strip()
    if data_dir:
        cfg["app"]["data_dir"] = data_dir
    return cfg


def load_config(path: Optional[Path | str] = None) -> Dict[str, Any]:
    """读取配置。文件不存在时使用默认配置（不报错，保证开箱即用）。"""
    cfg_path = Path(path) if path else CONFIG_PATH
    file_cfg: Dict[str, Any] = {}
    if cfg_path.exists():
        try:
            file_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            file_cfg = {}
    return _apply_env(_deep_merge(DEFAULT_CONFIG, file_cfg))


def resolve_data_dir(cfg: Optional[Dict[str, Any]] = None) -> Path:
    cfg = cfg or load_config()
    raw = cfg.get("app", {}).get("data_dir", "data")
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_config(cfg: Dict[str, Any], path: Optional[Path | str] = None) -> Path:
    cfg_path = Path(path) if path else CONFIG_PATH
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg_path
