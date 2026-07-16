---
name: project-bot-customer-service
description: 飞书客服 Bot 全套工具能力。涵盖日历查询与会议管理、飞书项目工作项查询/创建/更新、发消息（私信 & 群内@）、飞书文档读写、PPT生成与发送、群聊历史抓取、Wiki 访问权限控制，以及网络搜索共 14 项能力。持续更新中。
category: feishu-bot
author: claude
---

# project-bot-customer-service

## When to Use This Skill

- 查询某人某天的飞书日历会议安排
- 创建或删除飞书会议并邀请参会人
- 查询/创建/更新飞书项目工作项（需求、缺陷、任务等）
- 向飞书用户发送私信或在群内 @某人
- 读取/搜索飞书文档、Wiki 或多维表格内容
- 在飞书上新建文档并写入内容
- 执行 Python/Node.js 脚本生成 PPT 并发送给用户
- 抓取飞书群聊历史消息（用于日报/周报/汇总）
- 管理各群的 Wiki 空间访问白名单
- 网络搜索（Claude 内置能力）

---

## 工具能力总览（14 项）

| # | 工具名 | 功能 | 实现模块 |
|---|--------|------|----------|
| 1 | `get_calendar_events` | 查询指定日期的飞书日历会议 | `calendar_skill.py` |
| 2 | `feishu_project` | 查询/创建/更新飞书项目工作项（MOQL 查询等） | `feishu_project_skill.py` |
| 3 | `create_meeting` | 在飞书日历创建会议并发送邀请 | `meeting_skill.py` |
| 4 | `delete_meeting` | 按标题关键词删除飞书会议 | `meeting_skill.py` |
| 5 | `send_direct_message` | 向指定用户发送私信 | `message_skill.py` |
| 6 | `send_group_at_message` | 在群聊中 @某成员并发消息 | `message_skill.py` |
| 7 | `run_python_script` | 执行 Python 脚本（生成/编辑 PPT 等） | 内置 subprocess |
| 8 | `run_node_script` | 执行 Node.js 脚本（pptxgenjs 生成 PPT） | 内置 subprocess |
| 9 | `send_feishu_file` | 上传本地文件并发送到飞书对话 | bot_ws.py 内置 |
| 10 | `fetch_chat_history` | 抓取飞书群聊/单聊历史消息 | `feishu-chat-processor` skill |
| 11 | `feishu_sync` | 读取/搜索飞书文档、Wiki、妙记等 | `feishu-sync-cli` |
| 12 | `create_feishu_doc` | 新建飞书云文档或 Wiki 子页面 | `doc_skill.py` |
| 13 | `access_control` | 管理群聊的 Wiki 空间访问白名单 | `feishu-bot-access-control` skill |
| 14 | `web_search` | 网络搜索（Claude 内置，最多 5 次/轮） | Claude built-in |

---

## 工具详情与调用示例

### 1. `get_calendar_events` — 查询日历

查询指定用户某天的日历事件。

```bash
python3 run_tool.py get_calendar_events --date 2026-03-20 --user_name "张三"
```

### 2. `feishu_project` — 飞书项目工作项

支持 MOQL 语法查询、创建、更新工作项。

```bash
python3 run_tool.py feishu_project --action query --moql "project_key = 'MXS' AND status != '已关闭'"
```

### 3. `create_meeting` — 创建会议

```bash
python3 run_tool.py create_meeting \
  --title "项目周会" \
  --start "2026-03-20T14:00:00" \
  --end "2026-03-20T15:00:00" \
  --attendees "张三,李四"
```

### 4. `delete_meeting` — 删除会议

按标题关键词查找并删除会议。

### 5-6. 消息发送

- `send_direct_message` — 发送私信
- `send_group_at_message` — 群聊中 @某人

### 7-8. 脚本执行

- `run_python_script` — 执行 Python 脚本
- `run_node_script` — 执行 Node.js 脚本

### 9. `send_feishu_file` — 文件发送

上传本地文件并发送到飞书对话。

### 10. `fetch_chat_history` — 历史消息抓取

抓取群聊/单聊历史消息，支持时间范围过滤。

### 11. `feishu_sync` — 文档读写

底层调用 `feishu-sync-cli` 实现飞书文档搜索和读取。

### 12. `create_feishu_doc` — 新建文档

在飞书知识库中创建新的云文档或 Wiki 子页面。

### 13. `access_control` — 权限管理

管理各群聊对应的 Wiki 空间访问白名单。

### 14. `web_search` — 网络搜索

Claude 内置网络搜索能力，最多 5 次/轮。

---

## 使用说明

**⚠️ 重要提示**：此 Skill 依赖 Momenta 企业飞书账号和内部系统，离职后无法运行。代码架构和设计模式可用于技术能力证明。

**配置文件**：`.env`
```env
ANTHROPIC_API_KEY=sk-xxx
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_PROJECT_USER_KEY=m-xxx
```

**启动方式**：
```bash
python run_tool.py
```
