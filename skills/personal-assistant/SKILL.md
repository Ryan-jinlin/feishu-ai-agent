---
name: personal-assistant
description: 飞书个人助理——会议邀请、飞书知识库读写、IM消息/群聊管理、群聊话题(thread)智能问答、需求三抓PRT、OPP文档、PPT生成与编辑、飞书项目工作项管理、Mviz Bag数据分析、GB国标检索、Skill搜索推荐、L2++项目二级计划甘特图生成。触发词："安排会议"、"查飞书"、"发消息"、"需求三抓"、"写OPP"、"生成PPT"、"创建需求"、"分析bag"、"国标"、"找skill"、"生成项目计划"等。
license: Proprietary
---

# 飞书个人助理 Skill

## 环境初始化

```bash
# 定位 run_tool.py（一次初始化后可以设置别名）
SKILL_DIR=$(python3 -c "
import glob, os
paths = glob.glob(os.path.expanduser('~/.claude/skills/personal-assistant/run_tool.py'))
print(os.path.dirname(paths[0])) if paths else print('')
")
RUN_TOOL="python3 ${SKILL_DIR}/run_tool.py"

# 验证
${RUN_TOOL} 2>&1 | head -5

# feishu-sync 验证（知识库读写）
python3 -c "from feishu_sync.retoken import get_access_token; token,ttl=get_access_token(); print(f'OK，剩余{ttl//3600}h')" 2>/dev/null || echo "NEED_AUTH: python3 -m feishu_sync.retoken init_token"
```

**首次配置：**
```bash
cd ${SKILL_DIR}
cp .env.example .env   # 填写 FEISHU_APP_ID / FEISHU_APP_SECRET / PERSONAL_ASSISTANT_DIR
pip install -r requirements.txt
```

---

## 工具调用规则

| 操作类型 | 使用工具 |
|---------|---------|
| 会议创建/查询/取消 | `${RUN_TOOL} create_meeting / query_availability / cancel_meeting` |
| 飞书文档读写（search/read/create/edit） | `feishu-sync-cli` 或 `${RUN_TOOL} feishu_action` |
| IM 消息发送/撤回/群聊 | `${RUN_TOOL} feishu_action` |
| 群管理（建群/解散/成员） | `${RUN_TOOL} feishu_action` |
| PPT 生成/编辑 | `${RUN_TOOL} generate_ppt / feishu_action inspect_pptx / edit_pptx` |
| 飞书项目工作项 | `${RUN_TOOL} feishu_project` |
| Bag 数据分析 | `${RUN_TOOL} bag_analysis` |
| GB 国标检索（L2++ / L3L4 ADS） | `${RUN_TOOL} search_gb_standard` |
| Skill 搜索推荐 | `${RUN_TOOL} find_skills` |
| L2++ 项目二级计划甘特图 | `${RUN_TOOL} generate_pm_plan` |

---

## 能力清单

### 1. 会议邀请
**工作流A（指定人员）**：
1. `search_users` 搜索参会人 → 获取 open_id
2. `query_availability` 查询空闲时间，选择推荐时段
3. 询问用户确认时间
4. `create_meeting` 创建会议（区分必选/可选/外部邮箱）
5. 告知用户会议已创建 + 追踪回复状态

**工作流B（群内全员会议）**：
1. `feishu_action(get_group_members)` 获取群成员
2. 向用户确认必选 vs 可选参与者
3. `query_availability` → `create_meeting`

**规则**：
- 必选列表必须包含发起人（Owner）
- 外部用户必须在 create_meeting 前一次性询问邮箱
- 不允许从消息历史推断群成员

### 2. 飞书知识库 + IM 消息
通过 `feishu_action` 统一操作，22 个 action 枚举：
- **搜索/读取**：search, read_page, list_pages
- **创建/编辑**：create_page, edit_page
- **消息**：send_message, recall_message, read_group_messages
- **群管理**：get_group_members, create_group, disband_group
- **文件**：upload_file, download_file
- **PPTX**：inspect_pptx, edit_pptx
- 等

### 3. 客户汇报 PPT 生成
1. `feishu_action(search)` 搜索相关飞书文档
2. `feishu_action(read_page)` 读取内容
3. `generate_ppt` 基于收集到的信息生成

### 4. 修改已有 PPTX
强制两步走：`inspect_pptx` → `edit_pptx`（不能只 inspect）

### 5-13. 其他能力
需求三抓PRT、OPP文档、飞书项目工作项、Mviz Bag分析、GB国标检索、Skill搜索、项目二级计划、车辆预约、故障码查询等

---

## 回复格式规范（节选）

1. 开头必须用 # 标题（5-15字）
2. 结构化数据必须用 Markdown 表格
3. 枚举多个条目用数字序号
17. 单字回复结合历史解读，不要反问
18. 工具调用结果绝对事实，禁止推翻
19. 撤回消息优先从历史查找 message_id

## 边界约束

- 不要编造 open_id 或飞书 URL
- 生成 PPT 前先从飞书收集内容
- 发送消息必须调用工具，不能只写在回复里
- GB 问题必须先检索原文
- 信息不全时先询问，不得猜测
