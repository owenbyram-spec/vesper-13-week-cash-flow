"""Execute connection-form and delayed-response behavior without real credentials."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_connection_forms_and_delayed_assistant_results_keep_displayed_scope():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required only for this browser-JavaScript regression")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
const closeEvents = [];
const cancelledTimers = [];
let nextTimer = 0;
function flushCloseEvents() { while (closeEvents.length) closeEvents.shift()(); }
function element(id) {
  if (elements.has(id)) return elements.get(id);
  const el = {
    id, value: '', checked: false, open: false, hidden: false, textContent: '',
    children: [], listeners: {}, dataset: {}, classList: {toggle() {}},
    append(...children) { this.children.push(...children); },
    prepend(...children) { this.children.unshift(...children); },
    remove() { for (const parent of elements.values()) parent.children = parent.children.filter(child => child !== this); },
    replaceChildren(...children) { this.children = children; this.textContent = ''; },
    setAttribute() {},
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
    querySelectorAll(selector) { return selector === '.dialog-feedback' ? this.children.filter(child => child.className === 'dialog-feedback') : []; },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    showModal() { this.open = true; },
    close() { this.open = false; closeEvents.push(() => this.listeners.close?.()); },
    checkValidity() { return !['api_key','model','consent'].some(name => this.elements[name]?.validity.valueMissing); },
    reset() {
      for (const [name, field] of Object.entries(this.elements || {})) {
        field.value = name === 'model' ? 'default-model' : name === 'environment' ? 'sandbox' : '';
        field.checked = false;
      }
    },
  };
  elements.set(id, el);
  return el;
}
for (const [id, fields] of [
  ['model-form', ['api_key', 'model', 'persist', 'consent']],
  ['qbo-form', ['client_id', 'client_secret', 'realm_id', 'refresh_token', 'environment', 'persist']],
]) {
  element(id).elements = Object.fromEntries(fields.map(name => [name, {
    name, value: '', checked: false, disabled: false,
    get validity() { return {valueMissing: name === 'consent' ? !this.checked : ['api_key','model'].includes(name) && !this.value}; },
  }]));
}
const history = [];
const response = (data, ok = true) => ({ok, headers: {get: () => 'application/json'}, json: async () => data});
const context = vm.createContext({
  document: {
    getElementById: element, createElement: () => element(Symbol()),
    querySelectorAll: selector => selector === 'dialog' ? [element('settings-dialog')] : [],
    querySelector: selector => selector === 'dialog[open]' && element('settings-dialog').open ? element('settings-dialog') : null,
  },
  location: {origin: 'http://127.0.0.1:8876'},
  setTimeout: () => ++nextTimer, clearTimeout: timer => cancelledTimers.push(timer),
  fetch: (...args) => { history.push(args); return context.fetchImpl(...args); },
  fetchImpl: async () => response({model: {model: 'test-model'}, polling: {enabled: false}}),
});
const source = fs.readFileSync(process.argv[1], 'utf8');
// Install actual browser handlers; the network bootstrap is outside this test harness.
vm.runInContext(source.slice(0, source.lastIndexOf('\n(async()=>')), context);
function select(id, version = null) {
  context.selected = {id, name: `Client ${id.toUpperCase()}`, synthetic: false, currency: 'USD'};
  context.version = version;
  vm.runInContext(`state.data = {workspace: selected, version: null, workspaces: [selected],
    scope: {id: selected.id, version_id: version}, data_directory: '/tmp/zip-verification'}; render();`, context);
}
(async () => {
  select('a');
  await vm.runInContext('openSettings()', context);
  assert.match(element('settings-context').textContent, /Client A/);
  assert.match(element('settings-context').textContent, /127\.0\.0\.1:8876/);
  assert.match(element('settings-context').textContent, /\/tmp\/zip-verification/);
  assert.match(element('model-connection-status').textContent, /No AI connection saved for Client A/);
  assert.equal(element('test-model').disabled, true);
  const ai = element('model-form');
  const beforeInvalid = history.length;
  ai.elements.api_key.value = ''; ai.elements.model.value = ''; ai.elements.consent.checked = false;
  ai.listeners.invalid({preventDefault() {}});
  assert.match(element('model-save-feedback').textContent, /Enter your OpenAI API key/);
  assert.match(element('model-save-feedback').textContent, /Enter a model ID/);
  assert.match(element('model-save-feedback').textContent, /Acknowledge the data-transfer/);
  const nativeFailure = element('model-save-feedback').textContent;
  await element('test-model').listeners.click({preventDefault() {}});
  assert.equal(element('model-save-feedback').textContent, nativeFailure);
  assert.equal(history.length, beforeInvalid, 'Invalid fields and Test must send no request');

  ai.elements.api_key.value = 'synthetic-preserved-on-error';
  ai.elements.model.value = 'invalid model'; ai.elements.consent.checked = true;
  context.fetchImpl = async () => response({detail: 'The model name contains unsupported characters.'}, false);
  await ai.listeners.submit({preventDefault() {}});
  assert.equal(ai.elements.api_key.value, 'synthetic-preserved-on-error');
  assert.match(element('model-save-feedback').textContent, /save failed: The model name contains unsupported characters/);
  assert.equal(element('model-save-feedback').dataset.state, 'failed');
  assert.equal(element('test-model').disabled, true);
  const afterFailure = history.length, originalFailure = element('model-save-feedback').textContent;
  await element('test-model').listeners.click({preventDefault() {}});
  assert.equal(history.length, afterFailure, 'Test must not mask an unsuccessful Save');
  assert.equal(element('model-save-feedback').textContent, originalFailure);
  const failedPayload = JSON.parse(history.at(-1)[1].body);
  assert.deepEqual(failedPayload, {api_key:'synthetic-preserved-on-error',model:'invalid model',persist:false,consent:true});
  assert.equal(history.at(-1)[1].method, 'POST');
  assert.equal(JSON.parse(history.at(-1)[1].headers['X-Vesper-Scope']).id, 'a');

  ai.elements.model.value = 'test-model';
  context.fetchImpl = async () => response({model: {configured: false}});
  await ai.listeners.submit({preventDefault() {}});
  assert.match(element('model-save-feedback').textContent, /did not confirm the saved AI connection/);
  assert.equal(ai.elements.api_key.value, 'synthetic-preserved-on-error');
  context.fetchImpl = async url => response({model: {configured: url === '/api/settings/model', model:'test-model'}});
  await ai.listeners.submit({preventDefault() {}});
  assert.match(element('model-save-feedback').textContent, /not available after saving/);
  assert.equal(ai.elements.api_key.value, 'synthetic-preserved-on-error');

  let resolveSave;
  context.fetchImpl = url => url === '/api/settings/model'
    ? new Promise(resolve => { resolveSave = resolve; })
    : Promise.resolve(response({model: {configured:true,model:'test-model'},polling:{enabled:false}}));
  const saving = ai.listeners.submit({preventDefault() {}});
  assert.equal(element('model-save-feedback').dataset.state, 'saving');
  assert.equal(ai.elements.api_key.value, 'synthetic-preserved-on-error');
  for (const name of ['api_key','model','persist','consent'])assert.equal(ai.elements[name].disabled,true);
  assert.equal(element('test-model').disabled,true);
  assert.equal(element('remove-model').disabled,true);
  const whileSaving=history.length;
  await element('test-model').listeners.click({preventDefault() {}});
  await element('remove-model').listeners.click({preventDefault() {}});
  await ai.listeners.submit({preventDefault() {}});
  assert.equal(history.length,whileSaving,'Pending Save blocks Test, Remove and duplicate Save');
  resolveSave(response({model:{configured:true,model:'test-model'},storage_mode:'session-only'}));
  await saving;
  assert.equal(ai.elements.api_key.value,'');
  assert.equal(ai.elements.api_key.disabled,false);
  assert.equal(element('test-model').disabled,false);
  assert.equal(element('model-save-feedback').dataset.state,'confirmed');
  assert.match(element('model-save-feedback').textContent,/saved and confirmed.*Session-only storage/);

  ai.elements.api_key.value='synthetic-unsaved-new-entry';
  ai.listeners.input({target:ai.elements.api_key});
  assert.equal(element('test-model').disabled,true);
  const beforeDraftTest=history.length;
  await element('test-model').listeners.click({preventDefault() {}});
  assert.equal(history.length,beforeDraftTest,'Test cannot use an old bundle while displaying an unsaved draft');
  context.fetchImpl=async()=>response({detail:'Synthetic rejected replacement'},false);
  await ai.listeners.submit({preventDefault(){}});
  assert.equal(vm.runInContext('state.modelSaveState',context),'failed');
  assert.equal(ai.elements.api_key.value,'synthetic-unsaved-new-entry');
  context.fetchImpl=async()=>response({model:{configured:false,model:'test-model'},polling:{enabled:false}});
  await element('remove-model').listeners.click({preventDefault() {}});
  assert.equal(ai.elements.api_key.value,'');
  assert.equal(vm.runInContext('state.modelDraftDirty',context),false);
  assert.equal(vm.runInContext('state.modelSaveState',context),'idle');
  assert.equal(element('test-model').disabled,true);
  assert.match(element('model-save-feedback').textContent,/removed for Client A/);

  ai.elements.api_key.value='synthetic-late-save'; ai.elements.model.value='test-model'; ai.elements.consent.checked=true;
  context.fetchImpl=url=>url==='/api/settings/model'
    ? new Promise(resolve=>{resolveSave=resolve;})
    : Promise.resolve(response({model:{configured:false,model:'test-model'},polling:{enabled:false}}));
  const lateSave=ai.listeners.submit({preventDefault(){}});
  element('settings-dialog').close(); flushCloseEvents();
  assert.equal(ai.elements.api_key.value,'');
  assert.equal(element('test-model').disabled,true);
  await vm.runInContext('openSettings()',context);
  const newFeedback=element('model-save-feedback').textContent;
  resolveSave(response({model:{configured:true,model:'test-model'}})); await lateSave;
  assert.equal(ai.elements.api_key.value,'');
  assert.equal(element('test-model').disabled,true,'Old completion must not enable the new form');
  assert.equal(element('model-save-feedback').textContent,newFeedback);
  context.fetchImpl = async url => response(url === '/api/settings/model/test'
    ? {model: 'test-model'}
    : {model: {configured: true, model: 'test-model'}, polling: {enabled: false}});
  element('model-form').elements.api_key.value = 'fixture-key-for-a';
  element('model-form').elements.model.value = 'test-model';
  element('model-form').elements.consent.checked = true;
  await element('model-form').listeners.submit({preventDefault() {}});
  assert.match(element('model-connection-status').textContent, /AI connection saved for Client A.*test-model/);
  assert.match(element('model-connection-status').textContent, /Saving does not verify/);
  assert.match(element('toast').textContent, /saved for Client A.*scoped assistant response separately/);
  await element('test-model').listeners.click({preventDefault() {}});
  assert.match(element('toast').textContent, /Connection test succeeded for Client A.*test-model/);
  assert.match(element('toast').textContent, /does not verify a scoped assistant response/);
  assert.equal(element('settings-dialog').querySelectorAll('.dialog-feedback').length, 1);
  element('model-form').elements.api_key.value = 'fixture-key-for-a';
  element('model-form').elements.consent.checked = true;
  element('model-form').elements.persist.checked = true;
  element('qbo-form').elements.client_secret.value = 'fixture-secret-for-a';
  element('qbo-company').append({textContent: 'Company A confirmation'});
  const activeToastTimer = vm.runInContext('toastTimer', context);
  element('settings-dialog').close();
  flushCloseEvents();
  assert.equal(element('model-form').elements.api_key.value, '');
  assert.equal(element('model-form').elements.consent.checked, false);
  assert.equal(element('model-form').elements.persist.checked, false);
  assert.equal(element('qbo-form').elements.client_secret.value, '');
  assert.equal(element('qbo-company').children.length, 0);
  assert.equal(element('settings-dialog').querySelectorAll('.dialog-feedback').length, 0);
  assert.equal(element('model-connection-status').textContent, '');
  assert.equal(element('toast').hidden, true);
  assert.equal(element('toast').textContent, '');
  assert(cancelledTimers.includes(activeToastTimer));

  await vm.runInContext('openSettings()', context);
  // Native close events are queued. Reopening must survive an older queued close.
  const reopen = vm.runInContext('openSettings()', context);
  flushCloseEvents();
  await reopen;
  assert.equal(element('settings-dialog').open, true);
  assert.equal(vm.runInContext('state.settingsWorkspace', context), 'a');
  element('model-form').elements.api_key.value = 'another-fixture-key-for-a';
  select('b');
  flushCloseEvents();
  assert.equal(element('settings-dialog').open, false);
  assert.equal(element('model-form').elements.api_key.value, '');
  const before = history.length;
  await element('model-form').listeners.submit({preventDefault() {}});
  assert.equal(history.length, before, 'A stale connection form must not issue a save');
  assert.match(element('toast').textContent, /Reopen Connections/);
  context.fetchImpl = async () => response({model: {configured: false, model: 'test-model'}, polling: {enabled: false}});
  await vm.runInContext('openSettings()', context);
  assert.match(element('settings-context').textContent, /Client B/);
  assert.match(element('model-connection-status').textContent, /No AI connection saved for Client B/);
  assert(!element('model-connection-status').textContent.includes('Client A'));
  assert.equal(element('settings-dialog').querySelectorAll('.dialog-feedback').length, 0);

  context.fetchImpl = async () => response({company: {id: '123', name: 'Fixture company'}, requires_confirmation: true});
  await element('test-qbo').listeners.click({preventDefault() {}});
  const confirm = element('qbo-company').children.find(child => child.textContent === 'Confirm this company for the active entity');
  assert(confirm, 'A successful company test must require a separate confirmation');
  vm.runInContext("state.data.scope.version_id = 'new-qbo-version'", context);
  const beforeConfirmation = history.length;
  await confirm.listeners.click({preventDefault() {}});
  assert.equal(history.length, beforeConfirmation, 'A company test from an older scope must not bind');
  assert.match(element('toast').textContent, /company confirmation is stale/);
  element('settings-dialog').close();
  flushCloseEvents();
  await vm.runInContext('openSettings()', context);
  await confirm.listeners.click({preventDefault() {}});
  assert.match(element('toast').textContent, /company confirmation is stale/);

  let resolveRequest;
  context.fetchImpl = () => new Promise(resolve => { resolveRequest = resolve; });
  vm.runInContext("state.data.version = {id: 'base-version'}", context);
  element('assistant-input').value = 'Explain current cash';
  const pending = vm.runInContext('submitAssistant()', context);
  select('a');
  resolveRequest(response({message: 'Old entity assistant reply', evidence: [], proposals: []}));
  await assert.rejects(pending, /entity or forecast version changed/);
  assert(!element('assistant-thread').children.some(child => child.textContent === 'Old entity assistant reply'));
  assert.equal(element('assistant-send').disabled, false);

  const next = vm.runInContext("api('/api/settings/model/test', {method:'POST', body:{}})", context);
  vm.runInContext("state.data.scope.version_id = 'new-version'", context);
  resolveRequest(response({message: 'Old version connection result'}));
  await assert.rejects(next, /entity or forecast version changed/);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    app_js = Path(__file__).parents[1] / "vesper_cashflow/static/app.js"
    result = subprocess.run([node, "-e", script, str(app_js)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_credential_forms_use_post_when_javascript_is_unavailable():
    from html.parser import HTMLParser

    class Forms(HTMLParser):
        def __init__(self):
            super().__init__()
            self.forms = {}

        def handle_starttag(self, tag, attrs):
            if tag == "form":
                data = dict(attrs)
                self.forms[data.get("id")] = data

    parser = Forms()
    parser.feed((Path(__file__).parents[1] / "vesper_cashflow/static/index.html").read_text())
    for form_id, action in (("model-form", "/api/settings/model"), ("qbo-form", "/api/settings/qbo")):
        assert parser.forms[form_id]["method"] == "post"
        assert parser.forms[form_id]["action"] == action
