"""Streaming transport for a tools-disabled final Responses API pass only."""
from __future__ import annotations

import codecs
import json
import time
from types import SimpleNamespace

import requests


class StreamCancelled(Exception):
    """The requesting client no longer wants this final model pass."""


def _sse_events(chunks, check_active):
    """Decode SSE across network/UTF-8 boundaries, including multiline data."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    pending = ""
    data = []
    event_name = ""
    event_bytes = 0

    def lines(text, final=False):
        nonlocal pending
        pending += text
        while pending:
            positions = [p for p in (pending.find("\n"), pending.find("\r")) if p >= 0]
            if not positions:
                break
            pos = min(positions)
            if pending[pos] == "\r" and pos + 1 == len(pending) and not final:
                break
            width = 2 if pending[pos:pos + 2] == "\r\n" else 1
            line, pending = pending[:pos], pending[pos + width:]
            yield line
        if len(pending) > 1_048_576:
            raise ValueError("Provider stream frame is too large")

    for chunk in chunks:
        check_active()
        if not chunk:
            continue
        for line in lines(decoder.decode(chunk)):
            check_active()
            if not line:
                if data:
                    yield event_name, "\n".join(data)
                data, event_name, event_bytes = [], "", 0
                continue
            if line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if field == "data":
                data.append(value)
                event_bytes += len(value)
                if event_bytes > 1_048_576:
                    raise ValueError("Provider stream frame is too large")
            elif field == "event":
                event_name = value
    # A truncated UTF-8 sequence is an error. An unterminated SSE event is not
    # dispatched; callers still require an explicit response.completed event.
    decoder.decode(b"", final=True)


def stream_response(provider, *, instructions, input_items, tools,
                    previous_response_id, timeout_seconds, tool_choice,
                    on_delta, cancelled):
    """Return the normal ProviderResponse while forwarding only final text deltas.

    This entry point cannot run tools. Runtime decides when its existing tool
    loop is finished and handles display redaction/speech-tag filtering.
    Cancellation is cooperative between reads; a blocked read is bounded by
    the request timeout. No database resources are opened or held here.
    """
    from agent_runtime import ProviderResponse, _log_provider_failure

    if tool_choice not in ("none", "auto") or (tools and tool_choice != "none"):
        raise ValueError("Streaming requires a tools-disabled final model pass")
    # Preserve complete()'s model-visible payload. A disabled catalog can still
    # be present with tool_choice=none; an empty catalog also permits auto.
    # Neither case permits a tool call, and neither needs a new model pass.
    api_tools = []
    for tool in tools:
        item = dict(tool)
        item["name"] = tool["name"].replace(".", "__")
        api_tools.append(item)
    deadline = time.monotonic() + float(timeout_seconds)

    def check_active():
        if cancelled is not None and cancelled():
            raise StreamCancelled("Agent stream cancelled")
        if time.monotonic() >= deadline:
            raise requests.Timeout("Agent stream deadline exceeded")

    payload = {
        "model": provider.model, "instructions": instructions, "input": input_items,
        "tools": api_tools, "tool_choice": tool_choice, "parallel_tool_calls": True,
        "store": False, "include": ["reasoning.encrypted_content"],
        "max_output_tokens": 2000, "stream": True,
    }
    # store=False: continuation is carried by input_items, just like complete().
    response = None
    stage = "request"
    try:
        check_active()
        response = requests.post(
            provider.endpoint,
            headers={"Authorization": f"Bearer {provider.api_key}",
                     "Content-Type": "application/json", "Accept": "text/event-stream"},
            json=payload, stream=True, timeout=max(0.001, deadline - time.monotonic()),
        )
        response.raise_for_status()
        check_active()
        stage = "response parsing"
        completed = None
        text_size = 0
        for event_name, raw in _sse_events(response.iter_content(chunk_size=64), check_active):
            check_active()
            if raw == "[DONE]":
                break
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError("Malformed provider stream event")
            event_type = event.get("type") or event_name
            if event_type == "response.output_text.delta":
                delta = event.get("delta")
                if not isinstance(delta, str):
                    raise ValueError("Malformed provider text delta")
                text_size += len(delta)
                if text_size > 128_000:
                    raise ValueError("Provider stream text is too large")
                if delta:
                    on_delta(delta)
                    check_active()
            elif event_type == "response.completed":
                completed = event.get("response")
                break
            elif event_type in {"error", "response.failed", "response.incomplete", "response.cancelled"}:
                raise ValueError("Provider did not complete response")
            # Reasoning, function arguments and other event payloads never
            # enter the display callback or application logs.
        check_active()
        if not isinstance(completed, dict) or completed.get("status") != "completed":
            raise ValueError("Provider stream ended without completed response")
        output = completed.get("output")
        if not isinstance(output, list):
            raise ValueError("Malformed completed provider response")
        stage = "final response"
        texts = []
        for item in output:
            if not isinstance(item, dict):
                raise ValueError("Malformed provider output item")
            if item.get("type") == "reasoning":
                continue
            if item.get("type") != "message" or item.get("role", "assistant") != "assistant":
                raise ValueError("Unexpected tool output in final model pass")
            content = item.get("content")
            if not isinstance(content, list):
                raise ValueError("Malformed provider message content")
            for part in content:
                if not isinstance(part, dict):
                    raise ValueError("Malformed provider message part")
                if part.get("type") == "output_text":
                    if not isinstance(part.get("text"), str):
                        raise ValueError("Malformed provider final text")
                    texts.append(part["text"])
        usage = completed.get("usage") or {}
        if not isinstance(usage, dict):
            raise ValueError("Malformed provider usage")
        return ProviderResponse(
            text="\n".join(filter(None, texts)), output_items=tuple(dict(item) for item in output),
            response_id=str(completed.get("id") or ""),
            request_id=str(response.headers.get("x-request-id") or ""),
            model=str(completed.get("model") or provider.model),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
        )
    except StreamCancelled:
        raise
    except Exception as exc:
        # Do not call response.json() on a partially consumed SSE response:
        # that could block on the rest of the stream during error handling.
        diagnostic_response = None if response is None else SimpleNamespace(
            status_code=response.status_code, json=lambda: {},
        )
        _log_provider_failure(exc=exc, model=provider.model, stage=stage,
                              response=diagnostic_response)
        raise
    finally:
        if response is not None:
            response.close()
