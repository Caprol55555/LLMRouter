import asyncio
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from openclaw_router.config import (
    LLMConfig,
    OpenClawConfig,
    RouterConfig,
    SecurityConfig,
    SessionRoutingConfig,
)
from openclaw_router.server import create_app
from openclaw_router.session_routing import (
    AutoPolicy,
    SessionRouteCache,
    derive_session_key,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class RouterAwareAsyncClient:
    judge_calls = 0
    backend_calls = 0
    judge_model = "glm"
    judge_payloads = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, headers=None, json=None, timeout=None):
        if json["model"] == "judge-deepseek":
            type(self).judge_calls += 1
            type(self).judge_payloads.append(json)
            return FakeResponse(
                payload={
                    "choices": [
                        {"message": {"content": json_module.dumps({"model": type(self).judge_model})}}
                    ]
                }
            )

        type(self).backend_calls += 1
        return FakeResponse(
            payload={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "model": json["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )


json_module = json


def build_config(*, secret="inbound-secret", interval=2):
    return OpenClawConfig(
        show_model_prefix=False,
        router=RouterConfig(
            strategy="llm",
            provider="nine_router",
            model="judge-deepseek",
            base_url="http://9router:20128/v1",
            auth_mode="bearer",
            default_model="glm",
        ),
        llms={
            name: LLMConfig(
                name=name,
                provider="nine_router",
                model_id=name,
                base_url="http://9router:20128/v1",
                auth_mode="bearer",
                description=f"{name} backend",
            )
            for name in ("glm", "deepseek", "qwen")
        },
        api_keys={"nine_router": "internal-secret"},
        session_routing=SessionRoutingConfig(
            enabled=True,
            ttl_seconds=1800,
            rejudge_every_user_turns=interval,
            allowed_rejudge_intervals=[1, 2, 3, 5, 10],
            max_entries=100,
        ),
        security=SecurityConfig(inbound_api_key=secret),
    )


class SessionAwareServerTests(unittest.TestCase):
    def setUp(self):
        RouterAwareAsyncClient.judge_calls = 0
        RouterAwareAsyncClient.backend_calls = 0
        RouterAwareAsyncClient.judge_model = "glm"
        RouterAwareAsyncClient.judge_payloads = []

    def _client(self, **kwargs):
        return TestClient(create_app(config=build_config(**kwargs)))

    @staticmethod
    def _headers():
        return {"Authorization": "Bearer inbound-secret"}

    def test_v1_requires_configured_bearer_key(self):
        client = self._client()
        self.assertEqual(client.get("/health").status_code, 200)
        self.assertEqual(client.get("/v1/models").status_code, 401)
        self.assertEqual(client.get("/v1/models", headers=self._headers()).status_code, 200)

    def test_tool_loop_reuses_judge_and_new_user_turns_rejudge(self):
        client = self._client(interval=2)
        first = {
            "model": "auto",
            "user": "session-1",
            "messages": [{"role": "user", "content": "Implement a parser"}],
        }
        tool_loop = {
            **first,
            "messages": first["messages"]
            + [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call-1", "content": "file contents"},
            ],
        }
        second_user = {
            **tool_loop,
            "messages": tool_loop["messages"] + [{"role": "user", "content": "Add tests"}],
        }
        third_user = {
            **second_user,
            "messages": second_user["messages"] + [{"role": "user", "content": "Optimize it"}],
        }

        with patch("openclaw_router.routers.httpx.AsyncClient", RouterAwareAsyncClient), patch(
            "openclaw_router.server.httpx.AsyncClient", RouterAwareAsyncClient
        ):
            for body in (first, tool_loop, second_user, third_user):
                response = client.post("/v1/chat/completions", headers=self._headers(), json=body)
                self.assertEqual(response.status_code, 200, response.text)

        self.assertEqual(RouterAwareAsyncClient.judge_calls, 2)
        self.assertEqual(RouterAwareAsyncClient.backend_calls, 4)
        self.assertTrue(all(payload["stream"] is False for payload in RouterAwareAsyncClient.judge_payloads))

    def test_auto_once_and_explicit_model_bypass_repeated_judging(self):
        client = self._client(interval=1)
        with patch("openclaw_router.routers.httpx.AsyncClient", RouterAwareAsyncClient), patch(
            "openclaw_router.server.httpx.AsyncClient", RouterAwareAsyncClient
        ):
            for turn in range(1, 4):
                body = {
                    "model": "auto:once",
                    "user": "sticky-session",
                    "messages": [
                        {"role": "user", "content": f"turn {index}"}
                        for index in range(1, turn + 1)
                    ],
                }
                self.assertEqual(
                    client.post("/v1/chat/completions", headers=self._headers(), json=body).status_code,
                    200,
                )

            explicit = {
                "model": "qwen",
                "messages": [{"role": "user", "content": "direct"}],
            }
            self.assertEqual(
                client.post("/v1/chat/completions", headers=self._headers(), json=explicit).status_code,
                200,
            )

        self.assertEqual(RouterAwareAsyncClient.judge_calls, 1)
        self.assertEqual(RouterAwareAsyncClient.backend_calls, 4)

    def test_unknown_auto_interval_is_rejected(self):
        client = self._client()
        response = client.post(
            "/v1/chat/completions",
            headers=self._headers(),
            json={"model": "auto:99", "messages": [{"role": "user", "content": "hello"}]},
        )
        self.assertEqual(response.status_code, 404)


class SessionRouteCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_singleflight_and_ttl(self):
        now = [100.0]
        config = SessionRoutingConfig(enabled=True, ttl_seconds=10, max_entries=10)
        cache = SessionRouteCache(config, clock=lambda: now[0])
        calls = 0

        async def selector():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return "glm"

        policy = AutoPolicy("auto:once", 0)
        results = await asyncio.gather(
            *[
                cache.get_or_select(
                    "session",
                    user_turns=1,
                    policy=policy,
                    modality="text",
                    allowed_models=["glm"],
                    selector=selector,
                )
                for _ in range(5)
            ]
        )
        self.assertEqual(calls, 1)
        self.assertEqual(sum(1 for _, hit in results if not hit), 1)

        now[0] = 111.0
        await cache.get_or_select(
            "session",
            user_turns=1,
            policy=policy,
            modality="text",
            allowed_models=["glm"],
            selector=selector,
        )
        self.assertEqual(calls, 2)

    def test_fallback_session_key_stays_stable_across_tool_loops(self):
        first = [{"role": "system", "content": "You are a coder"}, {"role": "user", "content": "Build it"}]
        tool_loop = first + [
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
            {"role": "tool", "content": "result"},
        ]
        self.assertEqual(
            derive_session_key(first, user=None, header_value=None, fallback_hash_chars=4096),
            derive_session_key(tool_loop, user=None, header_value=None, fallback_hash_chars=4096),
        )


if __name__ == "__main__":
    unittest.main()
