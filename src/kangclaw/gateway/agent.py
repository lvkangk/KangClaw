"""Agent 核心：LangChain 模型调用、工具绑定、上下文组装。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncGenerator

from langchain_core.messages import (
    HumanMessage, AIMessage, AIMessageChunk,
    SystemMessage, ToolMessage,
)

from kangclaw.config import AppConfig, ModelConfig, PROVIDERS, get_active_model
from kangclaw.gateway.memory import MemoryManager, Message, _gen_tool_call_id
from kangclaw.gateway.tokens import count_message_tokens
from kangclaw.tools import ALL_TOOLS

logger = logging.getLogger("kangclaw.agent")


# ── Patch: 让 LangChain 序列化 assistant 消息时自动补上 reasoning_content ──
# LangChain ChatOpenAI 不保留第三方扩展字段，但部分开启 thinking 的模型
# 要求所有 assistant 消息必须带 reasoning_content，否则返回 400。
def _patch_convert_message_to_dict():
    import langchain_openai.chat_models.base as _base
    _orig = _base._convert_message_to_dict

    def _patched(message, api="chat/completions"):
        d = _orig(message, api)
        if d.get("role") == "assistant":
            d.setdefault("reasoning_content", "")
            logger.debug("patched assistant msg: keys=%s", list(d.keys()))
        return d

    _base._convert_message_to_dict = _patched
    logger.info("_convert_message_to_dict patched successfully")

_patch_convert_message_to_dict()


def _build_user_content(text: str, attachments: list) -> str | list:
    """将用户文本和附件转换为 LangChain 消息内容。"""
    if not attachments:
        return text


    image_atts = [a for a in attachments if a.type == "image" and a.extra.get("data_url")]
    other_atts = [a for a in attachments if a not in image_atts]

    extra_parts = []
    for att in other_atts:
        if att.extra.get("transcription"):
            extra_parts.append(f"[语音转写 - {att.filename}]: {att.extra['transcription']}")
        elif att.extra.get("extracted_text"):
            extra_parts.append(f"[文件内容 - {att.filename}]:\n{att.extra['extracted_text']}")
        elif att.file_path:
            extra_parts.append(f"[附件: {att.filename} (类型: {att.type}), 本地路径: {att.file_path}]")
        elif att.url:
            extra_parts.append(f"[附件: {att.filename} (类型: {att.type}), URL: {att.url}]")

    if extra_parts:
        text = f"{text}\n" + "\n".join(extra_parts) if text else "\n".join(extra_parts)

    if not image_atts:
        return text

    content = [{"type": "text", "text": text}]
    for img in image_atts:
        content.append({"type": "image_url", "image_url": {"url": img.extra["data_url"]}})
        if img.file_path:
            content[0]["text"] += f"\n[图片本地路径: {img.file_path}]"
    return content


def create_chat_model(model: ModelConfig):
    """根据模型配置创建 LangChain ChatModel。"""
    provider = model.provider
    kwargs = {
        "streaming": True,
    }

    api_key = model.api_key or None
    base_url = model.base_url or PROVIDERS.get(provider, {}).get("default_base_url", "")

    if provider in ("openai", "deepSeek", "ollama", "modelScope", "kimi", "miniMax", "xiaomi", "otherOpenai"):
        from langchain_openai import ChatOpenAI
        extra = {}
        if base_url:
            extra["base_url"] = base_url
        if api_key:
            extra["api_key"] = api_key
        return ChatOpenAI(model=model.id, **kwargs, **extra)

    elif provider in ("anthropic", "otherAnthropic"):
        from langchain_anthropic import ChatAnthropic
        extra = {}
        if api_key:
            extra["api_key"] = api_key
        if base_url:
            extra["base_url"] = base_url
        return ChatAnthropic(model=model.id, **kwargs, **extra)

    elif provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        extra = {}
        if api_key:
            extra["google_api_key"] = api_key
        return ChatGoogleGenerativeAI(model=model.id, **kwargs, **extra)

    else:
        # 默认尝试 OpenAI 兼容接口
        from langchain_openai import ChatOpenAI
        extra = {}
        if base_url:
            extra["base_url"] = base_url
        if api_key:
            extra["api_key"] = api_key
        return ChatOpenAI(model=model.id, **kwargs, **extra)


class Agent:
    """自行实现的调用循环，不使用 LangChain AgentExecutor。"""

    def __init__(self, config: AppConfig, memory: MemoryManager, skills_summary: str = ""):
        self.config = config
        self.memory = memory
        self.skills_summary = skills_summary
        active_model = get_active_model(config)
        if active_model is None:
            raise ValueError("没有配置任何模型，请先在配置文件中添加 [[model]]")
        self.active_model = active_model
        self.llm = create_chat_model(active_model)
        self.llm_with_tools = self.llm.bind_tools(ALL_TOOLS)
        self._tool_map = {t.name: t for t in ALL_TOOLS}
        # 每个 session 一把锁，防止同一会话并发调用 process()
        self._session_locks: dict[str, asyncio.Lock] = {}
        # 每个 session 的取消标志
        self._cancel_flags: dict[str, bool] = {}
        # 已检查过中断状态的 session，每个 session 只检查一次
        self._interrupted_checked: set[str] = set()
        # 每个 session 的消息队列
        self._session_queues: dict[str, asyncio.Queue] = {}

    def reload_model(self, config: AppConfig) -> None:
        """热更新模型：重新加载配置并重建 LLM。"""
        self.config = config
        active_model = get_active_model(config)
        if active_model is None:
            raise ValueError("没有配置任何模型")
        self.active_model = active_model
        self.llm = create_chat_model(active_model)
        self.llm_with_tools = self.llm.bind_tools(ALL_TOOLS)
        logger.info(f"模型已热更新: {active_model.show_name or active_model.id} ({active_model.provider})")

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
        return self._session_locks[session_id]

    def _get_session_queue(self, session_id: str) -> asyncio.Queue:
        if session_id not in self._session_queues:
            self._session_queues[session_id] = asyncio.Queue(maxsize=10)
        return self._session_queues[session_id]

    def request_cancel(self, session_id: str) -> bool:
        """请求取消指定 session 的执行，同时清空消息队列。返回是否有正在执行的任务。"""
        lock = self._session_locks.get(session_id)
        if lock and lock.locked():
            self._cancel_flags[session_id] = True
            # 清空队列，通知所有等待者
            queue = self._session_queues.get(session_id)
            if queue:
                while not queue.empty():
                    try:
                        _, _, _, _, token_sink = queue.get_nowait()
                        token_sink.put_nowait("[已取消]")
                        token_sink.put_nowait(None)
                    except Exception:
                        pass
            return True
        return False

    def _is_cancelled(self, session_id: str) -> bool:
        """检查并清除取消标志。"""
        if self._cancel_flags.get(session_id):
            self._cancel_flags.pop(session_id, None)
            return True
        return False

    async def shutdown(self, timeout: float = 3.0) -> None:
        """安全关闭：取消所有进行中的 session，清空队列，等待退出，写入中断标记。"""
        # 取消所有正在执行的 session
        active_sessions = []
        for session_id, lock in self._session_locks.items():
            if lock.locked():
                self._cancel_flags[session_id] = True
                active_sessions.append(session_id)

        # 清空所有队列
        for sid, queue in self._session_queues.items():
            while not queue.empty():
                try:
                    _, _, _, _, token_sink = queue.get_nowait()
                    token_sink.put_nowait("[网关关闭]")
                    token_sink.put_nowait(None)
                except Exception:
                    pass

        if not active_sessions:
            return

        logger.info(f"正在等待 {len(active_sessions)} 个会话退出...")

        # 等待锁释放（最多 timeout 秒）
        deadline = asyncio.get_event_loop().time() + timeout
        for session_id in active_sessions:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            lock = self._session_locks[session_id]
            try:
                await asyncio.wait_for(lock.acquire(), timeout=remaining)
                lock.release()
            except asyncio.TimeoutError:
                # 超时未退出，直接写入中断标记
                logger.warning(f"会话 {session_id} 未能在超时内退出，写入中断标记")
                self.memory.append_message(
                    session_id, Message(role="assistant", content="[网关关闭，执行被中断]")
                )

    async def process(
        self,
        session_id: str,
        user_content: str,
        channel: str = "web",
        metadata: dict | None = None,
        attachments: list | None = None,
    ) -> AsyncGenerator[str, None]:
        """处理用户消息，返回流式 token 生成器。如果会话忙则静默排队。"""
        lock = self._get_session_lock(session_id)

        if lock.locked():
            # 会话正忙 — 静默排队，阻塞等待处理
            queue = self._get_session_queue(session_id)
            token_sink: asyncio.Queue[str | None] = asyncio.Queue()
            try:
                queue.put_nowait((user_content, channel, metadata, attachments, token_sink))
            except asyncio.QueueFull:
                yield "[队列已满，请稍后再试]"
                return
            # 静默等待，直到轮到自己被处理后读取实际回复
            while True:
                token = await token_sink.get()
                if token is None:
                    break
                yield token
            return

        async with lock:
            async for token in self._process_inner(session_id, user_content, channel, metadata, attachments):
                yield token
            # 处理完后，消费队列中的排队消息
            await self._drain_queue(session_id)

    async def _drain_queue(self, session_id: str) -> None:
        """消费 session 队列中的排队消息（在持有锁的情况下调用）。"""
        queue = self._session_queues.get(session_id)
        if queue is None:
            return
        while not queue.empty():
            user_content, channel, metadata, attachments, token_sink = queue.get_nowait()
            try:
                async for token in self._process_inner(session_id, user_content, channel, metadata, attachments):
                    await token_sink.put(token)
            except Exception as e:
                await token_sink.put(f"[处理出错: {e}]")
            finally:
                await token_sink.put(None)

    async def _process_inner(
        self,
        session_id: str,
        user_content: str,
        channel: str = "web",
        metadata: dict | None = None,
        attachments: list | None = None,
    ) -> AsyncGenerator[str, None]:
        """实际的消息处理逻辑。"""
        # 检测上次执行是否被网关重启中断（每个 session 只检查一次）
        interrupted = False
        if session_id not in self._interrupted_checked:
            self._interrupted_checked.add(session_id)
            interrupted = self.memory.is_session_interrupted(session_id)

        # 记录用户消息
        self.memory.append_message(session_id, Message(role="user", content=user_content))

        # 如果上次被中断，在用户消息前注入系统提示
        if interrupted:
            self.memory.append_message(session_id, Message(
                role="user",
                content="[系统] 上一次执行被网关重启中断。请告知用户之前的任务未完成，询问是否继续，不要自行执行。",
            ))

        # 组装消息列表
        messages = self._build_messages(session_id, channel=channel, metadata=metadata)

        # 如果有多媒体附件，替换最后一条 user message 为 multimodal content
        if attachments:
            multimodal_content = _build_user_content(user_content, attachments)
            # 找到最后一条 HumanMessage 并替换其 content
            for i in range(len(messages) - 1, -1, -1):
                if isinstance(messages[i], HumanMessage):
                    messages[i] = HumanMessage(content=multimodal_content)
                    break

        # 调用循环
        max_iterations = self.config.agent.max_iterations
        initial_msg_count = len(messages)  # 记录初始消息数，压缩时只动这部分
        for _ in range(max_iterations):
            # 检查取消标志
            if self._is_cancelled(session_id):
                self.memory.append_message(session_id, Message(role="assistant", content="[用户中断了本次执行]"))
                yield "\n[已中断]"
                return

            # 检查并压缩上下文（只压缩初始历史部分，保护当前请求的工具循环消息）
            messages = await self._maybe_compress_context(
                messages, session_id=session_id, protect_tail=len(messages) - initial_msg_count
            )

            # 流式调用 — 使用 LangChain 1.x 推荐的 chunk 累积模式
            gathered: AIMessageChunk | None = None
            try:
                async for chunk in self.llm_with_tools.astream(messages):
                    if self._is_cancelled(session_id):
                        break
                    if isinstance(chunk, AIMessageChunk):
                        # 累积 chunk，自动合并 content 和 tool_call_chunks
                        gathered = chunk if gathered is None else gathered + chunk
                        # 实时推送文本 token
                        if chunk.content:
                            token = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
                            yield token
            except asyncio.CancelledError:
                self.memory.append_message(session_id, Message(
                    role="assistant", content="[网关关闭，执行被中断]"
                ))
                return

            if gathered is None:
                return

            # 从累积结果中提取完整 tool_calls
            if not gathered.tool_calls:
                # 无工具调用，结束
                full_content = gathered.content if isinstance(gathered.content, str) else str(gathered.content)
                self.memory.append_message(session_id, Message(role="assistant", content=full_content))
                # 检查是否需要整合
                await self._maybe_consolidate(session_id)
                return

            # 有工具调用 → 执行工具
            full_content = gathered.content if isinstance(gathered.content, str) else str(gathered.content or "")

            # 确保所有 tool_call ID 符合 ^[a-zA-Z0-9_-]+$
            ai_tool_calls = []
            for tc in gathered.tool_calls:
                tc_id = tc.get("id") or _gen_tool_call_id()
                ai_tool_calls.append({
                    "name": tc["name"],
                    "args": tc["args"],
                    "id": tc_id,
                })

            ai_msg = AIMessage(content=full_content, tool_calls=ai_tool_calls)
            messages.append(ai_msg)

            # 保存 assistant 消息（含 tool_calls 信息）
            self.memory.append_message(session_id, Message(
                role="assistant", content=full_content,
                tool_calls=ai_tool_calls,
            ))

            # 执行每个工具
            for tc in ai_tool_calls:
                # 工具执行前检查取消标志
                if self._is_cancelled(session_id):
                    self.memory.append_message(session_id, Message(role="assistant", content="[用户中断了本次执行]"))
                    yield "\n[已中断]"
                    return
                if self.config.agent.show_tool_calls:
                    yield f"\n[正在执行 {tc['name']}]\n"
                else:
                    # 不显示工具提示时，仍 yield 空标记作为分段信号
                    yield "\n[TOOL_BREAK]\n"
                tool_result = await self._execute_tool(tc["name"], tc["args"], channel=channel, session_id=session_id, metadata=metadata)
                messages.append(ToolMessage(
                    content=tool_result,
                    tool_call_id=tc["id"],
                ))
                self.memory.append_message(
                    session_id,
                    Message(role="tool", content=tool_result, name=tc["name"], tool_call_id=tc["id"]),
                )

            # 继续循环，让模型根据工具结果回复

        yield "\n[达到最大迭代次数]"

    def _build_messages(self, session_id: str, channel: str = "", metadata: dict | None = None) -> list:
        """组装上下文消息列表。"""
        import datetime

        metadata = metadata or {}
        messages = []

        # 从 metadata 获取聊天类型，兜底从 session_id 推断
        chat_type = metadata.get("chat_type", "group" if "-g_" in session_id else "private")
        open_id = metadata.get("open_id", "")
        is_group = chat_type == "group"
        ctx_channel = "group" if is_group else ""

        # System prompt
        system_parts = [self.memory.load_system_context(channel=ctx_channel)]

        # 注入运行时信息
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")
        system_parts.append(
            f"\n## 运行时信息\n\n"
            f"- 当前时间: {now}\n"
            f"- 消息渠道: {channel or 'unknown'}\n"
            f"- 聊天类型: {chat_type}\n"
            f"- open_id: {open_id}"
        )
        if self.skills_summary:
            system_parts.append(
                "\n## 可用技能\n\n"
                "以下技能提供了特定领域的操作指南。需要使用时，先用 read_file 读取详情文件获取具体用法，再按指南操作。\n\n"
                f"{self.skills_summary}"
            )
        # 告知工作区文件结构，帮助 LLM 使用正确的路径
        system_parts.append(
            "\n工作区文件结构（文件工具使用相对路径时基于此目录）：\n"
            "  AGENTS.md          — 行为指令\n"
            "  SOUL.md            — 人设定义\n"
            "  USER.md            — 用户画像\n"
            "  HEARTBEAT.md       — 心跳巡检任务\n"
            "  memory/MEMORY.md   — 长期记忆（修改记忆请使用此路径）\n"
            "  memory/HISTORY.md  — 时间线事件日志（只追加，不注入上下文，可用 grep 搜索）\n"
            "  memory/sessions/   — 会话历史\n"
            "  skills/            — 技能目录（下载或安装的技能必须放在此目录下）\n"
            "  cron/cron.json     — 定时任务\n"
            f"  media/             — 多媒体文件存储目录（按渠道分子目录，如 media/{channel}/）"
        )
        messages.append(SystemMessage(content="\n\n".join(system_parts)))

        # Session 历史
        session_meta = self.memory._sessions.get(session_id)
        is_task = session_meta and session_meta.type in ("cron", "heartbeat")
        if session_id == "system-heartbeat":
            history = self.memory.load_history(session_id, 8)
        elif self.active_model.context_window_tokens > 0 and not is_task:
            # 配置了上下文窗口时，加载全部历史，由 _maybe_compress_context 按 token 控制
            history = self.memory.load_all_history(session_id)
        else:
            history = self.memory.load_history(session_id, self.config.memory.max_history)
        for msg in history:
            messages.append(msg.to_langchain())

        return messages

    async def _compress_messages(self, messages: list) -> str:
        """用 LLM 将消息列表压缩为文本摘要。"""
        lines = []
        for msg in messages:
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            if not content.strip():
                continue
            if msg.type == "human":
                lines.append(f"用户: {content}")
            elif msg.type == "ai":
                if content:
                    lines.append(f"助手: {content}")
                if hasattr(msg, "tool_calls") and msg.tool_calls:
                    names = [tc.get("name", "?") for tc in msg.tool_calls]
                    lines.append(f"  [调用工具: {', '.join(names)}]")
            elif msg.type == "tool":
                name = getattr(msg, "name", "tool")
                truncated = content[:500] + "...(已截断)" if len(content) > 500 else content
                lines.append(f"  [工具结果 {name}]: {truncated}")

        conversation_text = "\n".join(lines)
        if len(conversation_text) > 12000:
            conversation_text = conversation_text[:12000] + "\n...(已截断)"

        prompt = (
            "请将以下对话记录压缩为一段简洁的摘要。要求：\n"
            "1. 保留关键信息：用户的意图、重要的决策、工具执行的结果和结论\n"
            "2. 省略冗余的中间过程和重复内容\n"
            "3. 保留具体的数据、文件名、路径等关键细节\n"
            "4. 用简洁的叙述体，不要用对话格式\n"
            "5. 控制在 300 字以内\n\n"
            f"对话记录：\n{conversation_text}"
        )

        try:
            response = await self.llm.ainvoke([HumanMessage(content=prompt)])
            summary = response.content if isinstance(response.content, str) else str(response.content)
            return summary.strip()
        except Exception as e:
            logger.error(f"上下文压缩 LLM 调用失败: {e}")
            return conversation_text[:800] + "\n...(压缩失败，已截断)"

    async def _maybe_compress_context(self, messages: list, session_id: str = "",
                                      protect_tail: int = 0) -> list:
        """检查 token 用量，超过阈值时压缩上下文并持久化。

        protect_tail: 尾部受保护的消息数（当前请求工具循环新增的），不参与压缩。
        """
        ctx_limit = self.active_model.context_window_tokens
        if ctx_limit <= 0:
            return messages

        # 心跳和定时任务不走压缩
        if session_id:
            session_meta = self.memory._sessions.get(session_id)
            if session_meta and session_meta.type in ("cron", "heartbeat"):
                return messages

        total_tokens = count_message_tokens(messages)
        threshold = int(ctx_limit * 0.6)
        logger.info(f"上下文 token 统计: {total_tokens}/{ctx_limit} ({len(messages)} 条消息, 阈值 {threshold})")

        if total_tokens < threshold:
            return messages

        logger.info(f"上下文压缩触发: {total_tokens} tokens >= {threshold} 阈值 (窗口: {ctx_limit})")

        # messages[0] 是 SystemMessage，不参与压缩
        # 尾部 protect_tail 条是当前请求工具循环新增的，也不压缩
        system_msg = messages[0]
        if protect_tail > 0:
            compressible = messages[1:-protect_tail]
            protected = messages[-protect_tail:]
        else:
            compressible = messages[1:]
            protected = []

        if len(compressible) <= 2:
            return messages

        # 从 compressible 尾部向前累积，确定保留 ~10% token 的"新鲜"部分
        fresh_token_target = int(ctx_limit * 0.10)
        fresh_start = len(compressible)
        fresh_tokens = 0
        for i in range(len(compressible) - 1, -1, -1):
            msg_tokens = count_message_tokens([compressible[i]])
            if fresh_tokens + msg_tokens > fresh_token_target and i < len(compressible) - 1:
                break
            fresh_tokens += msg_tokens
            fresh_start = i

        if fresh_start < 1:
            fresh_start = 1

        # 对齐到 user 消息边界，向前找以保留更多上下文
        while fresh_start > 1 and compressible[fresh_start].type != "human":
            fresh_start -= 1

        old_messages = compressible[:fresh_start]
        fresh_messages = compressible[fresh_start:]

        if not old_messages:
            return messages

        summary = await self._compress_messages(old_messages)
        summary_content = f"[系统] 以下是之前对话的压缩摘要：\n\n{summary}"

        summary_msg = HumanMessage(content=summary_content)
        compressed = [system_msg, summary_msg] + fresh_messages + protected

        new_total = count_message_tokens(compressed)
        logger.info(f"上下文已压缩: {total_tokens} -> {new_total} tokens "
                    f"({len(old_messages)} 条旧消息压缩, {len(fresh_messages)} 条新鲜历史保留, "
                    f"{len(protected)} 条当前任务消息受保护)")

        # 持久化：将压缩结果回写 session JSONL
        if session_id:
            # 构造新的 Message 列表：[摘要] + [新鲜消息] （不含 system_msg 和 protected）
            # protected 部分是当前请求工具循环中产生的，已经在 JSONL 中了
            persist_messages = [Message(role="user", content=summary_content)]
            for msg in fresh_messages:
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                if msg.type == "human":
                    persist_messages.append(Message(role="user", content=content))
                elif msg.type == "ai":
                    tc = msg.tool_calls if hasattr(msg, "tool_calls") else None
                    persist_messages.append(Message(role="assistant", content=content, tool_calls=tc or None))
                elif msg.type == "tool":
                    persist_messages.append(Message(
                        role="tool", content=content,
                        name=getattr(msg, "name", None),
                        tool_call_id=getattr(msg, "tool_call_id", None),
                    ))
            self.memory.rewrite_session(session_id, persist_messages)
            logger.info(f"压缩结果已持久化到 session {session_id}")

            # 后台触发记忆整合（提取 facts/events 到 MEMORY.md/HISTORY.md）
            asyncio.create_task(self._safe_consolidate(session_id, [
                Message(
                    role="user" if m.type == "human" else ("assistant" if m.type == "ai" else m.type),
                    content=m.content if isinstance(m.content, str) else str(m.content),
                ) for m in old_messages
                if m.type in ("human", "ai") and (m.content if isinstance(m.content, str) else str(m.content)).strip()
            ]))

        return compressed

    async def _maybe_consolidate(self, session_id: str):
        """检查并异步执行整合（不阻塞响应）。"""
        # 心跳 session：超过 100 条直接截断，不走 LLM 整合
        if session_id == "system-heartbeat":
            all_msgs = self.memory.read_all_messages(session_id)
            if len(all_msgs) > 100:
                self.memory.rewrite_session(session_id, all_msgs[-8:])
                logger.info(f"心跳 session 已截断: {len(all_msgs)} -> 8 条")
            return

        keep = self.config.memory.session_load_limit
        # 定时任务 session 没有真正的 user 消息，不需要对齐对话边界
        session_meta = self.memory._sessions.get(session_id)
        is_task = session_meta and session_meta.type in ("cron", "heartbeat")
        old_messages = self.memory.check_and_consolidate(
            session_id, keep_count=keep, align_boundary=not is_task
        )
        if old_messages:
            # 后台执行整合，不阻塞当前响应
            asyncio.create_task(self._safe_consolidate(session_id, old_messages))

    async def _safe_consolidate(self, session_id: str, messages: list[Message]):
        """安全执行整合，捕获异常。"""
        try:
            await self.consolidate(session_id, messages)
        except Exception as e:
            logger.error(f"后台整合失败 [{session_id}]: {e}")

    async def _execute_tool(self, name: str, args: dict, channel: str = "web", session_id: str = "", metadata: dict | None = None) -> str:
        """执行指定工具。"""
        tool_fn = self._tool_map.get(name)
        if not tool_fn:
            return f"错误：未知工具 {name}"
        metadata = metadata or {}
        # 自动注入当前 channel、session_id 和 chat_id 到 cron_add
        if name == "cron_add":
            args.setdefault("channel", channel)
            args.setdefault("session_id", session_id)
            args.setdefault("chat_id", metadata.get("chat_id", ""))
        # 自动注入到 send_image
        if name == "send_image":
            args.setdefault("channel", channel)
            args.setdefault("session_id", session_id)
            args.setdefault("chat_id", metadata.get("chat_id", ""))
        try:
            result = await asyncio.to_thread(tool_fn.invoke, args)
            return str(result)
        except Exception as e:
            logger.error(f"工具 {name} 执行失败: {e}")
            return f"工具执行失败：{e}"

    async def consolidate(self, session_id: str, messages: list[Message]):
        """整合旧消息：提取长期事实和时间线事件。"""
        if not messages:
            return

        # 只取 user 和 assistant 的文本内容，跳过 tool 细节
        # 附带时间戳，让 LLM 能准确提取事件日期
        import datetime as _dt
        lines = []
        for msg in messages:
            ts_str = _dt.datetime.fromtimestamp(msg.ts).strftime("%Y-%m-%d %H:%M")
            if msg.role == "user":
                lines.append(f"[{ts_str}] 用户: {msg.content}")
            elif msg.role == "assistant" and msg.content and not msg.tool_calls:
                lines.append(f"[{ts_str}] 助手: {msg.content}")
        conversation_text = "\n".join(lines)

        if not conversation_text.strip():
            return

        # 截断过长的文本，避免 token 消耗过大
        if len(conversation_text) > 15000:
            conversation_text = conversation_text[:15000] + "\n... (已截断)"

        # 第一步：提取 facts 和 events
        extract_prompt = (
            "你是一个信息提取助手。请从以下对话记录中提取两类信息：\n\n"
            "1. **facts** — 长期事实：用户偏好、个人信息、决策结论、项目状态等值得长期记住的信息\n"
            "2. **events** — 时间线事件：发生了什么事、什么时候、结果如何\n\n"
            "要求：\n"
            "- 只提取有价值的信息，忽略闲聊和临时性内容\n"
            "- events 每条以日期开头，格式如 `[2026-03-30] 用户设置了每天8点的天气提醒`\n"
            "- 对话记录每行已标注时间戳，请直接使用该时间戳作为事件日期，不要猜测\n"
            "- 返回严格 JSON 格式，不要有其他内容\n"
            "- 如果没有值得保留的信息，对应数组为空\n\n"
            f"对话记录：\n{conversation_text}\n\n"
            '返回格式：{"facts": ["...", ...], "events": ["[日期] ...", ...]}'
        )

        try:
            from langchain_core.messages import HumanMessage as HM
            response = await self.llm.ainvoke([HM(content=extract_prompt)])
            content = response.content if isinstance(response.content, str) else str(response.content)

            # 解析 JSON（容忍 markdown 代码块包裹）
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            result = json.loads(content)
            facts = result.get("facts", [])
            events = result.get("events", [])
        except Exception as e:
            logger.error(f"整合提取失败: {e}")
            return

        # 第二步：如果有 facts，合并到 MEMORY.md
        if facts:
            try:
                existing_memory = self.memory.load_memory_md()
                new_facts_text = "\n".join(f"- {f}" for f in facts)

                merge_prompt = (
                    "你是一个记忆管理助手。请将以下新提取的信息与现有长期记忆合并，"
                    "去除重复和过时的内容，生成一份完整的长期记忆文档。\n\n"
                    "要求：\n"
                    "- 保持 Markdown 格式\n"
                    "- 分类组织（如有必要）\n"
                    "- 去重和合并相近的条目\n"
                    "- 只输出合并后的文档内容，不要有其他说明\n\n"
                    f"现有长期记忆：\n{existing_memory}\n\n"
                    f"新提取的信息：\n{new_facts_text}"
                )
                merge_response = await self.llm.ainvoke([HM(content=merge_prompt)])
                merged = merge_response.content if isinstance(merge_response.content, str) else str(merge_response.content)
                # 去除可能的 markdown 代码块包裹
                merged = merged.strip()
                if merged.startswith("```"):
                    merged = merged.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                self.memory.rewrite_memory_md(merged)
                logger.info(f"MEMORY.md 已更新，合并了 {len(facts)} 条新事实")
            except Exception as e:
                logger.error(f"MEMORY.md 合并失败: {e}")

        # 第三步：如果有 events，追加到 HISTORY.md
        if events:
            self.memory.append_history(events)
            logger.info(f"HISTORY.md 已追加 {len(events)} 条事件")
