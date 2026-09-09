"""配置加载：config.yaml（可提交）+ .env（密钥，不提交）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .errors import ConfigError

DEFAULT_CONFIG_NAME = "config.yaml"


@dataclass
class SubtitleConfig:
    accept_auto_captions: bool = False
    preferred_languages: list[str] = field(
        default_factory=lambda: ["zh-Hans", "zh-CN", "zh", "en", "ja"]
    )


@dataclass
class DownloadConfig:
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    cookies_from_browser: str | None = None


@dataclass
class ASRConfig:
    provider: str = "funasr"
    model: str = "sensevoice-small"
    device: str = "auto"
    compute_type: str = "auto"
    language: str | None = None
    vad_filter: bool = True
    beam_size: int = 5
    # 主 provider 不支持该语言、或跑失败时的兜底
    fallback: str | None = "whisper"
    fallback_model: str | None = None


@dataclass
class SummarizerConfig:
    provider: str = "openai"
    model: str = "deepseek-chat"
    base_url: str | None = "https://api.deepseek.com/v1"
    api_key_env: str = "DEEPSEEK_API_KEY"
    max_context_tokens: int = 120000
    max_output_tokens: int = 8000
    temperature: float = 0.3
    chunk_strategy: str = "auto"
    chunk_tokens: int = 20000
    summary_type: str = "overall"

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) or None


@dataclass
class Config:
    subtitle: SubtitleConfig = field(default_factory=SubtitleConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    asr: ASRConfig = field(default_factory=ASRConfig)
    summarizer: SummarizerConfig = field(default_factory=SummarizerConfig)
    output_dir: Path = Path("./output")
    cache_db: Path = Path("./cache.sqlite")
    source_path: Path | None = None


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"配置项 `{key}` 应该是一个映射，实际是 {type(value).__name__}")
    return value


def _build(cls, data: dict[str, Any], section: str):
    known = {f for f in cls.__dataclass_fields__}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(
            f"配置项 `{section}` 里有无法识别的字段: {', '.join(sorted(unknown))}"
        )
    return cls(**data)


def find_config(explicit: Path | None = None) -> Path | None:
    """显式路径 > 当前目录 > 项目根目录。"""
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"配置文件不存在: {path}")
        return path
    for candidate in (
        Path.cwd() / DEFAULT_CONFIG_NAME,
        Path(__file__).resolve().parents[2] / DEFAULT_CONFIG_NAME,
    ):
        if candidate.is_file():
            return candidate
    return None


def load_config(path: Path | None = None) -> Config:
    """读 config.yaml + 加载 .env。配置文件缺失时用全套默认值。"""
    config_path = find_config(path)

    # .env 就近找：配置文件同级目录优先，其次当前目录往上找
    if config_path is not None:
        env_file = config_path.parent / ".env"
        if env_file.is_file():
            load_dotenv(env_file, override=False)
    load_dotenv(override=False)

    if config_path is None:
        return Config()

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件格式不对，顶层应该是映射: {config_path}")

    base_dir = config_path.parent
    output_dir = Path(raw.get("output_dir") or "./output")
    cache_db = Path(raw.get("cache_db") or "./cache.sqlite")

    return Config(
        subtitle=_build(SubtitleConfig, _section(raw, "subtitle"), "subtitle"),
        download=_build(DownloadConfig, _section(raw, "download"), "download"),
        asr=_build(ASRConfig, _section(raw, "asr"), "asr"),
        summarizer=_build(SummarizerConfig, _section(raw, "summarizer"), "summarizer"),
        output_dir=output_dir if output_dir.is_absolute() else base_dir / output_dir,
        cache_db=cache_db if cache_db.is_absolute() else base_dir / cache_db,
        source_path=config_path,
    )
