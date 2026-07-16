# 典型对话实录

> 以下为 Agent 在生产环境中的真实对话场景还原，展示 Claude 如何使用 System Prompt + Tools 完成复合任务。
> 每条消息标注了 **Agent 内部动作**（模型路由、工具调用、Skill 注入）。

---

## 场景一：会议邀请（全流程）

**触发 Skill**：personal-assistant（基础能力，始终在线）
**模型路由**：`write`（检测到"约"关键词）→ Sonnet 4.6 + tools

---

### 第 1 轮

> **用户**：帮我约张三、李四明天下午讨论 Q2 规划，一小时

**Agent 内部动作**：
- `_classify_request()` → `write`（"约"命中写操作关键词）
- 模型路由：Sonnet 4.6 + tools
- System Prompt 注入：基础能力 1「会议邀请」工作流 A

> **Claude**：[调用 search_users(keyword="张三")]
> → 找到 1 个匹配：张三，open_id: ou_aaa

> **Claude**：[调用 search_users(keyword="李四")]
> → 找到 1 个匹配：李四，open_id: ou_bbb

---

### 第 2 轮

> **Claude**：[调用 query_availability(user_open_ids=["ou_aaa", "ou_bbb", "ou_owner"], date="2026-07-16")]
> → 返回：
> ```
> 2026-07-16 空闲时段（≥60分钟）：
> 09:00-10:30   ⭐ 3人皆空闲
> 14:00-16:00   ⭐ 3人皆空闲
> 15:30-17:00   ⚠️ 张三有冲突
> ```

> **Claude** → 用户：
> ```
> # 📅 明天可用的会议时间
>
> 张三、李四和你明天下午的空闲情况：
>
> | 时段 | 张三 | 李四 | 你 | 状态 |
> |------|------|------|-----|------|
> | 14:00-15:00 | ✅ | ✅ | ✅ | 推荐 |
> | 14:30-15:30 | ✅ | ✅ | ✅ | 可选 |
> | 15:00-16:00 | ✅ | ✅ | ✅ | 可选 |
>
> 建议选 **14:00-15:00**，三位都空闲。需要我创建会议吗？
> ```

---

### 第 3 轮

> **用户**：好，就 14:00

**Agent 内部动作**：
- `_classify_request()` → 先判 `simple`（"好"是单字），但 `last_reply` 有 pending 操作 → `_classify_request` 检测到历史上下文
- 回退到 `write` 模式

> **Claude**：[调用 create_meeting(
>   title="Q2规划讨论",
>   start_time="2026-07-16T14:00:00",
>   end_time="2026-07-16T15:00:00",
>   attendee_open_ids=["ou_aaa", "ou_bbb", "ou_owner"],
>   description="讨论Q2规划"
> )]
>
> → 返回：
> ```
> 会议已创建成功！
> 标题：Q2规划讨论
> 时间：2026-07-16 14:00 - 15:00
> 必选与会人数：3 人
> 日历邀请已发送
> 已向内部受邀者发送飞书消息通知，将实时追踪回复状态。
> [event_id=evt_xxx calendar_id=cal_yyy]
> ```

**Agent 内部动作（后台）**：
- 事件登记到 `_PENDING_EVENTS` → 持久化 `.pending_events.json`
- 5 分钟后 RSVP 轮询任务启动

---

### 第 4 轮（5 分钟后，后台自动触发）

> **RSVP 轮询任务**：检测到张三 RSVP 状态变化：`needs_action → declined`

> **Agent** → 用户（DM 通知）：
> ```
> 【会议提醒】张三 拒绝了会议邀请
> 会议：Q2规划讨论
> 如需重新安排，请告诉我新的时间。
> ```

---

**这个场景展示了**：
1. 多步工具编排（search × 2 → query → create）
2. 单字回复上下文解读
3. 后台 RSVP 追踪 + 主动通知
4. 回复格式遵循 19 条规范（表格化、标题化）

---

## 场景二：故障码查询

**触发 Skill**：bag-fault-query（通过 `query_bag_fault` 工具）
**模型路由**：`read`（查询类）→ Sonnet 4.6 + tools

---

> **用户**：帮我查 VIN LSVUM40Z6MN123456 昨天有没有故障

**Agent 内部动作**：
- System Prompt 注入：基础能力 13「故障码查询」
- VIN 和时间已齐全，无需追问

> **Claude**：[调用 query_bag_fault(
>   vin="LSVUM40Z6MN123456",
>   time_points="2026-07-15",
>   size=20
> )]
>
> → 返回：
> ```
> VIN: LSVUM40Z6MN123456
> 查询时间范围: 2026-07-15
> 共找到 3 个 ESS 事件
>
> ⚠️ 事件 1: 2026-07-15 08:23 - 09:15
>   故障码 1: 0x12345678 (ESP_FAULT)
>     受影响功能: 车身稳定控制
>     降级模式: ESP 功能受限，车速限制 80km/h
>     故障级别: 功能性故障
>   故障码 2: 0x23456789 (CAN_TIMEOUT)
>     受影响功能: 底盘通信
>     降级模式: 无
>     故障级别: 通信告警
>
> ✅ 事件 2: 2026-07-15 12:00 - 13:30 — No faults detected
>
> ⚠️ 事件 3: 2026-07-15 17:45 - 18:10
>   故障码 1: 0x34567890 (CAMERA_OBSTRUCTION)
>     受影响功能: 前视摄像头
>     降级模式: L2++ 功能降级为 L1
>     故障级别: 功能性故障
> ```

> **Claude** → 用户：
> ```
> # 🔍 故障诊断报告
>
> VIN: LSVUM40Z6MN123456 | 查询日期: 7月15日
>
> ## 故障事件总览
>
> | 时间 | 故障码 | 影响功能 | 严重度 |
> |------|--------|----------|--------|
> | 08:23-09:15 | ESP_FAULT | 车身稳定控制 | ⚠️ 功能受限 |
> | 08:23-09:15 | CAN_TIMEOUT | 底盘通信 | ⚡ 告警 |
> | 12:00-13:30 | — | — | ✅ 无故障 |
> | 17:45-18:10 | CAMERA_OBSTRUCTION | 前视摄像头 | ⚠️ L2++降级L1 |
>
> ## 分析
>
> 1. **ESP_FAULT**（早高峰）：ESP 功能受限，车速被限制在 80km/h。可能与早上的 CAN_TIMEOUT 相关 — 底盘通信超时触发了 ESP 安全降级。
> 2. **CAMERA_OBSTRUCTION**（傍晚）：前视摄像头遮挡导致 L2++ 降级为 L1。建议检查摄像头镜头清洁状态和安装角度。
>
> 需要针对某个故障码深入分析 Bag 数据吗？（可用 `bag_analysis` 下载相关 topic）
> ```

---

**这个场景展示了**：
1. 故障码映射自动匹配
2. Claude 不仅输出数据，还做**因果推理**（CAN_TIMEOUT → ESP 降级）
3. 结构化 Markdown 表格渲染
4. 主动建议下一步操作（bag_analysis）

---

## 场景三：群内全员会议（必选/可选人分离）

**触发 Skill**：personal-assistant
**模型路由**：`write` → Sonnet + tools

---

> **用户**（在「MXS 项目群」中）：@Bot 给群里所有人约周五下午 2 点的项目周会

**Agent 内部动作**：
- System Prompt 注入：基础能力 1「工作流 B — 群内全员会议」
- 上下文注入：`chat_id=oc_mxs_project`

> **Claude**：[调用 feishu_action(action="get_group_members", chat_id="oc_mxs_project")]
>
> → 返回：
> ```
> 群「MXS项目群」共 8 名成员：
> 1. 张三 — open_id: ou_aaa
> 2. 李四 — open_id: ou_bbb
> 3. 王五 — open_id: ou_ccc
> 4. 赵六 — open_id: ou_ddd
> 5. 钱七 — open_id: ou_eee
> 6. 孙八 — open_id: ou_fff
> 7. 周九 — open_id: ou_ggg
> 8. Ryan（你） — open_id: ou_owner
> ```

> **Claude** → 用户：
> ```
> # 👥 群成员确认
>
> MXS项目群共 8 人。请确认以下分类：
>
> **所有成员**：
> 张三 / 李四 / 王五 / 赵六 / 钱七 / 孙八 / 周九 / 你
>
> 请问哪些人是**必选参与者（Required）**？
> 其余人将标记为 Optional（可选参加，日历中显示为非必选）。
> ```

> **用户**：张三、李四必需，其他 optional

> **Claude**：[调用 query_availability(...)]
> → 返回：14:00-15:00 全部空闲

> **Claude**：[调用 create_meeting(
>   title="MXS项目周会",
>   attendee_open_ids=["ou_aaa", "ou_bbb", "ou_owner"],
>   optional_attendee_open_ids=["ou_ccc", "ou_ddd", "ou_eee", "ou_fff", "ou_ggg"]
> )]

---

**这个场景展示了**：
1. 群上下文注入（chat_id 自动提供给 Claude）
2. 严格遵循工作流 B（先 get_group_members → 确认分类 → 再创建）
3. 必选/可选人员分离
4. 不依赖消息历史推断群成员（System Prompt 规则禁止）

---

## 场景四：PPT 生成 + 修改

**触发 Skill**：personal-assistant
**模型路由**：`complex`（"PPT"触发）→ Sonnet + tools

---

> **用户**：帮我生成一个 Q2 项目进展的 PPT，要有数据

> **Claude**：[调用 feishu_action(action="search", query="MXS Q2 项目进展")]
> → 返回 3 个相关页面

> **Claude**：[调用 feishu_action(action="read_page", url="feishu.cn/wiki/xxx")]
> → 返回页面正文（含进展数据、指标表格）

> **Claude**：[调用 generate_ppt(title="MXS项目Q2进展汇报", ...)]
> → 返回：PPTX 已保存

---

> **用户**：把第一页的"95%"改成"96%"，再加一句"超额完成"

> **Claude**：[调用 feishu_action(action="inspect_pptx", slide_index=0)]
> → 返回第 1 页所有形状坐标和文字

> **Claude**：[调用 feishu_action(action="edit_pptx", replacements=[...])]
> → 返回：修改已保存

---

**这个场景展示了**：
1. 复杂任务的关键词识别（"PPT"→ complex 路由）
2. 先收集资料再生成的严谨工作流
3. inspect → edit 强制两步走（System Prompt 规则 #12）
4. `edit_pptx` 在原文件上修改，保留格式

---

## 场景五：跨租户外部用户处理

**触发 Skill**：personal-assistant
**模型路由**：`write` → Sonnet + tools

---

> **用户**：约张三和 xiaoming@partner.com 明天 10 点开会

> **Claude**：[调用 search_users(keyword="张三")] → ou_aaa
> **Claude**：发现 `xiaoming@partner.com` 是外部邮箱，但描述中说「外部用户请在调用 create_meeting 前一次性询问所有外部邮箱」

> **Claude** → 用户：
> ```
> 已找到张三（内部用户）。xiaoming@partner.com 是外部用户，将用邮箱邀请。
>
> 请问还有其他外部参会者需要添加吗？请一次性提供所有外部邮箱。
> ```

> **用户**：还有 wangfang@supplier.cn

> **Claude**：[调用 create_meeting(
>   attendee_open_ids=["ou_aaa", "ou_owner"],
>   attendee_emails=["xiaoming@partner.com", "wangfang@supplier.cn"]
> )]

---

**这个场景展示了**：
1. 外部用户邮箱一次性收集（避免逐个询问的糟糕体验）
2. `attendee_emails` vs `attendee_open_ids` 的正确分流
3. 跨租户用户静默拒绝检测在后台生效

---

## 对话实录总结

| 场景 | 模型 | 工具调用轮数 | 关键能力 |
|------|------|-------------|---------|
| 会议邀请 | Sonnet | 4 轮 | search → availability → create → RSVP |
| 故障码查询 | Sonnet | 1 轮 | VIN → ESS → 故障分析 + 因果推理 |
| 群内全员会议 | Sonnet | 3 轮 | get_members → 分类 → availability → create |
| PPT 生成+修改 | Sonnet | 4 轮 | search → read → generate → inspect → edit |
| 跨租户用户 | Sonnet | 2 轮 | search → 邮箱收集 → create |

> **共同特征**：Claude 从不「一步到位」，而是**先收集信息、确认用户意图、再执行操作** — 这是 Agent 设计中「可靠性优先于速度」的理念体现。
