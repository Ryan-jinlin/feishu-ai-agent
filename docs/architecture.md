# 系统架构说明

## 1. 整体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        飞书服务器                                 │
│              消息事件 → WebSocket Push → Bot Server               │
└────────────────────────────────┬────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│  Layer 1: 接入层 (main_ws.py)                                    │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ • WebSocket 长连接管理（lark_oapi SDK）                     │  │
│  │ • 单实例守护（PID 文件锁 → SIGTERM → SIGKILL）              │  │
│  │ • 事件解密 + 签名验证                                       │  │
│  │ • 消息去重（message_id 缓存）                                │  │
│  │ • 消息分发：owner → PersonalAssistant.process()             │  │
│  │             others → PersonalAssistant.process_non_owner()  │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                 │                                │
│                                 ▼                                │
│  Layer 2: Agent 核心层 (agent/assistant.py)                      │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │                                                           │  │
│  │  PersonalAssistant.process(msg) → str                     │  │
│  │                                                           │  │
│  │  Step 1: _classify_request()                              │  │
│  │          分析消息 + 对话历史 → simple / read / write / complex │
│  │                                                           │  │
│  │  Step 2: 组装 System Prompt                               │  │
│  │          基础能力描述（13项，~400行）                        │  │
│  │          + 触发词命中 → 注入 Skill 指令（动态）              │  │
│  │          + @mention 信息 / 群聊上下文 / 当前时间             │  │
│  │                                                           │  │
│  │  Step 3: Model Dispatch                                   │  │
│  │          simple → Haiku（零工具，秒级响应）                  │  │
│  │          read/write/complex → Sonnet + tools + thinking    │  │
│  │                                                           │  │
│  │  Step 4: Tool Use Loop（最多 16 轮）                       │  │
│  │          Claude → tool_use → execute → result → Claude     │  │
│  │          until stop_reason == "end_turn"                   │  │
│  │                                                           │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                 │                                │
│                                 ▼                                │
│  Layer 3: 工具层 (agent/tools.py)                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │                                                           │  │
│  │  TOOL_DEFINITIONS: list[dict]   — 17 个 JSON Schema       │  │
│  │  ToolExecutor: class            — 统一的工具调度器          │  │
│  │                                                           │  │
│  │  每个 Tool Definition 包含:                                │  │
│  │    • name: str                  — 工具名                   │  │
│  │    • description: str           — 功能说明 + 使用场景       │  │
│  │    • input_schema: dict         — JSON Schema 参数定义      │  │
│  │                                                           │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                 │                                │
│                                 ▼                                │
│  Layer 4: 平台层 (feishu/ + 外部)                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ • feishu/client.py    — 飞书 OpenAPI 封装                  │  │
│  │   - Tenant Access Token 自动刷新（每 90 分钟）              │  │
│  │   - 用户 OAuth Token 管理                                   │  │
│  │   - 重试 + 限流 + 错误码处理                                │  │
│  │ • feishu/bot.py       — 消息解析（富文本/@mention/图片）     │  │
│  │ • feishu/mcp_client.py — 飞书项目 MCP 协议                  │  │
│  │ • feishu/message_cache.py — 消息去重缓存                    │  │
│  │ • feishu/fmp.py       — FMP 车辆管理 API                    │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## 2. 消息处理流程

```
用户发送消息（飞书群/私信）
    │
    ▼
飞书服务器 → WebSocket 推送事件
    │
    ▼
main_ws.py: do_handle_event()
    │
    ├── 事件类型过滤（仅处理 im.message.receive_v1）
    ├── 消息去重（message_id 缓存 5 分钟）
    ├── 解密（AES 加密模式）
    │
    ▼
feishu/bot.py: FeishuBotEventParser
    │
    ├── 解析富文本 → 提取纯文本 + @mention 列表
    ├── 下载图片 → base64 编码
    ├── 识别合并转发消息
    │
    ▼
agent/assistant.py: PersonalAssistant.process(msg)
    │
    ├── 提取历史对话（每用户独立，6 轮窗口）
    ├── 组装 System Prompt（基础 + 触发词 Skill 注入）
    ├── 分类请求 → 模型路由
    │
    ▼
Claude API 调用
    │
    ├── 无 tool_use → 直接返回文本
    └── 有 tool_use → ToolExecutor.execute() → 回传结果 → 继续循环
    │
    ▼
返回文本 → 飞书消息卡片渲染 → 发送到用户
```

## 3. 关键设计决策

### 3.1 为什么用 WebSocket 而不是 HTTP 轮询？

| 方案 | 延迟 | 资源消耗 | 可靠性 |
|------|------|----------|--------|
| HTTP 轮询（5s） | 0-5s | 高（空轮询浪费） | 中 |
| HTTP 长轮询 | 实时 | 中 | 中 |
| **WebSocket** | **实时** | **低（事件驱动）** | **高（飞书官方 SDK）** |

选择 WebSocket 因为它是飞书官方推荐的事件订阅方式，lark_oapi SDK 封装了重连、心跳等逻辑。

### 3.2 为什么用一个大的 `feishu_action` 而不是 20+ 个小工具？

Claude 的 tool choice 是在 17 个工具中做选择。如果飞书的 20+ 操作各自独立为工具，tool list 会膨胀到 40+，增加选择难度。

将飞书操作统一为 `feishu_action(action=xxx)` 后：
- Tool list 保持 17 个（精简）
- `action` 枚举提供明确的选项列表（22 个值）
- Claude 先选工具，再决定 action，两级决策更精准

### 3.3 Skill 触发词注入 vs 始终加载

**设计选择**：基础能力始终在 System Prompt 中，专项 Skill 通过触发词动态注入。

**理由**：
- 始终加载所有 Skill 会让 System Prompt 膨胀到 5000+ 行，大部分不相关
- 动态注入保持 Prompt 精简，降低 Claude 的注意力分散
- 触发词匹配在应用层做（`combined_text = msg + history`），包含历史对话上下文

### 3.4 单实例守护

WebSocket 连接要求单实例运行，否则：
- 多个进程共享同一份 Token 文件 → 文件写入竞态
- 多条 WebSocket 连接争抢消息 → 重复处理和回复

解决方案：
- 启动时检查 PID 文件 → 杀死旧进程 → 写入新 PID
- 进程退出时 `atexit` 清理 PID 文件
- 先 SIGTERM（优雅退出）→ 1 秒后 SIGKILL（强制终止）

## 4. 数据流示意

```
用户: "帮我约张三明天下午2点开会讨论Q2规划"
  │
  ▼
main_ws.py: 接收消息 → 解析 → 分发
  │
  ▼
agent/assistant.py: _classify_request()
  → "write"（含"约"、创建类关键词）
  → 模型路由：Sonnet + tools
  │
  ▼
Claude API 第1轮:
  → tool_use: search_users(keyword="张三")  → 返回 open_id: ou_xxx
  ▼
Claude API 第2轮:
  → tool_use: query_availability(users=[ou_xxx], date="2026-07-16")
  → 返回: 14:00-15:00 空闲
  ▼
Claude API 第3轮:
  → text: "张三明天14:00-15:00空闲，需要我创建会议吗？"
  ▼
用户: "好的"
  → _classify_request() → "simple"（单字确认，结合历史判断）
  → 回退 to "write"（历史中有 pending 操作）
  ▼
Claude API:
  → tool_use: create_meeting(title="Q2规划讨论", ...)
  → 返回: 会议已创建，calendar_id=xxx, event_id=yyy
  ▼
用户收到: "# ✅ 会议已创建\n| 标题 | Q2规划讨论 |\n| 时间 | ..."
```

## 5. 扩展性设计

新增一个 Skill 只需三步（不改核心代码）：

```
1. 创建 SKILL.md（指令内容）
2. 在 agent/assistant.py 中添加：
   _MY_SKILL_CONTENT = _load_simple_skill("my-skill")
   _MY_SKILL_TRIGGERS = ("触发词1", "触发词2")
3. 添加触发词匹配逻辑（复制已有模式）
```

核心循环（Tool Use Loop）保持不变，所有 Skill 通过 System Prompt 注入影响 Agent 行为。
