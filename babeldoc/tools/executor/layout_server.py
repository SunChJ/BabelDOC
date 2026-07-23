from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import cv2
import msgpack
import numpy as np

logger = logging.getLogger(__name__)

READY_PREFIX = "__GLOSS_BABELDOC_LAYOUT_READY__"
MAX_REQUEST_BYTES = 64 * 1024 * 1024
PARENT_WATCHDOG_INTERVAL_SECONDS = 1.0
ALLOW_FAKE_MODEL_ENV = "BABELDOC_LAYOUT_ALLOW_FAKE"


class LayoutServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, model):
        super().__init__(server_address, LayoutHandler)
        self.model = model
        self.model_lock = threading.Lock()


class LayoutHandler(BaseHTTPRequestHandler):
    server: LayoutServer

    def do_GET(self) -> None:
        if urlparse(self.path).path != "/healthz":
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        self._write_json(
            HTTPStatus.OK,
            {
                "status": "ok",
                "service": "gloss-babeldoc-layout",
                "schema_version": 1,
            },
        )

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/inference":
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        try:
            raw, content_type = self._read_request()
            request = (
                json.loads(raw)
                if content_type == "application/json"
                else msgpack.unpackb(raw, raw=False)
            )
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            images = _decode_images(request, is_json=content_type == "application/json")
            image_size = request.get("imgsz", 1024)
            if (
                not isinstance(image_size, int)
                or isinstance(image_size, bool)
                or not 1 <= image_size <= 4096
            ):
                raise ValueError("imgsz must be an integer between 1 and 4096")
            with self.server.model_lock:
                results = self.server.model.predict(images, imgsz=image_size)
            if content_type == "application/json":
                if len(results) != 1:
                    raise ValueError("rpc_doclayout8 requires one image")
                payload = _rpc_v1_result_payload(results[0])
                self._write_json(HTTPStatus.OK, payload)
            else:
                self._write_bytes(
                    HTTPStatus.OK,
                    msgpack.packb(
                        [_legacy_result_payload(result) for result in results],
                        use_bin_type=True,
                    ),
                    "application/msgpack",
                )
        except Exception:
            logger.warning("layout inference request rejected", exc_info=True)
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})

    def _read_request(self) -> tuple[bytes, str]:
        if self.headers.get("Transfer-Encoding") is not None:
            raise ValueError("chunked requests are not accepted")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
        if content_type not in {"application/json", "application/msgpack"}:
            raise ValueError("unsupported content type")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if not 0 < length <= MAX_REQUEST_BYTES:
            raise ValueError("invalid content length")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("request body ended early")
        return raw, content_type

    def _write_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        self._write_bytes(
            status,
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(),
            "application/json",
        )

    def _write_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def serve(
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    parent_pid: int,
    model_name: str = "onnx",
) -> None:
    if host != "127.0.0.1":
        raise ValueError("layout service must bind to 127.0.0.1")
    if not 0 <= port <= 65_535:
        raise ValueError("port must be between 0 and 65535")
    if parent_pid <= 0 or os.getppid() != parent_pid:
        raise ValueError("layout service parent identity does not match")
    model = _load_model(model_name)
    server = LayoutServer((host, port), model)
    watchdog = threading.Thread(
        target=_monitor_parent,
        args=(server, parent_pid),
        name="layout-parent-watchdog",
        daemon=True,
    )
    watchdog.start()
    print(f"{READY_PREFIX}{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _load_model(model_name: str):
    if model_name == "fake":
        if os.environ.get(ALLOW_FAKE_MODEL_ENV) != "1":
            raise ValueError("fake layout model is disabled outside explicit tests")
        return _FakeModel()
    if model_name != "onnx":
        raise ValueError(f"unknown layout model: {model_name}")
    from babeldoc.docvision.doclayout import OnnxModel

    return OnnxModel.from_pretrained()


def _monitor_parent(server: LayoutServer, expected_parent_pid: int) -> None:
    while True:
        if os.getppid() != expected_parent_pid:
            server.shutdown()
            return
        time.sleep(PARENT_WATCHDOG_INTERVAL_SECONDS)


def _decode_images(request: dict[str, Any], *, is_json: bool) -> list[np.ndarray]:
    raw_images: object
    if is_json:
        encoded = request.get("image")
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("image is required")
        raw_images = [base64.b64decode(encoded, validate=True)]
    else:
        raw_images = request.get("image", [])
    if not isinstance(raw_images, list) or not raw_images:
        raise ValueError("image is required")

    images: list[np.ndarray] = []
    for encoded in raw_images:
        if not isinstance(encoded, bytes | bytearray):
            raise ValueError("encoded image must be bytes")
        image = cv2.imdecode(
            np.frombuffer(encoded, dtype=np.uint8),
            cv2.IMREAD_COLOR,
        )
        if image is None:
            raise ValueError("invalid image")
        images.append(image)
    return images


def _legacy_result_payload(result) -> dict[str, Any]:
    return {
        "boxes": [
            {
                "xyxy": [float(value) for value in box.xyxy],
                "conf": float(box.conf),
                "cls": int(box.cls),
            }
            for box in result.boxes
        ],
        "names": {str(key): str(value) for key, value in dict(result.names).items()},
    }


def _rpc_v1_result_payload(result) -> dict[str, Any]:
    converted = _legacy_result_payload(result)
    boxes = []
    for box in converted["boxes"]:
        class_id = int(box["cls"])
        boxes.append(
            {
                "class_id": class_id,
                "label": converted["names"].get(str(class_id), str(class_id)),
                "score": float(box["conf"]),
                "box": [float(value) for value in box["xyxy"]],
            }
        )
    return {"schema_version": 1, "boxes": boxes}


class _FakeModel:
    def predict(self, images, *, imgsz: int):
        del imgsz
        return [
            _FakeResult(
                boxes=[
                    _FakeBox(
                        xyxy=[0.0, 0.0, float(image.shape[1]), float(image.shape[0])],
                        conf=1.0,
                        cls=0,
                    )
                ],
                names={0: "text"},
            )
            for image in images
        ]


class _FakeBox:
    def __init__(self, *, xyxy, conf: float, cls: int):
        self.xyxy = xyxy
        self.conf = conf
        self.cls = cls


class _FakeResult:
    def __init__(self, *, boxes, names):
        self.boxes = boxes
        self.names = names
