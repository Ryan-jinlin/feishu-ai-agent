"""飞书个人助理 — 长连接（WebSocket）模式，无需公网 URL"""
from __future__ import annotations

import atexit
import json
import logging
import os
import signal
import sys
import threading

import lark_oapi as lark
from apscheduler.schedulers.background import BackgroundScheduler
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from dotenv import load_dotenv

# 必须在内部模块 import 前加载 .env，否则 tools.py 等模块级常量（如 PPT_OUTPUT_DIR）
# 会在 os.environ 尚未注入时就已固化为默认值
load_dotenv()

# ── 单实例守卫：启动时自动终止旧进程，防止多实例并发导致 token 文件竞争 ──────
_PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bot.pid")

def _ensure_single_instance() -> None:
    """若 PID 文件已存在且对应进程仍在运行，先将其终止，再写入当前 PID。"""
    if os.path.exists(_PID_FILE):
        try:
            with open(_PID_FILE) as f:
                old_pid = int(f.read().strip())
            if old_pid != os.getpid():
                os.kill(old_pid, signal.SIGTERM)
                import time as _t
                _t.sleep(1)
                # 若 SIGTERM 后仍存活则强制终止
                try:
                    os.kill(old_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                print(f"[单实例] 已终止旧进程 PID={old_pid}", flush=True)
        except (ValueError, ProcessLookupError, PermissionError):
            pass
        except Exception as e:
            print(f"[单实例] 终止旧进程时出错（忽略）: {e}", flush=True)
    # 写入当前 PID
    with open(_PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(_PID_FILE) and os.remove(_PID_FILE))

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


def _fmt_ts(ts: int) -> str:
    """将 Unix 时间戳（秒）格式化为 MM-DD HH:MM（上海时区）"""
    try:
        import pytz as _pytz
        from datetime import datetime as _dt
        tz = _pytz.timezone("Asia/Shanghai")
        return _dt.fromtimestamp(ts, tz).strftime("%m-%d %H:%M")
    except Exception:
        return str(ts)


def _detect_media_type(b: bytes) -> str:
    """从字节头部判断图片 MIME 类型"""
    if b[:8] == b'\x89PNG\r\n\x1a\n':
        return "image/png"
    if b[:2] == b'\xff\xd8':
        return "image/jpeg"
    if b[:4] == b'GIF8':
        return "image/gif"
    if b[:4] == b'RIFF' and b[8:12] == b'WEBP':
        return "image/webp"
    return "image/jpeg"


def _parse_message(data: P2ImMessageReceiveV1) -> BotMessage | None:
    """将 lark_oapi 事件转换为 BotMessage"""
    try:
        event   = data.event
        message = event.message
        sender  = event.sender
        sender_id = sender.sender_id
        sender_open_id = sender_id.open_id if (sender_id and sender_id.open_id) else ""

        # ── 合并转发消息（仅处理 P2P，群内无法同时 @Bot）────────────────────
        if message.message_type == "merge_forward":
            if (message.chat_type or "p2p") == "group":
                logger.debug("群聊中收到 merge_forward，暂不处理（需要在 P2P 私信中转发给 Bot）")
                return None   # 群内转发暂不处理（无法附带 @mention）
            try:
                fwd_content = json.loads(message.content or "{}")
            except json.JSONDecodeError:
                fwd_content = {}
            forward_msg_id = fwd_content.get("create_message_id", "")
            # SDK 某些版本 content 返回纯文本而非 JSON，此时用 message_id 本身作为容器 ID
            if not forward_msg_id:
                forward_msg_id = message.message_id or ""
                logger.info(
                    "merge_forward content 非 JSON（content=%s），改用 message_id=%s 作为 container_id",
                    (message.content or "")[:100], forward_msg_id,
                )
            if not forward_msg_id:
                logger.warning("merge_forward 消息既无 create_message_id 也无 message_id，忽略")
                return None
            return BotMessage(
                message_id=message.message_id or "",
                sender_open_id=sender_open_id,
                chat_id=message.chat_id or "",
                chat_type=message.chat_type or "p2p",
                raw_text="",
                clean_text="[转发消息]",
                forward_msg_id=forward_msg_id,
            )

        # ── 图片消息 ──────────────────────────────────────────────────────
        if message.message_type == "image":
            try:
                img_content = json.loads(message.content or "{}")
            except json.JSONDecodeError:
                img_content = {}
            image_key = img_content.get("image_key", "")
            return BotMessage(
                message_id=message.message_id or "",
                sender_open_id=sender_open_id,
                chat_id=message.chat_id or "",
                chat_type=message.chat_type or "p2p",
                raw_text="",
                clean_text="",
                image_keys=[image_key] if image_key else [],
            )

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

        # 空消息忽略（纯图片消息 clean_text 为空但 image_keys 不为空，不忽略）
        if not msg.clean_text.strip() and not msg.image_keys:
            return

        # 立即回复确认，防止 WS 事件循环被长耗时任务阻塞导致 ping_timeout
        feishu.reply_message(msg.message_id, "正在处理，请稍候...")

        def _process_in_background() -> None:
            try:
                import base64 as _b64

                # ── 下载图片，填充 image_data ──────────────────────────────────
                if msg.image_keys:
                    for img_key in msg.image_keys:
                        img_bytes = feishu.download_message_resource(msg.message_id, img_key)
                        if img_bytes:
                            msg.image_data.append({
                                "data": _b64.b64encode(img_bytes).decode(),
                                "media_type": _detect_media_type(img_bytes),
                            })
                    if not msg.image_data:
                        feishu.reply_card(msg.message_id, "图片下载失败，请稍后重试。")
                        return

                # ── 合并转发：先获取转发内容，注入 clean_text 让 Claude 做摘要 ──
                if msg.forward_msg_id:
                    try:
                        fwd_msgs = feishu.get_merge_forward_messages(msg.forward_msg_id)
                    except RuntimeError as api_err:
                        feishu.reply_card(
                            msg.message_id,
                            f"无法读取转发的消息内容（{api_err}）。\n\n"
                            "可能原因：Bot 应用未开启 im:message:readonly 权限，或该转发消息不在 Bot 的访问范围内。",
                        )
                        return
                    if not fwd_msgs:
                        feishu.reply_card(
                            msg.message_id,
                            "转发的消息中没有文本内容（可能全部为图片或表情包）。",
                        )
                        return
                    lines = [
                        f"[{_fmt_ts(m['ts'])}] {m['sender_name']}: {m['text']}"
                        for m in fwd_msgs
                    ]
                    msg.clean_text = (
                        f"用户转发了以下 {len(fwd_msgs)} 条聊天记录，"
                        "请做摘要（提炼关键讨论议题、决策和行动项）：\n\n"
                        + "\n".join(lines)
                    )
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
                owner_oid = info.get("owner_open_id", "")
                if rsvp == "decline" and owner_oid and oid != owner_oid:
                    # 查找拒绝者姓名（owner 拒绝自己的会议时不通知）
                    user_info = feishu.get_user_by_open_id(oid)
                    name = user_info.get("name", oid) if user_info else oid
                    feishu.send_text_to_user(
                        owner_oid,
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
    """直接调用 feishu_sync.retoken.get_access_token() 强制刷新 access token。
    不走 feishu-sync-cli 命令（该命令在 list_spaces 有缓存时会跳过 token exchange）。
    access token 有效期 2 小时，每 90 分钟预热一次确保不过期。"""
    import subprocess as _sp, sys as _sys
    try:
        r = _sp.run(
            [_sys.executable, "-c",
             "from feishu_sync.retoken import get_access_token; "
             "_, ttl = get_access_token(); print(ttl)"],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode == 0:
            ttl = r.stdout.strip()
            logger.info("feishu-sync token 预热成功，access token TTL: %ss", ttl)
        else:
            logger.warning("feishu-sync token 预热异常: %s", (r.stderr or r.stdout)[:200])
    except Exception as e:
        logger.warning("feishu-sync token 预热失败（非致命）: %s", e)


# ------------------------------------------------------------------ #
# 启动
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    _ensure_single_instance()   # 终止旧进程，保证单实例运行
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
