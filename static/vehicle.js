'use strict';
(() => {
  const $ = id => document.getElementById(id);
  let catalog, preparation, baseline, connected = false, busy = false;
  const status = value => { $('vehicle-status').textContent = value; };
  async function request(path, data) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 65000);
    try {
      const response = await fetch(path, {cache: 'no-store', signal: controller.signal,
        ...(data === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)})});
      const result = await response.json();
      if (!response.ok || result.success === false) throw new Error(result.message || 'Vehicle operation failed');
      return result;
    } finally { clearTimeout(timer); }
  }
  function option(select, value, label, disabled = false) {
    const element = document.createElement('option');
    element.value = value; element.textContent = label; element.disabled = disabled;
    select.append(element);
  }
  function updateButtons() {
    $('vehicle-capture-before').disabled = busy || !connected;
    $('vehicle-capture-after').disabled = busy || !connected || !baseline;
    $('vehicle-read').disabled = busy || !connected || !$('vehicle-pid').value;
    $('vehicle-prepare').disabled = busy || !connected || !$('vehicle-control').value || !$('vehicle-state').value ||
      !$('vehicle-profile-confirmed').checked || !$('vehicle-conditions').checked;
  }
  function selectControl() {
    preparation = null;
    $('vehicle-state').replaceChildren();
    const control = catalog?.ecus[$('vehicle-ecu').value]?.controls[$('vehicle-control').value];
    $('vehicle-control-notes').textContent = control ? control.notes || 'Community verified; local validation outstanding.' : 'No documented control available for this module.';
    option($('vehicle-state'), '', 'Select and confirm current state');
    (control?.vehicle_states || []).filter(state => !['READY', 'CHARGING'].includes(state)).forEach(state => option($('vehicle-state'), state, state));
    updateButtons();
  }
  function selectECU() {
    const ecu = catalog.ecus[$('vehicle-ecu').value];
    $('vehicle-pid').replaceChildren();
    option($('vehicle-pid'), '', 'Select a documented read');
    Object.entries(ecu.pids).forEach(([pid, definition]) => option($('vehicle-pid'), pid,
      `${pid} · ${Object.keys(definition.parameters).join(', ') || 'Raw payload'} · ${definition.vehicle_states.join('/') || 'State unverified'}`));
    $('vehicle-control').replaceChildren();
    option($('vehicle-control'), '', 'Select a documented control');
    Object.entries(ecu.controls).forEach(([did, control]) => option($('vehicle-control'), did,
      `${control.label}${control.available ? '' : ' — unavailable: prerequisites unverified'}`, !control.available));
    selectControl();
  }
  async function run(action) {
    if (busy) return;
    busy = true; updateButtons();
    try { await action(); } catch (error) { status(error.message); }
    finally { busy = false; updateButtons(); }
  }
  function showReport(report) {
    const root = $('vehicle-result'); root.replaceChildren();
    const details = document.createElement('details');
    const summary = document.createElement('summary'); summary.textContent = 'Saved response and source definitions';
    const pre = document.createElement('pre'); pre.style.whiteSpace = 'pre-wrap'; pre.style.overflowWrap = 'anywhere';
    pre.textContent = JSON.stringify(report, null, 2);
    details.append(summary, pre); root.append(details);
    const link = document.createElement('a');
    link.href = `/api/diagnostics/reports/${report.id ?? report.audit_id}`;
    link.textContent = 'Download saved report'; link.download = 'vehicle-report.json'; root.append(link);
  }
  $('vehicle-read').addEventListener('click', () => run(async () => {
    status('Reading selected ECU…');
    const report = await request('/api/vehicle/read', {ecu: $('vehicle-ecu').value, pid: $('vehicle-pid').value});
    status(report.status === 'responding' ? 'Valid response saved. See raw payload and community definitions.' : 'No valid response; data unavailable.');
    showReport(report);
  }));
  $('vehicle-prepare').addEventListener('click', () => run(async () => {
    status('Checking fresh stationary telemetry…');
    preparation = await request('/api/vehicle/controls/prepare', {ecu: $('vehicle-ecu').value, control: $('vehicle-control').value,
      vehicle_state: $('vehicle-state').value, profile_confirmed: $('vehicle-profile-confirmed').checked,
      conditions_confirmed: $('vehicle-conditions').checked});
    $('vehicle-confirm-label').textContent = `${preparation.label}. Confirmation expires in 60 seconds.`;
    $('vehicle-control-dialog').showModal(); status('Awaiting operation confirmation.');
  }));
  $('vehicle-control-dialog').addEventListener('close', () => {
    const pending = preparation; preparation = null;
    if ($('vehicle-control-dialog').returnValue !== 'execute' || !pending) return;
    run(async () => {
      status('Executing and releasing selected control…');
      const result = await request('/api/vehicle/controls/execute', {confirmation_token: pending.confirmation_token,
        confirm: true, conditions_confirmed: $('vehicle-conditions').checked});
      status(result.message); showReport(result);
    });
  });
  $('vehicle-ecu').addEventListener('change', selectECU);
  $('vehicle-control').addEventListener('change', selectControl);
  ['vehicle-pid', 'vehicle-state', 'vehicle-profile-confirmed', 'vehicle-conditions'].forEach(id => $(id).addEventListener('change', updateButtons));
  window.addEventListener('telemetry-status', event => {
    connected = !!event.detail.obd.connected && !event.detail.preview;
    if (!connected) baseline = null;
    updateButtons();
  });
  $('vehicle-capture-setting').addEventListener('change', () => { baseline = null; updateButtons(); });
  for (const phase of ['before', 'after']) {
    $(`vehicle-capture-${phase}`).addEventListener('click', () => run(async () => {
      status('Reading candidate configuration pages…');
      const report = await request('/api/vehicle/settings/capture', {
        setting: $('vehicle-capture-setting').value,
        ...(phase === 'after' ? {before_report_id: baseline} : {})
      });
      if (phase === 'before') baseline = report.id;
      status(`Report #${report.id} saved. ${report.message}`); showReport(report);
    }));
  }
  function renderSettings(data) {
    const root = $('vehicle-setting-fields'); root.replaceChildren();
    Object.entries(data.settings).forEach(([key, setting]) => {
      const field = document.createElement('div'); field.className = 'field';
      const label = document.createElement('label'); label.htmlFor = `setting-${key}`; label.textContent = setting.label;
      const input = document.createElement(setting.type === 'integer' ? 'input' : 'select');
      input.id = `setting-${key}`; input.dataset.setting = key; input.dataset.type = setting.type;
      if (setting.type === 'integer') { input.type = 'number'; input.min = setting.min; input.max = setting.max; input.step = '1'; input.placeholder = 'No request'; }
      else { option(input, '', 'No request'); setting.choices.forEach(value => option(input, value, value.replaceAll('_', ' '))); }
      input.value = setting.desired_value ?? '';
      const hint = document.createElement('p'); hint.className = 'field-hint';
      hint.textContent = `Vehicle value: unavailable. ${setting.status === 'pending_protocol' ? 'Request saved; pending protocol. ' : ''}Not applied to vehicle.`;
      field.append(label, input, hint); root.append(field);
    });
    $('vehicle-settings-save').disabled = false;
  }
  $('vehicle-settings-form').addEventListener('submit', async event => {
    event.preventDefault(); $('vehicle-settings-save').disabled = true;
    try {
      const values = {};
      document.querySelectorAll('[data-setting]').forEach(input => {
        if (input.value !== '') values[input.dataset.setting] = input.dataset.type === 'integer' ? Number(input.value) : input.value;
      });
      const result = await request('/api/vehicle/settings', values);
      renderSettings(result); $('vehicle-settings-status').textContent = result.message;
    } catch (error) { $('vehicle-settings-status').textContent = error.message; }
    finally { $('vehicle-settings-save').disabled = false; }
  });
  request('/api/vehicle/capabilities').then(data => {
    catalog = data;
    $('vehicle-source').textContent = `${Object.keys(data.ecus).length} documented ECUs · Community source: canair · Local validation outstanding`;
    Object.entries(data.ecus).forEach(([name, ecu]) => option($('vehicle-ecu'), name, `${name} · ${ecu.description}`));
    selectECU();
  }).catch(error => status(error.message));
  request('/api/vehicle/settings').then(renderSettings).catch(error => { $('vehicle-settings-status').textContent = error.message; });
})();
