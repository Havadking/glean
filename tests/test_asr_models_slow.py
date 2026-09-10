"""需要真实模型和 GPU 的验证。默认不跑：`uv run pytest -m slow`。

依赖两样东西，缺任何一样就跳过而不是失败：
- 装了 funasr（`uv sync --extra funasr`）
- 输出目录里有跑过的音频（也就是至少用过一次这个工具）

之所以不往仓库里塞测试音频：一段够 ASR 出结果的语音至少几 MB，
不值得为了跑一个默认不执行的测试而背在版本库里。
"""

from __future__ import annotations

import glob

import pytest

pytestmark = pytest.mark.slow

from video_summarizer.config import ASRConfig  # noqa: E402


def _require_funasr():
    pytest.importorskip("funasr", reason="没装 funasr，跑 `uv sync --extra funasr`")


def _some_audio() -> str:
    files = sorted(glob.glob("output/*/audio/*.wav"))
    if not files:
        pytest.skip("output 下没有跑过的音频，先用工具处理一个视频")
    return files[0]


def test_funasr_produces_timestamped_segments():
    _require_funasr()
    from video_summarizer.asr.funasr_provider import FunASRProvider

    provider = FunASRProvider(ASRConfig(provider="funasr", model="sensevoice-small"))
    try:
        result = provider.transcribe(_some_audio())
    finally:
        provider.close()

    assert result.segments, "应该识别出内容"
    assert all(s.end > s.start for s in result.segments), "时间轴必须有效"
    assert all(s.text.strip() for s in result.segments), "不该有空文本的分句"
    # 富文本标签要被剥干净，别把 <|zh|> 或者 emoji 带进正文
    joined = " ".join(s.text for s in result.segments)
    assert "<|" not in joined and "🎼" not in joined
    assert result.meta["diarization"] is False


def test_vad_caps_segment_length():
    """SenseVoice 按 30 秒以内短段训练，超长段落识别质量会掉。"""
    _require_funasr()
    from video_summarizer.asr.funasr_provider import MAX_SEGMENT_MS, FunASRProvider

    provider = FunASRProvider(ASRConfig(provider="funasr", model="sensevoice-small"))
    try:
        result = provider.transcribe(_some_audio())
    finally:
        provider.close()

    longest = max(s.duration for s in result.segments)
    assert longest <= MAX_SEGMENT_MS / 1000 + 1.0, f"最长的段有 {longest:.1f} 秒"


def test_diarization_labels_speakers_and_merges_turns():
    _require_funasr()
    from video_summarizer.asr.funasr_provider import FunASRProvider

    provider = FunASRProvider(ASRConfig(provider="funasr"), diarize=True)
    try:
        result = provider.transcribe(_some_audio())
    finally:
        provider.close()

    assert result.meta["diarization"] is True
    assert result.segments and all(s.speaker for s in result.segments), "每段都要有说话人"
    assert all(s.speaker.startswith("Speaker_") for s in result.segments)
    # 合并成"轮"之后，条数应该明显少于原始句数
    assert len(result.segments) < result.meta["sentences"]
    # 相邻两轮不该是同一个人（同一个人连续说的应该已经合并了）
    for prev, nxt in zip(result.segments, result.segments[1:]):
        if prev.speaker == nxt.speaker:
            gap = nxt.start - prev.end
            span = nxt.end - prev.start
            assert gap > 1.5 or span > 45.0, "同一说话人的相邻段没有合并，也不满足断开条件"
