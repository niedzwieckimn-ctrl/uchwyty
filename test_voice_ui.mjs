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
                  sttText = 'jakie mam zaległe faktury?',
                  chatResponse, ttsResponse, audioPlayError = false,
                  firstAudioAlwaysBlocked = false, noTracks = false, mediaRecorder = true, getStream} = {}) {
  const elements = Object.fromEntries(['aiForm','aiInput','aiSend','aiMessages','aiEmpty',
    'aiNewConversation','aiVoice','aiVoiceStatus','aiReplayVoice','aiVoiceDebug'].map(id => [id, new Element()]));
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
  let remainingPlayErrors = audioPlayError ? 1 : 0;
  class Playback extends Element {
    constructor(url) {
      super(); this.url = url; this.src = url; this.paused = false; this.currentTime = 0;
      this.playAttempts = 0; this.readyState = 4; this.networkState = 1; this.error = null;
      this.duration = 1.25; this.muted = false; this.volume = 1; audios.push(this);
    }
    async play() {
      this.playAttempts += 1;
      if (firstAudioAlwaysBlocked && this === audios[0]) {
        throw Object.assign(new Error('Initial media element remains blocked.'), {name:'NotAllowedError'});
      }
      if (remainingPlayErrors > 0) {
        remainingPlayErrors -= 1;
        throw audioPlayError === true ? new Error('playback') : audioPlayError;
      }
    }
    pause() { this.paused = true; }
  }
  const context = {
    document:{getElementById:id => elements[id], querySelectorAll:() => [], createElement:() => new Element()},
    window:{setTimeout, clearTimeout}, navigator:{mediaDevices:{getUserMedia:async () => {
      if (captureError) throw captureError;
      return getStream ? getStream(stream) : stream;
    }}}, MediaRecorder:mediaRecorder ? Recorder : undefined,
    Blob, FormData, AbortController, performance, Audio:Playback,
    URL:{createObjectURL:() => 'blob:voice-test', revokeObjectURL() {}},
    console:{info:(event, details) => logs.push({event, ...(typeof details === 'string' ? JSON.parse(details) : details)})},
    fetch:async (url, options) => {
      calls.push({url, options});
      if (url.endsWith('/transcribe')) {
        if (sttResponse) return sttResponse();
        return {ok:true, status:200, json:async () => ({ok:true, text:sttText})};
      }
      if (url.endsWith('/synthesize')) {
        if (ttsResponse) return ttsResponse();
        return {ok:true, status:200,
          headers:{get:name => name.toLowerCase() === 'content-type' ? 'audio/mpeg'
            : name.toLowerCase() === 'content-length' ? '3' : ''},
          blob:async () => new Blob(['mp3'], {type:'audio/mpeg'})};
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
    assert.equal(b.elements.aiVoiceStatus.textContent, 'Voice: błąd STT');
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
    'VOICE_PLAYBACK_START','VOICE_PLAYBACK_SUCCESS','VOICE_ROUNDTRIP_COMPLETE']) {
    assert.ok(b.logs.some(entry => entry.event === event));
  }
  assert.equal(b.calls.filter(call => call.url.endsWith('/synthesize')).length, 1);
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
  assert.equal(b.elements.aiVoiceStatus.textContent, 'Voice: tekst OK, błąd TTS');
  assert.ok(b.logs.some(entry => entry.event === 'VOICE_TTS_ERROR' && entry.error_code === 'HTTP_503'));
});

test('playback NotAllowedError keeps answer and offers one-click replay without a second TTS', async () => {
  const blocked = Object.assign(new Error('Autoplay is not allowed.'), {name:'NotAllowedError'});
  const answer = 'Odpowiedź tekstowa pozostaje widoczna.';
  const b = browser({audioPlayError:blocked, chatResponse:async () => ({ok:true, status:200, json:async () => ({
    status:'SUCCESS', conversation_id:'voice-conversation', message:answer,
    speech_text:'Odpowiedź głosowa.', artifacts:[],
  })})});
  b.click(); await flush(); b.click(); await flush();
  const bubbles = b.elements.aiMessages.children.flatMap(row => row.children).map(child => child.textContent);
  assert.ok(bubbles.includes(answer));
  assert.equal(b.elements.aiVoiceStatus.textContent, 'Voice: tekst OK, odtwarzanie zablokowane');
  assert.equal(b.elements.aiReplayVoice.hidden, false);
  assert.equal(b.elements.aiReplayVoice.disabled, false);
  const error = b.logs.find(entry => entry.event === 'VOICE_PLAYBACK_ERROR');
  assert.equal(error.error_name, 'NotAllowedError');
  assert.equal(error.error_message, 'Autoplay is not allowed.');
  b.elements.aiReplayVoice.dispatch('click'); await flush();
  assert.equal(b.audios.length, 2);
  assert.equal(b.audios[0].url, b.audios[1].url);
  assert.equal(b.audios[1].playAttempts, 1);
  assert.equal(b.elements.aiVoiceStatus.textContent, 'Voice: odtwarzanie…');
  assert.equal(b.elements.aiReplayVoice.hidden, true);
  assert.equal(b.calls.filter(call => call.url.endsWith('/synthesize')).length, 1);
  const click = b.logs.find(entry => entry.event === 'VOICE_MANUAL_PLAYBACK_CLICK');
  assert.equal(click.handler_invoked, true);
  assert.equal(click.object_url_present, true);
  assert.equal(click.object_url_revoked, false);
  assert.equal(click.audio_src, 'blob:voice-test');
  assert.equal(click.ready_state, 4);
  assert.equal(click.network_state, 1);
  assert.equal(click.audio_error_code, null);
  assert.equal(click.audio_duration, 1.25);
  assert.equal(click.muted, false);
  assert.equal(click.volume, 1);
  assert.ok(b.logs.some(entry => entry.event === 'VOICE_MANUAL_PLAYBACK_SUCCESS'));
});

test('manual replay replaces an audio element that remains unusable after autoplay rejection', async () => {
  const b = browser({firstAudioAlwaysBlocked:true, chatResponse:async () => ({ok:true, status:200, json:async () => ({
    status:'SUCCESS', conversation_id:'voice-conversation', message:'Tekst odpowiedzi.',
    speech_text:'Głos odpowiedzi.', artifacts:[],
  })})});
  b.click(); await flush(); b.click(); await flush();
  assert.equal(b.elements.aiVoiceStatus.textContent, 'Voice: tekst OK, odtwarzanie zablokowane');
  b.elements.aiReplayVoice.dispatch('click'); await flush();
  assert.equal(b.audios.length, 2);
  assert.equal(b.audios[0].url, b.audios[1].url);
  assert.equal(b.elements.aiVoiceStatus.textContent, 'Voice: odtwarzanie…');
  assert.ok(b.logs.some(entry => entry.event === 'VOICE_MANUAL_PLAYBACK_SUCCESS'));
});

test('agent failure has a separate voice status and does not call TTS', async () => {
  const b = browser({chatResponse:async () => ({ok:false, status:503, json:async () => ({
    status:'FAILED', error_code:'MODEL_ERROR', message:'Nie udało się odpowiedzieć.',
  })})});
  b.click(); await flush(); b.click(); await flush();
  assert.equal(b.elements.aiVoiceStatus.textContent, 'Voice: błąd agenta');
  assert.equal(b.calls.filter(call => call.url.endsWith('/synthesize')).length, 0);
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
