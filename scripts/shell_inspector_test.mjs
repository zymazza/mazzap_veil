import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

function fakeClassList() {
  return {
    values: new Set(),
    add(name) { this.values.add(name); },
    remove(name) { this.values.delete(name); },
    toggle(name, on) {
      if (on) this.values.add(name);
      else this.values.delete(name);
    },
  };
}

function fakeElement(id, dataset = {}) {
  const listeners = {};
  return {
    id,
    dataset,
    hidden: true,
    textContent: '',
    style: {},
    classList: fakeClassList(),
    addEventListener(type, fn) { listeners[type] = fn; },
    dispatch(type, event = {}) { listeners[type]?.(event); },
    querySelectorAll() { return []; },
  };
}

function loadShell() {
  const source = fs.readFileSync(new URL('../public/shell.js', import.meta.url), 'utf8');
  const buttons = [
    fakeElement('layers-button', { mode: 'layers' }),
    fakeElement('ask-button', { mode: 'ask' }),
  ];
  const rail = fakeElement('rail');
  rail.querySelectorAll = () => buttons;

  const panes = [fakeElement('layers-pane', { pane: 'layers' })];
  const elements = new Map([
    ['rail', rail],
    ['flyout', fakeElement('flyout')],
    ['flyout-title', fakeElement('flyout-title')],
    ['inspector', fakeElement('inspector')],
    ['chat-panel', fakeElement('chat-panel')],
    ['flyout-close', fakeElement('flyout-close')],
    ['chat-close', fakeElement('chat-close')],
    ['inspector-close', fakeElement('inspector-close')],
    ['atlas-search', fakeElement('atlas-search')],
    ['reset-view', fakeElement('reset-view')],
    ['help-sheet', fakeElement('help-sheet')],
    ['help-btn', fakeElement('help-btn')],
    ['help-close', fakeElement('help-close')],
  ]);

  const docListeners = {};
  const document = {
    body: { classList: fakeClassList() },
    getElementById: (id) => elements.get(id),
    querySelectorAll: (selector) => selector === '.pane' ? panes : [],
    addEventListener(type, fn) { docListeners[type] = fn; },
    dispatchEvent(event) { docListeners[event.type]?.(event); },
  };

  vm.runInNewContext(source, {
    document,
    window: {},
    setInterval: () => 1,
    clearInterval: () => {},
    MutationObserver: class {
      constructor() {
        throw new Error('shell.js should not watch inspector DOM mutations');
      }
    },
  });

  return { document, elements };
}

test('inspector opens only from explicit inspect events and remains dismissible', () => {
  const { document, elements } = loadShell();
  const inspector = elements.get('inspector');

  assert.equal(inspector.hidden, true);
  document.dispatchEvent({ type: 'veil:inspect' });
  assert.equal(inspector.hidden, false);

  elements.get('inspector-close').dispatch('click');
  assert.equal(inspector.hidden, true);
});
