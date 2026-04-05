"""网络工具：web_search, web_fetch。"""

from __future__ import annotations

from langchain_core.tools import tool


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """搜索网页，返回标题、摘要和链接。

    Args:
        query: 搜索关键词
        max_results: 最大结果数，默认 5
    """
    try:
        from ddgs import DDGS
        results = DDGS().text(query, max_results=max_results)
        if not results:
            return "未找到相关结果"
        lines = []
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. [{r.get('title', '')}]({r.get('url', '')})")
            lines.append(f"   {r.get('content', '')}")
        return "\n".join(lines)
    except Exception as e:
        return f"搜索失败：{e}"


@tool
def web_fetch(url: str) -> str:
    """获取网页内容，返回 markdown 格式的纯文本。

    Args:
        url: 要获取的网页 URL
    """
    try:
        from ddgs import DDGS
        results = DDGS().extract(url)
        if not results:
            return "获取失败：无法提取内容"
        text = results[0].get("body", "")
        if not text:
            return "获取失败：页面内容为空"
        # 截断
        if len(text) > 8000:
            text = text[:8000] + "\n... (内容已截断)"
        return text
    except Exception as e:
        return f"获取失败：{e}"
