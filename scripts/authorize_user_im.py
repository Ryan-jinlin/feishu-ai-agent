"""
一次性脚本：用 Bot 的内部应用凭证做 OAuth 授权，获取用户的 user_access_token。
用途：让 Bot 以你的身份读取你所在群（但 Bot 不在）的消息。

使用前：在飞书开放平台 → 安全设置 → 重定向 URL，添加：
  http://localhost:19999/callback

使用方式：
  cd /Users/guoyanhua/项目管理/AI Agent/personal-assistant
  python3 scripts/authorize_user_im.py
"""
from __future__ import annotations

import http.server
import json
import os
import sys
import threading
import urllib.parse
import webbrowser

import requests

# ── 配置 ─────────────────────────────────────────────────────────────
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 从 .env 读取 app 凭证
def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    env_file = os.path.join(_BASE_DIR, ".env")
    if os.path.exists(env_file):
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip().strip('"').strip("'")
    return env

_env = _load_env()
APP_ID     = _env.get("FEISHU_APP_ID", "")
APP_SECRET = _env.get("FEISHU_APP_SECRET", "")

if not APP_ID or not APP_SECRET:
    print("错误：未找到 FEISHU_APP_ID / FEISHU_APP_SECRET，请检查 .env 文件")
    sys.exit(1)

REDIRECT_URI = "http://localhost:19999/callback"
SCOPE        = "im:message:readonly im:message.group_msg:get_as_user im:chat:readonly search:message im:message.send_as_user offline_access"
TOKEN_FILE   = os.path.join(_BASE_DIR, ".user_im_token.json")
FEISHU_HOST  = "https://open.feishu.cn"
FEISHU_AUTH_HOST = "https://accounts.feishu.cn"  # v2 OAuth 使用 accounts 子域名

# ── OAuth 回调服务器 ──────────────────────────────────────────────────
_code_holder: dict[str, str] = {}
_server_event = threading.Event()


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params:
            _code_holder["code"] = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                "<html><body><h2>授权成功，可以关闭此窗口。</h2></body></html>".encode()
            )
        else:
            self.send_response(400)
            self.end_headers()
        _server_event.set()

    def log_message(self, *args: object) -> None:  # 静默日志
        pass


def _start_callback_server() -> http.server.HTTPServer:
    server = http.server.HTTPServer(("localhost", 19999), _CallbackHandler)
    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()
    return server


# ── Token 交换 ────────────────────────────────────────────────────────
def _exchange_code(code: str) -> dict:
    """使用 v2 OAuth 端点换 token（form data，无需先获取 app_access_token）。"""
    resp = requests.post(
        f"{FEISHU_HOST}/open-apis/authen/v2/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": APP_ID,
            "client_secret": APP_SECRET,
            "code": code,
            "redirect_uri": REDIRECT_URI,
        },
        timeout=15,
    )
    return resp.json()


def _save_token(data: dict) -> None:
    import time as _time
    data = dict(data)
    data["saved_at"] = int(_time.time())
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Token 已保存至 {os.path.basename(TOKEN_FILE)}")


# ── 主流程 ────────────────────────────────────────────────────────────
def main() -> None:
    print("=" * 60)
    print("飞书用户 IM 权限授权")
    print("=" * 60)
    print()
    print("前置条件：飞书开放平台 → 安全设置 → 重定向 URL 中已添加：")
    print(f"  {REDIRECT_URI}")
    print()

    server = _start_callback_server()

    auth_url = (
        f"{FEISHU_HOST}/open-apis/authen/v1/authorize"
        f"?app_id={APP_ID}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
        f"&scope={urllib.parse.quote(SCOPE)}"
        f"&state=personal_assistant_im"
    )

    print("正在打开浏览器授权页面...")
    print(f"\n如果浏览器未自动打开，请手动访问：\n{auth_url}\n")
    webbrowser.open(auth_url)

    print("等待授权回调（最长 120 秒）...")
    _server_event.wait(timeout=120)
    server.server_close()

    if "code" not in _code_holder:
        print("授权超时或失败，请重试")
        sys.exit(1)

    code = _code_holder["code"]
    print(f"已获取授权码: {code[:12]}...")

    print("正在兑换 token...")
    result = _exchange_code(code)
    if result.get("code") != 0:
        print(f"Token 兑换失败: {result}")
        sys.exit(1)

    # v2 OAuth: data 字段直接在顶层（无 data 嵌套）
    token_data = result if result.get("access_token") else result.get("data", {})
    _save_token(token_data)

    expires_in = token_data.get("expires_in", "?")
    name       = token_data.get("name", "?")
    print(f"\n授权成功！用户：{name}，access_token 有效期：{expires_in}s")
    print("可以重启 Bot，用户身份读消息功能已就绪。")


if __name__ == "__main__":
    main()
