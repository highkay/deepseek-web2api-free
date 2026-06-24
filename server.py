"""
OpenAI-compatible API proxy for DeepSeek Chat
Supports streaming, tool calling (via DSML prompt injection), content parts, expert mode.
"""
import json
import os
import secrets
import time
import uuid
from typing import Optional, Union, Any

import uvicorn
from dotenv import load_dotenv
import os as _os
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from adapter import DeepSeekAdapter
from admin import router as admin_router, get_pool, get_stats
from tool_dsml import (
    parse_dsml_tool_calls,
    format_tool_calls_for_prompt,
    build_dsml_tool_prompt,
)
from tool_sieve import StreamSieve, SieveEvent
from anthropic_format import (
    AnthropicRequest,
    build_anthropic_prompt,
    build_nonstream_response,
    stream_response,
    _msg_id,
)

load_dotenv()

MODEL_NAME = os.environ.get("MODEL_NAME", "deepseek-chat")
MODE = os.environ.get("MODE", "auto").strip().lower()
THINKING = os.environ.get("THINKING", "auto").strip().lower()
SEARCH = os.environ.get("SEARCH", "auto").strip().lower()
PORT = int(os.environ.get("PORT", "8080"))
ALLOW_UNAUTHENTICATED_API = os.environ.get("ALLOW_UNAUTHENTICATED_API", "false").strip().lower() in {"1", "true", "yes", "on"}


def _load_api_keys() -> list[str]:
    keys = []
    raw_keys = os.environ.get("API_KEYS", "")
    for item in raw_keys.split(","):
        item = item.strip()
        if item:
            keys.append(item)
    single_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if single_key:
        keys.append(single_key)

    deduped = []
    seen = set()
    for key in keys:
        if key not in seen:
            seen.add(key)
            deduped.append(key)
    return deduped


API_KEYS = _load_api_keys()


def _extract_api_key(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def _check_api_auth(request: Request):
    if ALLOW_UNAUTHENTICATED_API:
        return
    if not API_KEYS:
        raise HTTPException(status_code=503, detail="API key authentication is not configured")
    supplied = _extract_api_key(request)
    if not supplied:
        raise HTTPException(status_code=401, detail="Missing API key")
    if not any(secrets.compare_digest(supplied, key) for key in API_KEYS):
        raise HTTPException(status_code=401, detail="Invalid API key")


app = FastAPI(title="DeepSeek Chat API (Expert Preview)", version="2.1.0-pre")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

pool = get_pool()

if pool.count() == 0:
    print("WARNING: No accounts in pool. Set DEEPSEEK_TOKEN and DEEPSEEK_COOKIES in .env")


# ---- Pydantic models ----

class ContentPart(BaseModel):
    type: str
    text: Optional[str] = None


class FunctionCall(BaseModel):
    name: str
    arguments: str  # JSON string


class ToolCall(BaseModel):
    id: str
    type: str = "function"
    function: FunctionCall


class ChatMessage(BaseModel):
    role: str
    content: Optional[Union[str, list[ContentPart]]] = None
    tool_calls: Optional[list[ToolCall]] = None
    tool_call_id: Optional[str] = None


class FunctionDef(BaseModel):
    name: str
    description: Optional[str] = ""
    parameters: Optional[dict] = None


class ToolDef(BaseModel):
    type: str = "function"
    function: FunctionDef


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = MODEL_NAME
    messages: list[ChatMessage]
    stream: Optional[bool] = False
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    tools: Optional[list[ToolDef]] = None
    tool_choice: Optional[Union[str, dict]] = None
    # Expert mode
    thinking_mode: Optional[bool] = False
    search_enabled: Optional[bool] = False


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int
    owned_by: str = "deepseek"


class ModelList(BaseModel):
    object: str = "list"
    data: list[ModelInfo]


# ---- Account pool helpers ----

class AcquiredAccount:
    """Context manager wrapping an acquired pool account."""
    def __init__(self, acct):
        self.acct = acct
        self.adapter = acct.adapter
        self._session_id: str | None = None

    def create_session(self) -> str:
        ds_id = self.adapter.create_session()
        self._session_id = ds_id
        return ds_id

    @property
    def session_id(self) -> str:
        if self._session_id is None:
            raise RuntimeError("No session created")
        return self._session_id

    def release(self):
        pool.release(self.acct)


def _acquire() -> AcquiredAccount:
    acct = pool.acquire()
    if acct is None:
        raise HTTPException(status_code=503, detail="All accounts busy, try again later")
    return AcquiredAccount(acct)


# ---- Message / prompt building ----

def _extract_text(content: Union[str, list[ContentPart], None]) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return " ".join(
        p.text for p in content if p.type == "text" and p.text
    )


def _build_prompt(messages: list[ChatMessage], tools: list[ToolDef] | None = None,
                  tool_choice: str | dict | None = None) -> str:
    """Build a prompt string from messages, injecting tool definitions if present."""
    parts = []

    tool_prompt_text = None
    if tools:
        tool_prompt_text = build_dsml_tool_prompt([t.model_dump() for t in tools], tool_choice)

    has_system_message = any(m.role == "system" for m in messages)
    tool_injected = False

    for m in messages:
        if m.role == "system":
            text = _extract_text(m.content)
            if tool_prompt_text and not tool_injected:
                text = text + "\n\n" + tool_prompt_text if text else tool_prompt_text
                tool_injected = True
            if text:
                parts.append(f"System: {text}")
        elif m.role == "user":
            parts.append(f"User: {_extract_text(m.content)}")
        elif m.role == "assistant":
            segs = []
            text = _extract_text(m.content)
            if text:
                segs.append(text)
            if m.tool_calls:
                dsml = format_tool_calls_for_prompt([tc.model_dump() for tc in m.tool_calls])
                if dsml:
                    segs.append(dsml)
            if segs:
                parts.append(f"Assistant: {' '.join(segs)}")
        elif m.role == "tool":
            result = _extract_text(m.content)
            prefix = f"Tool result (call_id={m.tool_call_id}):" if m.tool_call_id else "Tool result:"
            parts.append(f"{prefix} {result[:1000]}")

    if tool_prompt_text and not has_system_message:
        parts.insert(0, f"System: {tool_prompt_text}")

    return "\n".join(parts)


# ---- OpenAI SSE helpers ----

def _openai_chunk(proxy_id: str, content: str = "", finish: bool = False,
                  reasoning_content: str = None, role: str = None) -> str:
    delta = {}
    if not finish:
        if role:
            delta["role"] = role
        if reasoning_content is not None:
            delta["reasoning_content"] = reasoning_content
        elif content:
            delta["content"] = content
    choice = {
        "index": 0,
        "delta": delta,
        "finish_reason": "stop" if finish else None,
    }
    chunk = {
        "id": proxy_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_NAME,
        "choices": [choice],
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def _emit_tool_calls_chunks(tool_calls: list[dict], chat_id: str) -> list[str]:
    """生成 OpenAI 流式 tool_calls SSE 事件。"""
    chunks = []
    created = int(time.time())
    for i, tc in enumerate(tool_calls):
        # 首帧：id + name
        delta = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "index": i,
                "id": tc["id"],
                "type": "function",
                "function": {"name": tc["function"]["name"], "arguments": ""},
            }],
        }
        chunks.append(f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': MODEL_NAME, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]}, ensure_ascii=False)}\n\n")
        # 次帧：arguments
        delta2 = {
            "tool_calls": [{
                "index": i,
                "function": {"arguments": tc["function"]["arguments"]},
            }],
        }
        chunks.append(f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': MODEL_NAME, 'choices': [{'index': 0, 'delta': delta2, 'finish_reason': None}]}, ensure_ascii=False)}\n\n")
    # finish
    chunks.append(f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': MODEL_NAME, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]}, ensure_ascii=False)}\n\n")
    return chunks


# ---- Extract tool names ----

def _get_tool_names(tools: list[ToolDef] | None) -> list[str]:
    names = []
    for t in tools or []:
        if t.function and t.function.name:
            names.append(t.function.name)
    return names


# ---- Response parsing for non-streaming ----

def _parse_response_for_tools(text: str, tool_names: list[str]) -> tuple[list[dict] | None, str]:
    """Parse response text for DSML tool calls. Returns (tool_calls, cleaned_text)."""
    return parse_dsml_tool_calls(text, tool_names)


# ---- Endpoints ----

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models(request: Request):
    _check_api_auth(request)
    return ModelList(data=[
        ModelInfo(id=MODEL_NAME, created=int(time.time())),
    ])


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request):
    _check_api_auth(request)
    proxy_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    prompt = _build_prompt(req.messages, req.tools, req.tool_choice)

    # MODE controls model_type (quick → "default", expert → "expert")
    if MODE == "expert":
        model_type = "expert"
    else:
        model_type = "default"  # quick mode or auto, matches native request format

    # THINKING controls thinking_enabled independent of mode
    if THINKING == "enabled":
        thinking = True
    elif THINKING == "disabled":
        thinking = False
    else:
        thinking = req.thinking_mode or False

    # SEARCH controls search_enabled independently
    if SEARCH == "enabled":
        search = True
    elif SEARCH == "disabled":
        search = False
    else:
        search = req.search_enabled or False

    if req.stream:
        return await _handle_stream(proxy_id, prompt, req.tools, model_type=model_type, thinking_mode=thinking, search_enabled=search)

    return _handle_nonstream(proxy_id, prompt, req.tools, model_type=model_type, thinking_mode=thinking, search_enabled=search)


# ---- Anthropic /v1/messages endpoint ----


@app.post("/v1/messages")
async def messages(req: AnthropicRequest, request: Request):
    _check_api_auth(request)
    proxy_id = _msg_id()
    system_str = req.system
    if isinstance(system_str, list):
        system_str = " ".join(
            b.text for b in system_str if b.type == "text" and b.text
        )

    tools_dict = [t.model_dump() for t in req.tools] if req.tools else None
    prompt = build_anthropic_prompt(
        [m.model_dump() for m in req.messages],
        tools=tools_dict,
        system_str=system_str,
    )

    # MODE controls model_type
    if MODE == "expert":
        model_type = "expert"
    else:
        model_type = "default"

    # THINKING — Anthropic thinking param maps to thinking_mode
    if THINKING == "enabled":
        thinking = True
    elif THINKING == "disabled":
        thinking = False
    else:
        thinking = (req.thinking is not None and req.thinking.type == "enabled") or False

    # SEARCH
    if SEARCH == "enabled":
        search = True
    elif SEARCH == "disabled":
        search = False
    else:
        search = False

    tool_names = []
    if req.tools:
        for t in req.tools:
            if t.name:
                tool_names.append(t.name)

    if req.stream:
        return await _anthropic_stream(proxy_id, prompt, tool_names,
                                       model_type=model_type, thinking_mode=thinking,
                                       search_enabled=search)

    return _anthropic_nonstream(proxy_id, prompt, tool_names,
                                model_type=model_type, thinking_mode=thinking,
                                search_enabled=search)


def _anthropic_nonstream(msg_id: str, prompt: str, tool_names: list[str],
                         model_type: str | None = None,
                         thinking_mode: bool = False, search_enabled: bool = False):
    acq = _acquire()
    try:
        ds_id = acq.create_session()
        t0 = time.time()
        content = acq.adapter.chat(ds_id, prompt, model_type=model_type,
                                    thinking_enabled=thinking_mode, search_enabled=search_enabled)
        lat = (time.time() - t0) * 1000
        get_stats().record(MODEL_NAME, lat)
    except Exception as e:
        get_stats().record(MODEL_NAME, 0, success=False)
        pool.mark_error(acq.acct, str(e))
        raise
    finally:
        acq.release()

    tool_calls, cleaned = parse_dsml_tool_calls(content, tool_names)
    return build_nonstream_response(
        msg_id, MODEL_NAME,
        content_text=cleaned or content,
        tool_calls=tool_calls,
    )


async def _anthropic_stream(msg_id: str, prompt: str, tool_names: list[str],
                            model_type: str | None = None,
                            thinking_mode: bool = False, search_enabled: bool = False):
    acq = _acquire()
    try:
        ds_id = acq.create_session()
    except Exception as e:
        get_stats().record(MODEL_NAME, 0, success=False)
        pool.mark_error(acq.acct, str(e))
        acq.release()
        raise
    t0 = time.time()

    async def event_stream():
        nonlocal t0
        try:
            for event in stream_response(
                msg_id, MODEL_NAME,
                acq.adapter.chat_stream(ds_id, prompt,
                                       model_type=model_type,
                                       thinking_enabled=thinking_mode,
                                       search_enabled=search_enabled),
                tool_names,
                thinking_mode=thinking_mode,
            ):
                yield event
            get_stats().record(MODEL_NAME, (time.time() - t0) * 1000)
        except Exception as e:
            get_stats().record(MODEL_NAME, (time.time() - t0) * 1000, success=False)
            pool.mark_error(acq.acct, str(e))
            raise
        finally:
            acq.release()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


# ---- OpenAI handlers ----


def _handle_nonstream(proxy_id: str, prompt: str, tools: list[ToolDef] | None = None,
                      model_type: str | None = None,
                      thinking_mode: bool = False, search_enabled: bool = False):
    """Non-streaming completion with tool call detection."""
    acq = _acquire()
    try:
        ds_id = acq.create_session()
        t0 = time.time()
        content = acq.adapter.chat(ds_id, prompt, model_type=model_type,
                                    thinking_enabled=thinking_mode, search_enabled=search_enabled)
        get_stats().record(MODEL_NAME, (time.time() - t0) * 1000)
    except Exception as e:
        get_stats().record(MODEL_NAME, 0, success=False)
        pool.mark_error(acq.acct, str(e))
        raise
    finally:
        acq.release()

    tool_names = _get_tool_names(tools)
    tool_calls, cleaned = _parse_response_for_tools(content, tool_names)

    if tool_calls:
        return {
            "id": proxy_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": MODEL_NAME,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": tool_calls,
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": -1, "completion_tokens": -1, "total_tokens": -1},
        }

    return {
        "id": proxy_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_NAME,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": cleaned or content,
            },
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": -1, "completion_tokens": -1, "total_tokens": -1},
    }


async def _handle_stream(proxy_id: str, prompt: str, tools: list[ToolDef] | None = None,
                         model_type: str | None = None,
                         thinking_mode: bool = False, search_enabled: bool = False):
    """Streaming completion with StreamSieve tool call detection and expert mode support."""
    acq = _acquire()
    try:
        ds_id = acq.create_session()
    except Exception as e:
        get_stats().record(MODEL_NAME, 0, success=False)
        pool.mark_error(acq.acct, str(e))
        acq.release()
        raise
    tool_names = _get_tool_names(tools)
    t0 = time.time()

    async def event_stream():
        nonlocal t0
        try:
            yield _openai_chunk(proxy_id, finish=False)
            role_sent = False

            def _parse_fn(text):
                return parse_dsml_tool_calls(text, tool_names)

            sieve = StreamSieve(parse_fn=_parse_fn)
            full_buf = ""

            for token in acq.adapter.chat_stream(ds_id, prompt,
                                                  model_type=model_type,
                                                  thinking_enabled=thinking_mode,
                                                  search_enabled=search_enabled):
                if isinstance(token, dict):
                    tt = token.get("__type")
                    if tt == "status":
                        if token["status"] == "FINISHED":
                            break
                        continue
                    elif tt == "thinking":
                        content = token.get("content", "")
                        if content:
                            if not role_sent:
                                yield _openai_chunk(proxy_id, reasoning_content="", role="assistant")
                                role_sent = True
                            yield _openai_chunk(proxy_id, reasoning_content=content)
                        continue

                # Normal text token (str) — feed to sieve
                full_buf += token
                for evt in sieve.feed(token):
                    if evt.type == "text":
                        if evt.data:
                            if not role_sent:
                                if thinking_mode:
                                    yield _openai_chunk(proxy_id, reasoning_content="")
                                yield _openai_chunk(proxy_id, content=evt.data, role="assistant")
                                role_sent = True
                            else:
                                yield _openai_chunk(proxy_id, content=evt.data)
                    elif evt.type == "tool_calls":
                        for chunk in _emit_tool_calls_chunks(evt.data, proxy_id):
                            yield chunk
                        yield "data: [DONE]\n\n"
                        return

            # Flush sieve
            had_tool = False
            for evt in sieve.flush():
                if evt.type == "text" and evt.data:
                    if not role_sent:
                        if thinking_mode:
                            yield _openai_chunk(proxy_id, reasoning_content="")
                        yield _openai_chunk(proxy_id, content=evt.data, role="assistant")
                        role_sent = True
                    else:
                        yield _openai_chunk(proxy_id, content=evt.data)
                elif evt.type == "tool_calls":
                    had_tool = True
                    for chunk in _emit_tool_calls_chunks(evt.data, proxy_id):
                        yield chunk

            if had_tool:
                yield "data: [DONE]\n\n"
                return

            # Fallback: parse full buffer
            if not had_tool and full_buf:
                tc_result, _ = parse_dsml_tool_calls(full_buf, tool_names)
                if tc_result:
                    if not role_sent:
                        if thinking_mode:
                            yield _openai_chunk(proxy_id, reasoning_content="")
                        role_sent = True
                    for chunk in _emit_tool_calls_chunks(tc_result, proxy_id):
                        yield chunk
                    yield "data: [DONE]\n\n"
                    return

            yield _openai_chunk(proxy_id, finish=True)
            yield "data: [DONE]\n\n"
        except Exception as e:
            get_stats().record(MODEL_NAME, (time.time() - t0) * 1000, success=False)
            pool.mark_error(acq.acct, str(e))
            raise
        finally:
            acq.release()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


# ── Admin & static file serving ──────────────────────────────

_WEBUI_DIR = _os.path.realpath(_os.path.join(_os.path.dirname(__file__), "webui"))

app.include_router(admin_router)


def _safe_webui_path(rest_of_path: str) -> str | None:
    """Resolve a path inside the webui dir, refusing traversal.

    Returns the absolute file path if it points to a file under _WEBUI_DIR,
    otherwise None. Defends against `..\\..\\.env` style requests that would
    otherwise let unauthenticated callers read project secrets.
    """
    if not rest_of_path:
        return None
    # Reject obviously hostile inputs early.
    if "\x00" in rest_of_path:
        return None
    candidate = _os.path.realpath(_os.path.join(_WEBUI_DIR, rest_of_path))
    try:
        common = _os.path.commonpath([candidate, _WEBUI_DIR])
    except ValueError:
        # Different drives on Windows raise ValueError.
        return None
    if common != _WEBUI_DIR:
        return None
    if not _os.path.isfile(candidate):
        return None
    return candidate


if _os.path.isdir(_WEBUI_DIR):
    @app.get("/webui/{rest_of_path:path}")
    async def webui_spa(rest_of_path: str):
        safe = _safe_webui_path(rest_of_path)
        if safe is not None:
            return FileResponse(safe)
        index = _os.path.join(_WEBUI_DIR, "index.html")
        if _os.path.isfile(index):
            return FileResponse(index)
        return {"error": "webui not built"}

    @app.get("/webui")
    async def webui_root():
        index = _os.path.join(_WEBUI_DIR, "index.html")
        if _os.path.isfile(index):
            return FileResponse(index)
        return {"error": "webui not built"}

if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=PORT, reload=False)
