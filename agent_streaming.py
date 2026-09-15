"""Text-only SSE transport. Network consumers never own a database transaction."""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
import uuid

from flask import Response, copy_current_request_context
from agent_streaming_provider import StreamCancelled

logger = logging.getLogger(__name__)
EVENT_TYPES = frozenset({
    'turn_started', 'display_delta', 'speech_ready', 'tool_start', 'tool_result', 'done', 'error',
})


class StreamTrace:
    def __init__(self, started=None):
        self.started = time.perf_counter() if started is None else started
        self.turn_id = uuid.uuid4().hex
        self.points = {'request_received': 0.0}
        logger.info('AI_STREAM_TIMING %s', json.dumps({
            'turn_id': self.turn_id, 'point': 'request_received', 'elapsed_ms': 0.0,
        }, sort_keys=True))

    def mark(self, name):
        if name not in self.points:
            self.points[name] = round((time.perf_counter() - self.started) * 1000, 3)
            logger.info('AI_STREAM_TIMING %s', json.dumps({
                'turn_id': self.turn_id, 'point': name, 'elapsed_ms': self.points[name],
            }, sort_keys=True))


class DisplayTextFilter:
    """Keep speech metadata and incomplete credential tokens off the display stream.

    The final message remains authoritative. Whole-text presentation rules may
    retract a line (e.g. a technical field): stop appending if its safe prefix
    changes, and let the terminal event replace it. Do not emit unredacted deltas.
    """
    def __init__(self, emit, sanitize):
        self.emit = emit
        self.sanitize = sanitize
        self.raw = ''
        self.sent = ''
        self.frozen = False

    def feed(self, delta):
        self.raw += delta
        visible = self.raw
        screen_closed = False
        # Hold only speech metadata (including split tag prefixes), not ordinary
        # comparisons such as "stan < 10". The rest of the answer can stream.
        speech_tag = '<speech_text'
        for match in re.finditer('<', visible):
            suffix = visible[match.start():].casefold()
            if suffix.startswith(speech_tag) or speech_tag.startswith(suffix):
                visible = visible[:match.start()]
                screen_closed = bool(re.match(r'<speech_text(?:\s|>)', suffix))
                break
        if screen_closed:
            # The display portion has ended; even a three-word answer can now
            # appear while the provider is still generating its speech metadata.
            self._append(self.sanitize(visible))
            return
        # A pending Markdown link may later hide its entire destination. Never
        # expose an href while waiting for the closing delimiter, even when the
        # destination contains spaces. Keep complete links atomic at the later
        # lookahead boundary so sanitizing a prefix cannot reveal a partial URL.
        link_spans = []
        position = 0
        while (opening := visible.find('[', position)) >= 0:
            closing = visible.find(']', opening + 1)
            if closing < 0 or closing + 1 == len(visible):
                visible = visible[:opening]
                break
            if visible[closing + 1] == '(':
                end = visible.find(')', closing + 2)
                if end < 0:
                    visible = visible[:opening]
                    break
                link_spans.append((opening, end + 1))
                position = end + 1
            else:
                position = closing + 1
        words = list(re.finditer(r'\S+', visible))
        # Keep a short lookahead, not an arbitrary character truncate: complete
        # credential values must be seen by the same redactor as the final text.
        if len(words) < 4:
            return
        boundary = words[-3].start()
        for opening, end in link_spans:
            if opening < boundary < end:
                boundary = opening
                break
        self._append(self.sanitize(visible[:boundary]))

    def finish(self, screen_text):
        self._append(self.sanitize(screen_text))

    def _append(self, text):
        if self.frozen:
            return
        if not text.startswith(self.sent):
            self.frozen = True
            return
        delta = text[len(self.sent):]
        if delta:
            self.sent = text
            self.emit('display_delta', {'delta': delta})


def sse_response(run, finalize, *, trace=None):
    """Run the existing synchronous turn in its own request context.

    The queue is deliberately nonblocking. Runtime output is bounded by the
    existing answer/tool limits, so a slow browser cannot stall a commit/audit.
    Disconnect is cooperative: finish any in-flight BO, then release the turn.
    """
    trace = trace or StreamTrace()
    events = queue.SimpleQueue()
    cancelled = threading.Event()

    def emit(event, data):
        if event not in EVENT_TYPES:
            raise ValueError('Unknown agent stream event')
        if cancelled.is_set():
            return
        if event == 'display_delta':
            trace.mark('first_display_delta')
        events.put((event, data))

    @copy_current_request_context
    def work():
        try:
            result = run(emit, cancelled.is_set, trace)
            result, _status_code = finalize(result)
            result['stream_timings'] = dict(trace.points)
            # speech_ready is reserved. Stage 1 only returns final speech_text
            # inside done; the existing full-response TTS flow consumes it once.
            emit('done' if result.get('model_status', result.get('status')) == 'SUCCESS' else 'error', result)
        except Exception as exc:
            logger.error('AI_STREAM_ERROR %s', json.dumps({
                'turn_id': trace.turn_id, 'exception_type': type(exc).__name__,
            }, sort_keys=True))
            emit('error', {'ok': False, 'status': 'FAILED', 'error_code': 'STREAM_FAILED',
                           'message': 'Nie udało się zakończyć odpowiedzi. Sprawdź historię przed ponowieniem operacji.'})
        finally:
            events.put(None)

    def encode(event, data):
        return 'event: ' + event + '\ndata: ' + json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n\n'

    def generate():
        try:
            # Start only once WSGI starts consuming; never spawn an orphan worker
            # for a response that was closed without being iterated.
            yield encode('turn_started', {'turn_id': trace.turn_id, 'protocol_version': 1})
            if cancelled.is_set():
                return
            threading.Thread(target=work, name='agent-stream-' + trace.turn_id[:8], daemon=True).start()
            while True:
                try:
                    item = events.get(timeout=5)
                except queue.Empty:
                    yield ': keepalive\n\n'
                    continue
                if item is None:
                    return
                event, data = item
                if event in {'done', 'error'}:
                    trace.mark('done_sent')
                    data['stream_timings'] = dict(trace.points)
                yield encode(event, data)
        finally:
            cancelled.set()

    response = Response(generate(), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache, no-store, no-transform',
        'X-Accel-Buffering': 'no',
        'X-Agent-Stream-Version': '1',
    })
    response.call_on_close(cancelled.set)
    return response
