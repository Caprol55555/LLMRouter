from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from openclaw_router.config import (
    ControlCenterConfig,
    LLMConfig,
    OpenClawConfig,
    RouterConfig,
)
from openclaw_router.server import create_app


def test_websocket_stream_smoke_with_fake_backend(tmp_path: Path):
    config = OpenClawConfig(
        show_model_prefix=False,
        router=RouterConfig(strategy="random"),
        llms={
            "local": LLMConfig(
                name="local",
                provider="local",
                model_id="local-model",
                base_url="http://127.0.0.1:9/v1",
                auth_mode="none",
            )
        },
        control_center=ControlCenterConfig(enabled=False, data_dir=str(tmp_path)),
    )
    app = create_app(config=config)

    async def fake_call(
        _llm_name,
        _messages,
        _max_tokens=4096,
        _temperature=None,
        stream=False,
        **_kwargs,
    ):
        assert stream is True

        async def chunks():
            yield 'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            yield "data: [DONE]\n\n"

        return chunks()

    app.state.backend.call = fake_call
    with TestClient(app) as client:
        with client.websocket_connect("/v1/chat/ws") as websocket:
            websocket.send_json(
                {
                    "model": "auto",
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": True,
                }
            )
            first = websocket.receive_text()
            done = websocket.receive_text()

    first_payload = json.loads(first.removeprefix("data: ").strip())
    assert first_payload["choices"][0]["delta"]["content"] == "hello"
    assert "[DONE]" in done
