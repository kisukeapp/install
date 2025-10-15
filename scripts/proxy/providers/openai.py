"""OpenAI-compatible provider executor."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import string
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionError

from .. import logging_control
from ..auth import resolve_auth_strategy
from ..errors import anthropic_error_payload, extract_error_details
from ..sse import iter_openai_sse, new_message_stub, sse_event
from .streaming_utils import emit_message_tail, map_stop_reason
from ..translators.anthropic import map_anthropic_request_to_openai
from ..translators.chatgpt_backend import map_anthropic_to_chatgpt_backend
from ..utils import mask_secret
from .base import ProviderExecutor


log = logging.getLogger(__name__)
DEBUG_ENABLED = bool(os.getenv("KISUKE_DEBUG"))


def _debug_log(message: str, *args) -> None:
    if not DEBUG_ENABLED:
        return
    log.info(message, *args)
    formatted = message % args if args else message
    print(f"[DEBUG] {formatted}")


def _build_openai_url(cfg) -> str:
    base = cfg.base_url.rstrip("/")
    if cfg.provider == "azure":
        if cfg.azure_deployment and cfg.azure_api_version:
            return f"{base}/openai/deployments/{cfg.azure_deployment}/chat/completions?api-version={cfg.azure_api_version}"
        return f"{base}/chat/completions"
    return f"{base}/chat/completions"


def _build_openai_headers(cfg, auth_headers: Dict[str, str]) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    headers.update(auth_headers)
    headers.update(cfg.extra_headers or {})
    return headers


_TOOL_ID_ALPHABET = string.ascii_letters + string.digits


def _generate_tool_id() -> str:
    return "toolu_" + "".join(secrets.choice(_TOOL_ID_ALPHABET) for _ in range(24))


async def _read_json(response) -> Dict[str, Any]:
    try:
        return await response.json()
    except Exception:
        try:
            text = await response.text()
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
            return {"message": text}
        except Exception:
            return {"message": "unknown upstream error"}


class OpenAIExecutor(ProviderExecutor):
    def __init__(self, cfg, request_body, requested_model, alt: Optional[str] = None):
        super().__init__(cfg, request_body, requested_model, alt=alt)
        self.tool_id_map: Dict[str, str] = {}
        self.tool_id_alias: Dict[str, str] = {}

    def _to_anthropic_tool_id(self, raw_id: Optional[str]) -> str:
        if raw_id and raw_id in self.tool_id_alias:
            return self.tool_id_alias[raw_id]
        generated = _generate_tool_id()
        if raw_id:
            self.tool_id_alias[raw_id] = generated
        return generated

    def _assign_tool_identity(
        self,
        state: Dict[str, Any],
        raw_id: Optional[str],
        default_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        if raw_id:
            state["openai_id"] = raw_id
            if raw_id in self.tool_id_alias:
                state["anth_id"] = self.tool_id_alias[raw_id]
            elif state.get("anth_id"):
                self.tool_id_alias[raw_id] = state["anth_id"]
            else:
                state["anth_id"] = self._to_anthropic_tool_id(raw_id)
        elif not state.get("anth_id"):
            state["anth_id"] = _generate_tool_id()

        if raw_id and raw_id in self.tool_id_map:
            state["name"] = self.tool_id_map[raw_id]
        if default_name:
            state["name"] = default_name
        if not state.get("name"):
            state["name"] = self.tool_id_map.get(raw_id) if raw_id else None
        if not state.get("name"):
            state["name"] = "function"
        if raw_id and raw_id not in self.tool_id_alias:
            self.tool_id_alias[raw_id] = state["anth_id"]
        return state

    async def execute(self, request: web.Request) -> web.StreamResponse:
        if self.cfg.auth_method == "oauth":
            executor: ProviderExecutor = ChatGPTExecutor(
                self.cfg,
                self.request_body,
                self.requested_model,
                alt=self.alt,
            )
            return await executor.execute(request)

        upstream_body, tool_map = map_anthropic_request_to_openai(self.request_body)
        self.tool_id_map = tool_map
        if self.cfg.model:
            upstream_body["model"] = self.cfg.model

        url = _build_openai_url(self.cfg)
        auth = resolve_auth_strategy(self.cfg.provider, self.cfg)
        headers = _build_openai_headers(self.cfg, auth.headers())

        self._log_upstream(url, headers, upstream_body)

        stream = bool(self.request_body.get("stream", False))

        try:
            async with self._client_session() as session:
                async with session.post(url, json=upstream_body, headers=headers) as upstream:
                    if logging_control.is_enabled():
                        print("\nUPSTREAM RESPONSE:")
                        print(f"   Status: {upstream.status}")
                        print(f"   Headers: {dict(upstream.headers)}")

                    if upstream.status >= 400:
                        return await self._handle_error(request, upstream, stream)

                    if stream:
                        return await self._stream_response(request, upstream)
                    return await self._non_stream_response(upstream)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if logging_control.is_enabled():
                print(f"OpenAI executor error: {exc}")
            return web.json_response(
                anthropic_error_payload(f"Upstream error: {exc}"),
                status=502,
            )

    async def _handle_error(self, request: web.Request, upstream, stream: bool) -> web.StreamResponse:
        error_body = await _read_json(upstream)
        error_type, message = extract_error_details(error_body)

        _debug_log(
            "upstream error status=%s body=%s",
            upstream.status,
            error_body,
        )

        if stream:
            resp = web.StreamResponse(status=upstream.status, headers={"Content-Type": "text/event-stream"})
            await resp.prepare(request)
            await resp.write(sse_event("error", anthropic_error_payload(message, error_type)))
            await resp.write(
                sse_event(
                    "message_stop",
                    {"type": "message_stop", "stop_reason": "error"},
                )
            )
            await resp.write_eof()
            return resp

        return web.json_response(
            anthropic_error_payload(message, error_type),
            status=upstream.status,
        )

    async def _stream_response(self, request: web.Request, upstream) -> web.StreamResponse:
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)

        stub = new_message_stub(self.requested_model or self.cfg.model)
        await resp.write(sse_event("message_start", {"message": stub}))

        tool_states: Dict[int, Dict[str, Any]] = {}
        finish_reason: Optional[str] = None
        usage_info: Dict[str, Optional[int]] = {}
        text_started = False

        async def stop_active_tools() -> None:
            for state in tool_states.values():
                if state.get("started") and not state.get("stopped"):
                    try:
                        await resp.write(sse_event("content_block_stop", {"index": state["anth_index"]}))
                    except (ConnectionResetError, ClientConnectionError) as exc:
                        if logging_control.is_enabled():
                            print(f"Client disconnected closing OpenAI tool block: {exc}")
                        raise
                    state["stopped"] = True

        async for chunk in iter_openai_sse(upstream):
            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            if not finish_reason:
                finish_reason = choice.get("finish_reason")

            usage_chunk = chunk.get("usage")
            if isinstance(usage_chunk, dict):
                usage_info = {
                    "input_tokens": usage_chunk.get("prompt_tokens"),
                    "output_tokens": usage_chunk.get("completion_tokens"),
                }

            text = delta.get("content") or delta.get("text")
            if text:
                try:
                    if not text_started:
                        await resp.write(sse_event("content_block_start", {"index": 0, "type": "text"}))
                        text_started = True
                    await resp.write(
                        sse_event(
                            "content_block_delta",
                            {"index": 0, "delta": {"type": "text_delta", "text": text}},
                        )
                    )
                except (ConnectionResetError, ClientConnectionError) as exc:
                    if logging_control.is_enabled():
                        print(f"Client disconnected during OpenAI text streaming: {exc}")
                    break

            tool_deltas = delta.get("tool_calls")
            if isinstance(tool_deltas, list):
                _debug_log("Received OpenAI tool_calls delta: %s", tool_deltas)
                for tool in tool_deltas:
                    openai_index = int(tool.get("index", 0))
                    state = tool_states.setdefault(
                        openai_index,
                        {
                            "anth_index": openai_index + 1,
                            "openai_id": None,
                            "anth_id": None,
                            "name": None,
                            "arguments": "",
                            "started": False,
                            "stopped": False,
                        },
                    )

                    raw_id = tool.get("id")
                    function = tool.get("function") or {}
                    raw_name = function.get("name")

                    _debug_log(
                        "Processing tool[%d]: id=%s name=%s args=%s",
                        openai_index,
                        raw_id,
                        raw_name,
                        function.get("arguments", "")[:100] if function.get("arguments") else "None"
                    )

                    self._assign_tool_identity(state, raw_id, raw_name)

                    if not state.get("started"):
                        _debug_log(
                            "Emitting tool_use start: index=%d id=%s name=%s",
                            state["anth_index"],
                            state["anth_id"],
                            state["name"]
                        )
                        try:
                            await resp.write(
                                sse_event(
                                    "content_block_start",
                                    {
                                        "index": state["anth_index"],
                                        "type": "tool_use",
                                        "id": state["anth_id"],
                                        "name": state["name"],
                                        "input": {},
                                    },
                                )
                            )
                            await resp.write(
                                sse_event(
                                    "content_block_delta",
                                    {
                                        "index": state["anth_index"],
                                        "delta": {"type": "input_json_delta", "partial_json": ""},
                                    },
                                )
                            )
                        except (ConnectionResetError, ClientConnectionError) as exc:
                            if logging_control.is_enabled():
                                print(f"Client disconnected opening OpenAI tool block: {exc}")
                            return resp
                        state["started"] = True

                    arguments = function.get("arguments")
                    if isinstance(arguments, str) and arguments:
                        previous = state.get("arguments", "")
                        addition = arguments
                        if previous and arguments.startswith(previous):
                            addition = arguments[len(previous) :]
                        state["arguments"] = arguments
                        if addition:
                            _debug_log(
                                "Emitting tool arguments delta[%d]: %s",
                                state["anth_index"],
                                addition[:50] + ("..." if len(addition) > 50 else "")
                            )
                            try:
                                await resp.write(
                                    sse_event(
                                        "content_block_delta",
                                        {
                                            "index": state["anth_index"],
                                            "delta": {
                                                "type": "input_json_delta",
                                                "partial_json": addition,
                                            },
                                        },
                                    )
                                )
                            except (ConnectionResetError, ClientConnectionError) as exc:
                                if logging_control.is_enabled():
                                    print(f"Client disconnected during OpenAI tool delta: {exc}")
                                return resp

            if choice.get("finish_reason") == "tool_calls":
                try:
                    await stop_active_tools()
                except (ConnectionResetError, ClientConnectionError):
                    return resp

        if text_started:
            try:
                await resp.write(sse_event("content_block_stop", {"index": 0}))
            except (ConnectionResetError, ClientConnectionError) as exc:
                if logging_control.is_enabled():
                    print(f"Client disconnected closing OpenAI text block: {exc}")
                return resp

        try:
            await stop_active_tools()
        except (ConnectionResetError, ClientConnectionError):
            return resp

        stop_reason = map_stop_reason(finish_reason, tool_used=any(state.get("started") for state in tool_states.values()))

        try:
            await emit_message_tail(resp, stop_reason=stop_reason, usage=usage_info or None)
        except (ConnectionResetError, ClientConnectionError) as exc:
            if logging_control.is_enabled():
                print(f"Client disconnected sending message tail events: {exc}")
            return resp

        if logging_control.is_enabled():
            print("Streaming response completed successfully")
        return resp

    async def _non_stream_response(self, upstream) -> web.StreamResponse:
        payload = await upstream.json()
        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason")

        text_content = message.get("content", "")
        if isinstance(text_content, list):
            merged = "".join(part.get("text", "") for part in text_content if isinstance(part, dict))
            text = merged
        else:
            text = text_content or ""

        content_blocks: List[Dict[str, Any]] = []
        if text:
            content_blocks.append({"type": "text", "text": text})

        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            _debug_log("Non-stream received %d tool calls from OpenAI", len(tool_calls))

        for tc in tool_calls:
            raw_id = tc.get("id")
            tool_id = self._to_anthropic_tool_id(raw_id)
            function = tc.get("function") or {}
            tool_name = function.get("name") or self.tool_id_map.get(raw_id or tool_id) or "function"

            _debug_log(
                "Converting tool call: OpenAI[id=%s, name=%s] → Anthropic[id=%s, name=%s]",
                raw_id,
                function.get("name"),
                tool_id,
                tool_name
            )

            args = {}
            if function.get("arguments"):
                try:
                    args = json.loads(function["arguments"])
                except Exception:
                    args = {"_raw": function["arguments"]}
            tool_block = {"type": "tool_use", "id": tool_id, "name": tool_name, "input": args}
            _debug_log("Final tool block: %s", json.dumps(tool_block, ensure_ascii=False)[:200])
            content_blocks.append(tool_block)

        usage = payload.get("usage") or {}
        anthropic_usage = {
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }

        if finish_reason == "tool_calls":
            stop_reason = "tool_use"
        elif finish_reason == "stop":
            stop_reason = "end_turn"
        else:
            stop_reason = finish_reason

        return web.json_response(
            {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "role": "assistant",
                "model": self.requested_model or self.cfg.model,
                "content": content_blocks,
                "stop_reason": stop_reason,
                "usage": anthropic_usage,
            }
        )



class ChatGPTExecutor(ProviderExecutor):
    def __init__(self, cfg, request_body, requested_model, alt: Optional[str] = None):
        super().__init__(cfg, request_body, requested_model, alt=alt)
        self._codex_tool_reverse: Dict[str, str] = {}
        self._tool_id_alias: Dict[str, str] = {}

    def _tool_name_reverse_map(self) -> Dict[str, str]:
        return self._codex_tool_reverse or {}

    def _to_anthropic_tool_id(self, raw_id: Optional[str]) -> str:
        if raw_id and raw_id in self._tool_id_alias:
            return self._tool_id_alias[raw_id]
        generated = _generate_tool_id()
        if raw_id:
            self._tool_id_alias[raw_id] = generated
        return generated

    def _assign_tool_identity(
        self,
        state: Dict[str, Any],
        raw_id: Optional[str],
        default_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        if raw_id:
            state["openai_id"] = raw_id
            if raw_id in self._tool_id_alias:
                state["anth_id"] = self._tool_id_alias[raw_id]
            elif state.get("anth_id"):
                self._tool_id_alias[raw_id] = state["anth_id"]
            else:
                state["anth_id"] = self._to_anthropic_tool_id(raw_id)
        elif not state.get("anth_id"):
            state["anth_id"] = _generate_tool_id()

        if raw_id and raw_id in self._codex_tool_reverse:
            state["name"] = self._codex_tool_reverse[raw_id]
        if default_name:
            state["name"] = default_name
        if not state.get("name"):
            state["name"] = self._codex_tool_reverse.get(raw_id or "", "function")
        if raw_id and raw_id not in self._tool_id_alias:
            self._tool_id_alias[raw_id] = state["anth_id"]
        return state

    async def execute(self, request: web.Request) -> web.StreamResponse:
        upstream_body, reverse_map = map_anthropic_to_chatgpt_backend(
            self.request_body,
            self.cfg.model,
            provider=self.cfg.provider,
            auth_method=self.cfg.auth_method or "oauth",
            explicit_instruction=self.cfg.system_instruction,
        )

        self._codex_tool_reverse = reverse_map

        debug_dump = os.environ.get("KISUKE_DEBUG_DUMP")
        if debug_dump:
            timestamp = int(time.time() * 1000)
            debug_dir = Path(debug_dump)
            try:
                debug_dir.mkdir(parents=True, exist_ok=True)
                with open(debug_dir / f"anthropic_{timestamp}.json", "w", encoding="utf-8") as fh:
                    json.dump(self.request_body, fh, indent=2, ensure_ascii=False)
                with open(debug_dir / f"codex_{timestamp}.json", "w", encoding="utf-8") as fh:
                    json.dump(upstream_body, fh, indent=2, ensure_ascii=False)
            except Exception as dump_exc:
                if logging_control.is_enabled():
                    print(f"Failed to dump debug payloads: {dump_exc}")

        url = "https://chatgpt.com/backend-api/codex/responses"
        headers = {
            "Version": "0.21.0",
            "Content-Type": "application/json",
            "Openai-Beta": "responses=experimental",
            "Session_id": str(uuid.uuid4()),
            "Accept": "text/event-stream",
            "Connection": "Keep-Alive",
        }

        auth_method = (self.cfg.auth_method or "api_key").lower()
        extra_headers = dict(self.cfg.extra_headers or {})
        has_originator_override = any(k.lower() == "originator" for k in extra_headers)
        if auth_method != "api_key" and not has_originator_override:
            headers["Originator"] = "codex_cli_rs"

        auth = resolve_auth_strategy(self.cfg.provider, self.cfg)
        headers.update(auth.headers())
        headers.update(extra_headers)

        masked_headers = {
            key: mask_secret(value) if "authorization" in key.lower() or "key" in key.lower() else value
            for key, value in headers.items()
        }
        if logging_control.is_enabled():
            print("\nUPSTREAM REQUEST:")
            print(f"   Provider: {self.cfg.provider}")
            print(f"   Model: {self.cfg.model}")
            print(f"   Auth Method: {getattr(self.cfg, 'auth_method', 'oauth')}")
            print(f"   URL: {url}")
            print(f"   Headers: {masked_headers}")
            try:
                body_str = json.dumps(upstream_body, indent=2, ensure_ascii=False)
                print(f"   Request Body (size: {len(body_str)}):\n{body_str}")
            except Exception:
                print("   Request Body: <unserializable>")

        stream = bool(self.request_body.get("stream", False))

        try:
            async with self._client_session() as session:
                async with session.post(url, json=upstream_body, headers=headers) as upstream:
                    if logging_control.is_enabled():
                        print("\nUPSTREAM RESPONSE:")
                        print(f"   Status: {upstream.status}")
                        print(f"   Headers: {dict(upstream.headers)}")

                    if upstream.status >= 400:
                        error_body = await _read_json(upstream)
                        error_type, message = extract_error_details(error_body)
                        if stream:
                            resp = web.StreamResponse(status=upstream.status, headers={"Content-Type": "text/event-stream"})
                            await resp.prepare(request)
                            await resp.write(sse_event("error", anthropic_error_payload(message, error_type)))
                            await resp.write(
                                sse_event(
                                    "message_stop",
                                    {"type": "message_stop", "stop_reason": "error"},
                                )
                            )
                            await resp.write_eof()
                            return resp
                        return web.json_response(
                            anthropic_error_payload(message, error_type),
                            status=upstream.status,
                        )

                    if stream:
                        return await self._stream_codex(request, upstream)
                    return await self._non_stream_codex(upstream)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if logging_control.is_enabled():
                print(f"ChatGPT executor error: {exc}")
            return web.json_response(
                anthropic_error_payload(f"Upstream error: {exc}"),
                status=502,
            )


    async def _stream_codex(self, request: web.Request, upstream) -> web.StreamResponse:
        from ..sse import iter_codex_sse  # local import to avoid circular

        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)

        tool_name_map = self._tool_name_reverse_map()

        stub = new_message_stub(self.requested_model or self.cfg.model)
        await resp.write(sse_event("message_start", {"message": stub}))

        text_blocks: Dict[int, bool] = {}
        thinking_blocks: Dict[int, bool] = {}
        tool_states: Dict[tuple[str, int], Dict[str, Any]] = {}
        usage_info: Dict[str, Any] = {}
        finish_reason: Optional[str] = None

        def state_key(raw_id: Optional[str], index: int) -> tuple[str, int]:
            return (raw_id or "", index)

        async def ensure_tool(raw_id: Optional[str], index: int, raw_name: Optional[str]) -> Dict[str, Any]:
            key = state_key(raw_id, index)
            resolved_name = tool_name_map.get(raw_name or "", raw_name or "function")
            state = tool_states.setdefault(
                key,
                {
                    "key": key,
                    "raw_id": raw_id,
                    "anth_id": None,
                    "call_id": None,
                    "index": index,
                    "name": resolved_name,
                    "arguments": "",
                    "output_json": "",
                    "output_text": "",
                    "is_error": False,
                    "started": False,
                    "stopped": False,
                    "result_sent": False,
                },
            )
            state.setdefault("index", index)
            state.setdefault("name", resolved_name)
            state = self._assign_tool_identity(state, raw_id, resolved_name)
            state["call_id"] = state["anth_id"]
            if not state.get("started"):
                await resp.write(
                    sse_event(
                        "content_block_start",
                        {
                            "index": state["index"],
                            "type": "tool_use",
                            "id": state["anth_id"],
                            "name": state["name"],
                            "input": {},
                        },
                    )
                )
                await resp.write(
                    sse_event(
                        "content_block_delta",
                        {
                            "index": state["index"],
                            "delta": {"type": "input_json_delta", "partial_json": ""},
                        },
                    )
                )
                state["started"] = True
            return state

        async def stop_tool(state: Dict[str, Any]) -> None:
            if state.get("started") and not state.get("stopped"):
                await resp.write(sse_event("content_block_stop", {"index": state["index"]}))
                state["stopped"] = True

        try:
            async for event_name, event_data in iter_codex_sse(upstream):
                if event_name == "response.content_part.added":
                    index = event_data.get("output_index", 0)
                    if not text_blocks.get(index):
                        await resp.write(sse_event("content_block_start", {"index": index, "type": "text"}))
                        text_blocks[index] = True
                    continue

                if event_name == "response.output_text.delta":
                    index = event_data.get("output_index", 0)
                    if not text_blocks.get(index):
                        await resp.write(sse_event("content_block_start", {"index": index, "type": "text"}))
                        text_blocks[index] = True
                    delta_text = event_data.get("delta", "") or ""
                    if delta_text:
                        await resp.write(
                            sse_event(
                                "content_block_delta",
                                {"index": index, "delta": {"type": "text_delta", "text": delta_text}},
                            )
                        )
                    continue

                if event_name == "response.content_part.done":
                    index = event_data.get("output_index", 0)
                    if text_blocks.pop(index, None):
                        await resp.write(sse_event("content_block_stop", {"index": index}))
                    continue

                if event_name == "response.reasoning_summary_part.added":
                    index = event_data.get("output_index", 0)
                    if not thinking_blocks.get(index):
                        await resp.write(sse_event("content_block_start", {"index": index, "type": "thinking"}))
                        thinking_blocks[index] = True
                    continue

                if event_name == "response.reasoning_summary_text.delta":
                    index = event_data.get("output_index", 0)
                    if not thinking_blocks.get(index):
                        await resp.write(sse_event("content_block_start", {"index": index, "type": "thinking"}))
                        thinking_blocks[index] = True
                    delta_text = event_data.get("delta", "") or ""
                    if delta_text:
                        await resp.write(
                            sse_event(
                                "content_block_delta",
                                {"index": index, "delta": {"type": "thinking_delta", "thinking": delta_text}},
                            )
                        )
                    continue

                if event_name == "response.reasoning_summary_part.done":
                    index = event_data.get("output_index", 0)
                    if thinking_blocks.pop(index, None):
                        await resp.write(sse_event("content_block_stop", {"index": index}))
                    continue

                if event_name == "response.output_item.added":
                    item = event_data.get("item") or {}
                    if item.get("type") == "function_call":
                        raw_id = item.get("call_id")
                        index = event_data.get("output_index", 0)
                        name = item.get("name", "function")
                        state = await ensure_tool(raw_id, index, name)
                        arguments = item.get("arguments") or ""
                        if arguments:
                            state["arguments"] = arguments
                            await resp.write(
                                sse_event(
                                    "content_block_delta",
                                    {
                                        "index": state["index"],
                                        "delta": {"type": "input_json_delta", "partial_json": arguments},
                                    },
                                )
                            )
                    continue

                if event_name in {"response.function_call.arguments.delta", "response.function_call_arguments.delta"}:
                    raw_id = event_data.get("call_id")
                    index = event_data.get("output_index", 0)
                    name = event_data.get("name") or tool_name_map.get(raw_id or "", "function")
                    state = await ensure_tool(raw_id, index, name)
                    delta_chunk = event_data.get("delta", "") or ""
                    if delta_chunk:
                        state["arguments"] = state.get("arguments", "") + delta_chunk
                        await resp.write(
                            sse_event(
                                "content_block_delta",
                                {
                                    "index": state["index"],
                                    "delta": {"type": "input_json_delta", "partial_json": delta_chunk},
                                },
                            )
                        )
                    continue

                if event_name == "response.tool_call.delta":
                    raw_id = event_data.get("call_id")
                    index = event_data.get("output_index", 0)
                    name = event_data.get("name") or tool_name_map.get(raw_id or "", "function")
                    state = await ensure_tool(raw_id, index, name)
                    delta_chunk = event_data.get("delta", "") or ""
                    if delta_chunk:
                        state["arguments"] = state.get("arguments", "") + delta_chunk
                        await resp.write(
                            sse_event(
                                "content_block_delta",
                                {
                                    "index": state["index"],
                                    "delta": {"type": "input_json_delta", "partial_json": delta_chunk},
                                },
                            )
                        )
                    continue

                if event_name == "response.tool_call.output_json.delta":
                    raw_id = event_data.get("call_id")
                    index = event_data.get("output_index", 0)
                    state = await ensure_tool(raw_id, index, event_data.get("name", "function"))
                    state["output_json"] = state.get("output_json", "") + (event_data.get("delta", "") or "")
                    continue

                if event_name == "response.tool_call.output_text.delta":
                    raw_id = event_data.get("call_id")
                    index = event_data.get("output_index", 0)
                    state = await ensure_tool(raw_id, index, event_data.get("name", "function"))
                    state["output_text"] = state.get("output_text", "") + (event_data.get("delta", "") or "")
                    continue

                if event_name == "response.tool_call.error":
                    raw_id = event_data.get("call_id")
                    index = event_data.get("output_index", 0)
                    state = tool_states.get(state_key(raw_id, index))
                    if state is not None:
                        state["is_error"] = True
                        state["output_text"] = state.get("output_text", "") + (event_data.get("error", "") or "")
                    continue

                if event_name == "response.output_item.done":
                    item = event_data.get("item") or {}
                    if item.get("type") == "function_call":
                        raw_id = item.get("call_id")
                        index = event_data.get("output_index", 0)
                        state = tool_states.get(state_key(raw_id, index))
                        if state is not None:
                            await stop_tool(state)
                    continue

                if event_name == "response.function_call.completed":
                    raw_id = event_data.get("call_id")
                    index = event_data.get("output_index", 0)
                    state = tool_states.get(state_key(raw_id, index))
                    if state is not None:
                        await stop_tool(state)
                        await self._emit_tool_result(resp, state.get("call_id"), state)
                    continue

                if event_name == "response.tool_call.done":
                    raw_id = event_data.get("call_id")
                    index = event_data.get("output_index", 0)
                    state = tool_states.get(state_key(raw_id, index))
                    if state is not None:
                        await stop_tool(state)
                        await self._emit_tool_result(resp, state.get("call_id"), state)
                    continue

                if event_name == "response.completed":
                    finish_reason = event_data.get("finish_reason")
                    usage = event_data.get("response", {}).get("usage") if isinstance(event_data.get("response"), dict) else event_data.get("usage")
                    if isinstance(usage, dict):
                        usage_info = {
                            "input_tokens": usage.get("input_tokens"),
                            "output_tokens": usage.get("output_tokens"),
                        }
                    break
        except (ConnectionResetError, ClientConnectionError) as exc:
            if logging_control.is_enabled():
                print(f"Client disconnected during Codex streaming: {exc}")
            return resp

        for index in list(text_blocks.keys()):
            await resp.write(sse_event("content_block_stop", {"index": index}))
            text_blocks.pop(index, None)

        for index in list(thinking_blocks.keys()):
            await resp.write(sse_event("content_block_stop", {"index": index}))
            thinking_blocks.pop(index, None)

        tool_used = False
        for state in tool_states.values():
            if state.get("started") and not state.get("stopped"):
                await stop_tool(state)
            if state.get("started"):
                tool_used = True
            await self._emit_tool_result(resp, state.get("call_id"), state)

        stop_reason = map_stop_reason(finish_reason, tool_used=tool_used)

        await emit_message_tail(resp, stop_reason=stop_reason, usage=usage_info or None)

        return resp

    async def _emit_tool_result(self, resp: web.StreamResponse, call_id: Optional[str], state: Dict[str, Any]):
        if not call_id or state.get("result_sent"):
            return

        output_json = state.get("output_json", "").strip()
        output_text = state.get("output_text", "").strip()

        content: Any = output_text or output_json
        if not content:
            return

        try:
            parsed = json.loads(output_json) if output_json else None
        except json.JSONDecodeError:
            parsed = None

        payload: Dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": call_id,
            "is_error": bool(state.get("is_error", False)),
        }

        if parsed is not None:
            payload["content"] = parsed
        else:
            payload["content"] = content

        _debug_log(
            "tool result: call_id=%s is_error=%s preview=%s",
            call_id,
            payload["is_error"],
            str(payload["content"])[:200],
        )

        try:
            await resp.write(sse_event("tool_result", payload))
            state["result_sent"] = True
        except (ConnectionResetError, ClientConnectionError) as exc:
            print(f"Client disconnected sending Codex tool_result: {exc}")

    async def _non_stream_codex(self, upstream) -> web.StreamResponse:
        from ..sse import iter_codex_sse  # local import

        tool_name_map = self._tool_name_reverse_map()

        accumulated_text = ""
        tool_calls: Dict[str, Dict[str, Any]] = {}
        stop_reason = "end_turn"
        usage_info: Dict[str, Any] = {}

        async for event_name, event_data in iter_codex_sse(upstream):
            if event_name == "response.output_text.delta":
                accumulated_text += event_data.get("delta", "") or ""
            elif event_name in {"response.function_call.arguments.delta", "response.function_call_arguments.delta"}:
                call_id = event_data.get("call_id", f"tool_{uuid.uuid4().hex[:8]}")
                state = tool_calls.setdefault(
                    call_id,
                    {
                        "id": call_id,
                        "name": tool_name_map.get(event_data.get("name", "function"), event_data.get("name", "function")),
                        "arguments": "",
                    },
                )
                state["arguments"] += event_data.get("delta", "") or ""
            elif event_name == "response.output_item.added":
                item = event_data.get("item") or {}
                if item.get("type") == "function_call":
                    call_id = item.get("call_id", f"tool_{uuid.uuid4().hex[:8]}")
                    state = tool_calls.setdefault(
                        call_id,
                        {
                            "id": call_id,
                            "name": tool_name_map.get(item.get("name", "function"), item.get("name", "function")),
                            "arguments": "",
                        },
                    )
                    if item.get("arguments"):
                        state["arguments"] = item.get("arguments")
            elif event_name == "response.completed":
                finish = event_data.get("finish_reason")
                if finish == "max_tokens":
                    stop_reason = "max_tokens"
                elif finish == "tool_calls":
                    stop_reason = "tool_use"
                usage = (
                    event_data.get("response", {}).get("usage")
                    if isinstance(event_data.get("response"), dict)
                    else event_data.get("usage")
                )
                if isinstance(usage, dict):
                    usage_info = {
                        "input_tokens": usage.get("input_tokens"),
                        "output_tokens": usage.get("output_tokens"),
                    }

        content: List[Dict[str, Any]] = []
        if accumulated_text:
            content.append({"type": "text", "text": accumulated_text})
        for call in tool_calls.values():
            args_json: Any = {}
            arguments = call.get("arguments") or ""
            if arguments:
                try:
                    args_json = json.loads(arguments)
                except Exception:
                    args_json = {"_raw": arguments}
            raw_id = call.get("id") or call.get("call_id")
            anth_id = self._to_anthropic_tool_id(raw_id)
            content.append(
                {
                    "type": "tool_use",
                    "id": anth_id,
                    "name": call.get("name") or "function",
                    "input": args_json,
                }
            )

        payload: Dict[str, Any] = {
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": self.requested_model or self.cfg.model,
            "content": content or [{"type": "text", "text": ""}],
            "stop_reason": stop_reason,
        }
        if usage_info:
            payload["usage"] = usage_info

        return web.json_response(payload)
    async def _non_stream_codex(self, upstream) -> web.StreamResponse:
        from ..sse import iter_codex_sse  # local import

        accumulated_text = ""
        tool_calls: Dict[str, Dict[str, Any]] = {}
        stop_reason = "end_turn"
        usage_info: Dict[str, Any] = {}

        async for event_name, event_data in iter_codex_sse(upstream):
            if event_name == "response.output_text.delta":
                accumulated_text += event_data.get("delta", "")
            elif event_name == "response.function_call.arguments.delta":
                call_id = event_data.get("call_id", f"tool_{uuid.uuid4().hex[:8]}")
                state = tool_calls.setdefault(
                    call_id,
                    {
                        "id": call_id,
                        "name": event_data.get("name", "function"),
                        "arguments": "",
                    },
                )
                state["arguments"] += event_data.get("delta", "")
            elif event_name == "response.function_call.completed":
                call_id = event_data.get("call_id")
                if call_id in tool_calls:
                    tool_calls[call_id]["completed"] = True
            elif event_name == "response.completed":
                finish = event_data.get("finish_reason")
                if finish == "max_tokens":
                    stop_reason = "max_tokens"
                elif finish == "tool_calls":
                    stop_reason = "tool_use"
                usage = (
                    event_data.get("response", {}).get("usage")
                    if isinstance(event_data.get("response"), dict)
                    else event_data.get("usage")
                )
                if isinstance(usage, dict):
                    usage_info = {
                        "input_tokens": usage.get("input_tokens"),
                        "output_tokens": usage.get("output_tokens"),
                    }

        content = [{"type": "text", "text": accumulated_text or ""}]
        for call in tool_calls.values():
            args_json = {}
            if call.get("arguments"):
                try:
                    args_json = json.loads(call["arguments"])
                except Exception:
                    args_json = {"_raw": call["arguments"]}
            content.append({"type": "tool_use", "id": call.get("id"), "name": call.get("name"), "input": args_json})

        if tool_calls:
            mapped = {
                call_id: {
                    "name": details.get("name"),
                    "arguments_preview": (details.get("arguments") or "")[:200],
                }
                for call_id, details in tool_calls.items()
            }
            _debug_log("tool results (non-stream): %s", mapped)

        payload = {
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": self.requested_model or self.cfg.model,
            "content": content,
            "stop_reason": stop_reason,
        }
        if usage_info:
            payload["usage"] = usage_info

        return web.json_response(payload)
    async def _non_stream_codex(self, upstream) -> web.StreamResponse:
        from ..sse import iter_codex_sse  # local import

        accumulated_text = ""
        tool_calls: Dict[str, Dict[str, Any]] = {}
        stop_reason = "end_turn"

        async for event_name, event_data in iter_codex_sse(upstream):
            if event_name == "response.output_text.delta":
                accumulated_text += event_data.get("delta", "")
            elif event_name == "response.function_call.arguments.delta":
                call_id = event_data.get("call_id", f"tool_{uuid.uuid4().hex[:8]}")
                state = tool_calls.setdefault(
                    call_id,
                    {
                        "id": call_id,
                        "name": event_data.get("name", "function"),
                        "arguments": "",
                    },
                )
                state["arguments"] += event_data.get("delta", "")
            elif event_name == "response.function_call.completed":
                call_id = event_data.get("call_id")
                if call_id in tool_calls:
                    tool_calls[call_id]["completed"] = True
            elif event_name == "response.completed":
                finish = event_data.get("finish_reason")
                if finish == "max_tokens":
                    stop_reason = "max_tokens"
                elif finish == "tool_calls":
                    stop_reason = "tool_use"

        content = [{"type": "text", "text": accumulated_text or ""}]
        for call in tool_calls.values():
            args_json = {}
            if call.get("arguments"):
                try:
                    args_json = json.loads(call["arguments"])
                except Exception:
                    args_json = {"_raw": call["arguments"]}
            content.append({"type": "tool_use", "id": call.get("id"), "name": call.get("name"), "input": args_json})

        return web.json_response(
            {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "role": "assistant",
                "model": self.requested_model or self.cfg.model,
                "content": content,
                "stop_reason": stop_reason,
            }
        )


__all__ = ["OpenAIExecutor", "ChatGPTExecutor"]
