# proxy_server.py
"""
Anthropic-compatible proxy for Claude Code CLI/SDK.

Endpoints:
  - POST /v1/messages  (supports SSE streaming and non-streaming)
  - GET  /v1/models    (minimal)
  - GET  /health

Key features:
  • Full /v1/messages compatibility for Claude Code.  (streaming SSE, non-stream)       [README Features]
  • Tool coverage: Anthropic tool_use/tool_result <-> OpenAI tool_calls/tool roles.     [Function Calling]
  • Base64 image input (Anthropic 'image' blocks -> OpenAI image_url).                  [Image Support]
  • Model routing including haiku/sonnet/opus → small/middle/big mappings.              [Model Mapping]
  • Multiple OpenAI-compatible providers: openai, azure, openrouter, ollama.
  • Robust error & usage mapping; simple, resilient streaming parser.

Routing & security (your design):
  • iOS chooses a token id per "route" and sends it to the broker.
  • Broker calls register_route(token, UpstreamConfig(...)).
  • Claude CLI is spawned with:
        ANTHROPIC_BASE_URL = http://127.0.0.1:<proxy_port>
        ANTHROPIC_API_KEY  = <that token>
  • This proxy looks up the token from Authorization: Bearer <token>.

Notes:
  • We never log secrets (API keys masked).
  • JSON schema sanitizer removes 'format' keys for better provider compatibility.
  • Azure OpenAI supported both by explicit 'azure' provider AND by using a base_url that
    already points to the deployment path (works for many setups).

"""

from __future__ import annotations
import asyncio
import base64
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import web, ClientSession, ClientTimeout, ClientResponse

# =============================== Route Registry ===============================

@dataclass
class ModelConfig:
    """Configuration for a specific model size (small/medium/big)."""
    provider: str = "openai"             # "openai" | "azure" | "openrouter" | "ollama" | "anthropic" | "custom"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""                    # upstream secret
    model: str = "gpt-4o"                # target model to use at this provider
    extra_headers: Dict[str, str] = field(default_factory=dict)

    # Azure specifics (used if provider == "azure")
    azure_deployment: Optional[str] = None
    azure_api_version: Optional[str] = None
    
    # Authentication method (used if provider == "anthropic")
    auth_method: Optional[str] = None    # "oauth" | "api_key" | None (defaults to api_key)

@dataclass
class UpstreamConfig:
    """Route configuration with per-model-size provider settings."""
    small: Optional[ModelConfig] = None   # haiku models
    medium: Optional[ModelConfig] = None  # sonnet models
    big: Optional[ModelConfig] = None     # opus models

# In-memory: token -> config (broker populates this directly)
ROUTES: Dict[str, UpstreamConfig] = {}

def register_route(token: str, cfg: UpstreamConfig) -> None:
    """Broker calls this to register/replace a per-session upstream route."""
    ROUTES[token] = cfg

def get_route(token: str) -> Optional[UpstreamConfig]:
    return ROUTES.get(token)

def unregister_route(token: str) -> None:
    ROUTES.pop(token, None)

def clear_routes() -> None:
    ROUTES.clear()

# =============================== Helpers/Utilities ============================

def _mask_secret(s: Optional[str]) -> str:
    if not s:
        return ""
    return s[:4] + "..." + s[-4:] if len(s) > 8 else "****"

def get_model_size(anthropic_model: str) -> str:
    """
    Determine model size category from Anthropic model name.
    Returns: "small" | "medium" | "big"
    """
    m = (anthropic_model or "").lower()
    if "haiku" in m:  return "small"
    if "sonnet" in m: return "medium"
    if "opus" in m:   return "big"
    # default to medium
    return "medium"

def _default_model_map(anthropic_model: str) -> str:
    """
    Fallback mapping for common Claude families → reasonable OpenAI defaults.
    Mirrors the common 'haiku/sonnet/opus' → SMALL/MIDDLE/BIG mapping.
    """
    m = (anthropic_model or "").lower()
    if "haiku" in m:  return "gpt-4o-mini"
    if "sonnet" in m: return "gpt-4o"
    if "opus" in m:   return "gpt-4o"
    # safer default
    return "gpt-4o"

def _sanitize_json_schema(schema: Any) -> Any:
    """
    Remove/relax schema keys that frequently cause provider incompatibilities.
    - Strips 'format' (e.g., 'uri', 'email', etc.) across the schema tree.
    - Keeps the rest of the structure intact.
    """
    if isinstance(schema, dict):
        cleaned = {}
        for k, v in schema.items():
            if k == "format":
                # drop
                continue
            cleaned[k] = _sanitize_json_schema(v)
        return cleaned
    if isinstance(schema, list):
        return [_sanitize_json_schema(x) for x in schema]
    return schema

def _anthropic_tools_to_openai(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Anthropic tools: [{ name, description, input_schema }]
    OpenAI tools:   [{ type:"function", function:{ name, description, parameters } }]
    """
    out = []
    for t in tools or []:
        name = t.get("name")
        if not name:
            continue
        desc = t.get("description", "")
        params = t.get("input_schema", {"type": "object", "properties": {}})
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": desc,
                "parameters": _sanitize_json_schema(params),
            }
        })
    return out

def _anthropic_tool_choice_to_openai(choice: Any) -> Any:
    """
    Anthropic tool_choice:
      - "auto" | "any" | "none"
      - {"type":"tool","name":"..."}  (force a specific tool)
    OpenAI tool_choice:
      - "auto" | "none" | {"type":"function", "function":{"name":"..."}}
    """
    if choice in (None, "auto", "any"):
        return "auto"
    if choice == "none":
        return "none"
    if isinstance(choice, dict):
        name = choice.get("name")
        if name:
            return {"type": "function", "function": {"name": name}}
    return "auto"

def _is_base64_src(block: Dict[str, Any]) -> bool:
    src = block.get("source", {})
    return src.get("type") == "base64" and bool(src.get("media_type")) and bool(src.get("data"))

def _anthropic_system_to_openai(system: Any) -> Optional[Dict[str, Any]]:
    """Map Anthropic 'system' (string or text blocks) to an OpenAI system message."""
    if system is None:
        return None
    if isinstance(system, str):
        return {"role": "system", "content": system}
    if isinstance(system, list):
        texts = []
        for c in system:
            if isinstance(c, dict) and c.get("type") == "text":
                texts.append(c.get("text", ""))
        if texts:
            return {"role": "system", "content": "\n".join(texts)}
    return None

def _anthropic_messages_to_openai(messages: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """
    Convert Anthropic messages into OpenAI chat.completions messages.
    Returns:
      - openai_messages
      - tool_id_name_map (tool_use.id -> tool name)
    """
    out: List[Dict[str, Any]] = []
    tool_id_name: Dict[str, str] = {}

    for m in messages or []:
        role = m.get("role")
        content = m.get("content", [])

        if role == "user":
            # Split user text/images vs tool_result blocks.
            user_parts: List[Dict[str, Any]] = []
            if isinstance(content, str):
                user_parts.append({"type": "text", "text": content})
            else:
                for c in content:
                    ctype = c.get("type")
                    if ctype == "text":
                        user_parts.append({"type": "text", "text": c.get("text", "")})
                    elif ctype == "image" and _is_base64_src(c):
                        src = c["source"]
                        data_url = f"data:{src['media_type']};base64,{src['data']}"
                        user_parts.append({"type": "image_url", "image_url": {"url": data_url}})
                    elif ctype == "tool_result":
                        # Map to OpenAI role="tool"
                        tool_use_id = c.get("tool_use_id") or c.get("id") or f"tool_{uuid.uuid4().hex[:8]}"
                        result_content = c.get("content")
                        # collapse array-of-text blocks to a string
                        if isinstance(result_content, list):
                            texts = []
                            for it in result_content:
                                if isinstance(it, dict) and it.get("type") == "text":
                                    texts.append(it.get("text", ""))
                            result_content = "\n".join(texts)
                        if result_content is None:
                            result_content = ""
                        # include error flag if present (OpenAI has no native error field on tool role)
                        if c.get("is_error"):
                            result_content = json.dumps({"error": True, "content": result_content}, ensure_ascii=False)
                        out.append({
                            "role": "tool",
                            "tool_call_id": tool_use_id,
                            "content": str(result_content),
                        })
            if user_parts:
                out.append({"role": "user", "content": user_parts})

        elif role == "assistant":
            # text -> content string; tool_use -> tool_calls
            text_acc: List[str] = []
            tool_calls: List[Dict[str, Any]] = []
            if isinstance(content, str):
                text_acc.append(content)
            else:
                for c in content:
                    ctype = c.get("type")
                    if ctype == "text":
                        text_acc.append(c.get("text", ""))
                    elif ctype == "tool_use":
                        tname = c.get("name") or "function"
                        tid = c.get("id") or f"tool_{uuid.uuid4().hex[:8]}"
                        tinput = c.get("input", {})
                        tool_id_name[tid] = tname
                        tool_calls.append({
                            "id": tid,
                            "type": "function",
                            "function": {
                                "name": tname,
                                "arguments": json.dumps(tinput, ensure_ascii=False),
                            }
                        })
            msg: Dict[str, Any] = {"role": "assistant", "content": "".join(text_acc)}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)

        elif role == "tool":
            out.append({
                "role": "tool",
                "tool_call_id": m.get("tool_call_id", f"tool_{uuid.uuid4().hex[:8]}"),
                "content": str(m.get("content", "")),
            })

        elif role == "system":
            out.append({"role": "system", "content": str(m.get("content", ""))})

        # else: ignore unknown roles

    return out, tool_id_name

def _map_anthropic_to_chatgpt_backend(body: Dict[str, Any], model: str) -> Dict[str, Any]:
    """
    Build ChatGPT backend API payload from an Anthropic /v1/messages request body.
    Used for OAuth authentication with ChatGPT.
    """
    input_messages = []
    
    # Add system message if present
    if body.get("system"):
        input_messages.append({
            "type": "message",
            "role": "system",
            "content": [{"type": "input_text", "text": str(body["system"])}]
        })
    
    # Convert messages to input format
    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        
        if isinstance(content, str):
            input_messages.append({
                "type": "message",
                "role": role,
                "content": [{"type": "input_text", "text": content}]
            })
        elif isinstance(content, list):
            content_items = []
            for item in content:
                if item.get("type") == "text":
                    content_items.append({"type": "input_text", "text": item.get("text", "")})
                # Add other content types as needed
            input_messages.append({
                "type": "message",
                "role": role,
                "content": content_items
            })
    
    result = {
        "input": input_messages,
        "stream": True,  # Always true for OAuth
        "model": model
    }
    
    # Handle GPT-5 reasoning effort levels
    if "gpt-5" in model.lower():
        base_model = "gpt-5"
        result["model"] = base_model
        
        if "minimal" in model.lower():
            result["reasoning"] = {"effort": "minimal"}
        elif "low" in model.lower():
            result["reasoning"] = {"effort": "low"}
        elif "medium" in model.lower():
            result["reasoning"] = {"effort": "medium"}
        elif "high" in model.lower():
            result["reasoning"] = {"effort": "high"}
    
    return result

def _map_anthropic_request_to_openai(body: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """
    Build OpenAI chat.completions payload from an Anthropic /v1/messages request body.
    """
    sys_msg = _anthropic_system_to_openai(body.get("system"))
    messages, tool_id_map = _anthropic_messages_to_openai(body.get("messages", []))
    if sys_msg:
        messages.insert(0, sys_msg)

    oai: Dict[str, Any] = {"messages": messages}

    # streaming
    if body.get("stream") is not None:
        oai["stream"] = bool(body["stream"])
    else:
        oai["stream"] = False

    # temperature / top_p
    if "temperature" in body:
        oai["temperature"] = body["temperature"]
    if "top_p" in body and body["top_p"] is not None:
        oai["top_p"] = body["top_p"]

    # stop sequences -> "stop"
    if "stop_sequences" in body and body["stop_sequences"]:
        stops = body["stop_sequences"]
        oai["stop"] = stops if isinstance(stops, list) else [stops]

    # max_tokens
    if isinstance(body.get("max_tokens"), int):
        oai["max_tokens"] = body["max_tokens"]

    # tools & tool_choice
    if body.get("tools"):
        oai["tools"] = _anthropic_tools_to_openai(body["tools"])
    if "tool_choice" in body:
        oai["tool_choice"] = _anthropic_tool_choice_to_openai(body["tool_choice"])

    # response_format (json mode)
    rf = body.get("response_format")
    if isinstance(rf, dict) and rf.get("type") == "json_object":
        oai["response_format"] = {"type": "json_object"}
    elif rf == "json":
        oai["response_format"] = {"type": "json_object"}

    return oai, tool_id_map

# =========================== Provider URL & Headers ==========================

def _build_upstream_url_and_headers(cfg: ModelConfig) -> Tuple[str, Dict[str, str]]:
    """
    Prepare the POST URL + headers to call the upstream provider.
    """
    provider = (cfg.provider or "openai").lower()

    if provider == "anthropic":
        # Native Anthropic API
        base = cfg.base_url.rstrip("/") if cfg.base_url else "https://api.anthropic.com"
        url = f"{base}/v1/messages"  # Anthropic uses /v1/messages
        
        # Check authentication method
        if cfg.auth_method == "oauth":
            # OAuth authentication (Bearer token with special headers)
            headers = {
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
                "Anthropic-Beta": "claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14,fine-grained-tool-streaming-2025-05-14",
                "User-Agent": "claude-cli/1.0.83 (external, cli)",
                "X-App": "cli",
                "X-Stainless-Helper-Method": "stream",
                "X-Stainless-Lang": "js",
                "X-Stainless-Runtime": "node",
                "X-Stainless-Runtime-Version": "v24.3.0",
                "X-Stainless-Package-Version": "0.55.1",
                "Anthropic-Dangerous-Direct-Browser-Access": "true"
            }
            # Add ?beta=true to URL for OAuth
            url = f"{base}/v1/messages?beta=true"
        else:
            # Default to API key authentication (x-api-key header)
            headers = {
                "x-api-key": cfg.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json"
            }
        
        headers.update(cfg.extra_headers or {})
        return url, headers

    elif provider == "azure":
        # Two patterns supported:
        # 1) Use explicit deployment/api-version fields (recommended).
        # 2) If base_url already contains full deployment path, fall back to {base}/chat/completions.
        if cfg.azure_deployment and cfg.azure_api_version:
            base = cfg.base_url.rstrip("/")
            url = f"{base}/openai/deployments/{cfg.azure_deployment}/chat/completions?api-version={cfg.azure_api_version}"
        else:
            base = cfg.base_url.rstrip("/")
            url = f"{base}/chat/completions"
        headers = {"api-key": cfg.api_key, "Content-Type": "application/json"}
        headers.update(cfg.extra_headers or {})
        return url, headers

    # OpenAI / OpenRouter / Ollama (OpenAI-compatible)
    if provider == "openai" and cfg.auth_method == "oauth":
        # Use ChatGPT backend API endpoint for OAuth (Codex implementation)
        url = "https://chatgpt.com/backend-api/codex/responses"
        headers = {
            "Version": "0.21.0",
            "Content-Type": "application/json",
            "Openai-Beta": "responses=experimental",
            "Session_id": str(uuid.uuid4()),
            "Accept": "text/event-stream",
            "Originator": "codex_cli_rs",
            "Authorization": f"Bearer {cfg.api_key}"
        }
        # Add ChatGPT Account ID if provided
        if cfg.extra_headers and cfg.extra_headers.get("chatgpt_account_id"):
            headers["Chatgpt-Account-Id"] = cfg.extra_headers["chatgpt_account_id"]
    else:
        # Standard OpenAI API headers
        base = cfg.base_url.rstrip("/")
        url = f"{base}/chat/completions"
        headers = {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"}
    
    headers.update(cfg.extra_headers or {})
    return url, headers

# =============================== SSE Utilities ===============================

def _sse_event(event_type: str, data_obj: Dict[str, Any]) -> bytes:
    return f"event: {event_type}\ndata: {json.dumps(data_obj, ensure_ascii=False)}\n\n".encode("utf-8")

def _new_message_stub(model_id: str) -> Dict[str, Any]:
    return {
        "type": "message",
        "id": f"msg_{uuid.uuid4().hex}",
        "role": "assistant",
        "model": model_id,
        "stop_reason": None,
        "stop_sequence": None,
    }

async def _iter_openai_sse(resp: ClientResponse):
    """
    Minimal SSE reader for OpenAI stream payload (lines with 'data: {...}').
    Yields parsed JSON dicts for each data line; skips keepalives and non-data.
    """
    buffer = b""
    async for chunk in resp.content.iter_any():
        if not chunk:
            continue
        buffer += chunk
        # Split by newline; keep trailing partial in buffer
        *lines, buffer = buffer.split(b"\n")
        for raw in lines:
            line = raw.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                return
            try:
                yield json.loads(data.decode("utf-8", errors="ignore"))
            except Exception:
                # ignore malformed
                continue

async def _iter_anthropic_sse(resp: ClientResponse):
    """
    SSE reader for native Anthropic stream - just pass through the events.
    """
    buffer = b""
    async for chunk in resp.content.iter_any():
        if not chunk:
            continue
        buffer += chunk
        # Split by double newline (SSE event separator)
        while b"\n\n" in buffer:
            event, buffer = buffer.split(b"\n\n", 1)
            if event:
                yield event + b"\n\n"  # Include the separator for proper SSE format

# ================================ Handlers ===================================

async def handle_health(_req: web.Request) -> web.Response:
    return web.json_response({"ok": True})

async def handle_models(request: web.Request) -> web.Response:
    """
    Minimal model list; optionally could reflect the route's mapped models if authorized.
    """
    # If we want per-route reflection:
    # token = request.headers.get("Authorization","").replace("Bearer","").strip()
    # cfg = get_route(token)
    # ...
    return web.json_response({"data": [{"id": "claude-3-5-sonnet-latest", "type": "model"}]})

async def handle_messages(request: web.Request) -> web.StreamResponse:
    # --- extract route token from Authorization OR x-api-key ---
    auth = (request.headers.get("Authorization") or "").strip()
    x_api = (request.headers.get("x-api-key") or "").strip()

    token = ""
    if auth:
        parts = auth.split(None, 1)  # "Bearer <token>"
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()
    if not token and x_api:
        token = x_api  # Anthropic CLI/SDK uses x-api-key

    if not token:
        return web.Response(status=401, text="missing Authorization or x-api-key")

    route = get_route(token)
    if route is None:
        src = "Authorization" if auth else ("x-api-key" if x_api else "none")
        return web.Response(status=401, text=f"unknown route token ({src})")

    # Parse request
    try:
        body = await request.json()
    except Exception:
        return web.json_response({
            "type": "error",
            "error": {"type": "invalid_request_error", "message": "Invalid JSON body"}
        }, status=400)

    # Get the requested model and determine its size category
    requested_model = body.get("model", "")
    model_size = get_model_size(requested_model)

    # Select appropriate config based on model size
    if model_size == "small" and route.small:
        model_config = route.small
    elif model_size == "big" and route.big:
        model_config = route.big
    elif route.medium:
        model_config = route.medium
    else:
        # No config available for this model size
        return web.json_response({
            "type": "error",
            "error": {"type": "invalid_request_error",
                     "message": f"No provider configured for {model_size} models"}
        }, status=400)

    # Prepare upstream request based on provider
    is_anthropic = model_config.provider.lower() == "anthropic"
    tool_id_map = {}

    if is_anthropic:
        # Pass through native Anthropic format
        upstream_body = body.copy()
        upstream_body["model"] = model_config.model
    elif model_config.provider == "openai" and model_config.auth_method == "oauth":
        # Use ChatGPT backend API format for OAuth
        upstream_body = _map_anthropic_to_chatgpt_backend(body, model_config.model)
        # Note: ChatGPT backend doesn't use tool_id_map
    else:
        # Convert to OpenAI format for standard OpenAI-compatible providers
        upstream_body, tool_id_map = _map_anthropic_request_to_openai(body)
        upstream_body["model"] = model_config.model

    # Upstream URL + headers
    url, headers = _build_upstream_url_and_headers(model_config)
    timeout = ClientTimeout(total=float(os.getenv("REQUEST_TIMEOUT", "120")))

    # streaming?
    do_stream = bool(body.get("stream", False))

    async with ClientSession(timeout=timeout) as sess:
        try:
            if do_stream:
                # set up SSE response to Claude client
                resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
                await resp.prepare(request)

                msg_stub = _new_message_stub(requested_model or "claude-3-5-sonnet-latest")
                await resp.write(_sse_event("message_start", {"message": msg_stub}))

                tool_calls_accum: List[Dict[str, Any]] = []
                text_started = False
                finish_reason: Optional[str] = None

                async with sess.post(url, json=upstream_body, headers=headers) as r:
                    # Handle upstream non-200 with structured error
                    if r.status >= 400:
                        try:
                            errj = await r.json()
                        except Exception:
                            errj = {"message": await r.text()}
                        
                        # Log upstream errors for debugging
                        print(f"❌ UPSTREAM ERROR {r.status}:")
                        print(f"   Model: {model_config.model}")
                        print(f"   Provider: {model_config.provider}")
                        print(f"   URL: {url}")
                        print(f"   Auth Method: {getattr(model_config, 'auth_method', 'default')}")
                        print(f"   Error response: {json.dumps(errj, indent=2)[:1000]}")

                        # emit an SSE-style error event so the client can handle it
                        await resp.write(_sse_event("error", {
                            "type": "error",
                            "error": {"type": "api_error", "message": f"Upstream {r.status}: {errj}"}
                        }))
                        await resp.write(_sse_event("message_stop", {}))
                        await resp.write_eof()
                        return resp

                    if is_anthropic:
                        # Pass through native Anthropic SSE events
                        async for event in _iter_anthropic_sse(r):
                            await resp.write(event)
                    else:
                        # Convert OpenAI SSE to Anthropic format
                        async for chunk in _iter_openai_sse(r):
                            choice = (chunk.get("choices") or [{}])[0]
                            delta = choice.get("delta") or {}
                            finish_reason = finish_reason or choice.get("finish_reason")

                            # Text deltas
                            txt = delta.get("content") or delta.get("text")
                            if txt:
                                if not text_started:
                                    await resp.write(_sse_event("content_block_start", {"index": 0, "type": "output_text"}))
                                    text_started = True
                                await resp.write(_sse_event("content_block_delta", {
                                    "index": 0, "delta": {"type": "output_text_delta", "text": txt}
                                }))

                            # Tool calls deltas
                            tcd = delta.get("tool_calls")
                            if isinstance(tcd, list):
                                for tc in tcd:
                                    idx = tc.get("index", 0)
                                    while len(tool_calls_accum) <= idx:
                                        tool_calls_accum.append({"id": None, "name": None, "arguments": ""})
                                    entry = tool_calls_accum[idx]
                                    if tc.get("id"):
                                        entry["id"] = tc["id"]
                                    fn = tc.get("function") or {}
                                    if fn.get("name"):
                                        entry["name"] = fn["name"]
                                    if fn.get("arguments"):
                                        entry["arguments"] += fn["arguments"]

                        # Close text block if opened (only for OpenAI conversion)
                        if text_started:
                            await resp.write(_sse_event("content_block_stop", {"index": 0}))

                        # Emit finalized tool_use blocks (only for OpenAI conversion)
                        content_index = 1 if text_started else 0
                        for tc in tool_calls_accum:
                            args_json = {}
                            if tc["arguments"]:
                                try:
                                    args_json = json.loads(tc["arguments"])
                                except Exception:
                                    args_json = {"_raw": tc["arguments"]}
                            tool_id = tc["id"] or f"tool_{uuid.uuid4().hex[:8]}"
                            tool_name = tc["name"] or "function"
                            await resp.write(_sse_event("content_block_start", {
                                "index": content_index,
                                "type": "tool_use",
                                "id": tool_id,
                                "name": tool_name,
                                "input": args_json
                            }))
                            await resp.write(_sse_event("content_block_stop", {"index": content_index}))
                            content_index += 1

                        # Final stop (only for OpenAI conversion)
                        await resp.write(_sse_event("message_stop", {}))
                return resp

            else:
                # Non-streaming flow
                async with sess.post(url, json=upstream_body, headers=headers) as r:
                    if r.status >= 400:
                        try:
                            errj = await r.json()
                        except Exception:
                            errj = {"message": await r.text()}
                        
                        # Log upstream errors for debugging
                        print(f"❌ UPSTREAM ERROR {r.status}:")
                        print(f"   Model: {model_config.model}")
                        print(f"   Provider: {model_config.provider}")
                        print(f"   URL: {url}")
                        print(f"   Auth Method: {getattr(model_config, 'auth_method', 'default')}")
                        print(f"   Error response: {json.dumps(errj, indent=2)[:1000]}")
                        return web.json_response({
                            "type": "error",
                            "error": {"type": "api_error", "message": f"Upstream {r.status}: {errj}"}
                        }, status=502)

                    j = await r.json()

                    if is_anthropic:
                        # Pass through native Anthropic response
                        return web.json_response(j)

                    # Convert OpenAI response to Anthropic format
                    choice = (j.get("choices") or [{}])[0]
                    msg = choice.get("message") or {}
                    finish = choice.get("finish_reason")

                    # Text
                    text_content = msg.get("content")
                    if isinstance(text_content, list):
                        # Some providers might return list; collapse to text
                        merged = "".join(part.get("text", "") if isinstance(part, dict) else str(part)
                                         for part in text_content)
                        text = merged
                    else:
                        text = text_content or ""

                    content_blocks: List[Dict[str, Any]] = []
                    if text:
                        content_blocks.append({"type": "text", "text": text})

                    # Tool calls (non-stream)
                    tool_calls = msg.get("tool_calls") or []
                    for tc in tool_calls:
                        tool_id = tc.get("id") or f"tool_{uuid.uuid4().hex[:8]}"
                        fn = tc.get("function") or {}
                        tool_name = fn.get("name") or "function"
                        args = {}
                        if fn.get("arguments"):
                            try:
                                args = json.loads(fn["arguments"])
                            except Exception:
                                args = {"_raw": fn["arguments"]}
                        content_blocks.append({
                            "type": "tool_use",
                            "id": tool_id,
                            "name": tool_name,
                            "input": args
                        })

                    # Usage mapping
                    usage = j.get("usage") or {}
                    anthropic_usage = {
                        "input_tokens": usage.get("prompt_tokens"),
                        "output_tokens": usage.get("completion_tokens"),
                        "total_tokens": usage.get("total_tokens"),
                    }

                    # stop_reason mapping (best-effort)
                    if finish == "tool_calls":
                        stop_reason = "tool_use"
                    elif finish == "stop":
                        stop_reason = "end_turn"
                    else:
                        stop_reason = finish

                    return web.json_response({
                        "id": f"msg_{uuid.uuid4().hex}",
                        "type": "message",
                        "role": "assistant",
                        "model": requested_model or "claude-3-5-sonnet-latest",
                        "content": content_blocks,
                        "stop_reason": stop_reason,
                        "usage": anthropic_usage,
                    })

        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Log the full error details
            import traceback
            error_details = traceback.format_exc()
            print(f"❌ PROXY ERROR 502: {str(e)}")
            print(f"   Model: {model_config.model}")
            print(f"   Provider: {model_config.provider}")
            print(f"   URL: {url}")
            print(f"   Auth Method: {getattr(model_config, 'auth_method', 'default')}")
            print(f"   Full traceback:\n{error_details}")
            
            # Map to Anthropic error shape
            return web.json_response({
                "type": "error",
                "error": {"type": "api_error", "message": f"Upstream error: {str(e)}"}
            }, status=502)

# ============================== App Bootstrap ================================

def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_post("/v1/messages", handle_messages)
    return app

async def start_proxy(host: str = "127.0.0.1", port: int = 8082):
    """
    Start the proxy server (call once from your broker).
    """
    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner

if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8082"))
    loop = asyncio.get_event_loop()
    loop.run_until_complete(start_proxy(host, port))
    print(f"[proxy] listening on http://{host}:{port}")
    loop.run_forever()
