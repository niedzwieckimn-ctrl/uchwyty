import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {test} from 'node:test';

const source = readFileSync(new URL('./static/orderchamp_diagnostics.js', import.meta.url), 'utf8');
function harness(responses, onFetch) {
  const elements = Object.fromEntries(['connection','single','all','push','push-all','stop','download','sku','status','result']
    .map(id => [id, {disabled:false, textContent:'', value:'CH010-AB-N28', handlers:{},
      addEventListener(event, handler) {this.handlers[event] = handler;}}]));
  const calls = [];
  let download;
  const context = {
    document: {getElementById:id => elements[id], querySelector:() => ({content:'csrf-test'}),
      createElement:() => ({click() {}})},
    AbortController, Blob, JSON, Error,
    URL: {createObjectURL(blob) {download=blob; return 'blob:synthetic';}, revokeObjectURL(){}},
    setTimeout:() => 1, clearTimeout:() => {},
    fetch:async (url, options) => {
      calls.push({url, options});
      if (onFetch) onFetch(elements, calls.length);
      const {status=200, data} = responses.shift();
      return {status, headers:{get:()=>'application/json'}, json:async()=>data};
    }
  };
  vm.runInNewContext(source, context);
  return {elements, calls, click:id => elements[id].handlers.click(),
          result:() => JSON.parse(elements.result.textContent), download:()=>download};
}

function page(offset, more, summary, status=200) {
  return {status, data:{mode:'dry_run', writes_enabled:false, summary,
    pagination:{offset, limit:1, total_sku:3, has_more:more,
                next_offset:more ? offset+1 : null, catalog_version:'v'.repeat(64)}}};
}

test('connection uses admin session and CSRF with no credentials in body', async () => {
  const h = harness([{data:{connected:true, products_read:true, write_checked:false}}]);
  await h.click('connection');
  assert.equal(h.calls[0].url, '/api/admin/orderchamp/test-connection');
  assert.equal(h.calls[0].options.headers['X-CSRF-Token'], 'csrf-test');
  assert.equal(h.calls[0].options.credentials, 'same-origin');
  assert.deepEqual(JSON.parse(h.calls[0].options.body), {});
  assert.equal(h.result().connected, true);
});

test('single SKU sends exact input and displays missing result', async () => {
  const h = harness([{status:404, data:{rows:[{status:'MISSING'}]}}]);
  await h.click('single');
  assert.deepEqual(JSON.parse(h.calls[0].options.body), {sku:'CH010-AB-N28'});
  assert.equal(h.result().rows[0].status, 'MISSING');
  assert.equal(h.elements.single.disabled, false);
});

test('paged report does not count missing SKU as an API error', async () => {
  const h = harness([
    page(0,true,{local_sku:1,matched:1,missing:0,errors:0}),
    page(1,true,{local_sku:1,matched:1,missing:0,errors:0}),
    page(2,false,{local_sku:1,matched:0,missing:1,errors:0},404)
  ]);
  await h.click('all');
  assert.equal(h.calls.length, 3);
  assert.deepEqual(h.result().summary,{local_sku:3,matched:2,missing:1,errors:0,synchronized:0,skipped:0});
  assert.equal(h.result().complete,true);
  assert.deepEqual(JSON.parse(h.calls[1].options.body), {offset:1,limit:1,catalog_version:'v'.repeat(64)});
  await h.click('download');
  assert.equal(JSON.parse(await h.download().text()).summary.missing,1);
});

test('checked SKU sends only its confirmed stock and remote timestamp', async () => {
  const row = {status:'MATCHED', sku:'CH010-AB-N28', would_send:5,
    remote:{inventory_policy:'DENY', inventory_quantity:7, levels_complete:true,
      levels:[{id:'level-1',is_primary:true,quantity:7,available_quantity:7,
        updated_at:'2026-09-29T08:00:00Z'}]}};
  const h = harness([{data:{rows:[row],summary:{errors:0,missing:0}}},
    {data:{ok:true,status:'VERIFIED',wrote:true,sku:row.sku,quantity:5}}]);
  await h.click('single');
  assert.equal(h.elements.push.disabled,false);
  await h.click('push');
  assert.equal(h.calls[1].url,'/api/admin/orderchamp/push-one');
  assert.deepEqual(JSON.parse(h.calls[1].options.body),{
    sku:row.sku,expected_local:5,expected_remote_updated_at:'2026-09-29T08:00:00Z'});
  assert.equal(h.result().status,'VERIFIED');
  assert.equal(h.elements.push.disabled,true);
});

test('catalog failure preserves partial report and stops requesting', async () => {
  const h = harness([page(0,true,{local_sku:1,matched:1,missing:0,errors:0}),
    {status:409,data:{error_code:'CATALOG_CHANGED_RESTART'}}]);
  await h.click('all');
  assert.equal(h.result().complete,false);
  assert.equal(h.result().pages.length,1);
  assert.equal(h.result().error.error_code,'CATALOG_CHANGED_RESTART');
  assert.equal(h.calls.length,2);
});

test('stop ends after current SKU and leaves downloadable partial report', async () => {
  const h = harness([page(0,true,{local_sku:1,matched:1,missing:0,errors:0})],
    (elements)=>elements.stop.handlers.click());
  await h.click('all');
  assert.equal(h.calls.length,1);
  assert.equal(h.result().complete,false);
  assert.equal(h.elements.download.disabled,false);
});
