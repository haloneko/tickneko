from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from examples.ai_chat_workflow import build_graph
from tickneko.workflow import NodeExecutionContext, SimpleWorkflowRunner, WorkflowGraph
from tickneko.workflow.validator import validate_graph
from test_ai_service import http_stub


@pytest.mark.parametrize("chat", ["private", "group"])
@pytest.mark.parametrize("message,local", [("awa", "awa"), ("你好 awa", "awa"), ("bot ping", "机器人服务在线"), ("小猫，摸摸头", None), ("", None)])
def test_preset_selects_one_route(monkeypatch, chat, message, local):
    graph = WorkflowGraph.model_validate(build_graph("http://localhost/ai"))
    assert validate_graph(graph).valid
    calls = http_stub(monkeypatch)
    sent = []

    class Gateway:
        async def reply(self, target, content):
            sent.append(content)
            return SimpleNamespace(ok=True, data={}, message="")

    ctx = NodeExecutionContext(gateway=Gateway())
    ctx.trigger_data = dict(platform="onebot", chat=chat, chat_id="123", user_id="456", message_id="789", message=message,
                            target=SimpleNamespace(platform="onebot", chat=chat, user_id=456, group_id=123))
    asyncio.run(SimpleWorkflowRunner().run(graph, ctx))
    if local:
        assert len(sent) == 1 and local in sent[0]
        assert not calls
    else:
        assert not sent
        assert len(calls) == 1 and calls[0]["json"]["message"] == message


@pytest.mark.parametrize("user,should_call", [("456", False), ("999", False), ("777", True)])
def test_optional_user_filter(monkeypatch, user, should_call):
    graph = WorkflowGraph.model_validate(build_graph("http://localhost/ai", ignored_users=["456", "999", "456"]))
    assert validate_graph(graph).valid
    calls = http_stub(monkeypatch)
    ctx = NodeExecutionContext()
    ctx.trigger_data = dict(platform="onebot", chat="private", chat_id=user, user_id=user, message_id="789", message="你好",
                            target=SimpleNamespace(platform="onebot", chat="private", user_id=int(user)))
    asyncio.run(SimpleWorkflowRunner().run(graph, ctx))
    assert bool(calls) == should_call
