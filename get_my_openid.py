"""
快速获取你的飞书 open_id。
运行方法：
  python get_my_openid.py                        # 交互模式
  python get_my_openid.py --email xxx@momenta.ai # 邮箱直接查询
  python get_my_openid.py --mobile +8613800138000# 手机号查询
"""
from __future__ import annotations

import argparse
import os
import requests
from dotenv import load_dotenv

load_dotenv()

APP_ID = os.environ["FEISHU_APP_ID"]
APP_SECRET = os.environ["FEISHU_APP_SECRET"]
FEISHU_HOST = "https://open.feishu.cn"


def get_token():
    resp = requests.post(
        f"{FEISHU_HOST}/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET},
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取 Token 失败: {data}")
    return data["tenant_access_token"]


def get_open_id_by_email(email: str, token: str) -> str | None:
    resp = requests.post(
        f"{FEISHU_HOST}/open-apis/contact/v3/users/batch_get_id",
        headers={"Authorization": f"Bearer {token}"},
        params={"user_id_type": "open_id"},
        json={"emails": [email]},
    )
    data = resp.json()
    if data.get("code") != 0:
        print(f"查询失败: {data}")
        return None
    users = data.get("data", {}).get("user_list", [])
    if users:
        return users[0].get("user_id")
    return None


def get_open_id_by_mobile(mobile: str, token: str) -> str | None:
    # 手机号需加国际区号，如 +8613800138000
    resp = requests.post(
        f"{FEISHU_HOST}/open-apis/contact/v3/users/batch_get_id",
        headers={"Authorization": f"Bearer {token}"},
        params={"user_id_type": "open_id"},
        json={"mobiles": [mobile]},
    )
    data = resp.json()
    if data.get("code") != 0:
        print(f"查询失败: {data}")
        return None
    users = data.get("data", {}).get("user_list", [])
    if users:
        return users[0].get("user_id")
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="获取飞书 open_id")
    parser.add_argument("--email", help="飞书邮箱，如 xxx@momenta.ai")
    parser.add_argument("--mobile", help="手机号（含国际区号），如 +8613800138000")
    args = parser.parse_args()

    token = get_token()
    open_id = None

    if args.email:
        open_id = get_open_id_by_email(args.email, token)
    elif args.mobile:
        open_id = get_open_id_by_mobile(args.mobile, token)
    else:
        # 交互模式
        print("=== 获取飞书 open_id ===")
        print("1. 通过邮箱查询")
        print("2. 通过手机号查询")
        choice = input("选择方式 (1/2): ").strip()
        if choice == "1":
            email = input("输入你的飞书邮箱（如 xxx@momenta.ai）: ").strip()
            open_id = get_open_id_by_email(email, token)
        elif choice == "2":
            mobile = input("输入手机号（含国际区号，如 +8613800138000）: ").strip()
            open_id = get_open_id_by_mobile(mobile, token)
        else:
            print("无效选择")

    if open_id:
        print(f"\n你的 open_id 是：{open_id}")
        print(f"\n请将以下内容填入 .env 文件：")
        print(f"BOT_OWNER_OPEN_ID={open_id}")
    else:
        print("\n未找到用户，请确认邮箱/手机号是否正确，以及应用是否有 contact:user.base:readonly 权限。")
