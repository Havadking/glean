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
    # 抖音详情接口被风控拦下（yt-dlp 报 "Fresh cookies are needed" / 403）时，
    # 无头拉起本机浏览器替它请求，拿到的 cookie 也会写回 cookies_file。
    # auto = 依次试 chrome / msedge / playwright 自带 chromium；也可以指定其中一个；off = 关掉
    douyin_browser: str = "auto"
    # 批量处理时两个视频之间至少隔这么久再去碰站点。B 站 412 是按 IP 的频率风控
    batch_delay_sec: float = 5.0


@dataclass
class ASRConfig:
    provider: str = "funasr"
    model: str = "fun-asr-nano"
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
    name: str | None = None  # 用户友好显示名称，如 "DeepSeek v4.1" 或 "gemini 3.8 flash"
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
    # 处理完一条视频后顺手让模型打 3–5 个主题标签（用总结做输入，几乎不花钱）
    auto_tags: bool = True
    # ASR 转写完让模型出一张专有名词替换表（"语数科技"→"宇树科技"），叠加在原文上显示。
    # 要把全文喂一遍模型，1 小时视频约 ¥0.03、十几秒，所以默认关；界面上每次处理可单独勾
    correct_terms: bool = False

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


def _update_section_key(text: str, section: str, key: str, val_str: str) -> str:
    """在指定的顶层 section 下替换或追加 key: val。保留行尾注释与排版。"""
    pattern = rf"(?m)^({section}\s*:.*?\n)(?=(?:^[a-zA-Z_0-9]+:|\Z))"
    m = re.search(pattern, text, re.DOTALL)
    if not m:
        return text + f"\n{section}:\n  {key}: {val_str}\n"

    sec_block = m.group(1)
    key_pattern = rf"(?m)^(\s*{re.escape(key)}\s*:)[ \t]*([^#\r\n]*?)([ \t]*(#.*)?)$"
    m_key = re.search(key_pattern, sec_block)
    if m_key:
        prefix = m_key.group(1)
        comment = m_key.group(4)
        if comment:
            repl = f"{prefix} {val_str}   {comment}"
        else:
            repl = f"{prefix} {val_str}"
        new_sec_block = sec_block[:m_key.start()] + repl + sec_block[m_key.end():]
    else:
        new_sec_block = re.sub(
            rf"(?m)^({re.escape(section)}\s*:.*)$",
            rf"\1\n  {key}: {val_str}",
            sec_block,
            count=1,
        )
    return text[:m.start(1)] + new_sec_block + text[m.end(1):]


_UNSET: Any = object()


def update_summarizer_config(
    config_path: Path | None,
    name: str | None = _UNSET,
    provider: str | None = _UNSET,
    model: str | None = _UNSET,
    base_url: str | None = _UNSET,
    api_key_env: str | None = _UNSET,
    temperature: float | None = _UNSET,
    max_context_tokens: int | None = _UNSET,
    max_output_tokens: int | None = _UNSET,
    price_input_per_m: float | None = _UNSET,
    price_output_per_m: float | None = _UNSET,
    currency: str | None = _UNSET,
) -> None:
    """在线更新 config.yaml 中的总结大模型配置，保持文件注释和排版不变。"""
    if config_path is None or not config_path.is_file():
        return

    text = config_path.read_text(encoding="utf-8")

    if name is not _UNSET:
        n_val = "null" if not name or not name.strip() else f'"{name.strip()}"'
        text = _update_section_key(text, "summarizer", "name", n_val)
    if provider is not _UNSET and provider is not None:
        text = _update_section_key(text, "summarizer", "provider", provider.strip())
    if model is not _UNSET and model is not None:
        text = _update_section_key(text, "summarizer", "model", model.strip())
    if base_url is not _UNSET:
        b_val = "null" if not base_url or not base_url.strip() else f'"{base_url.strip()}"'
        text = _update_section_key(text, "summarizer", "base_url", b_val)
    if api_key_env is not _UNSET and api_key_env is not None:
        text = _update_section_key(text, "summarizer", "api_key_env", api_key_env.strip())
    if temperature is not _UNSET and temperature is not None:
        text = _update_section_key(text, "summarizer", "temperature", str(temperature))
    if max_context_tokens is not _UNSET and max_context_tokens is not None:
        text = _update_section_key(text, "summarizer", "max_context_tokens", str(max_context_tokens))
    if max_output_tokens is not _UNSET and max_output_tokens is not None:
        text = _update_section_key(text, "summarizer", "max_output_tokens", str(max_output_tokens))
    if price_input_per_m is not _UNSET:
        in_val = "null" if price_input_per_m is None else str(price_input_per_m)
        text = _update_section_key(text, "summarizer", "price_input_per_m", in_val)
    if price_output_per_m is not _UNSET:
        out_val = "null" if price_output_per_m is None else str(price_output_per_m)
        text = _update_section_key(text, "summarizer", "price_output_per_m", out_val)
    if currency is not _UNSET and currency is not None:
        text = _update_section_key(text, "summarizer", "currency", f'"{currency.strip()}"')

    config_path.write_text(text, encoding="utf-8")


def update_asr_config(
    config_path: Path | None,
    provider: str | None = _UNSET,
    model: str | None = _UNSET,
    device: str | None = _UNSET,
    diarize: str | bool | None = _UNSET,
) -> None:
    """在线更新 config.yaml 中的 ASR 识别模型配置，保持文件注释和排版不变。"""
    if config_path is None or not config_path.is_file():
        return

    text = config_path.read_text(encoding="utf-8")

    if provider is not _UNSET and provider is not None:
        text = _update_section_key(text, "asr", "provider", provider.strip())
    if model is not _UNSET and model is not None:
        text = _update_section_key(text, "asr", "model", model.strip())
    if device is not _UNSET and device is not None:
        text = _update_section_key(text, "asr", "device", device.strip())
    if diarize is not _UNSET and diarize is not None:
        d_val = str(diarize).lower() if isinstance(diarize, bool) else diarize.strip()
        text = _update_section_key(text, "asr", "diarize", d_val)

    config_path.write_text(text, encoding="utf-8")


def update_env_key(env_path: Path, key_name: str, key_val: str) -> None:
    """在 .env 文件中更新或添加环境变量，并在当前运行进程立即生效。"""
    key_name = key_name.strip()
    key_val = key_val.strip()
    os.environ[key_name] = key_val

    lines: list[str] = []
    if env_path.is_file():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    pattern = re.compile(rf"^\s*{re.escape(key_name)}\s*=.*$")
    found = False
    new_lines: list[str] = []
    for line in lines:
        if pattern.match(line):
            new_lines.append(f"{key_name}={key_val}")
            found = True
        else:
            new_lines.append(line)

    if not found:
        new_lines.append(f"{key_name}={key_val}")

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def mask_api_key(key: str | None) -> str:
    """对 API 密钥进行脱敏显示（如 sk-1234****abcd）。"""
    if not key:
        return ""
    k = key.strip()
    if len(k) <= 8:
        return "******"
    return f"{k[:4]}****{k[-4:]}"


def update_pricing(
    config_path: Path | None,
    price_input_per_m: float | None,
    price_output_per_m: float | None,
    currency: str = "¥",
) -> None:
    """在线更新 config.yaml 中的单价和货币符号，保持文件注释和排版不变。"""
    update_summarizer_config(
        config_path,
        price_input_per_m=price_input_per_m,
        price_output_per_m=price_output_per_m,
        currency=currency,
    )
