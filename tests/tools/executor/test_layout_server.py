from __future__ import annotations

import base64
import http.client
import json
import threading
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from babeldoc.tools.executor import layout_server
from babeldoc.tools.executor.layout_server import LayoutServer


class _Model:
    def predict(self, images, *, imgsz: int):
        assert len(images) == 1
        assert imgsz == 1024
        return [
            SimpleNamespace(
                boxes=[
                    SimpleNamespace(
                        xyxy=[1.0, 2.0, 3.0, 4.0],
                        conf=0.75,
                        cls=2,
                    )
                ],
                names={2: "title"},
            )
        ]


@pytest.fixture
def server():
    instance = LayoutServer(("127.0.0.1", 0), _Model())
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=2)


def _request(server: LayoutServer, method: str, path: str, **kwargs):
    connection = http.client.HTTPConnection(
        server.server_address[0],
        server.server_address[1],
        timeout=2,
    )
    connection.request(method, path, **kwargs)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, response.getheader("Content-Type"), body


def test_layout_health_and_rpc_doclayout8_inference(server: LayoutServer) -> None:
    status, content_type, body = _request(server, "GET", "/healthz")
    assert status == 200
    assert content_type == "application/json"
    assert json.loads(body) == {
        "schema_version": 1,
        "service": "gloss-babeldoc-layout",
        "status": "ok",
    }

    ok, encoded = cv2.imencode(
        ".jpg",
        np.zeros((8, 12, 3), dtype=np.uint8),
    )
    assert ok
    request = json.dumps(
        {
            "schema_version": 1,
            "image": base64.b64encode(encoded.tobytes()).decode(),
        }
    ).encode()
    status, content_type, body = _request(
        server,
        "POST",
        "/inference",
        body=request,
        headers={"Content-Type": "application/json"},
    )

    assert status == 200
    assert content_type == "application/json"
    assert json.loads(body) == {
        "schema_version": 1,
        "boxes": [
            {
                "box": [1.0, 2.0, 3.0, 4.0],
                "class_id": 2,
                "label": "title",
                "score": 0.75,
            }
        ],
    }


def test_layout_server_rejects_invalid_or_oversized_requests(
    server: LayoutServer,
) -> None:
    status, _content_type, body = _request(
        server,
        "POST",
        "/inference",
        body=b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert status == 400
    assert json.loads(body) == {"error": "invalid_request"}

    status, _content_type, body = _request(
        server,
        "POST",
        "/inference",
        body=b"",
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(layout_server.MAX_REQUEST_BYTES + 1),
        },
    )
    assert status == 400
    assert json.loads(body) == {"error": "invalid_request"}


def test_layout_server_restricts_host_parent_and_fake_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="127.0.0.1"):
        layout_server.serve(
            "0.0.0.0",  # noqa: S104 - the test verifies this is rejected
            0,
            parent_pid=1,
        )
    with pytest.raises(ValueError, match="parent identity"):
        layout_server.serve(
            "127.0.0.1",
            0,
            parent_pid=2**30,
        )
    monkeypatch.delenv(layout_server.ALLOW_FAKE_MODEL_ENV, raising=False)
    with pytest.raises(ValueError, match="fake layout model is disabled"):
        layout_server._load_model("fake")
