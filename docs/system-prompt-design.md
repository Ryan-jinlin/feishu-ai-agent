# System Prompt 设计

## 设计理念

System Prompt 是 Agent 的「操作系统」。它告诉 Claude：
- **你是谁**（角色定位）
- **你能做什么**（能力清单）
- **怎么做**（工作流 + 规则约束）
- **不能做什么**（边界）

本 Agent 的 System Prompt 采用**分层注入架构**：

```
┌──────────────────────────────────────┐
│  Layer 0: 时间/用户上下文             │  ← 动态生成
│  "今天是2026年7月15日，现在是14:30"    │
├──────────────────────────────────────┤
│  Layer 1: 基础能力描述（始终在线）      │  ← agent/assistant.py 内置
│  13项能力 × 工作流 × 规则约束          │     ~400 行
├──────────────────────────────────────┤
│  Layer 2: Skill 指令注入（触发词匹配）  │  ← skills/ SKILL.md 文件
│  PRT/OPP/CPM/feishu-project/...      │     按需加载
├──────────────────────────────────────┤
│  Layer 3: 上下文注入                  │  ← 运行时注入
│  @mention列表 / 群chat_id / 历史对话   │
└──────────────────────────────────────┘
```

## Layer 1: 基础能力描述结构

基础 System Prompt 按以下层次组织（详见 `agent/assistant.py` 的 `system_prompt` 变量）：

### 1. 角色定位（2行）
```
你是 {owner_name} 的飞书个人助理机器人。今天是 {date_str}，现在是 {time_str}（北京时间）。
```

### 2. 能力清单（13项，每项包含）
```
### N. 能力名称
简要描述

**工作流**：
1. 步骤1 → 工具A
2. 步骤2 → 工具B
3. 步骤3 → 完成

**规则**：
- 约束1
- 约束2

**触发词**：关键词列表
```

能力列表：
1. **会议邀请** — 最复杂的能力，含工作流A（指定人员）和工作流B（群内全员）
2. **飞书知识库 + IM 消息** — feishu_action 的 22 个 action 详解
3. **客户汇报 PPT 生成** — search → read → generate → upload
4. **修改已有 PPTX** — inspect_pptx → edit_pptx 强制编辑
5. **需求三抓 PRT** — 三种模式 + 质量 Review
6. **OPP 计划沟通** — OKR-Plan-Progress 文档
7. **飞书项目工作项管理** — MCP 协议的 10+ 子操作
8. **Mviz Bag 数据分析** — search_streams → lookup → download
9. **GB 国标检索** — 先检索原文，禁止凭记忆回答
10. **Skill 搜索** — find_skills 工具
11. **项目二级计划生成** — 甘特图
12. **车辆预约** — Fleet-Bot 集成
13. **故障码查询** — VIN → ESS → enable_signal_cmd

### 3. 回复格式规范（19条规则）

这是 Prompt 工程的关键部分——约束 Claude 的输出格式：

```
1. 开头必须用 # 标题（5-15字）
2. 结构化数据必须用 Markdown 表格
3. 枚举多个条目用数字序号
4. ...
17. 单字回复结合历史解读，不要反问
18. 工具调用结果绝对事实，禁止推翻
19. 撤回消息优先从历史查找 message_id
```

### 4. 边界约束

```
- 不要编造 open_id 或飞书 URL
- 生成 PPT 前先从飞书收集内容
- 发送消息必须调用工具，不能只写在回复里
- GB 问题必须先检索原文
- 信息不全时先询问，不得猜测
```

## Layer 2: Skill 动态注入

### 注入机制

```python
# agent/assistant.py 中的触发词匹配逻辑
combined_text = (msg.clean_text + history_text).lower()

if any(kw.lower() in combined_text for kw in _CPM_TRIGGERS):
    system_prompt += f"\n\n---\n\n{_CPM_SKILL_CONTENT}"

if any(kw.lower() in combined_text for kw in _FEISHU_PROJECT_TRIGGERS):
    system_prompt += f"\n\n---\n\n{_FEISHU_PROJECT_SKILL_CONTENT}"
# ... 共 9 个 Skill 的触发词匹配
```

### 为什么用触发词而不是语义匹配？

| 方案 | 延迟 | 准确性 | 成本 |
|------|------|--------|------|
| 触发词匹配 | 0ms | 高（确定性强） | 零 |
| LLM 判断 | 200-500ms | 中（可能误判） | 额外 API 调用 |
| Embedding 相似度 | 50ms | 中 | 需要向量库 |

选择触发词匹配：简单、可靠、零成本、可预期。

### 触发词设计原则

```python
# 好例子：覆盖面广，包含中英文、缩写、同义词
_CPM_TRIGGERS = (
    "Project-MXS", "vas", "车型适配", "cma", "ota", "发版", "准出",
    "p301", "c095", "c100", "c255",  # 车型代号
    "odc", "锁版", "toc", "sop", "ccm", "客户问题",
    "cpm", "ppm", "fst", "fit", "mtbf",
)

# 不好的例子：太窄或太泛
_BAD_TRIGGERS = ("帮助", "问题")  # 太泛，什么消息都会命中
```

## Layer 3: 上下文注入

```
# @mention 信息
消息中 @mention 的用户（可直接用其 open_id 作为与会者）：
  - 张三，open_id: ou_xxx
  - 李四，open_id: ou_yyy

# 群聊上下文
【当前消息来自群聊，chat_id = `oc_xxx`】
可直接用此 chat_id 调用 feishu_action(read_group_messages) 读取本群消息

# Owner 提示
发起人 Ryan 的 open_id 为 `ou_zzz`，必须加入与会者列表。
```

## 设计心得

1. **Prompt 需要版本管理**：19条格式规则不是一次写成的，是持续使用中发现 Claude 的「坏习惯」后逐条追加的
2. **规则之间会冲突**：如"信息不全先询问"和"单字回复结合历史"可能冲突，需要设置优先级
3. **越具体的约束越有效**："不要编造 open_id" 比 "要诚实" 效果好得多
4. **动态注入优于全量加载**：保持核心 Prompt 精简，专项指令按需注入，显著降低 Claude 的「注意力分散」
