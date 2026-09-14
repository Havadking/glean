"""配置加载：config.yaml（可提交）+ .env（密钥，不提交）。"""

from __future__ import annotations

import os
import re
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
    # Netscape 格式的 cookie 文件（浏览器扩展导出）。B 站带登录态时限流宽松得多。
    # Chrome 127 之后 yt-dlp 读不了 Windows 上的 Chrome cookie，用这个更省事
    cookies_file: str | None = None
    # 批量处理时两个视频之间至少隔这么久再去碰站点。B 站 412 是按 IP 的频率风控
    batch_delay_sec: float = 5.0


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
    # 说话人分离：auto = 只在需要分角色总结时开（它比不分离慢一半，且只有中文模型）
    diarize: str | bool = "auto"
    diarize_model: str = "paraformer-zh"

    def wants_diarization(self, needed: bool = False) -> bool:
        """diarize 在 yaml 里可能写成布尔量，也可能是字符串 auto。"""
        if isinstance(self.diarize, bool):
            return self.diarize
        value = (self.diarize or "").strip().lower()
        if value in {"true", "yes", "on", "always"}:
            return True
        if value in {"false", "no", "off", "never"}:
            return False
        return needed  # auto


@dataclass
class SummarizerConfig:
    provider: str = "openai"
    model: str = "deepseek-chat"
    # 默认留空，具体地址由 config.yaml 给。这里要是写死某一家的地址，
    # 换 provider 时忘了改就会把请求（连同密钥）发到错误的厂商去。
    base_url: str | None = None
    api_key_env: str = "DEEPSEEK_API_KEY"
    max_context_tokens: int = 120000
    max_output_tokens: int = 8000
    # 只对 openai / ollama 生效。Claude 当前一代移除了采样参数，传了会 400，
    # claude provider 刻意不发这个字段。
    temperature: float = 0.3
    # 只对 claude 生效：控制思考深度和整体花费。low | medium | high | xhigh | max
    effort: str | None = None
    chunk_strategy: str = "auto"
    chunk_tokens: int = 20000
    summary_type: str = "overall"
    # 每百万 token 的价格，用来在界面上把 token 数换算成钱。留空就只显示 token 数。
    price_input_per_m: float | None = None
    price_output_per_m: float | None = None
    currency: str = "¥"
    # 送给模型之前先去口水话（呃、嗯、就是就是）。省 token，但改变原文，默认关
    clean_transcript: bool = False

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


def update_pricing(
    config_path: Path | None,
    price_input_per_m: float | None,
    price_output_per_m: float | None,
    currency: str = "¥",
) -> None:
    """在线更新 config.yaml 中的单价和货币符号，保持文件注释和排版不变。"""
    if config_path is None or not config_path.is_file():
        return

    text = config_path.read_text(encoding="utf-8")

    in_val = "null" if price_input_per_m is None else str(price_input_per_m)
    if re.search(r"(?m)^(\s*price_input_per_m\s*:).*$", text):
        text = re.sub(r"(?m)^(\s*price_input_per_m\s*:).*$", rf"\g<1> {in_val}", text)
    else:
        text = re.sub(r"(?m)^(summarizer\s*:.*)$", rf"\1\n  price_input_per_m: {in_val}", text)

    out_val = "null" if price_output_per_m is None else str(price_output_per_m)
    if re.search(r"(?m)^(\s*price_output_per_m\s*:).*$", text):
        text = re.sub(r"(?m)^(\s*price_output_per_m\s*:).*$", rf"\g<1> {out_val}", text)
    else:
        text = re.sub(r"(?m)^(summarizer\s*:.*)$", rf"\1\n  price_output_per_m: {out_val}", text)

    cur_val = f'"{currency}"'
    if re.search(r"(?m)^(\s*currency\s*:).*$", text):
        text = re.sub(r"(?m)^(\s*currency\s*:).*$", rf"\g<1> {cur_val}", text)
    else:
        text = re.sub(r"(?m)^(summarizer\s*:.*)$", rf"\1\n  currency: {cur_val}", text)

    config_path.write_text(text, encoding="utf-8")
