"""Token 计数工具。"""

from __future__ import annotations

import logging
from functools import lru_cache

import tiktoken

logger = logging.getLogger("kangclaw.tokens")

_MSG_OVERHEAD = 4  # 每条消息的 role/framing 开销


@lru_cache(maxsize=1)
def _get_encoding() -> tiktoken.Encoding:
    """缓存 cl100k_base encoding（GPT-4/3.5 系列通用，作为跨模型近似）。"""
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """计算文本的 token 数。"""
    return len(_get_encoding().encode(text, disallowed_special=()))


def count_message_tokens(messages: list) -> int:
    """计算 LangChain 消息列表的总 token 数。"""
    total = 0
    for msg in messages:
        content = msg.content
        if isinstance(content, list):
            text = " ".join(
                part.get("text", "") for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        elif isinstance(content, str):
            text = content
        else:
            text = str(content)
        total += count_tokens(text) + _MSG_OVERHEAD
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            for tc in msg.tool_calls:
                total += count_tokens(tc.get("name", ""))
                total += count_tokens(str(tc.get("args", {})))
    return total
