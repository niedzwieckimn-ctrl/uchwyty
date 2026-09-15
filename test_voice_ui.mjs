import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const template = readFileSync(new URL('./templates/ai_assistant.html', import.meta.url), 'utf8');
const script = template.match(/<script>([\s\S]*?)<\/script>/)[1];

class Element {
  constructor() {
    this.listeners = {}; this.children = []; this.dataset = {}; this.value = '';
    this.disabled = false; this.textContent = ''; this.attributes = {};
    this.classList = {toggle() {}};
  }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  dispatch(name, extra = {}) { for (const cb of this.listeners[name] || []) cb({preventDefault() {}, ...extra}); }
  setAttribute(name, value) { this.attributes[name] = value; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.append(child); }
  remove() { this.removed = true; }
  focus() {}
  querySelector(selector) {
    if (selector === 'input[name="csrf_token"]') return {value:'rendered-csrf'};
    return this.children.find(child => child.className === selector.slice(1)) || null;
  }
  querySelectorAll() { return []; }
  requestSubmit() { this.dispatch('submit'); }
}

const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); };

function browser({types = ['audio/webm;codecs=opus'], captureError, chunks = ['recording'], sttResponse,
                  noTracks = false, mediaRecorder = true, getStream} = {}) {
  const elements = Object.fromEntries(['aiForm','aiInput','aiSend','aiMessages','aiEmpty',
    'aiNewConversation','aiVoice','aiVoiceStatus'].map(id => [id, new Element()]));
  const calls = [], logs = [], recorders = [];
  const track = {readyState:'live', stopped:false, stop() { this.stopped = true; }};
  const stream = {getAudioTracks:() => noTracks ? [] : [track], getTracks:() => [track]};
  class Recorder extends Element {
    static isTypeSupported(type) { return types.includes(type); }
    constructor(_stream, options) {
      super(); this.mimeType = options.mimeType; this.state = 'inactive'; recorders.push(this);
    }
    start() { this.state = 'recording'; }
    stop() {
      this.state = 'inactive';
      queueMicrotask(() => {
        this.dispatch('dataavailable', {data:new Blob(chunks, {type:this.mimeType})});
        this.dispatch('stop');
      });
    }
  }
  const context = {
    document:{getElementById:id => elements[id], querySelectorAll:() => [], createElement:() => new Element()},
    window:{setTimeout, clearTimeout}, navigator:{mediaDevices:{getUserMedia:async () => {
      if (captureError) throw captureError;
      return getStream ? getStream(stream) : stream;
    }}}, MediaRecorder:mediaRecorder ? Recorder : undefined,
    Blob, FormData, AbortController, performance, URL, console:{info:(event, details) => logs.push({event, ...details})},
    fetch:async (url, options) => {
      calls.push({url, options});
      if (url.endsWith('/transcribe')) {
        if (sttResponse) return sttResponse();
        return {ok:true, status:200, json:async () => ({ok:true, text:'jakie mam zaległe faktury?'})};
      }
      assert.equal(url, '/api/internal/ai/chat');
      return {ok:true, status:200, json:async () => ({status:'SUCCESS', conversation_id:'existing-conversation',
        message:'Odpowiedź z istniejącego chatu.', artifacts:[]})};
    },
  };
  vm.runInNewContext(script, context);
  return {elements, calls, logs, recorders, track, click:() => elements.aiVoice.dispatch('click'),
    type:message => { elements.aiInput.value = message; elements.aiForm.dispatch('submit'); }};
}

test('click start/stop -> STT -> same chat and conversation, transcript visible', async () => {
  const b = browser();
  b.type('Cześć'); await flush();
  b.click(); await flush();
  assert.equal(b.elements.aiVoiceStatus.textContent, 'Voice: słucham...');
  assert.equal(b.recorders[0].state, 'recording');
  assert.equal(b.calls.length, 1); // Releasing a click does not stop recording.
  b.click(); await flush();
  assert.deepEqual(b.calls.map(call => call.url), ['/api/internal/ai/chat',
    '/api/internal/ai/voice/transcribe', '/api/internal/ai/chat']);
  assert.equal(b.calls[1].options.headers['X-CSRF-Token'], 'rendered-csrf');
  assert.deepEqual(JSON.parse(b.calls[2].options.body), {message:'jakie mam zaległe faktury?',
    conversation_id:'existing-conversation'});
  const bubbles = b.elements.aiMessages.children.flatMap(row => row.children).map(child => child.textContent);
  assert.ok(bubbles.includes('jakie mam zaległe faktury?'));
  assert.equal(b.elements.aiVoiceStatus.textContent, 'Voice: gotowy');
  assert.equal(b.track.stopped, true);
  for (const event of ['VOICE_CAPTURE_START','VOICE_CAPTURE_READY','VOICE_CAPTURE_STOP',
    'VOICE_BLOB_READY','VOICE_STT_REQUEST_START','VOICE_STT_RESPONSE']) {
    assert.ok(b.logs.some(entry => entry.event === event));
  }
  assert.ok(!JSON.stringify(b.logs).includes('zaległe faktury'));
});

for (const [type, filename] of [['audio/webm','recording.webm'], ['audio/mp4','recording.mp4']]) {
  test(`supported MIME fallback ${type} keeps matching upload extension`, async () => {
    const b = browser({types:[type]});
    b.click(); await flush(); b.click(); await flush();
    const audio = b.calls[0].options.body.get('audio');
    assert.equal(audio.type, type); assert.equal(audio.name, filename);
  });
}

for (const [options, code] of [
  [{captureError:{name:'NotAllowedError', message:'private exception'}}, 'MICROPHONE_PERMISSION_DENIED'],
  [{noTracks:true}, 'NO_AUDIO_TRACKS'],
  [{mediaRecorder:false}, 'MEDIA_RECORDER_UNSUPPORTED'],
  [{types:[]}, 'UNSUPPORTED_AUDIO_TYPE'],
]) {
  test(`${code} is diagnosed and normal chat stays usable`, async () => {
    const b = browser(options); b.click(); await flush();
    assert.equal(b.elements.aiVoiceStatus.textContent, 'Voice: błąd');
    assert.ok(b.logs.some(entry => entry.error_code === code));
    assert.ok(!JSON.stringify(b.logs).includes('private exception'));
    assert.equal(b.calls.length, 0);
    b.type('Normalny chat'); await flush();
    assert.equal(b.calls[0].url, '/api/internal/ai/chat');
  });
}

test('empty blob is never uploaded', async () => {
  const b = browser({chunks:[]}); b.click(); await flush(); b.click(); await flush();
  assert.equal(b.calls.length, 0);
  assert.ok(b.logs.some(entry => entry.error_code === 'EMPTY_AUDIO' && entry.blob_size === 0));
});

for (const status of [401, 403, 404, 405, 503]) {
  test(`non-JSON HTTP ${status} preserves status in diagnostics and leaves chat usable`, async () => {
    const b = browser({sttResponse:() => ({ok:false, status, json:async () => { throw new Error('private HTML'); }})});
    b.click(); await flush(); b.click(); await flush();
    assert.ok(b.logs.some(entry => entry.error_code === `HTTP_${status}` && entry.http_status === status));
    assert.equal(b.calls.length, 1);
    b.type('Normalny chat'); await flush();
    assert.equal(b.calls[1].url, '/api/internal/ai/chat');
    assert.ok(!JSON.stringify(b.logs).includes('private HTML'));
  });
}

test('invalid transcript is rejected before chat', async () => {
  const b = browser({sttResponse:() => ({ok:true, status:200, json:async () => ({text:{unexpected:true}})})});
  b.click(); await flush(); b.click(); await flush();
  assert.ok(b.logs.some(entry => entry.error_code === 'STT_INVALID_RESPONSE'));
  assert.equal(b.calls.length, 1);
});

test('new conversation cancels pending microphone permission without recording or sending', async () => {
  let allow;
  const b = browser({getStream:stream => new Promise(resolve => { allow = () => resolve(stream); })});
  b.click(); await flush();
  b.elements.aiNewConversation.dispatch('click'); await flush();
  allow(); await flush();
  assert.equal(b.recorders.length, 0); assert.equal(b.calls.length, 0);
  assert.equal(b.track.stopped, true);
});
