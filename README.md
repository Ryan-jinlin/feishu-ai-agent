# Feishu AI Agent

基于大语言模型（LLM）构建的模块化飞书 AI Agent 框架，通过 Python + WebSocket 实现智能化办公协作。

## 项目简介

这是一个面向团队协作场景的智能助理系统，将 AI 能力与飞书生态深度集成，自动化处理高频工作流，提升团队效率。

**核心特性：**
- 🧩 模块化架构：16 个工具模块，封装为 5 个可复用 Skill
- 🚀 多 API 集成：飞书开放平台、Anthropic Claude、Mviz 等
- 🔄 事件驱动：WebSocket 长连接 + 定时调度
- 📦 开箱即用：配置简单，团队内已验证稳定运行

---

## 功能模块

### 1. 会议管理
- 自动搜索参会人并查询空闲时间
- 创建/取消会议，智能发送邀请
- 支持必选/可选参会人配置

### 2. 飞书知识库 RAG 检索
- 全局知识库搜索（Wiki/文档/多维表格）
- Markdown 格式内容读取与解析
- 支持页面创建、编辑与移动

### 3. IM 消息自动化
- 私信/群消息发送与撤回
- 群聊历史消息抓取（支持时间范围过滤）
- @mention 智能识别与自动回复

### 4. Bag 数据分析
- Mviz 平台数据解析
- 车辆控制信号与动态特征提取
- 支持 Topic Echo API 自动化查询

### 5. 项目工作项管理
- 飞书项目工作项 CRUD 操作
- MOQL 语法查询支持
- 视图数据导出与分析

### 6. GB 国标检索
- L2++/L3/L4 国标条款快速检索
- 关键词智能匹配
- 原文引用与条款编号标注

### 7. PPT 生成与编辑
- 基于模板自动生成演示文稿
- 支持在线 PPTX 文件编辑
- 幻灯片内容批量替换

---

## 技术架构

```
┌─────────────────────────────────────────┐
│         Feishu AI Agent Core            │
│  (Python + WebSocket + Event Scheduler) │
└─────────────────┬───────────────────────┘
                  │
        ┌─────────┴─────────┐
        │                   │
   ┌────▼────┐        ┌─────▼─────┐
   │  Tools  │        │  Scheduler │
   │ (16个)  │        │  (定时任务) │
   └────┬────┘        └───────────┘
        │
   ┌────┴─────────────────────┐
   │                          │
┌──▼──────┐          ┌────────▼─────┐
│ Feishu  │          │  Anthropic   │
│ OpenAPI │          │  Claude API  │
└─────────┘          └──────────────┘
   │                          │
┌──▼──────────┐      ┌────────▼─────┐
│   Mviz      │      │  Other APIs  │
│ (Bag分析)   │      │ (可扩展)     │
└─────────────┘      └──────────────┘
```

---

## 快速开始

### 环境要求
- Python 3.9+
- 飞书开放平台应用（需申请 App ID 和 Secret）
- Anthropic API Key

### 安装依赖

```bash
# 克隆仓库
git clone https://github.com/Ryan-jinlin/feishu-ai-agent.git
cd feishu-ai-agent

# 安装依赖
pip install -r requirements.txt
```

### 配置

1. 复制配置模板：
```bash
cp .env.example .env
```

2. 编辑 `.env` 文件，填入必要配置：
```env
FEISHU_APP_ID=your_app_id
FEISHU_APP_SECRET=your_app_secret
ANTHROPIC_API_KEY=your_api_key
PERSONAL_ASSISTANT_DIR=/path/to/this/repo
```

3. 飞书应用权限配置（需在飞书开放平台开启）：
   - `im:message` - 接收消息
   - `im:message:send_as_bot` - 发送消息
   - `im:chat` - 群组管理
   - `calendar:calendar` - 日历访问
   - `wiki:wiki` - 知识库访问
   - `contact:user.base:readonly` - 用户信息读取

### 运行

```bash
# WebSocket 模式（推荐）
python main_ws.py

# 查看日志
tail -f bot.log
```

---

## 项目结构

```
personal-assistant/
├── agent/                  # Agent 核心逻辑
│   ├── assistant.py       # 主助理模块
│   ├── tools.py           # 工具集合（16个工具模块）
│   ├── daily_summary.py   # 每日摘要
│   └── group_activity.py  # 群活动监控
├── feishu/                # 飞书 API 封装
│   └── mcp_client.py      # MCP 客户端
├── scheduler/             # 定时任务调度
├── scripts/               # 辅助脚本
├── main.py                # HTTP 模式入口
├── main_ws.py             # WebSocket 模式入口（推荐）
├── requirements.txt       # Python 依赖
├── .env.example           # 配置模板
└── README.md              # 项目文档
```

---

## Skill 模块

本项目采用模块化 Skill 架构，核心能力已封装为独立 Skill，支持跨项目复用：

| Skill 名称 | 功能描述 |
|-----------|---------|
| `personal-assistant` | 核心助理能力（会议/消息/知识库） |
| `feishu` | 飞书基础操作（文档/多维表格/日历） |
| `cpm` | 项目管理辅助工具 |
| `cmpc-collision-analysis` | CMPC 碰撞事故分析 |
| `project-bot-customer-service` | 飞书客服 Bot 全套能力 |

详细文档参见各 Skill 的 `SKILL.md`。

---

## 使用示例

### 1. 自动创建会议

在飞书群内 @Bot：
```
@Bot 帮我安排明天下午2点和张三、李四开会，主题是项目进度同步
```

Bot 会自动：
- 搜索参会人
- 查询空闲时间
- 创建会议并发送邀请

### 2. 知识库检索

```
@Bot 搜索HNP相关的问题点检文档
```

Bot 返回相关 Wiki 页面链接和摘要。

### 3. 数据分析

```
@Bot 分析这个bag的控制信号 [bag链接]
```

Bot 自动解析 Mviz 数据并输出分析结果。

---

## 开发指南

### 添加新工具模块

1. 在 `agent/tools.py` 中定义工具函数
2. 添加到 `TOOLS` 列表
3. 在 `ToolExecutor.execute()` 中注册调用

示例：
```python
{
    "name": "your_tool",
    "description": "工具功能描述",
    "input_schema": {
        "type": "object",
        "properties": {
            "param1": {"type": "string", "description": "参数说明"}
        },
        "required": ["param1"]
    }
}
```

### 扩展 Skill

参考 `scheduler/SKILL.md`，将新能力封装为独立 Skill 模块。

---

## 常见问题

**Q: Bot 无法接收消息？**
A: 检查飞书应用权限是否完整开启，并确认 WebSocket 连接状态。

**Q: 知识库检索无结果？**
A: 确认飞书账号有目标 Wiki 的访问权限。

**Q: 如何调试？**
A: 查看 `bot.log` 日志文件，或设置环境变量 `DEBUG=1` 启用详细日志。

---

## 技术栈

- **语言**: Python 3.9+
- **核心依赖**:
  - `anthropic` - Claude API SDK
  - `requests` - HTTP 客户端
  - `websockets` - WebSocket 长连接
  - `apscheduler` - 定时任务调度
- **API 集成**:
  - 飞书开放平台 API
  - Anthropic Claude API
  - Mviz 平台 API

---

## 贡献指南

欢迎提交 Issue 和 Pull Request！

提交前请确保：
- [ ] 代码通过 `flake8` 检查
- [ ] 新功能添加了相应文档
- [ ] 敏感信息已添加到 `.gitignore`

---

## 许可证

MIT License

---

## 致谢

- [Anthropic Claude](https://www.anthropic.com/) - 提供 AI 能力
- [飞书开放平台](https://open.feishu.cn/) - 提供企业协作基础设施
- 团队成员的持续反馈与支持

---

**💡 Tip**: 本项目在团队内已稳定运行，如有问题或建议，欢迎联系作者。
