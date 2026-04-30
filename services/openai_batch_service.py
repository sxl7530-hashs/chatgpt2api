from __future__ import annotations

import base64
import json
from typing import Any

from curl_cffi import requests
from fastapi import HTTPException

from services.config import config
from services.protocol.conversation import save_image_bytes
from services.proxy_service import proxy_settings
from utils.helper import ensure_ok


OPENAI_API_BASE_URL = "https://api.openai.com/v1"


def _require_openai_api_key() -> str:
    api_key = config.openai_api_key
    if not api_key:
        raise HTTPException(status_code=503, detail={"error": "openai api key is not configured"})
    return api_key


def _session() -> requests.Session:
    api_key = _require_openai_api_key()
    session = requests.Session(**proxy_settings.build_session_kwargs(impersonate="edge101", verify=True))
    session.headers.update({
        "Authorization": f"Bearer {api_key}",
    })
    return session


def _normalize_batch_item(item: dict[str, Any], index: int) -> dict[str, Any]:
    prompt = str(item.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail={"error": f"items[{index}].prompt is required"})
    model = str(item.get("model") or "gpt-image-2").strip() or "gpt-image-2"
    custom_id = str(item.get("custom_id") or f"image-{index + 1}").strip() or f"image-{index + 1}"
    n = int(item.get("n") or 1)
    if n < 1:
        raise HTTPException(status_code=400, detail={"error": f"items[{index}].n must be >= 1"})
    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "n": n,
    }
    size = item.get("size")
    if size not in (None, ""):
        body["size"] = size
    quality = item.get("quality")
    if quality not in (None, ""):
        body["quality"] = quality
    background = item.get("background")
    if background not in (None, ""):
        body["background"] = background
    output_format = item.get("output_format")
    if output_format not in (None, ""):
        body["output_format"] = output_format
    output_compression = item.get("output_compression")
    if output_compression not in (None, ""):
        body["output_compression"] = output_compression
    moderation = item.get("moderation")
    if moderation not in (None, ""):
        body["moderation"] = moderation
    user = item.get("user")
    if user not in (None, ""):
        body["user"] = user
    return {
        "custom_id": custom_id,
        "method": "POST",
        "url": "/v1/images/generations",
        "body": body,
    }


def _build_batch_jsonl(items: list[dict[str, Any]]) -> bytes:
    if not items:
        raise HTTPException(status_code=400, detail={"error": "items is required"})
    lines = [
        json.dumps(_normalize_batch_item(item, index), ensure_ascii=False, separators=(",", ":"))
        for index, item in enumerate(items)
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _upload_batch_file(session: requests.Session, content: bytes) -> dict[str, Any]:
    response = session.post(
        f"{OPENAI_API_BASE_URL}/files",
        files={"file": ("image_batch.jsonl", content, "application/jsonl")},
        data={"purpose": "batch"},
        timeout=120,
    )
    ensure_ok(response, "openai file upload")
    data = response.json()
    return data if isinstance(data, dict) else {}


def create_image_batch(items: list[dict[str, Any]], metadata: dict[str, str] | None = None) -> dict[str, Any]:
    jsonl_content = _build_batch_jsonl(items)
    session = _session()
    try:
        file_data = _upload_batch_file(session, jsonl_content)
        input_file_id = str(file_data.get("id") or "").strip()
        if not input_file_id:
            raise RuntimeError("openai file upload returned empty file id")
        payload: dict[str, Any] = {
            "input_file_id": input_file_id,
            "endpoint": "/v1/images/generations",
            "completion_window": "24h",
        }
        if metadata:
            payload["metadata"] = metadata
        response = session.post(
            f"{OPENAI_API_BASE_URL}/batches",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=120,
        )
        ensure_ok(response, "openai batch create")
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("openai batch create returned invalid response")
        data["input_file"] = {"id": input_file_id}
        return data
    finally:
        session.close()


def get_batch(batch_id: str) -> dict[str, Any]:
    session = _session()
    try:
        response = session.get(f"{OPENAI_API_BASE_URL}/batches/{batch_id}", timeout=60)
        ensure_ok(response, "openai batch get")
        data = response.json()
        return data if isinstance(data, dict) else {}
    finally:
        session.close()


def get_file_content(file_id: str) -> str:
    session = _session()
    try:
        response = session.get(f"{OPENAI_API_BASE_URL}/files/{file_id}/content", timeout=120)
        ensure_ok(response, "openai file content")
        return response.text
    finally:
        session.close()


def parse_batch_output(content: str, base_url: str | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for line in content.splitlines():
        raw = line.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except Exception:
            items.append({"status": "error", "error": {"message": "invalid jsonl line"}, "raw": raw})
            continue
        if not isinstance(record, dict):
            items.append({"status": "error", "error": {"message": "invalid result line"}, "raw": raw})
            continue
        custom_id = str(record.get("custom_id") or "")
        error = record.get("error")
        response = record.get("response") if isinstance(record.get("response"), dict) else {}
        status_code = int(response.get("status_code") or 0) if response.get("status_code") is not None else 0
        body = response.get("body") if isinstance(response.get("body"), dict) else {}
        if error:
            items.append({
                "custom_id": custom_id,
                "status": "error",
                "error": error,
            })
            continue
        if status_code and status_code >= 400:
            items.append({
                "custom_id": custom_id,
                "status": "error",
                "status_code": status_code,
                "error": body.get("error") if isinstance(body.get("error"), dict) else {"message": "request failed"},
            })
            continue
        items.append({
            "custom_id": custom_id,
            "status": "success",
            "response": body,
            "data": body.get("data") if isinstance(body.get("data"), list) else [],
            "usage": body.get("usage"),
            "created": body.get("created"),
        })
        current = items[-1]
        data = current["data"] if isinstance(current.get("data"), list) else []
        normalized_data: list[dict[str, Any]] = []
        for image in data:
            if not isinstance(image, dict):
                continue
            normalized = dict(image)
            b64_json = str(image.get("b64_json") or "").strip()
            if b64_json:
                try:
                    normalized["url"] = save_image_bytes(base64.b64decode(b64_json), base_url)
                except Exception:
                    pass
            normalized_data.append(normalized)
        current["data"] = normalized_data
    return items


def get_batch_result(batch_id: str, base_url: str | None = None) -> dict[str, Any]:
    batch = get_batch(batch_id)
    status = str(batch.get("status") or "")
    output_file_id = str(batch.get("output_file_id") or "").strip()
    error_file_id = str(batch.get("error_file_id") or "").strip()
    result: dict[str, Any] = {
        "id": batch.get("id") or batch_id,
        "status": status,
        "request_counts": batch.get("request_counts"),
        "output_file_id": output_file_id or None,
        "error_file_id": error_file_id or None,
    }
    if status != "completed":
        return result
    if output_file_id:
        result["items"] = parse_batch_output(get_file_content(output_file_id), base_url)
    else:
        result["items"] = []
    if error_file_id:
        result["error_items"] = parse_batch_output(get_file_content(error_file_id), base_url)
    return result
