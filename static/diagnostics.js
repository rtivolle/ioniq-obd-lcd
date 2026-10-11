'use strict';
(() => {
  const $ = id => document.getElementById(id);
  let clearPreparation = null;
  let chargingBusy = false;
  let diagnosticBusy = false;
  const status = text => { $('diagnostic-status').textContent = text; };
  async function request(path, payload) {
    const res = await fetch(path, payload === undefined ? {cache: 'no-store'} : {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload), cache: 'no-store'
    });
    const data = await res.json();
    if (!res.ok || data.success === false) throw new Error(data.message || 'Request failed');
    return data;
  }
  function text(parent, tag, value) {
    const element = document.createElement(tag);
    element.textContent = String(value);
    parent.appendChild(element);
    return element;
  }
  function download(parent, id) {
    const link = text(parent, 'a', `Download saved report #${id}`);
    link.href = `/api/diagnostics/reports/${id}`;
    link.download = `ioniq_diagnostic_${id}.json`;
  }
  async function charging() {
    if (chargingBusy) return;
    chargingBusy = true;
    try {
      const list = await request('/api/charging');
      if (!Array.isArray(list)) throw new Error('Invalid charging history');
      const tbody = $('charging-history-list');
      tbody.replaceChildren();
      list.forEach(session => {
        const row = document.createElement('tr');
        [session.start_time, session.status, session.energy_added_kwh.toFixed(2) + ' kWh',
          Math.round(session.duration_sec) + ' s',
          `${session.start_soc ?? '—'}% → ${session.end_soc ?? '—'}%`,
          session.soc_change == null ? 'Data unavailable' : session.soc_change.toFixed(1) + '%'
        ].forEach(value => text(row, 'td', value));
        tbody.appendChild(row);
      });
      if (!list.length) {
        const row = document.createElement('tr');
        text(row, 'td', 'No charging sessions recorded.').colSpan = 6;
        tbody.appendChild(row);
      }
    } catch (error) {
      $('charging-history-list').replaceChildren();
      const row = document.createElement('tr');
      text(row, 'td', 'Charging history unavailable: ' + error.message).colSpan = 6;
      $('charging-history-list').appendChild(row);
    } finally { chargingBusy = false; }
  }
  async function run(action) {
    if (diagnosticBusy) return;
    diagnosticBusy = true;
    ['btn-diagnostic-read', 'btn-expanded-read', 'btn-clear-faults'].forEach(id => { $(id).disabled = true; });
    try { await action(); }
    catch (error) { status(error.message); }
    finally {
      diagnosticBusy = false;
      ['btn-diagnostic-read', 'btn-expanded-read', 'btn-clear-faults'].forEach(id => { $(id).disabled = false; });
    }
  }
  $('btn-diagnostic-read').addEventListener('click', () => run(async () => {
    status('Reading supported OBD modules and faults…');
    const report = await request('/api/diagnostics/read', {});
    const root = $('diagnostic-results'); root.replaceChildren();
    text(root, 'p', report.scope);
    const modules = Object.values(report.modules);
    if (!modules.length) text(root, 'p', 'No valid ECU responses. Fault status is unavailable.');
    modules.forEach(module => {
      text(root, 'h3', `${module.name || 'OBD ECU'} ${module.ecu}: ${module.status}`);
      if (module.identification?.vin) text(root, 'p', 'VIN: ' + module.identification.vin);
      else text(root, 'p', 'Identification unavailable');
      for (const mode of ['03', '07', '0A']) {
        const fault = module.faults?.[mode];
        text(root, 'p', `${({'03':'Stored', '07':'Pending', '0A':'Permanent'})[mode]} faults: ` +
          (!fault || fault.status !== 'ok' ? 'Unavailable / unsupported' : fault.codes.length ? '' : 'No codes reported by this ECU'));
        if (fault?.status === 'ok') fault.codes.forEach(code => {
          text(root, 'p', code.code + ': ' + code.description);
          if (code.source) { const source = text(root, 'a', 'Verified definition source'); source.href = code.source; }
        });
      }
      if (module.supported_pid_groups) text(root, 'p', 'Supported PID groups: ' + JSON.stringify(module.supported_pid_groups));
    });
    download(root, report.id);
    status('Diagnostic report saved. Coverage is limited to responding standard OBD ECUs.');
  }));
  $('btn-expanded-read').addEventListener('click', () => run(async () => {
    status('Reading community battery PIDs…');
    const report = await request('/api/diagnostics/expanded', {});
    const root = $('expanded-results'); root.replaceChildren();
    text(root, 'h3', 'Expanded readings');
    text(root, 'p', report.validation);
    const readings = report.readings;
    text(root, 'p', 'Cell imbalance: ' + (readings.cell_imbalance_v == null ? 'Data unavailable' : readings.cell_imbalance_v.toFixed(3) + ' V'));
    text(root, 'p', 'Battery module temperatures: ' + readings.module_temperatures_c.map(v => v == null ? '—' : v + ' °C').join(', '));
    text(root, 'p', 'Motor speed: ' + (readings.motor_rpm ?? 'Data unavailable') + ' rpm');
    text(root, 'p', 'Accelerator: ' + (readings.accelerator_pedal_pct ?? 'Data unavailable') + ' %');
    text(root, 'p', 'Gear indicators: ' + (readings.gear_flags ? Object.keys(readings.gear_flags).filter(k => readings.gear_flags[k]).join(', ') : 'Data unavailable'));
    const details = document.createElement('details');
    text(details, 'summary', 'All 96 cell voltages');
    const table = document.createElement('table');
    readings.cell_voltages_v.forEach((value, index) => {
      const row = document.createElement('tr'); text(row, 'th', 'Cell ' + (index + 1));
      text(row, 'td', value == null ? 'Data unavailable' : value.toFixed(2) + ' V'); table.appendChild(row);
    });
    details.appendChild(table); root.appendChild(details);
    download(root, report.id);
    status('Expanded reading saved. Compare readings with trusted vehicle diagnostics before validating this profile.');
  }));
  $('btn-clear-faults').disabled = false;
  $('btn-clear-faults').addEventListener('click', () => run(async () => {
    status('Saving a fresh report before fault clearing…');
    clearPreparation = await request('/api/diagnostics/prepare-clear', {});
    $('clear-conditions').checked = false;
    $('confirm-clear-faults').disabled = true;
    status(`Report #${clearPreparation.report_id} saved. Confirmation expires in five minutes.`);
    download($('diagnostic-results'), clearPreparation.report_id);
    $('clear-faults-dialog').showModal();
  }));
  $('clear-conditions').addEventListener('change', () => { $('confirm-clear-faults').disabled = !$('clear-conditions').checked; });
  $('clear-faults-dialog').addEventListener('close', () => {
    const preparation = clearPreparation; clearPreparation = null;
    if ($('clear-faults-dialog').returnValue !== 'clear' || !$('clear-conditions').checked || !preparation) return;
    run(async () => {
      status('Sending confirmed standard fault-clearing request…');
      const result = await request('/api/diagnostics/clear', {
        confirmation_token: preparation.confirmation_token, confirm: true, conditions_confirmed: true
      });
      status(result.message); download($('diagnostic-results'), result.audit_id);
    });
  });
  window.addEventListener('charging-refresh', charging);
  window.addEventListener('telemetry-status', event => {
    const data = event.detail;
    const blocked = !data.obd.connected || data.preview;
    if (!diagnosticBusy) ['btn-diagnostic-read', 'btn-expanded-read', 'btn-clear-faults'].forEach(id => { $(id).disabled = blocked; });
  });
  charging();
  setInterval(() => { if (!$('tab-trips').hidden) charging(); }, 5000);
})();
