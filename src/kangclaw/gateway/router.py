"""消息路由：接收各渠道消息 → 分发给 agent。"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import AsyncGenerator

logger = logging.getLogger("kangclaw.router")


@dataclass
class Attachment:
    """统一多媒体附件格式。"""
    type: str             # "image" | "audio" | "video" | "file"
    url: str = ""         # 远程 URL（下载链接）
    filename: str = ""    # 文件名
    file_path: str = ""   # 本地文件路径（下载后）
    mime_type: str = ""   # MIME 类型
    duration: int = 0     # 音频/视频时长（毫秒）
    extra: dict = None    # 平台特定元数据

    def __post_init__(self):
        if self.extra is None:
            self.extra = {}

    def to_dict(self) -> dict:
        return {k: v for k, v in {
            "type": self.type,
            "url": self.url,
            "filename": self.filename,
            "file_path": self.file_path,
            "mime_type": self.mime_type,
            "duration": self.duration,
        }.items() if v}


@dataclass
class IncomingMessage:
    """统一消息格式。"""
    channel: str
    session_id: str
    user_id: str
    content: str
    metadata: dict | None = None
    attachments: list[Attachment] = field(default_factory=list)


class Router:
    """消息路由器。"""

    def __init__(self, agent):
        from kangclaw.gateway.agent import Agent
        self.agent: Agent = agent

    async def handle(self, msg: IncomingMessage) -> AsyncGenerator[str, None]:
        """处理一条消息，返回流式 token 生成器。"""
        logger.info(f"[{msg.channel}] {msg.session_id}: {msg.content[:50]}...")

        # 确保 session 存在
        self.agent.memory.get_or_create_session(
            msg.session_id, channel=msg.channel, type_=msg.channel
        )

        # 特殊指令
        if msg.content.strip() == "/stop":
            cancelled = self.agent.request_cancel(msg.session_id)
            if not cancelled:
                yield "[当前没有正在执行的任务]"
            return

        if msg.content.strip() in ("/new", "/reset"):
            # 整合旧消息，再清空
            old_messages = self.agent.memory.reset_session(msg.session_id)
            if old_messages:
                asyncio.create_task(self.agent._safe_consolidate(msg.session_id, old_messages))

            # 让 AI 主动打招呼
            if self.agent.config.agent.auto_greeting:
                greeting_prompt = self._build_greeting_prompt()
                async for token in self.agent.process(
                    session_id=msg.session_id,
                    user_content=greeting_prompt,
                    channel=msg.channel,
                    metadata=msg.metadata,
                ):
                    yield token
            return

        if msg.content.strip() == "/greeting":
            greeting_prompt = self._build_greeting_prompt()
            async for token in self.agent.process(
                session_id=msg.session_id,
                user_content=greeting_prompt,
                channel=msg.channel,
                metadata=msg.metadata,
            ):
                yield token
            return

        # 调用 agent 处理
        async for token in self.agent.process(
            session_id=msg.session_id,
            user_content=msg.content,
            channel=msg.channel,
            metadata=msg.metadata,
            attachments=msg.attachments,
        ):
            yield token

    def _build_greeting_prompt(self) -> str:
        """根据 SOUL.md 是否已定义，生成不同的打招呼提示词。"""
        soul_file = self.agent.memory.workspace / "SOUL.md"
        soul_defined = False
        if soul_file.exists():
            content = soul_file.read_text(encoding="utf-8")
            # 匹配 "## 名字" 后面的非空、非标题行
            name_match = re.search(r"## 名字\s*\n+([^#\n].+)", content)
            personality_match = re.search(r"## 性格\s*\n+([^#\n].+)", content)
            if (name_match and name_match.group(1).strip()
                    and personality_match and personality_match.group(1).strip()):
                soul_defined = True

        if soul_defined:
            return "[系统] 用户开始了新对话，请主动跟用户打个招呼。"
        else:
            return (
                "[系统] 用户开始了新对话。检测到你的人设（SOUL.md）尚未定义。"
                "请在打招呼的同时，询问用户希望你叫什么名字，以及偏好的性格和说话风格。"
                "给出几个不同的选项供用户选择，例如：\n"
                "- 名字：小虾、阿Kang、小爪、或自定义\n"
                "- 性格：干脆利落型 / 温柔贴心型 / 毒舌吐槽型 / 冷静理性型\n"
                "- 说话风格：简洁直接 / 轻松幽默 / 正式专业 / 可爱卖萌\n"
                "用户选择后，用 edit_file 工具更新 SOUL.md。"
            )
