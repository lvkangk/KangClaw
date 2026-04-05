"""kangclaw 测试套件"""

import asyncio
import json
import os
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
import pytest_asyncio

# ─────────────────────────────────────────────
# config.py
# ─────────────────────────────────────────────

from kangclaw.config import (
    _expand_env,
    _expand_dict,
    _make_dataclass,
    load_config,
    AppConfig,
    GeneralConfig,
    ModelConfig,
    WebConfig,
    MemoryConfig,
    HeartbeatConfig,
    ChannelConfig,
    get_active_model,
)


class TestExpandEnv:
    def test_expand_existing_var(self):
        with patch.dict(os.environ, {"MY_VAR": "hello"}):
            assert _expand_env("${MY_VAR}") == "hello"

    def test_expand_missing_var(self):
        result = _expand_env("${NONEXISTENT_VAR_12345}")
        assert result == ""

    def test_expand_no_vars(self):
        assert _expand_env("plain text") == "plain text"

    def test_expand_multiple_vars(self):
        with patch.dict(os.environ, {"A": "1", "B": "2"}):
            assert _expand_env("${A}-${B}") == "1-2"

    def test_expand_partial(self):
        with patch.dict(os.environ, {"A": "val"}):
            assert _expand_env("pre_${A}_post") == "pre_val_post"


class TestExpandDict:
    def test_expand_nested(self):
        with patch.dict(os.environ, {"KEY": "secret"}):
            result = _expand_dict({"a": {"b": "${KEY}"}})
            assert result["a"]["b"] == "secret"

    def test_expand_list_in_dict(self):
        with patch.dict(os.environ, {"X": "yes"}):
            result = _expand_dict({"items": ["${X}", "plain", 42]})
            assert result["items"] == ["yes", "plain", 42]

    def test_non_string_passthrough(self):
        result = _expand_dict({"num": 42, "flag": True, "f": 1.5})
        assert result == {"num": 42, "flag": True, "f": 1.5}


class TestLoadConfig:
    def test_load_default_when_no_file(self, tmp_path):
        cfg = load_config(tmp_path / "nonexistent.toml")
        assert isinstance(cfg, AppConfig)
        assert cfg.models == []
        assert cfg.agent.model_primary_key == ""
        assert cfg.web.port == 12255

    def test_load_from_toml(self, tmp_path):
        toml_path = tmp_path / "config.toml"
        toml_path.write_text("""
[general]
log_level = "debug"

[[model]]
primary_key = "m1"
id = "claude-3"
show_name = "Claude 3"
provider = "anthropic"
temperature = 0.5

[agent]
model_primary_key = "m1"

[web]
port = 9999

[memory]
max_history = 50
""")
        cfg = load_config(toml_path)
        assert cfg.general.log_level == "debug"
        assert len(cfg.models) == 1
        assert cfg.models[0].provider == "anthropic"
        assert cfg.models[0].id == "claude-3"
        assert cfg.models[0].temperature == 0.5
        assert cfg.agent.model_primary_key == "m1"
        active = get_active_model(cfg)
        assert active is not None
        assert active.primary_key == "m1"
        assert cfg.web.port == 9999
        assert cfg.memory.max_history == 50

    def test_load_with_env_expansion(self, tmp_path):
        toml_path = tmp_path / "config.toml"
        toml_path.write_text("""
[[model]]
primary_key = "m1"
id = "gpt-4o"
api_key = "${TEST_API_KEY_KANGCLAW}"
""")
        with patch.dict(os.environ, {"TEST_API_KEY_KANGCLAW": "sk-test123"}):
            cfg = load_config(toml_path)
            assert cfg.models[0].api_key == "sk-test123"

    def test_load_with_channels(self, tmp_path):
        toml_path = tmp_path / "config.toml"
        toml_path.write_text("""
[[channel]]
name = "qq"
enabled = true
app_id = "12345"
""")
        cfg = load_config(toml_path)
        assert len(cfg.channels) == 1
        assert cfg.channels[0].name == "qq"
        assert cfg.channels[0].enabled is True
        assert cfg.channels[0].extra.get("app_id") == "12345"

    def test_unknown_fields_ignored(self, tmp_path):
        toml_path = tmp_path / "config.toml"
        toml_path.write_text("""
[[model]]
primary_key = "m1"
provider = "openai"
unknown_field = "should be ignored"
""")
        cfg = load_config(toml_path)
        assert cfg.models[0].provider == "openai"

    def test_load_multiple_models(self, tmp_path):
        toml_path = tmp_path / "config.toml"
        toml_path.write_text("""
[[model]]
primary_key = "m1"
id = "gpt-4o"
provider = "openai"

[[model]]
primary_key = "m2"
id = "claude-3"
provider = "anthropic"

[agent]
model_primary_key = "m2"
""")
        cfg = load_config(toml_path)
        assert len(cfg.models) == 2
        assert cfg.models[0].primary_key == "m1"
        assert cfg.models[1].primary_key == "m2"
        active = get_active_model(cfg)
        assert active.primary_key == "m2"
        assert active.provider == "anthropic"

    def test_get_active_model_fallback(self, tmp_path):
        toml_path = tmp_path / "config.toml"
        toml_path.write_text("""
[[model]]
primary_key = "m1"
id = "gpt-4o"
provider = "openai"

[agent]
model_primary_key = "nonexistent"
""")
        cfg = load_config(toml_path)
        active = get_active_model(cfg)
        assert active.primary_key == "m1"  # falls back to first

    def test_get_active_model_empty(self):
        cfg = AppConfig()
        assert get_active_model(cfg) is None


# ─────────────────────────────────────────────
# memory.py
# ─────────────────────────────────────────────

from kangclaw.gateway.memory import MemoryManager, Message, SessionMeta, _gen_tool_call_id


class TestGenToolCallId:
    def test_format(self):
        tid = _gen_tool_call_id()
        assert tid.startswith("call_")
        assert len(tid) == 17  # "call_" + 12 hex chars

    def test_uniqueness(self):
        ids = {_gen_tool_call_id() for _ in range(100)}
        assert len(ids) == 100


class TestMessage:
    def test_to_dict_basic(self):
        msg = Message(role="user", content="hello")
        d = msg.to_dict()
        assert d["role"] == "user"
        assert d["content"] == "hello"
        assert "name" not in d
        assert "tool_call_id" not in d

    def test_to_dict_tool(self):
        msg = Message(role="tool", content="result", name="read_file", tool_call_id="call_abc")
        d = msg.to_dict()
        assert d["name"] == "read_file"
        assert d["tool_call_id"] == "call_abc"

    def test_to_langchain_user(self):
        msg = Message(role="user", content="hi")
        lc = msg.to_langchain()
        assert lc.__class__.__name__ == "HumanMessage"
        assert lc.content == "hi"

    def test_to_langchain_assistant(self):
        msg = Message(role="assistant", content="reply")
        lc = msg.to_langchain()
        assert lc.__class__.__name__ == "AIMessage"

    def test_to_langchain_tool(self):
        msg = Message(role="tool", content="ok", name="test", tool_call_id="call_x")
        lc = msg.to_langchain()
        assert lc.__class__.__name__ == "ToolMessage"
        assert lc.tool_call_id == "call_x"

    def test_to_langchain_system(self):
        msg = Message(role="system", content="you are helpful")
        lc = msg.to_langchain()
        assert lc.__class__.__name__ == "SystemMessage"


class TestMemoryManager:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.workspace = Path(self.tmp)
        self.mm = MemoryManager(self.workspace)

    def test_session_creation(self):
        s = self.mm.get_or_create_session("test-1", channel="web", type_="web")
        assert isinstance(s, SessionMeta)
        assert s.session_id == "test-1"
        assert s.channel == "web"
        # Index file should exist
        assert self.mm.sessions_index.exists()

    def test_session_idempotent(self):
        s1 = self.mm.get_or_create_session("s1", channel="cli")
        s2 = self.mm.get_or_create_session("s1", channel="cli")
        assert s1.session_id == s2.session_id

    def test_append_and_load(self):
        self.mm.get_or_create_session("s1")
        self.mm.append_message("s1", Message(role="user", content="q1"))
        self.mm.append_message("s1", Message(role="assistant", content="a1"))
        self.mm.append_message("s1", Message(role="user", content="q2"))
        self.mm.append_message("s1", Message(role="assistant", content="a2"))

        history = self.mm.load_history("s1", limit=10)
        assert len(history) == 4
        assert history[0].content == "q1"
        assert history[3].content == "a2"

    def test_load_history_limit(self):
        self.mm.get_or_create_session("s1")
        for i in range(10):
            self.mm.append_message("s1", Message(role="user", content=f"msg-{i}"))
        history = self.mm.load_history("s1", limit=3)
        assert len(history) == 3
        assert history[0].content == "msg-7"

    def test_load_history_truncates_orphan_tool_messages(self):
        """Truncated history should not start with tool or assistant(tool_calls) messages."""
        self.mm.get_or_create_session("s1")
        # Write messages so after limit truncation, first message is a tool msg
        for i in range(5):
            self.mm.append_message("s1", Message(role="user", content=f"u-{i}"))
        # Add tool-related messages that would end up at the start after truncation
        self.mm.append_message("s1", Message(
            role="assistant", content="", tool_calls=[{"name": "t", "args": {}, "id": "c1"}]
        ))
        self.mm.append_message("s1", Message(
            role="tool", content="result", name="t", tool_call_id="c1"
        ))
        self.mm.append_message("s1", Message(role="assistant", content="done"))
        self.mm.append_message("s1", Message(role="user", content="last"))

        # limit=4 → last 4 messages: assistant(tool_calls), tool, assistant, user
        # Should strip the leading assistant(tool_calls) and tool
        history = self.mm.load_history("s1", limit=4)
        assert history[0].role in ("user", "assistant")
        if history[0].role == "assistant":
            assert history[0].tool_calls is None

    def test_reset_session(self):
        self.mm.get_or_create_session("s1")
        self.mm.append_message("s1", Message(role="user", content="hi"))
        self.mm.reset_session("s1")
        history = self.mm.load_history("s1")
        assert len(history) == 0

    def test_load_nonexistent_session(self):
        history = self.mm.load_history("does-not-exist")
        assert history == []

    def test_load_memory_md(self):
        md_path = self.workspace / "memory" / "MEMORY.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("# Long term memory\n- item1")
        assert "item1" in self.mm.load_memory_md()

    def test_load_memory_md_missing(self):
        assert self.mm.load_memory_md() == ""

    def test_load_system_context(self):
        (self.workspace / "AGENTS.md").write_text("agent instructions")
        (self.workspace / "SOUL.md").write_text("personality")
        result = self.mm.load_system_context()
        assert "agent instructions" in result
        assert "personality" in result

    def test_thread_safety_append(self):
        """Multiple threads appending should not corrupt the JSONL."""
        self.mm.get_or_create_session("s1")
        errors = []

        def writer(n):
            try:
                for i in range(20):
                    self.mm.append_message("s1", Message(role="user", content=f"t{n}-m{i}"))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # Should have 100 valid messages
        history = self.mm.load_history("s1", limit=200)
        assert len(history) == 100

    def test_read_all_messages(self):
        self.mm.get_or_create_session("s1")
        for i in range(5):
            self.mm.append_message("s1", Message(role="user", content=f"m{i}"))
        all_msgs = self.mm.read_all_messages("s1")
        assert len(all_msgs) == 5
        assert all_msgs[0].content == "m0"
        assert all_msgs[4].content == "m4"

    def test_read_all_messages_empty(self):
        assert self.mm.read_all_messages("nonexistent") == []

    def test_rewrite_session(self):
        self.mm.get_or_create_session("s1")
        for i in range(10):
            self.mm.append_message("s1", Message(role="user", content=f"m{i}"))
        # Rewrite with only 3 messages
        keep = [Message(role="user", content=f"keep-{i}") for i in range(3)]
        self.mm.rewrite_session("s1", keep)
        all_msgs = self.mm.read_all_messages("s1")
        assert len(all_msgs) == 3
        assert all_msgs[0].content == "keep-0"

    def test_check_and_consolidate_under_threshold(self):
        self.mm.get_or_create_session("s1")
        for i in range(10):
            self.mm.append_message("s1", Message(role="user", content=f"m{i}"))
        result = self.mm.check_and_consolidate("s1", keep_count=50)
        assert result is None
        # Original file unchanged
        assert len(self.mm.read_all_messages("s1")) == 10

    def test_check_and_consolidate_over_count(self):
        self.mm.get_or_create_session("s1")
        for i in range(210):
            self.mm.append_message("s1", Message(role="user", content=f"m{i}"))
        old = self.mm.check_and_consolidate("s1", keep_count=50)
        assert old is not None
        assert len(old) == 160  # 210 - 50
        assert old[0].content == "m0"
        # JSONL should now have only 50 messages
        remaining = self.mm.read_all_messages("s1")
        assert len(remaining) == 50
        assert remaining[0].content == "m160"

    def test_check_and_consolidate_over_size(self):
        self.mm.get_or_create_session("s1")
        # Write messages with large content to exceed 100KB
        big_content = "x" * 2000
        for i in range(60):
            self.mm.append_message("s1", Message(role="user", content=big_content))
        f = self.mm._session_file("s1")
        assert f.stat().st_size > 100_000
        old = self.mm.check_and_consolidate("s1", keep_count=10)
        assert old is not None
        assert len(old) == 50  # 60 - 10
        assert len(self.mm.read_all_messages("s1")) == 10

    def test_check_and_consolidate_nonexistent(self):
        assert self.mm.check_and_consolidate("nope") is None

    def test_reset_session_returns_messages(self):
        self.mm.get_or_create_session("s1")
        self.mm.append_message("s1", Message(role="user", content="hello"))
        self.mm.append_message("s1", Message(role="assistant", content="hi"))
        old = self.mm.reset_session("s1")
        assert len(old) == 2
        assert old[0].content == "hello"
        # Session should be empty now
        assert self.mm.read_all_messages("s1") == []

    def test_append_history(self):
        events = ["[2026-03-30] 用户设置了天气提醒", "[2026-03-30] 创建了定时任务"]
        self.mm.append_history(events)
        content = self.mm.history_md.read_text(encoding="utf-8")
        assert "[2026-03-30] 用户设置了天气提醒" in content
        assert "[2026-03-30] 创建了定时任务" in content
        # Append more — should not overwrite
        self.mm.append_history(["[2026-03-31] 新事件"])
        content = self.mm.history_md.read_text(encoding="utf-8")
        assert "[2026-03-30] 用户设置了天气提醒" in content
        assert "[2026-03-31] 新事件" in content

    def test_append_history_empty(self):
        self.mm.append_history([])
        assert not self.mm.history_md.exists()

    def test_rewrite_memory_md(self):
        self.mm.rewrite_memory_md("# Memory\n- fact 1\n- fact 2")
        content = self.mm.memory_md.read_text(encoding="utf-8")
        assert "fact 1" in content
        assert "fact 2" in content
        # Rewrite again — should overwrite
        self.mm.rewrite_memory_md("# Memory\n- only this")
        content = self.mm.memory_md.read_text(encoding="utf-8")
        assert "fact 1" not in content
        assert "only this" in content

    def test_is_session_interrupted_no_session(self):
        assert self.mm.is_session_interrupted("nonexistent") is False

    def test_is_session_interrupted_normal_complete(self):
        """正常完成的对话不应检测为中断。"""
        sid = "test-interrupt-ok"
        self.mm.get_or_create_session(sid)
        self.mm.append_message(sid, Message(role="user", content="hello"))
        self.mm.append_message(sid, Message(role="assistant", content="hi",
            tool_calls=[{"name": "read_file", "args": {"path": "x"}, "id": "tc1"}]))
        self.mm.append_message(sid, Message(role="tool", content="file content",
            name="read_file", tool_call_id="tc1"))
        self.mm.append_message(sid, Message(role="assistant", content="done"))
        assert self.mm.is_session_interrupted(sid) is False

    def test_is_session_interrupted_missing_tool_result(self):
        """有 tool_call 但缺少 tool result 应检测为中断。"""
        sid = "test-interrupt-yes"
        self.mm.get_or_create_session(sid)
        self.mm.append_message(sid, Message(role="user", content="do something"))
        self.mm.append_message(sid, Message(role="assistant", content="",
            tool_calls=[{"name": "exec_command", "args": {"cmd": "ls"}, "id": "tc1"},
                        {"name": "read_file", "args": {"path": "x"}, "id": "tc2"}]))
        # 只有 tc1 的结果，tc2 缺失
        self.mm.append_message(sid, Message(role="tool", content="result",
            name="exec_command", tool_call_id="tc1"))
        assert self.mm.is_session_interrupted(sid) is True

    def test_is_session_interrupted_no_tool_results_at_all(self):
        """assistant 有 tool_calls 但完全没有 tool result。"""
        sid = "test-interrupt-none"
        self.mm.get_or_create_session(sid)
        self.mm.append_message(sid, Message(role="user", content="go"))
        self.mm.append_message(sid, Message(role="assistant", content="",
            tool_calls=[{"name": "web_search", "args": {"q": "test"}, "id": "tc1"}]))
        assert self.mm.is_session_interrupted(sid) is True

    def test_is_session_interrupted_plain_assistant(self):
        """最后是普通 assistant 消息（无 tool_calls），不应中断。"""
        sid = "test-interrupt-plain"
        self.mm.get_or_create_session(sid)
        self.mm.append_message(sid, Message(role="user", content="hi"))
        self.mm.append_message(sid, Message(role="assistant", content="hello!"))
        assert self.mm.is_session_interrupted(sid) is False


# ─────────────────────────────────────────────
# file_tools.py
# ─────────────────────────────────────────────

from kangclaw.tools.file_tools import read_file, write_file, edit_file, list_files, grep_file, configure, _resolve


class TestFileToolsResolve:
    def test_absolute_path(self):
        p = _resolve("/tmp/test.txt")
        assert p == Path("/tmp/test.txt")

    def test_relative_path_with_workspace(self, tmp_path):
        configure(tmp_path)
        p = _resolve("memory/MEMORY.md")
        assert p == tmp_path / "memory" / "MEMORY.md"

    def test_home_expansion(self):
        p = _resolve("~/test.txt")
        assert "~" not in str(p)


class TestFileTools:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        configure(self.tmp)

    def test_write_and_read(self):
        result = write_file.invoke({"file_path": "test.txt", "content": "hello world"})
        assert "已写入" in result

        result = read_file.invoke({"file_path": "test.txt"})
        assert result == "hello world"

    def test_read_nonexistent(self):
        result = read_file.invoke({"file_path": "no_such_file.txt"})
        assert "不存在" in result

    def test_edit_file(self):
        write_file.invoke({"file_path": "edit_test.txt", "content": "foo bar baz"})
        result = edit_file.invoke({
            "file_path": "edit_test.txt",
            "old_string": "bar",
            "new_string": "qux",
        })
        assert "已替换" in result
        content = read_file.invoke({"file_path": "edit_test.txt"})
        assert content == "foo qux baz"

    def test_edit_not_found(self):
        write_file.invoke({"file_path": "edit2.txt", "content": "abc"})
        result = edit_file.invoke({
            "file_path": "edit2.txt",
            "old_string": "xyz",
            "new_string": "new",
        })
        assert "未找到" in result

    def test_list_files(self):
        write_file.invoke({"file_path": "a.txt", "content": "a"})
        write_file.invoke({"file_path": "b.txt", "content": "b"})
        result = list_files.invoke({"directory": self.tmp, "pattern": "*.txt"})
        assert "a.txt" in result
        assert "b.txt" in result

    def test_list_nonexistent_dir(self):
        result = list_files.invoke({"directory": "/nonexistent_dir_12345"})
        assert "不存在" in result

    def test_write_creates_parent_dirs(self):
        result = write_file.invoke({"file_path": "sub/dir/file.txt", "content": "nested"})
        assert "已写入" in result
        assert (Path(self.tmp) / "sub" / "dir" / "file.txt").exists()

    def test_grep_file_single(self):
        write_file.invoke({"file_path": "grep_test.txt", "content": "line one\nline two\nline three"})
        result = grep_file.invoke({"pattern": "two", "file_path": "grep_test.txt"})
        assert "2: line two" in result

    def test_grep_file_no_match(self):
        write_file.invoke({"file_path": "grep_test2.txt", "content": "hello world"})
        result = grep_file.invoke({"pattern": "xyz", "file_path": "grep_test2.txt"})
        assert "未找到" in result

    def test_grep_directory(self):
        write_file.invoke({"file_path": "dir_a/a.txt", "content": "foo bar"})
        write_file.invoke({"file_path": "dir_a/b.txt", "content": "baz qux"})
        result = grep_file.invoke({"pattern": "foo", "directory": "dir_a"})
        assert "foo bar" in result
        assert "b.txt" not in result

    def test_grep_no_args(self):
        result = grep_file.invoke({"pattern": "test"})
        assert "错误" in result


# ─────────────────────────────────────────────
# cron_tools.py
# ─────────────────────────────────────────────

from kangclaw.tools.cron_tools import cron_list, cron_add, cron_remove, configure as cron_configure


class TestCronTools:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.cron_file = Path(self.tmp) / "cron.json"
        self.cron_file.write_text("[]")
        cron_configure(self.cron_file, None)

    def test_list_empty(self):
        result = cron_list.invoke({})
        assert "没有" in result

    def test_add_and_list(self):
        result = cron_add.invoke({
            "cron_expr": "0 8 * * *",
            "description": "morning check",
        })
        assert "已创建" in result
        assert "morning check" in result

        result = cron_list.invoke({})
        assert "morning check" in result
        assert "0 8 * * *" in result

    def test_remove(self):
        cron_add.invoke({"cron_expr": "0 9 * * *", "description": "task1"})
        jobs = json.loads(self.cron_file.read_text())
        job_id = jobs[0]["id"]

        result = cron_remove.invoke({"job_id": job_id})
        assert "已删除" in result

        result = cron_list.invoke({})
        assert "没有" in result

    def test_remove_nonexistent(self):
        result = cron_remove.invoke({"job_id": "fake_id"})
        assert "未找到" in result

    def test_add_default_channel(self):
        cron_add.invoke({"cron_expr": "0 0 * * *", "description": "test"})
        jobs = json.loads(self.cron_file.read_text())
        assert jobs[0]["channel"] == "web"
        assert jobs[0]["session_id"] == ""
        assert jobs[0]["chat_id"] == ""

    def test_add_cli_channel(self):
        cron_add.invoke({"cron_expr": "0 9 * * *", "description": "cli task", "channel": "cli"})
        jobs = json.loads(self.cron_file.read_text())
        assert jobs[0]["channel"] == "cli"
        assert jobs[0]["session_id"] == ""


# ─────────────────────────────────────────────
# skills/loader.py
# ─────────────────────────────────────────────

from kangclaw.skills.loader import load_skills_summary, load_skill_detail, _BUILTIN_SKILLS_DIR


class TestSkillsLoader:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.skills_dir = self.tmp / "skills"
        self.skills_dir.mkdir()
        # Mock builtin skills dir to an empty temp dir so tests are isolated
        self._empty_dir = Path(tempfile.mkdtemp())
        self._patcher = patch("kangclaw.skills.loader._BUILTIN_SKILLS_DIR", self._empty_dir)
        self._patcher.start()

    def teardown_method(self):
        self._patcher.stop()

    def test_no_skills(self):
        assert load_skills_summary(self.skills_dir) == ""

    def test_load_one_skill(self):
        s = self.skills_dir / "weather"
        s.mkdir()
        (s / "SKILL.md").write_text("# Weather\nCheck weather conditions.\n\nMore details...")
        result = load_skills_summary(self.skills_dir)
        assert "weather" in result
        assert "Check weather conditions" in result
        assert "read_file" in result

    def test_load_multiple_skills(self):
        for name, desc in [("a", "Skill A desc"), ("b", "Skill B desc")]:
            d = self.skills_dir / name
            d.mkdir()
            (d / "SKILL.md").write_text(f"# {name}\n{desc}")
        result = load_skills_summary(self.skills_dir)
        assert "Skill A desc" in result
        assert "Skill B desc" in result

    def test_skip_non_directories(self):
        (self.skills_dir / "readme.txt").write_text("not a skill")
        assert load_skills_summary(self.skills_dir) == ""

    def test_skip_dirs_without_skill_md(self):
        (self.skills_dir / "incomplete").mkdir()
        assert load_skills_summary(self.skills_dir) == ""

    def test_load_skill_detail(self):
        s = self.skills_dir / "test"
        s.mkdir()
        (s / "SKILL.md").write_text("full content here")
        result = load_skill_detail(self.skills_dir, "test")
        assert result == "full content here"

    def test_load_skill_detail_missing(self):
        assert load_skill_detail(self.skills_dir, "nope") is None

    def test_nonexistent_skills_dir(self):
        assert load_skills_summary(self.tmp / "nope") == ""

    def test_skip_yaml_frontmatter(self):
        s = self.skills_dir / "weather"
        s.mkdir()
        (s / "SKILL.md").write_text(
            "---\nname: weather\ndescription: Get weather\n---\n\n# Weather\n\nTwo free services, no API keys needed."
        )
        result = load_skills_summary(self.skills_dir)
        assert "weather" in result
        assert "Two free services" in result
        assert "name: weather" not in result

    def test_builtin_and_user_skills_merged(self):
        # Add a user skill
        s = self.skills_dir / "user_skill"
        s.mkdir()
        (s / "SKILL.md").write_text("# User Skill\nUser skill description.")
        # Add a builtin skill
        b = self._empty_dir / "builtin_skill"
        b.mkdir()
        (b / "SKILL.md").write_text("# Builtin\nBuiltin description.")
        result = load_skills_summary(self.skills_dir)
        assert "User skill description" in result
        assert "Builtin description" in result

    def test_user_skill_overrides_builtin(self):
        # Same name in both dirs — user version should win
        for d in (self.skills_dir, self._empty_dir):
            s = d / "weather"
            s.mkdir()
        (self.skills_dir / "weather" / "SKILL.md").write_text("# Weather\nUser version.")
        (self._empty_dir / "weather" / "SKILL.md").write_text("# Weather\nBuiltin version.")
        result = load_skills_summary(self.skills_dir)
        assert "User version" in result
        assert "Builtin version" not in result


# ─────────────────────────────────────────────
# agent.py — session lock logic
# ─────────────────────────────────────────────

from kangclaw.gateway.agent import Agent


class TestAgentSessionLock:
    def test_get_session_lock_creates_lock(self):
        cfg = AppConfig()
        mm = MagicMock()
        mm.load_system_context.return_value = ""
        agent = Agent.__new__(Agent)
        agent._session_locks = {}
        lock = agent._get_session_lock("s1")
        assert isinstance(lock, asyncio.Lock)

    def test_get_session_lock_reuses(self):
        agent = Agent.__new__(Agent)
        agent._session_locks = {}
        l1 = agent._get_session_lock("s1")
        l2 = agent._get_session_lock("s1")
        assert l1 is l2

    def test_different_sessions_different_locks(self):
        agent = Agent.__new__(Agent)
        agent._session_locks = {}
        l1 = agent._get_session_lock("s1")
        l2 = agent._get_session_lock("s2")
        assert l1 is not l2


# ─────────────────────────────────────────────
# router.py
# ─────────────────────────────────────────────

from kangclaw.gateway.router import Router, IncomingMessage, Attachment


class TestRouter:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp())
        # Create a SOUL.md with content so _build_greeting_prompt works
        (self.tmp / "SOUL.md").write_text(
            "# SOUL.md\n\n## 名字\n\nkangclaw\n\n## 性格\n\n- 干脆利落\n"
        )

    @pytest.mark.asyncio
    async def test_reset_command(self):
        mock_agent = MagicMock()
        mock_agent.memory = MagicMock()
        mock_agent.memory.workspace = self.tmp
        mock_agent.memory.reset_session.return_value = [Message(role="user", content="old")]
        mock_agent.consolidate = AsyncMock()

        async def fake_process(session_id, user_content, channel, metadata=None, attachments=None):
            yield "你好！"

        mock_agent.process = fake_process
        router = Router(mock_agent)

        tokens = []
        async for t in router.handle(IncomingMessage(
            channel="web", session_id="s1", user_id="u1", content="/reset"
        )):
            tokens.append(t)

        mock_agent.memory.reset_session.assert_called_once_with("s1")
        mock_agent.consolidate.assert_called_once()
        # Should have greeting from agent
        assert any("你好" in t for t in tokens)

    @pytest.mark.asyncio
    async def test_new_command(self):
        mock_agent = MagicMock()
        mock_agent.memory = MagicMock()
        mock_agent.memory.workspace = self.tmp
        mock_agent.memory.reset_session.return_value = []
        mock_agent.consolidate = AsyncMock()

        async def fake_process(session_id, user_content, channel, metadata=None, attachments=None):
            yield "hi"

        mock_agent.process = fake_process
        router = Router(mock_agent)

        tokens = []
        async for t in router.handle(IncomingMessage(
            channel="cli", session_id="s1", user_id="u1", content="/new"
        )):
            tokens.append(t)

        # Empty messages — consolidate should not be called
        mock_agent.consolidate.assert_not_called()
        # Should still get a greeting
        assert any("hi" in t for t in tokens)

    @pytest.mark.asyncio
    async def test_normal_message_calls_agent(self):
        mock_agent = MagicMock()
        mock_agent.memory = MagicMock()

        async def fake_process(session_id, user_content, channel, metadata=None, attachments=None):
            yield "hello"
            yield " world"

        mock_agent.process = fake_process
        router = Router(mock_agent)

        tokens = []
        async for t in router.handle(IncomingMessage(
            channel="web", session_id="s1", user_id="u1", content="hi"
        )):
            tokens.append(t)

        assert tokens == ["hello", " world"]


    @pytest.mark.asyncio
    async def test_stop_command_with_running_task(self):
        """有正在执行的任务时 /stop 不应产生额外回复。"""
        mock_agent = MagicMock()
        mock_agent.memory = MagicMock()
        mock_agent.request_cancel = MagicMock(return_value=True)
        router = Router(mock_agent)

        tokens = []
        async for t in router.handle(IncomingMessage(
            channel="web", session_id="s1", user_id="u1", content="/stop"
        )):
            tokens.append(t)

        mock_agent.request_cancel.assert_called_once_with("s1")
        assert tokens == []

    @pytest.mark.asyncio
    async def test_stop_command_no_running_task(self):
        """/stop 无正在执行的任务时应提示。"""
        mock_agent = MagicMock()
        mock_agent.memory = MagicMock()
        mock_agent.request_cancel = MagicMock(return_value=False)
        router = Router(mock_agent)

        tokens = []
        async for t in router.handle(IncomingMessage(
            channel="web", session_id="s1", user_id="u1", content="/stop"
        )):
            tokens.append(t)

        assert tokens == ["[当前没有正在执行的任务]"]


# ─────────────────────────────────────────────
# web_tools.py
# ─────────────────────────────────────────────

from kangclaw.tools.web_tools import web_search, web_fetch


class TestWebTools:
    def test_web_search_returns_string(self):
        """web_search should return a string (success or error)."""
        result = web_search.invoke({"query": "python programming", "max_results": 2})
        assert isinstance(result, str)
        # Should either have results or an error message, not crash
        assert len(result) > 0

    def test_web_fetch_invalid_url(self):
        result = web_fetch.invoke({"url": "http://this-domain-does-not-exist-12345.com"})
        assert "失败" in result


# ─────────────────────────────────────────────
# defaults — ensure all template files exist
# ─────────────────────────────────────────────

class TestDefaults:
    def test_all_default_files_exist(self):
        from importlib import resources
        defaults = resources.files("kangclaw") / "defaults"
        expected = ["config.toml", "AGENTS.md", "SOUL.md", "USER.md", "HEARTBEAT.md", "MEMORY.md"]
        for name in expected:
            f = defaults / name
            assert f.is_file(), f"Missing default file: {name}"


# ─────────────────────────────────────────────
# web_ui — static files exist
# ─────────────────────────────────────────────

class TestWebUI:
    def test_static_files_exist(self):
        static = Path(__file__).parent.parent / "src" / "kangclaw" / "web_ui" / "static"
        for name in ["index.html", "app.js", "style.css"]:
            assert (static / name).exists(), f"Missing static file: {name}"


# ─────────────────────────────────────────────
# channels/feishu.py
# ─────────────────────────────────────────────

from kangclaw.channels.feishu import FeishuChannel


class TestFeishuChannel:
    """飞书渠道单元测试。"""

    def _make_channel(self, **extra) -> FeishuChannel:
        defaults = {
            "app_id": "cli_test_id",
            "app_secret": "cli_test_secret",
            "allow_from": [],
        }
        defaults.update(extra)
        cfg = ChannelConfig(name="feishu", enabled=True, extra=defaults)
        router = MagicMock()
        return FeishuChannel(cfg, router)

    def test_init_parses_config(self):
        ch = self._make_channel()
        assert ch.app_id == "cli_test_id"
        assert ch.app_secret == "cli_test_secret"
        assert ch.allow_from == []

    def test_check_allow_empty_allows_all(self):
        ch = self._make_channel()
        assert ch._check_allow("anyone") is True

    def test_check_allow_whitelist(self):
        ch = self._make_channel(allow_from=["user_a", "user_b"])
        assert ch._check_allow("user_a") is True
        assert ch._check_allow("user_c") is False

    def test_session_chat_map(self):
        ch = self._make_channel()
        ch._session_chat_map["feishu-g_chat123"] = "chat123"
        assert ch._session_chat_map.get("feishu-g_chat123") == "chat123"
        assert ch._session_chat_map.get("feishu-u_unknown") is None

    @pytest.mark.asyncio
    async def test_start_without_lark_oapi(self):
        """lark-oapi 缺失时 start 应优雅处理。"""
        ch = self._make_channel()
        with patch.dict("sys.modules", {"lark_oapi": None}):
            # start 不应抛异常
            await ch.start()
            assert ch._client is None

    @pytest.mark.asyncio
    async def test_stop_clears_state(self):
        ch = self._make_channel()
        ch._client = MagicMock()
        ch._ws_client = MagicMock()
        await ch.stop()
        assert ch._client is None
        assert ch._ws_client is None

    @pytest.mark.asyncio
    async def test_send_without_client_logs_warning(self):
        """客户端未初始化时 send 不应抛异常。"""
        ch = self._make_channel()
        ch._client = None
        # 不应抛异常
        await ch.send("feishu-g_chat1", "hello")

    @pytest.mark.asyncio
    async def test_send_without_chat_id_logs_warning(self):
        """未知 session 的 send 不应抛异常。"""
        ch = self._make_channel()
        ch._client = MagicMock()
        # session_chat_map 中没有这个 session
        await ch.send("feishu-g_unknown", "hello")

    @pytest.mark.asyncio
    async def test_handle_message_routes_to_router(self):
        """群聊消息应先发思考中卡片，再流式更新。"""
        ch = self._make_channel()
        ch._client = MagicMock()

        async def fake_handle(msg):
            yield "你好"
            yield "世界"

        ch.router.handle = fake_handle
        ch._reply_card = AsyncMock(return_value="msg_reply_id")
        ch._send_card = AsyncMock()
        ch._patch_card = AsyncMock()

        await ch._handle_message("feishu-g_chat1", "user1", "hello", "msg_id_1", "group", "chat1")

        # 先发"思考中"卡片
        ch._reply_card.assert_called_once_with("msg_id_1", "思考中...", streaming=True)
        # 最终更新完整内容
        ch._patch_card.assert_called_with("msg_reply_id", "你好世界", streaming=False)

    @pytest.mark.asyncio
    async def test_handle_message_private_chat_uses_send(self):
        """私聊应用 send_card 发送思考中卡片。"""
        ch = self._make_channel()
        ch._client = MagicMock()

        async def fake_handle(msg):
            yield "hi"

        ch.router.handle = fake_handle
        ch._send_card = AsyncMock(return_value="msg_send_id")
        ch._reply_card = AsyncMock()
        ch._patch_card = AsyncMock()

        await ch._handle_message("feishu-u_user1", "user1", "hello", "msg_id_1", "p2p", "chat1")

        ch._send_card.assert_called_once_with("chat1", "思考中...", streaming=True)
        ch._patch_card.assert_called_with("msg_send_id", "hi", streaming=False)

    @pytest.mark.asyncio
    async def test_handle_message_empty_reply(self):
        """空回复应更新卡片为无回复提示。"""
        ch = self._make_channel()
        ch._client = MagicMock()

        async def fake_handle(msg):
            return
            yield

        ch.router.handle = fake_handle
        ch._send_card = AsyncMock(return_value="msg_send_id")
        ch._patch_card = AsyncMock()

        await ch._handle_message("feishu-u_user1", "user1", "hello", "msg_id_1", "p2p", "chat1")

        ch._patch_card.assert_called_with("msg_send_id", "（无回复）", streaming=False)

    def test_build_card_basic(self):
        """卡片构建应包含 markdown 元素。"""
        ch = self._make_channel()
        card_json = ch._build_card("hello world")
        card = json.loads(card_json)
        assert card["elements"][0]["tag"] == "markdown"
        assert card["elements"][0]["content"] == "hello world"
        assert len(card["elements"]) == 1

    def test_build_card_streaming(self):
        """流式卡片应包含 note 指示器。"""
        ch = self._make_channel()
        card_json = ch._build_card("thinking", streaming=True)
        card = json.loads(card_json)
        assert len(card["elements"]) == 2
        assert card["elements"][1]["tag"] == "note"


# ─────────────────────────────────────────────
# channels/dingtalk.py
# ─────────────────────────────────────────────

from kangclaw.channels.dingtalk import DingTalkChannel


class TestDingTalkChannel:
    """钉钉渠道单元测试。"""

    def _make_channel(self, **extra) -> DingTalkChannel:
        defaults = {
            "client_id": "test_client_id",
            "client_secret": "test_client_secret",
            "allow_from": [],
        }
        defaults.update(extra)
        cfg = ChannelConfig(name="dingtalk", enabled=True, extra=defaults)
        router = MagicMock()
        return DingTalkChannel(cfg, router)

    def test_init_parses_config(self):
        ch = self._make_channel()
        assert ch.client_id == "test_client_id"
        assert ch.client_secret == "test_client_secret"
        assert ch.allow_from == []

    def test_check_allow_empty_allows_all(self):
        ch = self._make_channel()
        assert ch._check_allow("anyone") is True

    def test_check_allow_star_allows_all(self):
        ch = self._make_channel(allow_from=["*"])
        assert ch._check_allow("anyone") is True

    def test_check_allow_whitelist(self):
        ch = self._make_channel(allow_from=["user_a", "user_b"])
        assert ch._check_allow("user_a") is True
        assert ch._check_allow("user_c") is False

    def test_session_chat_map(self):
        ch = self._make_channel()
        ch._session_conversation_map["dingtalk-g_conv123"] = "conv123"
        assert ch._session_conversation_map.get("dingtalk-g_conv123") == "conv123"
        assert ch._session_conversation_map.get("dingtalk-u_unknown") is None

    @pytest.mark.asyncio
    async def test_start_without_dingtalk_stream(self):
        """dingtalk-stream 缺失时 start 应优雅处理。"""
        ch = self._make_channel()
        with patch.dict("sys.modules", {"dingtalk_stream": None}):
            await ch.start()
            assert ch._client is None

    @pytest.mark.asyncio
    async def test_stop_clears_state(self):
        ch = self._make_channel()
        ch._client = MagicMock()
        await ch.stop()
        assert ch._client is None
        assert ch._main_loop is None

    @pytest.mark.asyncio
    async def test_send_without_client_logs_warning(self):
        """客户端未初始化时 send 不应抛异常。"""
        ch = self._make_channel()
        ch._client = None
        await ch.send("dingtalk-g_conv1", "hello")

    @pytest.mark.asyncio
    async def test_handle_message_group(self):
        """群聊消息应路由到 router 并分段回复。"""
        ch = self._make_channel()

        async def fake_handle(msg):
            yield "你好"
            yield "[TOOL_BREAK]"
            yield "世界"

        ch.router.handle = fake_handle
        ch._reply_markdown = MagicMock()

        # 模拟 incoming_message
        incoming = MagicMock()
        incoming.conversation_type = "2"
        incoming.conversation_id = "conv_group_1"
        incoming.sender_staff_id = "staff_1"
        incoming.sender_id = "sender_1"
        incoming.text.content = " hello "
        incoming.message_id = "msg_001"

        await ch._handle_message(incoming)

        # 应分两段回复
        assert ch._reply_markdown.call_count == 2

    @pytest.mark.asyncio
    async def test_handle_message_private(self):
        """单聊消息应正确设置 session_id。"""
        ch = self._make_channel()

        async def fake_handle(msg):
            yield "hi"

        ch.router.handle = fake_handle
        ch._reply_markdown = MagicMock()

        incoming = MagicMock()
        incoming.conversation_type = "1"
        incoming.conversation_id = ""
        incoming.sender_staff_id = "staff_private"
        incoming.sender_id = "sender_private"
        incoming.text.content = " hello "
        incoming.message_id = "msg_002"

        await ch._handle_message(incoming)

        assert ch._reply_markdown.call_count == 1

    @pytest.mark.asyncio
    async def test_handle_message_empty_reply(self):
        """空回复不应发送消息。"""
        ch = self._make_channel()

        async def fake_handle(msg):
            return
            yield

        ch.router.handle = fake_handle
        ch._reply_markdown = MagicMock()

        incoming = MagicMock()
        incoming.conversation_type = "1"
        incoming.conversation_id = ""
        incoming.sender_staff_id = "staff_1"
        incoming.sender_id = "sender_1"
        incoming.text.content = " hello "
        incoming.message_id = "msg_003"

        await ch._handle_message(incoming)

        ch._reply_markdown.assert_not_called()

    def test_message_dedup(self):
        """重复消息 ID 应被跳过。"""
        ch = self._make_channel()
        assert ch._is_duplicate("msg_001") is False
        assert ch._is_duplicate("msg_001") is True
        assert ch._is_duplicate("msg_002") is False


# ─────────────────────────────────────────────
# Attachment 数据模型
# ─────────────────────────────────────────────


class TestAttachment:
    def test_create_image_attachment(self):
        att = Attachment(type="image", url="https://example.com/img.png", filename="img.png")
        assert att.type == "image"
        assert att.url == "https://example.com/img.png"
        assert att.filename == "img.png"
        assert att.mime_type == ""
        assert att.file_path == ""

    def test_create_file_attachment_with_path(self):
        att = Attachment(type="file", filename="doc.pdf", file_path="/tmp/doc.pdf", mime_type="application/pdf")
        assert att.type == "file"
        assert att.file_path == "/tmp/doc.pdf"

    def test_attachment_to_dict(self):
        att = Attachment(type="image", url="https://example.com/img.png", filename="img.png")
        d = att.to_dict()
        assert d["type"] == "image"
        assert d["url"] == "https://example.com/img.png"
        assert d["filename"] == "img.png"

    def test_incoming_message_with_attachments(self):
        att = Attachment(type="image", url="https://example.com/img.png", filename="img.png")
        msg = IncomingMessage(
            channel="web", session_id="s1", user_id="u1",
            content="看这张图", attachments=[att],
        )
        assert len(msg.attachments) == 1
        assert msg.attachments[0].type == "image"

    def test_incoming_message_default_no_attachments(self):
        msg = IncomingMessage(channel="web", session_id="s1", user_id="u1", content="hello")
        assert msg.attachments == []


# ─────────────────────────────────────────────
# gateway/media.py — MediaManager
# ─────────────────────────────────────────────


class TestMediaManager:
    """媒体管理器测试。"""

    def test_media_dir_created(self, tmp_path):
        from kangclaw.gateway.media import MediaManager
        mm = MediaManager(tmp_path)
        assert (tmp_path / "media").is_dir()

    @pytest.mark.asyncio
    async def test_download_file(self, tmp_path):
        """下载远程文件到本地。"""
        from kangclaw.gateway.media import MediaManager
        mm = MediaManager(tmp_path)
        with patch("kangclaw.gateway.media.aiohttp") as mock_aiohttp:
            mock_resp = AsyncMock()
            mock_resp.status = 200
            mock_resp.read = AsyncMock(return_value=b"fake image data")
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session.get = MagicMock(return_value=mock_resp)
            mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_resp.__aexit__ = AsyncMock(return_value=False)
            mock_aiohttp.ClientSession = MagicMock(return_value=mock_session)

            path = await mm.download("https://example.com/img.png", "img.png")
            assert path.endswith("img.png")
            assert Path(path).parent.exists()

    def test_save_base64_data_url(self, tmp_path):
        """保存 base64 data URL 到本地文件。"""
        from kangclaw.gateway.media import MediaManager
        import base64
        mm = MediaManager(tmp_path)
        data = base64.b64encode(b"hello").decode()
        data_url = f"data:text/plain;base64,{data}"
        path = mm.save_data_url(data_url, "test.txt")
        assert Path(path).exists()
        assert Path(path).read_bytes() == b"hello"

    def test_image_to_base64(self, tmp_path):
        """图片文件转 base64 data URL。"""
        from kangclaw.gateway.media import MediaManager
        mm = MediaManager(tmp_path)
        img_path = tmp_path / "test.png"
        img_path.write_bytes(b"\x89PNG\r\n\x1a\n fake png")
        result = mm.image_to_data_url(str(img_path))
        assert result.startswith("data:image/png;base64,")

    def test_extract_pdf_text(self, tmp_path):
        """PDF 文本提取（mock pdfplumber）。"""
        from kangclaw.gateway.media import MediaManager
        mm = MediaManager(tmp_path)
        with patch("kangclaw.gateway.media.pdfplumber") as mock_pdf:
            mock_page = MagicMock()
            mock_page.extract_text.return_value = "Hello PDF"
            mock_doc = MagicMock()
            mock_doc.pages = [mock_page]
            mock_doc.__enter__ = MagicMock(return_value=mock_doc)
            mock_doc.__exit__ = MagicMock(return_value=False)
            mock_pdf.open.return_value = mock_doc
            text = mm.extract_pdf_text("/fake/doc.pdf")
            assert text == "Hello PDF"

    def test_extract_pdf_not_installed(self, tmp_path):
        """pdfplumber 未安装时返回提示。"""
        from kangclaw.gateway.media import MediaManager
        mm = MediaManager(tmp_path)
        with patch("kangclaw.gateway.media.pdfplumber", None):
            text = mm.extract_pdf_text("/fake/doc.pdf")
            assert "pdfplumber" in text.lower() or "未安装" in text


# ─────────────────────────────────────────────
# agent.py — _build_user_content
# ─────────────────────────────────────────────

class TestAgentAttachments:
    """Agent 处理多媒体附件的测试。"""

    def test_build_user_content_text_only(self):
        from kangclaw.gateway.agent import _build_user_content
        result = _build_user_content("hello", [])
        assert result == "hello"

    def test_build_user_content_with_image(self):
        from kangclaw.gateway.agent import _build_user_content
        from kangclaw.gateway.router import Attachment
        att = Attachment(type="image", filename="img.png",
                         file_path="/tmp/img.png",
                         extra={"data_url": "data:image/png;base64,abc123"})
        result = _build_user_content("看这张图", [att])
        assert isinstance(result, list)
        assert result[0]["type"] == "text"
        assert "看这张图" in result[0]["text"]
        assert "/tmp/img.png" in result[0]["text"]
        assert result[1]["type"] == "image_url"
        assert result[1]["image_url"]["url"] == "data:image/png;base64,abc123"

    def test_build_user_content_with_pdf(self):
        from kangclaw.gateway.agent import _build_user_content
        from kangclaw.gateway.router import Attachment
        att = Attachment(type="file", filename="report.pdf",
                         file_path="/tmp/report.pdf",
                         extra={"extracted_text": "这是 PDF 内容"})
        result = _build_user_content("请看附件", [att])
        assert isinstance(result, str)
        assert "这是 PDF 内容" in result
        assert "report.pdf" in result

    def test_build_user_content_with_audio_transcription(self):
        from kangclaw.gateway.agent import _build_user_content
        from kangclaw.gateway.router import Attachment
        att = Attachment(type="audio", filename="voice.wav",
                         file_path="/tmp/voice.wav",
                         extra={"transcription": "你好世界"})
        result = _build_user_content("", [att])
        assert isinstance(result, str)
        assert "你好世界" in result

    def test_build_user_content_with_generic_file(self):
        from kangclaw.gateway.agent import _build_user_content
        from kangclaw.gateway.router import Attachment
        att = Attachment(type="file", filename="data.csv",
                         file_path="/tmp/data.csv")
        result = _build_user_content("看看这个文件", [att])
        assert isinstance(result, str)
        assert "/tmp/data.csv" in result

    def test_build_user_content_mixed(self):
        from kangclaw.gateway.agent import _build_user_content
        from kangclaw.gateway.router import Attachment
        img = Attachment(type="image", filename="img.png",
                          file_path="/tmp/img.png",
                          extra={"data_url": "data:image/png;base64,abc"})
        doc = Attachment(type="file", filename="doc.txt",
                          file_path="/tmp/doc.txt")
        result = _build_user_content("看看这些", [img, doc])
        assert isinstance(result, list)
        text_part = result[0]["text"]
        assert "/tmp/doc.txt" in text_part
        assert "/tmp/img.png" in text_part
        assert result[1]["type"] == "image_url"


# ─────────────────────────────────────────────
# 钉钉渠道多媒体消息
# ─────────────────────────────────────────────


class TestDingTalkMultimedia:
    """钉钉渠道多媒体消息测试。"""

    def _make_channel(self, **extra):
        from kangclaw.channels.dingtalk import DingTalkChannel
        defaults = {"client_id": "test_id", "client_secret": "test_secret", "allow_from": []}
        defaults.update(extra)
        cfg = ChannelConfig(name="dingtalk", enabled=True, extra=defaults)
        router = MagicMock()
        return DingTalkChannel(cfg, router)

    @pytest.mark.asyncio
    async def test_handle_image_message(self):
        """图片消息应解析为 Attachment。"""
        ch = self._make_channel()
        captured_msg = {}

        async def fake_handle(msg):
            captured_msg.update(vars(msg))
            yield "收到图片"

        ch.router.handle = fake_handle
        ch._reply_markdown = MagicMock()
        ch._get_image_url = MagicMock(return_value="https://example.com/image.png")

        incoming = MagicMock()
        incoming.message_type = "picture"
        incoming.conversation_type = "1"
        incoming.conversation_id = ""
        incoming.sender_staff_id = "staff_1"
        incoming.sender_id = "sender_1"
        incoming.message_id = "msg_img_001"
        incoming.text = None
        incoming.image_content = MagicMock()
        incoming.image_content.download_code = "dl_code_123"

        await ch._handle_message(incoming)

        assert len(captured_msg.get("attachments", [])) == 1
        assert captured_msg["attachments"][0].type == "image"
        assert captured_msg["attachments"][0].url == "https://example.com/image.png"

    @pytest.mark.asyncio
    async def test_handle_text_message_no_attachments(self):
        """纯文本消息应无附件。"""
        ch = self._make_channel()
        captured_msg = {}

        async def fake_handle(msg):
            captured_msg.update(vars(msg))
            yield "ok"

        ch.router.handle = fake_handle
        ch._reply_markdown = MagicMock()

        incoming = MagicMock()
        incoming.message_type = "text"
        incoming.conversation_type = "1"
        incoming.conversation_id = ""
        incoming.sender_staff_id = "staff_1"
        incoming.sender_id = "sender_1"
        incoming.message_id = "msg_txt_001"
        incoming.text = MagicMock()
        incoming.text.content = " hello "
        incoming.image_content = None

        await ch._handle_message(incoming)

        assert captured_msg.get("attachments", []) == []


# ─────────────────────────────────────────────
# 飞书渠道多媒体消息
# ─────────────────────────────────────────────


class TestFeishuMultimedia:
    """飞书渠道多媒体消息测试。"""

    def _make_channel(self, **extra):
        defaults = {"app_id": "cli_test_id", "app_secret": "cli_test_secret", "allow_from": []}
        defaults.update(extra)
        cfg = ChannelConfig(name="feishu", enabled=True, extra=defaults)
        router = MagicMock()
        return FeishuChannel(cfg, router)

    def test_parse_image_content(self):
        from kangclaw.channels.feishu import FeishuChannel
        content_json = '{"image_key": "img_v2_abc123"}'
        result = FeishuChannel._parse_media_content("image", content_json)
        assert result["image_key"] == "img_v2_abc123"

    def test_parse_file_content(self):
        from kangclaw.channels.feishu import FeishuChannel
        content_json = '{"file_key": "file_v2_xyz", "file_name": "doc.pdf"}'
        result = FeishuChannel._parse_media_content("file", content_json)
        assert result["file_key"] == "file_v2_xyz"
        assert result["file_name"] == "doc.pdf"

    def test_parse_audio_returns_none(self):
        """audio 不再作为支持类型。"""
        from kangclaw.channels.feishu import FeishuChannel
        result = FeishuChannel._parse_media_content("audio", '{"file_key": "file_v2_audio"}')
        assert result is None

    def test_parse_video_returns_none(self):
        from kangclaw.channels.feishu import FeishuChannel
        result = FeishuChannel._parse_media_content("video", '{"file_key": "file_v2_video"}')
        assert result is None

    def test_parse_unsupported_type_returns_none(self):
        from kangclaw.channels.feishu import FeishuChannel
        result = FeishuChannel._parse_media_content("sticker", '{"sticker_key":"s1"}')
        assert result is None


class TestQQMultimedia:
    """QQ 渠道多媒体消息测试。"""

    def test_parse_attachments_image(self):
        from kangclaw.channels.qq import QQChannel
        att_mock = MagicMock()
        att_mock.content_type = "image/jpeg"
        att_mock.url = "https://example.com/img.jpg"
        att_mock.filename = "photo.jpg"
        result = QQChannel._parse_attachment(att_mock)
        assert result.type == "image"
        assert result.url == "https://example.com/img.jpg"
        assert result.filename == "photo.jpg"

    def test_parse_attachments_video(self):
        from kangclaw.channels.qq import QQChannel
        att_mock = MagicMock()
        att_mock.content_type = "video/mp4"
        att_mock.url = "https://example.com/v.mp4"
        att_mock.filename = "clip.mp4"
        result = QQChannel._parse_attachment(att_mock)
        assert result.type == "video"

    def test_parse_attachments_audio(self):
        from kangclaw.channels.qq import QQChannel
        att_mock = MagicMock()
        att_mock.content_type = "audio/silk"
        att_mock.url = "https://example.com/a.silk"
        att_mock.filename = "voice.silk"
        result = QQChannel._parse_attachment(att_mock)
        assert result.type == "audio"

    def test_parse_attachments_file(self):
        from kangclaw.channels.qq import QQChannel
        att_mock = MagicMock()
        att_mock.content_type = "application/pdf"
        att_mock.url = "https://example.com/doc.pdf"
        att_mock.filename = "doc.pdf"
        result = QQChannel._parse_attachment(att_mock)
        assert result.type == "file"

    def test_parse_attachments_none_content_type(self):
        from kangclaw.channels.qq import QQChannel
        att_mock = MagicMock()
        att_mock.content_type = None
        att_mock.url = "https://example.com/unknown"
        att_mock.filename = "unknown"
        result = QQChannel._parse_attachment(att_mock)
        assert result.type == "file"


# ─────────────────────────────────────────────
# Task 7: Web 渠道接收多媒体消息
# ─────────────────────────────────────────────

class TestWebMultimedia:
    def test_parse_ws_message_text(self):
        from kangclaw.gateway.server import _parse_ws_message
        content, attachments = _parse_ws_message("hello")
        assert content == "hello"
        assert attachments == []

    def test_parse_ws_message_json_with_attachments(self):
        from kangclaw.gateway.server import _parse_ws_message
        msg = json.dumps({
            "content": "看图",
            "attachments": [
                {"type": "image", "data": "data:image/png;base64,iVBOR", "filename": "img.png"}
            ]
        })
        content, attachments = _parse_ws_message(msg)
        assert content == "看图"
        assert len(attachments) == 1
        assert attachments[0].type == "image"
        assert attachments[0].filename == "img.png"
        assert attachments[0].url == "data:image/png;base64,iVBOR"

    def test_parse_ws_message_json_no_attachments(self):
        from kangclaw.gateway.server import _parse_ws_message
        msg = json.dumps({"content": "hi"})
        content, attachments = _parse_ws_message(msg)
        assert content == "hi"
        assert attachments == []

    def test_parse_ws_message_invalid_json(self):
        from kangclaw.gateway.server import _parse_ws_message
        content, attachments = _parse_ws_message("{invalid json")
        assert content == "{invalid json"
        assert attachments == []


# ─────────────────────────────────────────────
# MediaManager.process_attachment 集成
# ─────────────────────────────────────────────

class TestProcessAttachment:
    """MediaManager.process_attachment 下载+转换测试。"""

    @pytest.mark.asyncio
    async def test_process_image_attachment(self, tmp_path):
        """图片附件应下载并转 base64 data URL。"""
        from kangclaw.gateway.media import MediaManager
        from kangclaw.gateway.router import Attachment

        mm = MediaManager(tmp_path)

        # mock download 返回本地路径
        img_path = tmp_path / "media" / "test.png"
        img_path.parent.mkdir(parents=True, exist_ok=True)
        img_path.write_bytes(b"\x89PNG\r\n\x1a\n fake png")
        mm.download = AsyncMock(return_value=str(img_path))

        att = Attachment(type="image", url="https://example.com/img.png", filename="img.png")
        result = await mm.process_attachment(att)

        assert result.file_path == str(img_path)
        assert result.extra.get("data_url", "").startswith("data:image/png;base64,")

    @pytest.mark.asyncio
    async def test_process_pdf_attachment(self, tmp_path):
        """PDF 附件应下载并提取文本。"""
        from kangclaw.gateway.media import MediaManager
        from kangclaw.gateway.router import Attachment

        mm = MediaManager(tmp_path)

        pdf_path = tmp_path / "media" / "doc.pdf"
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(b"fake pdf")
        mm.download = AsyncMock(return_value=str(pdf_path))
        mm.extract_pdf_text = MagicMock(return_value="PDF 内容")

        att = Attachment(type="file", url="https://example.com/doc.pdf", filename="doc.pdf")
        result = await mm.process_attachment(att)

        assert result.file_path == str(pdf_path)
        assert result.extra["extracted_text"] == "PDF 内容"

    @pytest.mark.asyncio
    async def test_process_audio_with_transcription(self, tmp_path):
        """已有转写的音频附件不应被覆盖。"""
        from kangclaw.gateway.media import MediaManager
        from kangclaw.gateway.router import Attachment

        mm = MediaManager(tmp_path)

        audio_path = tmp_path / "media" / "voice.wav"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"fake audio")
        mm.download = AsyncMock(return_value=str(audio_path))

        att = Attachment(type="audio", url="https://example.com/voice.wav",
                         filename="voice.wav", extra={"transcription": "你好"})
        result = await mm.process_attachment(att)

        assert result.file_path == str(audio_path)
        assert result.extra["transcription"] == "你好"

    @pytest.mark.asyncio
    async def test_process_generic_file(self, tmp_path):
        """普通文件应下载并记录路径。"""
        from kangclaw.gateway.media import MediaManager
        from kangclaw.gateway.router import Attachment

        mm = MediaManager(tmp_path)

        file_path = tmp_path / "media" / "data.csv"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(b"a,b,c")
        mm.download = AsyncMock(return_value=str(file_path))

        att = Attachment(type="file", url="https://example.com/data.csv", filename="data.csv")
        result = await mm.process_attachment(att)

        assert result.file_path == str(file_path)
        assert "extracted_text" not in result.extra

    @pytest.mark.asyncio
    async def test_process_already_local(self, tmp_path):
        """已有 file_path 的附件不应重复下载。"""
        from kangclaw.gateway.media import MediaManager
        from kangclaw.gateway.router import Attachment

        mm = MediaManager(tmp_path)
        mm.download = AsyncMock()

        att = Attachment(type="image", file_path="/existing/img.png", filename="img.png")
        result = await mm.process_attachment(att)

        mm.download.assert_not_called()
        assert result.file_path == "/existing/img.png"

    @pytest.mark.asyncio
    async def test_process_data_url_image(self, tmp_path):
        """base64 data URL 图片应保存到本地。"""
        from kangclaw.gateway.media import MediaManager
        from kangclaw.gateway.router import Attachment
        import base64

        mm = MediaManager(tmp_path)

        b64 = base64.b64encode(b"\x89PNG fake").decode()
        data_url = f"data:image/png;base64,{b64}"
        att = Attachment(type="image", url=data_url, filename="screenshot.png")
        result = await mm.process_attachment(att)

        assert result.file_path  # 应有本地路径
        assert result.extra.get("data_url") == data_url  # 保留原始 data URL


# ─────────────────────────────────────────────
# tools/image_tools.py
# ─────────────────────────────────────────────

class TestImageTools:
    """图像处理工具测试。"""

    def _make_image(self, tmp_path, name="test.png", size=(100, 80), color="red"):
        from PIL import Image
        img = Image.new("RGB", size, color)
        path = tmp_path / name
        img.save(path)
        return str(path)

    def test_image_filter_grayscale(self, tmp_path):
        from kangclaw.tools.image_tools import image_filter
        from PIL import Image
        path = self._make_image(tmp_path)
        result = image_filter.invoke({"file_path": path, "filter_name": "grayscale"})
        assert "处理完成" in result
        # 提取路径并验证
        out_path = result.split("保存到: ")[1].split("\n")[0]
        img = Image.open(out_path)
        assert img.mode == "L"

    def test_image_filter_blur(self, tmp_path):
        from kangclaw.tools.image_tools import image_filter
        path = self._make_image(tmp_path)
        result = image_filter.invoke({"file_path": path, "filter_name": "blur"})
        assert "处理完成" in result
        assert "send_image" in result

    def test_image_filter_invalid(self, tmp_path):
        from kangclaw.tools.image_tools import image_filter
        path = self._make_image(tmp_path)
        result = image_filter.invoke({"file_path": path, "filter_name": "nonexistent"})
        assert "不支持" in result or "支持" in result

    def test_image_watermark(self, tmp_path):
        from kangclaw.tools.image_tools import image_watermark
        path = self._make_image(tmp_path, size=(200, 200))
        result = image_watermark.invoke({"file_path": path, "text": "KANGCLAW"})
        assert "处理完成" in result
        assert "send_image" in result

    def test_image_convert_to_jpg(self, tmp_path):
        from kangclaw.tools.image_tools import image_convert
        path = self._make_image(tmp_path)
        result = image_convert.invoke({"file_path": path, "target_format": "jpg"})
        assert "处理完成" in result
        out_path = result.split("保存到: ")[1].split("\n")[0]
        assert out_path.endswith(".jpg")
        assert Path(out_path).exists()

    def test_image_convert_invalid_format(self, tmp_path):
        from kangclaw.tools.image_tools import image_convert
        path = self._make_image(tmp_path)
        result = image_convert.invoke({"file_path": path, "target_format": "xyz"})
        assert "不支持" in result or "支持" in result

    def test_nonexistent_file(self, tmp_path):
        from kangclaw.tools.image_tools import image_filter
        result = image_filter.invoke({"file_path": "/nonexistent/img.png", "filter_name": "blur"})
        assert "不存在" in result


# ─────────────────────────────────────────────
# tools/send_image tool
# ─────────────────────────────────────────────

class TestSendImageTool:
    """send_image 工具测试。"""

    def test_send_image_no_channels(self, tmp_path):
        """未配置渠道时应返回错误。"""
        from kangclaw.tools import send_tools
        from PIL import Image
        img_path = tmp_path / "test.png"
        Image.new("RGB", (10, 10), "red").save(img_path)
        send_tools.configure({}, {})
        result = send_tools.send_image.invoke({
            "file_path": str(img_path),
            "channel": "dingtalk",
            "session_id": "dingtalk-u_123",
        })
        assert "未找到" in result or "不支持" in result

    def test_send_image_file_not_exist(self):
        from kangclaw.tools import send_tools
        send_tools.configure({}, {})
        result = send_tools.send_image.invoke({
            "file_path": "/nonexistent/img.png",
            "channel": "web",
            "session_id": "web-default",
        })
        assert "不存在" in result

    def test_send_image_web_channel(self, tmp_path):
        """Web 渠道应通过 WebSocket 发送 base64。"""
        from kangclaw.tools import send_tools
        import base64

        # 创建测试图片
        from PIL import Image
        img_path = tmp_path / "test.png"
        Image.new("RGB", (10, 10), "red").save(img_path)

        mock_ws = MagicMock()
        ws_connections = {"web-default": {mock_ws}}
        send_tools.configure({}, ws_connections)

        result = send_tools.send_image.invoke({
            "file_path": str(img_path),
            "channel": "web",
            "session_id": "web-default",
        })
        assert "已发送" in result

    def test_send_image_registered_in_all_tools(self):
        from kangclaw.tools import ALL_TOOLS
        names = [t.name for t in ALL_TOOLS]
        assert "send_image" in names


class TestMessageQueue:
    """Agent 消息队列测试。"""

    def setup_method(self):
        from kangclaw.config import AppConfig, ModelConfig
        from kangclaw.gateway.memory import MemoryManager
        from kangclaw.gateway.agent import Agent
        self.workspace = Path(tempfile.mkdtemp())
        self.memory = MemoryManager(self.workspace)
        self.config = AppConfig(
            models=[ModelConfig(primary_key="test", id="test-model", provider="openai")],
        )
        self.config.agent.model_primary_key = "test"
        self.agent = Agent(self.config, self.memory)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.workspace, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_concurrent_message_queued(self):
        """第二条消息应排队而非被拒绝，两条都被处理。"""
        call_count = 0

        async def mock_process_inner(session_id, user_content, channel="web", metadata=None, attachments=None):
            nonlocal call_count
            call_count += 1
            if user_content == "first":
                await asyncio.sleep(0.3)
                yield "reply-to-first"
            else:
                yield "reply-to-second"

        with patch.object(self.agent, '_process_inner', side_effect=mock_process_inner):
            session_id = "test-session"
            self.memory.get_or_create_session(session_id, channel="web")

            async def collect_tokens(content):
                tokens = []
                async for token in self.agent.process(session_id, content):
                    tokens.append(token)
                return tokens

            results = await asyncio.gather(
                collect_tokens("first"),
                collect_tokens("second"),
            )

            # 第一条消息应正常处理
            assert "reply-to-first" in results[0]
            # 第二条消息应静默等待后收到实际回复（无排队通知）
            assert "reply-to-second" in results[1]
            assert not any("排队" in t for t in results[1])
            # 两条消息都应被处理
            assert call_count == 2

    @pytest.mark.asyncio
    async def test_stop_clears_queue(self):
        """取消操作应清空消息队列。"""
        async def slow_process_inner(session_id, user_content, channel="web", metadata=None, attachments=None):
            await asyncio.sleep(1.0)
            yield "should-not-finish"

        with patch.object(self.agent, '_process_inner', side_effect=slow_process_inner):
            session_id = "test-session"
            self.memory.get_or_create_session(session_id, channel="web")

            async def send_first():
                tokens = []
                async for token in self.agent.process(session_id, "first"):
                    tokens.append(token)
                return tokens

            async def send_second_then_cancel():
                await asyncio.sleep(0.05)
                tokens = []
                async for token in self.agent.process(session_id, "second"):
                    tokens.append(token)
                await asyncio.sleep(0.05)
                self.agent.request_cancel(session_id)
                return tokens

            await asyncio.gather(send_first(), send_second_then_cancel())

            queue = self.agent._session_queues.get(session_id)
            assert queue is None or queue.empty()

    @pytest.mark.asyncio
    async def test_queue_full_rejects(self):
        """队列满时应拒绝新消息。"""
        async def slow_process_inner(session_id, user_content, channel="web", metadata=None, attachments=None):
            await asyncio.sleep(2.0)
            yield "slow"

        with patch.object(self.agent, '_process_inner', side_effect=slow_process_inner):
            session_id = "test-session"
            self.memory.get_or_create_session(session_id, channel="web")

            # 先启动一个慢任务占住锁
            task = asyncio.create_task(self._consume(session_id, "blocker"))
            await asyncio.sleep(0.05)

            # 填满队列 (maxsize=10)
            waiters = []
            for i in range(10):
                waiters.append(asyncio.create_task(self._consume(session_id, f"msg-{i}")))
            await asyncio.sleep(0.05)

            # 第 11 条应被拒绝
            tokens = []
            async for t in self.agent.process(session_id, "overflow"):
                tokens.append(t)
            assert any("队列已满" in t for t in tokens)

            # 清理
            self.agent.request_cancel(session_id)
            await asyncio.gather(task, *waiters, return_exceptions=True)

    async def _consume(self, session_id, content):
        tokens = []
        async for t in self.agent.process(session_id, content):
            tokens.append(t)
        return tokens
