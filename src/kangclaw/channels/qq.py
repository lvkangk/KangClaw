"""QQ 渠道：基于 botpy 1.2.1 SDK（QQ 群 + C2C 私聊）。"""

from __future__ import annotations

import asyncio
import logging

from kangclaw.channels.base import BaseChannel
from kangclaw.config import ChannelConfig
from kangclaw.gateway.router import Attachment, IncomingMessage

logger = logging.getLogger("kangclaw.qq")


class QQChannel(BaseChannel):
    """QQ Bot 渠道实现。"""

    def __init__(self, config: ChannelConfig, router, media_manager=None):
        super().__init__(config, router, media_manager)
        self.app_id = config.extra.get("app_id", "")
        self.app_secret = config.extra.get("app_secret", "")
        self.allow_from = config.extra.get("allow_from", [])
        self._bot = None
        self._task: asyncio.Task | None = None
        # msg_seq 计数器：同一 msg_id 多次回复时递增，避免被去重
        self._msg_seq_counter: dict[str, int] = {}

    async def start(self) -> None:
        """启动 QQ Bot 监听。"""
        try:
            import botpy
            from botpy.message import GroupMessage, C2CMessage
        except ImportError:
            logger.error("qq-botpy 未安装，请运行: pip install qq-botpy")
            return

        channel = self

        class MyClient(botpy.Client):
            async def on_group_at_message_create(self, message: GroupMessage):
                """群内 @机器人 消息。"""
                await channel._handle_group_message(message)

            async def on_c2c_message_create(self, message: C2CMessage):
                """C2C 单聊消息。"""
                await channel._handle_c2c_message(message)

            async def on_group_add_robot(self, event):
                """机器人被添加到群。"""
                logger.info(f"[QQ] 机器人被添加到群: {event.group_openid}")

            async def on_friend_add(self, event):
                """用户添加机器人为好友。"""
                logger.info(f"[QQ] 新好友: {event.user_openid}")

        intents = botpy.Intents(public_messages=True)
        self._bot = MyClient(intents=intents, ext_handlers=False)

        async def _run_bot():
            try:
                await self._bot.start(appid=self.app_id, secret=self.app_secret)
            except Exception as e:
                logger.error(f"QQ Bot 启动失败: {e}")

        self._task = asyncio.create_task(_run_bot())
        logger.info("QQ 渠道已启动")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._bot:
            try:
                await self._bot.close()
            except Exception:
                pass

    async def send(self, session_id: str, content: str, **kwargs) -> None:
        logger.info(f"QQ send to {session_id}: {content[:50]}...")

    async def send_image(self, session_id: str, file_path: str, chat_id: str = "") -> str:
        """发送图片到 QQ（需要公网可访问的 URL，暂不支持本地文件直接发送）。"""
        # QQ botpy 的 post_group_file/post_c2c_file 需要公网 URL
        # 本地文件无法直接发送，返回提示
        return f"QQ 渠道暂不支持直接发送本地图片，文件已保存在: {file_path}"

    def _check_allow(self, user_id: str) -> bool:
        if not self.allow_from or self.allow_from == ["*"]:
            return True
        return user_id in self.allow_from

    @staticmethod
    def _parse_attachment(att) -> Attachment:
        """将 botpy 附件对象转为统一 Attachment。"""
        content_type = (att.content_type or "").lower()
        url = att.url or ""
        filename = att.filename or ""

        if content_type.startswith("image/"):
            media_type = "image"
        elif content_type.startswith("video/"):
            media_type = "video"
        elif content_type.startswith("audio/"):
            media_type = "audio"
        else:
            media_type = "file"

        return Attachment(type=media_type, url=url, filename=filename, mime_type=content_type)

    def _next_msg_seq(self, msg_id: str) -> int:
        """获取下一个 msg_seq，避免同 msg_id 回复被去重。"""
        seq = self._msg_seq_counter.get(msg_id, 0) + 1
        self._msg_seq_counter[msg_id] = seq
        return seq

    async def _handle_group_message(self, message):
        """处理群 @机器人 消息。"""
        user_id = message.author.member_openid
        if not self._check_allow(user_id):
            return

        content = message.content.strip()
        session_id = f"qq-g_{message.group_openid}"
        logger.info(f"[QQ群] {session_id} from {user_id}: {content[:50]}")

        attachments = []
        has_unsupported = False
        if hasattr(message, 'attachments') and message.attachments:
            for att in message.attachments:
                parsed = self._parse_attachment(att)
                if parsed.type in ("video", "audio"):
                    has_unsupported = True
                else:
                    attachments.append(parsed)
        if has_unsupported and not content and not attachments:
            await self._send_reply(message, "暂不支持语音和视频消息，请发送文字、图片或文件。")
            return

        # 下载并转换附件
        if attachments and self.media_manager:
            for i, att in enumerate(attachments):
                attachments[i] = await self.media_manager.process_attachment(att, channel="qq")

        msg = IncomingMessage(
            channel="qq",
            session_id=session_id,
            user_id=user_id,
            content=content,
            attachments=attachments,
            metadata={"chat_type": "group", "chat_id": message.group_openid, "open_id": user_id},
        )

        await self._stream_reply(message, msg)

    async def _handle_c2c_message(self, message):
        """处理 C2C 单聊消息。"""
        user_id = message.author.user_openid
        if not self._check_allow(user_id):
            return

        content = message.content.strip()
        session_id = f"qq-u_{user_id}"
        logger.info(f"[QQ私聊] {session_id}: {content[:50]}")

        attachments = []
        has_unsupported = False
        if hasattr(message, 'attachments') and message.attachments:
            for att in message.attachments:
                parsed = self._parse_attachment(att)
                if parsed.type in ("video", "audio"):
                    has_unsupported = True
                else:
                    attachments.append(parsed)
        if has_unsupported and not content and not attachments:
            await self._send_reply(message, "暂不支持语音和视频消息，请发送文字、图片或文件。")
            return

        # 下载并转换附件
        if attachments and self.media_manager:
            for i, att in enumerate(attachments):
                attachments[i] = await self.media_manager.process_attachment(att, channel="qq")

        msg = IncomingMessage(
            channel="qq",
            session_id=session_id,
            user_id=user_id,
            content=content,
            attachments=attachments,
            metadata={"chat_type": "private", "chat_id": user_id, "open_id": user_id},
        )

        await self._stream_reply(message, msg)

    async def _stream_reply(self, message, msg: IncomingMessage) -> None:
        """流式处理并分段回复：思考内容 → 工具提示（可选） → 最终结果。"""
        pending = []

        async for token in self.router.handle(msg):
            stripped = token.strip()

            # 工具标记：先发积累的思考内容
            if stripped.startswith("[正在执行") and stripped.endswith("]"):
                text = "".join(pending).strip()
                if text:
                    await self._send_reply(message, text)
                    pending.clear()
                # 发送工具提示
                await self._send_reply(message, stripped)
                continue

            # 静默分段标记：只分段，不发提示
            if stripped == "[TOOL_BREAK]":
                text = "".join(pending).strip()
                if text:
                    await self._send_reply(message, text)
                    pending.clear()
                continue

            pending.append(token)

        # 发送最终结果
        final = "".join(pending).strip()
        if final:
            await self._send_reply(message, final)

    async def _send_reply(self, message, text: str) -> None:
        """发送一条回复消息。"""
        try:
            await message.reply(
                content=text,
                msg_type=0,
                event_id=message.event_id,
                msg_seq=self._next_msg_seq(message.id),
            )
        except Exception as e:
            logger.error(f"QQ 回复失败: {e}")
