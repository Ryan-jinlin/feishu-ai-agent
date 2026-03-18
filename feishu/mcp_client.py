"""
飞书项目 MCP HTTP 客户端。

通过标准 JSON-RPC 2.0 调用 feishu-project MCP Server，
无需额外依赖，直接用 requests 库。

MCP Server URL 格式:
  https://project.feishu.cn/mcp_server/v1?mcpKey=<key>&userKey=<user_key>
"""
from __future__ import annotations

import itertools
import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)

_MCP_KEY = "m-2127104a-3bb6-44b6-abb4-33f0c20f4026"
_MCP_BASE = "https://project.feishu.cn/mcp_server/v1"
_TIMEOUT = 30
_counter = itertools.count(1)


def _mcp_url(user_key: str) -> str:
    return f"{_MCP_BASE}?mcpKey={_MCP_KEY}&userKey={user_key}"


def _call(user_key: str, method: str, params: dict | None = None) -> Any:
    """发送一个 JSON-RPC 2.0 请求，返回 result 字段内容；出错时抛异常。"""
    payload = {
        "jsonrpc": "2.0",
        "id": next(_counter),
        "method": method,
        "params": params or {},
    }
    url = _mcp_url(user_key)
    resp = requests.post(
        url,
        json=payload,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        raise RuntimeError(f"MCP error: {body['error']}")
    return body.get("result")


class FeishuProjectMCP:
    """飞书项目 MCP 工具调用封装。每个方法对应一个 MCP 工具。"""

    def __init__(self, user_key: str | None = None) -> None:
        self.user_key = user_key or os.getenv("FEISHU_PROJECT_USER_KEY", "")
        if not self.user_key:
            raise ValueError(
                "缺少 FEISHU_PROJECT_USER_KEY，请在 .env 中配置飞书项目用户 Key。"
                "获取方式：飞书项目左下角头像 → 双击头像 → 复制 user_key。"
            )

    def _tool(self, name: str, args: dict) -> Any:
        """统一调用 tools/call 方法。"""
        result = _call(self.user_key, "tools/call", {"name": name, "arguments": args})
        # result 是 {"content": [{"type":"text","text":"..."}], ...}
        content = result.get("content", []) if isinstance(result, dict) else []
        if content:
            # 拼接所有 text 块
            parts = [c.get("text", "") for c in content if c.get("type") == "text"]
            return "\n".join(parts)
        return str(result)

    def get_workitem_brief(self, project_key: str, work_item_id: str,
                           work_item_type: str = "", fields: list[str] | None = None) -> str:
        args: dict = {"project_key": project_key, "work_item_id": work_item_id,
                      "user_key": self.user_key}
        if work_item_type:
            args["work_item_type"] = work_item_type
        if fields:
            args["fields"] = fields
        return self._tool("get_workitem_brief", args)

    def get_workitem_info(self, project_key: str, work_item_type: str) -> str:
        return self._tool("get_workitem_info", {
            "project_key": project_key,
            "work_item_type": work_item_type,
            "user_key": self.user_key,
        })

    def get_view_detail(self, project_key: str, view_id: str,
                        fields: list[str] | None = None, page_num: int = 1) -> str:
        args: dict = {"project_key": project_key, "view_id": view_id,
                      "user_key": self.user_key, "page_num": page_num}
        if fields:
            args["fields"] = fields
        return self._tool("get_view_detail", args)

    def create_workitem(self, project_key: str, work_item_type: str, fields: dict) -> str:
        # MCP 服务期望 fields 为 list[{field_key, field_value}]，而不是 dict
        fields_list = [{"field_key": k, "field_value": v} for k, v in fields.items()]
        return self._tool("create_workitem", {
            "project_key": project_key,
            "work_item_type": work_item_type,
            "fields": fields_list,
            "user_key": self.user_key,
        })

    def update_field(self, project_key: str, work_item_id: str, fields: list[dict]) -> str:
        return self._tool("update_field", {
            "project_key": project_key,
            "work_item_id": work_item_id,
            "fields": fields,
            "user_key": self.user_key,
        })

    def finish_node(self, project_key: str, work_item_id: str, nodes: list[str]) -> str:
        return self._tool("finish_node", {
            "project_key": project_key,
            "work_item_id": work_item_id,
            "nodes": nodes,
            "user_key": self.user_key,
        })

    def get_node_detail(self, project_key: str, work_item_id: str, node_id: str) -> str:
        return self._tool("get_node_detail", {
            "project_key": project_key,
            "work_item_id": work_item_id,
            "node_id": node_id,
            "user_key": self.user_key,
        })

    def search_by_mql(self, moql: str) -> str:
        return self._tool("search_by_mql", {"moql": moql, "user_key": self.user_key})

    def list_schedule(self, project_key: str, user_keys: list[str],
                      start_date: str, end_date: str) -> str:
        return self._tool("list_schedule", {
            "project_key": project_key,
            "user_keys": user_keys,
            "start_date": start_date,
            "end_date": end_date,
            "user_key": self.user_key,
        })
