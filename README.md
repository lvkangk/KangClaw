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

### 启动

```bash
# 前台启动 gateway
kangclaw gateway

# 打开 Web UI
kangclaw web

# CLI 终端对话
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


## 渠道配置
建议打开 Web UI配置

### QQ 机器人

```toml
[[channel]]
name = "qq"
enabled = true
app_id = "${QQ_BOT_APPID}"
app_secret = "${QQ_BOT_APPSECRET}"
allow_from = []
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
