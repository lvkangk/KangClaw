"""记忆系统：Session 管理、.jsonl 读写、MEMORY.md 操作。"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from dataclasses import dataclass, field, asdict

logger = logging.getLogger("kangclaw.memory")


def _gen_tool_call_id() -> str:
    """生成符合 ^[a-zA-Z0-9_-]+$ 的 tool_call_id。"""
    return f"call_{uuid.uuid4().hex[:12]}"


@dataclass
class Message:
    role: str  # user / assistant / tool / system
    content: str
    ts: float = field(default_factory=time.time)
    name: str | None = None  # tool name (仅 role=tool 时有值)
    tool_call_id: str | None = None  # tool_call_id (role=tool 时必须)
    tool_calls: list[dict] | None = None  # assistant 的 tool_calls 列表

    def to_dict(self) -> dict:
        d = {"role": self.role, "content": self.content, "ts": self.ts}
        if self.name:
            d["name"] = self.name
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        return d

    def to_langchain(self):
        from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
        if self.role == "user":
            return HumanMessage(content=self.content)
        elif self.role == "assistant":
            if self.tool_calls:
                return AIMessage(content=self.content, tool_calls=self.tool_calls)
            return AIMessage(content=self.content)
        elif self.role == "system":
            return SystemMessage(content=self.content)
        elif self.role == "tool":
            tid = self.tool_call_id or _gen_tool_call_id()
            return ToolMessage(content=self.content, name=self.name or "unknown", tool_call_id=tid)
        return HumanMessage(content=self.content)


@dataclass
class SessionMeta:
    session_id: str
    channel: str
    type: str  # private / web / cli
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class MemoryManager:
    """管理 session 和长期记忆。"""

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace)
        self.memory_dir = self.workspace / "memory"
        self.sessions_dir = self.memory_dir / "sessions"
        self.sessions_index = self.memory_dir / "sessions.json"
        self.memory_md = self.memory_dir / "MEMORY.md"
        self.history_md = self.memory_dir / "HISTORY.md"

        # 确保目录存在
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

        # 文件写入锁，防止并发写入交错
        self._write_lock = threading.Lock()

        # 加载 sessions 索引
        self._sessions: dict[str, SessionMeta] = {}
        self._load_sessions_index()

    def _load_sessions_index(self):
        if self.sessions_index.exists():
            try:
                data = json.loads(self.sessions_index.read_text(encoding="utf-8"))
                for item in data:
                    sid = item.get("sessionId", item.get("session_id", ""))
                    self._sessions[sid] = SessionMeta(
                        session_id=sid,
                        channel=item.get("channel", ""),
                        type=item.get("type", ""),
                        created_at=item.get("createdAt", item.get("created_at", 0)),
                        updated_at=item.get("updatedAt", item.get("updated_at", 0)),
                    )
            except (json.JSONDecodeError, KeyError):
                self._sessions = {}

    def _save_sessions_index(self):
        data = []
        for s in self._sessions.values():
            data.append({
                "sessionId": s.session_id,
                "channel": s.channel,
                "type": s.type,
                "createdAt": s.created_at,
                "updatedAt": s.updated_at,
                "sessionFile": str(self.sessions_dir / f"{s.session_id}.jsonl"),
            })
        self.sessions_index.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get_or_create_session(self, session_id: str, channel: str = "", type_: str = "") -> SessionMeta:
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionMeta(
                session_id=session_id,
                channel=channel,
                type=type_,
            )
            self._save_sessions_index()
        return self._sessions[session_id]

    def _session_file(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.jsonl"

    def append_message(self, session_id: str, msg: Message):
        """追加一条消息到 session 的 .jsonl 文件。"""
        with self._write_lock:
            f = self._session_file(session_id)
            with open(f, "a", encoding="utf-8") as fp:
                fp.write(json.dumps(msg.to_dict(), ensure_ascii=False) + "\n")

            # 更新 session 时间戳
            if session_id in self._sessions:
                self._sessions[session_id].updated_at = time.time()
                self._save_sessions_index()

    def load_history(self, session_id: str, limit: int = 20) -> list[Message]:
        """从 .jsonl 加载最近 N 条消息。

        截断时对齐到 user 消息边界，确保从完整的一轮对话开始，
        避免 ToolMessage 缺少对应的 AIMessage(tool_calls) 导致 API 400 错误。
        """
        f = self._session_file(session_id)
        if not f.exists():
            return []

        lines = f.read_text(encoding="utf-8").strip().split("\n")
        lines = [l for l in lines if l.strip()]

        all_messages = []
        for line in lines:
            try:
                d = json.loads(line)
                all_messages.append(Message(
                    role=d["role"],
                    content=d["content"],
                    ts=d.get("ts", 0),
                    name=d.get("name"),
                    tool_call_id=d.get("tool_call_id"),
                    tool_calls=d.get("tool_calls"),
                ))
            except (json.JSONDecodeError, KeyError):
                continue

        total = len(all_messages)
        if total <= limit:
            return all_messages

        # 从 limit 位置向前（历史方向）找到最近的 user 消息作为起点，保留更多历史
        start = total - limit
        while start > 0 and all_messages[start].role != "user":
            start -= 1

        return all_messages[start:]

    def load_all_history(self, session_id: str) -> list[Message]:
        """加载 session 的全部历史消息（用于配置了上下文窗口时，由压缩机制控制上限）。"""
        return self.read_all_messages(session_id)

    def is_session_interrupted(self, session_id: str) -> bool:
        """检查 session 是否因网关重启而中断（尾部有未完成的 tool_call）。

        检测逻辑：从尾部向前扫描，如果发现 assistant 消息带有 tool_calls，
        但后续的 tool result 数量不足，说明执行被中断。
        """
        f = self._session_file(session_id)
        if not f.exists():
            return False

        lines = f.read_text(encoding="utf-8").strip().split("\n")
        lines = [l for l in lines if l.strip()]
        if not lines:
            return False

        # 从尾部解析消息
        tail_messages = []
        for line in reversed(lines):
            try:
                d = json.loads(line)
                tail_messages.insert(0, d)
            except (json.JSONDecodeError, KeyError):
                continue
            # 找到最后一条 user 消息即可停止，只需检查最近一轮
            if d.get("role") == "user":
                break

        if not tail_messages:
            return False

        # 找最后一条带 tool_calls 的 assistant 消息
        last_assistant_idx = None
        expected_tool_ids = set()
        for i, msg in enumerate(tail_messages):
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                last_assistant_idx = i
                expected_tool_ids = {tc["id"] for tc in msg["tool_calls"]}

        if last_assistant_idx is None:
            return False

        # 统计该 assistant 之后实际返回的 tool result
        actual_tool_ids = set()
        for msg in tail_messages[last_assistant_idx + 1:]:
            if msg.get("role") == "tool" and msg.get("tool_call_id"):
                actual_tool_ids.add(msg["tool_call_id"])

        # 如果 tool result 不完整，说明被中断
        return actual_tool_ids < expected_tool_ids

    def reset_session(self, session_id: str) -> list[Message]:
        """重置 session（清空短期记忆），返回清空前的全部消息。"""
        messages = self.read_all_messages(session_id)
        f = self._session_file(session_id)
        if f.exists():
            f.write_text("", encoding="utf-8")
        return messages

    def read_all_messages(self, session_id: str) -> list[Message]:
        """读取 session 的全部消息。"""
        f = self._session_file(session_id)
        if not f.exists():
            return []
        lines = f.read_text(encoding="utf-8").strip().split("\n")
        messages = []
        for line in lines:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                messages.append(Message(
                    role=d["role"],
                    content=d["content"],
                    ts=d.get("ts", 0),
                    name=d.get("name"),
                    tool_call_id=d.get("tool_call_id"),
                    tool_calls=d.get("tool_calls"),
                ))
            except (json.JSONDecodeError, KeyError):
                continue
        return messages

    def rewrite_session(self, session_id: str, messages: list[Message]):
        """用新的消息列表覆写 session 的 JSONL 文件。"""
        with self._write_lock:
            f = self._session_file(session_id)
            with open(f, "w", encoding="utf-8") as fp:
                for msg in messages:
                    fp.write(json.dumps(msg.to_dict(), ensure_ascii=False) + "\n")

    def check_and_consolidate(self, session_id: str, keep_count: int = 50,
                              align_boundary: bool = True) -> list[Message] | None:
        """检查 session 是否需要整合。

        如果 JSONL 文件 >100KB 或消息条数 >200，返回需要整合的旧消息列表，
        并将 JSONL 覆写为仅保留最新 keep_count 条。
        align_boundary=True 时，分割点会对齐到 user 消息边界，避免切断对话轮次。
        否则返回 None。
        """
        f = self._session_file(session_id)
        if not f.exists():
            return None

        # 检查文件大小
        file_size = f.stat().st_size
        all_messages = self.read_all_messages(session_id)
        msg_count = len(all_messages)

        if file_size <= 100_000 and msg_count <= 200:
            return None

        logger.info(f"Session {session_id} 需要整合: {msg_count} 条消息, {file_size} 字节")

        # 分割：旧消息待整合，新消息保留
        if msg_count <= keep_count:
            return None

        split = msg_count - keep_count

        # 对齐到 user 消息边界，向前找以保留更多历史
        if align_boundary:
            while split > 0 and all_messages[split].role != "user":
                split -= 1

        old_messages = all_messages[:split]
        keep_messages = all_messages[split:]

        # 覆写 JSONL 只保留最新消息
        self.rewrite_session(session_id, keep_messages)

        return old_messages

    def append_history(self, events: list[str]):
        """追加事件到 HISTORY.md。"""
        if not events:
            return
        with self._write_lock:
            with open(self.history_md, "a", encoding="utf-8") as fp:
                for event in events:
                    fp.write(event.rstrip("\n") + "\n")

    def rewrite_memory_md(self, content: str):
        """全量覆写 MEMORY.md。"""
        with self._write_lock:
            self.memory_md.write_text(content, encoding="utf-8")

    def load_memory_md(self) -> str:
        """读取 MEMORY.md 长期记忆。"""
        if self.memory_md.exists():
            return self.memory_md.read_text(encoding="utf-8")
        return ""

    def load_system_context(self, channel: str = "") -> str:
        """加载系统上下文：AGENTS.md + SOUL.md + USER.md + MEMORY.md。

        群聊渠道（session_id 含 '-g_'）不注入 MEMORY.md，保护隐私。
        """
        parts = []
        for name in ["AGENTS.md", "SOUL.md", "USER.md"]:
            p = self.workspace / name
            if p.exists():
                parts.append(p.read_text(encoding="utf-8"))

        # 非群聊才注入 MEMORY.md
        if channel != "group":
            memory = self.load_memory_md()
            if memory.strip():
                parts.append(memory)

        return "\n\n---\n\n".join(parts)
