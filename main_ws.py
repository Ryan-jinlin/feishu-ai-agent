"""飞书个人助理 — 长连接（WebSocket）模式，无需公网 URL"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading

import lark_oapi as lark
from apscheduler.schedulers.background import BackgroundScheduler
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from dotenv import load_dotenv

# 必须在内部模块 import 前加载 .env，否则 tools.py 等模块级常量（如 PPT_OUTPUT_DIR）
# 会在 os.environ 尚未注入时就已固化为默认值
load_dotenv()

from agent.assistant import PersonalAssistant
from agent.daily_summary import DailySummaryJob
from agent.tools import ToolExecutor, _PENDING_EVENTS, _save_pending_events
from feishu.bot import BotMessage, MentionedUser
from feishu.client import FeishuClient
from feishu.message_cache import MessageCache

# ------------------------------------------------------------------ #
# 日志
# ------------------------------------------------------------------ #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# 初始化
# ------------------------------------------------------------------ #

app_id       = os.environ["FEISHU_APP_ID"]
app_secret   = os.environ["FEISHU_APP_SECRET"]
anthropic_key = os.environ["ANTHROPIC_API_KEY"]
owner_name   = os.environ.get("BOT_OWNER_NAME", "用户")
owner_open_id = os.environ.get("BOT_OWNER_OPEN_ID", "")

feishu        = FeishuClient(app_id, app_secret)
message_cache = MessageCache()
executor  = ToolExecutor(feishu=feishu, message_cache=message_cache, owner_open_id=owner_open_id)

# 从持久化文件加载 P2P chat_id 映射（跨重启保留）
executor._p2p_chat_ids.update(MessageCache.load_p2p_chat_ids())

# 群名称缓存，避免每条消息都调 API
_chat_name_cache: dict[str, str] = {}
assistant = PersonalAssistant(
    anthropic_api_key=anthropic_key,
    tool_executor=executor,
    owner_name=owner_name,
    owner_open_id=owner_open_id,
)

daily_summary = DailySummaryJob(
    feishu_client=feishu,
    anthropic_api_key=anthropic_key,
    owner_open_id=owner_open_id,
)

# Bot 自身 open_id（群聊中用于判断是否被 @）
_BOT_OPEN_ID: str = feishu.get_bot_open_id()

# 消息去重
_processed_ids: set[str] = set()
_MAX_DEDUP = 1000

# ------------------------------------------------------------------ #
# 事件处理
# ------------------------------------------------------------------ #

def _extract_post_body(lang_body: dict) -> str:
    """从单个语言体 {"title":..., "content":[[...],...]} 提取纯文本"""
    parts: list[str] = []
    title = lang_body.get("title", "")
    if title:
        parts.append(title)
    for paragraph in lang_body.get("content", []):
        for elem in paragraph:
            tag = elem.get("tag", "")
            if tag == "text":
                parts.append(elem.get("text", ""))
            elif tag == "a":
                href = elem.get("href", "")
                text = elem.get("text", "")
                # 把链接文本和 URL 都保留，让 Claude 能看到 URL
                if href and text and href != text:
                    parts.append(f"{text}（{href}）")
                elif href:
                    parts.append(href)
                else:
                    parts.append(text)
            elif tag == "at":
                parts.append(elem.get("user_name", ""))
    return "".join(parts)


def _extract_post_text(content: dict) -> str:
    """从飞书 post 消息 JSON 中提取纯文本（含超链接 URL，便于 Claude 拿到飞书文档链接）。
    支持两种格式：
    - 语言包裹格式：{"zh_cn": {"title": "", "content": [...]}}
    - 直接格式：{"title": "", "content": [...]}（部分客户端发出）
    """
    # 优先尝试语言包裹格式
    for lang in ("zh_cn", "en_us", "ja_jp"):
        lang_body = content.get(lang, {})
        if not lang_body:
            continue
        result = _extract_post_body(lang_body)
        if result:
            return result
    # 回退：直接格式（content key 在顶层）
    if "content" in content:
        return _extract_post_body(content)
    return ""


def _parse_message(data: P2ImMessageReceiveV1) -> BotMessage | None:
    """将 lark_oapi 事件转换为 BotMessage"""
    try:
        event   = data.event
        message = event.message
        sender  = event.sender

        if message.message_type not in ("text", "post"):
            return None

        content_str = message.content or "{}"
        try:
            content = json.loads(content_str)
        except json.JSONDecodeError:
            content = {}

        if message.message_type == "post":
            raw_text = _extract_post_text(content)
        else:
            raw_text = content.get("text", "")

        if not raw_text.strip():
            logger.warning("消息文本解析为空，type=%s content=%s", message.message_type, content_str[:500])

        # 解析 @mentions
        mentions: list[MentionedUser] = []
        for m in (message.mentions or []):
            oid = m.id.open_id if (m.id and m.id.open_id) else ""
            if oid:
                mentions.append(MentionedUser(
                    key=m.key or "",
                    open_id=oid,
                    name=m.name or "",
                ))

        # 群聊中只响应 Bot 自身被 @mention 的消息
        chat_type = message.chat_type or "p2p"
        if chat_type == "group":
            if _BOT_OPEN_ID:
                # 精确匹配：Bot 的 open_id 出现在 mention 列表中
                bot_mentioned = any(m.open_id == _BOT_OPEN_ID for m in mentions)
            else:
                # fallback：获取 Bot ID 失败时，有任何 @mention 才响应（保守策略）
                bot_mentioned = bool(mentions)
            if not bot_mentioned:
                logger.debug("群聊消息未 @Bot，忽略: %s", (raw_text or "")[:40])
                return None

        clean_text = raw_text
        for m in mentions:
            clean_text = clean_text.replace(m.key, m.name).strip()

        sender_id = sender.sender_id
        sender_open_id = sender_id.open_id if (sender_id and sender_id.open_id) else ""

        return BotMessage(
            message_id=message.message_id or "",
            sender_open_id=sender_open_id,
            chat_id=message.chat_id or "",
            chat_type=message.chat_type or "p2p",
            raw_text=raw_text,
            clean_text=clean_text,
            mentions=mentions,
        )
    except Exception as e:
        logger.exception("解析消息事件失败: %s", e)
        return None


def _cache_message(data: P2ImMessageReceiveV1) -> None:
    """在 @mention 过滤之前，把所有文本消息写入本地缓存。"""
    try:
        import time as _time
        event   = data.event
        message = event.message
        sender  = event.sender

        if message.message_type not in ("text", "post"):
            return

        content_str = message.content or "{}"
        try:
            content = json.loads(content_str)
        except json.JSONDecodeError:
            content = {}

        if message.message_type == "post":
            text = _extract_post_text(content)
        else:
            text = content.get("text", "")
        if not text.strip():
            return

        chat_id   = message.chat_id or ""
        chat_type = message.chat_type or "p2p"
        msg_id    = message.message_id or ""
        ts        = int(message.create_time) if message.create_time else int(_time.time())

        sender_id_obj = sender.sender_id
        sender_id = sender_id_obj.open_id if (sender_id_obj and sender_id_obj.open_id) else ""
        sender_name = feishu._resolve_sender_name(sender_id) if sender_id else "未知"

        # 获取群名（优先用缓存，否则调 API）
        chat_name = _chat_name_cache.get(chat_id, "")
        if not chat_name and chat_id:
            try:
                import requests as _req
                resp = _req.get(
                    f"https://open.feishu.cn/open-apis/im/v1/chats/{chat_id}",
                    headers=feishu._headers(),
                    params={"user_id_type": "open_id"},
                    timeout=5,
                )
                d = resp.json()
                chat_name = d.get("data", {}).get("name", "") or ""
                if chat_name:
                    _chat_name_cache[chat_id] = chat_name
            except Exception:
                pass
        if not chat_name:
            chat_name = sender_name if chat_type == "p2p" else chat_id

        message_cache.store(msg_id, chat_id, chat_name, chat_type, sender_id, sender_name, text, ts)
    except Exception as exc:
        logger.debug("_cache_message 失败（非致命）: %s", exc)


def do_p2_im_message_receive_v1(data: P2ImMessageReceiveV1) -> None:
    _cache_message(data)          # 先缓存，不受 @mention 过滤影响
    msg = _parse_message(data)
    if msg is None:
        return

    # 去重
    global _processed_ids
    if msg.message_id in _processed_ids:
        logger.debug("重复消息，忽略: %s", msg.message_id)
        return
    _processed_ids.add(msg.message_id)
    if len(_processed_ids) > _MAX_DEDUP:
        items = list(_processed_ids)
        _processed_ids.clear()
        _processed_ids.update(items[-(_MAX_DEDUP // 2):])

    logger.info("处理消息 [%s] from %s: %s",
                msg.message_id, msg.sender_open_id, msg.clean_text[:80])

    try:
        # ------------------------------------------------------------------ #
        # 非 owner 的 P2P 消息：直接转发给 owner，不进入 Claude Agent
        # 场景：Bot 代 owner 发消息后，收件人回复 Bot，此时应转告 owner
        # ------------------------------------------------------------------ #
        if (msg.chat_type == "p2p"
                and owner_open_id
                and msg.sender_open_id != owner_open_id):
            # 记录 open_id → chat_id 映射，供 owner 日后查询 P2P 历史（持久化跨重启）
            if msg.chat_id:
                executor._p2p_chat_ids[msg.sender_open_id] = msg.chat_id
                MessageCache.save_p2p_chat_ids(executor._p2p_chat_ids)
            sender_info = feishu.get_user_by_open_id(msg.sender_open_id)
            sender_name = sender_info.get("name", "对方") if sender_info else "对方"
            # 1. 始终通知 owner
            forward_text = f"📨 {sender_name} 给你发消息了：\n\n{msg.clean_text}"
            feishu.send_text_to_user(owner_open_id, forward_text)
            logger.info("非 owner 消息已转发给 owner（from %s: %s）",
                        sender_name, msg.clean_text[:60])
            # 2. 用 Claude 智能回复对方（无工具，精简 prompt）
            feishu.reply_message(msg.message_id, "正在处理，请稍候...")

            def _reply_non_owner(_msg=msg, _name=sender_name) -> None:
                try:
                    reply_text = assistant.process_non_owner(_msg, _name)
                    feishu.reply_card(_msg.message_id, reply_text)
                except Exception as exc:
                    logger.warning("非 owner 回复失败: %s", exc)
                    feishu.reply_message(_msg.message_id, f"收到，已转告给{owner_name}。")

            threading.Thread(target=_reply_non_owner, daemon=True).start()
            return

        # 内置指令
        if msg.clean_text.strip() in ("我的openid", "whoami", "/whoami", "我的open_id"):
            feishu.reply_message(msg.message_id, f"你的 open_id 是：\n{msg.sender_open_id}")
            return
        if msg.clean_text.strip() in ("/clear", "清除记忆", "清除上下文", "重新开始"):
            assistant.clear_history(msg.sender_open_id)
            feishu.reply_message(msg.message_id, "已清除对话历史，下一条消息将开始全新对话。")
            return

        # 每日摘要确认指令（仅限 owner）
        _confirm_triggers = ("确认群摘要", "群摘要确认")
        if any(msg.clean_text.strip().startswith(t) for t in _confirm_triggers):
            if owner_open_id and msg.sender_open_id != owner_open_id:
                feishu.reply_message(msg.message_id, "该指令仅限 Bot 管理员使用。")
                return
            reply = daily_summary.handle_confirm(msg.clean_text.strip())
            feishu.reply_message(msg.message_id, reply or "未识别的确认指令，请使用：确认群摘要 [群名]")
            return

        # 空消息忽略
        if not msg.clean_text.strip():
            return

        # 立即回复确认，防止 WS 事件循环被长耗时任务阻塞导致 ping_timeout
        feishu.reply_message(msg.message_id, "正在处理，请稍候...")

        def _process_in_background() -> None:
            try:
                reply_text = assistant.process(msg)
                logger.info("回复: %s", reply_text[:200])
                feishu.reply_card(msg.message_id, reply_text)
            except Exception as exc:
                logger.exception("处理消息时出错: %s", exc)
                try:
                    feishu.reply_message(msg.message_id, f"抱歉，处理时出现错误：{exc}")
                except Exception:
                    pass

        threading.Thread(target=_process_in_background, daemon=True).start()

    except Exception as e:
        logger.exception("处理消息时出错: %s", e)
        try:
            feishu.reply_message(msg.message_id, f"抱歉，处理时出现错误：{e}")
        except Exception:
            pass


# ------------------------------------------------------------------ #
# RSVP 轮询（每 5 分钟检查日历事件与会者回复状态）
# ------------------------------------------------------------------ #

def _check_rsvp_changes() -> None:
    """检查所有待跟踪日历事件的 RSVP 状态，有人拒绝时立即通知 owner。"""
    import time as _time
    now = _time.time()
    expired = [eid for eid, info in _PENDING_EVENTS.items()
               if now - info.get("created_at", now) > 7 * 24 * 3600]
    for eid in expired:
        _PENDING_EVENTS.pop(eid, None)
    if expired:
        _save_pending_events()

    for event_id, info in list(_PENDING_EVENTS.items()):
        try:
            attendees = feishu.get_event_attendees(info["calendar_id"], event_id)
            for att in attendees:
                oid = att.get("user_id", "")
                rsvp = att.get("rsvp_status", "needs_action")
                if not oid or oid not in info["attendees"]:
                    continue
                old_rsvp = info["attendees"][oid]
                if old_rsvp == rsvp:
                    continue
                info["attendees"][oid] = rsvp
                _save_pending_events()
                logger.info("RSVP 变化: event=%s user=%s %s→%s", event_id, oid, old_rsvp, rsvp)
                if rsvp == "decline" and info.get("owner_open_id"):
                    # 查找拒绝者姓名
                    user_info = feishu.get_user_by_open_id(oid)
                    name = user_info.get("name", oid) if user_info else oid
                    feishu.send_text_to_user(
                        info["owner_open_id"],
                        f"【会议提醒】{name} 拒绝了会议邀请\n"
                        f"会议：{info['title']}\n"
                        f"如需重新安排，请告诉我新的时间。",
                    )
        except Exception as e:
            logger.warning("RSVP 轮询失败 (event=%s): %s", event_id, e)


# ------------------------------------------------------------------ #
# feishu-sync token 预热（避免 access token 过期时首次请求失败）
# ------------------------------------------------------------------ #

def _warmup_feishu_token() -> None:
    """调用一次轻量 feishu-sync-cli 操作，触发 access token 自动刷新。
    access token 有效期约 2 小时，每 90 分钟主动预热一次即可保持不过期。"""
    try:
        result = executor._run_feishu_cli("list_spaces", timeout=20)
        if result and not result.startswith("[错误]") and not result.startswith("[飞书工具"):
            logger.info("feishu-sync token 预热成功")
        else:
            logger.warning("feishu-sync token 预热异常: %s", (result or "")[:100])
    except Exception as e:
        logger.warning("feishu-sync token 预热失败（非致命）: %s", e)


# ------------------------------------------------------------------ #
# 启动
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    logger.info("个人助理机器人启动（长连接模式）")

    # RSVP 轮询定时任务（每 5 分钟）+ 每日摘要（每天 00:00:05）+ token 预热（每 90 分钟）
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(_check_rsvp_changes, "interval", minutes=5, id="rsvp_check")
    scheduler.add_job(daily_summary.run, "cron", hour=0, minute=0, second=5, id="daily_summary")
    scheduler.add_job(_warmup_feishu_token, "interval", minutes=90, id="token_warmup")
    scheduler.start()
    logger.info("RSVP 轮询任务已启动（每 5 分钟）")
    logger.info("每日摘要任务已启动（每天 00:00:05）")
    logger.info("feishu-sync token 预热任务已启动（每 90 分钟）")

    # 启动时立即预热一次（后台执行，不阻塞 WS 启动）
    threading.Thread(target=_warmup_feishu_token, daemon=True, name="token-warmup-init").start()

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1)
        .build()
    )

    ws_client = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )

    ws_client.start()
