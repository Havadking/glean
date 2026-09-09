"""统一的异常类型，方便 CLI 层把预期内的失败渲染成人话而不是堆栈。"""


class VideoSummarizerError(Exception):
    """本项目所有预期内错误的基类。"""


class SubtitleNotFoundError(VideoSummarizerError):
    """没有可用的人工字幕（弹幕/自动字幕不算）。"""


class DownloadError(VideoSummarizerError):
    """yt-dlp 抓取失败。"""


class ASRError(VideoSummarizerError):
    """语音识别失败。"""


class SummarizerError(VideoSummarizerError):
    """LLM 总结失败。"""


class ConfigError(VideoSummarizerError):
    """配置文件或密钥有问题。"""
