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
    if local:
        monkeypatch.delenv("AI_SERVICE_TOKEN")
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


def test_regular_workflow_without_ai_node_never_calls_ai(monkeypatch):
    calls = http_stub(monkeypatch)
    graph = WorkflowGraph.model_validate({"nodes": [
        {"id": "start", "type": "start", "config": {"trigger": "message"}},
        {"id": "send", "type": "send", "config": {"message": "普通工作流"}},
        {"id": "end", "type": "end"},
    ], "edges": [
        {"source": "start", "target": "send"},
        {"source": "start", "target": "send", "source_port": "target", "target_port": "target"},
        {"source": "send", "target": "end"},
    ]})
    assert validate_graph(graph).valid
    sent = []
    class Gateway:
        async def reply(self, target, content):
            sent.append(content)
            return SimpleNamespace(ok=True, data={}, message="")
    ctx = NodeExecutionContext(gateway=Gateway())
    ctx.trigger_data = {"message": "随便的内容", "target": object()}
    asyncio.run(SimpleWorkflowRunner().run(graph, ctx))
    assert sent == ["普通工作流"]
    assert calls == []


@pytest.mark.parametrize("chat", ["private", "group"])
def test_other_users_service_replies_through_their_own_target(monkeypatch, chat):
    calls = http_stub(monkeypatch, status=200, body={"message": "自己的 AI 正文", "target": "不能让服务改发给别人"})
    sent = []

    class Gateway:
        async def reply(self, target, content):
            sent.append((target, content))
            return SimpleNamespace(ok=True, data={}, message="")

    targets = []
    for owner in ("alice", "bob"):
        endpoint = f"http://{owner}.localhost/ai"
        token_env = f"{owner.upper()}_SERVICE_TOKEN"
        monkeypatch.setenv(token_env, f"{owner}-test-only")
        graph = WorkflowGraph.model_validate(build_graph(endpoint, token_env, response_mode="return-message"))
        assert validate_graph(graph).valid
        target = SimpleNamespace(platform="onebot", owner_id=owner, chat=chat, user_id=456, group_id=123)
        targets.append(target)
        ctx = NodeExecutionContext(gateway=Gateway(), owner_id=owner)
        ctx.trigger_data = dict(platform="onebot", chat=chat, chat_id="123", user_id="456", message_id="789", message="你好", target=target)
        asyncio.run(SimpleWorkflowRunner().run(graph, ctx))

    assert sent == [(targets[0], "自己的 AI 正文"), (targets[1], "自己的 AI 正文")]
    assert sent[0][0] is targets[0] and sent[1][0] is targets[1]
    assert [call["url"] for call in calls] == ["http://alice.localhost/ai", "http://bob.localhost/ai"]
    assert [call["headers"]["Authorization"] for call in calls] == ["Bearer alice-test-only", "Bearer bob-test-only"]


@pytest.mark.parametrize("message", ["awa", "bot ping"])
def test_reply_preset_local_branch_needs_no_ai_credentials(monkeypatch, message):
    calls = http_stub(monkeypatch, status=200, body={"message": "不该调用"})
    monkeypatch.delenv("AI_SERVICE_TOKEN")
    graph = WorkflowGraph.model_validate(build_graph("http://others.localhost/ai", response_mode="return-message"))
    sent = []
    class Gateway:
        async def reply(self, target, content):
            sent.append(content)
            return SimpleNamespace(ok=True, data={}, message="")
    ctx = NodeExecutionContext(gateway=Gateway())
    ctx.trigger_data = dict(message=message, target=object())
    asyncio.run(SimpleWorkflowRunner().run(graph, ctx))
    assert len(sent) == 1 and not calls


def test_reply_service_empty_body_stops_send_branch(monkeypatch):
    calls = http_stub(monkeypatch, status=200, body={"accepted": True})
    graph = WorkflowGraph.model_validate(build_graph("http://others.localhost/ai", response_mode="return-message"))
    class Gateway:
        async def reply(self, *args):
            pytest.fail("空正文不应该触发下游发送")
    ctx = NodeExecutionContext(gateway=Gateway())
    ctx.trigger_data = dict(platform="onebot", chat="private", chat_id="456", user_id="456", message_id="789", message="你好", target=object())
    asyncio.run(SimpleWorkflowRunner().run(graph, ctx))
    assert len(calls) == 1
    assert any("[skip]" in entry and "send_ai" in entry for entry in ctx.log)


def test_preset_rejects_unknown_reply_mode():
    with pytest.raises(ValueError, match="回复方式"):
        build_graph("http://localhost/ai", response_mode="unknown")
