"""Claude 个人助理 Agent（手动 tool use 循环）"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime

import anthropic
import pytz

from agent.tools import TOOL_DEFINITIONS, ToolExecutor
from feishu.bot import BotMessage

logger = logging.getLogger(__name__)
TZ_SHANGHAI = pytz.timezone("Asia/Shanghai")

# 最多允许 Claude 调用多少轮工具，防止死循环
# Bag 分析（search → lookup → download）+ PPT 等组合任务可能需要较多轮
MAX_TOOL_ROUNDS = 16

# PRT Skill 触发词
_PRT_TRIGGERS = ("需求三抓", "写prt", "做prt", "prt", "三抓", "需求挖掘", "写PRT", "做PRT")


def _load_prt_skill() -> str:
    """加载 ~/.claude/skills/prt/ 下的 SKILL.md 和模板，返回拼接文本。"""
    skill_dir = os.path.expanduser("~/.claude/skills/prt")
    parts: list[str] = []
    skill_md = os.path.join(skill_dir, "SKILL.md")
    if os.path.exists(skill_md):
        with open(skill_md, encoding="utf-8") as f:
            parts.append(f.read())
    tpl_dir = os.path.join(skill_dir, "templates")
    if os.path.isdir(tpl_dir):
        for fname in sorted(os.listdir(tpl_dir)):
            if fname.endswith(".md"):
                with open(os.path.join(tpl_dir, fname), encoding="utf-8") as f:
                    parts.append(f"\n\n---\n## 模板文件：{fname}\n\n{f.read()}")
    return "\n\n".join(parts)


def _load_opp_skill() -> str:
    """加载 ~/.claude/skills/opp/ 下的 SKILL.md 和 references/*.md，返回拼接文本。"""
    skill_dir = os.path.expanduser("~/.claude/skills/opp")
    parts: list[str] = []
    skill_md = os.path.join(skill_dir, "SKILL.md")
    if os.path.exists(skill_md):
        with open(skill_md, encoding="utf-8") as f:
            parts.append(f.read())
    ref_dir = os.path.join(skill_dir, "references")
    if os.path.isdir(ref_dir):
        for fname in sorted(os.listdir(ref_dir)):
            if fname.endswith(".md"):
                with open(os.path.join(ref_dir, fname), encoding="utf-8") as f:
                    parts.append(f"\n\n---\n## 参考文件：{fname}\n\n{f.read()}")
    return "\n\n".join(parts)


def _load_simple_skill(name: str) -> str:
    """通用 Skill 加载：从多个候选目录中查找 <name>/SKILL.md 并返回内容。
    搜索顺序：全局 ~/.claude/skills → bot 目录 .agents/skills → 父目录 .agents/skills
    """
    bot_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.expanduser(f"~/.claude/skills/{name}/SKILL.md"),
        os.path.join(bot_dir, ".agents", "skills", name, "SKILL.md"),
        os.path.join(bot_dir, "..", ".agents", "skills", name, "SKILL.md"),
    ]
    for path in candidates:
        norm = os.path.normpath(path)
        if os.path.exists(norm):
            with open(norm, encoding="utf-8") as f:
                return f.read()
    return ""


# 模块加载时预读所有 Skill（避免每次请求重复 I/O）
_PRT_SKILL_CONTENT: str = _load_prt_skill()
_OPP_SKILL_CONTENT: str = _load_opp_skill()
_INTEGRATION_SKILL_CONTENT: str = _load_simple_skill("integration-guide")
_WEB_SKILL_CONTENT: str = _load_simple_skill("momenta-web-skill")
_FTT_SKILL_CONTENT: str = _load_simple_skill("ftt-workflow")
_MVIZ_RECORDER_SKILL_CONTENT: str = _load_simple_skill("mviz-recorder")
_ROUTE_EXTRACT_SKILL_CONTENT: str = _load_simple_skill("route-extract")
_APA_SNAPSHOT_SKILL_CONTENT: str = _load_simple_skill("apa-mviz-snapshot")


def _load_feishu_project_skill() -> str:
    """加载 feishu-project skill：SKILL.md + 关键 references（跳过超大文件）"""
    bot_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    skill_dir = os.path.join(bot_dir, ".agents", "skills", "feishu-project")
    if not os.path.isdir(skill_dir):
        return ""
    parts: list[str] = []
    skill_md = os.path.join(skill_dir, "SKILL.md")
    if os.path.exists(skill_md):
        with open(skill_md, encoding="utf-8") as f:
            parts.append(f.read())
    # 加载关键参考文件（跳过 openapi-reference.md 和 examples.md，文件过大）
    for fname in ("project-mapping.md", "workitem-type-mapping.md", "mcp-methods.md", "moql-reference.md"):
        fpath = os.path.join(skill_dir, "references", fname)
        if os.path.exists(fpath):
            with open(fpath, encoding="utf-8") as f:
                parts.append(f"\n\n---\n## 参考文件：{fname}\n\n{f.read()}")
    return "\n\n".join(parts)


_FEISHU_PROJECT_SKILL_CONTENT: str = _load_feishu_project_skill()

# OPP Skill 触发词
_OPP_TRIGGERS = ("opp", "写opp", "做opp", "创建opp", "新建opp", "opp文档", "计划沟通")

# integration-guide 触发词（来自 skill description）
_INTEGRATION_TRIGGERS = ("集成指南", "集成手册", "集成工程师", "系统集成", "integration guide", "integration-guide",
                          "startup_config", "mf_graph", "mf_scripts", "实车调试", "板端性能")

# momenta-web-skill 触发词
_WEB_TRIGGERS = ("momenta web", "momenta ui", "antd", "react ui", "前端页面", "web页面",
                 "dashboard", "管理后台", "品牌设计", "0066ff", "momenta-web")

# ftt-workflow 触发词
_FTT_TRIGGERS = ("ftt", "全流程", "ftt-workflow", "ftt bot", "fttbot", "bvp测试", "需求导入",
                 "自闭环报告", "提交pr")

# mviz-recorder 触发词
_MVIZ_RECORDER_TRIGGERS = ("录制mviz", "mviz录制", "mviz recorder", "mviz-recorder",
                            "截图录制", "录制视频", "录制bag", "录制播放器", "批量录制")

# route-extract 触发词
_ROUTE_EXTRACT_TRIGGERS = ("提取路径", "路径提取", "route-extract", "route extract",
                            "egopose", "路由名称", "recorder事件", "轨迹提取", "提取轨迹",
                            "提取route", "mff路由")

# apa-mviz-snapshot 触发词
_APA_SNAPSHOT_TRIGGERS = ("apa快照", "泊车快照", "apa-mviz", "apa mviz", "apa snapshot",
                           "环视图", "俯视图", "apa泊车", "泊车可视化", "进控截图", "ess事件",
                           "泊车场景")

# feishu-project 触发词
_FEISHU_PROJECT_TRIGGERS = (
    "飞书项目", "工作项", "需求管理", "feishu project", "feishu-project",
    "工作流", "ppm需求", "tlm需求", "meego", "rocky", "harz", "天马山", "武岳山",
    "大金山", "五华山", "筑波山", "秦望山", "莲花山", "白云山", "taunus",
    "创建需求", "查询需求", "更新需求", "推进节点", "视图数据", "moql", "search_by_mql",
    "finish_node", "get_workitem", "create_workitem", "project_key",
)

# ── 请求分类（用于模型路由） ──────────────────────────────────────────

# 简单问答：不需要工具，Haiku 直接回答
_SIMPLE_GREETINGS = (
    "你好", "hi", "hello", "早上好", "早安", "下午好", "晚上好",
    "谢谢", "感谢", "辛苦了", "好棒", "厉害", "好的谢谢",
)
_SIMPLE_CAPABILITY_PATTERNS = (
    "你能做什么", "你有什么功能", "你会什么", "功能介绍", "你是谁",
    "介绍一下你自己", "/help",
)

# 写操作关键词 → Opus + thinking（保证准确性）
_WRITE_KEYWORDS = (
    "创建", "新建", "建群", "解散群", "发送", "发消息", "发通知",
    "生成", "制作", "更新", "修改", "编辑", "添加", "新增",
    "删除", "取消会议", "推进节点", "发布", "上传", "起草",
    "约会议", "安排会议", "发会邀", "撤回",
)

# 复杂多步骤任务 → Opus + thinking（不可降级）
_COMPLEX_KEYWORDS = (
    "ppt", "幻灯片", "汇报ppt", "bag分析", "bag 分析", "分析bag",
    "mviz分析", "需求三抓", "opp文档", "写opp", "写prt", "做prt",
)


class PersonalAssistant:
    """
    接收 BotMessage，通过 Claude claude-opus-4-6 + tool use 解析意图、调用工具，
    返回最终回复文本。
    """

    def __init__(
        self,
        anthropic_api_key: str,
        tool_executor: ToolExecutor,
        owner_name: str = "",
        owner_open_id: str = "",
    ):
        self.client = anthropic.Anthropic(api_key=anthropic_api_key)
        self.executor = tool_executor
        self.owner_name = owner_name or "用户"
        self.owner_open_id = owner_open_id
        # 每用户对话历史（保留最近 _MAX_HISTORY_TURNS 轮）
        # key: sender_open_id, value: [{"role": "user"|"assistant", "content": str}, ...]
        self._histories: dict[str, list[dict]] = {}

    # 每用户保留最近 N 轮对话（1 轮 = 1 user + 1 assistant）
    _MAX_HISTORY_TURNS = 6

    def process(self, msg: BotMessage) -> str:
        """处理一条飞书消息，返回回复文本"""
        now = datetime.now(TZ_SHANGHAI)
        date_str = now.strftime("%Y年%m月%d日（%A）")
        time_str = now.strftime("%H:%M")

        # 构建 @mention 信息，注入给 Claude 参考
        mentions_info = ""
        if msg.mentions:
            lines = ["消息中 @mention 的用户（可直接用其 open_id 作为与会者）："]
            for m in msg.mentions:
                lines.append(f"  - {m.name}，open_id: {m.open_id}")
            mentions_info = "\n".join(lines)

        # 群聊消息：注入当前群的 chat_id，Claude 可直接用它读取群消息，无需先调用 list_groups
        if msg.chat_type == "group" and msg.chat_id:
            group_ctx = (
                f"\n\n【当前消息来自群聊，chat_id = `{msg.chat_id}`】\n"
                "可直接用此 chat_id 调用 feishu_action(read_group_messages) 读取本群消息；\n"
                "也可用此 chat_id 调用 feishu_action(send_message) 向本群发消息（send_to_chat=true）。"
            )
            mentions_info = (mentions_info + group_ctx) if mentions_info else group_ctx.strip()

        owner_id_hint = (
            f"发起人 {self.owner_name} 的 open_id 为 `{self.owner_open_id}`，必须加入与会者列表。"
            if self.owner_open_id else
            f"发起人是 {self.owner_name}，请确保他/她也在与会者列表中（用 search_users 获取其 open_id）。"
        )

        logger.debug("[CHECKPOINT A] 开始构建 system_prompt")
        system_prompt = f"""你是 {self.owner_name} 的飞书个人助理机器人。今天是 {date_str}，现在是 {time_str}（北京时间）。

## 当前能力

### 1. 会议邀请
创建飞书日历会议并邀请与会者，支持查询多人空闲时间。

**工作流 A — 指定人员会议：**
1. `search_users` 获取各参与者 open_id（@mention 直接可用，姓名搜索需调用工具）
2. `query_availability` 查询所有人的共同空闲时间（含发起人自己），展示候选时段供用户确认
3. 用户确认时间后，`create_meeting` 创建会议并向所有人发送邀请 + IM 消息通知
4. 系统自动追踪 RSVP 状态，有人拒绝时第一时间通知发起人
5. 用户要求取消会议时，`cancel_meeting` 删除日历事件并通知与会者（需要 event_id + calendar_id，可从历史对话中获取）

**工作流 B — 群内全员会议（用户说"给群里的人"/"全员"/"群里所有人"）：**
⚠️ **必须严格按此流程，禁止用 read_group_messages 来推断群成员**
1. 调用 `feishu_action(get_group_members, chat_id=当前群chat_id)` 获取群成员列表
2. 将成员列表展示给用户，询问："哪些人是必选参与者（Required）？其余将标记为 Optional。"
3. 用户确认后，`query_availability` 查询空闲时间
4. `create_meeting` 时：必选成员 → `attendee_open_ids`，其余 → `optional_attendee_open_ids`

**规则：**
- 若用户未指定结束时间，默认会议时长 1 小时
- 若用户未指定地点，location 留空
- {owner_id_hint}
- 可通过 @mention 直接获取 open_id，也可用 search_users 工具按姓名查找
- 用户要求"查看空闲时间/找可用时间"时，先调用 query_availability 再 create_meeting

### 2. 飞书知识库 + IM 消息 + 群聊记录
使用 **feishu_action** 工具，通过 action 参数指定操作：
- **action=search**：按关键词搜索 Wiki，返回相关页面列表
- **action=read_page**：读取指定飞书页面的正文内容（Markdown 格式）
- **action=list_pages**：列出某个空间或父页面下的所有子页面
- **action=create_page**：在指定位置新建 Wiki 页面
- **action=edit_page**：替换页面中的指定文字片段（精确匹配）
- **action=move_page**：将页面移动到另一个目录下（需要 url + target_url）
- **action=send_message**：发送飞书 IM 文本消息
  - 发私信：open_id + message
  - 发群消息：chat_id + message + send_to_chat=true；可在 message 中用 `<at user_id="对方open_id">姓名</at>` @mention 他人
  - ⚠️ **必须真正调用此工具才算发送成功**，不能只把消息内容写在回复卡片里
  - 成功时工具返回 `message_id=om_xxx`，记下此 ID——用户若要撤回时直接用
- **action=recall_message**：撤回（删除）一条消息（需要 message_id；只能撤回 Bot 自身发送的消息）
  - message_id 来源：① 刚才 send_message 返回的结果；② 先 read_group_messages 查最近消息列表，每条都带 `[id:om_xxx]`
- **action=list_groups**：获取 Bot 所在的所有群列表（返回群名 + chat_id）
- **action=read_group_messages**：读取群或 P2P 私信最近 N 小时的消息（群用 chat_id，与某用户的私信用 p2p_open_id；可先 search_users 获取 open_id；可选 hours，默认 24）；每条消息会带 `[id:om_xxx]`，可用于撤回
- 用途示例：更新项目状态、记录会议纪要、维护文档、发送通知、按需读取群/私信消息并总结

**每日群摘要（自动后台任务）**：Bot 每天 00:00 自动读取所有群的昨日消息，生成摘要并发布到飞书知识库。首次处理某个群时会 DM 你确认存放位置。你也可以用 `feishu_action(list_groups + read_group_messages)` 按需读取任意群消息。

### 3. 客户汇报 PPT 生成
根据飞书知识库中的资料，生成 Momenta 风格的客户汇报 PPT。
- 工作流：feishu_action(search) 找页面 → feishu_action(read_page) 读内容 → generate_ppt 生成文件
- PPT 生成后自动保存到本地并上传飞书云盘，返回飞书链接
- PPT 格式：深蓝封面、蓝色标题栏、要点列表，符合 Momenta 品牌规范

### 3b. 修改已有 PPTX 文件（关键）
当用户要求修改飞书云盘中的 PPTX 时，必须使用以下工作流：
1. feishu_action(read_page) — 读取包含该 PPTX 的飞书页面，从结果末尾的【文件 token：xxx】获取 obj_token
2. feishu_action(inspect_pptx, obj_token=..., slide_index=N) — 查看目标幻灯片的所有形状坐标和文字
3. **立即调用** feishu_action(edit_pptx, obj_token=..., replacements=[...], shape_updates=[...]) — 应用修改
   - **inspect 之后必须 edit，不能只 inspect 不 edit！**
   - replacements：替换文字内容（如日期、文本）
   - shape_updates：移动形状位置（如时间线上的线条、标注框，需和文字日期同步移动）
4. edit_pptx 会保存修改后的文件并上传到飞书同目录
- 如果一次 inspect_pptx 信息不够，可多次 inspect 不同页
- 时间线类修改：必须同时更新文字（replacements）和形状位置（shape_updates），确保视觉对齐

### 4. 需求三抓 PRT
帮助产品经理挖掘、提炼、验证核心需求，输出标准 PRT 飞书文档。
- 触发词：「需求三抓」「写PRT」「做PRT」「prt」「三抓」「需求挖掘」
- 三种模式：模式1 对话挑战 / 模式2 群消息分析 / 模式3 分布式调研
- 完成后自动质量 Review + OPP 联动
- 使用 `feishu_action(create_page)` 将 PRT 文档发布到用户个人飞书知识库
- **创建页面后必须紧接着调用 `feishu_action(apply_mentions)`**，将文档中的 @Name 纯文本替换为真正的飞书 @mention。mention_map 来源：消息中 @mention 的用户（系统已提供其 open_id），以及通过 search_users 查到的用户

### 5. OPP 计划沟通文档
帮助用户快速完成 OPP（OKR-Plan-Progress）计划沟通文档，自动发布到飞书个人知识库。
- 触发词：「opp」「写OPP」「创建OPP」「OPP文档」「计划沟通」
- 工作流：需求三抓检查 → 起名 → 讨论OKR → 大致方案 → 发布到飞书 → 行动建议
- 使用 `feishu_action(create_page)` 将 OPP 文档发布到用户个人飞书知识库

### 6. 飞书项目工作项管理（MCP）
使用 **feishu_project** 工具通过 MCP 查询和管理飞书项目工作项：
- **action=get_workitem_brief**：查询工作项基本信息（需要 project_key + work_item_id）
- **action=get_workitem_info**：查询工作项类型配置（字段列表、必填字段等）
- **action=get_view_detail**：查询视图数据，支持 TopN、统计、分组
- **action=create_workitem**：创建工作项（先查询字段配置，智能收集必填字段）
- **action=update_field**：更新工作项字段
- **action=finish_node**：推进工作项节点
- **action=search_by_mql**：MOQL 高级查询（支持复杂筛选条件）
- 项目名自动映射：触发飞书项目相关操作时，Skill 指令会被注入，自动读取 project-mapping.md 找到 project_key

### 7. Mviz Bag 数据分析
使用 **bag_analysis** 工具分析自动驾驶数据：
- **action=search_streams**：按关键词搜索 Mviz stream（如 'aes'、'fusion'、'lidar'）
- **action=lookup_stream**：查看某 stream 订阅的 ROS topic 和渲染逻辑
- **action=resolve_url**：解析 Mviz/bag URL，提取 MD5 和 storage 参数
- **action=download_topic**：从 Mviz URL 或 bag MD5 下载指定 topic 数据并返回摘要
- 典型工作流：search_streams 找目标 → lookup_stream 确认 topic → download_topic 获取数据 → 分析
- 支持输入：Mviz 链接（含 bag_md5= 或 meta= 参数）、直接 bag MD5
- **速度优化**：若已通过 resolve_url 获得 bag_md5，后续 download_topic 时直接传入 `bag_md5` 参数（同时保留 url 参数），系统会自动跳过重复解析，节省约 30 秒

### 8. GB 国标检索
使用 **search_gb_standard** 工具查询自动驾驶相关国家标准原文：
- **standard=l2pp**：《智能网联汽车 组合驾驶辅助系统安全要求》GB 报批稿，适用于 L2++/LACS/领航/组合驾驶辅助相关问题
- **standard=ads_l3l4**：《智能网联汽车 自动驾驶系统安全要求》征求意见稿，适用于 L3/L4 ADS 技术要求、接管能力、MRM、ODD 等问题
- 触发词：国标/GB/强标/L2++/L3/L4/ADS/组合驾驶辅助/LACS/领航/接管/TTC/ODC 等
- **必须先调用工具检索原文**，不得凭记忆或推测作答
- 工作流：search_gb_standard(query=核心术语) → 基于返回条款原文组织回答 → 给出条款编号依据
- 若单次搜索结果不全，可用不同关键词多次搜索

### 9. Skill 搜索
使用 **find_skills** 工具搜索推荐 Agent Skill：
- 触发词：找 skill、搜索 skill、有没有能做 X 的工具、推荐一个 skill、有没有 X 的 skill
- 工作流：find_skills(query=关键词) → 从结果中识别最相关的 skill → 给出名称、描述、安装命令
- 公共库建议用英文关键词（如 `code review`、`diagram`），内部库中英文均可
- 可多次调用，每次换不同关键词，扩大搜索范围

### 10. 项目二级计划生成
使用 **generate_pm_plan** 工具生成 Momenta L2++ 适配项目二级开发计划甘特图：
- 触发词：生成项目计划、排项目计划、生成二级计划、项目排期、PM计划
- 必填：project_name（项目名称）、t0_date（T0 日期，YYYY-MM-DD）
- 可选：drive_folder_token（上传 HTML/PNG 到 Drive）、wiki_parent_url（在 Wiki 下创建文档）
- 工作流：提取项目名称和 T0 日期 → 调用 generate_pm_plan → 返回 HTML 路径和飞书链接
- 飞书 Drive 文件夹 URL 格式 `https://momenta.feishu.cn/drive/folder/XXXXXXXX`，URL 末尾字符串即为 drive_folder_token

## 回复格式规范（重要）
回复会被渲染为飞书富文本卡片，请遵循以下格式：

1. **开头标题**：第一行必须是 `# 简短标题`（5–15字，概括内容）
2. **章节分级**：用 `## 章节名` 划分主要部分，`### 子节` 作次级标题（渲染时自动加粗，不显示 # 号）
3. **结构化数据必须用表格**：状态列表、参数对比、配置项、Topic 列表等，一律使用 Markdown 表格格式：
   ```
   | 字段 | 值 |
   |------|-----|
   | 内容 | 内容 |
   ```
4. **枚举多个条目使用数字序号**：介绍多个功能、步骤或结论时用 `1.` `2.` `3.`，而非单纯 `-` 列表堆叠
5. **分割线 `---`** 仅用于主要章节之间，不要频繁使用
6. **单个章节内容控制在 400 字以内**，超长时拆分为多个子节
7. 操作完成后给出简明摘要；若信息不足，直接向用户询问，不要猜测
17. **单字/极短回复（如"需要"、"好"、"是"、"继续"、"要"、"可以"）**：结合上一轮对话历史解读含义，视为对前一条消息的确认或跟进，直接执行或继续，**不要反问"需要什么帮助"**
8. 不要编造 open_id 或飞书 URL，必须通过工具获取
9. 搜索飞书时：先 feishu_action(search) 找页面，再 feishu_action(read_page) 读详情
10. 生成 PPT 前：先从飞书收集足够内容，不要用空洞占位符
11. Bag 分析时：优先 search_streams 找 topic，再 download_topic 获取数据；已知 bag_md5 时直接传入 bag_md5 参数
12. 修改 PPTX 时：inspect_pptx 仅用于获取信息，**必须紧接着调用 edit_pptx 才能真正保存修改**，不能只 inspect 就结束
13. 发送 IM 消息时：**必须调用 feishu_action(send_message) 工具**，不能只在回复里写消息内容就声称"已发送"——那只是回复给用户的文字，并未真正发出飞书消息
14. 回答国标/GB/强标问题时：**必须先调用 search_gb_standard 工具**检索原文条款，基于条款原文作答并标注条款编号，不得凭记忆回答
15. 用户询问有没有某类 skill/工具时：**调用 find_skills 工具**搜索，不要凭印象猜测
16. 用户要求生成项目计划/二级计划/排期时：**调用 generate_pm_plan 工具**，提取项目名称和 T0 日期，若未提供则先询问

{mentions_info}"""

        # 检测触发词：当前消息或近期历史中含触发词时，注入对应 Skill 指令
        history_text = " ".join(
            m["content"] for m in self._histories.get(msg.sender_open_id, [])
            if isinstance(m.get("content"), str)
        )
        combined_text = (msg.clean_text + history_text).lower()
        if _PRT_SKILL_CONTENT and any(kw.lower() in combined_text for kw in _PRT_TRIGGERS):
            system_prompt += f"\n\n---\n\n{_PRT_SKILL_CONTENT}"
            logger.info("PRT Skill 指令已注入（触发词检测到）")
        if _OPP_SKILL_CONTENT and any(kw.lower() in combined_text for kw in _OPP_TRIGGERS):
            system_prompt += f"\n\n---\n\n{_OPP_SKILL_CONTENT}"
            logger.info("OPP Skill 指令已注入（触发词检测到）")
        if _INTEGRATION_SKILL_CONTENT and any(kw.lower() in combined_text for kw in _INTEGRATION_TRIGGERS):
            system_prompt += f"\n\n---\n\n{_INTEGRATION_SKILL_CONTENT}"
            logger.info("integration-guide Skill 已注入（触发词检测到）")
        if _WEB_SKILL_CONTENT and any(kw.lower() in combined_text for kw in _WEB_TRIGGERS):
            system_prompt += f"\n\n---\n\n{_WEB_SKILL_CONTENT}"
            logger.info("momenta-web-skill Skill 已注入（触发词检测到）")
        if _FTT_SKILL_CONTENT and any(kw.lower() in combined_text for kw in _FTT_TRIGGERS):
            system_prompt += f"\n\n---\n\n{_FTT_SKILL_CONTENT}"
            logger.info("ftt-workflow Skill 已注入（触发词检测到）")
        if _MVIZ_RECORDER_SKILL_CONTENT and any(kw.lower() in combined_text for kw in _MVIZ_RECORDER_TRIGGERS):
            system_prompt += f"\n\n---\n\n{_MVIZ_RECORDER_SKILL_CONTENT}"
            logger.info("mviz-recorder Skill 已注入（触发词检测到）")
        if _ROUTE_EXTRACT_SKILL_CONTENT and any(kw.lower() in combined_text for kw in _ROUTE_EXTRACT_TRIGGERS):
            system_prompt += f"\n\n---\n\n{_ROUTE_EXTRACT_SKILL_CONTENT}"
            logger.info("route-extract Skill 已注入（触发词检测到）")
        if _APA_SNAPSHOT_SKILL_CONTENT and any(kw.lower() in combined_text for kw in _APA_SNAPSHOT_TRIGGERS):
            system_prompt += f"\n\n---\n\n{_APA_SNAPSHOT_SKILL_CONTENT}"
            logger.info("apa-mviz-snapshot Skill 已注入（触发词检测到）")
        if _FEISHU_PROJECT_SKILL_CONTENT and any(kw.lower() in combined_text for kw in _FEISHU_PROJECT_TRIGGERS):
            system_prompt += f"\n\n---\n\n{_FEISHU_PROJECT_SKILL_CONTENT}"
            logger.info("feishu-project Skill 已注入（触发词检测到）")

        # 载入该用户的对话历史（纯文本轮次，不含工具调用中间状态）
        history = list(self._histories.get(msg.sender_open_id, []))

        # ── 请求分类与模型路由 ──────────────────────────────────────
        last_reply = next(
            (m["content"] for m in reversed(history) if m.get("role") == "assistant"),
            "",
        )
        req_category = self._classify_request(msg.clean_text, last_reply)
        logger.info("[路由] 请求分类: %s（消息: %s…）", req_category, msg.clean_text[:40])

        # ── simple：Haiku 直接回答，不调用工具 ──────────────────────
        if req_category == "simple":
            simple_system = (
                f"你是 {self.owner_name} 的飞书个人助理机器人。"
                f"今天是 {date_str}，现在是 {time_str}（北京时间）。"
                f"主要能力：会议邀请、飞书知识库读写、客户汇报PPT生成、"
                f"需求三抓PRT、OPP文档、飞书项目工作项管理、Mviz Bag数据分析、"
                f"L2++项目二级计划甘特图生成、GB国标条款检索、Agent Skill搜索推荐。"
                f"请简短友好地回答用户问题（2-4句话）。"
            )
            if mentions_info:
                simple_system += f"\n{mentions_info}"
            try:
                resp = self.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=1024,
                    system=simple_system,
                    messages=history + [{"role": "user", "content": msg.clean_text}],
                )
                text = self._extract_text(resp)
                new_history = history + [
                    {"role": "user", "content": msg.clean_text},
                    {"role": "assistant", "content": text},
                ]
                self._histories[msg.sender_open_id] = new_history[-(self._MAX_HISTORY_TURNS * 2):]
                return text
            except Exception as e:
                logger.warning("[路由] Haiku 调用失败，回退到 read 模式: %s", e)
                req_category = "read"

        # ── 选择工具调用模型 ─────────────────────────────────────────
        if req_category in ("write", "complex"):
            _model = "claude-opus-4-6"
            _thinking: dict | None = {"type": "adaptive"}
            _max_tokens = 16384
        else:  # read
            _model = "claude-sonnet-4-6"
            _thinking = None
            _max_tokens = 8192

        logger.info("[路由] 使用模型: %s, thinking: %s", _model, _thinking)
        # ─────────────────────────────────────────────────────────────

        messages = history + [{"role": "user", "content": msg.clean_text}]

        logger.info("[DEBUG] API base_url=%s", os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com (default)"))
        logger.info("[DEBUG] 发送 %d 个工具到 API: %s，历史轮数: %d",
                    len(TOOL_DEFINITIONS), [t["name"] for t in TOOL_DEFINITIONS], len(history) // 2)

        for _round in range(MAX_TOOL_ROUNDS):
            logger.info("[DEBUG] 第 %d 轮 API 请求，messages 长度: %d", _round + 1, len(messages))
            try:
                api_kwargs: dict = dict(
                    model=_model,
                    max_tokens=_max_tokens,
                    system=system_prompt,
                    tools=TOOL_DEFINITIONS,
                    messages=messages,
                )
                if _thinking:
                    api_kwargs["thinking"] = _thinking
                response = self.client.messages.create(**api_kwargs)
            except Exception as api_err:
                logger.exception("[ERROR] 第 %d 轮 API 调用失败: %s", _round + 1, api_err)
                return f"API 请求失败（第 {_round + 1} 轮）：{api_err}"

            logger.info("[DEBUG] API 响应 stop_reason=%s, content_types=%s",
                        response.stop_reason,
                        [b.type for b in response.content])

            # 没有工具调用，Claude 已完成
            if response.stop_reason == "end_turn":
                text = self._extract_text(response)
                logger.info("最终回复（前300字）: %s", text[:300])
                # 保存本轮到历史（仅保留纯文本，不包含工具调用中间步骤）
                new_history = history + [
                    {"role": "user", "content": msg.clean_text},
                    {"role": "assistant", "content": text},
                ]
                max_msgs = self._MAX_HISTORY_TURNS * 2
                self._histories[msg.sender_open_id] = new_history[-max_msgs:]
                return text

            # 有工具调用
            if response.stop_reason == "tool_use":
                tool_blocks = [b for b in response.content if b.type == "tool_use"]
                # 将助手消息（含 tool_use 块）追加到上下文
                # thinking 模式下可能出现空 text block，API 不接受空 text，需过滤
                filtered_content = [
                    b for b in response.content
                    if not (b.type == "text" and not (b.text or "").strip())
                ]
                messages.append({"role": "assistant", "content": filtered_content})

                # 执行所有工具并收集结果
                tool_results = []
                for tb in tool_blocks:
                    logger.info("调用工具: %s，参数: %s", tb.name, tb.input)
                    try:
                        result_text = self.executor.execute(tb.name, tb.input)
                    except Exception as tool_err:
                        logger.exception("[ERROR] 工具 %s 执行异常: %s", tb.name, tool_err)
                        result_text = f"[工具执行错误] {tb.name}: {tool_err}"
                    logger.info("工具结果: %s", result_text[:200])
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tb.id,
                        "content": result_text,
                    })

                messages.append({"role": "user", "content": tool_results})
                continue

            # max_tokens：输出被截断，无法完成操作
            if response.stop_reason == "max_tokens":
                partial = self._extract_text(response)
                logger.warning("stop_reason=max_tokens（第 %d 轮），partial=%s", _round + 1, partial[:200])
                return (
                    partial or
                    "⚠️ 回复被 max_tokens 截断，任务未完成。请简化请求或分步操作后重试。"
                )

            # 其他停止原因（refusal 等）
            logger.warning("意外的 stop_reason: %s", response.stop_reason)
            return self._extract_text(response) or "处理时出现问题，请重试。"

        return "操作超过最大步骤限制，请稍后重试或简化您的请求。"

    def process_non_owner(self, msg: "BotMessage", sender_name: str) -> str:
        """处理非 owner 的 P2P 消息，使用精简 system prompt，不调用工具。"""
        import time as _time

        # 拉取今日 P2P 对话历史，注入给 Claude 参考
        chat_history_ctx = ""
        if msg.chat_id:
            try:
                end_ts = int(_time.time())
                start_ts = end_ts - 24 * 3600
                msgs = self.executor.feishu.get_group_messages(
                    msg.chat_id, start_ts, end_ts, max_msgs=100
                )
                if msgs:
                    lines = [
                        f"[{m.get('sender_name', '?')}] {m.get('text', '')}"
                        for m in msgs
                    ]
                    chat_history_ctx = (
                        f"\n\n以下是今天你与{sender_name}的对话记录（供你参考，"
                        "如对方询问历史内容可基于此回答）：\n"
                        + "\n".join(lines)
                    )
            except Exception as e:
                logger.warning("拉取 non-owner 对话历史失败: %s", e)

        system_prompt = (
            f"你是 {self.owner_name} 的飞书个人助理机器人。"
            f"现在有位叫「{sender_name}」的用户直接给你发了消息（他/她不是 {self.owner_name}）。"
            f"{self.owner_name} 已收到消息通知。\n\n"
            f"你的任务：\n"
            f"- 如果对方在问一个你能直接回答的问题（包括总结今日对话），简短回答（2-4句话）\n"
            f"- 如果对方是在给 {self.owner_name} 传话/回复，告知「消息已转告给{self.owner_name}，稍后会与你联系」\n"
            f"- 不要假装你能帮对方执行任务（预约会议、查文档等），这些权限只有 {self.owner_name} 才能使用\n"
            f"- 语气友好简洁，不要过度解释"
            + chat_history_ctx
        )
        try:
            response = self.client.messages.create(
                model="claude-opus-4-6",
                max_tokens=512,
                system=system_prompt,
                messages=[{"role": "user", "content": msg.clean_text}],
            )
            return self._extract_text(response)
        except Exception as e:
            logger.warning("process_non_owner 失败: %s", e)
            return f"收到，已转告给{self.owner_name}，稍后会与你联系。"

    def clear_history(self, sender_open_id: str) -> None:
        """清除指定用户的对话历史（由 /clear 命令触发）"""
        self._histories.pop(sender_open_id, None)
        logger.info("已清除用户 %s 的对话历史", sender_open_id)

    @staticmethod
    def _classify_request(text: str, last_assistant_reply: str = "") -> str:
        """快速分类请求，决定使用哪个模型和 thinking 配置。
        Returns: "simple" | "read" | "write" | "complex"
        """
        t = text.strip().lower()

        # 极短消息：问候 / 感谢 → simple
        if len(t) <= 20:
            if any(t.startswith(g.lower()) or t == g.lower() for g in _SIMPLE_GREETINGS):
                return "simple"
            if any(p.lower() in t for p in _SIMPLE_CAPABILITY_PATTERNS):
                return "simple"

        # 能力咨询（任意长度）
        if any(p.lower() in t for p in _SIMPLE_CAPABILITY_PATTERNS):
            return "simple"

        # 复杂多步骤任务（不可降级）
        if any(kw in t for kw in _COMPLEX_KEYWORDS):
            return "complex"

        # 写操作
        if any(kw in t for kw in _WRITE_KEYWORDS):
            return "write"

        # 上一轮助手正在引导确认写操作，当前是短响应 → 跟进写操作
        if last_assistant_reply and len(t) <= 15:
            last_lower = last_assistant_reply.lower()
            if any(kw in last_lower for kw in ("请确认", "确认后", "是否继续", "请问是否", "已准备好")):
                return "write"

        # 默认：读操作（搜索 / 查询 / 读取）
        return "read"

    @staticmethod
    def _extract_text(response) -> str:
        """从响应内容中提取文本块"""
        parts = []
        for block in response.content:
            if block.type == "text" and block.text.strip():
                parts.append(block.text.strip())
        return "\n\n".join(parts) if parts else "完成。"
