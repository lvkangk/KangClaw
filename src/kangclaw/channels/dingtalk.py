"""钉钉渠道：基于 dingtalk-stream 官方 SDK，使用 Stream Mode（WebSocket）接收事件。"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import TYPE_CHECKING

from kangclaw.channels.base import BaseChannel
from kangclaw.config import ChannelConfig
from kangclaw.gateway.router import Attachment, IncomingMessage

if TYPE_CHECKING:
    from kangclaw.gateway.router import Router

logger = logging.getLogger("kangclaw.dingtalk")


class DingTalkChannel(BaseChannel):
    """钉钉 Bot 渠道实现（Stream Mode 长连接）。"""

    def __init__(self, config: ChannelConfig, router: Router, media_manager=None):
        super().__init__(config, router, media_manager)
        self.client_id = config.extra.get("client_id", "")
        self.client_secret = config.extra.get("client_secret", "")
        self.allow_from: list[str] = config.extra.get("allow_from", [])
        self._client = None
        self._thread: threading.Thread | None = None
        self._main_loop: asyncio.AbstractEventLoop | None = None
        # session_id -> conversation_id 映射，用于 send() 主动推送
        self._session_conversation_map: dict[str, str] = {}
        # 已处理的 message_id 去重缓存
        self._processed_msg_ids: set[str] = set()

    async def start(self) -> None:
        """启动钉钉渠道（Stream Mode，在独立线程中运行）。"""
        try:
            import dingtalk_stream  # noqa: F401
        except ImportError:
            logger.error("dingtalk-stream 未安装，请运行: pip install dingtalk-stream")
            return

        self._main_loop = asyncio.get_running_loop()
        self._client = self._build_client()

        self._thread = threading.Thread(
            target=self._run_stream,
            name="dingtalk-stream",
            daemon=True,
        )
        self._thread.start()
        logger.info("钉钉渠道已启动（Stream Mode）")

    def _build_client(self):
        """构建 DingTalkStreamClient。"""
        import dingtalk_stream

        channel = self

        class BotHandler(dingtalk_stream.ChatbotHandler):
            def __init__(self):
                super(dingtalk_stream.ChatbotHandler, self).__init__()
                self.logger = logger

            async def process(self, callback: dingtalk_stream.CallbackMessage):
                incoming_message = dingtalk_stream.ChatbotMessage.from_dict(callback.data)

                if channel._main_loop is not None:
                    asyncio.run_coroutine_threadsafe(
                        channel._handle_message(incoming_message, raw_data=callback.data),
                        channel._main_loop,
                    )

                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

        credential = dingtalk_stream.Credential(self.client_id, self.client_secret)
        client = dingtalk_stream.DingTalkStreamClient(credential)
        client.register_callback_handler(
            dingtalk_stream.chatbot.ChatbotMessage.TOPIC,
            BotHandler(),
        )
        return client

    def _run_stream(self):
        """在独立线程中运行 Stream 长连接。"""
        try:
            self._client.start_forever()
        except Exception as e:
            logger.error(f"[钉钉] Stream 长连接异常退出: {e}", exc_info=True)

    async def stop(self) -> None:
        self._client = None
        self._main_loop = None

    async def send(self, session_id: str, content: str, **kwargs) -> None:
        """主动发送消息（用于 cron/心跳推送）。"""
        if not self._client:
            logger.warning("[钉钉] 客户端未初始化，无法发送消息")
            return
        logger.info(f"[钉钉] 主动推送到 {session_id}: {content[:50]}...")

    async def send_image(self, session_id: str, file_path: str, chat_id: str = "") -> str:
        """发送图片到钉钉（通过 OpenAPI）。"""
        if not self._client:
            return "钉钉客户端未初始化"

        import base64
        import mimetypes
        from pathlib import Path

        p = Path(file_path)
        if not p.exists():
            return f"文件不存在: {file_path}"

        try:
            # 读取图片并上传获取 mediaId
            import dingtalk_stream
            handler = dingtalk_stream.ChatbotHandler()
            handler.dingtalk_client = self._client

            data = p.read_bytes()
            mime = mimetypes.guess_type(str(p))[0] or "image/png"
            media_id = handler.upload_to_dingtalk(data, filetype="image", filename=p.name, mimetype=mime)

            if not media_id:
                return "图片上传失败"

            # 判断群聊还是私聊
            conversation_id = chat_id or self._session_conversation_map.get(session_id)

            if session_id.startswith("dingtalk-g_") and conversation_id:
                # 群聊：通过 robot API 发送
                import requests
                token = self._client.get_access_token()
                resp = requests.post(
                    "https://api.dingtalk.com/v1.0/robot/groupMessages/send",
                    headers={"x-acs-dingtalk-access-token": token, "Content-Type": "application/json"},
                    json={
                        "robotCode": self.client_id,
                        "openConversationId": conversation_id,
                        "msgKey": "sampleImageMsg",
                        "msgParam": json.dumps({"photoURL": f"dingtalk://dingtalkclient/media?mediaId={media_id}"}),
                    },
                )
                if resp.status_code == 200:
                    return f"已发送图片 {p.name}"
                else:
                    return f"发送失败: {resp.text}"
            else:
                # 私聊：通过 batch send API
                user_id = session_id.replace("dingtalk-u_", "")
                import requests
                token = self._client.get_access_token()
                resp = requests.post(
                    "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
                    headers={"x-acs-dingtalk-access-token": token, "Content-Type": "application/json"},
                    json={
                        "robotCode": self.client_id,
                        "userIds": [user_id],
                        "msgKey": "sampleImageMsg",
                        "msgParam": json.dumps({"photoURL": f"dingtalk://dingtalkclient/media?mediaId={media_id}"}),
                    },
                )
                if resp.status_code == 200:
                    return f"已发送图片 {p.name}"
                else:
                    return f"发送失败: {resp.text}"
        except Exception as e:
            logger.error(f"[钉钉] 发送图片失败: {e}", exc_info=True)
            return f"发送失败: {e}"

    def _check_allow(self, user_id: str) -> bool:
        if not self.allow_from or self.allow_from == ["*"]:
            return True
        return user_id in self.allow_from

    def _is_duplicate(self, message_id: str) -> bool:
        """检查消息是否重复。"""
        if message_id in self._processed_msg_ids:
            return True
        self._processed_msg_ids.add(message_id)
        if len(self._processed_msg_ids) > 10000:
            self._processed_msg_ids = set(list(self._processed_msg_ids)[-5000:])
        return False

    async def _handle_message(self, incoming_message, raw_data: dict | None = None) -> None:
        """处理消息：路由到 agent，分段回复。"""
        msg_id = incoming_message.message_id
        if self._is_duplicate(msg_id):
            logger.debug(f"[钉钉] 跳过重复消息: {msg_id}")
            return

        # 解析消息类型和内容
        message_type = getattr(incoming_message, 'message_type', 'text')
        text = ""
        attachments = []

        if message_type == "text":
            if incoming_message.text:
                text = incoming_message.text.content.strip()
        elif message_type == "picture":
            # 图片消息
            if incoming_message.image_content:
                download_code = incoming_message.image_content.download_code
                image_url = self._get_image_url(download_code)
                if image_url:
                    attachments.append(Attachment(
                        type="image",
                        url=image_url,
                        filename=f"image_{msg_id[:8]}.png",
                        extra={"download_code": download_code},
                    ))
            text = "[图片]"
        elif message_type == "richText":
            # 富文本消息
            text_parts = incoming_message.get_text_list() if hasattr(incoming_message, 'get_text_list') else []
            text = " ".join(str(t.get("text", "")) for t in text_parts).strip()
            image_codes = incoming_message.get_image_list() if hasattr(incoming_message, 'get_image_list') else []
            for code in image_codes:
                dc = code.get("downloadCode", code) if isinstance(code, dict) else code
                image_url = self._get_image_url(dc)
                if image_url:
                    attachments.append(Attachment(type="image", url=image_url, filename="richtext_image.png"))
        elif message_type == "audio":
            # 语音消息：从原始 callback data 的 content 中提取平台 ASR 转写
            recognition = ""
            if raw_data:
                content_data = raw_data.get("content", {})
                if isinstance(content_data, str):
                    try:
                        content_data = json.loads(content_data)
                    except (json.JSONDecodeError, TypeError):
                        content_data = {}
                recognition = content_data.get("recognition", "")
            if recognition:
                text = recognition
                logger.info(f"[钉钉] 语音转写: {recognition[:50]}")
            else:
                text = "[语音消息，未能识别内容]"
        elif message_type == "file":
            # 文件消息：从 raw_data 提取 downloadCode 和 fileName
            if raw_data:
                content_data = raw_data.get("content", {})
                if isinstance(content_data, str):
                    try:
                        content_data = json.loads(content_data)
                    except (json.JSONDecodeError, TypeError):
                        content_data = {}
                download_code = content_data.get("downloadCode", "")
                file_name = content_data.get("fileName", f"file_{msg_id[:8]}")
                if download_code:
                    file_url = self._get_image_url(download_code)
                    if file_url:
                        attachments.append(Attachment(
                            type="file",
                            url=file_url,
                            filename=file_name,
                            extra={"download_code": download_code},
                        ))
                text = f"[文件: {file_name}]"
            else:
                text = "[文件消息]"
        elif message_type == "video":
            self._reply_markdown("暂不支持视频消息，请发送文字、图片或文件。", incoming_message)
            return
        else:
            # 未知类型，尝试获取文本
            if hasattr(incoming_message, 'text') and incoming_message.text:
                text = incoming_message.text.content.strip()
            else:
                text = f"[{message_type} 消息]"

        if not text and not attachments:
            return

        conversation_type = incoming_message.conversation_type
        sender_staff_id = incoming_message.sender_staff_id or incoming_message.sender_id

        if not self._check_allow(sender_staff_id):
            return

        if conversation_type == "2":
            session_id = f"dingtalk-g_{incoming_message.conversation_id}"
        else:
            session_id = f"dingtalk-u_{sender_staff_id}"

        # 缓存 session -> conversation_id 映射
        if incoming_message.conversation_id:
            self._session_conversation_map[session_id] = incoming_message.conversation_id

        chat_type = "group" if conversation_type == "2" else "private"
        logger.info(f"[钉钉{('群' if chat_type == 'group' else '私聊')}] "
                     f"{session_id} from {sender_staff_id}: {text[:50]}")

        # 下载并转换附件
        if attachments and self.media_manager:
            for i, att in enumerate(attachments):
                attachments[i] = await self.media_manager.process_attachment(att, channel="dingtalk")

        msg = IncomingMessage(
            channel="dingtalk",
            session_id=session_id,
            user_id=sender_staff_id,
            content=text,
            attachments=attachments,
            metadata={
                "chat_type": chat_type,
                "conversation_id": incoming_message.conversation_id,
                "sender_staff_id": sender_staff_id,
            },
        )

        await self._stream_reply(incoming_message, msg)

    async def _stream_reply(self, incoming_message, msg: IncomingMessage) -> None:
        """流式处理并分段回复：类似 QQ 渠道模式。"""
        pending = []

        async for token in self.router.handle(msg):
            stripped = token.strip()

            # 工具标记：先发积累的内容
            if stripped.startswith("[正在执行") and stripped.endswith("]"):
                text = "".join(pending).strip()
                if text:
                    self._reply_markdown(text, incoming_message)
                    pending.clear()
                self._reply_markdown(stripped, incoming_message)
                continue

            # 静默分段标记
            if stripped == "[TOOL_BREAK]":
                text = "".join(pending).strip()
                if text:
                    self._reply_markdown(text, incoming_message)
                    pending.clear()
                continue

            pending.append(token)

        final = "".join(pending).strip()
        if final:
            self._reply_markdown(final, incoming_message)

    def _get_image_url(self, download_code: str) -> str:
        """获取图片下载 URL（通过 DingTalk API）。"""
        try:
            import dingtalk_stream
            handler = dingtalk_stream.ChatbotHandler()
            handler.dingtalk_client = self._client
            return handler.get_image_download_url(download_code)
        except Exception as e:
            logger.error(f"[钉钉] 获取图片 URL 失败: {e}")
            return ""

    def _reply_markdown(self, text: str, incoming_message) -> None:
        """发送 Markdown 回复。"""
        try:
            import dingtalk_stream
            handler = dingtalk_stream.ChatbotHandler()
            handler.reply_markdown("回复", text, incoming_message)
        except Exception as e:
            logger.error(f"[钉钉] 回复失败: {e}")
