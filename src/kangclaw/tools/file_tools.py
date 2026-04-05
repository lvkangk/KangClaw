"""文件操作工具：read_file, write_file, edit_file, list_files, grep_file。"""

from __future__ import annotations

import glob as glob_module
import re
from pathlib import Path

from langchain_core.tools import tool

# workspace 根目录，由外部调用 configure() 设置
_workspace: Path | None = None


def configure(workspace: str | Path):
    """设置文件工具的 workspace 根目录，相对路径将基于此解析。"""
    global _workspace
    _workspace = Path(workspace).expanduser().resolve()


def _resolve(file_path: str) -> Path:
    """解析文件路径：绝对路径直接使用，相对路径基于 workspace。"""
    p = Path(file_path).expanduser()
    if p.is_absolute():
        return p
    if _workspace:
        return _workspace / p
    return p.resolve()


@tool
def read_file(file_path: str) -> str:
    """读取文件内容。

    Args:
        file_path: 要读取的文件路径
    """
    p = _resolve(file_path)
    if not p.exists():
        return f"错误：文件不存在 - {p}"
    if not p.is_file():
        return f"错误：不是文件 - {p}"
    try:
        return p.read_text(encoding="utf-8")
    except Exception as e:
        return f"读取失败：{e}"


@tool
def write_file(file_path: str, content: str) -> str:
    """写入文件，自动创建父目录。

    Args:
        file_path: 要写入的文件路径
        content: 要写入的内容
    """
    p = _resolve(file_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"已写入：{p}"
    except Exception as e:
        return f"写入失败：{e}"


@tool
def edit_file(file_path: str, old_string: str, new_string: str) -> str:
    """修改文件中的指定内容（old_string → new_string 替换）。

    Args:
        file_path: 要编辑的文件路径
        old_string: 要被替换的原始文本
        new_string: 替换后的新文本
    """
    p = _resolve(file_path)
    if not p.exists():
        return f"错误：文件不存在 - {p}"
    try:
        text = p.read_text(encoding="utf-8")
        if old_string not in text:
            return f"错误：未找到要替换的内容"
        text = text.replace(old_string, new_string, 1)
        p.write_text(text, encoding="utf-8")
        return f"已替换：{p}"
    except Exception as e:
        return f"编辑失败：{e}"


@tool
def list_files(directory: str, pattern: str = "*") -> str:
    """列出目录中的文件，支持 glob pattern。

    Args:
        directory: 目录路径
        pattern: glob 匹配模式，默认 *
    """
    p = _resolve(directory)
    if not p.exists():
        return f"错误：目录不存在 - {p}"
    try:
        matches = sorted(glob_module.glob(str(p / pattern), recursive=True))
        if not matches:
            return "无匹配文件"
        return "\n".join(matches[:100])  # 限制输出数量
    except Exception as e:
        return f"列出文件失败：{e}"


@tool
def grep_file(pattern: str, file_path: str = "", directory: str = "") -> str:
    """在文件或目录中搜索匹配关键词的行，返回匹配结果。

    Args:
        pattern: 搜索关键词或正则表达式
        file_path: 要搜索的单个文件路径（与 directory 二选一）
        directory: 要搜索的目录路径，递归搜索所有文本文件（与 file_path 二选一）
    """
    if not file_path and not directory:
        return "错误：请指定 file_path 或 directory"

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return f"错误：无效的正则表达式 - {e}"

    results = []

    if file_path:
        p = _resolve(file_path)
        if not p.exists():
            return f"错误：文件不存在 - {p}"
        results = _grep_single_file(p, regex)
    else:
        p = _resolve(directory)
        if not p.exists():
            return f"错误：目录不存在 - {p}"
        for f in sorted(p.rglob("*")):
            if not f.is_file():
                continue
            try:
                f.read_bytes()[:128].decode("utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            file_results = _grep_single_file(f, regex)
            if file_results:
                results.append(f"── {f} ──")
                results.extend(file_results)
            if len(results) > 200:
                results.append("... (结果过多，已截断)")
                break

    if not results:
        return "未找到匹配内容"
    return "\n".join(results)


def _grep_single_file(path: Path, regex: re.Pattern) -> list[str]:
    """搜索单个文件，返回匹配行列表（带行号）。"""
    matches = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines, 1):
            if regex.search(line):
                matches.append(f"{i}: {line}")
    except Exception:
        pass
    return matches
