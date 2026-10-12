/* Node contract checks only: no browser, network, or production data. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const fixture = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = fs.readFileSync(path.join(__dirname, 'demo_api.js'), 'utf8');

function start(storage = new Map(), version = 'first') {
  const listeners = {}, context = {URL, Response, Date, Promise, JSON};
  context.localStorage = {
    getItem: k => storage.get(k) ?? null,
    setItem: (k, v) => storage.set(k, v),
    removeItem: k => storage.delete(k)
  };
  context.document = {
    getElementById: id => id === 'demo-seed'
      ? {textContent: JSON.stringify(fixture.seed), dataset: {version}}
      : id === 'demo-contract' ? {textContent: JSON.stringify(fixture.contract)} : null,
    addEventListener: (event, handler) => {listeners[event] = handler;}
  };
  context.location = {href: 'https://demo.example/mailwork/', reload: () => {context.reloaded = true;}};
  context.window = {fetch: () => {throw new Error('Unexpected real network request');}};
  vm.runInNewContext(source, context, {filename: 'demo_api.js'});
  async function request(route, data) {
    const response = await context.window.fetch('/api/' + route,
      data === undefined ? {} : {method: 'POST', body: JSON.stringify(data)});
    return {status: response.status, data: response.status === 204 ? null : await response.json()};
  }
  return {request, listeners, context, storage};
}

(async () => {
  let demo = start();
  const id = '123456789abc';
  assert.equal((await demo.request('ping')).status, 204);
  assert.deepEqual((await demo.request('feedback')).data, fixture.seed);
  let response = await demo.request('feedback', {items: [
    {task_id: id, label: 'done', on: '2020-01-02'},
    {task_id: 'abcdef123456', label: 'excluded', reason: '남의 일'}
  ]});
  assert.equal(response.status, 200);
  assert.equal(response.data.states[id].labeled_at, '2020-01-02T03:00:00.000Z');
  assert.equal(response.data.states.abcdef123456.reason, '남의 일');
  // Build fingerprint changes must not erase visitor choices.
  demo = start(demo.storage, 'new-build');
  assert.equal((await demo.request('feedback')).data.states[id].label, 'done');
  assert.equal((await demo.request('feedback', {task_id: id, label: 'open'})).data.states[id], null);
  assert.equal((await demo.request('feedback', {task_id: 'bad', label: 'done'})).status, 400);
  assert.equal((await demo.request('feedback', {task_id: id, label: 'excluded', reason: 'invalid'})).status, 400);
  assert.equal((await demo.request('feedback', [])).status, 400);
  assert.equal((await demo.request('mark', {target: 'project', key: '가상 프로젝트', label: 'not_mine'})).status, 200);
  assert.equal((await demo.request('feedback')).data.projects['가상 프로젝트'].label, 'not_mine');
  assert.equal((await demo.request('mark', {target: 'project', key: '가상 프로젝트', label: 'open'})).data.state, null);
  assert.equal((await demo.request('mark', {target: 'thread', key: 'thread', label: 'not_mine'})).status, 400);
  assert.equal((await demo.request('mark', {target: 'project', key: '__proto__', label: 'done'})).status, 400);
  assert.equal((await demo.request('mark', {target: 'states', key: 'anything', label: 'done'})).status, 400);
  assert.equal((await demo.request('mark', {target: 'project', key: 'valid', label: 'done', ref_at: 'invalid'})).status, 400);
  const year = new Date().getUTCFullYear();
  assert.equal((await demo.request('due', {task_id: id, due: `${year}-02-31`})).status, 400);
  assert.equal((await demo.request('due', {task_id: 'bad', due: `${year}-10-20`})).status, 400);
  assert.equal((await demo.request('due', {task_id: id, due: `${year + 4}-01-01`})).status, 400);
  assert.equal((await demo.request('due', {task_id: id, due: `${year}-10-20`})).data.state.due, `${year}-10-20`);
  assert.equal((await demo.request('due', {task_id: id, due: ''})).data.state, null);
  assert.equal((await demo.request('color', {project: '가상 프로젝트', color: '#123ABC'})).data.color.color, '#123abc');
  assert.equal((await demo.request('color', {project: '가상 프로젝트', color: 's0'})).status, 400);
  assert.equal((await demo.request('color', {project: '가상 프로젝트', color: ''})).data.color.color, '');
  assert.equal((await demo.request('filters', {op: 'remove_rule', id: 1})).status, 501);
  assert.equal((await demo.request('missing')).status, 404);
  // Damaged storage falls back to a complete synthetic starting state.
  for (const broken of ['[1]', '{', 'null', '{"states":null,"dues":42}']) {
    const state = (await start(new Map([['mailwork.demo.state.v1', broken]])).request('feedback')).data;
    for (const key of ['states', 'dues', 'colors', 'threads', 'projects']) assert.equal(typeof state[key], 'object');
  }
  const legacy = JSON.parse(JSON.stringify(fixture.seed));
  legacy.projects.test = {label: 'not_mine'};
  const migrated = start(new Map([['nwmail.demo.first', JSON.stringify(legacy)]]));
  assert.equal((await migrated.request('feedback')).data.projects.test.label, 'not_mine');
  demo.listeners.click({preventDefault() {}, target: {closest: () => true}});
  assert.equal(demo.storage.has('mailwork.demo.state.v1'), false);
  assert.equal(demo.context.reloaded, true);
  // The exact transformed production theme script starts light, including when storage fails.
  for (const stored of [null, 'dark', 'system', 'invalid', 'storage-error']) {
    let mode, click;
    const button = {addEventListener: (_, fn) => {click = fn;}, textContent: ''};
    vm.runInNewContext(fixture.theme, {
      document: {getElementById: () => button, documentElement: {
        setAttribute: (_, value) => {mode = value;}, removeAttribute: () => {mode = 'system';}
      }},
      localStorage: {
        getItem: () => {if (stored === 'storage-error') throw Error('blocked'); return stored;},
        setItem() {}
      }
    });
    assert.equal(mode, stored === 'dark' || stored === 'system' ? stored : 'light');
    if (mode === 'light') {click(); assert.equal(mode, 'dark'); click(); assert.equal(mode, 'system'); click(); assert.equal(mode, 'light');}
  }
  console.log('PASS: demo API state, project ownership, due dates, migration, reset, validation');
})().catch(error => {console.error(error); process.exitCode = 1;});
