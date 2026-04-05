"""媒体管理器：下载、保存、转换多媒体资源。"""

from __future__ import annotations

import base64
import logging
import mimetypes
import uuid
from pathlib import Path

logger = logging.getLogger("kangclaw.media")

try:
    import aiohttp
except ImportError:
    aiohttp = None

try:
    import pdfplumber
except ImportError:
    pdfplumber = None


class MediaManager:
    """管理多媒体资源的下载、存储和转换。"""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self.media_dir = self.workspace / "media"
        self.media_dir.mkdir(parents=True, exist_ok=True)

    def _channel_dir(self, channel: str = "default") -> Path:
        d = self.media_dir / channel
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _unique_name(self, filename: str) -> str:
        return f"{uuid.uuid4().hex[:8]}_{filename}"

    async def download(self, url: str, filename: str = "", channel: str = "default") -> str:
        """下载远程 URL 到本地，返回本地文件路径。"""
        if aiohttp is None:
            logger.error("aiohttp 未安装，无法下载远程资源")
            return ""

        if not filename:
            filename = url.split("/")[-1].split("?")[0] or "download"

        local_name = self._unique_name(filename)
        local_path = self._channel_dir(channel) / local_name

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        local_path.write_bytes(data)
                        logger.info(f"下载完成: {url} -> {local_path}")
                        return str(local_path)
                    else:
                        logger.error(f"下载失败: {url} status={resp.status}")
                        return ""
        except Exception as e:
            logger.error(f"下载异常: {url} {e}")
            return ""

    def save_data_url(self, data_url: str, filename: str = "", channel: str = "default") -> str:
        """保存 base64 data URL 到本地文件，返回路径。"""
        try:
            header, encoded = data_url.split(",", 1)
            data = base64.b64decode(encoded)
        except (ValueError, Exception) as e:
            logger.error(f"解析 data URL 失败: {e}")
            return ""

        if not filename:
            mime = header.split(":")[1].split(";")[0] if ":" in header else ""
            ext = mimetypes.guess_extension(mime) or ""
            filename = f"file{ext}"

        local_name = self._unique_name(filename)
        local_path = self._channel_dir(channel) / local_name
        local_path.write_bytes(data)
        return str(local_path)

    def save_bytes(self, data: bytes, filename: str, channel: str = "default") -> str:
        """保存字节数据到本地文件，返回路径。"""
        local_name = self._unique_name(filename)
        local_path = self._channel_dir(channel) / local_name
        local_path.write_bytes(data)
        return str(local_path)

    def image_to_data_url(self, file_path: str) -> str:
        """将本地图片文件转为 base64 data URL。"""
        path = Path(file_path)
        if not path.exists():
            return ""
        mime = mimetypes.guess_type(str(path))[0] or "image/png"
        data = path.read_bytes()
        b64 = base64.b64encode(data).decode()
        return f"data:{mime};base64,{b64}"

    def extract_pdf_text(self, file_path: str, max_pages: int = 20) -> str:
        """从 PDF 提取文本内容。"""
        if pdfplumber is None:
            return "[PDF 文本提取需要 pdfplumber，未安装。请运行: pip install pdfplumber]"

        try:
            texts = []
            with pdfplumber.open(file_path) as doc:
                for i, page in enumerate(doc.pages):
                    if i >= max_pages:
                        texts.append(f"\n... (已截取前 {max_pages} 页)")
                        break
                    text = page.extract_text()
                    if text:
                        texts.append(text)
            return "\n".join(texts)
        except Exception as e:
            return f"[PDF 提取失败: {e}]"

    def extract_text_file(self, file_path: str, max_chars: int = 10000) -> str:
        """读取文本类文件内容。"""
        try:
            text = Path(file_path).read_text(encoding="utf-8", errors="replace")
            if len(text) > max_chars:
                text = text[:max_chars] + "\n... (已截断)"
            return text
        except Exception as e:
            return f"[读取文件失败: {e}]"

    async def process_attachment(self, att, channel: str = "default") -> "Attachment":
        """下载并转换附件：图片→base64, PDF→提取文本, 音频保留转写。

        修改并返回同一个 Attachment 对象，填充 file_path 和 extra 字段。
        """
        # 1. 下载到本地（如果还没有本地路径）
        if not att.file_path and att.url:
            if att.url.startswith("data:"):
                # base64 data URL → 保存到本地文件
                att.file_path = self.save_data_url(att.url, att.filename, channel)
                if att.type == "image":
                    att.extra["data_url"] = att.url
            else:
                att.file_path = await self.download(att.url, att.filename, channel)

        if not att.file_path:
            return att

        # 2. 按类型转换
        if att.type == "image" and "data_url" not in att.extra:
            att.extra["data_url"] = self.image_to_data_url(att.file_path)

        elif att.type == "file" and att.filename.lower().endswith(".pdf"):
            att.extra["extracted_text"] = self.extract_pdf_text(att.file_path)

        # 音频：如果已有 transcription 则保留，不覆盖
        # （钉钉 audio 消息自带 recognition 字段，渠道层已填入 extra["transcription"]）

        return att
