<p align="center">
  <img src="logo.png" width="280" alt="KangClaw Logo" />
</p>

<h1 align="center">KangClaw</h1>

<p align="center">
  <b>个人AI智能助手</b><br/>
  一个 Gateway，多端接入
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-≥3.11-blue" alt="Python" />
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License" />
  <img src="https://img.shields.io/badge/version-0.1.0-orange" alt="Version" />
</p>

---

## 特性

- **多模型支持** — OpenAI / Anthropic / Google Gemini / DeepSeek / Ollama 等 LLM 供应商
- **多渠道接入** — 终端 CLI、Web UI、QQ 机器人、飞书机器人、钉钉机器人等
- **内置工具** — 文件读写编辑、Shell 执行、Web 搜索与抓取、定时任务、图片处理、消息发送
- **智能记忆** — 短期记忆 + 长期记忆，让智能体记住更多重要信息
- **技能系统** — 通过Skills扩展智能体功能，实现更复杂的任务

## 快速开始

### 安装

```bash
pip install -e .
```

### 初始化

```bash
kangclaw init
```

这会在 `~/.kangclaw/` 下创建配置文件和工作区：

```
~/.kangclaw/
├── config.toml          # 全局配置（模型、渠道、参数等）
└── workspace/
    ├── AGENTS.md        # 智能体行为指令
    ├── SOUL.md          # 助手人格
    ├── USER.md          # 用户画像
    ├── HEARTBEAT.md     # 心跳任务表
    ├── memory/
    │   ├── MEMORY.md    # 长期记忆
    │   └── sessions/    # 会话历史 (JSONL)
    ├── skills/          # 自定义技能
    └── cron/            # 定时任务
```

### 配置模型

编辑 `~/.kangclaw/config.toml`，填入你的 API Key：

```toml
[[model]]
primary_key = "default"
id = "gpt-4o"
show_name = "GPT-4o"
provider = "openai"
api_key = "${OPENAI_API_KEY}"
base_url = ""
context_window_tokens = 128000
```

支持使用环境变量 `${ENV_VAR}` 来管理敏感信息。

### 启动

```bash
# 前台启动 gateway
kangclaw gateway

# 后台启动
kangclaw gateway -d

# 打开 Web UI
kangclaw web

# 终端对话
kangclaw chat
```

## CLI 命令

| 命令 | 说明 |
|------|------|
| `kangclaw init` | 初始化配置和工作区 |
| `kangclaw gateway` | 启动 gateway 服务（前台） |
| `kangclaw gateway -d` | 后台启动 gateway |
| `kangclaw gateway status` | 查看 gateway 状态 |
| `kangclaw gateway stop` | 停止 gateway |
| `kangclaw gateway restart` | 重启 gateway |
| `kangclaw chat` | 终端对话客户端 |
| `kangclaw web` | 打开 Web UI |
| `kangclaw status` | 查看整体状态 |
| `kangclaw skills list` | 列出已安装技能 |
| `kangclaw cron list` | 查看定时任务 |
| `kangclaw cron remove <id>` | 删除定时任务 |

## 架构

```
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│   终端 CLI   │  │   Web UI     │  │  QQ/飞书/钉钉 │
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │                 │                 │
       └────────┬────────┴────────┬────────┘
                │                 │
         ┌──────▼──────┐   ┌─────▼─────┐
         │  WebSocket  │   │  Channels  │
         └──────┬──────┘   └─────┬─────┘
                │                │
         ┌──────▼────────────────▼──────┐
         │          Router              │
         └──────────────┬───────────────┘
                        │
         ┌──────────────▼───────────────┐
         │           Agent              │
         │  ┌───────┐ ┌──────────────┐  │
         │  │ Tools │ │ Memory Mgr   │  │
         │  └───────┘ └──────────────┘  │
         │  ┌───────────────────────┐   │
         │  │   LLM (LangChain)    │   │
         │  └───────────────────────┘   │
         └──────────────────────────────┘
```

**Gateway 模式**：一个 FastAPI 进程统一管理 LLM 交互、会话记忆、工具执行、定时任务和多渠道消息。Agent 使用自定义工具调用循环（非 LangChain AgentExecutor），支持流式输出、并发会话锁和消息队列。

## 内置工具

| 工具 | 说明 |
|------|------|
| `read_file` | 读取文件内容 |
| `write_file` | 写入文件 |
| `edit_file` | 编辑文件（精确替换） |
| `list_files` | 列出目录文件 |
| `grep_file` | 搜索文件内容 |
| `exec_command` | 执行 Shell 命令 |
| `web_search` | Web 搜索（DuckDuckGo） |
| `web_fetch` | 抓取网页内容 |
| `cron_list` | 查看定时任务 |
| `cron_add` | 添加定时任务 |
| `cron_remove` | 删除定时任务 |
| `image_filter` | 图片滤镜处理 |
| `image_watermark` | 图片加水印 |
| `image_convert` | 图片格式转换 |
| `send_image` | 发送图片到渠道 |

## 渠道配置

### QQ 机器人

```toml
[[channel]]
name = "qq"
enabled = true
app_id = "${QQ_BOT_APPID}"
app_secret = "${QQ_BOT_APPSECRET}"
allow_from = []   # 空数组表示不限制
```

### 飞书机器人

```toml
[[channel]]
name = "feishu"
enabled = true
app_id = "${FEISHU_APP_ID}"
app_secret = "${FEISHU_APP_SECRET}"
allow_from = []
```

### 钉钉机器人

```toml
[[channel]]
name = "dingtalk"
enabled = true
client_id = "${DINGTALK_CLIENT_ID}"
client_secret = "${DINGTALK_CLIENT_SECRET}"
allow_from = []
```

## 技能系统

在 `~/.kangclaw/workspace/skills/` 下创建目录，添加 `SKILL.md` 即可注册技能：

```
skills/
└── weather/
    └── SKILL.md
```

`SKILL.md` 使用 YAML frontmatter 定义元信息，正文为技能的使用说明和提示词。

## 开发

```bash
# 安装开发依赖
pip install -e .

# 运行测试
pytest tests/
```

## 许可证

[MIT](LICENSE)