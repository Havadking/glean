"""ASR 装配和兜底（DESIGN.md 6 节 asr.fallback）。

用打桩的 provider，不加载任何模型。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from video_summarizer.asr import get_provider
from video_summarizer.asr.base import ASRResult, BaseASRProvider
from video_summarizer.asr.fallback import FallbackASRProvider
from video_summarizer.config import ASRConfig
from video_summarizer.errors import ASRError, ConfigError
from video_summarizer.models import Segment

DUMMY_AUDIO = Path("不存在.wav")  # 打桩 provider 根本不读它


class StubProvider(BaseASRProvider):
    def __init__(self, name: str, behavior: str = "ok", languages=None, diarize=False) -> None:
        self.name = name
        self.behavior = behavior
        self.supported_languages = languages
        self.supports_diarization = diarize
        self.called = False

    def transcribe(self, audio_path):
        self.called = True
        if self.behavior == "raise":
            raise ASRError("模拟失败")
        if self.behavior == "empty":
            return ASRResult(segments=[], meta={"asr_provider": self.name})
        return ASRResult(
            segments=[Segment(start=0, end=1, text=f"来自 {self.name}")],
            language="zh",
            meta={"asr_provider": self.name},
        )


# ---------- 兜底触发的四条路径 ----------


def test_primary_succeeds_fallback_untouched():
    primary, backup = StubProvider("primary"), StubProvider("backup")
    result = FallbackASRProvider(primary, backup).transcribe(DUMMY_AUDIO)
    assert result.meta["asr_provider"] == "primary"
    assert not backup.called


def test_falls_back_when_primary_raises():
    primary, backup = StubProvider("primary", "raise"), StubProvider("backup")
    result = FallbackASRProvider(primary, backup).transcribe(DUMMY_AUDIO)
    assert result.meta["asr_provider"] == "backup"
    assert "模拟失败" in result.meta["fallback_reason"]


def test_falls_back_when_primary_returns_nothing():
    primary, backup = StubProvider("primary", "empty"), StubProvider("backup")
    result = FallbackASRProvider(primary, backup).transcribe(DUMMY_AUDIO)
    assert result.meta["asr_provider"] == "backup"
    assert "结果为空" in result.meta["fallback_reason"]


def test_skips_primary_entirely_for_unsupported_language():
    """这是设计文档给 fallback 的原始理由：FunASR 不覆盖的小语种交给 whisper。"""
    primary = StubProvider("primary", languages=frozenset({"zh", "en"}))
    backup = StubProvider("backup")
    result = FallbackASRProvider(primary, backup, language="de").transcribe(DUMMY_AUDIO)
    assert result.meta["asr_provider"] == "backup"
    assert not primary.called, "语言不支持时不该白跑一遍主 provider"


def test_supported_language_uses_primary():
    primary = StubProvider("primary", languages=frozenset({"zh", "en"}))
    result = FallbackASRProvider(primary, StubProvider("backup"), language="zh").transcribe(DUMMY_AUDIO)
    assert result.meta["asr_provider"] == "primary"


# ---------- 语言能力声明 ----------


@pytest.mark.parametrize("lang,supported", [
    ("zh", True), ("zh-Hans", True), ("en", True), ("ja", True),
    ("ko", True), ("yue", True), ("de", False), ("fr", False), (None, True),
])
def test_sensevoice_language_coverage(lang, supported):
    from video_summarizer.asr.funasr_provider import FunASRProvider

    assert FunASRProvider(ASRConfig(model="sensevoice-small")).supports_language(lang) is supported


@pytest.mark.parametrize("lang,supported", [
    ("zh", True), ("en", True), ("ja", True), ("ko", False), ("yue", False), ("de", False), (None, True),
])
def test_nano_language_coverage(lang, supported):
    """Nano 官方只说中英日；粤语和韩语走 whisper 兜底。默认模型就是它。"""
    from video_summarizer.asr.funasr_provider import FunASRProvider

    provider = FunASRProvider(ASRConfig())
    assert provider.is_nano and provider.supports_language(lang) is supported


def test_hotwords_are_trimmed_and_capped():
    from video_summarizer.asr.funasr_provider import MAX_HOTWORDS, FunASRProvider

    provider = FunASRProvider(ASRConfig(), hotwords=["", " ", *[f"w{i}" for i in range(MAX_HOTWORDS + 5)]])
    assert len(provider.hotwords) == MAX_HOTWORDS and provider.hotwords[0] == "w0"
    assert get_provider(ASRConfig(fallback=None), hotwords=["鹰派"]).hotwords == ["鹰派"]


def test_whisper_has_no_language_limit():
    from video_summarizer.asr.whisper_provider import WhisperProvider

    w = WhisperProvider(ASRConfig(provider="whisper", model="tiny"))
    assert all(w.supports_language(x) for x in ["de", "fr", "sw", None])


def test_diarization_narrows_funasr_to_chinese():
    """分离走 paraformer-zh，只做中文 —— 别声称还能做别的。"""
    from video_summarizer.asr.funasr_provider import FunASRProvider

    assert FunASRProvider(ASRConfig(), diarize=True).supported_languages == frozenset({"zh"})


# ---------- registry 装配 ----------


def test_default_config_wires_primary_and_fallback():
    provider = get_provider(ASRConfig())
    assert provider.name == "funasr+whisper"
    assert provider.fallback.cfg.model == "large-v3"


def test_no_fallback_returns_bare_provider():
    assert get_provider(ASRConfig(provider="whisper", model="tiny", fallback=None)).name == "whisper"


def test_fallback_same_as_primary_is_not_wrapped():
    p = get_provider(ASRConfig(provider="whisper", model="tiny", fallback="whisper"))
    assert p.name == "whisper"


def test_unknown_provider_is_rejected():
    with pytest.raises(ConfigError, match="未知的 ASR provider"):
        get_provider(ASRConfig(provider="没这个"))


def test_diarization_on_a_provider_without_it_is_rejected():
    with pytest.raises(ConfigError, match="不支持说话人分离"):
        get_provider(ASRConfig(provider="whisper", model="tiny"), diarize=True)


# ---------- diarize 三态解析 ----------


@pytest.mark.parametrize("value,when_needed,when_not", [
    ("auto", True, False),
    (True, True, True),
    (False, False, False),
    ("true", True, True),
    ("false", False, False),
    ("never", False, False),
])
def test_diarize_tristate(value, when_needed, when_not):
    cfg = ASRConfig(diarize=value)
    assert cfg.wants_diarization(needed=True) is when_needed
    assert cfg.wants_diarization(needed=False) is when_not


def test_funasr_strip_tags_fixes_decimal_points():
    from video_summarizer.asr.funasr_provider import _strip_tags, fix_decimal_point

    # ct-punc 把小数点当句号切开；只在两边都是数字时才当小数点
    assert fix_decimal_point("当日收盘价是49。5块钱每股。中签率0。0181。") == "当日收盘价是49.5块钱每股。中签率0.0181。"
    assert fix_decimal_point("第一句。第二句。") == "第一句。第二句。"
    text, lang = _strip_tags("<|zh|><|NEUTRAL|><|Speech|><|withitn|>发行价是151。5块。")
    assert (text, lang) == ("发行价是151.5块。", "zh")
