const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../docs/join.html'), 'utf8');
const script = html.match(/<script>\s*(const params = new URLSearchParams\(location\.search\);[\s\S]*?)<\/script>/)?.[1];
assert.ok(script, 'join form script exists');
const now = Date.parse('2026-10-31T12:00:00+08:00');

function run(search, saved, existingStore) {
  const store = existingStore || new Map();
  if (saved !== undefined) store.set('ginguiGuestSubmission', JSON.stringify(saved));
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, {
      hidden: id === 'successPanel' || id === 'calendarButton',
      value: '', textContent: '', listeners: {},
      addEventListener(event, callback) { this.listeners[event] = callback; },
      focus() { this.focused = true; }
    });
    return elements.get(id);
  };
  const events = [];
  const historyCalls = [];
  const document = {
    getElementById: element,
    querySelector(selector) { return element(selector); }
  };
  const sessionStorage = {
    getItem(key) { return store.get(key) || null; },
    setItem(key, value) { store.set(key, value); }
  };
  class TestDate extends Date {
    constructor(...args) { super(...(args.length ? args : [now])); }
    static now() { return now; }
  }
  vm.runInNewContext(script, {
    Date: TestDate, document, sessionStorage, URL, URLSearchParams,
    Number, JSON,
    location: { search, pathname: '/join.html', hash: '#form' },
    history: { replaceState(...args) { historyCalls.push(args); } },
    window: { ginguiTrack(name, params) { events.push({ name, params }); } }
  });
  return { element, events, historyCalls, store };
}

const direct = run('?sent=1');
assert.equal(direct.element('guestForm').hidden, false);
assert.equal(direct.element('successPanel').hidden, true);
assert.equal(direct.events.length, 0);
assert.equal(direct.historyCalls[0][2], '/join.html#form');
assert.equal(run('?sent=1&source=directory_no_result').historyCalls[0][2], '/join.html?source=directory_no_result#form');

const brokenStore = new Map([['ginguiGuestSubmission', '{broken']]);
assert.equal(run('?sent=1', undefined, brokenStore).element('successPanel').hidden, true);
assert.equal(run('?sent=1', { submittedAt: now + 1, status: 'pending' }).element('successPanel').hidden, true);
assert.equal(run('?sent=1', { submittedAt: now, status: 'other' }).element('successPanel').hidden, true);
assert.equal(run('?sent=1', { submittedAt: now - 30 * 60 * 1000, status: 'pending' }).element('successPanel').hidden, true);

const stale = run('?sent=1', { date: '2026-11-06', invitedBy: '謝豐安', submittedAt: now - 31 * 60 * 1000, status: 'pending' });
assert.equal(stale.element('successPanel').hidden, true);
assert.equal(stale.events.length, 0);

const store = new Map();
const form = run('?invited_by=%E8%AC%9D%E8%B1%90%E5%AE%89', undefined, store);
form.element('g-date').value = '2026-11-06（五）06:30｜黃鈺峯主講';
form.element('guestForm').listeners.submit();
assert.equal(form.events.at(-1).name, 'guest_form_submit');
assert.equal(form.events.at(-1).params.inviter_id, 'xiefengan');
assert.equal(JSON.parse(store.get('ginguiGuestSubmission')).status, 'pending');

const redirected = run('?sent=1', undefined, store);
assert.equal(redirected.element('guestForm').hidden, true);
assert.equal(redirected.element('successPanel').hidden, false);
assert.equal(redirected.element('calendarButton').hidden, false);
assert.equal(redirected.events.length, 1);
assert.equal(redirected.events[0].name, 'guest_form_success');
assert.equal(JSON.parse(store.get('ginguiGuestSubmission')).status, 'confirmed');

const reload = run('?sent=1', undefined, store);
assert.equal(reload.element('successPanel').hidden, false);
assert.equal(reload.events.length, 0);

const blockedStore = {
  get() { return JSON.stringify({ date: '2026-11-06', submittedAt: now, status: 'pending' }); },
  set() { throw new Error('storage blocked'); }
};
const blockedReturn = run('?sent=1', undefined, blockedStore);
assert.equal(blockedReturn.element('successPanel').hidden, false);
assert.equal(blockedReturn.events.length, 0);
const blockedSubmit = run('', undefined, blockedStore);
blockedSubmit.element('guestForm').listeners.submit();
assert.equal(blockedSubmit.events[0].name, 'guest_form_submit');

console.log('join success guard: all scenarios passed');
