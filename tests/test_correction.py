"""纠专有名词：解析、校验规则逐条、应用、缓存 key、切块。不碰网络。"""

from __future__ import annotations

from video_summarizer import correction
from video_summarizer.cache import summary_key
from video_summarizer.config import SummarizerConfig
from video_summarizer.models import Segment, Transcript
from video_summarizer.summarizer.base import BaseSummarizer

TEXT = "再看语数科技发行价是151块钱。梁文峰两家量化基金，一焕方量化，一个是浙江九章。语数科技很牛。"


def _t(text: str = TEXT, **meta) -> Transcript:
    parts = [p + "。" for p in text.strip("。").split("。")]
    segs = [Segment(float(i), float(i + 1), p) for i, p in enumerate(parts)]
    return Transcript("u", "asr", "zh", float(len(parts)), segs, title="t", video_id="v", meta=meta)


# ---------- 解析 ----------


def test_parse_takes_first_json_array_and_drops_junk():
    out = correction.parse('好的：[{"from": "语数科技", "to": "宇树科技", "why": "机器人公司"}, {"from": "", "to": "x"}, 3]')
    assert out == [{"from": "语数科技", "to": "宇树科技", "why": "机器人公司"}]
    assert correction.parse("没有需要改的。") == []
    assert correction.parse("[]") == []
    assert correction.parse('{"from": "a"}') == []


def test_parse_falls_back_when_prose_contains_brackets():
    out = correction.parse('规则[5]说要输出数组：[{"from": "语数", "to": "宇树"}] 完毕[结束]')
    assert out == [{"from": "语数", "to": "宇树", "why": ""}]


# ---------- 校验 ----------


def _v(items):
    return [(c.src, c.dst) for c in correction.validate(items, TEXT)]


def test_validate_keeps_only_present_terms_and_counts_hits():
    got = correction.validate([{"from": "语数科技", "to": "宇树科技", "why": "w"},
                               {"from": "不存在的", "to": "也不存在", "why": ""}], TEXT)
    assert [(c.src, c.dst, c.hits, c.why) for c in got] == [("语数科技", "宇树科技", 2, "w")]


def test_validate_rejects_numbers_single_chars_and_long_edits():
    assert _v([{"from": "151块", "to": "150块"}]) == []              # V2 数字
    assert _v([{"from": "峰", "to": "锋"}]) == []                     # V4 单字
    assert _v([{"from": "梁文峰", "to": "梁文锋"}]) == [("梁文峰", "梁文锋")]
    assert _v([{"from": "一焕方量化", "to": "幻方量化"}]) == [("一焕方量化", "幻方量化")]   # 差 1 字
    assert _v([{"from": "语数科技", "to": "宇树科技（机器人公司）"}]) == []   # V3 长度差
    assert _v([{"from": "语数科技", "to": "语数科技"}]) == []           # 没改
    assert _v([{"from": "语数科技很牛。", "to": "宇树科技很牛！"}]) == []   # 带句末标点


def test_validate_votes_and_blocks_chains():
    # V5：同一个 from 两个 to，多数胜出
    items = [{"from": "语数科技", "to": "宇树科技"}, {"from": "语数科技", "to": "语速科技"},
             {"from": "语数科技", "to": "宇树科技"}]
    assert _v(items) == [("语数科技", "宇树科技")]
    # V6：A 的 from 出现在 B 的 to 里，A 会把 B 的结果再改一遍，丢 A
    items = [{"from": "九章", "to": "九章资产"}, {"from": "浙江九章", "to": "浙江九章资产"}]
    assert _v(items) == [("浙江九章", "浙江九章资产")]


def test_validate_caps_by_hits():
    items = [{"from": f"词{chr(0x4E00 + i)}", "to": f"字{chr(0x4E00 + i)}"} for i in range(80)]
    text = "".join(it["from"] for it in items)
    assert len(correction.validate(items, text)) == correction.MAX_ITEMS


# ---------- 应用 ----------


def test_apply_longest_first_and_skips_rejected():
    items = [correction.Correction("语数", "宇树"), correction.Correction("语数科技", "宇树科技"),
             correction.Correction("梁文峰", "梁文锋", state="rejected")]
    assert correction.apply_text("语数科技和语数，梁文峰", items) == "宇树科技和宇树，梁文峰"


def test_apply_transcript_keeps_timeline_and_original():
    t = correction.set_items(_t(), [correction.Correction("语数科技", "宇树科技", hits=2)], provider_desc="fake/m")
    assert t.segments[0].text.startswith("再看语数科技")            # 原文没动
    fixed = correction.apply(t)
    assert fixed.segments[0].text.startswith("再看宇树科技") and fixed.meta["corrected"] is True
    assert [(s.start, s.end) for s in fixed.segments] == [(s.start, s.end) for s in t.segments]
    assert len(fixed.segments) == len(t.segments)
    assert correction.apply(_t()) is not None and "corrected" not in correction.apply(_t()).meta


def test_set_items_keeps_rejections_across_reruns():
    t = correction.set_items(_t(), [correction.Correction("语数科技", "宇树科技")], provider_desc="p")
    t = correction.set_state(t, 0, "rejected")
    assert correction.applied_items(t) == []
    t = correction.set_items(t, [correction.Correction("语数科技", "宇树科技"), correction.Correction("梁文峰", "梁文锋")],
                             provider_desc="p")
    assert [(c.src, c.state) for c in correction.items_of(t)] == [("语数科技", "rejected"), ("梁文峰", "applied")]
    t = correction.set_state(t, 0, "applied")
    assert len(correction.applied_items(t)) == 2


def test_fingerprint_changes_summary_key_only_when_table_active():
    base = summary_key(transcript_key="k", provider_desc="p", summary_type="overall", language="zh", extra=None)
    assert summary_key(transcript_key="k", provider_desc="p", summary_type="overall", language="zh", extra=None,
                       corrections=correction.fingerprint(_t())) == base
    t = correction.set_items(_t(), [correction.Correction("语数科技", "宇树科技")], provider_desc="p")
    assert summary_key(transcript_key="k", provider_desc="p", summary_type="overall", language="zh", extra=None,
                       corrections=correction.fingerprint(t)) != base
    assert correction.fingerprint(correction.set_state(t, 0, "rejected")) == ""


# ---------- 调模型 ----------


class Fake(BaseSummarizer):
    name = "fake"

    def __init__(self, cfg, reply):
        super().__init__(cfg)
        self.reply = reply
        self.prompts: list[str] = []

    def _complete(self, system, user):
        assert "校对员" in system
        self.prompts.append(user)
        return self.reply


def test_polish_chunks_long_transcripts_and_merges():
    cfg = SummarizerConfig(max_context_tokens=3000, max_output_tokens=500, chunk_tokens=400)
    fake = Fake(cfg, '[{"from": "语数科技", "to": "宇树科技", "why": "w"}, {"from": "梁文峰", "to": "梁文锋", "why": ""}]')
    long_t = _t("。".join([TEXT.strip("。")] * 12) + "。", uploader="某 UP")
    out = correction.polish(fake, long_t, title="标题", uploader="某 UP",
                            terms=[_term("哈哈哈", "宇树科技")])   # 没命中的词表条目只当已知术语用
    assert len(fake.prompts) > 1
    assert "视频标题：标题" in fake.prompts[0] and "UP 主：某 UP" in fake.prompts[0] and "已知术语" in fake.prompts[0]
    block = out.meta["corrections"]
    assert block["provider"] == "fake/deepseek-chat" and block["prompt_version"] == correction.PROMPT_VERSION
    assert [(i["from"], i["to"], i["hits"]) for i in block["items"]] == [("语数科技", "宇树科技", 24), ("梁文峰", "梁文锋", 12)]


# ---------- 词表 ----------


def _term(src, dst, state="applied", why="表里的"):
    from video_summarizer.cache import TermEntry
    return TermEntry("某 UP", src, dst, why, 3, 1, state)


def test_from_table_only_lists_hits_and_skips_rejected():
    items = correction.from_table([_term("语数科技", "宇树科技"), _term("一焕方", "幻方", state="rejected"),
                                   _term("没出现", "也没出现")], TEXT)
    assert [(c.src, c.dst, c.hits, c.source, c.why) for c in items] == [("语数科技", "宇树科技", 2, "table", "表里的")]


def test_merge_table_wins_over_model_for_same_source():
    table = correction.from_table([_term("语数科技", "宇树科技")], TEXT)
    model = correction.validate([{"from": "语数科技", "to": "御树科技", "why": ""},
                                 {"from": "一焕方", "to": "幻方", "why": "量化"}], TEXT)
    out = correction.merge(table, model)
    assert [(c.src, c.dst, c.source) for c in out] == [("语数科技", "宇树科技", "table"), ("一焕方", "幻方", "model")]


def test_polish_applies_table_first_and_tells_model_the_known_terms():
    cfg = SummarizerConfig(max_context_tokens=3000, max_output_tokens=500, chunk_tokens=4000)
    fake = Fake(cfg, '[{"from": "一焕方", "to": "幻方", "why": "量化"}]')
    out = correction.polish(fake, _t(), title="t", uploader="某 UP",
                            terms=[_term("语数科技", "宇树科技"), _term("没出现", "九坤")])
    assert "已知术语" in fake.prompts[0] and "宇树科技" in fake.prompts[0] and "九坤" in fake.prompts[0]
    block = out.meta["corrections"]
    assert [(i["from"], i["to"], i["source"]) for i in block["items"]] == [("语数科技", "宇树科技", "table"), ("一焕方", "幻方", "model")]
    assert block["table_hits"] == 2


def test_apply_table_without_model_and_leaves_no_empty_block():
    out = correction.apply_table(_t(), [_term("语数科技", "宇树科技")])
    assert out.meta["corrections"]["provider"] == "词表"
    assert correction.apply(out).segments[0].text.startswith("再看宇树科技")
    untouched = correction.apply_table(_t(), [_term("没出现", "x")])
    assert "corrections" not in untouched.meta


def test_source_survives_round_trip():
    c = correction.Correction("a", "b", source="table")
    assert correction.Correction.from_dict(c.to_dict()).source == "table"
    assert correction.Correction.from_dict({"from": "a", "to": "b"}).source == "model"


# ---------- 流水线里的 polish 阶段 ----------


def _pipeline(monkeypatch, tmp_path, transcript, reply, provider_name="openai"):
    from video_summarizer import pipeline
    from video_summarizer.config import Config
    from video_summarizer.ytdlp_base import VideoInfo

    cfg = Config(output_dir=tmp_path / "out", cache_db=tmp_path / "c.sqlite")
    cfg.summarizer.correct_terms = True
    cfg.summarizer.provider = provider_name
    fake = Fake(cfg.summarizer, reply)
    monkeypatch.setattr(pipeline, "_get_transcript", lambda *a, **k: transcript)
    monkeypatch.setattr(pipeline.summarizer_registry, "get_provider", lambda scfg: fake)
    info = VideoInfo(url="u", video_id="v", title="标题", duration_sec=3.0, extractor="x", uploader="某 UP")
    return pipeline, cfg, fake, info


def test_pipeline_polishes_asr_transcript_and_feeds_summary(monkeypatch, tmp_path):
    from video_summarizer.models import SummaryOptions

    class Both(Fake):
        def _complete(self, system, user):
            self.prompts.append(user)
            self._record_usage(100, 10)
            return self.reply if "校对员" in system else "## 总结"

    pipeline, cfg, _fake, info = _pipeline(monkeypatch, tmp_path, _t(), "")
    fake = Both(cfg.summarizer, '[{"from": "语数科技", "to": "宇树科技", "why": "w"}]')
    monkeypatch.setattr(pipeline.summarizer_registry, "get_provider", lambda scfg: fake)
    stages = []
    res = pipeline.run("u", cfg, options=SummaryOptions(), info=info, on_stage=lambda s, d: stages.append(s))

    assert "polish" in stages and stages.index("polish") < stages.index("summarize")
    assert res.correction_usage == (100, 10, 1) and res.correction_provider_desc == "fake/deepseek-chat"
    # 落盘的是原文 + 表；总结看到的是修正后的
    saved = Transcript.load(res.transcript_path)
    assert saved.segments[0].text.startswith("再看语数科技")
    assert saved.meta["corrections"]["items"][0]["to"] == "宇树科技"
    assert "宇树科技" in fake.prompts[-1] and "语数科技" not in fake.prompts[-1]
    # 总结的用量是单独的一笔（纠错那笔已经取走）
    assert fake.take_usage() == (100, 10, 1)


def test_pipeline_skips_subtitle_sources_ollama_and_failures(monkeypatch, tmp_path):
    from video_summarizer.models import SummaryOptions

    sub = _t()
    sub.source_type = "subtitle"
    pipeline, cfg, fake, info = _pipeline(monkeypatch, tmp_path, sub, "[]")
    pipeline.run("u", cfg, options=SummaryOptions(), info=info, skip_summary=True)
    assert fake.prompts == []

    pipeline, cfg, fake, info = _pipeline(monkeypatch, tmp_path, _t(), "[]", provider_name="ollama")
    res = pipeline.run("u", cfg, options=SummaryOptions(), info=info, skip_summary=True)
    assert fake.prompts == [] and "corrections" not in res.transcript.meta

    class Boom(Fake):
        def _complete(self, system, user):
            raise RuntimeError("接口挂了")

    pipeline, cfg, _fake, info = _pipeline(monkeypatch, tmp_path, _t(), "")
    monkeypatch.setattr(pipeline.summarizer_registry, "get_provider", lambda scfg: Boom(cfg.summarizer, ""))
    res = pipeline.run("u", cfg, options=SummaryOptions(), info=info, skip_summary=True)
    assert res.transcript_path.is_file() and "corrections" not in res.transcript.meta and res.correction_usage is None


def test_pipeline_learns_terms_and_reuses_them_for_the_next_video(monkeypatch, tmp_path):
    from video_summarizer.cache import Cache
    from video_summarizer.models import SummaryOptions

    # 第一条视频：模型发现"语数科技→宇树科技"，回填进词表
    pipeline, cfg, fake, info = _pipeline(monkeypatch, tmp_path, _t(), '[{"from": "语数科技", "to": "宇树科技", "why": "w"}]')
    pipeline.run("u", cfg, options=SummaryOptions(), info=info, skip_summary=True)
    terms = Cache(cfg.cache_db).get_terms("某 UP")
    assert [(t.src, t.dst, t.hits, t.videos) for t in terms] == [("语数科技", "宇树科技", 2, 1)]

    # 第二条视频（同一 UP）：模型什么都没说，词表照样套上，且提示词里带了已知术语
    t2 = _t(); t2.video_id = "v2"
    pipeline, cfg, fake, info = _pipeline(monkeypatch, tmp_path, t2, "[]")
    info.video_id = "v2"
    res = pipeline.run("u", cfg, options=SummaryOptions(), info=info, skip_summary=True)
    assert "宇树科技" in fake.prompts[0]
    assert [(i["from"], i["source"]) for i in res.transcript.meta["corrections"]["items"]] == [("语数科技", "table")]
    assert Cache(cfg.cache_db).get_terms("某 UP")[0].videos == 2

    # 纠错关掉也套词表，不调模型
    t3 = _t(); t3.video_id = "v3"
    pipeline, cfg, fake, info = _pipeline(monkeypatch, tmp_path, t3, "[]")
    cfg.summarizer.correct_terms = False
    res = pipeline.run("u", cfg, options=SummaryOptions(), info=info, skip_summary=True)
    assert fake.prompts == [] and res.transcript.meta["corrections"]["provider"] == "词表"

    # 别的 UP 主看不到这张表
    pipeline, cfg, fake, info = _pipeline(monkeypatch, tmp_path, _t(), "[]")
    info.uploader = "路人"
    res = pipeline.run("u", cfg, options=SummaryOptions(), info=info, skip_summary=True)
    assert "已知术语" not in fake.prompts[0] and "corrections" in res.transcript.meta
    assert res.transcript.meta["corrections"]["items"] == []


def test_pipeline_does_not_rerun_when_table_exists(monkeypatch, tmp_path):
    from video_summarizer.models import SummaryOptions

    done = correction.set_items(_t(), [], provider_desc="p")
    pipeline, cfg, fake, info = _pipeline(monkeypatch, tmp_path, done, "[]")
    pipeline.run("u", cfg, options=SummaryOptions(), info=info, skip_summary=True)
    assert fake.prompts == []
