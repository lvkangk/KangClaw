"""FastAPI app + WebSocket 端点 + 启动逻辑。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from kangclaw.config import KANGCLAW_HOME, CONFIG_PATH, load_config, AppConfig, SUPPORTED_CHANNELS, MODEL_SCHEMA, load_raw_channel_configs, save_channel_config, load_raw_model_configs, save_model_configs, save_agent_model_primary_key, get_active_model
from kangclaw.gateway.memory import MemoryManager
from kangclaw.gateway.agent import Agent
from kangclaw.gateway.router import Router, IncomingMessage
from kangclaw.gateway.scheduler import Scheduler
from kangclaw.gateway.media import MediaManager
from kangclaw.skills.loader import load_skills_summary
from kangclaw.tools import cron_tools
from kangclaw.tools import file_tools
from kangclaw.tools import send_tools

logger = logging.getLogger("kangclaw.server")

# 全局状态
_app: FastAPI | None = None
_router: Router | None = None
_scheduler: Scheduler | None = None
_channels: dict = {}
_channel_errors: dict[str, str] = {}
_media_manager: MediaManager | None = None

# WebSocket 连接池：session_id -> set[WebSocket]
_ws_connections: dict[str, set[WebSocket]] = {}


def _parse_ws_message(raw: str) -> tuple[str, list]:
    """解析 WebSocket 消息，支持纯文本和带附件的 JSON。"""
    from kangclaw.gateway.router import Attachment
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "content" in data:
            content = data["content"]
            attachments = []
            for att_data in data.get("attachments", []):
                attachments.append(Attachment(
                    type=att_data.get("type", "file"),
                    url=att_data.get("data", ""),
                    filename=att_data.get("filename", ""),
                    mime_type=att_data.get("mime_type", ""),
                ))
            return content, attachments
    except (json.JSONDecodeError, TypeError):
        pass
    return raw, []


def create_app(config: AppConfig | None = None, channel_filter: str | None = None) -> FastAPI:
    """创建并配置 FastAPI 应用。"""
    global _app, _router, _scheduler, _media_manager

    if config is None:
        config = load_config()

    # 设置日志
    logging.basicConfig(
        level=getattr(logging, config.general.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    workspace = Path(config.general.workspace)

    # 配置文件工具的 workspace 根目录
    file_tools.configure(workspace)

    # 初始化记忆系统
    memory = MemoryManager(workspace)

    # 初始化媒体管理器
    media_manager = MediaManager(workspace)
    _media_manager = media_manager

    # 加载技能摘要
    skills_summary = load_skills_summary(workspace / "skills")

    # 创建 Agent
    agent = Agent(config, memory, skills_summary)

    # 创建路由器
    _router = Router(agent)

    # 配置 cron 工具
    cron_file = workspace / "cron" / "cron.json"
    cron_tools.configure(cron_file, None)  # scheduler 稍后设置

    # 创建调度器
    async def on_heartbeat():
        """心跳回调：读取 HEARTBEAT.md 并发给 agent。"""
        heartbeat_file = workspace / "HEARTBEAT.md"
        if not heartbeat_file.exists():
            return
        content = heartbeat_file.read_text(encoding="utf-8")
        if not content.strip():
            return
        import datetime
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        prompt = f"[心跳巡检 {now}]\n\n以下是用户设置的巡检任务：\n{content}\n\n请检查是否有需要执行的事项。如果没有需要执行的，直接回复：HEARTBEAT_SKIP（HEARTBEAT_SKIP前后不要加任何其他字符）。"
        session_id = "system-heartbeat"
        memory.get_or_create_session(session_id, channel="system", type_="heartbeat")

        full_reply = []
        async for token in agent.process(session_id, prompt, channel="system"):
            # 过滤工具执行提示，不推送给用户
            stripped = token.strip()
            if stripped.startswith("[正在执行") and stripped.endswith("]"):
                continue
            full_reply.append(token)

        # 如果有实质内容且非跳过标记，推送到 web-default 连接
        reply_text = "".join(full_reply).strip()
        if reply_text and "HEARTBEAT_SKIP" not in reply_text:
            ws_set = _ws_connections.get("web-default")
            if ws_set:
                dead = set()
                for ws in ws_set:
                    try:
                        await ws.send_text(f"\n[心跳巡检 {now}]\n\n")
                        await ws.send_text(reply_text)
                        await ws.send_text("[DONE]")
                    except Exception:
                        dead.add(ws)
                ws_set -= dead

    async def on_cron(job: dict):
        """Cron 任务回调。"""
        channel = job.get("channel") or "web"
        # 优先用创建任务时保存的 session_id，兜底用 {channel}-default
        session_id = job.get("session_id") or f"{channel}-default"
        description = job.get("description", "")
        memory.get_or_create_session(session_id, channel=channel, type_="cron")

        full_reply = []
        async for token in agent.process(session_id, f"[定时任务提醒] {description}", channel=channel):
            # 过滤工具执行提示，不推送给用户
            stripped = token.strip()
            if stripped.startswith("[正在执行") and stripped.endswith("]"):
                continue
            full_reply.append(token)

        reply_text = "".join(full_reply)
        if not reply_text:
            return

        # 推送到创建任务时的 session 对应的 WebSocket 连接
        pushed = False
        ws_set = _ws_connections.get(session_id)
        if ws_set:
            dead = set()
            for ws in ws_set:
                try:
                    await ws.send_text(f"\n[定时任务] {description}\n")
                    await ws.send_text(reply_text)
                    await ws.send_text("[DONE]")
                    pushed = True
                except Exception:
                    dead.add(ws)
            ws_set -= dead

        # 尝试渠道推送（飞书、QQ 等）
        if not pushed:
            ch = _channels.get(channel)
            if ch:
                try:
                    chat_id = job.get("chat_id", "")
                    await ch.send(session_id, reply_text, chat_id=chat_id)
                    pushed = True
                except Exception as e:
                    logger.error(f"Cron 推送失败: {e}")

        if not pushed:
            logger.warning(f"Cron 任务 [{job.get('id')}] 执行完毕但无可用推送渠道")

    _scheduler = Scheduler(config, on_heartbeat=on_heartbeat, on_cron=on_cron)
    cron_tools.configure(cron_file, _scheduler)
    _scheduler.setup()

    # lifespan: 替代已废弃的 on_event("startup") / on_event("shutdown")
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # ── startup ──
        _scheduler.start()
        # 渠道在后台启动，不阻塞 Web 服务
        asyncio.create_task(_start_channels(config, channel_filter, media_manager))
        send_tools.configure(_channels, _ws_connections)
        logger.info("kangclaw gateway 已启动")
        yield
        # ── shutdown ──
        logger.info("kangclaw gateway 正在关闭...")

        # 1. 取消所有进行中的 agent 会话，等待退出
        await agent.shutdown(timeout=3.0)

        # 2. 通知各渠道正在进行的会话（更新飞书卡片等）
        for ch in _channels.values():
            try:
                await ch.shutdown_notify()
            except Exception as e:
                logger.error(f"渠道 shutdown_notify 失败: {e}")

        # 3. 通知 WebSocket 客户端
        for sid, ws_set in list(_ws_connections.items()):
            for ws in list(ws_set):
                try:
                    await ws.send_text("[网关已关闭]")
                    await ws.close()
                except Exception:
                    pass

        # 4. 停止调度器和渠道
        _scheduler.stop()
        for ch in _channels.values():
            try:
                await ch.stop()
            except Exception:
                pass
        logger.info("kangclaw gateway 已关闭")

    # 创建 FastAPI app
    app = FastAPI(title="kangclaw gateway", lifespan=lifespan)

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket, client: str = Query("web")):
        await ws.accept()
        # 根据客户端类型分配不同的 session
        if client == "cli":
            session_id = "cli-default"
            channel = "cli"
        else:
            session_id = "web-default"
            channel = "web"

        # 注册到连接池
        if session_id not in _ws_connections:
            _ws_connections[session_id] = set()
        _ws_connections[session_id].add(ws)

        try:
            while True:
                data = await ws.receive_text()
                content, attachments = _parse_ws_message(data)

                # 下载并转换附件
                for i, att in enumerate(attachments):
                    attachments[i] = await media_manager.process_attachment(att, channel=channel)

                msg = IncomingMessage(
                    channel=channel,
                    session_id=session_id,
                    user_id=channel,
                    content=content,
                    attachments=attachments,
                )

                has_tokens = False
                async for token in _router.handle(msg):
                    await ws.send_text(token)
                    has_tokens = True
                if has_tokens:
                    await ws.send_text("[DONE]")

        except WebSocketDisconnect:
            logger.info(f"WebSocket 断开: {session_id}")
        except Exception as e:
            logger.error(f"WebSocket 错误: {e}")
            try:
                await ws.send_text(str(e))
                await ws.send_text("[DONE]")
            except Exception:
                pass
        finally:
            # 从连接池移除
            ws_set = _ws_connections.get(session_id)
            if ws_set:
                ws_set.discard(ws)
                if not ws_set:
                    del _ws_connections[session_id]

            # CLI 断开时后台整合历史消息
            if channel == "cli":
                asyncio.create_task(_consolidate_on_disconnect(session_id))

    @app.get("/api/status")
    async def api_status():
        from kangclaw import __version__
        return {
            "status": "running",
            "version": __version__,
            "channels": list(_channels.keys()),
        }

    @app.get("/api/history")
    async def api_history(session_id: str = "web-default", limit: int = 50):
        """返回指定 session 的历史消息。"""
        messages = memory.load_history(session_id, limit=limit)
        logger.info(f"[{session_id}] 历史消息查询: {len(messages)} 条")
        return [
            {
                "role": msg.role,
                "content": msg.content,
                "name": msg.name,
            }
            for msg in messages
            if msg.role in ("user", "assistant")
            and msg.content
            and not msg.content.startswith("[系统]")
        ]

    @app.get("/api/model")
    async def api_model():
        """返回所有模型配置和 schema。"""
        raw_models = load_raw_model_configs()
        raw_agent = {}
        if CONFIG_PATH.exists():
            import tomllib
            with open(CONFIG_PATH, "rb") as f:
                raw_agent = tomllib.load(f).get("agent", {})
        return {
            "schema": MODEL_SCHEMA,
            "models": raw_models,
            "active_primary_key": raw_agent.get("model_primary_key", ""),
        }

    @app.put("/api/model")
    async def api_model_update(request: Request):
        """保存所有模型配置和激活状态。"""
        body = await request.json()
        try:
            models = body.get("models", [])
            active_pk = body.get("active_primary_key", "")
            save_model_configs(models)
            if active_pk:
                save_agent_model_primary_key(active_pk)
            # 热更新 Agent 模型
            new_config = load_config()
            agent.reload_model(new_config)
        except Exception as e:
            return {"error": f"保存配置失败: {e}"}
        return {"ok": True}

    @app.get("/api/channels")
    async def api_channels():
        """返回所有支持渠道的状态和配置。"""
        raw_configs = load_raw_channel_configs()
        raw_map = {ch.get("name"): ch for ch in raw_configs}

        result = []
        for name, schema in SUPPORTED_CHANNELS.items():
            raw = raw_map.get(name, {})
            enabled = raw.get("enabled", False)

            if name in _channels:
                status = "online"
            elif name in _channel_errors:
                status = "error"
            elif not enabled:
                status = "disabled"
            else:
                status = "offline"

            cfg = {}
            for f in schema["fields"]:
                val = raw.get(f["key"], "")
                cfg[f["key"]] = val

            result.append({
                "name": name,
                "label": schema["label"],
                "enabled": enabled,
                "status": status,
                "error": _channel_errors.get(name, ""),
                "config": cfg,
            })
        return result

    @app.get("/api/channels/schema")
    async def api_channels_schema():
        """返回各渠道配置字段定义。"""
        return SUPPORTED_CHANNELS

    @app.put("/api/channels/{name}")
    async def api_channel_update(name: str, request: Request):
        """更新渠道配置并热重载。"""
        if name not in SUPPORTED_CHANNELS:
            return {"error": f"不支持的渠道: {name}"}

        body = await request.json()

        try:
            save_channel_config(name, body)
        except Exception as e:
            return {"error": f"保存配置失败: {e}"}

        # 热重载：先停旧的
        if name in _channels:
            try:
                await _channels[name].stop()
            except Exception:
                pass
            del _channels[name]
        _channel_errors.pop(name, None)

        # 如果启用则启动新的
        enabled = body.get("enabled", False)
        if enabled:
            try:
                new_config = load_config()
                ch_config = None
                for c in new_config.channels:
                    if c.name == name:
                        ch_config = c
                        break
                if ch_config:
                    await _start_single_channel(name, ch_config, _media_manager)
            except Exception as e:
                _channel_errors[name] = str(e)
                logger.error(f"渠道 {name} 热重载失败: {e}")

        return {"ok": True}

    # ── 技能管理 API ──

    def _build_file_tree(root: Path, base: Path) -> list:
        """递归构建目录的文件树结构，path 相对于 workspace。"""
        items = []
        for entry in sorted(root.iterdir(), key=lambda e: (e.is_file(), e.name)):
            if entry.name.startswith("."):
                continue
            rel = str(entry.relative_to(base))
            if entry.is_dir():
                items.append({
                    "name": entry.name,
                    "type": "dir",
                    "path": rel,
                    "children": _build_file_tree(entry, base),
                })
            else:
                items.append({"name": entry.name, "type": "file", "path": rel})
        return items

    def _extract_skill_description(skill_md: Path) -> str:
        """从 SKILL.md 提取简述（跳过 YAML frontmatter）。"""
        try:
            lines = skill_md.read_text(encoding="utf-8").split("\n")
            start = 0
            if lines and lines[0].strip() == "---":
                for i in range(1, len(lines)):
                    if lines[i].strip() == "---":
                        start = i + 1
                        break
            for line in lines[start:]:
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("---"):
                    return line
        except Exception:
            pass
        return ""

    @app.get("/api/skills")
    async def api_skills():
        """返回用户技能列表（含文件树）。"""
        user_skills_dir = workspace / "skills"
        if not user_skills_dir.exists():
            return []
        skills = []
        for skill_dir in sorted(user_skills_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            skills.append({
                "name": skill_dir.name,
                "description": _extract_skill_description(skill_md),
                "files": _build_file_tree(skill_dir, workspace),
            })
        return skills

    @app.get("/api/skills/file")
    async def api_skills_file_read(path: str):
        """读取技能目录下的单个文件内容。"""
        from fastapi.responses import JSONResponse
        full = (workspace / path).resolve()
        skills_root = (workspace / "skills").resolve()
        if not str(full).startswith(str(skills_root)):
            return JSONResponse({"error": "非法路径"}, status_code=400)
        if not full.exists():
            return JSONResponse({"error": "文件不存在"}, status_code=404)
        try:
            content = full.read_text(encoding="utf-8")
        except Exception as e:
            return JSONResponse({"error": f"读取失败: {e}"}, status_code=500)
        return {"content": content}

    @app.put("/api/skills/file")
    async def api_skills_file_write(request: Request):
        """保存技能目录下的单个文件内容。"""
        from fastapi.responses import JSONResponse
        body = await request.json()
        path = body.get("path", "")
        content = body.get("content", "")
        full = (workspace / path).resolve()
        skills_root = (workspace / "skills").resolve()
        if not str(full).startswith(str(skills_root)):
            return JSONResponse({"error": "非法路径"}, status_code=400)
        try:
            full.write_text(content, encoding="utf-8")
        except Exception as e:
            return JSONResponse({"error": f"保存失败: {e}"}, status_code=500)
        return {"ok": True}

    @app.delete("/api/skills/{name}")
    async def api_skills_delete(name: str):
        """删除用户技能目录。"""
        import shutil
        from fastapi.responses import JSONResponse
        skill_dir = (workspace / "skills" / name).resolve()
        skills_root = (workspace / "skills").resolve()
        if not str(skill_dir).startswith(str(skills_root)) or not skill_dir.exists():
            return JSONResponse({"error": "技能不存在"}, status_code=404)
        try:
            shutil.rmtree(skill_dir)
        except Exception as e:
            return JSONResponse({"error": f"删除失败: {e}"}, status_code=500)
        return {"ok": True}

    # ── Heartbeat 配置 API ──

    @app.get("/api/heartbeat")
    async def api_heartbeat_get():
        """返回 heartbeat 配置。"""
        return {
            "enabled": config.heartbeat.enabled,
            "interval_minutes": config.heartbeat.interval_minutes,
        }

    @app.put("/api/heartbeat")
    async def api_heartbeat_update(request: Request):
        """更新 heartbeat 配置并热生效。"""
        from kangclaw.config import save_heartbeat_config
        from apscheduler.triggers.interval import IntervalTrigger
        body = await request.json()
        updates = {}
        if "enabled" in body:
            updates["enabled"] = bool(body["enabled"])
        if "interval_minutes" in body:
            val = int(body["interval_minutes"])
            if val < 1:
                from fastapi.responses import JSONResponse
                return JSONResponse({"error": "间隔不能小于 1 分钟"}, status_code=400)
            updates["interval_minutes"] = val
        if updates:
            save_heartbeat_config(updates)
            # 热更新内存中的 config 和 scheduler
            for k, v in updates.items():
                setattr(config.heartbeat, k, v)
            if _scheduler:
                try:
                    _scheduler.scheduler.remove_job("heartbeat")
                except Exception:
                    pass
                if config.heartbeat.enabled and _scheduler._on_heartbeat:
                    _scheduler.scheduler.add_job(
                        _scheduler._on_heartbeat,
                        IntervalTrigger(minutes=config.heartbeat.interval_minutes),
                        id="heartbeat",
                        replace_existing=True,
                    )
        return {"ok": True}

    @app.get("/api/heartbeat/file")
    async def api_heartbeat_file_read():
        """读取 HEARTBEAT.md 内容。"""
        hb_file = workspace / "HEARTBEAT.md"
        if not hb_file.exists():
            return {"content": ""}
        try:
            return {"content": hb_file.read_text(encoding="utf-8")}
        except Exception as e:
            from fastapi.responses import JSONResponse
            return JSONResponse({"error": f"读取失败: {e}"}, status_code=500)

    @app.put("/api/heartbeat/file")
    async def api_heartbeat_file_write(request: Request):
        """保存 HEARTBEAT.md 内容。"""
        body = await request.json()
        content = body.get("content", "")
        hb_file = workspace / "HEARTBEAT.md"
        try:
            hb_file.write_text(content, encoding="utf-8")
        except Exception as e:
            from fastapi.responses import JSONResponse
            return JSONResponse({"error": f"保存失败: {e}"}, status_code=500)
        return {"ok": True}

    # ── Cron 定时任务 API ──

    @app.get("/api/cron")
    async def api_cron_list():
        """返回所有定时任务列表。"""
        from kangclaw.tools.cron_tools import _load_jobs
        return _load_jobs()

    @app.put("/api/cron/{job_id}")
    async def api_cron_update(job_id: str, request: Request):
        """编辑定时任务的 cron 表达式和描述。"""
        from fastapi.responses import JSONResponse
        from kangclaw.tools.cron_tools import _load_jobs, _save_jobs
        body = await request.json()
        jobs = _load_jobs()
        target = None
        for j in jobs:
            if j["id"] == job_id:
                target = j
                break
        if not target:
            return JSONResponse({"error": f"未找到任务: {job_id}"}, status_code=404)

        if "cron_expr" in body:
            parts = body["cron_expr"].split()
            if len(parts) != 5:
                return JSONResponse({"error": "cron 表达式格式错误，需要 5 个字段（分 时 日 月 星期）"}, status_code=400)
            # 预校验表达式是否合法
            try:
                from apscheduler.triggers.cron import CronTrigger
                CronTrigger(
                    minute=parts[0], hour=parts[1],
                    day=parts[2], month=parts[3],
                    day_of_week=parts[4],
                )
            except Exception as e:
                return JSONResponse({"error": f"cron 表达式无效：{e}"}, status_code=400)
            target["cron_expr"] = body["cron_expr"]
        if "description" in body:
            target["description"] = body["description"]

        _save_jobs(jobs)

        # 重新注册到 scheduler
        if _scheduler:
            try:
                _scheduler.scheduler.remove_job(job_id)
            except Exception:
                pass
            _scheduler._register_cron_job(target)

        return {"ok": True}

    @app.delete("/api/cron/{job_id}")
    async def api_cron_delete(job_id: str):
        """删除定时任务。"""
        from fastapi.responses import JSONResponse
        from kangclaw.tools.cron_tools import _load_jobs, _save_jobs
        jobs = _load_jobs()
        new_jobs = [j for j in jobs if j["id"] != job_id]
        if len(new_jobs) == len(jobs):
            return JSONResponse({"error": f"未找到任务: {job_id}"}, status_code=404)
        _save_jobs(new_jobs)
        if _scheduler:
            try:
                _scheduler.scheduler.remove_job(job_id)
            except Exception:
                pass
        return {"ok": True}

    @app.get("/api/agent-settings")
    async def api_agent_settings_get():
        """返回 agent 配置。"""
        return {
            "auto_greeting": config.agent.auto_greeting,
            "show_tool_calls": config.agent.show_tool_calls,
        }

    @app.put("/api/agent-settings")
    async def api_agent_settings_update(request: Request):
        """更新 agent 配置并热生效。"""
        from kangclaw.config import save_agent_config
        body = await request.json()
        updates = {}
        if "auto_greeting" in body:
            updates["auto_greeting"] = bool(body["auto_greeting"])
        if "show_tool_calls" in body:
            updates["show_tool_calls"] = bool(body["show_tool_calls"])
        if updates:
            save_agent_config(updates)
            for k, v in updates.items():
                setattr(config.agent, k, v)
        return {"ok": True}

    @app.get("/api/config-status")
    async def api_config_status():
        """返回配置更新时间和网关启动时间，用于判断是否需要重启。"""
        import tomllib as _tomllib
        from datetime import datetime, timezone

        # 读取配置中的 config_updated_at
        config_updated_at = None
        try:
            with open(CONFIG_PATH, "rb") as f:
                raw = _tomllib.load(f)
            ts = raw.get("general", {}).get("config_updated_at", "")
            if ts:
                config_updated_at = ts
        except Exception:
            pass

        # 读取 PID 文件的创建时间作为网关启动时间
        gateway_started_at = None
        pid_file = KANGCLAW_HOME / "gateway.pid"
        if pid_file.exists():
            try:
                stat = pid_file.stat()
                # macOS: st_birthtime; Linux fallback: st_mtime
                ctime = getattr(stat, "st_birthtime", stat.st_mtime)
                gateway_started_at = datetime.fromtimestamp(ctime, tz=timezone.utc).isoformat()
            except Exception:
                pass

        # 判断是否需要重启
        needs_restart = False
        if config_updated_at and gateway_started_at:
            try:
                t_config = datetime.fromisoformat(config_updated_at)
                t_gateway = datetime.fromisoformat(gateway_started_at)
                needs_restart = t_config > t_gateway
            except Exception:
                pass

        return {
            "config_updated_at": config_updated_at,
            "gateway_started_at": gateway_started_at,
            "needs_restart": needs_restart,
        }

    @app.post("/api/restart")
    async def api_restart():
        """重启网关进程。"""
        import subprocess
        logger.info("收到重启请求，正在重启网关...")

        # 重新启动自身进程
        pid_file = KANGCLAW_HOME / "gateway.pid"
        cmd = [sys.executable, "-m", "kangclaw.gateway.server"]
        # 如果有 channel_filter，传递下去
        if channel_filter:
            cmd += ["--channel", channel_filter]

        try:
            subprocess.Popen(cmd, start_new_session=True)
        except Exception as e:
            return {"error": f"启动新进程失败: {e}"}

        # 延迟关闭当前进程，让响应先返回
        async def _delayed_exit():
            await asyncio.sleep(0.5)
            pid_file.unlink(missing_ok=True)
            os._exit(0)

        asyncio.create_task(_delayed_exit())
        return {"ok": True}

    # 挂载静态文件（Web UI）
    static_dir = Path(__file__).parent.parent / "web_ui" / "static"
    if static_dir.exists():
        # 用 CSS/JS 文件的最新修改时间作为缓存版本号，文件没变则版本号不变
        _mtime = max(
            int(f.stat().st_mtime)
            for f in static_dir.iterdir()
            if f.suffix in (".css", ".js")
        ) if any(static_dir.glob("*.[cj][ss]*")) else 0
        _cache_bust = str(_mtime)
        _index_html = (static_dir / "index.html").read_text(encoding="utf-8")

        from fastapi.responses import HTMLResponse

        _spa_pages = {"model", "channels", "skills", "cron", "heartbeat"}

        @app.get("/", response_class=HTMLResponse)
        async def serve_index():
            return _index_html.replace("__CACHE_BUST__", _cache_bust)

        @app.get("/{page}", response_class=HTMLResponse)
        async def serve_spa_page(page: str):
            if page in _spa_pages:
                return _index_html.replace("__CACHE_BUST__", _cache_bust)
            from fastapi.responses import Response
            # 尝试返回静态文件，否则回退到 index.html
            f = static_dir / page
            if f.exists() and f.is_file():
                media_type = {
                    ".css": "text/css", ".js": "application/javascript",
                    ".html": "text/html", ".json": "application/json",
                    ".svg": "image/svg+xml", ".png": "image/png",
                }.get(f.suffix, "application/octet-stream")
                return Response(f.read_bytes(), media_type=media_type)
            return HTMLResponse(_index_html.replace("__CACHE_BUST__", _cache_bust))

        app.mount("/", StaticFiles(directory=str(static_dir)), name="static")

    async def _consolidate_on_disconnect(sid: str):
        """CLI 断开后后台整合历史消息。"""
        try:
            keep = config.memory.session_load_limit
            old_messages = memory.check_and_consolidate(sid, keep_count=keep)
            if old_messages:
                logger.info(f"CLI 断开，后台整合 {sid} 的 {len(old_messages)} 条旧消息")
                await agent.consolidate(sid, old_messages)
        except Exception as e:
            logger.error(f"CLI 断开整合失败 [{sid}]: {e}")

    _app = app
    return app


async def _start_single_channel(name: str, ch_config, media_manager=None):
    """启动单个渠道实例。"""
    if name == "qq":
        from kangclaw.channels.qq import QQChannel
        ch = QQChannel(ch_config, _router, media_manager)
    elif name == "feishu":
        from kangclaw.channels.feishu import FeishuChannel
        ch = FeishuChannel(ch_config, _router, media_manager)
    elif name == "dingtalk":
        from kangclaw.channels.dingtalk import DingTalkChannel
        ch = DingTalkChannel(ch_config, _router, media_manager)
    else:
        raise ValueError(f"未知渠道: {name}")

    await ch.start()
    _channels[name] = ch
    logger.info(f"{name} 渠道已启动")


async def _start_channels(config: AppConfig, channel_filter: str | None, media_manager=None):
    """启动已启用的渠道。"""
    for ch_config in config.channels:
        if not ch_config.enabled:
            continue
        if channel_filter and ch_config.name != channel_filter:
            continue

        try:
            await _start_single_channel(ch_config.name, ch_config, media_manager)
        except ImportError as e:
            _channel_errors[ch_config.name] = str(e)
            logger.warning(f"渠道 {ch_config.name} 启动失败（依赖缺失）: {e}")
        except Exception as e:
            _channel_errors[ch_config.name] = str(e)
            logger.error(f"渠道 {ch_config.name} 启动失败: {e}")


def _get_ws_url(config: AppConfig | None = None) -> str:
    if config is None:
        config = load_config()
    return f"ws://{config.web.host}:{config.web.port}/ws"


def _print_banner():
    """打印 KangClaw 启动 banner。"""
    from rich.console import Console
    from rich.text import Text

    console = Console()
    # Kang 部分和 Claw 部分，分别着色
    kang_lines = [
        r"██╗  ██╗ █████╗ ███╗   ██╗ ██████╗ ",
        r"██║ ██╔╝██╔══██╗████╗  ██║██╔════╝ ",
        r"█████╔╝ ███████║██╔██╗ ██║██║  ███╗",
        r"██╔═██╗ ██╔══██║██║╚██╗██║██║   ██║",
        r"██║  ██╗██║  ██║██║ ╚████║╚██████╔╝",
        r"╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝ ╚═════╝ ",
    ]
    claw_lines = [
        r"  ██████╗██╗      █████╗ ██╗    ██╗",
        r" ██╔════╝██║     ██╔══██╗██║    ██║",
        r" ██║     ██║     ███████║██║ █╗ ██║",
        r" ██║     ██║     ██╔══██║██║███╗██║",
        r" ╚██████╗███████╗██║  ██║╚███╔███╔╝",
        r"  ╚═════╝╚══════╝╚═╝  ╚═╝ ╚══╝╚══╝ ",
    ]

    kang_color = "#3d5a80"   # 深蓝 (logo Kang 色)
    claw_color = "#b5403a"   # 砖红 (logo Claw 色)
    for k, c in zip(kang_lines, claw_lines):
        txt = Text()
        txt.append(k, style=kang_color)
        txt.append(c, style=claw_color)
        console.print(txt)

    from kangclaw import __version__
    console.print(f" [dim]v{__version__}  ·  Local AI Assistant Framework[/dim]\n")


def run_gateway(channel_filter: str | None = None):
    """前台启动 gateway。"""
    import uvicorn

    _print_banner()

    config = load_config()
    app = create_app(config, channel_filter)

    # 写 PID 文件
    pid_file = KANGCLAW_HOME / "gateway.pid"
    pid_file.write_text(str(os.getpid()))

    def cleanup(*args):
        pid_file.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    try:
        uvicorn.run(app, host=config.web.host, port=config.web.port, log_level=config.general.log_level)
    finally:
        pid_file.unlink(missing_ok=True)


# 支持 python -m kangclaw.gateway.server 启动
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", default=None)
    args = parser.parse_args()
    run_gateway(channel_filter=args.channel)
