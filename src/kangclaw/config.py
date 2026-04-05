"""kangclaw 配置系统：解析 config.toml，支持 ${ENV_VAR} 替换。"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import tomli_w

KANGCLAW_HOME = Path(os.environ.get("KANGCLAW_HOME", Path.home() / ".kangclaw"))
CONFIG_PATH = KANGCLAW_HOME / "config.toml"


def _expand_env(value: str) -> str:
    """替换字符串中的 ${ENV_VAR} 为环境变量值。"""
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)


def _expand_dict(d: dict) -> dict:
    """递归展开 dict 中所有字符串值的环境变量。"""
    out = {}
    for k, v in d.items():
        if isinstance(v, str):
            out[k] = _expand_env(v)
        elif isinstance(v, dict):
            out[k] = _expand_dict(v)
        elif isinstance(v, list):
            out[k] = [_expand_dict(i) if isinstance(i, dict) else (_expand_env(i) if isinstance(i, str) else i) for i in v]
        else:
            out[k] = v
    return out


@dataclass
class GeneralConfig:
    log_level: str = "info"
    workspace: str = str(KANGCLAW_HOME / "workspace")


@dataclass
class ModelConfig:
    primary_key: str = ""
    id: str = ""
    show_name: str = ""
    provider: str = "openai"
    api_key: str = ""
    base_url: str = ""
    context_window_tokens: int = 0


@dataclass
class AgentConfig:
    max_iterations: int = 50
    show_tool_calls: bool = False
    auto_greeting: bool = True
    model_primary_key: str = ""


@dataclass
class WebConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 12255


@dataclass
class MemoryConfig:
    max_history: int = 20
    session_load_limit: int = 50


@dataclass
class HeartbeatConfig:
    enabled: bool = True
    interval_minutes: int = 30


@dataclass
class ChannelConfig:
    name: str = ""
    enabled: bool = False
    extra: dict = field(default_factory=dict)


@dataclass
class AppConfig:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    models: list[ModelConfig] = field(default_factory=list)
    agent: AgentConfig = field(default_factory=AgentConfig)
    web: WebConfig = field(default_factory=WebConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    heartbeat: HeartbeatConfig = field(default_factory=HeartbeatConfig)
    channels: list[ChannelConfig] = field(default_factory=list)


def _make_dataclass(cls, data: dict):
    """从 dict 创建 dataclass，忽略未定义字段。"""
    import dataclasses
    valid = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in valid})


def load_config(path: Path | None = None) -> AppConfig:
    """加载并解析 config.toml。"""
    path = path or CONFIG_PATH
    if not path.exists():
        return AppConfig()

    with open(path, "rb") as f:
        raw = tomllib.load(f)
    raw = _expand_dict(raw)

    general = _make_dataclass(GeneralConfig, raw.get("general", {}))
    # 展开 workspace 路径中的 ~ 和环境变量
    general.workspace = str(Path(general.workspace).expanduser())

    models = []
    for m_raw in raw.get("model", []):
        models.append(_make_dataclass(ModelConfig, m_raw))

    agent = _make_dataclass(AgentConfig, raw.get("agent", {}))
    web = _make_dataclass(WebConfig, raw.get("web", {}))
    memory = _make_dataclass(MemoryConfig, raw.get("memory", {}))
    heartbeat = _make_dataclass(HeartbeatConfig, raw.get("heartbeat", {}))

    channels = []
    for ch_raw in raw.get("channel", []):
        name = ch_raw.pop("name", "")
        enabled = ch_raw.pop("enabled", False)
        channels.append(ChannelConfig(name=name, enabled=enabled, extra=ch_raw))

    return AppConfig(
        general=general,
        models=models,
        agent=agent,
        web=web,
        memory=memory,
        heartbeat=heartbeat,
        channels=channels,
    )


# ── 模型提供商定义 ──

PROVIDERS = {
    "openai": {"show_name": "OpenAI", "default_base_url": "https://api.openai.com/v1"},
    "anthropic": {"show_name": "Anthropic", "default_base_url": "https://api.anthropic.com"},
    "gemini": {"show_name": "Google Gemini", "default_base_url": "https://generativelanguage.googleapis.com"},
    "deepSeek": {"show_name": "DeepSeek", "default_base_url": "https://api.deepseek.com"},
    "ollama": {"show_name": "Ollama", "default_base_url": "http://localhost:11434/v1"},
    "modelScope": {"show_name": "ModelScope", "default_base_url": "https://api-inference.modelscope.cn/v1"},
    "kimi": {"show_name": "Kimi", "default_base_url": "https://api.moonshot.cn/v1"},
    "miniMax": {"show_name": "MiniMax", "default_base_url": "https://api.minimaxi.com/v1"},
    "xiaomi": {"show_name": "Xiaomi", "default_base_url": "https://api.xiaomimimo.com/v1"},
    "otherOpenai": {"show_name": "OpenAI兼容", "default_base_url": ""},
    "otherAnthropic": {"show_name": "Anthropic兼容", "default_base_url": ""},
}

MODEL_SCHEMA = {
    "providers": PROVIDERS,
    "fields": [
        {"key": "primary_key", "label": "Primary Key", "type": "hidden"},
        {"key": "id", "label": "模型 ID", "type": "text"},
        {"key": "show_name", "label": "模型名称", "type": "text"},
        {"key": "provider", "label": "模型提供商", "type": "select", "options": list(PROVIDERS.keys())},
        {"key": "api_key", "label": "API Key", "type": "password", "secret": True},
        {"key": "base_url", "label": "Base URL", "type": "text"},
        {"key": "context_window_tokens", "label": "上下文长度 (单位：K)", "type": "number", "min": 0, "step": 1},
    ],
}


def get_active_model(config: AppConfig) -> ModelConfig | None:
    """返回当前激活的模型配置。"""
    pk = config.agent.model_primary_key
    for m in config.models:
        if m.primary_key == pk:
            return m
    return config.models[0] if config.models else None


def load_raw_model_configs(path: Path | None = None) -> list[dict]:
    """加载原始 model 配置列表（不展开环境变量），用于 UI 显示。"""
    path = path or CONFIG_PATH
    if not path.exists():
        return []
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return list(raw.get("model", []))


def save_model_configs(models: list[dict], path: Path | None = None) -> None:
    """保存全部模型配置并原子写回 config.toml。"""
    path = path or CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    raw["model"] = models

    tmp_path = path.with_suffix(".toml.tmp")
    with open(tmp_path, "wb") as f:
        tomli_w.dump(raw, f)
    os.replace(tmp_path, path)


def save_agent_model_primary_key(primary_key: str, path: Path | None = None) -> None:
    """更新 agent.model_primary_key 并原子写回 config.toml。"""
    path = path or CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    agent = raw.setdefault("agent", {})
    agent["model_primary_key"] = primary_key

    tmp_path = path.with_suffix(".toml.tmp")
    with open(tmp_path, "wb") as f:
        tomli_w.dump(raw, f)
    os.replace(tmp_path, path)


# ── 渠道 schema 定义 ──

SUPPORTED_CHANNELS: dict[str, dict] = {
    "qq": {
        "label": "QQ",
        "fields": [
            {"key": "app_id", "label": "App ID", "type": "text", "secret": False},
            {"key": "app_secret", "label": "App Secret", "type": "password", "secret": True},
            {"key": "allow_from", "label": "Allow From", "type": "list", "secret": False},
        ],
    },
    "feishu": {
        "label": "飞书",
        "fields": [
            {"key": "app_id", "label": "App ID", "type": "text", "secret": False},
            {"key": "app_secret", "label": "App Secret", "type": "password", "secret": True},
            {"key": "allow_from", "label": "Allow From", "type": "list", "secret": False},
        ],
    },
    "dingtalk": {
        "label": "钉钉",
        "fields": [
            {"key": "client_id", "label": "Client ID", "type": "text", "secret": False},
            {"key": "client_secret", "label": "Client Secret", "type": "password", "secret": True},
            {"key": "allow_from", "label": "Allow From", "type": "list", "secret": False},
        ],
    },
}


def load_raw_channel_configs(path: Path | None = None) -> list[dict]:
    """加载原始 channel 配置（不展开环境变量），用于 UI 显示。"""
    path = path or CONFIG_PATH
    if not path.exists():
        return []
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return list(raw.get("channel", []))


def save_channel_config(name: str, updates: dict, path: Path | None = None) -> None:
    """更新指定渠道的配置并原子写回 config.toml。"""
    path = path or CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    channels = raw.setdefault("channel", [])
    target = None
    for ch in channels:
        if ch.get("name") == name:
            target = ch
            break

    if target is None:
        target = {"name": name, "enabled": False}
        channels.append(target)

    for k, v in updates.items():
        if k == "name":
            continue
        target[k] = v

    # 更新配置修改时间
    general = raw.setdefault("general", {})
    general["config_updated_at"] = datetime.now(timezone.utc).isoformat()

    tmp_path = path.with_suffix(".toml.tmp")
    with open(tmp_path, "wb") as f:
        tomli_w.dump(raw, f)
    os.replace(tmp_path, path)


def save_heartbeat_config(updates: dict, path: Path | None = None) -> None:
    """更新 heartbeat 配置并原子写回 config.toml（不更新 config_updated_at）。"""
    path = path or CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    hb = raw.setdefault("heartbeat", {})
    for k, v in updates.items():
        hb[k] = v

    tmp_path = path.with_suffix(".toml.tmp")
    with open(tmp_path, "wb") as f:
        tomli_w.dump(raw, f)
    os.replace(tmp_path, path)


def save_agent_config(updates: dict, path: Path | None = None) -> None:
    """更新 agent 配置并原子写回 config.toml（不更新 config_updated_at）。"""
    path = path or CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    agent = raw.setdefault("agent", {})
    for k, v in updates.items():
        agent[k] = v

    tmp_path = path.with_suffix(".toml.tmp")
    with open(tmp_path, "wb") as f:
        tomli_w.dump(raw, f)
    os.replace(tmp_path, path)
