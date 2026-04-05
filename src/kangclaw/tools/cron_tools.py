"""定时任务工具：cron_list, cron_add, cron_remove。"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from langchain_core.tools import tool

# 运行时由 server.py 设置
_cron_file: Path | None = None
_kangclaw_scheduler = None  # kangclaw.gateway.scheduler.Scheduler 实例


def configure(cron_file: Path, kangclaw_scheduler=None):
    """配置 cron 工具的文件路径和调度器。

    Args:
        cron_file: cron.json 路径
        kangclaw_scheduler: kangclaw.gateway.scheduler.Scheduler 实例
    """
    global _cron_file, _kangclaw_scheduler
    _cron_file = cron_file
    _kangclaw_scheduler = kangclaw_scheduler


def _load_jobs() -> list[dict]:
    if _cron_file and _cron_file.exists():
        try:
            return json.loads(_cron_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
    return []


def _save_jobs(jobs: list[dict]):
    if _cron_file:
        _cron_file.parent.mkdir(parents=True, exist_ok=True)
        _cron_file.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")


@tool
def cron_list() -> str:
    """查看所有定时任务列表。"""
    jobs = _load_jobs()
    if not jobs:
        return "当前没有定时任务"
    lines = []
    for j in jobs:
        lines.append(f"[{j['id']}] {j['cron_expr']} — {j['description']} (渠道: {j.get('channel', 'N/A')})")
    return "\n".join(lines)


@tool
def cron_add(cron_expr: str, description: str, channel: str = "web", session_id: str = "", chat_id: str = "") -> str:
    """创建一个定时任务。

    Args:
        cron_expr: cron 表达式，如 "0 8 * * *" 表示每天早上8点
        description: 任务描述
        channel: 推送渠道，默认 web
        session_id: 创建任务的来源会话 ID（自动注入，无需手动填写）
        chat_id: 推送目标的 chat_id（自动注入，无需手动填写）
    """
    job_id = f"job_{uuid.uuid4().hex[:8]}"
    job = {
        "id": job_id,
        "cron_expr": cron_expr,
        "description": description,
        "channel": channel,
        "session_id": session_id,
        "chat_id": chat_id,
        "created_at": time.time(),
    }
    jobs = _load_jobs()
    jobs.append(job)
    _save_jobs(jobs)

    # 通过 Scheduler 实例注册，确保回调走 on_cron 链路
    if _kangclaw_scheduler:
        try:
            _kangclaw_scheduler._register_cron_job(job)
        except Exception as e:
            return f"任务已保存但调度注册失败：{e}"

    return f"已创建定时任务 [{job_id}]: {cron_expr} — {description}"


@tool
def cron_remove(job_id: str) -> str:
    """删除指定的定时任务。

    Args:
        job_id: 任务 ID
    """
    jobs = _load_jobs()
    new_jobs = [j for j in jobs if j["id"] != job_id]
    if len(new_jobs) == len(jobs):
        return f"未找到任务: {job_id}"

    _save_jobs(new_jobs)

    if _kangclaw_scheduler:
        try:
            _kangclaw_scheduler.scheduler.remove_job(job_id)
        except Exception:
            pass

    return f"已删除任务: {job_id}"
