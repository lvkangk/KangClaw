"""发送图片工具：让 agent 将处理后的图片发回给用户。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
from pathlib import Path

from langchain_core.tools import tool

logger = logging.getLogger("kangclaw.send_tools")

# 运行时由 server.py 设置
_channels: dict = {}
_ws_connections: dict = {}


def configure(channels: dict, ws_connections: dict):
    """配置发送工具的渠道引用和 WebSocket 连接池。"""
    global _channels, _ws_connections
    _channels = channels
    _ws_connections = ws_connections


@tool
def send_image(file_path: str, channel: str = "web", session_id: str = "", chat_id: str = "") -> str:
    """将本地图片发送给用户。处理完图片后调用此工具将结果发回。

    Args:
        file_path: 要发送的本地图片文件路径
        channel: 发送到的渠道（自动注入，无需手动填写）
        session_id: 目标会话 ID（自动注入，无需手动填写）
        chat_id: 目标 chat_id（自动注入，无需手动填写）
    """
    p = Path(file_path)
    if not p.exists():
        return f"错误：文件不存在 {file_path}"

    try:
        if channel in ("web", "cli"):
            return _send_via_ws(p, session_id)
        else:
            return _send_via_channel(p, channel, session_id, chat_id)
    except Exception as e:
        logger.error(f"发送图片失败: {e}", exc_info=True)
        return f"发送失败：{e}"


def _send_via_ws(file_path: Path, session_id: str) -> str:
    """通过 WebSocket 发送图片（base64 JSON）。"""
    ws_set = _ws_connections.get(session_id)
    if not ws_set:
        return f"未找到 WebSocket 连接: {session_id}"

    mime = mimetypes.guess_type(str(file_path))[0] or "image/png"
    data = file_path.read_bytes()
    b64 = base64.b64encode(data).decode()
    msg = json.dumps({
        "type": "image",
        "data": f"data:{mime};base64,{b64}",
        "filename": file_path.name,
    })

    dead = set()
    for ws in ws_set:
        try:
            # WebSocket.send_text 是 coroutine，需要在事件循环中执行
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(ws.send_text(msg), loop).result(timeout=10)
            else:
                loop.run_until_complete(ws.send_text(msg))
        except Exception:
            dead.add(ws)
    ws_set -= dead

    return f"已发送图片 {file_path.name} 到 {session_id}"


def _send_via_channel(file_path: Path, channel: str, session_id: str, chat_id: str) -> str:
    """通过 IM 渠道发送图片。"""
    ch = _channels.get(channel)
    if not ch:
        return f"未找到渠道: {channel}，不可用"

    # 各渠道的 send_image 是 async 方法，需要调度到事件循环
    if not hasattr(ch, "send_image"):
        return f"渠道 {channel} 不支持发送图片"

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                ch.send_image(session_id, str(file_path), chat_id=chat_id), loop
            )
            result = future.result(timeout=30)
            return result or f"已发送图片 {file_path.name}"
        else:
            result = loop.run_until_complete(
                ch.send_image(session_id, str(file_path), chat_id=chat_id)
            )
            return result or f"已发送图片 {file_path.name}"
    except Exception as e:
        return f"发送失败：{e}"
