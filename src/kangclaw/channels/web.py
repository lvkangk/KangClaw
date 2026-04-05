"""Web 渠道（WebSocket handler）— 主要逻辑在 server.py 中。"""

from __future__ import annotations

from kangclaw.channels.base import BaseChannel


class WebChannel(BaseChannel):
    """Web 渠道，实际 WebSocket 处理在 server.py 中。"""

    async def start(self) -> None:
        pass  # WebSocket 由 FastAPI 处理

    async def stop(self) -> None:
        pass

    async def send(self, session_id: str, content: str) -> None:
        # WebSocket 推送需要持有 ws 连接引用，由 server.py 管理
        pass
