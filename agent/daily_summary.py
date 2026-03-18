"""每日群聊摘要功能：读取昨日群消息 → Claude 摘要 → 发布飞书 Wiki"""
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
_PENDING_FILE  = os.path.join(_BASE_DIR, ".pending_summaries.json")

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
    # 1. 环境变量指定
    env_path = os.environ.get("FEISHU_SYNC_CLI", "")
    if env_path and os.path.isfile(env_path):
        return env_path
    # 2. PATH 中查找
    found = shutil.which("feishu-sync-cli")
    if found:
        return found
    # 3. 常见 user-install 路径
    candidates = [
        os.path.expanduser("~/Library/Python/3.9/bin/feishu-sync-cli"),
        os.path.expanduser("~/Library/Python/3.10/bin/feishu-sync-cli"),
        os.path.expanduser("~/Library/Python/3.11/bin/feishu-sync-cli"),
        os.path.expanduser("~/.local/bin/feishu-sync-cli"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return "feishu-sync-cli"  # fallback，可能会失败


# ------------------------------------------------------------------ #
# DailySummaryJob
# ------------------------------------------------------------------ #

class DailySummaryJob:
    """每天 00:00 读取 Bot 所在群的昨日消息，生成摘要，发布到飞书 Wiki。"""

    def __init__(
        self,
        feishu_client,          # FeishuClient 实例
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
        """每天 00:00:05 运行：读昨日群消息 → 摘要 → 询问/发布 wiki。"""
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
        pending:  dict = _load_json(_PENDING_FILE)

        groups = self._feishu.get_joined_groups()
        logger.info("Bot 所在群: %d 个", len(groups))

        for group in groups:
            chat_id    = group["chat_id"]
            group_name = group.get("name") or chat_id
            try:
                self._process_group(
                    chat_id=chat_id,
                    group_name=group_name,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    date_str=date_str,
                    mappings=mappings,
                    pending=pending,
                )
            except Exception as e:
                logger.error("处理群 [%s] 摘要失败: %s", group_name, e)

        _save_json(_MAPPINGS_FILE, mappings)
        _save_json(_PENDING_FILE, pending)
        logger.info("每日摘要任务完成: %s", date_str)

    def _process_group(
        self,
        chat_id: str,
        group_name: str,
        start_ts: int,
        end_ts: int,
        date_str: str,
        mappings: dict,
        pending: dict,
    ) -> None:
        messages = self._feishu.get_group_messages(chat_id, start_ts, end_ts)
        if not messages:
            logger.info("群 [%s] 昨日无消息，跳过", group_name)
            return

        logger.info("群 [%s] 昨日 %d 条消息，开始摘要", group_name, len(messages))
        summary_md = self._summarize(group_name, date_str, messages)

        if chat_id in mappings:
            # 已有确认映射，直接发布
            info = mappings[chat_id]
            result = self._publish_doc(
                group_name=group_name,
                date_str=date_str,
                markdown=summary_md,
                root_node_token=info["root_node_token"],
            )
            logger.info("群 [%s] 摘要已发布: %s", group_name, result)
        else:
            # 首次：建议空间，存入 pending，DM owner 确认
            suggested_id, suggested_name = self._suggest_space(group_name)
            pending[chat_id] = {
                "group_name": group_name,
                "date": date_str,
                "markdown": summary_md,
                "suggested_space_id": suggested_id or "",
                "suggested_space_name": suggested_name or "",
            }
            self._send_confirm_request(
                group_name=group_name,
                date_str=date_str,
                suggested_space=suggested_name or "（未找到合适空间，请手动指定）",
                msg_count=len(messages),
            )

    # ------------------------------------------------------------------ #
    # 用户确认处理（main_ws.py 调用）
    # ------------------------------------------------------------------ #

    def handle_confirm(self, text: str) -> str:
        """
        处理用户发来的确认消息。
        格式：「确认群摘要 [群名] [空间名]」或「确认群摘要 [群名]」（使用建议位置）
        返回回复文本。
        """
        # 去除触发前缀
        stripped = text.strip()
        for prefix in ("确认群摘要", "群摘要确认"):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):].strip()
                break
        else:
            return ""  # 不是确认命令

        # 解析 "群名 [空间名]"（允许群名含空格，空间名是最后一段）
        parts = stripped.split()
        if not parts:
            return "格式错误，请使用：确认群摘要 [群名] [空间名]"

        # 策略：逐步尝试把 parts 前 N 段拼成群名，剩余部分为空间名
        pending  = _load_json(_PENDING_FILE)
        mappings = _load_json(_MAPPINGS_FILE)

        matched_chat_id: str | None = None
        matched_info:    dict | None = None
        space_name_query: str = ""

        for split_at in range(len(parts), 0, -1):
            candidate_group = " ".join(parts[:split_at])
            candidate_space = " ".join(parts[split_at:])
            for cid, info in pending.items():
                gname = info.get("group_name", "")
                if candidate_group == gname or candidate_group in gname:
                    matched_chat_id  = cid
                    matched_info     = info
                    space_name_query = candidate_space
                    break
            if matched_chat_id:
                break

        if not matched_chat_id:
            return (
                f"未找到待确认的群「{stripped}」，可能已确认或没有待发布的摘要。\n"
                "可用：确认群摘要 [群名]  或  确认群摘要 [群名] [空间名]"
            )

        # 确定目标空间
        if space_name_query:
            space_id, space_name = self._find_space_by_name(space_name_query)
            if not space_id:
                return f"未找到名为「{space_name_query}」的 Wiki 空间，请检查空间名称。"
        else:
            space_id   = matched_info.get("suggested_space_id", "")  # type: ignore[union-attr]
            space_name = matched_info.get("suggested_space_name", "")  # type: ignore[union-attr]
            if not space_id:
                return "未找到建议空间，请手动指定：确认群摘要 [群名] [空间名]"

        # 获取根节点 token
        root_token = self._feishu.get_space_root_node_token(space_id)
        if not root_token:
            return f"无法获取空间「{space_name}」的根节点，请检查 Bot 是否有该空间权限。"

        # 保存映射（以后每天自动发布到此空间）
        mappings[matched_chat_id] = {
            "space_id": space_id,
            "space_name": space_name,
            "root_node_token": root_token,
        }
        _save_json(_MAPPINGS_FILE, mappings)

        # 发布摘要
        result = self._publish_doc(
            group_name=matched_info["group_name"],  # type: ignore[index]
            date_str=matched_info["date"],          # type: ignore[index]
            markdown=matched_info["markdown"],      # type: ignore[index]
            root_node_token=root_token,
        )

        # 清除 pending
        pending.pop(matched_chat_id, None)
        _save_json(_PENDING_FILE, pending)

        return (
            f"{result}\n\n"
            f"以后「{matched_info['group_name']}」的每日摘要将自动发布到「{space_name}」。"  # type: ignore[index]
        )

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
        """根据群名推断建议的 Wiki 空间。返回 (space_id, space_name)。"""
        if not self._spaces_cache:
            return "", ""

        # 从群名提取地名关键词（XX山/XX河/XX湖），匹配同名 wiki 空间
        m = _PLACE_PATTERN.search(group_name)
        if m:
            keyword = m.group(1)
            for space in self._spaces_cache:
                if keyword in space.get("name", ""):
                    return space["space_id"], space["name"]

        # 没有地名匹配，查找个人空间（space_type == "personal"）
        for space in self._spaces_cache:
            if space.get("space_type") == "personal":
                return space["space_id"], space["name"]

        # 兜底：返回第一个空间
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
    ) -> str:
        """通过 feishu-sync-cli create_page 将摘要发布为 wiki 页面。"""
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
                # 尝试从输出提取页面 URL
                url_match = re.search(r"https://\S+wiki/\w+", result.stdout)
                page_url = url_match.group(0) if url_match else ""
                if page_url:
                    return f"已发布：[{title}]({page_url})"
                return f"已发布：{title}"
            else:
                err = (result.stderr or result.stdout or "未知错误").strip()
                logger.error("create_page 失败: %s", err)
                return f"发布失败：{err[:300]}"
        except subprocess.TimeoutExpired:
            return "发布超时，请稍后重试"
        except Exception as e:
            logger.error("_publish_doc 异常: %s", e)
            return f"发布异常：{e}"

    def _send_confirm_request(
        self,
        group_name: str,
        date_str: str,
        suggested_space: str,
        msg_count: int,
    ) -> None:
        """向 owner 发 DM，请求确认摘要存放位置。"""
        if not self._owner_open_id:
            logger.warning("未配置 owner_open_id，无法发送确认 DM")
            return
        text = (
            f"【每日摘要】「{group_name}」{date_str} 共 {msg_count} 条消息。\n"
            f"建议将摘要存放到：「{suggested_space}」\n\n"
            f"确认使用该位置，请回复：\n"
            f"确认群摘要 {group_name}\n\n"
            f"指定其他位置，请回复：\n"
            f"确认群摘要 {group_name} [空间名]"
        )
        try:
            self._feishu.send_text_to_user(self._owner_open_id, text)
        except Exception as e:
            logger.error("发送确认 DM 失败: %s", e)
