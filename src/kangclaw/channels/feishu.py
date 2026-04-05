"""飞书渠道：基于 lark-oapi 官方 SDK，使用长连接（WebSocket）接收事件。"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import TYPE_CHECKING

from kangclaw.channels.base import BaseChannel
from kangclaw.config import ChannelConfig
from kangclaw.gateway.router import Attachment, IncomingMessage

if TYPE_CHECKING:
    from kangclaw.gateway.router import Router

logger = logging.getLogger("kangclaw.feishu")


class FeishuChannel(BaseChannel):
    """飞书 Bot 渠道实现（长连接模式）。"""

    def __init__(self, config: ChannelConfig, router: Router, media_manager=None):
        super().__init__(config, router, media_manager)
        self.app_id = config.extra.get("app_id", "")
        self.app_secret = config.extra.get("app_secret", "")
        self.allow_from: list[str] = config.extra.get("allow_from", [])
        self._client = None
        self._ws_client = None
        self._thread: threading.Thread | None = None
        # 主事件循环引用，用于从 ws 线程调度异步任务
        self._main_loop: asyncio.AbstractEventLoop | None = None
        # session_id -> chat_id 映射，用于 send() 主动推送
        self._session_chat_map: dict[str, str] = {}
        # session_id -> 正在流式更新的卡片 message_id，用于 shutdown 时更新
        self._active_cards: dict[str, str] = {}
        # 已处理的 message_id 去重缓存，防止飞书重连后重复投递
        self._processed_msg_ids: set[str] = set()

    def _build_api_client(self):
        """构建 lark-oapi API 客户端（用于发送/回复消息）。"""
        import lark_oapi as lark

        self._client = lark.Client.builder() \
            .app_id(self.app_id) \
            .app_secret(self.app_secret) \
            .log_level(lark.LogLevel.WARNING) \
            .build()

    def _build_ws_client(self):
        """构建 lark-oapi WebSocket 长连接客户端。"""
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

        channel = self

        def on_message_receive(data: P2ImMessageReceiveV1) -> None:
            """收到消息事件回调（在 ws 线程中被调用）。"""
            try:
                event = data.event
                message = event.message
                sender = event.sender

                # 按 message_id 去重，防止飞书重连后重复投递
                msg_id = message.message_id
                if msg_id in channel._processed_msg_ids:
                    logger.debug(f"[飞书] 跳过重复消息: {msg_id}")
                    return
                channel._processed_msg_ids.add(msg_id)
                # 限制缓存大小，避免内存无限增长
                if len(channel._processed_msg_ids) > 10000:
                    channel._processed_msg_ids = set(list(channel._processed_msg_ids)[-5000:])

                open_id = sender.sender_id.open_id
                if not channel._check_allow(open_id):
                    return

                # 解析消息内容和附件
                attachments_data = []  # list of dicts with attachment info
                msg_type = message.message_type

                if msg_type == "text":
                    try:
                        content_obj = json.loads(message.content)
                        text = content_obj.get("text", "").strip()
                    except (json.JSONDecodeError, AttributeError):
                        text = ""
                elif msg_type in ("image", "file"):
                    parsed = channel._parse_media_content(msg_type, message.content)
                    if parsed:
                        if msg_type == "image":
                            attachments_data.append({
                                "type": "image",
                                "key": parsed.get("image_key", ""),
                                "message_id": msg_id,
                            })
                            text = "[图片]"
                        elif msg_type == "file":
                            attachments_data.append({
                                "type": "file",
                                "key": parsed.get("file_key", ""),
                                "filename": parsed.get("file_name", "file"),
                                "message_id": msg_id,
                            })
                            text = f"[文件: {parsed.get('file_name', '')}]"
                    else:
                        text = ""
                elif msg_type in ("audio", "video", "media"):
                    logger.info(f"[飞书] 收到{msg_type}消息，回复不支持提示")
                    if channel._main_loop is not None:
                        asyncio.run_coroutine_threadsafe(
                            channel._handle_unsupported(session_id, chat_type, chat_id, message.message_id),
                            channel._main_loop,
                        )
                    return
                else:
                    logger.debug(f"[飞书] 忽略不支持的消息类型: {msg_type}")
                    return

                if not text:
                    return

                chat_type = message.chat_type
                chat_id = message.chat_id

                if chat_type == "group":
                    session_id = f"feishu-g_{chat_id}"
                else:
                    session_id = f"feishu-u_{open_id}"

                # 缓存 session -> chat_id 映射
                channel._session_chat_map[session_id] = chat_id

                logger.info(f"[飞书{('群' if chat_type == 'group' else '私聊')}] "
                            f"{session_id} from {open_id}: {text[:50]}")

                # 从 ws 线程调度到主事件循环处理
                if channel._main_loop is not None:
                    asyncio.run_coroutine_threadsafe(
                        channel._handle_message(
                            session_id, open_id, text, message.message_id,
                            chat_type, chat_id, attachments_data,
                        ),
                        channel._main_loop,
                    )

            except Exception as e:
                logger.error(f"[飞书] 消息处理异常: {e}", exc_info=True)

        event_handler = lark.EventDispatcherHandler.builder(
            encrypt_key="",
            verification_token="",
        ).register_p2_im_message_receive_v1(
            on_message_receive
        ).build()

        self._ws_client = lark.ws.Client(
            app_id=self.app_id,
            app_secret=self.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.DEBUG,
        )

    async def start(self) -> None:
        """启动飞书渠道（长连接模式，在独立线程中运行）。"""
        try:
            import lark_oapi  # noqa: F401
        except ImportError:
            logger.error("lark-oapi 未安装，请运行: pip install lark-oapi")
            return

        self._main_loop = asyncio.get_running_loop()
        self._build_api_client()
        self._build_ws_client()

        # ws.Client.start() 是阻塞的，需要在独立线程中运行
        self._thread = threading.Thread(
            target=self._run_ws,
            name="feishu-ws",
            daemon=True,
        )
        self._thread.start()
        logger.info("飞书渠道已启动（长连接模式）")

    def _run_ws(self):
        """在独立线程中运行 WebSocket 长连接（需要独立事件循环）。"""
        import lark_oapi.ws.client as ws_module

        # SDK 内部使用模块级 loop，必须替换为本线程的独立事件循环
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        ws_module.loop = new_loop

        try:
            self._ws_client.start()
        except Exception as e:
            logger.error(f"[飞书] WebSocket 长连接异常退出: {e}", exc_info=True)

    async def stop(self) -> None:
        self._client = None
        self._ws_client = None
        self._main_loop = None

    async def shutdown_notify(self) -> None:
        """网关关闭时，更新所有正在流式更新的卡片。"""
        for session_id, msg_id in list(self._active_cards.items()):
            try:
                await self._patch_card(msg_id, "⚠️ 网关已关闭", streaming=False)
            except Exception as e:
                logger.error(f"[飞书] shutdown 更新卡片失败 [{session_id}]: {e}")
        self._active_cards.clear()

    async def send(self, session_id: str, content: str, chat_id: str = "") -> None:
        """主动发送消息到飞书（用于 cron/心跳推送）。"""
        if not self._client:
            logger.warning("[飞书] 客户端未初始化，无法发送消息")
            return

        # 优先用传入的 chat_id，其次从缓存查找
        target_chat_id = chat_id or self._session_chat_map.get(session_id)
        if not target_chat_id:
            logger.warning(f"[飞书] 未找到 session {session_id} 对应的 chat_id，无法推送")
            return

        msg_id = await self._send_card(target_chat_id, content, streaming=False)
        if msg_id:
            logger.info(f"[飞书] 主动推送到 {session_id}: {content[:50]}...")

    async def send_image(self, session_id: str, file_path: str, chat_id: str = "") -> str:
        """发送图片到飞书。"""
        if not self._client:
            return "飞书客户端未初始化"

        from pathlib import Path
        p = Path(file_path)
        if not p.exists():
            return f"文件不存在: {file_path}"

        target_chat_id = chat_id or self._session_chat_map.get(session_id)
        if not target_chat_id:
            return f"未找到 session {session_id} 对应的 chat_id"

        try:
            from lark_oapi.api.im.v1 import (
                CreateImageRequest, CreateImageRequestBody,
                CreateMessageRequest, CreateMessageRequestBody,
            )

            # 上传图片
            body = CreateImageRequestBody.builder() \
                .image_type("message") \
                .image(open(file_path, "rb")) \
                .build()
            request = CreateImageRequest.builder().request_body(body).build()
            resp = await self._client.im.v1.image.acreate(request)
            if not resp.success():
                return f"图片上传失败: {resp.msg}"
            image_key = resp.data.image_key

            # 发送图片消息
            msg_body = CreateMessageRequestBody.builder() \
                .receive_id(target_chat_id) \
                .msg_type("image") \
                .content(json.dumps({"image_key": image_key})) \
                .build()
            msg_request = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(msg_body) \
                .build()
            msg_resp = await self._client.im.v1.message.acreate(msg_request)
            if msg_resp.success():
                return f"已发送图片 {p.name}"
            else:
                return f"发送失败: {msg_resp.msg}"
        except Exception as e:
            logger.error(f"[飞书] 发送图片失败: {e}", exc_info=True)
            return f"发送失败: {e}"

    def _check_allow(self, open_id: str) -> bool:
        if not self.allow_from or self.allow_from == ["*"]:
            return True
        return open_id in self.allow_from

    # 流式更新间隔（秒）
    STREAM_UPDATE_INTERVAL = 0.5

    @staticmethod
    def _parse_media_content(message_type: str, content_json: str) -> dict | None:
        """解析不同消息类型的 content JSON。"""
        SUPPORTED_TYPES = {"text", "image", "file"}
        if message_type not in SUPPORTED_TYPES:
            return None
        try:
            return json.loads(content_json)
        except (json.JSONDecodeError, TypeError):
            return None

    @staticmethod
    def _build_card(text: str, streaming: bool = False) -> str:
        """构建飞书交互卡片 JSON（支持 Markdown）。"""
        elements = [{"tag": "markdown", "content": text.lstrip("\n")}]
        if streaming:
            elements.append({"tag": "note", "elements": [
                {"tag": "plain_text", "content": "..."}
            ]})
        card = {
            "config": {"wide_screen_mode": True},
            "elements": elements,
        }
        return json.dumps(card)

    # 不需要发"思考中"卡片的命令
    _QUICK_COMMANDS = frozenset({"/stop", "/new", "/reset", "/greeting"})

    async def _handle_unsupported(self, session_id: str, chat_type: str, chat_id: str, message_id: str) -> None:
        """回复不支持的消息类型。"""
        tip = "暂不支持语音和视频消息，请发送文字、图片或文件。"
        if chat_type == "group":
            await self._reply_card(message_id, tip, streaming=False)
        else:
            await self._send_card(chat_id, tip, streaming=False)

    async def _download_resource(self, message_id: str, file_key: str,
                                  media_type: str, filename: str) -> str:
        """通过飞书 message_resource API 下载附件，返回本地路径。"""
        try:
            from lark_oapi.api.im.v1 import GetMessageResourceRequest

            resource_type = "image" if media_type == "image" else "file"
            request = GetMessageResourceRequest.builder() \
                .message_id(message_id) \
                .file_key(file_key) \
                .type(resource_type) \
                .build()

            response = await self._client.im.v1.message_resource.aget(request)
            if not response.success():
                logger.error(f"[飞书] 下载资源失败: code={response.code}, msg={response.msg}")
                return ""

            # response.file 是 BytesIO
            data = response.file.read()
            if not filename:
                filename = response.file_name or f"{media_type}_{file_key[:8]}"

            local_path = self.media_manager.save_bytes(data, filename, channel="feishu")
            logger.info(f"[飞书] 下载资源完成: {file_key} -> {local_path}")
            return local_path

        except Exception as e:
            logger.error(f"[飞书] 下载资源异常: {e}", exc_info=True)
            return ""

    async def _handle_message(
        self, session_id: str, user_id: str, content: str,
        message_id: str, chat_type: str, chat_id: str,
        attachments_data: list[dict] | None = None,
    ) -> None:
        """处理消息：路由到 agent，流式更新卡片回复。"""
        is_group = chat_type == "group"
        is_quick = content.strip() in self._QUICK_COMMANDS

        # 快捷命令不发"思考中"卡片
        reply_msg_id = None
        if not is_quick:
            if is_group:
                reply_msg_id = await self._reply_card(message_id, "思考中...", streaming=True)
            else:
                reply_msg_id = await self._send_card(chat_id, "思考中...", streaming=True)
            # 跟踪正在更新的卡片
            if reply_msg_id:
                self._active_cards[session_id] = reply_msg_id

        # 构建附件列表
        attachments = []
        for att_data in (attachments_data or []):
            att = Attachment(
                type=att_data["type"],
                filename=att_data.get("filename", ""),
                duration=att_data.get("duration", 0),
                extra={"key": att_data.get("key", ""), "message_id": att_data.get("message_id", "")},
            )
            # 飞书附件通过 message_resource API 下载（不是普通 URL）
            if att.extra.get("key") and att.extra.get("message_id") and self._client and self.media_manager:
                local_path = await self._download_resource(
                    att.extra["message_id"], att.extra["key"], att.type, att.filename,
                )
                if local_path:
                    att.file_path = local_path
            attachments.append(att)

        # 转换附件（图片转 base64、PDF 提取文本等）
        if attachments and self.media_manager:
            for i, att in enumerate(attachments):
                attachments[i] = await self.media_manager.process_attachment(att, channel="feishu")

        msg = IncomingMessage(
            channel="feishu",
            session_id=session_id,
            user_id=user_id,
            content=content,
            metadata={"chat_type": chat_type, "chat_id": chat_id, "open_id": user_id},
            attachments=attachments,
        )

        full_reply = []
        last_update_time = time.monotonic()

        try:
            async for token in self.router.handle(msg):
                # 过滤静默分段标记
                if token.strip() == "[TOOL_BREAK]":
                    continue
                full_reply.append(token)
                now = time.monotonic()

                # 按时间间隔更新卡片，体感更流畅
                if reply_msg_id and now - last_update_time >= self.STREAM_UPDATE_INTERVAL:
                    current_text = "".join(full_reply)
                    await self._patch_card(reply_msg_id, current_text, streaming=True)
                    last_update_time = now

            # 最终更新：完整内容 + 去掉 streaming 标记
            final_text = "".join(full_reply)
            if reply_msg_id:
                if final_text:
                    await self._patch_card(reply_msg_id, final_text, streaming=False)
                else:
                    await self._patch_card(reply_msg_id, "（无回复）", streaming=False)
            elif final_text:
                if is_group:
                    await self._reply_card(message_id, final_text, streaming=False)
                else:
                    await self._send_card(chat_id, final_text, streaming=False)
        finally:
            # 清理 active card 跟踪
            self._active_cards.pop(session_id, None)

    async def _send_card(self, chat_id: str, content: str, streaming: bool = False) -> str | None:
        """发送卡片消息到聊天（私聊用）。返回 message_id。"""
        if not self._client:
            return None

        try:
            from lark_oapi.api.im.v1 import (
                CreateMessageRequest,
                CreateMessageRequestBody,
            )

            body = CreateMessageRequestBody.builder() \
                .receive_id(chat_id) \
                .msg_type("interactive") \
                .content(self._build_card(content, streaming)) \
                .build()

            request = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(body) \
                .build()

            response = await self._client.im.v1.message.acreate(request)

            if not response.success():
                logger.error(f"[飞书] 发送卡片失败: code={response.code}, msg={response.msg}")
                return None
            return response.data.message_id

        except Exception as e:
            logger.error(f"[飞书] 发送卡片异常: {e}", exc_info=True)
            return None

    async def _reply_card(self, message_id: str, content: str, streaming: bool = False) -> str | None:
        """回复卡片消息（群聊用，关联原消息）。返回 message_id。"""
        if not self._client:
            return None

        try:
            from lark_oapi.api.im.v1 import (
                ReplyMessageRequest,
                ReplyMessageRequestBody,
            )

            body = ReplyMessageRequestBody.builder() \
                .msg_type("interactive") \
                .content(self._build_card(content, streaming)) \
                .build()

            request = ReplyMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(body) \
                .build()

            response = await self._client.im.v1.message.areply(request)

            if not response.success():
                logger.error(f"[飞书] 回复卡片失败: code={response.code}, msg={response.msg}")
                return None
            return response.data.message_id

        except Exception as e:
            logger.error(f"[飞书] 回复卡片异常: {e}", exc_info=True)
            return None

    async def _patch_card(self, message_id: str, content: str, streaming: bool = False) -> None:
        """更新卡片消息内容（流式效果）。"""
        if not self._client:
            return

        try:
            from lark_oapi.api.im.v1 import (
                PatchMessageRequest,
                PatchMessageRequestBody,
            )

            body = PatchMessageRequestBody.builder() \
                .content(self._build_card(content, streaming)) \
                .build()

            request = PatchMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(body) \
                .build()

            response = await self._client.im.v1.message.apatch(request)

            if not response.success():
                logger.error(f"[飞书] 更新卡片失败: code={response.code}, msg={response.msg}")

        except Exception as e:
            logger.error(f"[飞书] 更新卡片异常: {e}", exc_info=True)
