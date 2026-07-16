# 踩坑与迭代

> 以下为 Agent 在生产环境 **10 个月运行中**遇到的实际问题及其解决方案。
> 每个问题按「现象 → 根因 → 解决 → 教训」四段式记录。

---

## 1. 飞书日历 API 静默丢弃跨公司用户

**时间**：2025.04 | **严重度**：🔴 高（功能缺失）

**现象**：
用户邀请外部合作方开会，Bot 返回「会议创建成功」，但外部人员从未收到邀请。反复测试后发现：日历 API 不报错、不返回失败信息，只是**静默跳过**了跨租户的 open_id。

**根因**：
飞书日历 API `/calendar/v4/calendars/:id/events` 的 `attendees` 参数仅支持**同租户** open_id。跨公司飞书用户的 open_id 在目标租户的日历系统中无法解析，API 直接忽略，不返回任何错误。

**解决**：
1. 在 `create_meeting` 实现中，对比「请求添加的用户」vs「API 实际返回的用户」，发现差异
2. 差异用户 → 返回明确提示：「以下 open_id 被日历系统静默拒绝：[列表]，请向用户一次性收集所有外部邮箱」
3. 新增 `attendee_emails` 参数，用邮件方式邀请外部用户
4. 在 Tool description 中加 `【重要】` 标记，让 Claude 在 create_meeting 前主动询问外部邮箱

**教训**：
- **API 不报错不等于成功了** — 静默失败是最危险的一类 bug
- SDK 返回值必须做**一致性校验**（请求了什么 vs 实际得到了什么）
- Tool description 中做防御性设计（让 Claude 主动询问），比修复实现代码更有效

**相关代码**：`agent/tools.py:1321-1341`

---

## 2. Haiku 不支持图片 → 模型路由遗漏

**时间**：2025.05 | **严重度**：🟡 中（特定场景失败）

**现象**：
用户发了一张照片让 Bot 分析。Bot 返回 API 错误。排查发现：该消息被路由到 Haiku（判为 `simple`），但 **Haiku 4.5 不支持视觉输入**。

**根因**：
`_classify_request()` 的 `simple` 分类只检查文本关键词（问候语、功能询问），没检查消息是否包含图片。当用户发「看看这张图」时，文本很短但实际需要视觉能力。

**解决**：
在模型路由之前插入一行守卫代码：
```python
if msg.image_data and req_category == "simple":
    req_category = "read"  # 强制跳过 Haiku
```

**教训**：
- 路由条件必须覆盖**非文本模态**（图片、文件、音频）
- 分类函数应该接收**完整消息对象**而不只是文本
- 守卫代码（Guard Clause）比修改分类逻辑更清晰

**相关代码**：`agent/assistant.py:532-533`

---

## 3. feishu-sync CLI 多线程 Token 竞态

**时间**：2025.05 | **严重度**：🔴 高（Token 损坏导致全部知识库操作失败）

**现象**：
高峰期时，飞书知识库操作间歇性返回 401 Unauthorized。Token 文件内容偶尔变成乱码。低负载时一切正常。

**根因**：
`feishu-sync-cli` 在内部读写同一份 Token JSON 文件。当两个并发的 Claude 请求同时触发 `feishu_action` → 两个子进程同时读写 Token 文件 → 文件损坏。

**解决**：
在所有 `feishu-sync-cli` 调用处加上全局线程锁 `_FEISHU_CLI_LOCK`：
```python
with _FEISHU_CLI_LOCK:
    result = subprocess.run([*_FEISHU_SYNC_CMD, "search_wiki", query], ...)
```

**教训**：
- 第三方 CLI 工具的内部状态不是线程安全的 — 需要**应用层串行化**
- Token 文件不是数据库，不支持并发写 — 要么用锁，要么改成每次请求新 Token
- 并发 bug 是**最难复现**的 bug（只在高峰期出现）

**相关代码**：`agent/tools.py:118`（锁定义）、`agent/tools.py:1917`（使用点）

---

## 4. 多实例 Token 文件损坏

**时间**：2025.06 | **严重度**：🔴 高（Bot 完全不可用）

**现象**：
用户重启电脑后，Bot 频繁崩溃。日志显示 `Tenant Access Token` 校验失败。进一步发现有两个 Bot 进程在运行。

**根因**：
Windows 重启后，旧的 Bot 进程没有被正确终止（残留进程），而 `main_ws.py` 再次被启动 → 两个进程共享 Token 文件 → 一个刷新了 Token，另一个用旧 Token 调用 API → 更新 Token → 文件竞态。

**解决**：
实现**单实例守护**（PID 文件锁）：
```python
_PID_FILE = ".bot.pid"

def _ensure_single_instance():
    if os.path.exists(_PID_FILE):
        old_pid = int(open(_PID_FILE).read())
        os.kill(old_pid, signal.SIGTERM)   # 优雅终止
        time.sleep(1)
        try:
            os.kill(old_pid, signal.SIGKILL)  # 强制终止
        except ProcessLookupError:
            pass
    open(_PID_FILE, "w").write(str(os.getpid()))
    atexit.register(lambda: os.remove(_PID_FILE))
```

**教训**：
- 长连接服务必须处理**进程生命周期**
- Windows 和 Linux 的进程管理差异很大（Windows 没有 `SIGTERM` 概念，但 Git Bash 模拟了）
- PID 文件不是完美的（进程被杀后 PID 文件可能残留），需要 `PermissionError` / `ProcessLookupError` 兜底

**相关代码**：`main_ws.py:22-47`

---

## 5. WebSocket 消息重放导致重复回复

**时间**：2025.06 | **严重度**：🟡 中（影响体验但不丢数据）

**现象**：
偶尔用户收到两条完全一样的 Bot 回复。频率不高（约 1-2% 的消息），但体验很差。

**根因**：
飞书 WebSocket 在网络不稳定时会重放消息事件。Bot 没有去重机制，每次都当作新消息处理。

**解决**：
内存中维护 `_processed_ids: set`（最多 1000 条），处理前检查 `message_id`：
```python
_processed_ids: set[str] = set()
_MAX_DEDUP = 1000

if message.message_id in _processed_ids:
    return  # 已处理，跳过
_processed_ids.add(message.message_id)
if len(_processed_ids) > _MAX_DEDUP:
    _processed_ids.clear()  # 防止内存泄漏（保留最近 1000 条足够）
```

**教训**：
- WebSocket 不是 exactly-once 语义 — 应用层必须做**幂等**
- 去重集合需要**容量上限**，否则长时间运行会 OOM
- `message_id` 是天然的幂等键

**相关代码**：`main_ws.py:104-105`

---

## 6. Claude 声称「消息未发送」的幻觉

**时间**：2025.07 | **严重度**：🟡 中（混淆用户）

**现象**：
用户：消息发出去了吗？
Claude：抱歉，消息可能没有成功发送，让我重新发一次。
→ 用户收到了两条消息。

**根因**：
工具已返回 `message_id=om_xxx`（发送成功），但 Claude 在后续对话中「忘记」了工具调用结果，凭感觉猜测。

**解决**：
在 System Prompt 中新增**规则 #18**：
> "工具调用结果是绝对事实，禁止推翻。若工具已返回成功结果，则操作一定已真实执行。严禁向用户声称操作'未执行'——即使用户表示怀疑，也只能说'已发送，请确认对方是否收到'。"

这种硬编码规则比依赖 Claude 推理更可靠。

**教训**：
- LLM 对「工具已执行但用户没看到」的情况有天然的怀疑倾向
- 用**绝对语句**（"禁止"、"一定"、"严禁"）比委婉语句（"建议"、"可能"）有效得多
- 每发现一个 Claude 的「坏习惯」，就加一条规则 — Prompt 是迭代出来的

**相关代码**：`agent/assistant.py:474`

---

## 7. 单字回复「好」被误解为需要帮助

**时间**：2025.07 | **严重度**：🟢 低（体验问题）

**现象**：
经典对话：
> Bot：需要我创建会议吗？
> 用户：好
> Bot：好的！请问需要什么帮助？

**根因**：
Claude 在判断「好」这条消息时，无法确定这是「确认创建」还是「新对话的开始」。`_classify_request()` 把"好"分类为 `simple`（单字问候）→ 走 Haiku → 没有工具，「忘记」了上一轮在聊什么。

**解决**：
两处修改：
1. System Prompt 规则 #17：明确告知 Claude 单字回复应结合历史解读
2. `_classify_request()` 的单字判定后，如果 `last_reply` 存在 pending 操作 → 回退到 `write` 而非 `simple`

**教训**：
- 单字消息的意图**完全取决于上下文**
- 分类函数不能只看当前消息，必须看**最近一轮对话**
- 对话历史的「最后一轮」是最重要的信号

**相关代码**：`agent/assistant.py:463`（规则）、`agent/assistant.py:525-533`（回退逻辑）

---

## 8. inspect_pptx 后不 edit 的「假完成」

**时间**：2025.08 | **严重度**：🟡 中（任务看似完成实际没做）

**现象**：
用户：把这个 PPTX 的日期改一下。
Claude：[调用 inspect_pptx] → 返回了所有形状的坐标和文字
Claude：已完成！第 3 页的日期已更新为 7 月 15 日。
→ 用户打开文件，日期根本没变。

**根因**：
Claude 把 `inspect_pptx`（只读查看）当成了修改操作。因为它「看到了」所有形状信息，就以为自己已经完成了修改。

**解决**：
System Prompt 规则 #12：
> "inspect_pptx 仅用于获取信息，必须紧接着调用 edit_pptx 才能真正保存修改，不能只 inspect 就结束"

同时在 `inspect_pptx` 的返回结果的第一行加上 `⚠️【仅查看】` 提示。

**教训**：
- Read-only 工具和 Write 工具的区别对 LLM 并不直观
- 需要在工具描述和返回结果中**反复强调**「只读」
- 用规则硬编码比期望 Claude 理解更可靠

**相关代码**：`agent/assistant.py:468`

---

## 9. 合并转发消息解析失败

**时间**：2025.09 | **严重度**：🟢 低（边缘 case）

**现象**：
用户从别的群转发了一段对话给 Bot，Bot 返回「消息格式错误」。

**根因**：
飞书合并转发消息的 `message.content` 结构是嵌套 JSON，与普通富文本完全不同。早期代码只处理了 `post` 类型消息。

**解决**：
检测 `message_type == "merge_forward"` → 提取 `create_message_id` 作为容器 ID → 用飞书 API 单独拉取转发内容。

同时做了边界判断：**群聊中的合并转发暂时跳过**（因为无法附带 @Bot 的 mention，消息不会路由给 Bot）。

**教训**：
- 消息类型的**枚举不是闭合的** — 飞书会不断加新类型
- 对未知消息类型做**优雅降级**而不是崩溃
- 不是所有消息都需要处理 — 合理跳过也是一种策略

**相关代码**：`feishu/bot.py:191-218`

---

## 10. Bot 被移出群后无法查历史

**时间**：2025.10 | **严重度**：🟡 中（摘要功能缺失）

**现象**：
用户把 Bot 从某个群移除，后来又重新拉入。旧的群摘要功能无法生成该群的历史摘要（因为 Bot 不在群期间，飞书 API 不给消息记录）。

**根因**：
飞书的消息查询 API 只对**当前在群内的 Bot**开放。Bot 不在群时 → 查不到历史。

**解决**：
实现 `MessageCache`（SQLite 后端）：
- 所有进入 Bot 的消息**实时写入 SQLite**
- 保留最近 14 天
- Bot 重新入群后，摘要从本地缓存读取而不依赖飞书 API
- P2P 对话的 chat_id 映射也持久化到 JSON 文件

**教训**：
- 不要假设「Bot 一直在群里」— **数据要做本地备份**
- SQLite 是 Bot 场景的完美选择：零配置、单文件、足够快
- 14 天是消息的合理保鲜期（太短不够用，太长浪费磁盘）

**相关代码**：`feishu/message_cache.py`

---

## 总结：10 个月，10 个坑

```
时间线 ─────────────────────────────────────────────────→

2025.04  跨租户静默丢弃    →  一致性校验 + attendee_emails
2025.05  Haiku不支持图片   →  模型路由守卫
2025.05  Token竞态损坏     →  CLI调用串行化锁
2025.06  多实例Token损坏   →  单实例守护(PID锁)
2025.06  消息重放重复回复   →  消息去重
2025.07  声称消息未发送     →  绝对事实规则
2025.07  单字回复误解       →  上下文回退路由
2025.08  只inspect不edit   →  强制两步走规则
2025.09  合并转发解析失败   →  merge_forward检测
2025.10  被移出群查不了历史 →  SQLite消息缓存
```

**规律**：
- **4-5 月的坑**都在 API 边界（飞书日历、Haiku、Token）
- **6-7 月的坑**都在 Claude 的行为理解（幻觉、误解）
- **8-10 月的坑**都在边缘 case（合并转发、被踢出群）

> 一个好的 Agent 不是写出来的，是**用出来的**。每个坑都是一次对边界条件认知的迭代。
