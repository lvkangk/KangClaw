"""命令执行工具。"""

from __future__ import annotations

import asyncio

from langchain_core.tools import tool


@tool
def exec_command(command: str, timeout: int = 60) -> str:
    """执行终端命令，返回输出结果。

    Args:
        command: 要执行的 shell 命令
        timeout: 超时时间（秒），默认 60
    """
    async def _run():
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            output = stdout.decode("utf-8", errors="replace")
            # 截断过长输出
            if len(output) > 10000:
                output = output[:10000] + "\n... (输出已截断)"
            return_code = proc.returncode
            if return_code != 0:
                return f"[退出码 {return_code}]\n{output}"
            return output
        except asyncio.TimeoutError:
            proc.kill()
            return f"错误：命令执行超时（{timeout}秒）"
        except Exception as e:
            return f"执行失败：{e}"

    # 如果当前有事件循环则用它，否则新建
    try:
        loop = asyncio.get_running_loop()
        # 已在 async 上下文中，创建 task
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = loop.run_in_executor(pool, lambda: asyncio.run(_run()))
            # 这里我们需要同步返回，所以用另一种方式
        return asyncio.run(_run())
    except RuntimeError:
        return asyncio.run(_run())
