"""群活跃度分级管理

Tier 规则（基于"距最近一条消息的天数"）：
  hot     ≤1天   → 热门群，日报需要统计
  active  1-7天  → 活跃群，周报/双周报需要统计
  warm    7-30天 → 温热群，周报/双周报需要统计（无日报）
  cool    30-90天 → 冷却群，月报需要统计
  cold    90-180天 → 冷清群，三月报需要统计
  zombie  >180天  → 僵尸群，仅按需统计
"""
from __future__ import annotations

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Tier 常量
# ------------------------------------------------------------------ #

TIER_HOT    = "hot"     # ≤1 天
TIER_ACTIVE = "active"  # 1–7 天
TIER_WARM   = "warm"    # 7–30 天
TIER_COOL   = "cool"    # 30–90 天
TIER_COLD   = "cold"    # 90–180 天
TIER_ZOMBIE = "zombie"  # >180 天

TIER_LABELS: dict[str, str] = {
    TIER_HOT:    "热门",
    TIER_ACTIVE: "活跃",
    TIER_WARM:   "温热",
    TIER_COOL:   "冷却",
    TIER_COLD:   "冷清",
    TIER_ZOMBIE: "僵尸",
}

# (tier, 最大静默天数)，按优先级排列
_THRESHOLDS: list[tuple[str, int]] = [
    (TIER_HOT,    1),
    (TIER_ACTIVE, 7),
    (TIER_WARM,   30),
    (TIER_COOL,   90),
    (TIER_COLD,   180),
]

# 各 report_type 允许处理的 tier 集合
# weekly 自动：本周有消息（≤7天）= HOT + ACTIVE
# biweekly/monthly/quarterly 按需：覆盖更冷的群
REPORT_TIERS: dict[str, set[str]] = {
    "daily":     {TIER_HOT},
    "weekly":    {TIER_HOT, TIER_ACTIVE},
    "biweekly":  {TIER_HOT, TIER_ACTIVE, TIER_WARM},
    "monthly":   {TIER_HOT, TIER_ACTIVE, TIER_WARM, TIER_COOL},
    "quarterly": {TIER_HOT, TIER_ACTIVE, TIER_WARM, TIER_COOL, TIER_COLD},
}

# report_type 对应的默认回看天数
REPORT_DAYS: dict[str, int] = {
    "daily":     1,
    "weekly":    7,
    "biweekly":  14,
    "monthly":   30,
    "quarterly": 90,
}

REPORT_LABELS: dict[str, str] = {
    "daily":     "日报",
    "weekly":    "周报",
    "biweekly":  "双周报",
    "monthly":   "月报",
    "quarterly": "三月报",
}

# 缓存有效期（天）：过期后下次 run 会重新扫描
TIER_TTL_DAYS: dict[str, int] = {
    TIER_HOT:    1,
    TIER_ACTIVE: 3,
    TIER_WARM:   7,
    TIER_COOL:   14,
    TIER_COLD:   30,
    TIER_ZOMBIE: 60,
}


# ------------------------------------------------------------------ #
# 工具函数
# ------------------------------------------------------------------ #

def classify_tier(last_msg_ts: int | float) -> str:
    """根据最近消息的 Unix 时间戳（秒）返回 tier。"""
    if not last_msg_ts:
        return TIER_ZOMBIE
    age_days = (time.time() - float(last_msg_ts)) / 86400.0
    for tier, max_days in _THRESHOLDS:
        if age_days <= max_days:
            return tier
    return TIER_ZOMBIE


# ------------------------------------------------------------------ #
# GroupActivityCache
# ------------------------------------------------------------------ #

class GroupActivityCache:
    """持久化缓存：记录每个群的活跃等级与最近消息时间。"""

    def __init__(self, path: str) -> None:
        self._path = path
        self._data: dict[str, dict] = {}
        self._load()

    # ── 持久化 ───────────────────────────────────────────────────────

    def _load(self) -> None:
        if os.path.exists(self._path):
            try:
                with open(self._path, encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception as e:
                logger.warning("加载群活跃缓存失败: %s", e)

    def save(self) -> None:
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("保存群活跃缓存失败: %s", e)

    # ── 更新 ─────────────────────────────────────────────────────────

    def update_found(self, chat_id: str, name: str, last_msg_ts: int) -> str:
        """找到消息后调用：更新 last_msg_ts，重新计算 tier，返回新 tier。"""
        tier = classify_tier(last_msg_ts)
        entry = self._data.setdefault(chat_id, {})
        entry.update({
            "name": name,
            "tier": tier,
            "last_msg_ts": last_msg_ts,
            "checked_at": int(time.time()),
        })
        return tier

    def update_empty(self, chat_id: str, name: str) -> str:
        """本次检查无消息后调用：不改变 last_msg_ts，只更新 checked_at。"""
        entry = self._data.setdefault(chat_id, {})
        last_msg_ts = entry.get("last_msg_ts", 0)
        tier = classify_tier(last_msg_ts) if last_msg_ts else TIER_ZOMBIE
        entry.update({
            "name": name,
            "tier": tier,
            "last_msg_ts": last_msg_ts,
            "checked_at": int(time.time()),
        })
        return tier

    # ── 查询 ─────────────────────────────────────────────────────────

    def get_tier(self, chat_id: str) -> str:
        """返回群的当前 tier（基于 last_msg_ts 实时计算）。"""
        entry = self._data.get(chat_id, {})
        last_msg_ts = entry.get("last_msg_ts", 0)
        if last_msg_ts:
            return classify_tier(last_msg_ts)
        return entry.get("tier", TIER_ZOMBIE)

    def is_fresh(self, chat_id: str) -> bool:
        """缓存是否在有效期内（不需要重新扫描）。"""
        entry = self._data.get(chat_id)
        if not entry:
            return False
        tier = self.get_tier(chat_id)
        ttl = TIER_TTL_DAYS.get(tier, 1) * 86400
        return (time.time() - entry.get("checked_at", 0)) < ttl

    def split_groups(
        self, groups: list[dict], report_type: str
    ) -> tuple[list[dict], list[dict]]:
        """按 report_type 将群列表拆分为 (应处理, 跳过)。

        应处理：
          - 缓存不存在或已过期（需重新扫描）
          - 缓存新鲜且 tier 在 report_type 允许范围内

        跳过：
          - 缓存新鲜且 tier 在 report_type 允许范围外
        """
        allowed = REPORT_TIERS.get(report_type, {TIER_HOT})
        to_process: list[dict] = []
        to_skip: list[dict] = []

        for g in groups:
            cid = g["chat_id"]
            if not self.is_fresh(cid):
                # 未知或缓存过期 → 纳入，结果再更新缓存
                to_process.append(g)
            elif self.get_tier(cid) in allowed:
                to_process.append(g)
            else:
                to_skip.append(g)

        return to_process, to_skip

    def stats(self) -> dict[str, int]:
        """返回各 tier 的群数量统计。"""
        counts: dict[str, int] = {t: 0 for t in
                                   [TIER_HOT, TIER_ACTIVE, TIER_WARM,
                                    TIER_COOL, TIER_COLD, TIER_ZOMBIE]}
        for entry in self._data.values():
            last_msg_ts = entry.get("last_msg_ts", 0)
            tier = classify_tier(last_msg_ts) if last_msg_ts else entry.get("tier", TIER_ZOMBIE)
            counts[tier] = counts.get(tier, 0) + 1
        return counts

    def get_groups_by_tier(self, tier: str) -> list[dict]:
        """返回指定 tier 的所有已缓存群（chat_id + name）。"""
        result = []
        for chat_id, entry in self._data.items():
            last_msg_ts = entry.get("last_msg_ts", 0)
            t = classify_tier(last_msg_ts) if last_msg_ts else entry.get("tier", TIER_ZOMBIE)
            if t == tier:
                result.append({"chat_id": chat_id, "name": entry.get("name", "")})
        return result
