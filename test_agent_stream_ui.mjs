import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const template = readFileSync(new URL('./templates/ai_assistant.html', import.meta.url), 'utf8');
const script = template.match(/<script>([\s\S]*?)<\/script>/)[1];
const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); };
const encoder = new TextEncoder();
const event = (name, data, newline = '\n') => `event: ${name}${newline}data: ${JSON.stringify(data)}${newline}${newline}`;
const finalData = (message = 'Masz jedną sztukę.') => ({status:'SUCCESS', conversation_id:'conversation-1',
  message, speech_text:'Masz jedną sztukę.', artifacts:[]});

class Element {
  constructor() {
    this.listeners = {}; this.children = []; this.dataset = {}; this.value = '';
    this.disabled = false; this.hidden = false; this._text = ''; this.className = '';
    this.classList = {toggle:(name, enabled) => {
      const names = new Set(this.className.split(/\s+/).filter(Boolean));
      if (enabled) names.add(name); else names.delete(name);
      this.className = [...names].join(' ');
    }, add:(name) => this.classList.toggle(name, true)};
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  get childElementCount() { return this.children.length; }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  dispatch(name, extra = {}) { for (const callback of this.listeners[name] || []) callback({preventDefault() {}, ...extra}); }
  setAttribute() {}
  append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
  appendChild(child) { this.append(child); return child; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); }
  focus() {}
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    if (selector === 'input[name="csrf_token"]') return [{value:'csrf'}];
    const matches = child => selector === '[data-approval-id]' ? Boolean(child.dataset.approvalId)
      : selector.startsWith('.') ? child.className.split(/\s+/).includes(selector.slice(1))
      : child.tagName === selector;
    return this.children.flatMap(child => [...(matches(child) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
}

function controlledStream() {
  let controller, cancelled = false;
  const body = new ReadableStream({start(value) { controller = value; }, cancel() { cancelled = true; }});
  return {
    response:{ok:true, status:200, headers:{get:() => 'text/event-stream; charset=utf-8'}, body},
    send(text) { controller.enqueue(encoder.encode(text)); },
    bytes(bytes) { controller.enqueue(bytes); },
    close() { controller.close(); },
    get cancelled() { return cancelled; },
  };
}

function browser({chat, approval, streaming = true} = {}) {
  const elements = Object.fromEntries(['aiForm','aiInput','aiSend','aiMessages','aiEmpty',
    'aiNewConversation','aiVoice','aiVoiceStatus','aiReplayVoice','aiVoiceDebug'].map(id => [id, new Element()]));
  const calls = [], logs = [], audios = [], windowListeners = {};
  let expireTurn;
  class Audio extends Element {
    constructor(src) { super(); this.src = src; this.readyState = 4; this.networkState = 1;
      this.duration = 1; this.currentTime = 0; this.volume = 1; this.error = null; audios.push(this); }
    play() { return Promise.resolve(); }
    pause() {}
  }
  const context = {
    document:{getElementById:id => elements[id], querySelectorAll:() => [], createElement:tag => {
      const element = new Element(); element.tagName = tag; return element;
    }},
    window:{setTimeout:(callback, delay) => {
      if (delay === 120000) expireTurn = callback;
      return setTimeout(callback, delay);
    }, clearTimeout, addEventListener:(name, callback) => { windowListeners[name] = callback; }},
    navigator:{}, Blob, AbortController, performance, Audio,
    TextDecoder:streaming ? TextDecoder : undefined, ReadableStream:streaming ? ReadableStream : undefined,
    URL:{createObjectURL:() => 'blob:stream-test', revokeObjectURL() {}},
    console:{info:(name, details) => logs.push({name, ...JSON.parse(details)})},
    fetch:async (url, options) => {
      calls.push({url, options});
      if (url.endsWith('/synthesize')) return {ok:true, status:200,
        headers:{get:() => 'audio/mpeg'}, blob:async () => new Blob(['audio'], {type:'audio/mpeg'})};
      if (url.endsWith('/conversation/reset')) return {ok:true};
      if (url.includes('/approvals/')) {
        assert.ok(approval, 'Unexpected approval request');
        return approval(options);
      }
      assert.equal(url, '/api/internal/ai/chat');
      return chat ? chat(options) : {ok:true, status:200, json:async () => finalData()};
    },
  };
  // Expose the existing entry point only inside this VM. STT/playback themselves
  // retain their separate regression suite; this suite tests chat-origin rules.
  vm.runInNewContext(script.replace(/\}\)\(\);\s*$/, 'globalThis.submitForTest = submitMessage;\n})();'), context);
  return {elements, calls, logs, audios,
    submit:(message, voice = false) => context.submitForTest(message, voice),
    newConversation:() => elements.aiNewConversation.dispatch('click'),
    pagehide:() => windowListeners.pagehide(),
    timeout:() => expireTurn(),
    assistant:() => elements.aiMessages.querySelectorAll('.assistant'),
    text:() => elements.aiMessages.querySelectorAll('.assistant').map(row => row.querySelector('.ai-bubble').textContent),
  };
}

test('UTF-8 byte fragments, CRLF, comments and multiline SSE data render before done in one safe bubble', async () => {
  const stream = controlledStream(); const b = browser({chat:() => stream.response});
  const pending = b.submit('Ile mam?'); await flush();
  assert.equal(b.calls[0].options.headers.Accept, 'text/event-stream');
  const first = ': keepalive\r\n\r\n' + event('turn_started', {turn_id:'turn-1'}, '\r\n')
    + 'event: display_delta\r\ndata: {\r\ndata: "delta":"Zażółć <script>"}\r\n\r\n';
  for (const byte of encoder.encode(first)) stream.bytes(Uint8Array.of(byte));
  await flush();
  assert.deepEqual(b.text(), ['Zażółć <script>']);
  assert.equal(b.assistant().length, 1);
  assert.equal(b.elements.aiSend.disabled, true);
  assert.equal(b.assistant()[0].querySelector('.ai-bubble').children.length, 0);
  const metrics = b.logs.filter(entry => entry.event === 'frontend_first_delta_rendered');
  assert.equal(metrics.length, 1); assert.equal(metrics[0].turn_id, 'turn-1');
  assert.ok(metrics[0].elapsed_ms >= 0); assert.ok(metrics[0].monotonic_ms > 0);
  assert.ok(!JSON.stringify(b.logs).includes('Zażółć'));
  stream.send(event('display_delta', {delta:' gęślą.'}) + event('done', finalData('Zażółć <script> gęślą.')));
  await pending;
  assert.deepEqual(b.text(), ['Zażółć <script> gęślą.']);
  assert.equal(b.assistant().length, 1); assert.equal(b.elements.aiSend.disabled, false);
  assert.equal(stream.cancelled, true); assert.equal(b.audios.length, 0);
});

test('done replaces provisional text and adds structured artifacts exactly once', async () => {
  const stream = controlledStream(); const b = browser({chat:() => stream.response});
  const pending = b.submit('Produkt'); await flush();
  stream.send(event('display_delta', {delta:'Częściowa '}) + event('done', {...finalData('Wynik końcowy.'),
    artifacts:[{type:'product_card', model:'Tom', stock:108}]}) + event('done', finalData('Duplikat.')));
  await pending;
  assert.equal(b.assistant().length, 1);
  assert.ok(b.text()[0].startsWith('Wynik końcowy.'));
  assert.ok(!b.text()[0].includes('Częściowa')); assert.ok(!b.text()[0].includes('Duplikat'));
  assert.equal(b.assistant()[0].querySelectorAll('.ai-artifacts').length, 1);
});

for (const voice of [false, true]) {
  test(`speech_ready/tool events never play audio; successful done speaks ${voice ? 'once for voice' : 'zero times for typed'}`, async () => {
    const stream = controlledStream(); const b = browser({chat:() => stream.response});
    const pending = b.submit('Ile mam?', voice); await flush();
    stream.send(event('speech_ready', {speech_text:'Zarezerwowany komunikat.'})
      + event('tool_start', {arguments:'INTERNAL_SECRET'}) + event('tool_result', {result:'INTERNAL_SECRET'})
      + event('display_delta', {delta:'Masz jedną'}));
    await flush(); assert.equal(b.calls.length, 1); assert.ok(!b.text()[0].includes('INTERNAL_SECRET'));
    stream.send(event('done', finalData()) + event('done', finalData())); await pending; await flush();
    assert.equal(b.calls.filter(call => call.url.endsWith('/synthesize')).length, voice ? 1 : 0);
    assert.equal(b.audios.length, voice ? 1 : 0);
    if (voice) assert.equal(b.elements.aiVoiceStatus.textContent, 'Voice: odtwarzanie…');
  });
}

test('inventory speech_ready starts short TTS before done and does not repeat it', async t => {
  const stream = controlledStream(); const b = browser({chat:() => stream.response});
  const started = performance.now();
  const pending = b.submit('Cerne 128 BB 5', true); await flush();
  assert.equal(JSON.parse(b.calls[0].options.body).voice_fast_mode, true);
  stream.send(event('display_delta', {delta:'Cerne 128 BB — system 1, policzono 5, różnica +4.'})
    + event('speech_ready', {tts_text:'System jeden. Różnica plus cztery. Zapisać?',
      mode:'inventory_voice_fast'}));
  await flush();
  const ttsRequestMs = performance.now() - started;
  assert.equal(b.calls.filter(call => call.url.endsWith('/synthesize')).length, 1);
  assert.equal(b.elements.aiSend.disabled, true);
  await new Promise(resolve => setTimeout(resolve, 30));
  stream.send(event('done', {...finalData('Cerne 128 BB — system 1, policzono 5, różnica +4.'),
    tts_text:'System jeden. Różnica plus cztery. Zapisać?', inventory_fast_trace_ms:{turn_complete:30}}));
  await pending; await flush();
  assert.equal(b.calls.filter(call => call.url.endsWith('/synthesize')).length, 1);
  t.diagnostic(`Simulated browser: TTS request ${ttsRequestMs.toFixed(2)} ms, done ${(performance.now()-started).toFixed(2)} ms. No live STT or TTS call.`);
});

test('inventory fail-fast unlocks input and starts TTS before done', async t => {
  const stream = controlledStream(); const b = browser({chat:() => stream.response});
  const started = performance.now();
  const pending = b.submit('Cerne 128 XB pięć', true); await flush();
  stream.send(event('display_delta', {delta:'Nie znaleziono jednoznacznego produktu. Powtórz.'})
    + event('speech_ready', {tts_text:'Nie znalazłem. Powtórz.',
      mode:'inventory_voice_fast', retry_ready:true}));
  await flush();
  assert.equal(b.elements.aiSend.disabled, false);
  assert.equal(b.calls.filter(call => call.url.endsWith('/synthesize')).length, 1);
  assert.ok(b.logs.some(log => log.name === 'VOICE_FAST_INPUT_UNLOCKED'));
  const unlockedMs = performance.now() - started;
  assert.equal(b.elements.aiMessages.querySelectorAll('.assistant').length, 1);
  b.elements.aiVoice.dispatch('click'); await flush();
  assert.ok(b.logs.some(log => log.name === 'VOICE_CAPTURE_START'));
  stream.send(event('done', {...finalData('Nie znaleziono jednoznacznego produktu. Powtórz.'),
    tts_text:'Nie znalazłem. Powtórz.', inventory_fast_failure:'product_not_found'}));
  await pending; await flush();
  assert.equal(b.calls.filter(call => call.url.endsWith('/synthesize')).length, 1);
  t.diagnostic(`Simulated fail-fast browser: input unlocked and TTS requested ${unlockedMs.toFixed(2)} ms after submit, before done. No live STT/TTS.`);
});

for (const failure of ['error', 'error-with-success-status', 'eof', 'invalid-json', 'invalid-utf8']) {
  test(`${failure} terminates visibly, preserves previous messages and never retries POST or starts TTS`, async () => {
    const stream = controlledStream(); let requests = 0;
    const b = browser({chat:() => ++requests === 1
      ? {ok:true, status:200, json:async () => finalData('Poprzednia odpowiedź.')}
      : stream.response});
    await b.submit('Poprzednie pytanie');
    const pending = b.submit('Bieżące pytanie', true); await flush();
    stream.send(event('display_delta', {delta:'Niedokończona odpowiedź'})); await flush();
    if (failure === 'error') stream.send(event('error', {status:'FAILED', error_code:'PROVIDER_FAILED'}));
    if (failure === 'error-with-success-status') stream.send(event('error', finalData()));
    if (failure === 'eof') stream.close();
    if (failure === 'invalid-json') stream.send('event: display_delta\ndata: {broken}\n\n');
    if (failure === 'invalid-utf8') stream.bytes(Uint8Array.of(0xff));
    await pending;
    assert.equal(b.text()[0], 'Poprzednia odpowiedź.');
    assert.ok(!b.text()[1].includes('Niedokończona'));
    assert.match(b.assistant()[1].className, /error/);
    assert.equal(b.calls.length, 2); assert.equal(b.audios.length, 0);
    assert.equal(b.elements.aiVoiceStatus.textContent, 'Voice: błąd agenta');
    assert.equal(b.elements.aiSend.disabled, false);
  });
}

test('New conversation cancels the reader and permits a new turn while old finalization cannot unlock it', async () => {
  const first = controlledStream(), second = controlledStream(); let requests = 0;
  const b = browser({chat:() => ++requests === 1 ? first.response : second.response});
  const oldPending = b.submit('Stare pytanie', true); await flush();
  first.send(event('turn_started', {turn_id:'old', conversation_id:'old-conversation'})
    + event('display_delta', {delta:'Stary tekst'})); await flush();
  assert.equal(b.elements.aiNewConversation.disabled, false);
  b.newConversation();
  const newPending = b.submit('Nowe pytanie'); await flush(); await oldPending;
  assert.equal(b.calls[0].options.signal.aborted, true); assert.equal(first.cancelled, true);
  assert.equal(b.elements.aiSend.disabled, true);
  assert.ok(!b.text().some(value => value.includes('Stary')));
  const chats = b.calls.filter(call => call.url.endsWith('/chat'));
  assert.equal(JSON.parse(chats[1].options.body).conversation_id, '');
  second.send(event('done', {...finalData('Nowa odpowiedź.'), conversation_id:'new-conversation'})); await newPending;
  assert.deepEqual(b.text(), ['Nowa odpowiedź.']); assert.equal(b.audios.length, 0);
});

test('late JSON response after cancellation cannot replace the new conversation or trigger Voice', async () => {
  let resolveOld; let requests = 0;
  const b = browser({chat:() => ++requests === 1 ? new Promise(resolve => { resolveOld = resolve; })
    : {ok:true, status:200, json:async () => finalData('Nowa odpowiedź.')}});
  const pending = b.submit('Stara wiadomość', true); await flush(); b.newConversation();
  await b.submit('Nowa wiadomość');
  resolveOld({ok:true, status:200, json:async () => finalData('Spóźniona odpowiedź.')}); await pending;
  assert.deepEqual(b.text(), ['Nowa odpowiedź.']); assert.equal(b.audios.length, 0);
  assert.equal(b.calls.filter(call => call.url.endsWith('/chat')).length, 2);
});

test('pagehide aborts active transport without a late error bubble', async () => {
  const stream = controlledStream(); const b = browser({chat:() => stream.response});
  const pending = b.submit('Pytanie'); await flush(); b.pagehide(); await pending;
  assert.equal(b.calls[0].options.signal.aborted, true); assert.equal(stream.cancelled, true);
  assert.equal(b.assistant().filter(row => row.className.includes('error')).length, 0);
});

test('request timeout terminates pending stream visibly and does not retry', async () => {
  const stream = controlledStream(); const b = browser({chat:() => stream.response});
  const pending = b.submit('Pytanie'); await flush(); b.timeout(); await pending;
  assert.equal(b.calls[0].options.signal.aborted, true); assert.equal(stream.cancelled, true);
  assert.equal(b.calls.length, 1); assert.match(b.assistant()[0].className, /error/);
  assert.equal(b.elements.aiSend.disabled, false);
});

const approvalData = () => ({...finalData('Zmiana wymaga zatwierdzenia.'), approvals:[{
  approval_id:'approval-1', operation:'inventory.adjust', product_name:'Tom', from_quantity:108, to_quantity:100,
}]});

test('approval-generated final answer uses the same stream and leaves business decision disabled after success', async () => {
  const stream = controlledStream();
  const b = browser({chat:() => ({ok:true, status:200, json:async () => approvalData()}), approval:() => stream.response});
  await b.submit('Przygotuj korektę');
  const buttons = b.elements.aiMessages.querySelector('[data-approval-id]').querySelectorAll('button');
  const pending = buttons[0].onclick(); await flush();
  assert.equal(b.calls[1].options.headers.Accept, 'text/event-stream');
  assert.equal(JSON.parse(b.calls[1].options.body).conversation_id, 'conversation-1');
  stream.send(event('turn_started', {turn_id:'approval-turn'}) + event('display_delta', {delta:'Stan zmieniono'}));
  await flush(); assert.equal(b.text().at(-1), 'Stan zmieniono');
  assert.equal(b.elements.aiSend.disabled, true);
  stream.send(event('done', {status:'SUCCESS', model_status:'SUCCESS', conversation_id:'conversation-1',
    message:'Stan zmieniono ze 108 na 100.', speech_text:'Gotowe.', execution_outcome:{execution_status:'SUCCESS'}}));
  await pending;
  assert.equal(b.text().at(-1), 'Stan zmieniono ze 108 na 100.');
  assert.equal(b.assistant().length, 3); assert.ok(buttons.every(button => button.disabled));
  assert.equal(b.calls.length, 2); assert.equal(b.audios.length, 0);
  assert.ok(b.logs.some(log => log.event === 'frontend_first_delta_rendered' && log.turn_id === 'approval-turn'));
});

test('committed approval success plus follow-up model error retains business result without retry or false answer', async () => {
  const stream = controlledStream();
  const b = browser({chat:() => ({ok:true, status:200, json:async () => approvalData()}), approval:() => stream.response});
  await b.submit('Przygotuj korektę');
  const buttons = b.elements.aiMessages.querySelector('[data-approval-id]').querySelectorAll('button');
  const pending = buttons[0].onclick(); await flush();
  stream.send(event('display_delta', {delta:'Niedokończony opis'}) + event('error', {
    status:'SUCCESS', model_status:'FAILED', execution_outcome:{execution_status:'SUCCESS'},
  })); await pending;
  assert.ok(!b.text().at(-1).includes('Niedokończony'));
  assert.match(b.assistant().at(-1).className, /error/);
  assert.ok(buttons.every(button => button.disabled)); assert.equal(b.calls.length, 2);
});

test('New conversation aborts approval follow-up stream and prevents late control changes', async () => {
  const stream = controlledStream();
  const b = browser({chat:() => ({ok:true, status:200, json:async () => approvalData()}), approval:() => stream.response});
  await b.submit('Przygotuj korektę');
  const pending = b.elements.aiMessages.querySelector('[data-approval-id]').querySelector('button').onclick();
  await flush(); b.newConversation(); await pending;
  assert.equal(b.calls[1].options.signal.aborted, true); assert.equal(stream.cancelled, true);
  assert.equal(b.assistant().length, 0); assert.equal(b.elements.aiSend.disabled, false);
});

test('approval JSON fallback preserves NOOP button behavior and never automatically retries', async () => {
  const b = browser({streaming:false, chat:() => ({ok:true, status:200, json:async () => approvalData()}),
    approval:() => ({ok:true, status:200, json:async () => ({status:'NOOP', message:'Decyzja już rozpatrzona.'})})});
  await b.submit('Przygotuj korektę');
  const buttons = b.elements.aiMessages.querySelector('[data-approval-id]').querySelectorAll('button');
  await buttons[0].onclick();
  assert.equal(b.calls[1].options.headers.Accept, 'application/json');
  assert.equal(b.text().at(-1), 'Decyzja już rozpatrzona.');
  assert.ok(buttons.every(button => !button.disabled)); assert.equal(b.calls.length, 2);
});

test('unsupported streaming uses JSON once, preserving conversation and voice behavior', async () => {
  const b = browser({streaming:false}); await b.submit('Pierwsze pytanie'); await b.submit('Kolejne pytanie', true); await flush();
  const chats = b.calls.filter(call => call.url.endsWith('/chat'));
  assert.equal(chats.length, 2); assert.equal(chats[0].options.headers.Accept, 'application/json');
  assert.equal(JSON.parse(chats[1].options.body).conversation_id, 'conversation-1');
  assert.deepEqual(b.text(), ['Masz jedną sztukę.', 'Masz jedną sztukę.']);
  assert.equal(b.calls.filter(call => call.url.endsWith('/synthesize')).length, 1);
});

test('JSON authentication error is supported even when streaming was requested', async () => {
  const b = browser({chat:() => ({ok:false, status:401, json:async () => ({status:'DENIED'})})});
  await b.submit('Pytanie');
  assert.equal(b.calls.length, 1); assert.deepEqual(b.text(), ['Sesja wygasła. Zaloguj się ponownie.']);
  assert.equal(b.elements.aiSend.disabled, false);
});

test('test latency records a first display delta before delayed final response', async t => {
  const stream = controlledStream(); const b = browser({chat:() => stream.response});
  const started = performance.now(); const pending = b.submit('Ile mam?'); await flush();
  await new Promise(resolve => setTimeout(resolve, 25));
  stream.send(event('turn_started', {turn_id:'timing-test'}) + event('display_delta', {delta:'Masz jedną sztukę.'}));
  await flush();
  const first = b.logs.find(entry => entry.event === 'frontend_first_delta_rendered');
  assert.ok(first); assert.equal(b.elements.aiSend.disabled, true);
  await new Promise(resolve => setTimeout(resolve, 80));
  stream.send(event('done', finalData())); await pending;
  const completed = performance.now() - started;
  assert.ok(completed - first.elapsed_ms >= 60);
  t.diagnostic(`Simulated provider: first display delta ${first.elapsed_ms.toFixed(2)} ms; completed ${completed.toFixed(2)} ms. No live provider call.`);
});
