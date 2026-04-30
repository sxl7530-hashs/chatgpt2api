from __future__ import annotations

import concurrent.futures
import json
import os
import re
import threading
import time
import uuid

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
import requests

from api.support import require_identity, resolve_image_base_url
from services.log_service import LOG_TYPE_CALL, LoggedCall, log_service
from services.openai_batch_service import create_image_batch, get_batch, get_batch_result
from services.protocol import (
    anthropic_v1_messages,
    openai_v1_chat_complete,
    openai_v1_image_edit,
    openai_v1_image_generations,
    openai_v1_models,
    openai_v1_response,
)

PUBLIC_IMAGE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="public-image")
PUBLIC_IMAGE_TASKS: dict[str, dict[str, object]] = {}
PUBLIC_IMAGE_TASKS_LOCK = threading.Lock()
PUBLIC_IMAGE_MAX_TASKS = 100
PUBLIC_IMAGE_PARALLEL_PER_TASK = 8
IMGBB_API_KEY = os.getenv("IMGBB_API_KEY", "8f0e4821acad06f8ff38c340bfcf0aec")
IMGBB_EXPIRATION_SECONDS = 40 * 60


class ImageGenerationRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    model: str = "gpt-image-2"
    n: int = Field(default=1, ge=1, le=4)
    size: str | None = None
    response_format: str = "b64_json"
    history_disabled: bool = True
    stream: bool | None = None


class PublicImageGenerationRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    n: int = Field(default=1, ge=1, le=4)
    size: str | None = None
    response_format: str = "b64_json"


class OpenAIAsyncImageBatchItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    custom_id: str | None = None
    prompt: str = Field(..., min_length=1)
    model: str = "gpt-image-2"
    n: int = Field(default=1, ge=1)
    size: str | None = None
    quality: str | None = None
    background: str | None = None
    output_format: str | None = None
    output_compression: int | None = None
    moderation: str | None = None
    user: str | None = None


class OpenAIAsyncImageBatchRequest(BaseModel):
    items: list[OpenAIAsyncImageBatchItem] = Field(..., min_length=1)
    metadata: dict[str, str] | None = None


class ImageHostDeleteRequest(BaseModel):
    path: str = Field(..., min_length=1)
    token: str = Field(..., min_length=1)


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    prompt: str | None = None
    n: int | None = None
    stream: bool | None = None
    modalities: list[str] | None = None
    messages: list[dict[str, object]] | None = None


class ResponseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    input: object | None = None
    tools: list[dict[str, object]] | None = None
    tool_choice: object | None = None
    stream: bool | None = None


class AnthropicMessageRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    messages: list[dict[str, object]] | None = None
    system: object | None = None
    stream: bool | None = None


def _public_identity() -> dict[str, object]:
    return {"id": "public-gpt-page", "name": "公开生图页", "role": "user"}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _public_task_snapshot(task: dict[str, object]) -> dict[str, object]:
    snapshot = {
        "id": task.get("id"),
        "status": task.get("status"),
        "created_at_ms": task.get("created_at_ms"),
        "started_at_ms": task.get("started_at_ms"),
        "ended_at_ms": task.get("ended_at_ms"),
        "error": task.get("error"),
    }
    if task.get("status") == "success":
        snapshot["result"] = task.get("result")
    return snapshot


def _prune_public_tasks() -> None:
    if len(PUBLIC_IMAGE_TASKS) <= PUBLIC_IMAGE_MAX_TASKS:
        return
    removable = sorted(
        (
            task
            for task in PUBLIC_IMAGE_TASKS.values()
            if task.get("status") in {"success", "error"}
        ),
        key=lambda item: int(item.get("ended_at_ms") or item.get("created_at_ms") or 0),
    )
    for task in removable[: max(0, len(PUBLIC_IMAGE_TASKS) - PUBLIC_IMAGE_MAX_TASKS)]:
        task_id = str(task.get("id") or "")
        PUBLIC_IMAGE_TASKS.pop(task_id, None)


def _run_public_image_task(task_id: str, payload: dict[str, object], endpoint: str, summary: str, handler) -> None:
    call = LoggedCall(_public_identity(), endpoint, "gpt-image-2", summary)
    with PUBLIC_IMAGE_TASKS_LOCK:
        task = PUBLIC_IMAGE_TASKS.get(task_id)
        if not task:
            return
        task["status"] = "running"
        task["started_at_ms"] = _now_ms()
    try:
        result = _run_public_image_payload(handler, payload)
        call.log("调用完成", result)
        with PUBLIC_IMAGE_TASKS_LOCK:
            task = PUBLIC_IMAGE_TASKS.get(task_id)
            if task:
                task["status"] = "success"
                task["result"] = result
                task["ended_at_ms"] = _now_ms()
    except HTTPException as exc:
        call.log("调用失败", status="failed", error=str(exc.detail))
        with PUBLIC_IMAGE_TASKS_LOCK:
            task = PUBLIC_IMAGE_TASKS.get(task_id)
            if task:
                task["status"] = "error"
                task["error"] = exc.detail
                task["ended_at_ms"] = _now_ms()
    except Exception as exc:
        call.log("调用失败", status="failed", error=str(exc))
        with PUBLIC_IMAGE_TASKS_LOCK:
            task = PUBLIC_IMAGE_TASKS.get(task_id)
            if task:
                task["status"] = "error"
                task["error"] = str(exc)
                task["ended_at_ms"] = _now_ms()


def _run_single_public_image_payload(handler, payload: dict[str, object]) -> dict[str, object]:
    result = handler(payload)
    if not isinstance(result, dict):
        result = {"data": list(result)}
    return result


def _run_public_image_payload(handler, payload: dict[str, object]) -> dict[str, object]:
    n = int(payload.get("n") or 1)
    if n <= 1:
        return _run_single_public_image_payload(handler, payload)

    payloads = []
    for _ in range(n):
        item = dict(payload)
        item["n"] = 1
        payloads.append(item)

    data: list[object] = []
    messages: list[str] = []
    created = int(time.time())
    workers = min(n, PUBLIC_IMAGE_PARALLEL_PER_TASK)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="public-image-part") as executor:
        futures = [executor.submit(_run_single_public_image_payload, handler, item) for item in payloads]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            created = int(result.get("created") or created)
            result_data = result.get("data")
            if isinstance(result_data, list):
                data.extend(result_data)
            message = result.get("message")
            if isinstance(message, str) and message.strip():
                messages.append(message.strip())

    merged: dict[str, object] = {"created": created, "data": data}
    if not data and messages:
        merged["message"] = "\n".join(messages)
    return merged


def _submit_public_image_task(
        payload: dict[str, object],
        endpoint: str,
        summary: str,
        handler,
) -> dict[str, object]:
    task_id = uuid.uuid4().hex
    task = {
        "id": task_id,
        "status": "queued",
        "created_at_ms": _now_ms(),
        "started_at_ms": None,
        "ended_at_ms": None,
        "error": "",
        "result": None,
    }
    with PUBLIC_IMAGE_TASKS_LOCK:
        PUBLIC_IMAGE_TASKS[task_id] = task
        _prune_public_tasks()
    PUBLIC_IMAGE_EXECUTOR.submit(_run_public_image_task, task_id, payload, endpoint, summary, handler)
    return _public_task_snapshot(task)


def _read_uploads(uploads: list[UploadFile]) -> list[tuple[bytes, str, str]]:
    images: list[tuple[bytes, str, str]] = []
    for upload in uploads:
        image_data = upload.file.read()
        if not image_data:
            raise HTTPException(status_code=400, detail={"error": "image file is empty"})
        images.append((image_data, upload.filename or "image.png", upload.content_type or "image/png"))
    return images


def _clean_image_urls(values: list[str] | None) -> list[str]:
    urls: list[str] = []
    for value in values or []:
        url = str(value or "").strip()
        if url.startswith(("http://", "https://")):
            urls.append(url)
    return urls


def _extract_image_host_path(body: object) -> str:
    candidates: list[str] = []
    if isinstance(body, dict):
        for key in ("path", "url", "src", "data", "image", "filename", "name"):
            value = body.get(key)
            if isinstance(value, str):
                candidates.append(value)
            elif isinstance(value, dict):
                candidates.append(_extract_image_host_path(value))
    elif isinstance(body, str):
        candidates.append(body)
    for candidate in candidates:
        text = str(candidate or "").strip().strip('"')
        if not text:
            continue
        match = re.search(r"/?image/([^\"'\s]+)", text)
        if match:
            return match.group(1).lstrip("/")
        if "/" not in text and len(text) > 4:
            return text
    return ""


def _upload_image_host(image_data: bytes, filename: str, content_type: str, token: str) -> dict[str, object]:
    last_error = ""
    response = None
    for attempt in range(1, 3):
        session = None
        try:
            session = requests.Session()
            session.trust_env = False
            response = session.post(
                "https://api.imgbb.com/1/upload",
                params={"expiration": str(IMGBB_EXPIRATION_SECONDS), "key": IMGBB_API_KEY},
                files={"image": (filename, image_data, content_type)},
                headers={"User-Agent": "chatgpt2api-imgbb/1.0"},
                timeout=(10, 45),
            )
            break
        except Exception as exc:
            last_error = str(exc) or exc.__class__.__name__
            if attempt >= 2:
                log_service.add(LOG_TYPE_CALL, "图床上传失败", detail={"filename": filename, "content_type": content_type, "size": len(image_data), "error": last_error})
                raise HTTPException(status_code=502, detail={"error": f"image host upload failed: {last_error}"}) from exc
        finally:
            if session is not None:
                session.close()
    if response is None:
        raise HTTPException(status_code=502, detail={"error": f"image host upload failed: {last_error or 'unknown error'}"})
    raw_body = response.text
    try:
        body: object = response.json()
    except json.JSONDecodeError:
        body = raw_body
    if response.status_code >= 400 or not (isinstance(body, dict) and body.get("success")):
        log_service.add(LOG_TYPE_CALL, "图床上传失败", detail={"filename": filename, "content_type": content_type, "size": len(image_data), "status": response.status_code, "body": raw_body[:300]})
        raise HTTPException(status_code=502, detail={"error": f"imgbb upload failed: HTTP {response.status_code}", "body": raw_body[:300]})
    data = body.get("data") if isinstance(body, dict) else {}
    data = data if isinstance(data, dict) else {}
    image = data.get("image") if isinstance(data.get("image"), dict) else {}
    url = str(image.get("url") or data.get("url") or data.get("display_url") or "")
    delete_url = str(data.get("delete_url") or "")
    return {
        "token": token,
        "path": str(data.get("id") or ""),
        "url": url,
        "display_url": str(data.get("display_url") or url),
        "delete_url": delete_url,
        "expiration": int(data.get("expiration") or IMGBB_EXPIRATION_SECONDS),
        "raw": body,
    }


def _delete_image_host(path: str, token: str) -> dict[str, object]:
    delete_url = path.strip()
    if not delete_url.startswith(("http://", "https://")):
        return {"ok": False, "status": 400, "body": "imgbb delete requires delete_url"}
    response = requests.get(delete_url, timeout=30)
    return {"ok": response.status_code < 400, "status": response.status_code, "body": response.text[:300]}


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/v1/models")
    async def list_models(authorization: str | None = Header(default=None)):
        require_identity(authorization)
        try:
            return await run_in_threadpool(openai_v1_models.list_models)
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc

    @router.post("/v1/images/generations")
    async def generate_images(
            body: ImageGenerationRequest,
            request: Request,
            authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        payload = body.model_dump(mode="python")
        payload["base_url"] = resolve_image_base_url(request)
        call = LoggedCall(identity, "/v1/images/generations", body.model, "文生图")
        return await call.run(openai_v1_image_generations.handle, payload)

    @router.post("/api/openai/images/batches")
    async def create_openai_image_batch(
            body: OpenAIAsyncImageBatchRequest,
            authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        payload_items = [item.model_dump(mode="python") for item in body.items]
        metadata = body.metadata or {}
        call = LoggedCall(identity, "/api/openai/images/batches", "gpt-image-2", "OpenAI异步生图批任务")
        return await call.run(create_image_batch, payload_items, metadata)

    @router.get("/api/openai/images/batches/{batch_id}")
    async def get_openai_image_batch(
            batch_id: str,
            authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        call = LoggedCall(identity, f"/api/openai/images/batches/{batch_id}", "gpt-image-2", "OpenAI异步生图批任务状态")
        return await call.run(get_batch, batch_id)

    @router.get("/api/openai/images/batches/{batch_id}/result")
    async def get_openai_image_batch_result(
            batch_id: str,
            request: Request,
            authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        call = LoggedCall(identity, f"/api/openai/images/batches/{batch_id}/result", "gpt-image-2", "OpenAI异步生图批任务结果")
        return await call.run(get_batch_result, batch_id, resolve_image_base_url(request))

    @router.post("/api/public/images/generations")
    async def generate_public_images(body: PublicImageGenerationRequest, request: Request):
        payload = body.model_dump(mode="python")
        payload["model"] = "gpt-image-2"
        payload["response_format"] = "b64_json"
        payload["base_url"] = resolve_image_base_url(request)
        identity = _public_identity()
        call = LoggedCall(identity, "/api/public/images/generations", "gpt-image-2", "公开文生图")
        return await call.run(openai_v1_image_generations.handle, payload)

    @router.post("/api/public/images/generations/jobs")
    async def create_public_image_generation_job(body: PublicImageGenerationRequest, request: Request):
        payload = body.model_dump(mode="python")
        payload["model"] = "gpt-image-2"
        payload["response_format"] = "b64_json"
        payload["base_url"] = resolve_image_base_url(request)
        return _submit_public_image_task(
            payload,
            "/api/public/images/generations/jobs",
            "公开文生图任务",
            openai_v1_image_generations.handle,
        )

    @router.post("/api/public/images/edits")
    async def edit_public_images(
            request: Request,
            image: list[UploadFile] | None = File(default=None),
            image_list: list[UploadFile] | None = File(default=None, alias="image[]"),
            image_url: list[str] | None = Form(default=None),
            prompt: str = Form(...),
            n: int = Form(default=1),
            size: str | None = Form(default=None),
    ):
        if n < 1 or n > 4:
            raise HTTPException(status_code=400, detail={"error": "n must be between 1 and 4"})
        uploads = [*(image or []), *(image_list or [])]
        image_urls = _clean_image_urls(image_url)
        if not uploads and not image_urls:
            raise HTTPException(status_code=400, detail={"error": "image file is required"})
        log_service.add(
            LOG_TYPE_CALL,
            "公开图生图参考图",
            detail={
                "endpoint": "/api/public/images/edits",
                "image_url_count": len(image_urls),
                "file_count": len(uploads),
                "size": size,
                "n": n,
            },
        )
        images = [*image_urls, *(await run_in_threadpool(_read_uploads, uploads) if uploads else [])]
        payload = {
            "prompt": prompt,
            "images": images,
            "model": "gpt-image-2",
            "n": n,
            "size": size,
            "response_format": "b64_json",
            "base_url": resolve_image_base_url(request),
        }
        call = LoggedCall(_public_identity(), "/api/public/images/edits", "gpt-image-2", "公开图生图")
        return await call.run(openai_v1_image_edit.handle, payload)

    @router.post("/api/public/images/edits/jobs")
    async def create_public_image_edit_job(
            request: Request,
            image: list[UploadFile] | None = File(default=None),
            image_list: list[UploadFile] | None = File(default=None, alias="image[]"),
            image_url: list[str] | None = Form(default=None),
            prompt: str = Form(...),
            n: int = Form(default=1),
            size: str | None = Form(default=None),
    ):
        if n < 1 or n > 4:
            raise HTTPException(status_code=400, detail={"error": "n must be between 1 and 4"})
        uploads = [*(image or []), *(image_list or [])]
        image_urls = _clean_image_urls(image_url)
        if not uploads and not image_urls:
            raise HTTPException(status_code=400, detail={"error": "image file is required"})
        log_service.add(
            LOG_TYPE_CALL,
            "公开图生图任务参考图",
            detail={
                "endpoint": "/api/public/images/edits/jobs",
                "image_url_count": len(image_urls),
                "file_count": len(uploads),
                "size": size,
                "n": n,
            },
        )
        images = [*image_urls, *(await run_in_threadpool(_read_uploads, uploads) if uploads else [])]
        payload = {
            "prompt": prompt,
            "images": images,
            "model": "gpt-image-2",
            "n": n,
            "size": size,
            "response_format": "b64_json",
            "base_url": resolve_image_base_url(request),
        }
        return _submit_public_image_task(
            payload,
            "/api/public/images/edits/jobs",
            "公开图生图任务",
            openai_v1_image_edit.handle,
        )

    @router.get("/api/public/images/jobs/{task_id}")
    async def get_public_image_job(task_id: str):
        with PUBLIC_IMAGE_TASKS_LOCK:
            task = PUBLIC_IMAGE_TASKS.get(task_id)
            if not task:
                raise HTTPException(status_code=404, detail={"error": "task not found"})
            return _public_task_snapshot(task)

    @router.post("/api/public/image-host")
    async def upload_public_image_host(
            image: UploadFile = File(...),
            token: str = Form(default=""),
    ):
        image_data = await image.read()
        if not image_data:
            raise HTTPException(status_code=400, detail={"error": "image file is empty"})
        auth_token = token.strip() or uuid.uuid4().hex
        return await run_in_threadpool(
            _upload_image_host,
            image_data,
            image.filename or "image.png",
            image.content_type or "image/png",
            auth_token,
        )

    @router.delete("/api/public/image-host")
    async def delete_public_image_host(body: ImageHostDeleteRequest):
        return await run_in_threadpool(_delete_image_host, body.path, body.token)

    @router.post("/v1/images/edits")
    async def edit_images(
            request: Request,
            authorization: str | None = Header(default=None),
            image: list[UploadFile] | None = File(default=None),
            image_list: list[UploadFile] | None = File(default=None, alias="image[]"),
            prompt: str = Form(...),
            model: str = Form(default="gpt-image-2"),
            n: int = Form(default=1),
            size: str | None = Form(default=None),
            response_format: str = Form(default="b64_json"),
            stream: bool | None = Form(default=None),
    ):
        identity = require_identity(authorization)
        if n < 1 or n > 4:
            raise HTTPException(status_code=400, detail={"error": "n must be between 1 and 4"})
        uploads = [*(image or []), *(image_list or [])]
        if not uploads:
            raise HTTPException(status_code=400, detail={"error": "image file is required"})
        images: list[tuple[bytes, str, str]] = []
        for upload in uploads:
            image_data = await upload.read()
            if not image_data:
                raise HTTPException(status_code=400, detail={"error": "image file is empty"})
            images.append((image_data, upload.filename or "image.png", upload.content_type or "image/png"))
        payload = {
            "prompt": prompt,
            "images": images,
            "model": model,
            "n": n,
            "size": size,
            "response_format": response_format,
            "stream": stream,
            "base_url": resolve_image_base_url(request),
        }
        call = LoggedCall(identity, "/v1/images/edits", model, "图生图")
        return await call.run(openai_v1_image_edit.handle, payload)

    @router.post("/v1/chat/completions")
    async def create_chat_completion(body: ChatCompletionRequest, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        payload = body.model_dump(mode="python")
        model = str(payload.get("model") or "auto")
        call = LoggedCall(identity, "/v1/chat/completions", model, "文本生成")
        return await call.run(openai_v1_chat_complete.handle, payload)

    @router.post("/v1/responses")
    async def create_response(body: ResponseCreateRequest, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        payload = body.model_dump(mode="python")
        model = str(payload.get("model") or "auto")
        call = LoggedCall(identity, "/v1/responses", model, "Responses")
        return await call.run(openai_v1_response.handle, payload)

    @router.post("/v1/messages")
    async def create_message(
            body: AnthropicMessageRequest,
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="x-api-key"),
            anthropic_version: str | None = Header(default=None, alias="anthropic-version"),
    ):
        identity = require_identity(authorization or (f"Bearer {x_api_key}" if x_api_key else None))
        payload = body.model_dump(mode="python")
        model = str(payload.get("model") or "auto")
        call = LoggedCall(identity, "/v1/messages", model, "Messages")
        return await call.run(anthropic_v1_messages.handle, payload, sse="anthropic")

    return router
