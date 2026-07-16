# 工程特色机制

> 本文档列出本 Agent 系统中**超出常规 Tool Use 循环**的工程巧思。
> 每个机制都标注了**解决的问题**、**实现方式**和**代码位置**。

---

## 一、可靠性机制

### 1. RSVP 实时追踪 + 拒绝即时通知

**问题**：创建会议后，Claude 不知道谁接受了、谁拒绝了。用户需要手动检查日历。

**方案**：
- 创建会议时将事件信息写入 `_PENDING_EVENTS` dict 并持久化到 `.pending_events.json`
- 后台 `BackgroundScheduler` 每 **5 分钟**轮询所有待追踪事件的 RSVP 状态
- 检测到状态变化（`needs_action → accepted/declined`）时更新本地记录
- **有人拒绝 → 立刻 DM 通知 Owner**，附带会议标题和建议重新安排
- 事件创建超过 **7 天**自动过期清理

**代码位置**：`main_ws.py:517-554`（`_check_rsvp_changes`），`agent/tools.py:1304-1313`（事件登记）

```
创建会议 → 登记到 _PENDING_EVENTS → 持久化 JSON
                ↓
         每5分钟轮询飞书API
                ↓
       RSVP变化? → 是 → 拒绝? → DM通知Owner
                ↓ 否
            继续等待
```

---

### 2. 单实例守护（PID 文件锁）

**问题**：WebSocket 长连接要求单实例。如果用户误启动两个进程，会导致：
- 共享 Token 文件写入竞态损坏
- 两条 WebSocket 连接同时回复同一消息

**方案**：
1. 启动时读取 `.bot.pid`，若旧进程存活 → `SIGTERM` → 等 1 秒 → `SIGKILL`
2. 写入当前 PID
3. `atexit` 注册清理函数，进程退出时删除 PID 文件
4. 处理 `PermissionError` / `ProcessLookupError` 等边界情况

**代码位置**：`main_ws.py:22-47`（`_ensure_single_instance`）

---

### 3. feishu-sync CLI 调用串行化锁

**问题**：`feishu-sync-cli` 读写同一份 Token 文件。多线程并发调用时产生**文件竞态**，导致 Token 损坏。

**方案**：所有对 `feishu-sync-cli` 的调用都经过 `_FEISHU_CLI_LOCK`（`threading.Lock()`）。

**代码位置**：`agent/tools.py:118`（锁定义），`agent/tools.py:1917`（使用点）

---

### 4. 消息去重

**问题**：飞书 WebSocket 在某些网络条件下可能**重放消息**，导致 Bot 重复回复。

**方案**：内存中维护 `_processed_ids: set`（最多 1000 条），处理前检查 `message_id`。

**代码位置**：`main_ws.py:104-105`

---

## 二、性能优化机制

### 5. 三级模型路由 + 自动降级

**问题**："你好" 和 "帮我分析这个 Bag 数据" 不该用同一个模型处理。全用 Opus 太贵，全用 Haiku 太弱。

**方案**：

| 请求类型 | 模型 | 占比 | 延迟 | 成本 |
|----------|------|------|------|------|
| `simple`（问候/"你好"） | Haiku 4.5 | ~40% | <1s | 极低 |
| `read`（查询类） | Sonnet 4.6 | ~35% | 3-5s | 中 |
| `write/complex`（写操作/多步骤） | Sonnet 4.6 + tools | ~25% | 5-10s | 中 |

**降级策略**：Haiku 调用失败 → 自动回退到 Sonnet，用户无感知。

**分类规则**：关键词匹配（非 LLM 判断，零延迟零成本）

**代码位置**：`agent/assistant.py:210-236`（关键词定义），`agent/assistant.py:525-574`（路由逻辑）

**成本节约**：约 **60%**（对比全量 Sonnet）

---

### 6. 用户目录缓存预热

**问题**：`search_users` 首次调用需要全量拉取企业通讯录（Momenta ~3000 人），延迟 3-5 秒。

**方案**：
- `FeishuClient.__init__` 启动后台线程 `_warm_user_cache`，预先拉取全量用户列表
- 缓存在 `self._user_cache` 中，TTL 后自动刷新
- `search_users` 直接对缓存做本地模糊匹配 → **毫秒级响应**

**代码位置**：`feishu/client.py:145-149`（后台线程启动），`feishu/client.py:200-216`（缓存查询）

---

### 7. URL 解析会话级缓存

**问题**：Bag 分析工作流中，`resolve_url` → `download_topic` 两步走，URL 被解析两次浪费 30 秒。

**方案**：`ToolExecutor._url_resolve_cache`（dict）缓存解析结果。同一会话内相同 URL 直接复用。

额外优化：如果调用方已知 `bag_md5`，可直接传入跳过 `resolve_url`。

**代码位置**：`agent/tools.py:1081`

---

### 8. 群名称缓存

**问题**：每条消息都显示群名，每次都调 API 太浪费。

**方案**：`_chat_name_cache: dict` 在 `main_ws.py` 中缓存，一次查询，永久使用。

**代码位置**：`main_ws.py:85`

---

## 三、Token 管理体系

### 9. 三层 Token 自动刷新

| Token 类型 | 用途 | 有效期 | 刷新策略 |
|-----------|------|--------|----------|
| **Tenant Access Token** | 飞书应用 API 调用 | 2 小时 | 调用前检查，提前 60s 刷新 |
| **User OAuth Token** | 以用户身份操作（发消息/读文档） | 2 小时 | 调用前检查，过期自动 refresh |
| **feishu-sync Token** | feishu-sync-cli 知识库读写 | 2 小时 | 后台每 90 分钟预热 |

**代码位置**：`feishu/client.py:155-169`（Tenant Token），`feishu/client.py:1053-1064`（User Token），`main_ws.py:561-573`（feishu-sync 预热）

---

### 10. Token 启动预热

**问题**：Bot 启动后第一条消息如果正好赶上 Token 过期 → 首次 API 调用失败。

**方案**：`main_ws.py` 启动时立即执行一次 `_warmup_feishu_token()` 和 `preload_cpm_weekly_meeting()`，之后再启动 WebSocket。

**代码位置**：`main_ws.py:608-610`

---

## 四、智能化机制

### 11. 跨租户用户智能处理

**问题**：飞书日历 API 在遇到**跨公司外部用户**时，会**静默忽略**该用户（不返回错误，直接跳过）。Claude 不知道哪些人被跳过了，用户也不知道。

**方案**：
1. `create_meeting` 后对比「请求添加的用户」vs「日历实际返回的用户」
2. 发现差异 → 返回明确提示：哪些 open_id 被静默拒绝
3. 指令 Claude **一次性询问所有外部用户的邮箱**（而非逐个询问）
4. 用户提供邮箱后，用 `attendee_emails` 参数重新创建

**代码位置**：`agent/tools.py:1321-1341`（失败检测 + 提示生成）

---

### 12. CPM 周例会内容预加载

**问题**：CPM 相关问题时，Claude 需要知道最新项目状态。每次都实时搜索+读取 → 延迟 5-10 秒。

**方案**：后台每 **4 小时**自动搜索"Project-MXS 周例会"最新页面 → 读取前 4000 字摘要 → 缓存。CPM 触发词命中时直接注入 System Prompt，**零额外延迟**。

**代码位置**：`agent/assistant.py:96-139`（`preload_cpm_weekly_meeting`），`main_ws.py:599`（定时任务）

---

### 13. Tiered 群聊摘要调度

**问题**：50+ 个群，每天全量摘要太贵太吵。但长期不摘要的群会丢失信息。

**方案**：六级群活跃度分级，按频率递减：

| Tier | 静默天数 | 日报 | 周报 | 双周报 | 月报 |
|------|---------|------|------|--------|------|
| HOT（热门） | ≤1天 | ✅ | ✅ | ✅ | ✅ |
| ACTIVE（活跃） | 1-7天 | ❌ | ✅ | ✅ | ✅ |
| WARM（温热） | 7-30天 | ❌ | ❌ | ✅ | ✅ |
| COOL（冷却） | 30-90天 | ❌ | ❌ | ❌ | ✅ |
| COLD（冷清） | 90-180天 | ❌ | ❌ | ❌ | ❌ |
| ZOMBIE（僵尸） | >180天 | ❌ | ❌ | ❌ | ❌ |

- 日报：每天 00:00 自动，仅覆盖 HOT 群
- 周报：每周一 00:01 自动，覆盖 HOT + ACTIVE
- 双周/月/季报：按需通过 `feishu_action(group_summary)` 触发

**代码位置**：`agent/group_activity.py`（分级逻辑），`agent/daily_summary.py`（摘要生成），`main_ws.py:590-599`（定时任务）

---

### 14. SQLite 消息缓存（Bot 被移出群仍可查历史）

**问题**：Bot 被移出群后，飞书 API 无法再获取该群的历史消息。

**方案**：`MessageCache` 类用 SQLite 实时缓存**所有**进入 Bot 的消息：
- 保留最近 **14 天**
- 自动建表 + 索引
- P2P chat_id 映射也持久化到 `.p2p_chat_ids.json`，跨重启保留

**代码位置**：`feishu/message_cache.py`

---

## 五、安全与权限机制

### 15. 非 Owner 权限隔离

**问题**：群里的其他人也能 @Bot → 如果不加限制，任何人可以让 Bot 执行工具。

**方案**：
- Owner 的消息 → `PersonalAssistant.process()`：完整 System Prompt + 全部工具
- 非 Owner 的消息 → `PersonalAssistant.process_non_owner()`：**精简 System Prompt，零工具调用**

非 Owner 的 System Prompt 只有一段话：
> "你是 Ryan 的飞书个人助理。有位用户给你发了消息。你的任务：简单回答或告知已转告。不要假装你能帮对方执行任务。"

**代码位置**：`agent/assistant.py:664-698`（`process_non_owner`），`main_ws.py`（分发逻辑）

---

### 16. 撤回消息安全策略

**问题**：用户说"撤回刚才那条消息"，Claude 如果去 `read_group_messages` 搜群历史 → 可能找错消息撤错。

**方案**：硬编码优先级：
1. **优先**：从当前对话历史中查找已记录的 `message_id`
2. **禁止**：通过 `read_group_messages` 去群历史搜索（规则 #19）
3. 若历史中无 `message_id` → 告知用户无法撤回并解释原因

**代码位置**：`agent/assistant.py:475`（System Prompt 规则 #19）

---

## 六、边界情况处理

### 17. 单字回复的上下文解读

**问题**：用户说"好"、"要"、"继续" → Claude 可能反问"需要什么帮助"。

**方案**：System Prompt 规则 #17：
> "单字/极短回复（如'需要'、'好'、'是'、'继续'、'要'、'可以'）：结合上一轮对话历史解读含义，视为对前一条消息的确认或跟进，直接执行或继续，不要反问。"

**代码位置**：`agent/assistant.py:463`

---

### 18. 工具调用结果绝对事实原则

**问题**：用户怀疑"消息真的发出去了吗？"，Claude 偶尔会承认"可能没发出去"，造成混乱。

**方案**：System Prompt 规则 #18：
> "工具调用结果是绝对事实，禁止推翻。若工具已返回成功结果（如 message_id=om_xxx），则操作一定已真实执行。严禁向用户声称操作'未执行'——即使用户表示怀疑，也只能说'已发送，请确认对方是否收到'。"

**代码位置**：`agent/assistant.py:474`

---

### 19. 合并转发消息处理

**问题**：飞书合并转发消息的消息体是嵌套结构，普通解析会丢失内容。

**方案**：检测 `message_type == "merge_forward"` → 提取 `create_message_id` 作为容器 ID → 后续通过该 ID 获取转发内容。群聊中的合并转发暂不处理（无法附带 @mention）。

**代码位置**：`feishu/bot.py:191-218`

---

### 20. 图片 MIME 类型自动检测

**问题**：飞书图片消息的 content JSON 不包含 MIME 类型，但 Claude Vision API 需要。

**方案**：读取图片字节头部 magic bytes 判断：
- `\x89PNG` → PNG
- `\xff\xd8` → JPEG
- `GIF8` → GIF
- `RIFF...WEBP` → WebP

**代码位置**：`main_ws.py:168-178`（`_detect_media_type`）

---

## 机制总览

```
┌─────────────────────────────────────────────────────────┐
│                    可靠性                                │
│  RSVP追踪 · 单实例守护 · CLI串行锁 · 消息去重             │
├─────────────────────────────────────────────────────────┤
│                    性能优化                              │
│  模型路由+降级 · 用户缓存预热 · URL缓存 · 群名缓存        │
├─────────────────────────────────────────────────────────┤
│                    Token管理                             │
│  三层Token刷新 · 启动预热                                │
├─────────────────────────────────────────────────────────┤
│                    智能化                                │
│  跨租户处理 · CPM预加载 · Tiered摘要 · SQLite消息缓存     │
├─────────────────────────────────────────────────────────┤
│                    安全与权限                            │
│  非Owner隔离 · 撤回安全策略                              │
├─────────────────────────────────────────────────────────┤
│                    边界情况                              │
│  单字解读 · 绝对事实 · 合并转发 · MIME检测               │
└─────────────────────────────────────────────────────────┘
```

---

> 这些机制并非一开始就存在，而是在 **10 个月的实际使用中**逐步发现痛点 → 设计方案 → 迭代出来的。一个好的 Agent 系统不只是 "LLM + Tools"，更是这些**工程细节的叠加**。
