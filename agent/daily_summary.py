"""群聊摘要功能：读取群消息 → Claude 摘要 → 发布飞书 Wiki → DM 通知

Tier 调度策略：
  daily   (每天自动)  : HOT 群（≤1天有消息）
  weekly  (每周自动)  : HOT + ACTIVE 群（≤7天有消息）
  biweekly/monthly/quarterly (按需触发) : 覆盖更冷的群
  zombie  : 完全跳过调度，仅按需
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta

import anthropic
import pytz

from .group_activity import (
    GroupActivityCache,
    REPORT_DAYS,
    REPORT_LABELS,
    REPORT_TIERS,
    TIER_LABELS,
)

logger = logging.getLogger(__name__)
TZ_SHANGHAI = pytz.timezone("Asia/Shanghai")

# 持久化文件（放在项目根目录）
_BASE_DIR      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAPPINGS_FILE = os.path.join(_BASE_DIR, ".space_mappings.json")
_ACTIVITY_FILE = os.path.join(_BASE_DIR, ".group_activity.json")

FEISHU_WIKI_BASE = "https://momenta.feishu.cn/wiki"

# 匹配群名中的地名关键词（XX山 / XX河 / XX湖）
_PLACE_PATTERN = re.compile(r"([\u4e00-\u9fff]{1,6}[山河湖])")


# ------------------------------------------------------------------ #
# 工具函数
# ------------------------------------------------------------------ #

def _load_json(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_json(path: str, data: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("保存 %s 失败: %s", path, e)


def _find_feishu_sync_cli() -> str:
    """按优先级查找 feishu-sync-cli 可执行文件路径。"""
    env_path = os.environ.get("FEISHU_SYNC_CLI", "")
    if env_path and os.path.isfile(env_path):
        return env_path
    found = shutil.which("feishu-sync-cli")
    if found:
        return found
    candidates = [
        os.path.expanduser("~/Library/Python/3.9/bin/feishu-sync-cli"),
        os.path.expanduser("~/Library/Python/3.10/bin/feishu-sync-cli"),
        os.path.expanduser("~/Library/Python/3.11/bin/feishu-sync-cli"),
        os.path.expanduser("~/.local/bin/feishu-sync-cli"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return "feishu-sync-cli"


# ------------------------------------------------------------------ #
# DailySummaryJob
# ------------------------------------------------------------------ #

class DailySummaryJob:
    """群聊摘要调度任务。

    调度入口：
      run()        → daily   (每天 00:00:05 自动，仅 HOT 群)
      run_weekly() → weekly  (每周一 00:01:00 自动，HOT+ACTIVE 群)
      run_for_report_type(report_type, days_back) → 按需触发任意类型
    """

    def __init__(
        self,
        feishu_client,
        anthropic_api_key: str,
        owner_open_id: str,
    ):
        self._feishu        = feishu_client
        self._owner_open_id = owner_open_id
        self._feishu_sync_cli = _find_feishu_sync_cli()
        logger.info("feishu-sync-cli 路径: %s", self._feishu_sync_cli)

        base_url = os.environ.get("ANTHROPIC_BASE_URL")
        self._claude = anthropic.Anthropic(
            api_key=anthropic_api_key,
            **({"base_url": base_url} if base_url else {}),
        )

        self._spaces_cache: list[dict] = []
        self._activity = GroupActivityCache(_ACTIVITY_FILE)

    # ------------------------------------------------------------------ #
    # 公开调度入口
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        """每天 00:00:05 自动运行：只处理 HOT 群（昨日有消息）。"""
        self.run_for_report_type("daily")

    def run_weekly(self) -> None:
        """每周一 00:01:00 自动运行：处理本周有消息的群（HOT + ACTIVE）。"""
        self.run_for_report_type("weekly")

    def run_for_report_type(
        self,
        report_type: str = "daily",
        days_back: int | None = None,
    ) -> list[dict]:
        """通用摘要入口，支持 daily / weekly / biweekly / monthly / quarterly。

        Args:
            report_type: 报告类型，决定 tier 过滤范围和默认回看天数。
            days_back:   回看天数，默认由 report_type 决定。

        Returns:
            results 列表（每个元素对应一个处理过的群）。
        """
        if days_back is None:
            days_back = REPORT_DAYS.get(report_type, 1)

        now       = datetime.now(TZ_SHANGHAI)
        end_day   = now - timedelta(days=1)
        start_day = now - timedelta(days=days_back)

        start_ts = int(start_day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        end_ts   = int(end_day.replace(hour=23, minute=59, second=59, microsecond=0).timestamp())

        if days_back == 1:
            date_str = end_day.strftime("%Y-%m-%d")
        else:
            date_str = (
                f"{start_day.strftime('%Y-%m-%d')} 至 {end_day.strftime('%Y-%m-%d')}"
            )

        report_label = REPORT_LABELS.get(report_type, report_type)
        logger.info(
            "%s 任务开始: %s，回看 %d 天，tier 范围: %s",
            report_label, date_str, days_back,
            {TIER_LABELS.get(t, t) for t in REPORT_TIERS.get(report_type, set())},
        )

        # 刷新 wiki 空间缓存
        self._spaces_cache = self._feishu.list_wiki_spaces()

        mappings: dict = _load_json(_MAPPINGS_FILE)

        # 获取用户所在群列表
        groups = self._feishu.get_user_joined_groups()
        if groups:
            logger.info("用户所在群: %d 个", len(groups))
        else:
            logger.warning("用户 IM token 不可用，回退到 Bot 所在群")
            groups = self._feishu.get_joined_groups()
            logger.info("Bot 所在群: %d 个", len(groups))

        # Tier 过滤：跳过确定不在范围内的群
        to_process, to_skip = self._activity.split_groups(groups, report_type)
        logger.info(
            "Tier 过滤: 待处理 %d 个，跳过 %d 个（缓存显示 tier 不符）",
            len(to_process), len(to_skip),
        )
        if to_skip:
            skip_preview = ", ".join(g.get("name", g["chat_id"]) for g in to_skip[:5])
            logger.info("跳过示例: %s%s", skip_preview, "..." if len(to_skip) > 5 else "")

        results: list[dict] = []

        for group in to_process:
            chat_id    = group["chat_id"]
            group_name = group.get("name") or chat_id
            try:
                entry = self._process_group(
                    chat_id=chat_id,
                    group_name=group_name,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    date_str=date_str,
                    mappings=mappings,
                    report_type=report_type,
                )
                if entry:
                    results.append(entry)
            except Exception as e:
                logger.error("处理群 [%s] 失败: %s", group_name, e)
                results.append({
                    "group_name": group_name,
                    "msg_count":  0,
                    "result_str": f"处理失败：{e}",
                    "page_url":   "",
                })

        # 保存持久化数据
        _save_json(_MAPPINGS_FILE, mappings)
        self._activity.save()

        # 打印活跃度统计
        stats = self._activity.stats()
        stats_str = "  ".join(
            f"{TIER_LABELS.get(t, t)}:{n}" for t, n in stats.items() if n > 0
        )
        logger.info(
            "%s 任务完成: %s，生成 %d 份摘要。缓存统计: %s",
            report_label, date_str, len(results), stats_str,
        )

        if results:
            self._send_summary_dm(date_str, results, report_label)

        return results

    # ------------------------------------------------------------------ #
    # 单群处理
    # ------------------------------------------------------------------ #

    def _process_group(
        self,
        chat_id: str,
        group_name: str,
        start_ts: int,
        end_ts: int,
        date_str: str,
        mappings: dict,
        report_type: str = "daily",
    ) -> dict | None:
        """处理单个群：读消息 → 更新 activity 缓存 → 摘要 → 发布。无消息返回 None。"""
        messages = self._get_messages_for_group(chat_id, start_ts, end_ts)

        if not messages:
            self._activity.update_empty(chat_id, group_name)
            logger.info("群 [%s] 期间无消息，跳过", group_name)
            return None

        # 更新活跃缓存
        last_msg_ts = max(m.get("ts", 0) for m in messages)
        new_tier = self._activity.update_found(chat_id, group_name, last_msg_ts)
        logger.info(
            "群 [%s] %d 条消息，tier=%s，开始摘要",
            group_name, len(messages), TIER_LABELS.get(new_tier, new_tier),
        )

        summary_md = self._summarize(group_name, date_str, messages)

        # 确定目标 Wiki 空间
        if chat_id in mappings:
            root_token = mappings[chat_id]["root_node_token"]
            space_name = mappings[chat_id].get("space_name", "")
        else:
            space_id, space_name = self._suggest_space(group_name)
            root_token = self._feishu.get_space_root_node_token(space_id) if space_id else ""
            if root_token:
                mappings[chat_id] = {
                    "space_id":        space_id,
                    "space_name":      space_name,
                    "root_node_token": root_token,
                }
                logger.info("群 [%s] 自动映射到空间「%s」", group_name, space_name)
            else:
                logger.warning("群 [%s] 未找到可用 Wiki 空间，跳过发布", group_name)
                return {
                    "group_name": group_name,
                    "msg_count":  len(messages),
                    "result_str": "未找到可用 Wiki 空间，摘要未发布",
                    "page_url":   "",
                }

        report_label = REPORT_LABELS.get(report_type, report_type)
        result_str, page_url = self._publish_doc(
            group_name=group_name,
            date_str=date_str,
            markdown=summary_md,
            root_node_token=root_token,
            report_label=report_label,
        )
        logger.info("群 [%s] 发布结果: %s", group_name, result_str)

        return {
            "group_name": group_name,
            "msg_count":  len(messages),
            "result_str": result_str,
            "page_url":   page_url,
            "space_name": space_name,
        }

    # ------------------------------------------------------------------ #
    # 内部方法
    # ------------------------------------------------------------------ #

    def _get_messages_for_group(
        self, chat_id: str, start_ts: int, end_ts: int
    ) -> list[dict]:
        """读取群消息：优先 Bot token（可靠直接 API），Bot 不在群时回退用户搜索 API。"""
        # 1. Bot token：速度快、覆盖全，Bot 所在群首选
        try:
            bot_result = self._feishu.get_group_messages(chat_id, start_ts, end_ts)
        except Exception as e:
            logger.warning("Bot token 读取 [%s] 异常，回退用户 token: %s", chat_id, e)
            bot_result = None

        if bot_result is not None:
            # None 表示 Bot 不在群；非 None（含 []）为有效结果
            return bot_result

        # 2. Bot 不在群 → 用用户 IM token + 搜索 API（尽力而为）
        logger.debug("群 [%s] Bot 不在其中，尝试用户身份读取", chat_id)
        user_token = self._feishu._get_user_im_token()
        if user_token:
            try:
                return self._feishu.get_group_messages_as_user(chat_id, start_ts, end_ts)
            except Exception as e:
                logger.warning("用户身份读取 [%s] 失败: %s", chat_id, e)
        return []

    def _summarize(self, group_name: str, date_str: str, messages: list[dict]) -> str:
        """调用 Claude 生成群消息摘要，返回 Markdown。"""
        lines = [
            f"{m.get('sender_name', '未知')}: {m.get('text', '')}"
            for m in messages
        ]
        chat_text = "\n".join(lines)

        prompt = (
            f"请对以下飞书群「{group_name}」{date_str} 的聊天记录进行整理和摘要。\n\n"
            "要求：\n"
            "1. 提炼出关键讨论议题和决策\n"
            "2. 列出重要的行动项和跟进事项（如有）\n"
            "3. 保留重要的数据、日期、链接\n"
            "4. 输出格式为 Markdown，使用清晰的章节结构\n"
            "5. 语言简洁，重点突出\n\n"
            f"聊天记录：\n{chat_text}"
        )

        try:
            response = self._claude.messages.create(
                model="claude-opus-4-6",
                max_tokens=4096,
                thinking={"type": "adaptive"},
                messages=[{"role": "user", "content": prompt}],
            )
            for block in response.content:
                if block.type == "text":
                    return block.text
        except Exception as e:
            logger.error("Claude 摘要失败: %s", e)

        return f"# {group_name} {date_str} 摘要\n\n（摘要生成失败，请检查日志）"

    def _suggest_space(self, group_name: str) -> tuple[str, str]:
        """根据群名推断建议的 Wiki 空间。优先匹配地名，其次个人空间。"""
        if not self._spaces_cache:
            return "", ""

        m = _PLACE_PATTERN.search(group_name)
        if m:
            keyword = m.group(1)
            for space in self._spaces_cache:
                if keyword in space.get("name", ""):
                    return space["space_id"], space["name"]

        for space in self._spaces_cache:
            if space.get("space_type") == "personal":
                return space["space_id"], space["name"]

        if self._spaces_cache:
            s = self._spaces_cache[0]
            return s["space_id"], s["name"]

        return "", ""

    def _publish_doc(
        self,
        group_name: str,
        date_str: str,
        markdown: str,
        root_node_token: str,
        report_label: str = "摘要",
    ) -> tuple[str, str]:
        """通过 feishu-sync-cli create_page 发布摘要页面。返回 (result_str, page_url)。"""
        title = f"{group_name} {date_str} {report_label}"
        parent_url = f"{FEISHU_WIKI_BASE}/{root_node_token}"
        try:
            result = subprocess.run(
                [self._feishu_sync_cli, "create_page", parent_url, title, markdown],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                logger.info("Wiki 页面已创建: %s", title)
                url_match = re.search(r"https://\S+wiki/\w+", result.stdout)
                page_url = url_match.group(0) if url_match else ""
                return "发布成功", page_url
            else:
                err = (result.stderr or result.stdout or "未知错误").strip()
                logger.error("create_page 失败: %s", err)
                return f"发布失败：{err[:200]}", ""
        except subprocess.TimeoutExpired:
            return "发布超时", ""
        except Exception as e:
            logger.error("_publish_doc 异常: %s", e)
            return f"发布异常：{e}", ""

    def _send_summary_dm(
        self, date_str: str, results: list[dict], report_label: str = "摘要"
    ) -> None:
        """向 owner 发送摘要汇总 DM。"""
        if not self._owner_open_id:
            logger.warning("未配置 owner_open_id，无法发送摘要 DM")
            return

        lines = [f"**{report_label} {date_str}**\n"]
        for r in results:
            name     = r["group_name"]
            count    = r["msg_count"]
            page_url = r.get("page_url", "")
            status   = r["result_str"]
            space    = r.get("space_name", "")

            if page_url:
                lines.append(f"📌 **{name}**（{count} 条）→ [查看摘要]({page_url})")
                if space:
                    lines.append(f"   存入：{space}")
            else:
                lines.append(f"📌 **{name}**（{count} 条）— {status}")

        text = "\n".join(lines)
        try:
            self._feishu.send_text_to_user(self._owner_open_id, text)
        except Exception as e:
            logger.error("发送摘要 DM 失败: %s", e)
