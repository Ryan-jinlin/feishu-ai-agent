"""飞书个人助理机器人 — FastAPI 主入口"""
from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager

import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request, Response

from agent.assistant import PersonalAssistant
from agent.tools import ToolExecutor
from feishu.bot import FeishuBotEventParser
from feishu.client import FeishuClient

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
# 全局单例
# ------------------------------------------------------------------ #

load_dotenv()

_feishu: FeishuClient | None = None
_parser: FeishuBotEventParser | None = None
_assistant: PersonalAssistant | None = None

# 消息去重（防止飞书重试时重复处理）
_processed_message_ids: set[str] = set()
_MAX_DEDUP_SIZE = 1000


def _init_services():
    global _feishu, _parser, _assistant

    app_id = os.environ["FEISHU_APP_ID"]
    app_secret = os.environ["FEISHU_APP_SECRET"]
    verify_token = os.environ.get("FEISHU_VERIFY_TOKEN", "")
    encrypt_key = os.environ.get("FEISHU_ENCRYPT_KEY", "")
    anthropic_key = os.environ["ANTHROPIC_API_KEY"]
    owner_name = os.environ.get("BOT_OWNER_NAME", "用户")
    owner_open_id = os.environ.get("BOT_OWNER_OPEN_ID", "")

    _feishu = FeishuClient(app_id, app_secret)
    _parser = FeishuBotEventParser(verify_token=verify_token, encrypt_key=encrypt_key)
    executor = ToolExecutor(feishu=_feishu)

    # 把 owner_open_id 挂载到 executor，供工具使用
    executor.owner_open_id = owner_open_id  # type: ignore[attr-defined]

    _assistant = PersonalAssistant(
        anthropic_api_key=anthropic_key,
        tool_executor=executor,
        owner_name=owner_name,
        owner_open_id=owner_open_id,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_services()
    logger.info("个人助理机器人已启动")
    yield
    logger.info("机器人已关闭")


app = FastAPI(title="Personal Assistant Bot", lifespan=lifespan)


# ------------------------------------------------------------------ #
# Webhook 端点
# ------------------------------------------------------------------ #

@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    """
    接收飞书事件推送。
    立即返回 {"code": 0}（飞书要求 3 秒内响应），在后台处理消息。
    """
    body = await request.json()

    # 0. 解密（如果启用了 Encrypt Key）
    body = _parser.decrypt_body(body)

    # 1. URL 验证 challenge
    challenge_resp = _parser.handle_challenge(body)
    if challenge_resp is not None:
        logger.info("处理 challenge 验证")
        return challenge_resp

    # 2. 解析消息事件
    msg = _parser.parse_message_event(body)
    if msg is None:
        return {"code": 0}

    # 3. 去重
    if msg.message_id in _processed_message_ids:
        logger.debug("重复消息，已忽略: %s", msg.message_id)
        return {"code": 0}
    _processed_message_ids.add(msg.message_id)
    if len(_processed_message_ids) > _MAX_DEDUP_SIZE:
        # 简单清理：移除最旧的条目（set 不保序，只清空超量的一半）
        items = list(_processed_message_ids)
        _processed_message_ids.clear()
        _processed_message_ids.update(items[-(_MAX_DEDUP_SIZE // 2):])

    # 4. 后台处理并回复
    background_tasks.add_task(_process_and_reply, msg)
    return {"code": 0}


async def _process_and_reply(msg):
    """在后台调用 Claude 处理消息并通过飞书发送回复"""
    try:
        logger.info("处理消息 [%s] from %s: %s", msg.message_id, msg.sender_open_id, msg.clean_text[:80])

        # 内置指令：查询自己的 open_id（不经过 Claude）
        if msg.clean_text.strip() in ("我的openid", "whoami", "/whoami", "我的open_id"):
            _feishu.reply_message(msg.message_id, f"你的 open_id 是：\n{msg.sender_open_id}")
            return

        # 空消息忽略（群聊 @bot 无正文时 clean_text 可能为空）
        if not msg.clean_text.strip():
            logger.debug("空消息，忽略")
            return

        reply_text = _assistant.process(msg)
        logger.info("回复: %s", reply_text[:200])
        _feishu.reply_message(msg.message_id, reply_text)
    except Exception as e:
        logger.exception("处理消息时出错: %s", e)
        try:
            _feishu.reply_message(msg.message_id, f"抱歉，处理时出现错误：{e}")
        except Exception:
            pass


# ------------------------------------------------------------------ #
# 健康检查
# ------------------------------------------------------------------ #

@app.get("/health")
async def health():
    return {"status": "ok"}


# ------------------------------------------------------------------ #
# 启动入口
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
