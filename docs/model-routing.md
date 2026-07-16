# 模型路由策略

## 设计理念

不是所有请求都需要最强的模型。通过**请求分类 + 模型分级**，在保证质量的前提下显著降低成本和延迟。

## 三级路由架构

```
用户消息
    │
    ▼
_classify_request(msg, last_reply)
    │
    ├── simple ──→ Haiku (claude-haiku-4-5)
    │   • 零工具调用
    │   • 精简 System Prompt
    │   • max_tokens = 1024
    │   • 延迟 < 1s，成本极低
    │
    ├── read ────→ Sonnet (claude-sonnet-4-6)
    │   • 可调用工具，最多 16 轮
    │   • 完整 System Prompt
    │   • max_tokens = 8192
    │   • 延迟 3-5s
    │
    └── write / complex ──→ Sonnet (claude-sonnet-4-6)
        • 可调用工具，最多 16 轮
        • 完整 System Prompt + Skill 注入
        • max_tokens = 8192
        • 延迟 5-10s
```

## 分类规则

```python
def _classify_request(msg, last_reply):
    text = msg.lower()

    # ── simple：不需要工具的简单交互 ──
    if any(kw in text for kw in _SIMPLE_GREETINGS):  # "你好"、"谢谢"
        return "simple"
    if any(kw in text for kw in _SIMPLE_CAPABILITY_PATTERNS):  # "你能做什么"
        return "simple"
    # 单字/极短回复 + 上一轮是 assistant → 视为确认，但需要工具 → 回退
    if len(text) <= 4 and last_reply:
        return "simple"  # 先按 simple，Haiku 失败后回退

    # ── complex：多步骤任务（不可降级）──
    if any(kw in text for kw in _COMPLEX_KEYWORDS):  # "PPT"、"bag分析"
        return "complex"

    # ── write：写操作 ──
    if any(kw in text for kw in _WRITE_KEYWORDS):  # "创建"、"发送"、"生成"
        return "write"

    # ── 默认：read（查询类操作）──
    return "read"
```

### 关键词列表设计

```python
# 简单问候 — 秒级响应
_SIMPLE_GREETINGS = ("你好", "hi", "hello", "早上好", "谢谢", "辛苦了")

# 复杂任务 — 不可降级，必须用最强模型
_COMPLEX_KEYWORDS = (
    "ppt", "bag分析", "需求三抓", "opp文档",
    "查故障", "enable_signal_cmd", "error_code",
)

# 写操作 — 准确性优先
_WRITE_KEYWORDS = (
    "创建", "新建", "发送", "生成", "更新", "修改",
    "删除", "取消会议", "撤回",
)
```

## 降级策略

```
simple → Haiku 尝试
    ├── 成功 → 返回结果（延迟 < 1s）
    └── 失败 → 自动降级到 Sonnet（read 模式）
```

关键设计：
- Haiku 作为 simple 请求的「第一道防线」— 处理 ~40% 的消息
- 失败时自动降级，用户**无感知**— 只是多等 2 秒
- 图片消息**强制跳过 Haiku**— Haiku 不支持视觉

## 效果数据

| 请求分类 | 占比 | 使用模型 | 平均延迟 | 相对成本 |
|----------|------|----------|----------|----------|
| simple | ~40% | Haiku | < 1s | 1x（基准） |
| read | ~35% | Sonnet | 3-5s | ~15x |
| write | ~20% | Sonnet | 5-8s | ~15x |
| complex | ~5% | Sonnet | 8-10s | ~15x |

**综合成本节约**：相比全量使用 Sonnet/Opus，路由策略节省约 **60%** 的 API 费用。

## 为什么不全部用 Opus？

| 模型 | 适用场景 | 延迟 | 成本 |
|------|----------|------|------|
| Haiku | 问候、简单问答 | < 1s | 最低 |
| Sonnet | Tool use、read/write、大多数日常任务 | 3-8s | 中等 |
| Opus | 极复杂推理（多工具组合 + 深度分析） | 8-15s | 最高 |

实践表明 Sonnet 在 Tool Use 场景下准确率与 Opus 差距很小（~95% vs ~97%），但成本仅为 1/3 ~ 1/5。仅在以下场景使用 Opus：
- PPT 生成（需要理解复杂的排版约束）
- Bag 深度分析（需要多轮工具调用 + 数据推理）
- 复杂会议调度（10+ 人的跨时区安排）

## 经验教训

1. **简单任务不要过度设计** — "你好"不需要走完整的 Agent 循环
2. **关键词分类足够好用** — 不需要训练一个分类模型，正则匹配覆盖 95% 场景
3. **失败降级比完美分类更重要** — 宁可 Haiku 误判后降级，也不要 Sonnet 误判 simple 导致能力不足
4. **图片消息记得跳过 Haiku** — 这个 bug 花了两天才发现
