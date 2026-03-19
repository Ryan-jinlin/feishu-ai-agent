"""每日群聊摘要功能：读取昨日群消息 → Claude 摘要 → 直接发布飞书 Wiki → DM 通知"""
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

logger = logging.getLogger(__name__)
TZ_SHANGHAI = pytz.timezone("Asia/Shanghai")

# 持久化文件（放在项目根目录）
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAPPINGS_FILE = os.path.join(_BASE_DIR, ".space_mappings.json")

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
    """每天 00:00 读取 Bot 所在群的昨日消息，生成摘要，直接发布到飞书 Wiki 并 DM 通知 owner。"""

    def __init__(
        self,
        feishu_client,
        anthropic_api_key: str,
        owner_open_id: str,
    ):
        self._feishu = feishu_client
        self._owner_open_id = owner_open_id
        self._feishu_sync_cli = _find_feishu_sync_cli()
        logger.info("feishu-sync-cli 路径: %s", self._feishu_sync_cli)

        base_url = os.environ.get("ANTHROPIC_BASE_URL")
        self._claude = anthropic.Anthropic(
            api_key=anthropic_api_key,
            **({"base_url": base_url} if base_url else {}),
        )

        self._spaces_cache: list[dict] = []

    # ------------------------------------------------------------------ #
    # 主入口（APScheduler 每日调用）
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        """每天 00:00:05 运行：读昨日群消息 → 摘要 → 发布 wiki → DM 通知（附链接）。"""
        now = datetime.now(TZ_SHANGHAI)
        yesterday = now - timedelta(days=1)
        start_dt = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt   = yesterday.replace(hour=23, minute=59, second=59, microsecond=0)
        start_ts = int(start_dt.timestamp())
        end_ts   = int(end_dt.timestamp())
        date_str = yesterday.strftime("%Y-%m-%d")

        logger.info("每日摘要任务开始: %s", date_str)

        # 刷新 wiki 空间缓存
        self._spaces_cache = self._feishu.list_wiki_spaces()
        logger.info("Wiki 空间: %d 个", len(self._spaces_cache))

        mappings: dict = _load_json(_MAPPINGS_FILE)

        groups = self._feishu.get_joined_groups()
        logger.info("Bot 所在群: %d 个", len(groups))

        results: list[dict] = []  # [{group_name, msg_count, result_str, page_url}]

        for group in groups:
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
                )
                if entry:
                    results.append(entry)
            except Exception as e:
                logger.error("处理群 [%s] 摘要失败: %s", group_name, e)
                results.append({
                    "group_name": group_name,
                    "msg_count": 0,
                    "result_str": f"处理失败：{e}",
                    "page_url": "",
                })

        _save_json(_MAPPINGS_FILE, mappings)
        logger.info("每日摘要任务完成: %s，共处理 %d 个群", date_str, len(results))

        # 发送汇总 DM
        if results:
            self._send_summary_dm(date_str, results)

    def _process_group(
        self,
        chat_id: str,
        group_name: str,
        start_ts: int,
        end_ts: int,
        date_str: str,
        mappings: dict,
    ) -> dict | None:
        """处理单个群：摘要 → 直接发布 → 返回结果 dict。无消息时返回 None。"""
        messages = self._feishu.get_group_messages(chat_id, start_ts, end_ts)
        if not messages:
            logger.info("群 [%s] 昨日无消息，跳过", group_name)
            return None

        logger.info("群 [%s] 昨日 %d 条消息，开始摘要", group_name, len(messages))
        summary_md = self._summarize(group_name, date_str, messages)

        # 确定目标空间（已有映射直接用；否则自动推断并保存）
        if chat_id in mappings:
            root_token = mappings[chat_id]["root_node_token"]
            space_name = mappings[chat_id].get("space_name", "")
        else:
            space_id, space_name = self._suggest_space(group_name)
            root_token = self._feishu.get_space_root_node_token(space_id) if space_id else ""
            if root_token:
                mappings[chat_id] = {
                    "space_id": space_id,
                    "space_name": space_name,
                    "root_node_token": root_token,
                }
                logger.info("群 [%s] 自动映射到空间「%s」", group_name, space_name)
            else:
                logger.warning("群 [%s] 未找到可用 Wiki 空间，跳过发布", group_name)
                return {
                    "group_name": group_name,
                    "msg_count": len(messages),
                    "result_str": "未找到可用 Wiki 空间，摘要未发布",
                    "page_url": "",
                }

        result_str, page_url = self._publish_doc(
            group_name=group_name,
            date_str=date_str,
            markdown=summary_md,
            root_node_token=root_token,
        )
        logger.info("群 [%s] 摘要发布结果: %s", group_name, result_str)

        return {
            "group_name": group_name,
            "msg_count": len(messages),
            "result_str": result_str,
            "page_url": page_url,
            "space_name": space_name,
        }

    # ------------------------------------------------------------------ #
    # 内部方法
    # ------------------------------------------------------------------ #

    def _summarize(self, group_name: str, date_str: str, messages: list[dict]) -> str:
        """调用 Claude 生成群消息摘要，返回 Markdown。"""
        lines = [f"{m.get('sender_name', '未知')}: {m.get('text', '')}" for m in messages]
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

    def _find_space_by_name(self, name: str) -> tuple[str, str]:
        """按名称模糊搜索 Wiki 空间。"""
        if not self._spaces_cache:
            self._spaces_cache = self._feishu.list_wiki_spaces()
        name_lower = name.lower()
        for space in self._spaces_cache:
            space_name = space.get("name", "")
            if name_lower in space_name.lower() or space_name.lower() in name_lower:
                return space["space_id"], space_name
        return "", ""

    def _publish_doc(
        self,
        group_name: str,
        date_str: str,
        markdown: str,
        root_node_token: str,
    ) -> tuple[str, str]:
        """通过 feishu-sync-cli create_page 将摘要发布为 wiki 页面。返回 (result_str, page_url)。"""
        title = f"{group_name} {date_str} 摘要"
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

    def _send_summary_dm(self, date_str: str, results: list[dict]) -> None:
        """向 owner 发送每日摘要汇总 DM，包含各群统计和链接。"""
        if not self._owner_open_id:
            logger.warning("未配置 owner_open_id，无法发送每日摘要 DM")
            return

        lines = [f"**每日摘要 {date_str}**\n"]
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
            logger.error("发送每日摘要 DM 失败: %s", e)
