"""
FMP 车辆查询模块 — 查询归属指定项目的车辆及其预约状态（空闲 / 占用）。

依赖：playwright、requests
Session 文件：.fmp_session.json（由 scripts/fmp_login.py 生成）
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytz
import requests

logger = logging.getLogger(__name__)

TZ_SHANGHAI = pytz.timezone("Asia/Shanghai")
FMP_BASE = "https://fmp.momenta.works"
SESSION_FILE = Path(__file__).parent.parent / ".fmp_session.json"

# token + cookies 缓存（内存）
_session_cache: dict = {}


def _load_session() -> dict:
    """读取 .fmp_session.json，返回 session dict。未找到则返回 {}。"""
    if SESSION_FILE.exists():
        try:
            return json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("读取 FMP session 失败: %s", e)
    return {}


def _get_session() -> tuple[str | None, dict]:
    """返回 (id_token, cookies_dict)。优先内存缓存，否则从文件读取。"""
    global _session_cache
    # 内存缓存有效期 1 小时
    if _session_cache.get("token") and time.time() - _session_cache.get("ts", 0) < 3600:
        return _session_cache["token"], _session_cache.get("cookies", {})

    session = _load_session()
    token = session.get("fmp_id_token") or session.get("fmp_access_token")

    # 从 Playwright storage_state 中提取 cookies
    cookies = {}
    for c in session.get("cookies", []):
        domain = c.get("domain", "")
        if "fmp.momenta.works" in domain or "mmtwork.com" in domain:
            cookies[c["name"]] = c["value"]

    if token:
        _session_cache = {"token": token, "cookies": cookies, "ts": time.time()}
    return token, cookies


def _get_token() -> str | None:
    token, _ = _get_session()
    return token


def _refresh_token_headless() -> str | None:
    """使用账号密码无头登录，刷新 FMP token 和 cookies。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("playwright 未安装")
        return None

    username = os.environ.get("FMP_USERNAME", "")
    password = os.environ.get("FMP_PASSWORD", "")
    if not username or not password:
        logger.error("FMP_USERNAME 或 FMP_PASSWORD 未配置")
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.goto(f"{FMP_BASE}/reserveVehicle", timeout=30000)
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
            except Exception as e:
                logger.error("FMP 登录表单填写失败: %s", e)
                browser.close()
                return None

            # 等待跳转回 FMP
            try:
                page.wait_for_url(f"{FMP_BASE}/**", timeout=20000)
            except Exception:
                pass

            # 等 id_token 写入 localStorage
            id_token = None
            for _ in range(15):
                try:
                    page.wait_for_load_state("networkidle", timeout=3000)
                except Exception:
                    pass
                id_token = page.evaluate("() => localStorage.getItem('id_token')")
                if id_token:
                    break
                page.wait_for_timeout(1000)

            if id_token:
                access_token = page.evaluate("() => localStorage.getItem('access_token')")
                storage = context.storage_state()
                storage["fmp_id_token"] = id_token
                storage["fmp_access_token"] = access_token
                SESSION_FILE.write_text(json.dumps(storage, ensure_ascii=False, indent=2))
                logger.info("FMP token 无头刷新成功，id_token len=%d", len(id_token))

            browser.close()
            return id_token
    except Exception as e:
        logger.error("FMP 无头刷新失败: %s", e)
        return None


def _fmp_get(path: str, params: dict | None = None, retry: bool = True) -> dict | list | None:
    """向 FMP API 发 GET 请求（带 token + cookies），自动处理 token 失效时的刷新。"""
    token, cookies = _get_session()
    if not token:
        logger.error("FMP token 不存在，请先运行 scripts/fmp_login.py 登录")
        return None

    headers = {
        "Authorization": token,
        "Content-Type": "application/json",
    }
    url = f"{FMP_BASE}{path}"
    try:
        resp = requests.get(url, headers=headers, cookies=cookies, params=params, timeout=15)
        if resp.status_code == 401 and retry:
            logger.info("FMP token 过期，尝试无头刷新...")
            new_token = _refresh_token_headless()
            if new_token:
                global _session_cache
                _session_cache = {}  # 清除缓存，强制重新读取文件
                return _fmp_get(path, params, retry=False)
            return None
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("code") not in (0, 200, None):
            logger.warning("FMP API 返回非 0: code=%s msg=%s", data.get("code"), data.get("message"))
        return data
    except Exception as e:
        logger.error("FMP GET %s 失败: %s", path, e)
        return None


def query_idle_vehicles(project: str = "Project-MXS", start_hours: int = 0, end_hours: int = 8) -> dict:
    """
    查询指定项目的车辆预约情况，找出当前时间段内空闲的车辆。

    Args:
        project: 项目名称，如"Project-MXS"
        start_hours: 查询开始偏移（相对当前，小时），0 = 现在
        end_hours: 查询结束偏移，8 = 8小时后

    Returns:
        {
            "idle": [{"car_plate": "...", "car_type": "...", "maintain_status": "..."}],
            "busy": [...],
            "total": int,
            "query_time": "HH:MM ~ HH:MM",
            "error": None or "...",
        }
    """
    now = datetime.now(TZ_SHANGHAI)
    start = now + timedelta(hours=start_hours)
    end = now + timedelta(hours=end_hours)

    # Step 1: 获取所有车辆，按 vehicle_project 过滤
    car_resp = _fmp_get("/api/v1/vehicle/apply_car/list", params={"page": 1, "page_size": 500})
    if not isinstance(car_resp, list):
        # 尝试第二次（可能 token 刚刷新）
        car_resp = _fmp_get("/api/v1/vehicle/apply_car/list", params={"page": 1, "page_size": 500})

    if not isinstance(car_resp, list):
        return {"idle": [], "busy": [], "total": 0, "query_time": "", "error": "获取车辆列表失败，请检查 FMP session 是否有效"}

    # 全量拉取（最多 2000 辆）
    all_cars = list(car_resp)
    if len(car_resp) == 500:
        for pg in range(2, 5):
            extra = _fmp_get("/api/v1/vehicle/apply_car/list", params={"page": pg, "page_size": 500})
            if not isinstance(extra, list) or not extra:
                break
            all_cars.extend(extra)
            if len(extra) < 500:
                break

    # 按项目过滤，同时排除维修中的车辆
    cars = []
    for c in all_cars:
        proj = c.get("vehicle_project") or c.get("belong_to") or ""
        if project and project not in str(proj):
            continue
        maintain = c.get("maintain_status") or ""
        if maintain in ("维修中", "停用", "报废"):
            continue
        cars.append(c)

    if not cars:
        return {"idle": [], "busy": [], "total": 0, "query_time": "", "error": f"未找到归属「{project}」的正常车辆，请确认项目名称是否正确"}

    # Step 2: 查询时间段内已有预约（使用正确的 ISO 时间格式 + cookies）
    start_iso = start.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    end_iso = end.strftime("%Y-%m-%dT%H:%M:%S+08:00")

    task_resp = _fmp_get(
        "/api/v1/apply_car/list_task_universal",
        params={
            "maintain_status": "正常",
            "start_time": start_iso,
            "end_time": end_iso,
            "only_mine": "false",
            "role": "creator",
            "page": 1,
            "page_size": 500,
        },
    )

    busy_plates: set[str] = set()
    tasks_raw = []
    if isinstance(task_resp, dict) and task_resp.get("code") in (0, 200):
        tasks_raw = task_resp.get("data") or []
    elif isinstance(task_resp, list):
        tasks_raw = task_resp

    for t in tasks_raw:
        car_info = t.get("car_info") or {}
        plate = (
            car_info.get("car_plate")
            or t.get("car_plate")
            or t.get("device_box_id")
            or ""
        )
        if plate:
            busy_plates.add(str(plate))

    # Step 3: 分类空闲 / 占用
    idle, busy = [], []
    for c in cars:
        plate = c.get("car_plate") or c.get("device_box_id") or ""
        entry = {
            "car_plate": plate,
            "car_type": c.get("car_type") or "",
            "vehicle_project": c.get("vehicle_project") or project,
            "maintain_status": c.get("maintain_status") or "正常",
            "team": c.get("team") or "",
        }
        if str(plate) in busy_plates:
            busy.append(entry)
        else:
            idle.append(entry)

    return {
        "idle": idle,
        "busy": busy,
        "total": len(cars),
        "query_time": f"{start.strftime('%H:%M')} ~ {end.strftime('%H:%M')}",
        "error": None,
    }


def check_session_valid() -> bool:
    """检查 FMP session 是否存在（不验证 token 有效性）。"""
    return SESSION_FILE.exists() and bool(_get_token())
