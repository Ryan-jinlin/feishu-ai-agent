"""
FMP 登录脚本 — 无头自动登录（从 .env 读取账号密码），保存 session 供 bot 复用。

用法：
    python scripts/fmp_login.py

登录成功后会把 storage state 保存到 .fmp_session.json，供 bot 使用。
"""
import json
import os
import sys
from pathlib import Path

SESSION_FILE = Path(__file__).parent.parent / ".fmp_session.json"
FMP_URL = "https://fmp.momenta.works/reserveVehicle"


def _load_env():
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def main():
    _load_env()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("请先安装 playwright：pip install playwright && python -m playwright install chromium")
        sys.exit(1)

    username = os.environ.get("FMP_USERNAME", "")
    password = os.environ.get("FMP_PASSWORD", "")

    if not username or not password:
        print("未找到 FMP_USERNAME / FMP_PASSWORD，请在 .env 中配置")
        sys.exit(1)

    print("=" * 60)
    print("FMP 自动登录")
    print("=" * 60)
    print(f"账号: {username}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        print(f"导航到 FMP...")
        page.goto(FMP_URL, timeout=30000)
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(2000)

        # 填写 Keycloak 登录表单
        try:
            page.wait_for_selector('input[name="username"]', timeout=8000)
            page.fill('input[name="username"]', username)
            page.fill('input[name="password"]', password)
            btn = page.query_selector("#kc-login, button[type=submit]")
            if btn:
                btn.click()
            else:
                page.keyboard.press("Enter")
            print("已提交登录表单，等待跳转...")
        except Exception as e:
            print(f"登录表单填写失败: {e}")
            browser.close()
            sys.exit(1)

        # 等待跳转回 FMP
        try:
            page.wait_for_url("https://fmp.momenta.works/**", timeout=20000)
            print(f"已跳转到 FMP")
        except Exception:
            print("等待跳转超时，继续尝试...")

        # 等 id_token 写入 localStorage（最多 20s）
        id_token = None
        for i in range(20):
            try:
                page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass
            id_token = page.evaluate("() => localStorage.getItem('id_token')")
            if id_token:
                print(f"id_token 已获取（长度 {len(id_token)}）第 {i+1} 次")
                break
            page.wait_for_timeout(1000)

        if not id_token:
            print("未能获取 id_token，登录可能失败，请检查账号密码")
            browser.close()
            sys.exit(1)

        access_token = page.evaluate("() => localStorage.getItem('access_token')")
        print(f"access_token 长度: {len(access_token) if access_token else 0}")

        # 保存完整 storage state（cookies + localStorage）
        storage = context.storage_state()
        storage["fmp_id_token"] = id_token
        storage["fmp_access_token"] = access_token

        SESSION_FILE.write_text(json.dumps(storage, ensure_ascii=False, indent=2))
        print(f"\nSession 已保存到：{SESSION_FILE}")
        print("Bot 现在可以无头查询 FMP 了！")

        browser.close()


if __name__ == "__main__":
    main()
