"""可选的 AI 聊天预设；只组合公开节点，不改变框架默认行为。

调用 build_graph(endpoint) 得到可保存为版本的图。部署步骤见
docs/workflow/ai-service.md。默认本地分支只是演示，可在画布中替换。
"""
from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from typing import Any


def build_graph(endpoint: str, token_env: str = "AI_SERVICE_TOKEN", ignored_users: Sequence[str] = (), *, response_mode: str = "service-sends") -> dict[str, Any]:
    """OneBot 消息 → 可选用户过滤 → 本地回复 → 未命中调用 AI 服务。

    endpoint / 凭据变量 / 屏蔽名单由使用者提供；不包含私人账号或密钥。
    服务自行发送时只等接单；返回正文时由现有发送节点回复本次触发的会话。
    """
    if response_mode not in ("service-sends", "return-message"):
        raise ValueError("回复方式必须是 service-sends 或 return-message")
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []

    def node(node_id: str, kind: str, x: int, y: int, **config: Any) -> None:
        nodes.append(dict(id=node_id, type=kind, x=x, y=y, config=config))

    def edge(source: str, target: str, source_port: str = "trigger", target_port: str = "trigger") -> None:
        edges.append(dict(source=source, target=target, source_port=source_port, target_port=target_port))

    node("start", "start", 0, 200, trigger="message")
    node("end", "end", 1200, 200)
    previous, previous_port = "start", "trigger"
    if ignored_users:
        node("identity", "unpack-onebot", 220, 500)
        edge("start", "identity")
        edge("start", "identity", "target", "target")
        previous = "identity"
        for index, user in enumerate(dict.fromkeys(map(str, ignored_users))):
            gate = f"allowed_{index}"
            node(gate, "condition", 440 + index * 220, 500, operator="!=", right=user)
            edge(previous, gate, previous_port)
            edge("identity", gate, "user_id", "left")
            edge(gate, "end", "false")
            previous, previous_port = gate, "true"

    node("awa", "condition", 440, 200, operator="contains", right="awa")
    node("ping", "condition", 680, 300, operator="contains", right="bot ping")
    node("send_awa", "send", 680, 0, message="awa")
    node("send_ping", "send", 900, 180, message="[系统提示：机器人服务在线。此回复不调用 AI。]")
    node("ai", "ai-service", 900, 400, endpoint=endpoint, token_env=token_env, response_mode=response_mode,
         timeout=60 if response_mode == "return-message" else 15)
    edge(previous, "awa", previous_port)
    edge("awa", "send_awa", "true")
    edge("awa", "ping", "false")
    edge("ping", "send_ping", "true")
    edge("ping", "ai", "false")
    for gate in ("awa", "ping"):
        edge("start", gate, "message", "left")
    edge("start", "ai", "message", "message")
    for sender in ("send_awa", "send_ping"):
        edge("start", sender, "target", "target")
        edge(sender, "end")
    if response_mode == "return-message":
        node("send_ai", "send", 1120, 400)
        edge("ai", "send_ai")
        edge("ai", "send_ai", "message", "message")
        edge("start", "send_ai", "target", "target")
        edge("send_ai", "end")
    else:
        edge("ai", "end")
    return dict(nodes=nodes, edges=edges)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="输出可选 AI 聊天预设的图 JSON，不自动创建或启用工作流")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--token-env", default="AI_SERVICE_TOKEN")
    parser.add_argument("--response-mode", choices=("service-sends", "return-message"), default="service-sends")
    arguments = parser.parse_args()
    print(json.dumps(build_graph(arguments.endpoint, arguments.token_env, response_mode=arguments.response_mode), ensure_ascii=False, indent=2))
