"""AI 服务节点：只在接线选中的分支投递消息，不改变 Gateway／消息分发架构。

服务可自行发送，也可返回正文交给已有发送节点；不默认绑定任何人的机器人或服务。
部署示例和接口契约见 docs/workflow/ai-service.md。
"""
from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlsplit

from ..models import ValidationIssue, WorkflowNode
from .base import ConfigField, EnvironmentFailure, NodeExecutionContext, NodeFailure, PortSpec, TRIGGER_PORT, input_value
from .http import _import_httpx
from .registry import register_node


SERVICE_SENDS = "service-sends"
RETURN_MESSAGE = "return-message"


def validate_ai_service(node: WorkflowNode) -> list[ValidationIssue]:
    errors = []
    mode = node.config.get("response_mode", SERVICE_SENDS)
    if mode not in (SERVICE_SENDS, RETURN_MESSAGE):
        errors.append(ValidationIssue(node_id=node.id, code="INVALID_AI_RESPONSE_MODE", message="AI 回复方式必须是服务自行发送或返回正文", suggestion="选择 service-sends 或 return-message"))
    try:
        url = urlsplit(str(node.config.get("endpoint", "")))
        valid_url = url.scheme in {"http", "https"} and bool(url.hostname) and not url.username and not url.password
        _ = url.port
    except ValueError:
        valid_url = False
    if not valid_url:
        errors.append(ValidationIssue(node_id=node.id, code="INVALID_AI_ENDPOINT", message="AI 服务地址必须是没有内嵌凭据的 HTTP(S) 地址", suggestion="填写服务的消息投递接口"))
    token_env = node.config.get("token_env")
    if not isinstance(token_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
        errors.append(ValidationIssue(node_id=node.id, code="INVALID_AI_CREDENTIAL_VARIABLE", message="AI 服务尚未配置凭据环境变量名", suggestion="显式填写自己的凭据变量名；不会使用其他服务的凭据"))
    try:
        timeout = float(node.config.get("timeout", 15))
        max_timeout = 300 if mode == RETURN_MESSAGE else 60
        if not 1 <= timeout <= max_timeout:
            raise ValueError
    except (ValueError, TypeError):
        errors.append(ValidationIssue(node_id=node.id, code="INVALID_AI_TIMEOUT", message="AI 请求超时超出范围", suggestion="服务自行发送：1–60 秒接单；返回正文：1–300 秒等待正文"))
    return errors


@register_node(
    "ai-service", label="AI 服务", color="#a855f7", category="action", order=125,
    inputs=[TRIGGER_PORT, PortSpec("message", "message", "消息内容", required=True)],
    outputs=[TRIGGER_PORT, PortSpec("accepted", "message", "请求成功"), PortSpec("message", "message", "返回正文")],
    fields=[ConfigField("message", "消息内容"),
            ConfigField("endpoint", "消息投递接口", required=True),
            ConfigField("token_env", "凭据环境变量名", required=True),
            ConfigField("response_mode", "回复方式（服务发送／返回正文）", default=SERVICE_SENDS, options=(SERVICE_SENDS, RETURN_MESSAGE)),
            ConfigField("timeout", "请求超时（秒）", default=15)],
    validator=validate_ai_service,
)
async def exec_ai_service(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    issues = validate_ai_service(node)
    if issues:
        raise ValueError(issues[0].message)
    token = os.environ.get(node.config["token_env"], "").strip()
    if not token:
        raise RuntimeError("AI 服务尚未配置：指定的凭据环境变量未设置；本次未发送 AI 请求")
    data = ctx.trigger_data
    payload = {key: str(data.get(key, "")) for key in ("platform", "chat", "chat_id", "message_id", "user_id")}
    payload["message"] = str(input_value(node, ctx, "message"))
    if payload["chat"] not in {"private", "group"} or not all(payload[k] for k in ("platform", "chat_id", "message_id", "user_id")):
        raise NodeFailure("AI 服务需要真实消息触发，缺少会话、发言人或消息编号")
    httpx = _import_httpx()
    try:
        async with httpx.AsyncClient(timeout=float(node.config.get("timeout", 15)), trust_env=False) as client:
            response = await client.post(str(node.config["endpoint"]), json=payload, headers={"Authorization": "Bearer " + token})
    except httpx.HTTPError as exc:
        # Never retry an uncertain acceptance here: the service may already be
        # generating. Its idempotent message key is what makes manual retry safe.
        raise EnvironmentFailure(f"AI 服务请求失败：{type(exc).__name__}；请检查连接，不自动重复投递") from exc
    if response.status_code >= 400:
        raise NodeFailure(f"AI 服务拒绝接单：HTTP {response.status_code}")
    try:
        result = response.json()
    except ValueError as exc:
        raise NodeFailure("AI 服务没有返回有效 JSON 结果") from exc
    if node.config.get("response_mode", SERVICE_SENDS) == RETURN_MESSAGE:
        if isinstance(result, dict) and result.get("accepted") is False:
            raise NodeFailure("AI 服务拒绝处理本条消息")
        message = result.get("message") if isinstance(result, dict) else None
        if not isinstance(message, str) or not message.strip():
            raise NodeFailure("AI 服务没有返回可发送的正文（需要非空字符串 message）")
        ctx.log.append(f"[ai-service] {node.id}: 正文已返回（由下游发送节点发送）")
        return {"accepted": True, "message": message}
    if not isinstance(result, dict) or result.get("accepted") is not True:
        raise NodeFailure("AI 服务未确认接单")
    ctx.log.append(f"[ai-service] {node.id}: 已接单（正文由服务发送）")
    return {"accepted": True}
