# Tool Schema 设计

## 设计理念

Tool（Function Calling）是 Agent 与外部世界交互的**唯一接口**。Tool Schema 的质量直接决定 Claude 能否正确选择和调用工具。

核心原则：
1. **description 即文档** — Claude 不看代码，只看 description，必须写清楚「什么时候用、怎么用」
2. **参数默认值最大化成功率** — 能默认的都给默认值，减少 Claude 的决策负担
3. **枚举优于自由文本** — 用 enum 约束 action 参数，避免 Claude 编造不存在的操作

## 工具清单（17 个）

完整定义见 `agent/tools.py` 的 `TOOL_DEFINITIONS`。

| # | 工具名 | 类型 | 说明 |
|---|--------|------|------|
| 1 | `search_users` | 独立 | 按姓名搜索飞书用户 |
| 2 | `create_meeting` | 独立 | 创建日历会议（必选/可选/外部邮箱） |
| 3 | `query_availability` | 独立 | 查多人空闲时间 |
| 4 | `cancel_meeting` | 独立 | 取消会议（多种查找方式） |
| 5 | `feishu_action` | **聚合型** | 飞书统一操作（22个action） |
| 6 | `generate_ppt` | 独立 | PPT 生成 |
| 7 | `bag_analysis` | 聚合型 | Mviz Bag 分析（4个action） |
| 8 | `feishu_project` | 聚合型 | 飞书项目 MCP（10+子操作） |
| 9 | `search_gb_standard` | 独立 | GB 国标检索 |
| 10 | `find_skills` | 独立 | Skill 搜索推荐 |
| 11 | `generate_pm_plan` | 独立 | 项目二级计划甘特图 |
| 12 | `book_vehicle` | 独立 | 车辆预约 |
| 13 | `check_fmp_vehicles` | 独立 | FMP 空闲车辆查询 |
| 14 | `query_bag_fault` | 独立 | 故障码查询 |
| 15 | `send_image` | 独立 | 图片发送 |
| 16 | `generate_diagram` | 独立 | 图表生成 |
| 17 | `run_code` | 独立 | 代码执行沙箱 |

## 设计模式一：聚合型工具（Facade Pattern）

### 问题

飞书开放平台有 50+ API 端点。如果每个端点做一个 Tool，tool list 会爆炸到 50+，Claude 选择困难。

### 解决方案

将同类操作聚合到一个工具，通过 `action` 参数分发：

```python
{
    "name": "feishu_action",
    "description": "操作飞书（Wiki + IM + 群聊 + PPTX）。通过 action 参数选择操作：\n"
                   "• search — 搜索 Wiki\n"
                   "• read_page — 读取页面\n"
                   "• create_page — 新建页面\n"
                   "• send_message — 发送消息\n"
                   "  ... (共22个action)\n",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "read_page", "list_pages", ...],  # 22个枚举值
                "description": "操作类型",
            },
            # 各 action 的差异化参数
            "query": {"type": "string", "description": "搜索关键词（action=search时必填）"},
            "url": {"type": "string", "description": "页面URL（action=read_page时必填）"},
            "open_id": {"type": "string", "description": "接收方open_id（action=send_message发私信时必填）"},
            # ... 共 20+ 参数
        },
        "required": ["action"],  # 仅 action 必填，其他按需
    },
}
```

### 设计要点

1. **`action` 必须用 enum**，不能是自由文本 — 否则 Claude 会编造不存在的 action
2. **参数命名要体现使用场景**：`open_id` 的描述写 "action=send_message 发私信时必填"，而不是 "用户ID"
3. **参数之间的互斥关系写在 description 里**：`chat_id` 的 description 写 "发群消息时必填"，`open_id` 写 "发私信时必填"
4. **default 值要合理**：`limit=5`、`hours=24`、`max_frames=15` — 减少 Claude 的决策负担

## 设计模式二：渐进式工作流

bag_analysis 工具展示了如何设计多步骤工具：

```python
{
    "name": "bag_analysis",
    "description": (
        "分析 Mviz bag 数据。通过 action 选择操作：\n"
        "• search_streams — 搜索 stream\n"
        "• lookup_stream — 查看 stream 详情\n"
        "• resolve_url — 解析 URL 提取 MD5\n"
        "• download_topic — 下载 topic 数据\n\n"
        "典型工作流：search_streams → lookup_stream → download_topic → 分析"
    ),
}
```

设计要点：
- **description 中嵌入工作流指引** — 告诉 Claude "先做什么、再做什么"
- **每一步的输出恰好是下一步的输入** — search 返回 stream_name → lookup 需要 stream_name → download 需要 topic
- **提供加速路径** — `bag_md5` 参数是可选的，但如果已知可以跳过 resolve_url 步骤

## 设计模式三：防呆设计

### 会议创建中的「必选/可选/外部」三分法

```python
{
    "name": "create_meeting",
    "input_schema": {
        "properties": {
            "attendee_open_ids": {
                "description": "必选与会者 open_id 列表（含发起人）",
            },
            "optional_attendee_open_ids": {
                "description": "可选与会者 open_id 列表",
                "default": [],
            },
            "attendee_emails": {
                "description": "外部用户邮箱列表（非本公司飞书账号）",
                "default": [],
            },
        },
    },
}
```

这是从实际使用中演化出来的：
- v1：只有 `attendee_open_ids` → 用户反映"我想邀请外部人"
- v2：加了 `attendee_emails` → 用户反映"有些人不需要一定来"
- v3：加了 `optional_attendee_open_ids` → 最终稳定

### 防呆规则嵌入 description

```python
"description": (
    "【重要】如果与会者来自跨公司飞书群（外部用户），"
    "请在调用本工具之前先一次性询问用户所有外部成员的邮箱，"
    "不要创建会议后再逐个询问。"
)
```

这不是给 Claude 看的"可选建议"，而是**必须遵守的规则**。通过 `【重要】` 标记提升 Claude 的注意力权重。

## 设计模式四：上下文感知参数

```python
"hours": {
    "type": "integer",
    "description": "读取最近N小时消息（action=read_group_messages时可选，默认24）",
    "default": 24,
},
"start_time": {
    "type": "string",
    "description": (
        "消息起始时间。支持格式：'2025-03-20 14:00'。"
        "与 end_time 同时填写时，忽略 hours 参数。"
    ),
},
```

参数之间可能有冲突（`hours` vs `start_time+end_time`）— 必须在 description 中明确优先级规则。

## 经验教训

1. **Tool description 是 Prompt Engineering 的一部分** — 花在写 description 上的时间比写实现代码还多
2. **测试驱动的 Tool 设计** — 跑 20 次实际对话，看 Claude 在哪些参数上犯错，然后改进 description
3. **不要把业务逻辑放在 Claude 的决策里** — 比如"外部用户必须用邮箱邀请"应该写在 description 里让 Claude 遵守，而不是期望 Claude 自己推理出来
4. **default 值是一把双刃剑** — 默认值能减少对话轮次，但如果默认值不合理，Claude 可能会偷懒不填关键参数
