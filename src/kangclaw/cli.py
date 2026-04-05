"""kangclaw CLI 入口，基于 Typer。"""

from __future__ import annotations

import os
import signal
import shutil
import subprocess
import sys
import webbrowser
from importlib import resources
from pathlib import Path

import typer
from rich.console import Console

from kangclaw.config import KANGCLAW_HOME, CONFIG_PATH, load_config

app = typer.Typer(help="kangclaw - 本地智能体框架")
console = Console()
gateway_app = typer.Typer(help="Gateway 服务管理")
skills_app = typer.Typer(help="技能管理")
cron_app = typer.Typer(help="定时任务管理")
app.add_typer(gateway_app, name="gateway")
app.add_typer(skills_app, name="skills")
app.add_typer(cron_app, name="cron")

PID_FILE = KANGCLAW_HOME / "gateway.pid"


# ── init ────────────────────────────────────────────────────────────

@app.command()
def init():
    """初始化 ~/.kangclaw/ 目录及默认配置。"""
    workspace = KANGCLAW_HOME / "workspace"
    dirs = [
        KANGCLAW_HOME,
        workspace,
        workspace / "memory" / "sessions",
        workspace / "skills",
        workspace / "cron",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)

    # 从 package data 复制默认文件
    defaults_pkg = resources.files("kangclaw") / "defaults"
    file_map = {
        "config.toml": CONFIG_PATH,
        "AGENTS.md": workspace / "AGENTS.md",
        "SOUL.md": workspace / "SOUL.md",
        "USER.md": workspace / "USER.md",
        "HEARTBEAT.md": workspace / "HEARTBEAT.md",
        "MEMORY.md": workspace / "memory" / "MEMORY.md",
    }
    for src_name, dst_path in file_map.items():
        if not dst_path.exists():
            src = defaults_pkg / src_name
            dst_path.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            console.print(f"  [green]创建[/green] {dst_path}")
        else:
            console.print(f"  [dim]跳过[/dim] {dst_path} (已存在)")

    # 初始化 sessions.json
    sessions_json = workspace / "memory" / "sessions.json"
    if not sessions_json.exists():
        sessions_json.write_text("[]", encoding="utf-8")

    # 初始化 cron.json
    cron_json = workspace / "cron" / "cron.json"
    if not cron_json.exists():
        cron_json.write_text("[]", encoding="utf-8")

    console.print("\n[bold green]✓ kangclaw 初始化完成！[/bold green]")
    console.print(f"  配置文件: {CONFIG_PATH}")
    console.print(f"  工作区:   {workspace}")


# ── gateway ─────────────────────────────────────────────────────────

@gateway_app.callback(invoke_without_command=True)
def gateway_start(
    ctx: typer.Context,
    channel: str = typer.Option(None, help="仅启动指定渠道"),
    daemon: bool = typer.Option(False, "--daemon", "-d", help="后台运行"),
):
    """启动 gateway 服务。"""
    if ctx.invoked_subcommand is not None:
        return

    if _is_gateway_running():
        console.print("[yellow]gateway 已在运行中[/yellow]")
        raise typer.Exit()

    if daemon:
        # 后台启动：通过 subprocess 以 detach 方式启动
        proc = subprocess.Popen(
            [sys.executable, "-m", "kangclaw.gateway.server", *(["--channel", channel] if channel else [])],
            stdout=open(KANGCLAW_HOME / "gateway.log", "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        PID_FILE.write_text(str(proc.pid))
        console.print(f"[green]gateway 已后台启动 (PID: {proc.pid})[/green]")
    else:
        # 前台启动
        from kangclaw.gateway.server import run_gateway
        run_gateway(channel_filter=channel)


@gateway_app.command("status")
def gateway_status():
    """查看 gateway 状态。"""
    if _is_gateway_running():
        pid = PID_FILE.read_text().strip()
        console.print(f"[green]gateway 运行中 (PID: {pid})[/green]")
    else:
        console.print("[dim]gateway 未运行[/dim]")


@gateway_app.command("stop")
def gateway_stop():
    """停止 gateway 服务。"""
    if not _is_gateway_running():
        console.print("[dim]gateway 未运行[/dim]")
        return
    pid = int(PID_FILE.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        console.print(f"[green]已发送 SIGTERM 到 PID {pid}[/green]")
    except ProcessLookupError:
        pass
    PID_FILE.unlink(missing_ok=True)


@gateway_app.command("restart")
def gateway_restart(
    channel: str = typer.Option(None, help="仅启动指定渠道"),
    daemon: bool = typer.Option(False, "--daemon", "-d", help="后台运行"),
):
    """重启 gateway 服务。"""
    gateway_stop()
    import time
    time.sleep(1)

    if _is_gateway_running():
        console.print("[yellow]gateway 已在运行中[/yellow]")
        raise typer.Exit()

    if daemon:
        proc = subprocess.Popen(
            [sys.executable, "-m", "kangclaw.gateway.server", *(["--channel", channel] if channel else [])],
            stdout=open(KANGCLAW_HOME / "gateway.log", "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        PID_FILE.write_text(str(proc.pid))
        console.print(f"[green]gateway 已后台启动 (PID: {proc.pid})[/green]")
    else:
        from kangclaw.gateway.server import run_gateway
        run_gateway(channel_filter=channel)


def _is_gateway_running() -> bool:
    if not PID_FILE.exists():
        return False
    pid = int(PID_FILE.read_text().strip())
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        PID_FILE.unlink(missing_ok=True)
        return False


# ── chat ────────────────────────────────────────────────────────────

@app.command()
def chat():
    """终端客户端，连接 gateway 进行对话。"""
    import asyncio
    asyncio.run(_chat_loop())


async def _chat_loop():
    from websockets.asyncio.client import connect
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import HTML

    cfg = load_config()
    ws_url = f"ws://{cfg.web.host}:{cfg.web.port}/ws?client=cli"
    session = PromptSession()

    try:
        async with connect(ws_url) as ws:
            console.print(f"[green]已连接 gateway ({ws_url})[/green]")
            # 让 AI 主动打招呼
            await ws.send("/greeting")
            first_token = True
            while True:
                msg = await ws.recv()
                if msg == "[DONE]":
                    break
                if first_token:
                    first_token = False
                    console.print("[bold green]AI>[/bold green] ", end="")
                print(msg, end="", flush=True)
            print()

            while True:
                try:
                    user_input = await session.prompt_async(HTML('<cyan><b>你&gt; </b></cyan>'))
                except (EOFError, KeyboardInterrupt):
                    console.print("\n[dim]再见[/dim]")
                    break

                if not user_input.strip():
                    continue

                await ws.send(user_input)
                # 显示思考中状态
                spinner = console.status("")
                spinner.start()
                first_token = True
                # 接收流式回复
                full_reply = []
                while True:
                    msg = await ws.recv()
                    if msg == "[DONE]":
                        break
                    if msg == "[ERROR]":
                        if first_token:
                            spinner.stop()
                            first_token = False
                        error_detail = await ws.recv()
                        console.print(f"[red]错误: {error_detail}[/red]")
                        break
                    if first_token:
                        spinner.stop()
                        first_token = False
                        console.print("[bold green]AI>[/bold green] ", end="")
                    full_reply.append(msg)
                    print(msg, end="", flush=True)
                if first_token:
                    spinner.stop()
                print()  # 换行
    except ConnectionRefusedError:
        console.print("[red]无法连接 gateway，请先运行: kangclaw gateway[/red]")
    except Exception as e:
        console.print(f"[red]连接错误: {e}[/red]")


# ── web ─────────────────────────────────────────────────────────────

@app.command()
def web(port: int = typer.Option(None, help="自定义端口")):
    """启动 Web UI，自动打开浏览器。"""
    cfg = load_config()
    actual_port = port or cfg.web.port
    url = f"http://{cfg.web.host}:{actual_port}"
    console.print(f"[green]Web UI: {url}[/green]")
    webbrowser.open(url)


# ── status ──────────────────────────────────────────────────────────

@app.command()
def status():
    """查看整体状态。"""
    cfg = load_config()
    console.print("[bold]kangclaw 状态[/bold]")
    console.print(f"  配置目录: {KANGCLAW_HOME}")
    console.print(f"  工作区:   {cfg.general.workspace}")
    from kangclaw.config import get_active_model
    active = get_active_model(cfg)
    if active:
        model_display = f"{active.show_name or active.id} ({active.provider})"
    else:
        model_display = "[dim]未配置[/dim]"
    console.print(f"  模型:     {model_display}")
    console.print(f"  已配置模型: {len(cfg.models)} 个")
    console.print(f"  Web:      {'启用' if cfg.web.enabled else '禁用'} ({cfg.web.host}:{cfg.web.port})")
    console.print(f"  心跳:     {'启用' if cfg.heartbeat.enabled else '禁用'} (每 {cfg.heartbeat.interval_minutes} 分钟)")

    gateway_running = _is_gateway_running()
    console.print(f"  Gateway:  {'[green]运行中[/green]' if gateway_running else '[dim]未运行[/dim]'}")

    for ch in cfg.channels:
        console.print(f"  渠道 {ch.name}: {'启用' if ch.enabled else '禁用'}")


# ── skills ──────────────────────────────────────────────────────────

@skills_app.command("list")
def skills_list():
    """列出已安装的技能。"""
    cfg = load_config()
    skills_dir = Path(cfg.general.workspace) / "skills"
    if not skills_dir.exists():
        console.print("[dim]无已安装技能[/dim]")
        return

    found = False
    for skill_dir in sorted(skills_dir.iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if skill_dir.is_dir() and skill_md.exists():
            found = True
            # 读取第一行作为标题
            first_line = skill_md.read_text(encoding="utf-8").split("\n")[0].strip("# ").strip()
            console.print(f"  [cyan]{skill_dir.name}[/cyan] — {first_line}")

    if not found:
        console.print("[dim]无已安装技能[/dim]")


# ── cron ────────────────────────────────────────────────────────────

@cron_app.command("list")
def cron_list():
    """查看定时任务列表。"""
    import json
    cfg = load_config()
    cron_file = Path(cfg.general.workspace) / "cron" / "cron.json"
    if not cron_file.exists():
        console.print("[dim]无定时任务[/dim]")
        return

    jobs = json.loads(cron_file.read_text(encoding="utf-8"))
    if not jobs:
        console.print("[dim]无定时任务[/dim]")
        return

    for job in jobs:
        console.print(f"  [{job['id']}] {job['cron_expr']} — {job['description']}")


@cron_app.command("add")
def cron_add(description: str = typer.Argument(..., help="任务描述")):
    """添加定时任务（需自行指定 cron 表达式，或通过对话让智能体解析）。"""
    console.print("[yellow]提示: 建议通过对话方式创建定时任务，智能体会自动解析自然语言为 cron 表达式。[/yellow]")
    console.print(f"  例如: 在对话中说 \"帮我设置{description}\"")


@cron_app.command("remove")
def cron_remove(job_id: str = typer.Argument(..., help="任务 ID")):
    """删除定时任务。"""
    import json
    cfg = load_config()
    cron_file = Path(cfg.general.workspace) / "cron" / "cron.json"
    if not cron_file.exists():
        console.print("[red]无定时任务文件[/red]")
        return

    jobs = json.loads(cron_file.read_text(encoding="utf-8"))
    new_jobs = [j for j in jobs if j["id"] != job_id]
    if len(new_jobs) == len(jobs):
        console.print(f"[red]未找到任务: {job_id}[/red]")
        return

    cron_file.write_text(json.dumps(new_jobs, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"[green]已删除任务: {job_id}[/green]")


# ── 入口 ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app()
