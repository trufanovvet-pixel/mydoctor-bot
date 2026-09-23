const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function worker() {
  const handlers = {}; const cached = []; const opened = []; const shown = [];
  const context = {
    URL, Set, Promise,
    fetch: async () => { throw new Error('offline'); },
    caches: {open: async () => ({addAll: async paths => cached.push(...paths)}),
      match: async path => ({offline: path}), keys: async () => [], delete: async () => true},
    self: {location: {origin: 'https://mydoctor.test'},
      addEventListener: (name, handler) => {handlers[name] = handler;}, skipWaiting: async () => {},
      clients: {claim: async () => {}, matchAll: async () => [], openWindow: async url => {opened.push(url);}},
      registration: {showNotification: async (title, options) => {shown.push({title, options});}}}
  };
  vm.runInNewContext(fs.readFileSync('web_static/service-worker.js', 'utf8'), context);
  async function run(name, values = {}) {
    let task; let intercepted = false;
    handlers[name]({...values, waitUntil: value => {task = value;}, respondWith: value => {intercepted = true; task = value;}});
    return {intercepted, result: await task};
  }
  return {context, run, cached, opened, shown};
}

test('offline app caches only the public shell and never intercepts writes, documents or APIs', async () => {
  const w = worker(); await w.run('install');
  assert.equal(w.cached.length, 4);
  assert(w.cached.every(path => path === '/static/offline.html' || path.startsWith('/app-icon/')));
  for (const [method, path, mode] of [['POST','/doctor/requests/114/messages','navigate'],
      ['GET','/consultation/chat-files/8','cors'], ['GET','/api/push/config','cors'],
      ['GET','/pets/1/photo','cors'], ['GET','/consultation/requests/114/messages','cors']]) {
    assert.equal((await w.run('fetch', {request: {method, mode, url: 'https://mydoctor.test'+path}})).intercepted, false);
  }
  const page = await w.run('fetch', {request: {method: 'GET', mode: 'navigate', url: 'https://mydoctor.test/doctor'}});
  assert.equal(page.result.offline, '/static/offline.html');
});

test('notifications ignore clinical previews and unsafe external destinations', async () => {
  const w = worker();
  await w.run('push', {data: {json: () => ({body: 'Patient diagnosis must stay private', language: 'en', url: 'https://attacker.test'})}});
  assert.equal(w.shown[0].title, 'MyDoctor');
  assert(!JSON.stringify(w.shown[0]).includes('Patient diagnosis'));
  await w.run('notificationclick', {notification: {close() {}, data: {url: 'https://attacker.test'}}});
  assert.equal(w.opened[0], 'https://mydoctor.test/messages');
});

test('notification opens the right case without replacing a different unsent conversation', async () => {
  const w = worker(); let focused = false;
  w.context.self.clients.matchAll = async () => [{url:'https://mydoctor.test/doctor/requests/2', focus:async()=>{focused=true;}}];
  await w.run('notificationclick', {notification: {close() {}, data: {url:'/doctor/requests/114#conversation'}}});
  assert.equal(focused, false);
  assert.equal(w.opened[0], 'https://mydoctor.test/doctor/requests/114#conversation');
  w.context.self.clients.matchAll = async () => [{url:'https://mydoctor.test/doctor/requests/114', focus:async()=>{focused=true;}}];
  await w.run('notificationclick', {notification: {close() {}, data: {url:'/doctor/requests/114#conversation'}}});
  assert.equal(focused, true); assert.equal(w.opened.length, 1);
});
