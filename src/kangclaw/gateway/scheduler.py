"""APScheduler：heartbeat + cron 任务管理。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from kangclaw.config import AppConfig

logger = logging.getLogger("kangclaw.scheduler")


class Scheduler:
    """定时任务调度器。"""

    def __init__(self, config: AppConfig, on_heartbeat=None, on_cron=None):
        self.config = config
        self.scheduler = AsyncIOScheduler()
        self._on_heartbeat = on_heartbeat
        self._on_cron = on_cron
        self.workspace = Path(config.general.workspace)

    def setup(self):
        """初始化所有定时任务。"""
        # Heartbeat
        if self.config.heartbeat.enabled and self._on_heartbeat:
            self.scheduler.add_job(
                self._on_heartbeat,
                IntervalTrigger(minutes=self.config.heartbeat.interval_minutes),
                id="heartbeat",
                replace_existing=True,
            )
            logger.info(f"心跳任务已启动，间隔 {self.config.heartbeat.interval_minutes} 分钟")

        # 加载 cron 任务
        cron_file = self.workspace / "cron" / "cron.json"
        if cron_file.exists():
            try:
                jobs = json.loads(cron_file.read_text(encoding="utf-8"))
                for job in jobs:
                    self._register_cron_job(job)
                logger.info(f"已加载 {len(jobs)} 个 cron 任务")
            except Exception as e:
                logger.error(f"加载 cron 任务失败: {e}")

    def _register_cron_job(self, job: dict):
        """注册一个 cron 任务到 scheduler。"""
        parts = job["cron_expr"].split()
        if len(parts) != 5:
            logger.warning(f"无效 cron 表达式: {job['cron_expr']}")
            return

        trigger = CronTrigger(
            minute=parts[0], hour=parts[1],
            day=parts[2], month=parts[3],
            day_of_week=parts[4],
        )

        async def callback():
            if self._on_cron:
                await self._on_cron(job)

        self.scheduler.add_job(
            callback, trigger,
            id=job["id"],
            replace_existing=True,
        )

    def start(self):
        self.scheduler.start()

    def stop(self):
        self.scheduler.shutdown(wait=False)
