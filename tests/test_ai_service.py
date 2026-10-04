from __future__ import annotations

import asyncio
from types import SimpleNamespace
import httpx
import pytest
from tickneko.workflow import NodeExecutionContext, WorkflowNode
from tickneko.workflow.nodes.ai_service import exec_ai_service, validate_ai_service
from tickneko.workflow.nodes.base import EnvironmentFailure, NodeFailure


def node(**config):
    return WorkflowNode(id="ai", type="ai-service", config={"endpoint": "http://127.0.0.1:5140/catbot/pipeline", **config})


def context():
    ctx = NodeExecutionContext()
    ctx.trigger_data = dict(platform="onebot", chat="group", chat_id="123", user_id="456", message_id="789")
    ctx.inputs["message"] = "hello"
    return ctx


def http_stub(monkeypatch, status=202, body=None, failure=None):
    calls = []
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            calls.append(kwargs)
            if failure: raise failure
            return httpx.Response(status, json={"accepted": True} if body is None else body)
    monkeypatch.setenv("AI_SERVICE_TOKEN", "local-test-only")
    monkeypatch.setattr("tickneko.workflow.nodes.ai_service._import_httpx", lambda: SimpleNamespace(AsyncClient=Client, HTTPError=httpx.HTTPError))
    return calls


def test_identity_and_message_are_preserved(monkeypatch):
    calls = http_stub(monkeypatch)
    result = asyncio.run(exec_ai_service(node(), context()))
    assert result == {"accepted": True}
    assert calls[0]["json"] == {"platform": "onebot", "chat": "group", "chat_id": "123", "user_id": "456", "message_id": "789", "message": "hello"}
    assert "token" not in calls[0]["json"]


@pytest.mark.parametrize("status,body", [(401, {}), (429, {}), (200, {"accepted": False}), (200, [])])
def test_rejected_or_invalid_ack_is_not_success(monkeypatch, status, body):
    calls = http_stub(monkeypatch, status, body)
    with pytest.raises(NodeFailure): asyncio.run(exec_ai_service(node(), context()))
    assert len(calls) == 1


def test_network_failure_does_not_auto_retry(monkeypatch):
    calls = http_stub(monkeypatch, failure=httpx.ReadTimeout("uncertain"))
    with pytest.raises(EnvironmentFailure): asyncio.run(exec_ai_service(node(), context()))
    assert len(calls) == 1


def test_missing_identity_and_credentials(monkeypatch):
    http_stub(monkeypatch)
    with pytest.raises(NodeFailure): asyncio.run(exec_ai_service(node(message="test"), NodeExecutionContext()))
    monkeypatch.delenv("AI_SERVICE_TOKEN")
    with pytest.raises(RuntimeError): asyncio.run(exec_ai_service(node(), context()))


@pytest.mark.parametrize("config", [{"endpoint": "file:///secret"}, {"endpoint": "http://key:secret@host"}, {"endpoint": "http://[invalid"}, {"endpoint": "http://host:wrong"}, {"timeout": 0}, {"timeout": "oops"}])
def test_config_validation(config):
    assert validate_ai_service(node(**config))
