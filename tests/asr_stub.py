"""给 test_asr_worker 用的打桩 provider，子进程按 "asr_stub:make_provider" 导入。"""

from __future__ import annotations

import logging
import sys

from video_summarizer.asr.base import ASRResult, BaseASRProvider
from video_summarizer.errors import ASRError
from video_summarizer.models import Segment

log = logging.getLogger("video_summarizer.asr.stub")


class StubProvider(BaseASRProvider):
    name = "stub"

    def __init__(self, cfg, diarize=False, hotwords=None) -> None:
        self.cfg, self.diarize, self.hotwords = cfg, diarize, hotwords or []
        self.closed = False

    def transcribe(self, audio_path):
        # 故意往 stdout 上 print：真实的 funasr 就这么干，协议流不能被它污染
        print("这行是模型库打印的噪音，不该混进协议")
        # modelscope 的 tqdm 进度条就是这么直接写 stderr 再 flush 的，父进程的 stderr 死了这里会炸
        sys.stderr.write("Downloading: 100%|██████████| 1/1\n")
        sys.stderr.flush()
        log.info("转写进度  50%%（00:01:00 / 00:02:00）")
        if self.cfg.model == "boom":
            raise ASRError("模拟失败")
        if self.cfg.model == "crash":
            raise RuntimeError("模型内部炸了")
        return ASRResult(
            segments=[Segment(start=0.0, end=1.5, text=f"来自 {audio_path.name}", speaker="Speaker_1")],
            language="zh",
            meta={"asr_provider": self.name, "hotwords": len(self.hotwords), "diarize": self.diarize},
        )

    def close(self) -> None:
        self.closed = True


def make_provider(cfg, diarize=False, hotwords=None):
    return StubProvider(cfg, diarize=diarize, hotwords=hotwords)
