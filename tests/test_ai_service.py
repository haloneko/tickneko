from __future__ import annotations

import asyncio
from types import SimpleNamespace
import httpx
import pytest
from tickneko.workflow import NodeExecutionContext, WorkflowNode
from tickneko.workflow.nodes.ai_service import exec_ai_service, validate_ai_service
from tickneko.workflow.nodes.base import EnvironmentFailure, NodeFailure


def node(**config):
    return WorkflowNode(id="ai", type="ai-service", config={"endpoint": "http://127.0.0.1:5140/catbot/pipeline", "token_env": "AI_SERVICE_TOKEN", **config})


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
            calls.append({"url": url, **kwargs})
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


@pytest.mark.parametrize("missing", ["endpoint", "token_env"])
def test_unconfigured_node_never_opens_http_even_with_other_credentials(monkeypatch, missing):
    monkeypatch.setenv("AI_SERVICE_TOKEN", "some-other-service")
    monkeypatch.setenv("BOTNODE_TOKEN", "private-cat-credential")
    def forbidden_http():
        pytest.fail("An unconfigured AI node must not open an HTTP client")
    monkeypatch.setattr("tickneko.workflow.nodes.ai_service._import_httpx", forbidden_http)
    incomplete = node()
    del incomplete.config[missing]
    assert validate_ai_service(incomplete)
    with pytest.raises(ValueError):
        asyncio.run(exec_ai_service(incomplete, context()))


@pytest.mark.parametrize("value", [None, "", "  ", "OTHER TOKEN", "${BOTNODE_TOKEN}"])
def test_invalid_credential_variable_is_configuration_error(value):
    assert any(issue.code == "INVALID_AI_CREDENTIAL_VARIABLE" for issue in validate_ai_service(node(token_env=value)))


def test_no_credentials_means_zero_requests_and_no_private_fallback(monkeypatch):
    monkeypatch.delenv("AI_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("BOTNODE_TOKEN", "private-cat-credential")
    def forbidden_http():
        pytest.fail("Missing credentials must be checked before importing / calling HTTP")
    monkeypatch.setattr("tickneko.workflow.nodes.ai_service._import_httpx", forbidden_http)
    with pytest.raises(RuntimeError, match="本次未发送 AI 请求"):
        asyncio.run(exec_ai_service(node(), context()))


def test_return_message_is_data_only_and_does_not_send(monkeypatch):
    calls = http_stub(monkeypatch, status=200, body={"message": "你好，这是我的服务。", "target": "其他机器人"})
    ctx = context()
    ctx.gateway = SimpleNamespace(reply=lambda *args: pytest.fail("AI 节点不能自己发送"))
    result = asyncio.run(exec_ai_service(node(response_mode="return-message"), ctx))
    assert result == {"accepted": True, "message": "你好，这是我的服务。"}
    assert len(calls) == 1


@pytest.mark.parametrize("body", [{"accepted": True}, {"message": " "}, {"message": None}, {"message": 123}, [], {"accepted": False, "message": "未处理"}])
def test_return_message_requires_real_body(monkeypatch, body):
    http_stub(monkeypatch, status=200, body=body)
    with pytest.raises(NodeFailure):
        asyncio.run(exec_ai_service(node(response_mode="return-message"), context()))


@pytest.mark.parametrize("mode,timeout,valid", [("service-sends", 60, True), ("service-sends", 61, False), ("return-message", 300, True), ("return-message", 301, False), ("unknown", 15, False)])
def test_reply_modes_and_timeout_validation(mode, timeout, valid):
    assert bool(validate_ai_service(node(response_mode=mode, timeout=timeout))) is not valid


def test_unconfigured_reply_mode_never_uses_network(monkeypatch):
    monkeypatch.setattr("tickneko.workflow.nodes.ai_service._import_httpx", lambda: pytest.fail("非法模式不能联网"))
    with pytest.raises(ValueError, match="AI 回复方式"):
        asyncio.run(exec_ai_service(node(response_mode="unknown"), context()))
