import json
from types import SimpleNamespace

import pytest
import requests

import agent_streaming_provider as streaming


def event(kind, **fields):
    return ("event: " + kind + "\ndata: " + json.dumps({"type": kind, **fields}, ensure_ascii=False) + "\n\n").encode()


def completed(text="Masz jedną sztukę.", **overrides):
    body = {
        "id": "response-test", "model": "model-test", "status": "completed",
        "output": [{"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 12, "output_tokens": 7},
    }
    body.update(overrides)
    return event("response.completed", response=body)


class Response:
    status_code = 200
    headers = {"x-request-id": "request-test"}

    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False
        self.reads = 0

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("Provider rejected request", response=self)

    def iter_content(self, chunk_size):
        assert chunk_size <= 64
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    def json(self):
        raise AssertionError("SSE must not be read as a full JSON response")

    def close(self):
        self.closed = True


@pytest.fixture
def harness(monkeypatch):
    state = SimpleNamespace(calls=[], response=None)

    def post(url, **kwargs):
        state.calls.append((url, kwargs))
        return state.response

    monkeypatch.setattr(streaming.requests, "post", post)
    return state


def call(harness, on_delta=lambda _text: None, **overrides):
    provider = SimpleNamespace(model="model-test", api_key="not-a-real-key", endpoint="https://example.invalid/responses")
    args = {
        "instructions": "Answer finally.", "input_items": [], "tools": [],
        "previous_response_id": "old", "timeout_seconds": 30,
        "tool_choice": "none", "on_delta": on_delta, "cancelled": lambda: False,
    }
    args.update(overrides)
    return streaming.stream_response(provider, **args)


def test_streams_before_completion_preserves_result_and_closes(harness):
    harness.response = Response([
        event("response.output_text.delta", delta="Masz "),
        event("response.output_text.delta", delta="jedną sztukę."),
        completed(),
    ])
    deltas = []

    def receive(text):
        assert harness.response.reads < 3
        assert not harness.response.closed
        deltas.append(text)

    result = call(harness, on_delta=receive)
    assert deltas == ["Masz ", "jedną sztukę."]
    assert result.text == "".join(deltas)
    assert result.response_id == "response-test"
    assert result.request_id == "request-test"
    assert result.input_tokens == 12 and result.output_tokens == 7
    assert not result.tool_calls
    assert harness.response.closed
    _, sent = harness.calls[0]
    assert sent["stream"] is True and sent["json"]["stream"] is True
    assert sent["json"]["tool_choice"] == "none" and sent["json"]["tools"] == []
    assert sent["json"]["store"] is False
    assert "previous_response_id" not in sent["json"]


def test_fragmented_utf8_comments_crlf_and_multiline_data(harness):
    multiline = json.dumps({"type": "response.output_text.delta", "delta": "Półka: pięć."}, ensure_ascii=False, indent=2)
    wire = b": keepalive\r\n\r\n" + ("event: response.output_text.delta\r\n" + "\r\n".join("data: " + line for line in multiline.splitlines()) + "\r\n\r\n").encode() + completed("Półka: pięć.")
    harness.response = Response([wire[i:i + 1] for i in range(len(wire))])
    deltas = []
    assert call(harness, deltas.append).text == "Półka: pięć."
    assert deltas == ["Półka: pięć."] and harness.response.closed


def test_reasoning_and_tool_arguments_never_reach_display(harness):
    harness.response = Response([
        event("response.reasoning_summary_text.delta", delta="internal reasoning"),
        event("response.function_call_arguments.delta", delta='{"secret":"hidden"}'),
        event("response.output_text.delta", delta="Odpowiedź."), completed("Odpowiedź."),
    ])
    deltas = []
    call(harness, deltas.append)
    assert deltas == ["Odpowiedź."]


@pytest.mark.parametrize("options", [
    {"tools": [{"name": "inventory.adjust"}], "tool_choice": "auto"},
    {"tool_choice": "required"},
    {"tool_choice": {"type": "function", "name": "inventory__adjust"}},
])
def test_refuses_tool_enabled_pass_before_request(harness, options):
    with pytest.raises(ValueError, match="tools-disabled"):
        call(harness, **options)
    assert harness.calls == []


@pytest.mark.parametrize("tools, tool_choice", [
    ([], "none"),
    ([], "auto"),
    ([{"type": "function", "name": "inventory.count.get_expected",
       "description": "Read current stock", "parameters": {"type": "object"}}], "none"),
])
def test_stream_changes_only_transport_of_existing_complete_payload(harness, tools, tool_choice):
    from agent_runtime import OpenAIResponsesProvider

    provider = OpenAIResponsesProvider(model="model-test", api_key="not-a-real-key")
    body = json.loads(completed().decode().split("data: ", 1)[1])["response"]
    harness.response = SimpleNamespace(
        headers={"x-request-id": "request-test"},
        raise_for_status=lambda: None, json=lambda: body,
    )
    args = dict(instructions="Keep the existing full instructions.",
                input_items=[{"role": "user", "content": "Test"}], tools=tools,
                previous_response_id="old", timeout_seconds=30, tool_choice=tool_choice)
    original_tools = json.loads(json.dumps(tools))
    normal_result = provider.complete(**args)
    assert len(harness.calls) == 1
    normal_payload = harness.calls[0][1]["json"]

    harness.response = Response([completed()])
    streaming_result = provider.complete_stream(
        **args, on_delta=lambda _text: None, cancelled=lambda: False)
    assert len(harness.calls) == 2  # Exactly one HTTP request per adapter call.
    streaming_payload = dict(harness.calls[1][1]["json"])
    assert streaming_payload.pop("stream") is True
    assert streaming_payload == normal_payload
    assert streaming_result == normal_result
    assert tools == original_tools
    assert harness.response.closed
    if tools:
        assert streaming_payload["tools"][0]["name"] == "inventory__count__get_expected"


@pytest.mark.parametrize("wire", [
    event("response.output_text.delta", delta="partial"),
    b"data: [DONE]\n\n",
    event("response.failed", response={"error": {"message": "private error"}}),
    event("response.incomplete", response={"status": "incomplete"}),
    event("error", message="private provider error"),
    completed(status="incomplete"),
    completed(output=[{"type": "function_call", "arguments": "private arguments"}]),
    b"data: {invalid json}\n\n",
    completed()[:-1],  # No terminating empty line: truncated event is not final.
])
def test_incomplete_failed_or_malformed_stream_never_succeeds(harness, wire, caplog):
    harness.response = Response([wire])
    with pytest.raises((ValueError, json.JSONDecodeError)):
        call(harness)
    assert harness.response.closed
    assert len(harness.calls) == 1  # Never regenerate or retry a failed stream.
    assert "private error" not in caplog.text
    assert "private provider error" not in caplog.text
    assert "private arguments" not in caplog.text
    assert "not-a-real-key" not in caplog.text


def test_cancellation_during_delta_closes_without_remaining_reads(harness):
    cancelled = False
    harness.response = Response([event("response.output_text.delta", delta="Pierwsze."), completed()])

    def receive(_text):
        nonlocal cancelled
        cancelled = True

    with pytest.raises(streaming.StreamCancelled):
        call(harness, receive, cancelled=lambda: cancelled)
    assert harness.response.closed and harness.response.reads == 1


def test_cancellation_before_start_does_not_call_provider(harness):
    with pytest.raises(streaming.StreamCancelled):
        call(harness, cancelled=lambda: True)
    assert harness.calls == []


def test_total_deadline_checked_between_chunks(harness, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(streaming.time, "monotonic", lambda: clock[0])
    harness.response = Response([event("response.output_text.delta", delta="Pierwsze."), completed()])

    def receive(_text):
        clock[0] += 31

    with pytest.raises(requests.Timeout):
        call(harness, receive)
    assert harness.response.closed and harness.response.reads == 1


def test_socket_read_exception_closes(harness):
    def chunks():
        yield event("response.output_text.delta", delta="Pierwsze.")
        raise requests.Timeout("read failed")

    harness.response = Response(chunks())
    with pytest.raises(requests.Timeout):
        call(harness)
    assert harness.response.closed
    assert len(harness.calls) == 1


def test_callback_exception_closes(harness):
    harness.response = Response([event("response.output_text.delta", delta="Pierwsze."), completed()])

    def broken_callback(_text):
        raise RuntimeError("Client disconnected")

    with pytest.raises(RuntimeError):
        call(harness, broken_callback)
    assert harness.response.closed


def test_http_failure_closes_without_parsing_sse(harness):
    harness.response = Response([])
    harness.response.status_code = 500
    with pytest.raises(requests.HTTPError):
        call(harness)
    assert harness.response.closed and harness.response.reads == 0
    assert len(harness.calls) == 1


def test_delta_precedes_final_by_known_test_delay(harness, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(streaming.time, "monotonic", lambda: clock[0])

    def chunks():
        clock[0] += 0.040
        yield event("response.output_text.delta", delta="Masz jedną sztukę.")
        clock[0] += 0.500
        yield completed()

    harness.response = Response(chunks())
    observed = []
    call(harness, lambda _text: observed.append(clock[0] - 10.0))
    assert observed == pytest.approx([0.040])
    assert clock[0] - 10.0 == pytest.approx(0.540)


@pytest.mark.parametrize('chunk_size', [1, 2, 9, 4096])
def test_display_filter_literal_comparison_does_not_block_prose(chunk_size):
    from agent_runtime import _plain_response_text
    from agent_streaming import DisplayTextFilter
    raw = 'Aktualny stan jest < 10 sztuk. Kolejna część odpowiedzi pojawia się na żywo bez czekania.'
    captured = []
    parser = DisplayTextFilter(lambda _name, data: captured.append(data['delta']), _plain_response_text)
    for offset in range(0, len(raw), chunk_size):
        parser.feed(raw[offset:offset + chunk_size])
    assert 'stan jest < 10 sztuk.' in ''.join(captured)
    assert 'Kolejna część odpowiedzi' in ''.join(captured)
    assert not parser.frozen
    parser.finish(raw)
    assert ''.join(captured) == _plain_response_text(raw)


@pytest.mark.parametrize('chunk_size', [1, 2, 9, 4096])
def test_display_filter_pending_link_never_exposes_destination(chunk_size):
    from agent_runtime import _plain_response_text
    from agent_streaming import DisplayTextFilter
    prefix = 'Wynik jest już gotowy do odczytu. '
    link = '[szczegółowe informacje na temat zamówienia](https://example.invalid/download?signature=privatecredential123 opis linku z wieloma słowami)'
    suffix = ' oraz pozostałe dane dostępne w pełnym podsumowaniu dla użytkownika.'
    raw = prefix + link + suffix
    captured = []
    parser = DisplayTextFilter(lambda _name, data: captured.append(data['delta']), _plain_response_text)
    for offset in range(0, len(raw), chunk_size):
        parser.feed(raw[offset:offset + chunk_size])
        emitted = ''.join(captured)
        assert 'privatecredential123' not in emitted
        assert 'https://' not in emitted
        assert '[' not in emitted
    assert 'szczegółowe informacje na temat zamówienia' in ''.join(captured)
    assert not parser.frozen
    parser.finish(raw)
    assert ''.join(captured) == _plain_response_text(raw)


def test_display_filter_plain_prose_streams_without_newline_or_sentence_end():
    from agent_runtime import _plain_response_text
    from agent_streaming import DisplayTextFilter
    captured = []
    parser = DisplayTextFilter(lambda _name, data: captured.append(data['delta']), _plain_response_text)
    parser.feed('Aktualny stan magazynu wynosi ')
    assert ''.join(captured) == 'Aktualny'
    parser.feed('dwadzieścia cztery sztuki tego produktu ')
    assert 'stan magazynu wynosi' in ''.join(captured)


def test_short_display_flushes_when_speech_metadata_begins_before_completion():
    from agent_runtime import _plain_response_text
    from agent_streaming import DisplayTextFilter
    captured = []
    parser = DisplayTextFilter(lambda _name, data: captured.append(data['delta']), _plain_response_text)
    parser.feed('Masz jedną sztukę. <spe')
    assert not captured
    parser.feed('ech_text mode="direct">')
    assert ''.join(captured) == 'Masz jedną sztukę.'
    parser.feed('Podsumowanie jeszcze powstaje')
    assert ''.join(captured) == 'Masz jedną sztukę.'


@pytest.mark.parametrize('chunk_size', [1, 3, 4096])
def test_display_filter_split_speech_tag_stays_hidden_after_literal_less_than(chunk_size):
    from agent_runtime import _plain_response_text, _split_final_response
    from agent_streaming import DisplayTextFilter
    raw = ('Aktualny stan jest < 10 sztuk produktu. To jest potwierdzona odpowiedź. '
           '<speech_text mode="direct">HIDDEN SPOKEN SUMMARY</speech_text>')
    captured = []
    parser = DisplayTextFilter(lambda _name, data: captured.append(data['delta']), _plain_response_text)
    for offset in range(0, len(raw), chunk_size):
        parser.feed(raw[offset:offset + chunk_size])
    assert '< 10' in ''.join(captured)
    assert all(value not in ''.join(captured) for value in ('speech_text', 'HIDDEN', 'mode='))
    screen, _, _ = _split_final_response(raw)
    parser.finish(screen)
    assert ''.join(captured) == _plain_response_text(screen)
