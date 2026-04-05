"""BaseChannel 抽象接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from kangclaw.config import ChannelConfig


class BaseChannel(ABC):
    """所有渠道的抽象基类。"""

    def __init__(self, config: ChannelConfig, router, media_manager=None):
        self.config = config
        self.router = router
        self.name = config.name
        self.media_manager = media_manager

    @abstractmethod
    async def start(self) -> None:
        """启动渠道监听。"""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """停止渠道。"""
        ...

    @abstractmethod
    async def send(self, session_id: str, content: str, **kwargs) -> None:
        """向指定 session 发送消息。"""
        ...

    async def shutdown_notify(self) -> None:
        """网关关闭时通知正在进行的会话。子类可覆盖。"""
        pass
