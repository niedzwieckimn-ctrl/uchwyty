import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import {webcrypto} from 'node:crypto';

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
                  sttText = 'jakie mam zaległe faktury?',
                  chatResponse, ttsResponse, audioPlayError = false,
                  noTracks = false, mediaRecorder = true, getStream} = {}) {
  const elements = Object.fromEntries(['aiForm','aiInput','aiSend','aiMessages','aiEmpty',
    'aiNewConversation','aiVoice','aiVoiceStatus'].map(id => [id, new Element()]));
  const calls = [], logs = [], recorders = [], audios = [];
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
  class Playback extends Element {
    constructor(url) { super(); this.url = url; this.paused = false; this.currentTime = 0; audios.push(this); }
    async play() {
      if (audioPlayError) throw Object.assign(new Error('playback'),
        {name:audioPlayError === true ? 'Error' : audioPlayError});
    }
    pause() { this.paused = true; }
  }
  const context = {
    document:{getElementById:id => elements[id], querySelectorAll:() => [], createElement:() => new Element()},
    window:{setTimeout, clearTimeout}, navigator:{mediaDevices:{getUserMedia:async () => {
      if (captureError) throw captureError;
      return getStream ? getStream(stream) : stream;
    }}}, MediaRecorder:mediaRecorder ? Recorder : undefined,
    Blob, FormData, AbortController, performance, Audio:Playback, crypto:webcrypto,
    URL:{createObjectURL:() => 'blob:voice-test', revokeObjectURL() {}},
    console:{info:(event, details) => logs.push({event, ...details})},
    fetch:async (url, options) => {
      calls.push({url, options});
      if (url.endsWith('/transcribe')) {
        if (sttResponse) return sttResponse();
        return {ok:true, status:200, json:async () => ({ok:true, text:sttText})};
      }
      if (url.endsWith('/synthesize')) {
        if (ttsResponse) return ttsResponse();
        return {ok:true, status:200, blob:async () => new Blob(['mp3'], {type:'audio/mpeg'})};
      }
      assert.equal(url, '/api/internal/ai/chat');
      if (chatResponse) return chatResponse();
      return {ok:true, status:200, json:async () => ({status:'SUCCESS', conversation_id:'existing-conversation',
        message:'Odpowiedź z istniejącego chatu.', artifacts:[]})};
    },
  };
  vm.runInNewContext(script, context);
  return {elements, calls, logs, recorders, audios, track, click:() => elements.aiVoice.dispatch('click'),
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
  assert.match(b.calls[1].options.body.get('capture_duration_ms'), /^\d+$/);
  assert.deepEqual(JSON.parse(b.calls[2].options.body), {message:'jakie mam zaległe faktury?',
    conversation_id:'existing-conversation'});
  const bubbles = b.elements.aiMessages.children.flatMap(row => row.children).map(child => child.textContent);
  assert.ok(bubbles.includes('jakie mam zaległe faktury?'));
  assert.equal(b.elements.aiVoiceStatus.textContent, 'Voice: błąd');
  assert.ok(b.logs.some(entry => entry.error_code === 'SPEECH_TEXT_MISSING'));
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

test('voice keeps full UI answer and automatically speaks only speech_text', async () => {
  const full = 'Masz 7 aktywnych zamówień. Najnowsze zamówienie wymaga uzupełnienia. Dalsza lista pozostaje na ekranie.';
  const speech = 'Masz jedno nowe zamówienie. Wymaga uzupełnienia.';
  const b = browser({sttText:'Mam jakieś nowe zamówienia?',
    chatResponse:async () => ({ok:true, status:200, json:async () => ({
    status:'SUCCESS', conversation_id:'voice-conversation', message:full, speech_text:speech, artifacts:[],
  })})});
  b.click(); await flush(); b.click(); await flush();
  assert.deepEqual(b.calls.map(call => call.url), ['/api/internal/ai/voice/transcribe',
    '/api/internal/ai/chat', '/api/internal/ai/voice/synthesize']);
  assert.equal(JSON.parse(b.calls[1].options.body).message, 'Mam jakieś nowe zamówienia?');
  assert.equal(JSON.parse(b.calls[2].options.body).speech_text, speech);
  const bubbles = b.elements.aiMessages.children.flatMap(row => row.children).map(child => child.textContent);
  assert.ok(bubbles.includes(full));
  assert.ok(speech.length < full.length);
  for (const event of ['VOICE_AGENT_RESPONSE','VOICE_TTS_REQUEST_START','VOICE_TTS_RESPONSE',
    'VOICE_ROUNDTRIP_COMPLETE']) assert.ok(b.logs.some(entry => entry.event === event));
});

test('voice daily briefing displays full sections and speaks the compact summary', async () => {
  const full = '1. Pilne wysyłki\n- Jedna wysyłka.\n2. Płatności\n- Brak.\n3. Braki\n- 13 sztuk.';
  const speech = 'Na dziś masz jedną pilną wysyłkę i 13 sztuk braków. Płatności po terminie brak.';
  const b = browser({sttText:'Co mam dziś do zrobienia?',
    chatResponse:async () => ({ok:true, status:200, json:async () => ({
      status:'SUCCESS', conversation_id:'daily-conversation', message:full, speech_text:speech, artifacts:[],
    })})});
  b.click(); await flush(); b.click(); await flush();
  assert.equal(JSON.parse(b.calls[1].options.body).message, 'Co mam dziś do zrobienia?');
  assert.equal(JSON.parse(b.calls[2].options.body).speech_text, speech);
  const bubbles = b.elements.aiMessages.children.flatMap(row => row.children).map(child => child.textContent);
  assert.ok(bubbles.includes(full));
  assert.notEqual(speech, full);
  assert.ok(!speech.includes('\n-'));
});

test('typed message never starts automatic TTS', async () => {
  const b = browser({chatResponse:async () => ({ok:true, status:200, json:async () => ({
    status:'SUCCESS', conversation_id:'text-conversation', message:'Pełna odpowiedź.',
    speech_text:'Krótka odpowiedź.', artifacts:[],
  })})});
  b.type('Wiadomość z klawiatury'); await flush();
  assert.deepEqual(b.calls.map(call => call.url), ['/api/internal/ai/chat']);
  assert.equal(b.audios.length, 0);
});

test('TTS error leaves the successful chat answer visible', async () => {
  const answer = 'Pełna odpowiedź nadal jest dostępna.';
  const b = browser({chatResponse:async () => ({ok:true, status:200, json:async () => ({
    status:'SUCCESS', conversation_id:'voice-conversation', message:answer,
    speech_text:'Krótka odpowiedź.', artifacts:[],
  })}), ttsResponse:async () => ({ok:false, status:503, blob:async () => new Blob()})});
  b.click(); await flush(); b.click(); await flush();
  const bubbles = b.elements.aiMessages.children.flatMap(row => row.children).map(child => child.textContent);
  assert.ok(bubbles.includes(answer));
  assert.equal(b.elements.aiVoiceStatus.textContent, 'Voice: błąd');
  assert.ok(b.logs.some(entry => entry.event === 'VOICE_TTS_ERROR' && entry.error_code === 'HTTP_503'));
});

test('voice button stops playback without changing chat history', async () => {
  const answer = 'Odpowiedź została wykonana i pozostaje w historii.';
  const b = browser({chatResponse:async () => ({ok:true, status:200, json:async () => ({
    status:'SUCCESS', conversation_id:'voice-conversation', message:answer,
    speech_text:'Odpowiedź została wykonana.', artifacts:[],
  })})});
  b.click(); await flush(); b.click(); await flush();
  const before = b.elements.aiMessages.children.length;
  assert.equal(b.elements.aiVoiceStatus.textContent, 'Voice: odtwarzanie…');
  b.click(); await flush();
  assert.equal(b.audios[0].paused, true);
  assert.equal(b.elements.aiVoiceStatus.textContent, 'Voice: gotowy');
  assert.equal(b.elements.aiMessages.children.length, before);
  assert.ok(b.logs.some(entry => entry.event === 'VOICE_TTS_STOP' && entry.stage === 'stopped'));
});

test('chat 403 stops the voice flow and keeps the error visible without calling TTS', async () => {
  const b = browser({chatResponse:async () => ({ok:false, status:403, json:async () => ({
    status:'DENIED', error_code:'TOOL_NOT_ALLOWED', message:'Ta operacja nie jest dostępna.',
    speech_text:'Odmowa.', conversation_id:'existing-conversation',
  })})});
  b.click(); await flush(); b.click(); await flush();
  assert.deepEqual(b.calls.map(call => call.url), ['/api/internal/ai/voice/transcribe', '/api/internal/ai/chat']);
  assert.equal(b.elements.aiVoiceStatus.textContent, 'Voice: błąd');
  assert.ok(b.logs.some(entry => entry.event === 'VOICE_AGENT_RESPONSE'
    && entry.http_status === 403 && entry.error_code === 'TOOL_NOT_ALLOWED'));
  assert.ok(b.logs.some(entry => entry.event === 'VOICE_TTS_SKIPPED' && entry.error_code === 'CHAT_REJECTED'));
  assert.ok(!b.logs.some(entry => entry.event === 'VOICE_ROUNDTRIP_COMPLETE'));
});

for (const [audioPlayError, expectedCode] of [[true, 'TTS_PLAYBACK_FAILED'], ['NotAllowedError', 'AUTOPLAY_BLOCKED']]) {
test(`audio.play rejection ${expectedCode} is diagnosed as playback`, async () => {
  const b = browser({audioPlayError, chatResponse:async () => ({
    ok:true, status:200, json:async () => ({
      status:'SUCCESS', conversation_id:'existing-conversation', message:'Odpowiedź.',
      speech_text:'Odpowiedź.',
    }),
  })});
  b.click(); await flush(); b.click(); await flush();
  assert.ok(b.logs.some(entry => entry.event === 'VOICE_TTS_RESPONSE' && entry.http_status === 200));
  assert.ok(b.logs.some(entry => entry.event === 'VOICE_TTS_ERROR'
    && entry.stage === 'playback' && entry.error_code === expectedCode));
  assert.equal(b.elements.aiVoiceStatus.textContent, 'Voice: błąd');
  assert.ok(!b.logs.some(entry => entry.event === 'VOICE_ROUNDTRIP_COMPLETE'));
});
}

test('identical typed and voice messages share URL, JSON, credentials, CSRF and conversation', async () => {
  const message = 'Co mam dziś do zrobienia?';
  const b = browser({sttText:message});
  b.type('Cześć'); await flush();
  b.type(message); await flush();
  b.click(); await flush(); b.click(); await flush();
  const chats = b.calls.filter(call => call.url === '/api/internal/ai/chat');
  const typed = chats[1], voice = chats[2];
  assert.equal(typed.url, voice.url);
  for (const key of ['method','credentials','body']) assert.equal(typed.options[key], voice.options[key]);
  for (const key of ['Content-Type','X-CSRF-Token']) assert.equal(typed.options.headers[key], voice.options.headers[key]);
  assert.equal(voice.options.headers['X-CSRF-Token'], 'rendered-csrf');
  assert.deepEqual(JSON.parse(voice.options.body), {message, conversation_id:'existing-conversation'});
  assert.ok(voice.options.headers['X-Request-ID']);
});

test('one request id and timing breakdown connect the entire voice roundtrip', async () => {
  const b = browser({chatResponse:async () => ({ok:true, status:200, json:async () => ({
    status:'SUCCESS', message:'Wynik pozostaje na ekranie.', speech_text:'Wynik jest gotowy.',
  })})});
  b.click(); await flush(); b.click(); await flush();
  const requestId = b.calls[0].options.headers['X-Request-ID'];
  for (const call of b.calls) assert.equal(call.options.headers['X-Request-ID'], requestId);
  const events = b.logs.filter(entry => ['VOICE_STT_RESPONSE','VOICE_AGENT_RESPONSE',
    'VOICE_TTS_RESPONSE','VOICE_ROUNDTRIP_COMPLETE'].includes(entry.event));
  assert.deepEqual(events.map(entry => entry.event), ['VOICE_STT_RESPONSE','VOICE_AGENT_RESPONSE',
    'VOICE_TTS_RESPONSE','VOICE_ROUNDTRIP_COMPLETE']);
  for (const entry of events) {
    assert.equal(entry.request_id, requestId);
    assert.equal(entry.http_status, 200);
    assert.ok(entry.latency_ms >= 0);
  }
  const total = events.at(-1);
  for (const key of ['stt_ms','agent_ms','tts_ms']) assert.ok(total[key] >= 0);
  assert.ok(b.logs.some(entry => entry.event === 'VOICE_TTS_RESPONSE'
    && entry.mime_type === 'audio/mpeg' && entry.blob_size > 0));
});
