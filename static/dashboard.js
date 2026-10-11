'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const tabs = [...document.querySelectorAll('[data-tab]')];
  const names = { dashboard: 'Overview', lcd: 'LCD display', bluetooth: 'Connection', trips: 'Trip history' };
  let activeTab = 'dashboard';
  let activeTrip = false;
  let canStartTrip = false;
  let tripBusy = false;
  let statusReady = false;
  let pollBusy = false;
  let configReady = false;
  let configDirty = false;
  let configBusy = false;
  let savedConfig = {};
  let toastTimer;
  let lastTripActive;

  const number = (value) => typeof value === 'number' && Number.isFinite(value) ? value : null;
  const format = (value, digits = 1) => number(value) === null ? 'Data unavailable' : value.toFixed(digits);
  const set = (id, value) => {
    const element = $(id);
    element.textContent = value;
    if (id.startsWith('val-')) {
      const metric = element.closest('.hero-value, .secondary-value, .charge-details strong, .metric-footer strong');
      metric?.classList.toggle('unavailable', value === 'Data unavailable');
    }
  };
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

  function icon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'icon');
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', `#i-${name}`);
    svg.append(use);
    return svg;
  }

  function showToast(message, error = false) {
    clearTimeout(toastTimer);
    set('toast-text', message);
    $('toast').classList.toggle('error', error);
    $('toast').hidden = false;
    toastTimer = setTimeout(() => { $('toast').hidden = true; }, error ? 8000 : 4500);
  }

  async function api(path, options = {}) {
    const { timeout = 15000, ...requestOptions } = options;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(path, { cache: 'no-store', ...requestOptions, signal: controller.signal });
      let data;
      try { data = await response.json(); } catch { throw new Error('The monitor returned an invalid response.'); }
      if (!response.ok || data.success === false) throw new Error(data.message || `Request failed (${response.status}).`);
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('The request timed out. Please try again.');
      if (error instanceof TypeError) throw new Error('Cannot reach the monitor. Check your connection.');
      throw error;
    } finally { clearTimeout(timer); }
  }

  function post(path, data, timeout) {
    return api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data || {}), timeout });
  }

  function switchTab(name, focus = false) {
    if (!names[name]) name = 'dashboard';
    if (activeTab !== name) window.scrollTo({ top: 0, left: 0, behavior: 'instant' });
    activeTab = name;
    tabs.forEach((button) => {
      const selected = button.dataset.tab === name;
      button.classList.toggle('active', selected);
      button.setAttribute('aria-selected', String(selected));
      button.tabIndex = selected ? 0 : -1;
      $(`tab-${button.dataset.tab}`).hidden = !selected;
      if (selected && focus) button.focus();
    });
    set('page-name', names[name]);
    document.title = `IONIQ · ${names[name]}`;
    if (location.hash !== `#${name}`) history.replaceState(null, '', `#${name}`);
    if (name === 'trips') { loadTrips(); window.dispatchEvent(new Event('charging-refresh')); }
  }

  function duration(seconds) {
    const total = Math.max(0, Math.floor(number(seconds) ?? 0));
    const hours = Math.floor(total / 3600);
    const minutes = String(Math.floor(total / 60) % 60).padStart(2, '0');
    const secs = String(total % 60).padStart(2, '0');
    return hours ? `${hours}:${minutes}:${secs}` : `${minutes}:${secs}`;
  }

  function valueWithUnit(id, value, unit) {
    const small = document.createElement('small');
    small.textContent = unit;
    $(id).replaceChildren(document.createTextNode(`${value} `), small);
  }

  function renderTelemetry(obd, available) {
    const val = (key) => available && obd.fresh?.[key] !== false ? number(obd[key]) : null;
    // Zero is a real reading, including at an empty battery or while parked.
    const soc = val('soc_display') ?? val('soc_bms');
    set('val-soc', format(soc));
    set('val-soc-bms', format(val('soc_bms')));
    set('val-soh', format(val('soh')));
    set('val-speed', format(val('speed_kmh'), 0));
    set('val-rpm', format(val('motor_rpm'), 0));
    set('val-voltage', format(val('battery_voltage')));
    set('val-current', format(val('battery_current')));
    set('val-aux', format(val('aux_voltage')));
    set('val-temp', format(val('battery_max_temp'), 0));
    set('val-temp-min', format(val('battery_min_temp'), 0));
    set('val-temp-max', format(val('battery_max_temp'), 0));
    $('soc-ring').setAttribute('stroke-dasharray', `${soc === null ? 0 : clamp(soc, 0, 100)} 100`);
    set('charge-state', !available || obd.is_charging == null ? 'Data unavailable' : obd.is_charging ? 'Charging' : 'Live reading');
    $('charge-state').classList.toggle('active', available);

    const power = val('battery_power_kw');
    set('val-power', power === null ? 'Data unavailable' : `${power > 0 ? '+' : ''}${format(power)}`);
    $('power-value').classList.toggle('regen', power !== null && power < -0.5);
    $('power-value').classList.toggle('discharge', power !== null && power > 0.5);
    set('power-state', power === null ? 'Data unavailable' : obd.is_charging && obd.speed_kmh <= 2 ? 'Charging battery' : power < -0.5 ? 'Regenerating energy' : power > 0.5 ? 'Drawing energy' : 'Low power flow');
    set('max-charge', format(val('max_charge_power_kw'), 0));
    set('max-discharge', format(val('max_discharge_power_kw'), 0));
    const limit = power !== null && power < 0 ? val('max_charge_power_kw') : val('max_discharge_power_kw');
    const width = power !== null && limit > 0 ? clamp(Math.abs(power) / limit, 0, 1) * 50 : 0;
    const fill = $('power-meter-fill');
    fill.style.width = `${width}%`;
    fill.style.left = `${power !== null && power < 0 ? 50 - width : 50}%`;
    fill.style.background = power !== null && power < 0 ? 'var(--mint)' : 'var(--warm)';
  }

  function renderTrip(trip) {
    activeTrip = Boolean(trip.active);
    $('badge-trip').classList.toggle('active', activeTrip);
    set('badge-trip', trip.state === 'unavailable' ? 'Data unavailable' : trip.state === 'charging' ? 'Charging' : activeTrip ? 'Recording' : trip.state || 'Parked');
    set('trip-description', activeTrip ? `Trip #${trip.trip_id} · Your drive is being recorded.` : trip.state === 'charging' ? 'Charging is recorded separately in your history.' : !canStartTrip ? 'Fresh, non-charging vehicle data is needed to start.' : trip.distance_km > 0 ? 'Last trip recorded. Ready for your next drive.' : 'Ready when you are.');
    valueWithUnit('trip-live-dist', format(number(trip.distance_km) ?? 0, 2), 'km');
    valueWithUnit('trip-live-kwh', format(number(trip.net_kwh) ?? 0, 2), 'kWh');
    valueWithUnit('trip-live-eff', trip.distance_km >= 0.1 ? format(number(trip.efficiency_kwh_100km)) : '—', 'kWh/100km');
    set('trip-duration', duration(trip.duration_sec));
    set('trip-regen', format(number(trip.energy_regen_kwh) ?? 0, 2));
    document.querySelectorAll('[data-action="trip"]').forEach((button) => {
      button.textContent = tripBusy ? 'Updating…' : activeTrip ? 'Stop trip' : 'Start trip ↗';
      button.classList.toggle('danger', activeTrip);
      button.classList.toggle('primary', !activeTrip);
      button.disabled = tripBusy || !statusReady || (!activeTrip && !canStartTrip);
      button.title = !activeTrip && !canStartTrip ? 'Connect your adapter and wait for fresh, non-charging telemetry.' : '';
    });
    if (lastTripActive !== undefined && lastTripActive !== activeTrip && activeTab === 'trips') loadTrips();
    lastTripActive = activeTrip;
  }

  function renderDevice(config) {
    set('device-name', config.bluetooth_mac ? config.bluetooth_name || 'OBD-II adapter' : 'No adapter paired');
    set('device-mac', config.bluetooth_mac || 'Pair a device below to get started.');
    ['btn-reconnect', 'btn-forget'].forEach((id) => {
      if (!$(id).dataset.busy) $(id).disabled = !statusReady || !config.bluetooth_mac;
    });
  }

  function syncConfig(config) {
    savedConfig = config;
    renderDevice(config);
    if (!configReady) {
      $('cfg-pin').value = config.bluetooth_pin || '1234';
      configReady = true;
    }
    if (!configBusy) {
      $('cfg-auto-connect').checked = Boolean(config.auto_connect);
      $('cfg-trip-auto').checked = Boolean(config.trip_auto_record);
    }
    if (!configDirty && !configBusy) {
      $('lcd-auto-cycle').checked = Boolean(config.lcd_auto_cycle);
      $('lcd-cycle-seconds').value = config.lcd_cycle_seconds ?? 4;
      $('lcd-line1').value = config.lcd_line1 || 'soc';
      $('lcd-line2').value = config.lcd_line2 || 'power';
      updateLCDMode();
    }
  }

  function updateControls() {
    document.querySelectorAll('[data-requires-status], #cfg-auto-connect, #cfg-trip-auto').forEach((element) => {
      if (!element.dataset.busy) element.disabled = !statusReady;
    });
    $('lcd-save').disabled = !statusReady || configBusy;
    renderDevice(savedConfig);
    document.querySelectorAll('[data-connect-device]').forEach((button) => {
      if (!button.dataset.busy) button.disabled = !statusReady;
    });
  }

  function connectionState(obd, online) {
    const age = number(obd.timestamp) > 0 ? Math.max(0, Date.now() / 1000 - obd.timestamp) : Infinity;
    const core = ['speed_kmh', 'battery_power_kw', 'is_charging'];
    const hasFreshness = core.every((key) => typeof obd.fresh?.[key] === 'boolean');
    // Server-evaluated freshness avoids depending on the phone and Pi clocks agreeing.
    const available = online && Boolean(obd.connected) && (hasFreshness || age <= 10) && core.every(key => obd[key] != null && obd.fresh?.[key] !== false);
    $('badge-bt').className = `pill ${available ? 'connected' : 'disconnected'}`;
    set('connection-label', !online ? 'Monitor unreachable' : !obd.connected ? 'Adapter disconnected' : !available ? 'Data unavailable' : 'OBD connected');
    set('update-label', available ? hasFreshness || age < 2 ? 'Live · updating every second' : `Live · sample ${Math.floor(age)}s ago` : 'Data unavailable');
    document.querySelector('.refresh-note').classList.toggle('fresh', available);
    $('connection-notice').hidden = available;
    set('connection-notice-text', !online ? 'The monitor is unreachable. Retrying automatically.' : !obd.connected ? `Waiting for your adapter. ${obd.status_text || 'Connect to start receiving live data.'}` : 'Vehicle data has stopped updating. Check your adapter and vehicle.');
    return available;
  }

  async function updateStatus() {
    if (pollBusy) return;
    pollBusy = true;
    try {
      const data = await api('/api/status', { timeout: 8000 });
      if (!data || typeof data.obd !== 'object' || !data.obd || typeof data.config !== 'object' || !data.config || typeof data.trip !== 'object' || !data.trip) throw new Error('Invalid status data');
      statusReady = true;
      $('preview-badge').hidden = !data.preview;
      const available = connectionState(data.obd, true);
      canStartTrip = available && data.obd.is_charging === false && number(data.obd.speed_kmh) !== null;
      renderTelemetry(data.obd, available);
      syncConfig(data.config);
      window.dispatchEvent(new CustomEvent('telemetry-status', { detail: data }));
      renderTrip(data.trip);
      for (const line of [1, 2]) {
        const text = String(data.lcd?.[`line${line}`] || '').slice(0, 16).padEnd(16, ' ');
        document.querySelectorAll(`[data-lcd-line="${line}"]`).forEach((element) => { element.textContent = text; });
      }
      document.querySelectorAll('.lcd-screen').forEach((screen) => screen.setAttribute('aria-label', `LCD: ${data.lcd?.line1 || 'blank'}; ${data.lcd?.line2 || 'blank'}`));
    } catch {
      statusReady = false;
      canStartTrip = false;
      connectionState({}, false);
      renderTelemetry({}, false);
      set('badge-trip', 'Unknown');
      $('badge-trip').classList.remove('active');
      set('trip-description', 'Reconnect to the monitor to check trip status.');
      document.querySelectorAll('[data-action="trip"]').forEach((button) => { button.disabled = true; });
    } finally {
      updateControls();
      pollBusy = false;
    }
  }

  async function poll() {
    await updateStatus();
    setTimeout(poll, document.hidden ? 4000 : 1000);
  }

  async function runAction(button, busyText, action, success) {
    if (button.dataset.busy || button.disabled) return;
    const original = [...button.childNodes].map((node) => node.cloneNode(true));
    button.dataset.busy = 'true';
    button.disabled = true;
    button.textContent = busyText;
    try {
      const result = await action();
      showToast(typeof success === 'function' ? success(result) : success);
      await updateStatus();
      return true;
    } catch (error) { showToast(error.message, true); return false; }
    finally {
      delete button.dataset.busy;
      button.replaceChildren(...original);
      button.disabled = !statusReady;
      updateControls();
    }
  }

  function updateLCDMode() {
    const auto = $('lcd-auto-cycle').checked;
    $('lcd-auto-cycle').disabled = configBusy;
    $('lcd-cycle-seconds').disabled = !auto || configBusy;
    $('lcd-line1').disabled = auto || configBusy;
    $('lcd-line2').disabled = auto || configBusy;
    set('lcd-mode-hint', auto ? 'Turn off auto-cycle to choose fixed metrics.' : 'Fixed metrics share each line with speed or auxiliary voltage.');
  }

  async function saveToggle(input, key) {
    if (configBusy) { input.checked = Boolean(savedConfig[key]); return; }
    const value = input.checked;
    configBusy = true;
    input.dataset.busy = 'true';
    input.disabled = true;
    try {
      const result = await post('/api/config', { [key]: value });
      savedConfig = result.config || { ...savedConfig, [key]: value };
      showToast(key === 'auto_connect' ? 'Startup connection setting saved.' : 'Automatic trip recording setting saved.');
    } catch (error) { input.checked = Boolean(savedConfig[key]); showToast(error.message, true); }
    finally { configBusy = false; delete input.dataset.busy; updateControls(); }
  }

  async function toggleTrip() {
    if (tripBusy || !statusReady || (!activeTrip && !canStartTrip)) return;
    tripBusy = true;
    document.querySelectorAll('[data-action="trip"]').forEach((button) => { button.disabled = true; button.textContent = 'Updating…'; });
    try {
      await post(activeTrip ? '/api/trips/stop' : '/api/trips/start');
      showToast(activeTrip ? 'Trip saved to your history.' : 'Trip recording started.');
      await updateStatus();
      if (activeTab === 'trips') await loadTrips();
    } catch (error) { showToast(error.message, true); }
    finally {
      tripBusy = false;
      document.querySelectorAll('[data-action="trip"]').forEach((button) => {
        button.disabled = !statusReady || (!activeTrip && !canStartTrip);
        button.textContent = activeTrip ? 'Stop trip' : 'Start trip ↗';
        button.classList.toggle('danger', activeTrip);
        button.classList.toggle('primary', !activeTrip);
      });
    }
  }

  function emptyDevices(title, description) {
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    const strong = document.createElement('strong'); strong.textContent = title;
    const p = document.createElement('p'); p.textContent = description;
    empty.append(icon('bluetooth'), strong, p);
    $('bt-device-list').replaceChildren(empty);
  }

  async function scanBluetooth() {
    const button = $('btn-scan');
    if (button.dataset.busy) return;
    button.dataset.busy = 'true'; button.disabled = true;
    set('scan-status', 'Scanning nearby devices. This may take a few seconds…');
    button.replaceChildren(icon('search'), document.createTextNode('Scanning…'));
    try {
      const list = await api('/api/bluetooth/scan', { timeout: 60000 });
      if (!Array.isArray(list)) throw new Error('The scan returned an invalid device list.');
      $('bt-device-list').replaceChildren();
      if (!list.length) emptyDevices('No devices found', 'Check that your adapter is plugged in and your vehicle is on, then scan again.');
      list.forEach((device) => {
        const row = document.createElement('div'); row.className = 'device-row';
        const badge = document.createElement('span'); badge.className = 'device-icon'; badge.append(icon('bluetooth'));
        const info = document.createElement('div'); info.className = 'device-info';
        const strong = document.createElement('strong'); strong.textContent = device.name || 'Unnamed adapter';
        const mac = document.createElement('code'); mac.textContent = device.mac || 'Unknown address';
        info.append(strong, mac);
        const meta = document.createElement('span'); meta.className = 'device-meta';
        const rssi = typeof device.rssi === 'string' && device.rssi.trim() ? number(Number(device.rssi)) : number(device.rssi);
        meta.textContent = `${device.paired ? 'Paired' : 'Not paired'}${rssi !== null ? ` · ${rssi} dBm` : ''}`;
        const connect = document.createElement('button'); connect.className = 'button secondary'; connect.textContent = 'Pair & connect'; connect.dataset.connectDevice = 'true';
        connect.disabled = !statusReady || !device.mac;
        connect.addEventListener('click', () => runAction(connect, 'Connecting…', () => post('/api/bluetooth/connect', { mac: device.mac, name: device.name || '', pin: $('cfg-pin').value || '1234' }, 60000), 'Adapter connected and remembered.'));
        row.append(badge, info, meta, connect); $('bt-device-list').append(row);
      });
      set('scan-status', `Found ${list.length} ${list.length === 1 ? 'device' : 'devices'}. Choose your OBD-II adapter.`);
    } catch (error) { set('scan-status', error.message); emptyDevices('Scan unavailable', 'Try scanning again when the monitor is reachable.'); showToast(error.message, true); }
    finally { delete button.dataset.busy; button.disabled = !statusReady; button.replaceChildren(icon('search'), document.createTextNode('Scan for devices')); }
  }

  function tripDate(value) {
    if (!value) return 'Date unavailable';
    // SQLite stores local wall time without an offset; avoid interpreting it as UTC.
    const date = new Date(String(value).replace(' ', 'T'));
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString(undefined, { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  function tableMessage(message) {
    const row = document.createElement('tr'); const cell = document.createElement('td');
    cell.colSpan = 7; cell.className = 'table-empty'; cell.textContent = message; row.append(cell);
    $('trip-history-list').replaceChildren(row);
  }

  let tripsBusy = false;
  async function loadTrips() {
    if (tripsBusy) return;
    tripsBusy = true; $('btn-refresh-trips').disabled = true;
    set('history-status', 'Loading your recent drives…');
    try {
      const list = await api('/api/trips');
      if (!Array.isArray(list)) throw new Error('The monitor returned an invalid trip list.');
      set('history-count', list.length);
      set('history-distance', list.reduce((sum, trip) => sum + (number(trip.distance_km) ?? 0), 0).toFixed(1));
      set('history-energy', list.reduce((sum, trip) => sum + (number(trip.net_kwh) ?? 0), 0).toFixed(2));
      $('trip-history-list').replaceChildren();
      if (!list.length) tableMessage('No trips yet. Your next drive is a good place to start.');
      list.forEach((trip) => {
        const row = document.createElement('tr');
        const dateCell = document.createElement('td');
        const title = document.createElement('strong'); title.textContent = `Trip #${trip.id}${trip.status === 'active' ? ' · Recording' : trip.status === 'interrupted' ? ' · Interrupted' : ''}`;
        const date = document.createElement('span'); date.className = 'trip-date'; date.textContent = tripDate(trip.start_time);
        dateCell.append(title, date); row.append(dateCell);
        const efficiency = number(trip.net_kwh) !== null && trip.distance_km >= 0.1 ? trip.net_kwh / trip.distance_km * 100 : null;
        [ `${format(number(trip.distance_km), 2)} km`, `${format(number(trip.net_kwh), 2)} kWh`, `${format(efficiency)} kWh/100km`, `${format(number(trip.max_speed_kmh), 0)} km/h`, `${format(number(trip.start_soc))}% → ${format(number(trip.end_soc))}%` ].forEach((value) => { const cell = document.createElement('td'); cell.textContent = value; row.append(cell); });
        const exportCell = document.createElement('td');
        if (Number.isInteger(trip.id) && trip.id > 0) {
          const link = document.createElement('a'); link.href = `/api/trips/${trip.id}/csv`; link.download = `ioniq_trip_${trip.id}.csv`; link.className = 'button secondary'; link.setAttribute('aria-label', `Download CSV for trip ${trip.id}`); link.append(icon('download'), document.createTextNode('CSV')); exportCell.append(link);
        }
        row.append(exportCell); $('trip-history-list').append(row);
      });
      set('history-status', list.length ? 'Select a trip’s CSV to download its telemetry.' : 'Trips appear here after recording begins.');
    } catch (error) {
      tableMessage('Trip history is unavailable. Use Refresh to try again.');
      ['history-count', 'history-distance', 'history-energy'].forEach((id) => set(id, '—'));
      set('history-status', error.message); showToast(error.message, true);
    } finally { tripsBusy = false; $('btn-refresh-trips').disabled = false; }
  }

  tabs.forEach((button, index) => {
    button.addEventListener('click', () => switchTab(button.dataset.tab));
    button.addEventListener('keydown', (event) => {
      let target;
      if (['ArrowDown', 'ArrowRight'].includes(event.key)) target = (index + 1) % tabs.length;
      if (['ArrowUp', 'ArrowLeft'].includes(event.key)) target = (index - 1 + tabs.length) % tabs.length;
      if (event.key === 'Home') target = 0;
      if (event.key === 'End') target = tabs.length - 1;
      if (target !== undefined) { event.preventDefault(); switchTab(tabs[target].dataset.tab, true); }
    });
  });
  document.querySelectorAll('[data-open]').forEach((button) => button.addEventListener('click', () => switchTab(button.dataset.open)));
  window.addEventListener('hashchange', () => switchTab(location.hash.slice(1)));
  document.addEventListener('visibilitychange', () => { if (!document.hidden) updateStatus(); });
  $('toast-close').addEventListener('click', () => { $('toast').hidden = true; });
  $('btn-scan').addEventListener('click', scanBluetooth);
  $('btn-reconnect').addEventListener('click', () => runAction($('btn-reconnect'), 'Connecting…', () => post('/api/bluetooth/reconnect', {}, 60000), (result) => result.message || 'Adapter reconnected.'));
  $('btn-forget').addEventListener('click', () => $('forget-dialog').showModal());
  $('forget-dialog').addEventListener('close', () => {
    if ($('forget-dialog').returnValue === 'forget') runAction($('btn-forget'), 'Forgetting…', () => post('/api/bluetooth/forget', {}, 60000), 'Adapter forgotten.');
  });
  $('cfg-auto-connect').addEventListener('change', () => saveToggle($('cfg-auto-connect'), 'auto_connect'));
  $('cfg-trip-auto').addEventListener('change', () => saveToggle($('cfg-trip-auto'), 'trip_auto_record'));
  document.querySelectorAll('[data-action="trip"]').forEach((button) => button.addEventListener('click', toggleTrip));
  $('btn-refresh-trips').addEventListener('click', loadTrips);
  $('lcd-config-form').addEventListener('input', () => { configDirty = true; set('lcd-save-status', 'You have unsaved changes.'); updateLCDMode(); });
  $('lcd-config-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (configBusy || !statusReady) return;
    configBusy = true;
    const payload = { lcd_auto_cycle: $('lcd-auto-cycle').checked, lcd_cycle_seconds: Number($('lcd-cycle-seconds').value), lcd_line1: $('lcd-line1').value, lcd_line2: $('lcd-line2').value };
    updateLCDMode();
    const success = await runAction($('lcd-save'), 'Saving…', () => post('/api/config', payload), 'LCD layout saved.');
    if (success) { configDirty = false; savedConfig = { ...savedConfig, ...payload }; set('lcd-save-status', 'Layout saved to your display.'); }
    configBusy = false; updateLCDMode(); updateControls();
  });
  $('lcd-message-form').addEventListener('submit', (event) => {
    event.preventDefault();
    runAction(event.submitter, 'Sending…', () => post('/api/lcd/message', { message: `${$('custom-lcd-line1').value}\n${$('custom-lcd-line2').value}`, duration: 10 }), 'Message shown for 10 seconds.');
  });
  $('lcd-clear').addEventListener('click', () => runAction($('lcd-clear'), 'Clearing…', () => post('/api/lcd/clear'), 'Display cleared. Live updates resume automatically.'));
  switchTab(location.hash.slice(1) || 'dashboard');
  poll();
})();
