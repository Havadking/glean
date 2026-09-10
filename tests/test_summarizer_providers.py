"""三个总结 provider 的协议层。

开发机上没有 Anthropic key、也没装 Ollama，所以：
Ollama 对着一个假服务器发真实 HTTP，Claude 用假 message 对象覆盖响应解析。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from video_summarizer.config import SummarizerConfig
from video_summarizer.errors import ConfigError, SummarizerError
from video_summarizer.summarizer import get_provider


# ---------- registry ----------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("openai", "OpenAICompatibleSummarizer"),
        ("claude", "ClaudeSummarizer"),
        ("anthropic", "ClaudeSummarizer"),
        ("ollama", "OllamaSummarizer"),
        ("deepseek", "OpenAICompatibleSummarizer"),
        ("KIMI", "OpenAICompatibleSummarizer"),
    ],
)
def test_registry_dispatch(name, expected):
    provider = get_provider(SummarizerConfig(provider=name, model="m"))
    assert type(provider).__name__ == expected


def test_unknown_provider_lists_the_options():
    with pytest.raises(ConfigError, match="可选"):
        get_provider(SummarizerConfig(provider="gemini"))


def test_describe_is_traceable():
    """describe() 会写进 summary.md 的头部，要能看出用了什么。"""
    assert get_provider(SummarizerConfig(
        provider="claude", model="claude-opus-5")).describe() == "anthropic/claude-opus-5"
    assert get_provider(SummarizerConfig(
        provider="ollama", model="qwen3:8b")).describe() == "ollama/qwen3:8b"
    assert "deepseek-chat" in get_provider(SummarizerConfig(
        provider="openai", model="deepseek-chat",
        base_url="https://api.deepseek.com/v1")).describe()


# ---------- Claude 响应解析 ----------


class _Block:
    def __init__(self, type_: str, text: str = "") -> None:
        self.type, self.text = type_, text


class _Usage:
    input_tokens, output_tokens = 100, 50


class _Message:
    def __init__(self, content, stop_reason="end_turn", stop_details=None) -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.stop_details = stop_details
        self.usage = _Usage()


class _Refusal:
    category, explanation = "cyber", "declined"


@pytest.fixture
def claude():
    return get_provider(SummarizerConfig(
        provider="claude", model="claude-opus-5", api_key_env="ANTHROPIC_API_KEY",
    ))


def test_claude_drops_thinking_blocks(claude):
    msg = _Message([_Block("thinking", "内心戏"), _Block("text", "正文一"), _Block("text", "正文二")])
    assert claude._extract_text(msg) == "正文一\n正文二"


def test_claude_refusal_surfaces_the_category(claude):
    msg = _Message([], stop_reason="refusal", stop_details=_Refusal())
    with pytest.raises(SummarizerError, match="cyber"):
        claude._extract_text(msg)


def test_claude_truncated_output_still_returned(claude):
    """截断了也把已有正文给出去，总比什么都没有强。"""
    msg = _Message([_Block("text", "被截断的正文")], stop_reason="max_tokens")
    assert claude._extract_text(msg) == "被截断的正文"


def test_claude_thinking_ate_the_budget(claude):
    """只有思考没正文 —— 报错要指向 max_output_tokens，否则很难查。"""
    msg = _Message([_Block("thinking", "想了很久")], stop_reason="max_tokens")
    with pytest.raises(SummarizerError, match="max_output_tokens"):
        claude._extract_text(msg)


def test_claude_missing_key_is_a_config_error(monkeypatch, claude):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        claude._complete("sys", "user")


def test_claude_refuses_to_send_another_vendors_key(monkeypatch):
    """provider 改成 claude 但忘了改 api_key_env，会把 DeepSeek 的 key 发给 Anthropic。

    这不是假想：写这套测试时 fixture 就漏了 api_key_env，真发出去过一次。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-abcdef0123456789")
    provider = get_provider(SummarizerConfig(
        provider="claude", model="claude-opus-5", api_key_env="DEEPSEEK_API_KEY",
    ))
    with pytest.raises(ConfigError, match="不像 Anthropic 的密钥"):
        provider._complete("sys", "user")


def test_claude_warns_when_base_url_points_elsewhere(monkeypatch, caplog):
    """自建中转的 key 格式不一定一样，配了 base_url 就放行，但要说清楚发去哪了。"""
    monkeypatch.setenv("PROXY_KEY", "whatever-format")
    provider = get_provider(SummarizerConfig(
        provider="claude", model="claude-opus-5",
        api_key_env="PROXY_KEY", base_url="http://127.0.0.1:59998",
    ))
    with caplog.at_level("WARNING"):
        # 走到真发请求那步才失败（连不上），说明密钥形状检查已放行
        with pytest.raises(SummarizerError):
            provider._complete("sys", "user")
    assert any("59998" in r.getMessage() for r in caplog.records)


def test_summarizer_base_url_default_is_neutral():
    """dataclass 默认值不能写死某一家的地址，否则换 provider 会发错地方。"""
    assert SummarizerConfig().base_url is None


def test_claude_never_sends_temperature():
    """当前一代 Claude 移除了采样参数，传了直接 400。"""
    import inspect

    from video_summarizer.summarizer import claude_provider

    src = inspect.getsource(claude_provider.ClaudeSummarizer._complete)
    assert '"temperature"' not in src and "temperature=" not in src


# ---------- Ollama：对着假服务器跑真实 HTTP ----------


class _FakeOllama(BaseHTTPRequestHandler):
    mode = "ok"
    received: dict = {}

    def log_message(self, *args):  # 别把测试输出刷满
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _FakeOllama.received = body

        if _FakeOllama.mode == "missing_model":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":"model not found"}')
            return

        payloads = {
            "ok": {"message": {"content": "本地模型的总结"},
                   "prompt_eval_count": 500, "eval_count": 120, "done_reason": "stop"},
            "ctx_full": {"message": {"content": "满了"},
                         "prompt_eval_count": 32000, "eval_count": 10, "done_reason": "stop"},
            "empty": {"message": {"content": ""}, "done_reason": "load"},
        }
        raw = json.dumps(payloads[_FakeOllama.mode]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def fake_ollama():
    server = HTTPServer(("127.0.0.1", 0), _FakeOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _FakeOllama.mode = "ok"
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _ollama(base_url: str, **kw) -> object:
    return get_provider(SummarizerConfig(
        provider="ollama", model="qwen3:8b", base_url=base_url,
        max_context_tokens=32000, max_output_tokens=2000, temperature=0.3, **kw,
    ))


def test_ollama_returns_content(fake_ollama):
    assert _ollama(fake_ollama)._complete("你是助手", "总结这个") == "本地模型的总结"


def test_ollama_sends_num_ctx(fake_ollama):
    """最重要的一条：不传 num_ctx 的话 Ollama 默认 4096，超出部分被静默丢弃。"""
    _ollama(fake_ollama)._complete("s", "u")
    assert _FakeOllama.received["options"]["num_ctx"] == 32000
    assert _FakeOllama.received["options"]["num_predict"] == 2000
    assert _FakeOllama.received["stream"] is False
    assert [m["role"] for m in _FakeOllama.received["messages"]] == ["system", "user"]


def test_ollama_warns_when_context_is_full(fake_ollama, caplog):
    _FakeOllama.mode = "ctx_full"
    with caplog.at_level("WARNING"):
        _ollama(fake_ollama)._complete("s", "u")
    assert any("丢" in r.message or "num_ctx" in r.message for r in caplog.records)


def test_ollama_missing_model_tells_you_to_pull(fake_ollama):
    _FakeOllama.mode = "missing_model"
    with pytest.raises(ConfigError, match="ollama pull"):
        _ollama(fake_ollama)._complete("s", "u")


def test_ollama_empty_response_is_an_error(fake_ollama):
    _FakeOllama.mode = "empty"
    with pytest.raises(SummarizerError):
        _ollama(fake_ollama)._complete("s", "u")


def test_ollama_tolerates_v1_suffix_in_base_url(fake_ollama):
    """照抄 OpenAI 兼容写法的人会在 base_url 末尾带 /v1，原生接口不需要。"""
    provider = _ollama(fake_ollama + "/v1")
    assert provider._base_url == fake_ollama
    assert provider._complete("s", "u") == "本地模型的总结"


def test_ollama_offline_points_at_ollama_serve():
    with pytest.raises(SummarizerError, match="ollama serve"):
        _ollama("http://127.0.0.1:59999")._complete("s", "u")
