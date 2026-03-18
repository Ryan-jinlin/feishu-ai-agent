"""飞书机器人 Webhook 事件解析与分发"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import base64
from dataclasses import dataclass, field

try:
    from Crypto.Cipher import AES
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False

logger = logging.getLogger(__name__)


@dataclass
class MentionedUser:
    key: str          # 消息中的占位符，如 @_user_1
    open_id: str
    name: str


@dataclass
class BotMessage:
    """解析后的机器人消息"""
    message_id: str
    sender_open_id: str
    chat_id: str
    chat_type: str              # "p2p" | "group"
    raw_text: str               # 含占位符的原始文本
    clean_text: str             # 去除 @机器人 后的干净文本
    mentions: list[MentionedUser] = field(default_factory=list)


class FeishuBotEventParser:
    """
    解析飞书 Webhook 事件（schema 2.0）。
    支持：URL 验证 challenge、消息接收事件（im.message.receive_v1）。
    """

    def __init__(self, verify_token: str = "", encrypt_key: str = ""):
        self.verify_token = verify_token
        self.encrypt_key = encrypt_key

    # ------------------------------------------------------------------ #
    # 加密消息解密
    # ------------------------------------------------------------------ #

    def decrypt_body(self, body: dict) -> dict:
        """
        如果 body 是飞书加密格式 {"encrypt": "..."} 则解密后返回明文 dict，
        否则原样返回。需要 pycryptodome 库。
        """
        encrypted = body.get("encrypt")
        if not encrypted:
            return body
        if not self.encrypt_key:
            logger.warning("收到加密消息但未配置 FEISHU_ENCRYPT_KEY，跳过解密")
            return body
        if not _HAS_CRYPTO:
            logger.error("收到加密消息但未安装 pycryptodome，无法解密")
            return body

        # Key = SHA256(encrypt_key)
        key = hashlib.sha256(self.encrypt_key.encode("utf-8")).digest()
        # base64 decode → IV(16) + ciphertext
        raw = base64.b64decode(encrypted)
        iv, ciphertext = raw[:16], raw[16:]
        cipher = AES.new(key, AES.MODE_CBC, iv)
        plaintext = cipher.decrypt(ciphertext)
        # 去除 PKCS7 padding
        pad = plaintext[-1]
        plaintext = plaintext[:-pad]
        return json.loads(plaintext.decode("utf-8"))

    # ------------------------------------------------------------------ #
    # URL 验证
    # ------------------------------------------------------------------ #

    def handle_challenge(self, body: dict) -> dict | None:
        """如果是 challenge 验证请求，返回 {"challenge": ...}，否则返回 None"""
        # schema 2.0 格式
        if body.get("type") == "url_verification":
            return {"challenge": body.get("challenge", "")}
        # schema 1.0 格式（旧版）
        if "challenge" in body and "token" in body:
            return {"challenge": body["challenge"]}
        return None

    # ------------------------------------------------------------------ #
    # 签名验证
    # ------------------------------------------------------------------ #

    def verify_signature(self, timestamp: str, nonce: str, body_str: str, signature: str) -> bool:
        """验证飞书推送请求的签名（可选）"""
        if not self.verify_token:
            return True
        s = timestamp + nonce + self.verify_token + body_str
        computed = hashlib.sha256(s.encode("utf-8")).hexdigest()
        return hmac.compare_digest(computed, signature)

    # ------------------------------------------------------------------ #
    # 消息解析
    # ------------------------------------------------------------------ #

    def parse_message_event(self, body: dict) -> BotMessage | None:
        """
        解析 im.message.receive_v1 事件，返回 BotMessage。
        不是消息事件则返回 None。
        """
        header = body.get("header", {})
        event_type = header.get("event_type", "")
        if event_type != "im.message.receive_v1":
            return None

        event = body.get("event", {})
        message = event.get("message", {})
        sender = event.get("sender", {})

        message_type = message.get("message_type", "")
        if message_type not in ("text", "post"):
            # 暂只处理文本和富文本消息
            return None

        # 解析消息内容
        content_str = message.get("content", "{}")
        try:
            content = json.loads(content_str)
        except json.JSONDecodeError:
            content = {}

        raw_text = content.get("text", "")

        # 解析 @ 提及的用户
        mentions: list[MentionedUser] = []
        for m in message.get("mentions", []):
            uid = m.get("id", {})
            open_id = uid.get("open_id", "")
            if open_id:
                mentions.append(MentionedUser(
                    key=m.get("key", ""),
                    open_id=open_id,
                    name=m.get("name", ""),
                ))

        # 去除 @机器人 占位符（群聊中 @bot 的占位符不包含实际用户信息，过滤掉）
        clean_text = raw_text
        for m in mentions:
            # 飞书机器人自身也会出现在 mentions 里，这里统一清理占位符
            clean_text = clean_text.replace(m.key, m.name).strip()

        return BotMessage(
            message_id=message.get("message_id", ""),
            sender_open_id=sender.get("sender_id", {}).get("open_id", ""),
            chat_id=message.get("chat_id", ""),
            chat_type=message.get("chat_type", "p2p"),
            raw_text=raw_text,
            clean_text=clean_text,
            mentions=mentions,
        )
