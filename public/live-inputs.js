/* VEIL live telemetry: gateway registration, live device markers, metadata
   inspection, and day replay controls. */
(function attachLiveInputs(global) {
  'use strict';

  const BLUE = 0x4ea8de;
  const REPLAY = 0xf2c14e;
  const DEFAULT_DEVICE_COLOR = '#4ea8de';
  const REPLAY_FRAME_MS = 50;
  const REPLAY_MAX_INTERPOLATE_GAP_MS = 2 * 60 * 1000;
  const REPLAY_MAX_INTERPOLATE_SPEED_MPS = 12;
  const TOKEN_KEY = 'veil-live-token';
  const SSE_ERROR_ESCALATE_COUNT = 3;
  const SSE_ERROR_ESCALATE_MS = 30 * 1000;
  const LIVE_FRESHNESS_TICK_MS = 1000;
  const LIVE_RECONNECT_MIN_INTERVAL_MS = 3000;
  const DEFAULT_ACTIVE_AFTER_SECONDS = 2 * 60;
  const DEFAULT_OFFLINE_AFTER_SECONDS = 15 * 60;

  function cleanGatewayText(value) {
    return typeof value === 'string' ? value.trim() : '';
  }

  function gatewayMatchesRegistration(gateway, request = {}) {
    if (!gateway) return false;
    const requestId = cleanGatewayText(request.gateway_id || request.device_id || request.id);
    if (requestId && gateway.id === requestId) return true;
    const requestAddress = cleanGatewayText(request.address);
    if (requestAddress && gateway.address === requestAddress && gateway.transport === request.transport) return true;
    const requestName = cleanGatewayText(request.name);
    if (requestName && (gateway.name === requestName || gateway.id === requestName)) return true;
    return false;
  }

  function resolveRegisteredGateway(payload = {}, request = {}, currentGateways = []) {
    if (payload.gateway) return payload.gateway;
    const registryGateways = Array.isArray(payload.registry?.gateways) ? payload.registry.gateways : [];
    return [...registryGateways, ...currentGateways].find((gateway) => gatewayMatchesRegistration(gateway, request)) || null;
  }

  function gatewaySuccessMessage(payload = {}, request = {}, currentGateways = []) {
    const gateway = resolveRegisteredGateway(payload, request, currentGateways);
    const label = cleanGatewayText(gateway?.name) ||
      cleanGatewayText(gateway?.id) ||
      cleanGatewayText(request.name) ||
      cleanGatewayText(request.gateway_id || request.device_id || request.id) ||
      'gateway';
    const bridgeState = cleanGatewayText(gateway?.bridge?.state) || 'registered';
    return `Gateway ${label} ${bridgeState}.`;
  }

  function destructiveActionConfirmationMessage(action, label = '') {
    const target = cleanGatewayText(label);
    if (action === 'remove-gateway') {
      return `Remove ${target || 'this gateway'} from live inputs? Linked/current child devices routed through this gateway will also be removed.`;
    }
    if (action === 'remove-device') {
      return `Remove ${target || 'this device'} from live inputs?`;
    }
    if (action === 'remove-live-token') {
      return 'Remove the live token? The live API will be open to anyone who can reach this server.';
    }
    return 'Continue with this destructive action?';
  }

  function confirmDestructiveAction(action, label = '', confirmFn = global.confirm) {
    if (typeof confirmFn !== 'function') return false;
    return confirmFn(destructiveActionConfirmationMessage(action, label)) === true;
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function firstObject(...values) {
    return values.find((value) => value && typeof value === 'object' && !Array.isArray(value)) || null;
  }

  function liveDeviceMetrics(device = {}) {
    return firstObject(
      device.battery,
      device.data?.battery,
      device.metadata?.node?.deviceMetrics,
      device.data?.node?.deviceMetrics,
      device.data?.telemetry?.device_metrics,
      device.data?.telemetry?.deviceMetrics,
      device.metadata?.telemetry?.device_metrics,
      device.metadata?.telemetry?.deviceMetrics,
    );
  }

  function liveBatteryLevel(device = {}) {
    const metrics = liveDeviceMetrics(device);
    const raw = Number(metrics?.battery_level ?? metrics?.batteryLevel ?? metrics?.battery_level_pct);
    return Number.isFinite(raw) ? raw : null;
  }

  function formatBatteryLabel(level) {
    if (!Number.isFinite(level)) return 'Battery --';
    if (level > 100) return 'Powered';
    if (level < 0) return 'Battery --';
    return `${Math.round(level)}%`;
  }

  function formatSpeed(speedMps) {
    return Number.isFinite(speedMps) ? `${speedMps.toFixed(speedMps >= 10 ? 0 : 1)} m/s` : '--';
  }

  function formatHeading(headingDeg) {
    return Number.isFinite(headingDeg) ? `${Math.round(headingDeg)} deg` : '--';
  }

  function buildTrackerSummaryHtml(device) {
    const battery = liveBatteryLevel(device);
    const batteryPct = Number.isFinite(battery) ? Math.max(0, Math.min(100, battery)) : 0;
    const hasBattery = Number.isFinite(battery);
    const speed = device.motion?.speed_mps;
    const heading = device.motion?.heading_deg;
    if (!hasBattery && !Number.isFinite(speed) && !Number.isFinite(heading)) return '';
    return `<div class="live-tracker-summary" aria-label="Tracker status">
      <div class="live-battery" title="${escapeHtml(formatBatteryLabel(battery))}">
        <span class="live-battery-icon" aria-hidden="true"><span style="width:${batteryPct}%"></span></span>
        <span class="live-battery-label">${escapeHtml(formatBatteryLabel(battery))}</span>
      </div>
      <div class="live-motion-pill"><span>Heading</span><strong>${escapeHtml(formatHeading(heading))}</strong></div>
      <div class="live-motion-pill"><span>Speed</span><strong>${escapeHtml(formatSpeed(speed))}</strong></div>
    </div>`;
  }

  function buildLiveMetadataHtml(device, freshnessLabelFn) {
    const batteryLevel = liveBatteryLevel(device);
    const metrics = liveDeviceMetrics(device);
    const rows = [
      ['Device', device.device_id],
      ['Label', device.label || device.device_id],
      ['Observed', device.observed_at],
      ['Received', device.received_at],
      ['Location state', freshnessLabelFn(device)],
      ['State reason', device.freshness?.reason],
      ['Location observed', device.freshness?.position_observed_at],
      ['Kind', device.kind],
      ['Lat, Lon', device.position ? `${device.position.lat.toFixed(7)}, ${device.position.lon.toFixed(7)}` : null],
      ['Altitude', Number.isFinite(device.position?.alt_m) ? `${device.position.alt_m} m` : null],
      ['Battery', Number.isFinite(batteryLevel) ? `${formatBatteryLabel(batteryLevel)}${Number.isFinite(metrics?.voltage) ? ` · ${metrics.voltage} V` : ''}` : null],
      ['Speed', Number.isFinite(device.motion?.speed_mps) ? formatSpeed(device.motion.speed_mps) : null],
      ['Heading', Number.isFinite(device.motion?.heading_deg) ? formatHeading(device.motion.heading_deg) : null],
      ['Gateway', device.link?.gateway_id],
      ['SNR', Number.isFinite(device.link?.snr_db) ? `${device.link.snr_db} dB` : null],
      ['RSSI', Number.isFinite(device.link?.rssi_dbm) ? `${device.link.rssi_dbm} dBm` : null],
      ['Source', device.source ? `${device.source.protocol || 'unknown'} / ${device.source.transport || 'unknown'}` : null],
      ['Ingress', device.source?.ingress_transport],
      ['Data', device.data ? JSON.stringify(device.data, null, 2) : null],
    ].filter((r) => r[1] !== undefined && r[1] !== null && r[1] !== '');
    const body = rows.map(([k, v]) =>
      `<div class="info-row"><span class="info-k">${escapeHtml(k)}</span><span class="info-v">${escapeHtml(v)}</span></div>`).join('');
    const msg = device.message ? `<p class="live-message">${escapeHtml(device.message)}</p>` : '';
    const summary = buildTrackerSummaryHtml(device);
    const details = body ? `<details class="live-device-details">
      <summary class="collapsible-header"><span>More telemetry</span></summary>
      <div class="collapsible-body">${body}</div>
    </details>` : '';
    return `<div class="info-card live-info-card" data-live-device-id="${escapeHtml(device.device_id)}">
      <p class="info-layer">Live telemetry</p>
      <p class="info-title">${escapeHtml(device.label || device.device_id)}</p>
      ${summary}${msg}${details}
    </div>`;
  }

  function safeParseSseJson(data, eventName = 'message') {
    try {
      return { ok: true, value: JSON.parse(data) };
    } catch (err) {
      return {
        ok: false,
        error: err,
        message: `Ignored malformed live telemetry ${eventName} frame: ${err?.message || 'invalid JSON'}.`,
      };
    }
  }

  function closeActiveEventSource(state) {
    const stream = state?.liveStream;
    if (!stream) return false;
    state.liveStream = null;
    try {
      stream.close?.();
    } catch (_err) {
      /* closing during page teardown must be best-effort */
    }
    return true;
  }

  function clearTimerState(state, key, clearFn = clearInterval) {
    const timer = state?.[key];
    if (!timer) return false;
    state[key] = null;
    clearFn(timer);
    return true;
  }

  function responseStatusText(res) {
    const status = Number.isFinite(res?.status) ? res.status : null;
    const label = cleanGatewayText(res?.statusText);
    if (status && label) return `${status} ${label}`;
    if (status) return String(status);
    return 'request failed';
  }

  function replayLoadErrorMessage(label, res, payload = null, parseError = null) {
    const serverMessage = cleanGatewayText(payload?.error || payload?.message);
    if (!res?.ok) {
      return serverMessage || `Could not load ${label} (${responseStatusText(res)}).`;
    }
    if (parseError) {
      return `Could not read ${label}: server returned invalid JSON.`;
    }
    return serverMessage || `Could not load ${label}.`;
  }

  async function parseReplayJsonResponse(res, label) {
    let payload;
    try {
      payload = await res.json();
    } catch (err) {
      throw new Error(replayLoadErrorMessage(label, res, null, err));
    }
    if (!res.ok || payload?.ok === false) {
      throw new Error(replayLoadErrorMessage(label, res, payload));
    }
    return payload;
  }

  function stopReplayPlayback(state, replayPlay, clearFn = clearInterval) {
    const stopped = clearTimerState(state, 'replayTimer', clearFn);
    if (replayPlay) replayPlay.textContent = 'Play';
    return stopped;
  }

  function normalizePromptLabel(value) {
    if (value === null || value === undefined) return null;
    const label = String(value).trim();
    return label || null;
  }

  function sortedTimeIndexFor(items, targetMs, timeForItem) {
    if (!items?.length || targetMs === null) return -1;
    let lo = 0;
    let hi = items.length - 1;
    let best = -1;
    while (lo <= hi) {
      const mid = Math.floor((lo + hi) / 2);
      const midMs = timeForItem(items[mid]);
      if (midMs !== null && midMs <= targetMs) {
        best = mid;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }
    return best;
  }

  function setFromIterable(values = []) {
    return values instanceof Set ? values : new Set(values);
  }

  function shouldPruneLiveHydrationDevice(deviceId, incomingDeviceIds, {
    replayMode = false,
    replayDeviceIds = [],
  } = {}) {
    if (setFromIterable(incomingDeviceIds).has(deviceId)) return false;
    if (replayMode && setFromIterable(replayDeviceIds).has(deviceId)) return false;
    return true;
  }

  function shouldApplyLiveHydrationDevice(deviceId, {
    replayMode = false,
    replayDeviceIds = [],
  } = {}) {
    return !(replayMode && setFromIterable(replayDeviceIds).has(deviceId));
  }

  function gatewayLinkedDeviceIds(devices, gatewayId, gatewayIdForDevice) {
    if (!gatewayId || typeof gatewayIdForDevice !== 'function') return [];
    const entries = typeof devices?.entries === 'function' ? devices.entries() : Object.entries(devices || {});
    return [...entries]
      .filter(([, device]) => gatewayIdForDevice(device) === gatewayId)
      .map(([deviceId]) => deviceId);
  }

  function recordSseSuccess(state, now = Date.now()) {
    state.liveStreamErrorCount = 0;
    state.liveStreamLastOkMs = now;
  }

  function sseErrorStatus({
    errorCount = 0,
    disconnectedMs = 0,
    retryThreshold = SSE_ERROR_ESCALATE_COUNT,
    elapsedThreshold = SSE_ERROR_ESCALATE_MS,
  } = {}) {
    const escalated = errorCount >= retryThreshold || disconnectedMs >= elapsedThreshold;
    if (!escalated) {
      return {
        tone: 'warn',
        text: 'Live stream disconnected; retrying in the background.',
      };
    }
    const elapsedSeconds = Math.max(1, Math.round(disconnectedMs / 1000));
    return {
      tone: 'err',
      text: `Live stream has been disconnected for ${elapsedSeconds}s after ${errorCount} retry event${errorCount === 1 ? '' : 's'}; browser retries continue, but telemetry may be stale.`,
    };
  }

  function controlList(controls) {
    if (!controls) return [];
    if (Array.isArray(controls)) return controls;
    return [controls];
  }

  function setControlsDisabled(controls, disabled) {
    const snapshots = [];
    controlList(controls).forEach((control) => {
      if (!control || !('disabled' in control)) return;
      snapshots.push([control, control.disabled]);
      control.disabled = disabled;
    });
    return () => {
      snapshots.forEach(([control, wasDisabled]) => {
        control.disabled = wasDisabled;
      });
    };
  }

  function createInFlightGuard() {
    const pending = new Set();
    return {
      isPending(key) {
        return pending.has(key);
      },
      async run(key, task, controls = []) {
        if (pending.has(key)) return { skipped: true };
        pending.add(key);
        const restore = setControlsDisabled(controls, true);
        try {
          return { skipped: false, value: await task() };
        } finally {
          restore();
          pending.delete(key);
        }
      },
    };
  }

  function timestampMs(value) {
    if (!value) return null;
    const ms = new Date(value).valueOf();
    return Number.isFinite(ms) ? ms : null;
  }

  function replayFrameTimestampMs(clockMs, event = null) {
    if (clockMs === null || clockMs === undefined || clockMs === '') {
      return timestampMs(event?.observed_at || event?.received_at);
    }
    const numericMs = Number(clockMs);
    if (Number.isFinite(numericMs)) return numericMs;
    return timestampMs(event?.observed_at || event?.received_at);
  }

  function defaultReplayTimestampFormatter(date) {
    try {
      return date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'medium' });
    } catch (_err) {
      return date.toLocaleString();
    }
  }

  function formatReplayTimestamp(clockMs, event = null, formatter = defaultReplayTimestampFormatter) {
    const ms = replayFrameTimestampMs(clockMs, event);
    if (ms === null) return event?.observed_at || event?.received_at || '';
    const date = new Date(ms);
    const formatted = typeof formatter === 'function' ? formatter(date) : defaultReplayTimestampFormatter(date);
    return formatted === null || formatted === undefined ? '' : String(formatted);
  }

  function replayFrameExportTimestamp(clockMs, event = null) {
    const ms = replayFrameTimestampMs(clockMs, event);
    if (ms !== null) return new Date(ms).toISOString();
    return event?.observed_at || event?.received_at || null;
  }

  function finiteNumber(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }

  function positiveThresholdSeconds(value, fallback) {
    const n = finiteNumber(value);
    return n !== null && n > 0 ? n : fallback;
  }

  function liveFreshnessPositionMs(device = {}) {
    const freshness = device.freshness || {};
    return timestampMs(freshness.position_observed_at) ??
      timestampMs(device.position_observed_at) ??
      timestampMs(freshness.position_received_at) ??
      timestampMs(device.position_received_at) ??
      timestampMs(device.observed_at) ??
      timestampMs(device.received_at);
  }

  function liveFreshnessLastPacketMs(device = {}) {
    const freshness = device.freshness || {};
    return timestampMs(freshness.last_event_received_at) ??
      timestampMs(device.last_event_received_at) ??
      timestampMs(device.received_at) ??
      timestampMs(freshness.last_event_observed_at) ??
      timestampMs(device.last_event_observed_at) ??
      timestampMs(device.observed_at);
  }

  function computeLiveDeviceFreshness(device = {}, nowMs = Date.now()) {
    const previous = device.freshness || {};
    const positionMs = liveFreshnessPositionMs(device);
    const lastPacketMs = liveFreshnessLastPacketMs(device);
    const activeAfter = positiveThresholdSeconds(previous.active_after_seconds, DEFAULT_ACTIVE_AFTER_SECONDS);
    const offlineAfter = Math.max(
      activeAfter,
      positiveThresholdSeconds(previous.offline_after_seconds, DEFAULT_OFFLINE_AFTER_SECONDS),
    );
    const ageSeconds = positionMs === null
      ? finiteNumber(previous.age_seconds)
      : Math.round(Math.max(0, nowMs - positionMs) / 1000);
    const lastPacketAgeSeconds = lastPacketMs === null
      ? finiteNumber(previous.last_packet_age_seconds)
      : Math.round(Math.max(0, nowMs - lastPacketMs) / 1000);
    const ageMs = positionMs === null ? null : Math.max(0, nowMs - positionMs);
    const gatewayState = previous.gateway_state || null;
    const gatewayOffline = previous.gateway_id && gatewayState && gatewayState !== 'running';
    let stateName = previous.state || (device.position ? 'unknown' : 'no_location');
    let reason = previous.reason || null;

    if (!device.position) {
      stateName = 'no_location';
      reason = 'no location packet has been received';
    } else if (gatewayOffline) {
      stateName = 'offline';
      reason = `gateway bridge is ${gatewayState}`;
    } else if (ageMs === null) {
      stateName = previous.state || 'unknown';
      reason = previous.reason || 'location timestamp is unavailable';
    } else if (ageMs > offlineAfter * 1000) {
      stateName = 'offline';
      reason = 'location is too old';
    } else if (ageMs > activeAfter * 1000) {
      stateName = 'stale';
      reason = 'location has not updated recently';
    } else {
      stateName = 'active';
      reason = 'location is current';
    }

    return {
      ...previous,
      state: stateName,
      active: stateName === 'active',
      stale: stateName !== 'active',
      reason,
      age_seconds: ageSeconds,
      active_after_seconds: activeAfter,
      offline_after_seconds: offlineAfter,
      last_packet_age_seconds: lastPacketAgeSeconds,
    };
  }

  function create(viewer, scene) {
    const { THREE, VEILGeoref, VEILTerrain } = global;
    const grid = viewer.terrainGrid;
    const group = new THREE.Group();
    group.renderOrder = 997;
    viewer.scene.add(group);
    const trackGroup = new THREE.Group();
    trackGroup.renderOrder = 996;
    group.add(trackGroup);

    const state = {
      gateways: [],
      devices: new Map(),
      markers: new Map(),
      replayTracks: new Map(),
      replaySamplesByDevice: new Map(),
      replayTrackPoints: new Map(),
      replayLabelsByDevice: new Map(),
      replayEvents: [],
      replayPov: false,
      replayPovDeviceId: null,
      replayIndex: 0,
      replayFloatIndex: 0,
      replayClockMs: null,
      replayTimer: null,
      replayMode: false,
      discovered: [],
      liveStream: null,
      liveStreamErrorCount: 0,
      liveStreamOpenedAtMs: null,
      liveStreamLastOkMs: null,
      liveStreamConnecting: false,
      liveStreamReconnectAtMs: null,
      liveHiddenSinceMs: null,
      freshnessTimer: null,
      liveStatusManaged: false,
      discoveryMode: false,
    };

    const els = {
      form: document.getElementById('live-gateway-form'),
      gatewayName: document.getElementById('live-gateway-name'),
      transport: document.getElementById('live-gateway-transport'),
      address: document.getElementById('live-gateway-address'),
      discover: document.getElementById('live-discover'),
      deviceSelect: document.getElementById('live-device-select'),
      discoverStatus: document.getElementById('live-discover-status'),
      status: document.getElementById('live-status'),
      gateways: document.getElementById('live-gateways'),
      replayToggle: document.getElementById('live-replay-toggle'),
      replayBar: document.getElementById('live-replay-bar'),
      replayPlay: document.getElementById('live-replay-play'),
      replayDay: document.getElementById('live-replay-day'),
      replaySpeed: document.getElementById('live-replay-speed'),
      replayProgress: document.getElementById('live-replay-progress'),
      replayExport: document.getElementById('live-replay-export'),
      replaySnapshot: document.getElementById('live-replay-snapshot'),
      replayPov: document.getElementById('live-replay-pov'),
      replayStatus: document.getElementById('live-replay-status'),
      accessRow: document.getElementById('live-access'),
      accessStatus: document.getElementById('live-access-status'),
      accessBtn: document.getElementById('live-access-token'),
    };
    const actionGuard = createInFlightGuard();

    function formControls(form) {
      return form ? [...form.querySelectorAll('button, input, select, textarea')] : [];
    }

    function setStatus(text, tone) {
      if (!els.status) return;
      els.status.textContent = text;
      els.status.className = `live-status${tone ? ` ${tone}` : ''}`;
      state.liveStatusManaged = false;
    }

    function setDiscoverStatus(text, tone) {
      if (!els.discoverStatus) return;
      els.discoverStatus.textContent = text;
      els.discoverStatus.className = `live-status${tone ? ` ${tone}` : ''}`;
    }

    function storedLiveToken() {
      try {
        return (localStorage.getItem(TOKEN_KEY) || '').trim();
      } catch (_err) {
        return '';
      }
    }

    function rememberLiveToken(token) {
      try {
        localStorage.setItem(TOKEN_KEY, token);
      } catch (_err) { /* storage may be unavailable */ }
    }

    function liveUrl(url, token = storedLiveToken()) {
      if (!token) return url;
      const parsed = new URL(url, window.location.href);
      parsed.searchParams.set('token', token);
      return `${parsed.pathname}${parsed.search}${parsed.hash}`;
    }

    function liveTelemetryUrl(path) {
      const parsed = new URL(path, window.location.href);
      if (state.discoveryMode) parsed.searchParams.set('discovery', '1');
      return `${parsed.pathname}${parsed.search}${parsed.hash}`;
    }

    async function refreshLiveAccess() {
      if (!els.accessRow) return;
      let status;
      try {
        status = await (await fetch('/api/live/token', { cache: 'no-store' })).json();
      } catch (_err) { return; }
      els.accessRow.hidden = false;
      const protectedTwin = !!status.protected;
      els.accessStatus.textContent = protectedTwin
        ? 'Token required — safe to publish to the web.'
        : 'Open access — fine locally; set a token before publishing to the web.';
      els.accessStatus.className = `live-status${protectedTwin ? ' ok' : ''}`;
      els.accessBtn.textContent = protectedTwin ? 'Remove token' : 'Generate token';
      // Token management is loopback-only; env-locked tokens can't be edited here.
      const editable = status.can_manage && !status.env_locked;
      els.accessBtn.disabled = !editable;
      els.accessBtn.title = status.env_locked
        ? 'VEIL_LIVE_TOKEN is set in the environment; unset it to manage the token here'
        : (status.can_manage ? '' : 'Set the token from the machine running VEIL');
    }

    async function manageLiveToken() {
      const removing = els.accessBtn.textContent === 'Remove token';
      if (removing && !confirmDestructiveAction('remove-live-token', 'live access')) return;
      els.accessBtn.disabled = true;
      try {
        const res = await fetch('/api/live/token', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(removing ? { token: '' } : { generate: true }),
        });
        const payload = await res.json().catch(() => ({}));
        if (!res.ok) { setStatus(payload.error || 'Could not update the live token.', 'err'); return; }
        if (payload.token) {
          rememberLiveToken(payload.token);
          await navigator.clipboard?.writeText(payload.token).catch(() => {});
          setStatus(`Token set and copied: ${payload.token} — restart gateways to use it.`, 'ok');
        } else {
          rememberLiveToken('');
          setStatus('Live token removed; access is open again.', 'ok');
        }
      } catch (err) {
        setStatus(err.message, 'err');
      } finally {
        refreshLiveAccess();
      }
    }

    async function liveFetch(url, options = {}, retried = false) {
      const token = storedLiveToken();
      const headers = new Headers(options.headers || {});
      if (token && !headers.has('Authorization') && !headers.has('X-VEIL-Live-Token')) {
        headers.set('Authorization', `Bearer ${token}`);
      }
      const res = await fetch(url, { ...options, headers });
      if (res.status === 403 && !retried) {
        const entered = window.prompt('This twin requires a live telemetry token:');
        if (!entered) return res;
        rememberLiveToken(entered.trim());
        return liveFetch(url, options, true);
      }
      return res;
    }

    function cleanColor(value) {
      const text = String(value || '').trim();
      return /^#[0-9a-fA-F]{6}$/.test(text) ? text.toLowerCase() : DEFAULT_DEVICE_COLOR;
    }

    function colorInt(value) {
      return Number.parseInt(cleanColor(value).slice(1), 16);
    }

    function deviceGatewayId(device) {
      return device?.freshness?.gateway_id || device?.link?.gateway_id || device?.gateway_id || null;
    }

    function freshnessState(device) {
      return device?.freshness?.state || (device?.position ? 'unknown' : 'no_location');
    }

    function freshnessLabel(device) {
      const stateName = freshnessState(device);
      const age = device?.freshness?.age_seconds;
      const ageText = Number.isFinite(age) ? ` · ${Math.round(age)}s old` : '';
      if (stateName === 'active') return `Active${ageText}`;
      if (stateName === 'stale') return `Stale location${ageText}`;
      if (stateName === 'offline') return `Offline${ageText}`;
      if (stateName === 'no_location') return 'No location';
      return `Unknown${ageText}`;
    }

    function freshnessClass(device) {
      const stateName = freshnessState(device);
      if (stateName === 'active') return 'ok';
      if (stateName === 'stale') return 'warn';
      return 'err';
    }

    function groundY(x, yNorth) {
      if (!grid || !VEILTerrain?.sampleTerrainHeightAtLocal) return 2;
      return VEILTerrain.sampleTerrainHeightAtLocal(grid, x, yNorth) + 2.8;
    }

    function localFromPosition(position) {
      if (!position || !Number.isFinite(position.lat) || !Number.isFinite(position.lon)) return null;
      const projected = VEILGeoref.geographicToProjected(position.lon, position.lat);
      const ox = Array.isArray(scene.origin_utm) ? scene.origin_utm[0] : 0;
      const oy = Array.isArray(scene.origin_utm) ? scene.origin_utm[1] : 0;
      return { x: projected[0] - ox, yNorth: projected[1] - oy };
    }

    function worldFromLocal(local) {
      if (!local) return null;
      const { x, yNorth } = local;
      return new THREE.Vector3(x, groundY(x, yNorth), -yNorth);
    }

    function worldFromPosition(position) {
      return worldFromLocal(localFromPosition(position));
    }

    function labelSprite(text, color) {
      const pad = 8;
      const canvas = document.createElement('canvas');
      const ctx = canvas.getContext('2d');
      const font = '600 24px system-ui, sans-serif';
      ctx.font = font;
      canvas.width = Math.max(88, Math.ceil(ctx.measureText(text).width) + pad * 2);
      canvas.height = 38;
      ctx.font = font;
      ctx.fillStyle = 'rgba(7, 16, 22, 0.78)';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = color;
      ctx.textBaseline = 'middle';
      ctx.fillText(text, pad, canvas.height / 2);
      const tex = new THREE.CanvasTexture(canvas);
      const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
        map: tex, depthTest: false, transparent: true,
      }));
      const h = 6;
      sprite.scale.set((h * canvas.width) / canvas.height, h, 1);
      sprite.renderOrder = 1000;
      return sprite;
    }

    function disposeMarker(marker) {
      group.remove(marker.root);
      marker.mesh.geometry.dispose();
      marker.mesh.material.dispose();
      marker.label.material.map.dispose();
      marker.label.material.dispose();
    }

    function disposeReplayTracks() {
      state.replayTracks.forEach((line) => {
        trackGroup.remove(line);
        line.geometry.dispose();
        line.material.dispose();
      });
      state.replayTracks.clear();
    }

    function disposeReplayTrack(deviceId) {
      const track = state.replayTracks.get(deviceId);
      if (!track) return false;
      trackGroup.remove(track);
      track.geometry.dispose();
      track.material.dispose();
      state.replayTracks.delete(deviceId);
      return true;
    }

    function removeLocalDevice(deviceId) {
      const marker = state.markers.get(deviceId);
      if (marker) disposeMarker(marker);
      state.markers.delete(deviceId);
      disposeReplayTrack(deviceId);
      state.devices.delete(deviceId);
    }

    function refreshDeviceFreshness(device, nowMs = Date.now()) {
      if (!device) return null;
      device.freshness = computeLiveDeviceFreshness(device, nowMs);
      return device.freshness;
    }

    function applyMarkerFreshness(device, replay = false) {
      const marker = state.markers.get(device?.device_id);
      if (!marker) return;
      const fresh = replay || freshnessState(device) === 'active';
      marker.mesh.material.opacity = fresh ? 1 : 0.38;
      marker.root.scale.setScalar(fresh ? 1 : 0.82);
    }

    function replayVisible() {
      return !!els.replayBar && !els.replayBar.hidden;
    }

    function updateFreshnessRows() {
      if (!els.gateways) return;
      els.gateways.querySelectorAll('.live-device-row').forEach((row) => {
        const device = state.devices.get(row.dataset.liveDeviceId);
        const freshness = row.querySelector('.live-device-freshness');
        if (!device || !freshness) return;
        freshness.textContent = freshnessLabel(device);
        freshness.className = `live-device-freshness ${freshnessClass(device)}`;
      });
    }

    function updateFreshnessMetadataCards() {
      document.querySelectorAll('.live-info-card[data-live-device-id]').forEach((card) => {
        const device = state.devices.get(card.dataset.liveDeviceId);
        if (!device) return;
        const openDetails = new Set([...card.querySelectorAll('details[open]')]
          .map((details) => details.className || details.querySelector('summary')?.textContent?.trim())
          .filter(Boolean));
        const wrapper = document.createElement('div');
        wrapper.innerHTML = metadataHtml(device);
        const next = wrapper.firstElementChild;
        if (!next) return;
        next.querySelectorAll('details').forEach((details) => {
          const key = details.className || details.querySelector('summary')?.textContent?.trim();
          if (key && openDetails.has(key)) details.open = true;
        });
        card.replaceWith(next);
      });
    }

    function refreshLiveFreshness(nowMs = Date.now()) {
      state.devices.forEach((device) => {
        refreshDeviceFreshness(device, nowMs);
        if (!replayVisible()) applyMarkerFreshness(device, false);
      });
      updateFreshnessRows();
      updateFreshnessMetadataCards();
      if (state.liveStatusManaged) setLiveDeviceSummaryStatus();
    }

    function startFreshnessTimer() {
      if (state.freshnessTimer) return;
      state.freshnessTimer = setInterval(() => {
        refreshLiveFreshness();
        ensureLiveStream();
      }, LIVE_FRESHNESS_TICK_MS);
    }

    function stopFreshnessTimer() {
      return clearTimerState(state, 'freshnessTimer');
    }

    function setReplayPov(enabled) {
      state.replayPov = !!enabled;
      state.replayPovDeviceId = enabled ? state.replayPovDeviceId : null;
      els.replayPov?.classList.toggle('active', state.replayPov);
      if (els.replayPov) {
        els.replayPov.textContent = state.replayPov ? 'Exit POV' : 'Tracker POV';
      }
      if (!state.replayPov) {
        window.__twin?.pov?.exitReplayFollow?.();
      }
    }

    function setReplayControlsEnabled(enabled) {
      const disabled = !enabled;
      [els.replayPlay, els.replayProgress, els.replayExport, els.replaySnapshot, els.replayPov]
        .forEach((control) => {
          if (control) control.disabled = disabled;
        });
    }

    function clearReplayData() {
      state.replayEvents = [];
      state.replaySamplesByDevice = new Map();
      state.replayTrackPoints = new Map();
      state.replayLabelsByDevice = new Map();
      state.replayIndex = 0;
      state.replayFloatIndex = 0;
      state.replayClockMs = null;
      state.replayPovDeviceId = null;
      disposeReplayTracks();
      if (els.replayProgress) {
        els.replayProgress.max = '0';
        els.replayProgress.value = '0';
      }
    }

    function markerFor(device, replay) {
      let marker = state.markers.get(device.device_id);
      const label = device.label || device.device_id;
      const color = replay ? '#f2c14e' : cleanColor(device.color);
      if (marker && (marker.labelText !== label || marker.labelColor !== color)) {
        disposeMarker(marker);
        marker = null;
      }
      if (!marker) {
        const root = new THREE.Group();
        const mesh = new THREE.Mesh(
          new THREE.SphereGeometry(3.0, 18, 12),
          new THREE.MeshBasicMaterial({ color: replay ? REPLAY : BLUE, transparent: true })
        );
        mesh.userData.liveDeviceId = device.device_id;
        mesh.renderOrder = 999;
        const labelObj = labelSprite(label, color);
        labelObj.position.y = 10;
        root.add(mesh);
        root.add(labelObj);
        group.add(root);
        marker = { root, mesh, label: labelObj, labelText: label, labelColor: color };
        state.markers.set(device.device_id, marker);
      }
      marker.mesh.material.color.setHex(replay ? REPLAY : colorInt(color));
      applyMarkerFreshness(device, replay);
      return marker;
    }

    function applyEvent(event, replay) {
      if (!event?.device_id) return;
      const prev = state.devices.get(event.device_id) || {};
      const device = {
        ...prev,
        ...event,
        label: event.label && event.label !== event.device_id
          ? event.label
          : (prev.label || event.device_id),
        color: event.color || prev.color || DEFAULT_DEVICE_COLOR,
        visible: event.visible !== false && prev.visible !== false,
      };
      if (!replay) refreshDeviceFreshness(device);
      state.devices.set(device.device_id, device);
      // While replay is showing, a live SSE event keeps the device record current
      // but must NOT touch the shared device_id-keyed marker — the replay renderer
      // owns it right now, and applying the live (blue) position would overwrite
      // the orange replay marker and make it flicker/jump. The marker is restored
      // to live on replay close (re-hydrate) and by the next event afterward.
      if (!replay && replayVisible()) return device;
      const pos = event._worldPosition || worldFromPosition(device.position);
      if (!pos) return device;
      const marker = markerFor(device, replay);
      marker.root.position.copy(pos);
      marker.root.visible = device.visible !== false;
      return device;
    }

    function setLiveDeviceSummaryStatus() {
      const count = state.devices.size;
      const active = [...state.devices.values()].filter((d) => freshnessState(d) === 'active').length;
      const suffix = state.discoveryMode ? ' Discovery mode is showing observed nodes.' : '';
      setStatus(count ? `${active}/${count} live device${count === 1 ? '' : 's'} active.${suffix}` : 'Waiting for live telemetry.');
      state.liveStatusManaged = true;
    }

    function hydrate(snapshot) {
      state.gateways = snapshot.gateways || [];
      const incoming = new Set();
      const replayDeviceIds = new Set(state.replaySamplesByDevice.keys());
      (snapshot.devices || []).forEach((event) => {
        incoming.add(event.device_id);
        if (!shouldApplyLiveHydrationDevice(event.device_id, {
          replayMode: state.replayMode,
          replayDeviceIds,
        })) return;
        applyEvent(event, false);
      });
      [...state.devices.keys()].forEach((deviceId) => {
        if (!shouldPruneLiveHydrationDevice(deviceId, incoming, {
          replayMode: state.replayMode,
          replayDeviceIds,
        })) return;
        removeLocalDevice(deviceId);
      });
      renderMenus();
      setLiveDeviceSummaryStatus();
    }

    async function setDiscoveryMode(enabled) {
      const next = !!enabled;
      if (state.discoveryMode === next) return;
      state.discoveryMode = next;
      state.devices.clear();
      state.markers.forEach((marker) => disposeMarker(marker));
      state.markers.clear();
      renderMenus();
      setStatus(next ? 'Discovery mode enabled; showing observed Meshtastic nodes.' : 'Configured mode enabled; only named trackers are shown.', 'ok');
      await connectStream();
    }

    function metadataHtml(device) {
      return buildLiveMetadataHtml(device, freshnessLabel);
    }

    function showMetadata(device) {
      const host = document.getElementById('identify-results');
      if (host) {
        host.innerHTML = metadataHtml(device);
        try {
          document.dispatchEvent(new CustomEvent('veil:inspect', { detail: { source: 'live' } }));
        } catch (_err) {
          // The metadata is already rendered; revealing the inspector is chrome-only.
        }
      }
    }

    function selectNear(x, yNorth) {
      let best = null;
      state.devices.forEach((device) => {
        if (device.visible === false || !device.position) return;
        const projected = VEILGeoref.geographicToProjected(device.position.lon, device.position.lat);
        const ox = Array.isArray(scene.origin_utm) ? scene.origin_utm[0] : 0;
        const oy = Array.isArray(scene.origin_utm) ? scene.origin_utm[1] : 0;
        const dx = (projected[0] - ox) - x;
        const dy = (projected[1] - oy) - yNorth;
        const dist = Math.hypot(dx, dy);
        if (dist < 14 && (!best || dist < best.dist)) best = { device, dist };
      });
      if (best) showMetadata(best.device);
      return !!best;
    }

    function pickAtScreen(clientX, clientY) {
      const canvas = viewer.renderer?.domElement;
      if (!canvas) return false;
      const rect = canvas.getBoundingClientRect();
      const ndc = new THREE.Vector2(
        ((clientX - rect.left) / rect.width) * 2 - 1,
        -((clientY - rect.top) / rect.height) * 2 + 1
      );
      const raycaster = new THREE.Raycaster();
      raycaster.setFromCamera(ndc, viewer.camera);
      const meshes = [...state.markers.values()]
        .filter((m) => m.root.visible)
        .map((m) => m.mesh);
      const hit = raycaster.intersectObjects(meshes, false)[0];
      const deviceId = hit?.object?.userData?.liveDeviceId;
      const device = deviceId ? state.devices.get(deviceId) : null;
      if (!device) return false;
      showMetadata(device);
      return true;
    }

    async function updateDevice(deviceId, patch) {
      const res = await liveFetch('/api/live/devices', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_id: deviceId, ...patch }),
      });
      if (!res.ok) throw new Error(`device update failed (${res.status})`);
      const current = state.devices.get(deviceId);
      if (current) {
        state.devices.set(deviceId, { ...current, ...patch });
        applyEvent(state.devices.get(deviceId), false);
      }
    }

    async function removeDevice(deviceId) {
      const res = await liveFetch('/api/live/devices/remove', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_id: deviceId }),
      });
      if (!res.ok) throw new Error(`device removal failed (${res.status})`);
      removeLocalDevice(deviceId);
      renderMenus();
    }

    async function addDiscoveredDevice(device) {
      const fallback = device.label && device.label !== device.device_id ? device.label : '';
      const label = normalizePromptLabel(prompt('Display name in VEIL', fallback || device.device_id));
      await updateDevice(device.device_id, {
        configured: true,
        visible: true,
        label: label || device.label || device.device_id,
        gateway_id: deviceGatewayId(device) || undefined,
      });
      const current = state.devices.get(device.device_id);
      if (current) {
        current.configured = true;
        current.visible = true;
        current.label = label || current.label || device.device_id;
        applyEvent(current, false);
      }
      renderMenus();
      setStatus(`${label || device.label || device.device_id} added to configured trackers.`, 'ok');
    }

    async function requestDeviceFix(device) {
      const gatewayId = deviceGatewayId(device);
      if (!gatewayId) throw new Error('device has no gateway');
      const res = await liveFetch('/api/live/devices/command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          gateway_id: gatewayId,
          device_id: device.device_id,
          command: 'request_position',
        }),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(payload.error || `request failed (${res.status})`);
      setStatus(`Requested position fix from ${device.label || device.device_id}.`, 'ok');
    }

    async function removeGateway(gatewayId) {
      const res = await liveFetch('/api/live/gateways/remove', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ gateway_id: gatewayId }),
      });
      if (!res.ok) throw new Error(`gateway removal failed (${res.status})`);
      gatewayLinkedDeviceIds(state.devices, gatewayId, deviceGatewayId)
        .forEach((deviceId) => removeLocalDevice(deviceId));
      state.gateways = state.gateways.filter((g) => g.id !== gatewayId);
      renderMenus();
    }

    async function restartGateway(gatewayId) {
      const res = await liveFetch('/api/live/gateways/restart', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ gateway_id: gatewayId }),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok || payload.ok === false) throw new Error(payload.error || `gateway restart failed (${res.status})`);
      if (payload.gateway) {
        const idx = state.gateways.findIndex((g) => g.id === payload.gateway.id);
        if (idx >= 0) state.gateways[idx] = payload.gateway;
        else state.gateways.push(payload.gateway);
      }
      renderMenus();
      setStatus(`Restarted ${payload.gateway?.name || gatewayId}.`, 'ok');
    }

    function deviceRow(device) {
      const row = document.createElement('div');
      row.className = 'live-device-row';
      row.dataset.liveDeviceId = device.device_id;
      if (device.configured === false) row.classList.add('discovered');
      const checked = device.visible !== false;
      const color = cleanColor(device.color);
      if (device.configured === false) {
        row.innerHTML =
          `<div class="live-device-head">
            <span class="live-device-color" style="background:${escapeHtml(color)}" aria-hidden="true"></span>
            <span class="toggle-label">${escapeHtml(device.label || device.device_id)}</span>
          </div>
          <div class="live-device-actions">
            <button type="button" class="live-device-add" aria-label="Add ${escapeHtml(device.label || device.device_id)} to configured trackers">Add</button>
            <button type="button" class="live-device-meta" aria-label="Show telemetry details for ${escapeHtml(device.label || device.device_id)}">Info</button>
          </div>
          <p class="live-device-freshness ${freshnessClass(device)}">${escapeHtml(freshnessLabel(device))}</p>`;
        const addButton = row.querySelector('.live-device-add');
        addButton.addEventListener('click', () => {
          actionGuard.run(
            `device-add:${device.device_id}`,
            () => addDiscoveredDevice(device),
            addButton,
          ).catch((err) => setStatus(err.message, 'err'));
        });
        row.querySelector('.live-device-meta').addEventListener('click', () => showMetadata(device));
        return row;
      }
      row.innerHTML =
        `<div class="live-device-head">
          <input type="color" class="live-device-color" value="${escapeHtml(color)}" title="Set marker color" aria-label="Set marker color for ${escapeHtml(device.label || device.device_id)}" />
          <label class="toggle-row live-device-toggle">
            <input type="checkbox" ${checked ? 'checked' : ''} />
            <span class="toggle-label">${escapeHtml(device.label || device.device_id)}</span>
          </label>
        </div>
        <div class="live-device-actions">
          <button type="button" class="live-device-fix" title="Request a Meshtastic position response" aria-label="Request position fix for ${escapeHtml(device.label || device.device_id)}">Fix</button>
          <button type="button" class="live-device-meta" aria-label="Show telemetry details for ${escapeHtml(device.label || device.device_id)}">Info</button>
          <button type="button" class="live-device-rename" aria-label="Rename ${escapeHtml(device.label || device.device_id)}">Name</button>
          <button type="button" class="live-device-remove" aria-label="Remove ${escapeHtml(device.label || device.device_id)}">Remove</button>
        </div>
        <p class="live-device-freshness ${freshnessClass(device)}">${escapeHtml(freshnessLabel(device))}</p>`;
      row.querySelector('.live-device-toggle input').addEventListener('change', async (e) => {
        await updateDevice(device.device_id, { visible: e.target.checked });
        renderMenus();
      });
      row.querySelector('.live-device-color').addEventListener('input', (e) => {
        const current = state.devices.get(device.device_id);
        if (!current) return;
        current.color = cleanColor(e.target.value);
        applyEvent(current, false);
      });
      row.querySelector('.live-device-color').addEventListener('change', async (e) => {
        await updateDevice(device.device_id, { color: cleanColor(e.target.value) });
        renderMenus();
      });
      const fixButton = row.querySelector('.live-device-fix');
      fixButton.addEventListener('click', () => {
        actionGuard.run(
          `device-fix:${device.device_id}`,
          () => requestDeviceFix(device),
          fixButton,
        ).catch((err) => setStatus(err.message, 'err'));
      });
      row.querySelector('.live-device-meta').addEventListener('click', () => showMetadata(device));
      row.querySelector('.live-device-rename').addEventListener('click', async () => {
        const label = normalizePromptLabel(prompt('Display name in VEIL', device.label || device.device_id));
        if (!label) return;
        await updateDevice(device.device_id, { label });
        renderMenus();
      });
      const removeButton = row.querySelector('.live-device-remove');
      removeButton.addEventListener('click', () => {
        const key = `remove-device:${device.device_id}`;
        if (actionGuard.isPending(key)) return;
        if (!confirmDestructiveAction('remove-device', device.label || device.device_id)) return;
        actionGuard.run(
          key,
          () => removeDevice(device.device_id),
          removeButton,
        ).catch((err) => setStatus(err.message, 'err'));
      });
      return row;
    }

    function renderMenus() {
      if (!els.gateways) return;
      els.gateways.replaceChildren();
      const mode = document.createElement('div');
      mode.className = 'live-mode-toggle';
      mode.innerHTML =
        `<button type="button" class="live-mode-configured${state.discoveryMode ? '' : ' active'}">Configured</button>
         <button type="button" class="live-mode-discovery${state.discoveryMode ? ' active' : ''}">Discovery</button>`;
      mode.querySelector('.live-mode-configured').addEventListener('click', () => setDiscoveryMode(false));
      mode.querySelector('.live-mode-discovery').addEventListener('click', () => setDiscoveryMode(true));
      els.gateways.appendChild(mode);
      const devices = [...state.devices.values()].sort((a, b) =>
        String(a.label || a.device_id).localeCompare(String(b.label || b.device_id)));
      const gatewayIds = new Set();
      state.gateways.forEach((gateway) => {
        gatewayIds.add(gateway.id);
        const details = document.createElement('details');
        details.className = 'menu-section live-gateway';
        details.open = true;
        details.innerHTML =
          `<summary class="collapsible-header"><span class="panel-label">Registered Gateway Device: ${escapeHtml(gateway.name || gateway.id)}</span></summary>
           <div class="collapsible-body"></div>`;
        const body = details.querySelector('.collapsible-body');
        const actions = document.createElement('div');
        actions.className = 'live-gateway-actions';
        const restart = document.createElement('button');
        restart.type = 'button';
        restart.textContent = 'Restart';
        restart.setAttribute('aria-label', `Restart ${gateway.name || gateway.id}`);
        restart.addEventListener('click', () => {
          actionGuard.run(
            `restart-gateway:${gateway.id}`,
            () => restartGateway(gateway.id),
            restart,
          ).catch((err) => setStatus(err.message, 'err'));
        });
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.textContent = 'Remove';
        remove.setAttribute('aria-label', `Remove ${gateway.name || gateway.id}`);
        remove.addEventListener('click', () => {
          const key = `remove-gateway:${gateway.id}`;
          if (actionGuard.isPending(key)) return;
          if (!confirmDestructiveAction('remove-gateway', gateway.name || gateway.id)) return;
          actionGuard.run(
            key,
            () => removeGateway(gateway.id),
            remove,
          ).catch((err) => setStatus(err.message, 'err'));
        });
        actions.appendChild(restart);
        actions.appendChild(remove);
        body.appendChild(actions);
        const meta = document.createElement('p');
        meta.className = 'readout-hint live-gateway-meta';
        const bridge = gateway.bridge || {};
        const bridgeText = bridge.state && bridge.state !== 'stopped'
          ? ` · bridge ${bridge.state}${bridge.pid ? ` pid ${bridge.pid}` : ''}`
          : ' · bridge stopped';
        const retryText = bridge.next_retry_at ? ` · retry ${new Date(bridge.next_retry_at).toLocaleTimeString()}` : '';
        meta.textContent = `${gateway.protocol || 'meshtastic'} via ${gateway.transport}${gateway.address ? ` (${gateway.address})` : ''}${bridgeText}${retryText}`;
        body.appendChild(meta);
        if (bridge.error) {
          const err = document.createElement('p');
          err.className = 'live-status err';
          err.textContent = bridge.error;
          body.appendChild(err);
        } else if (bridge.last_line) {
          const line = document.createElement('p');
          line.className = 'live-status';
          line.textContent = bridge.last_line;
          body.appendChild(line);
        }
        const linked = devices.filter((d) => deviceGatewayId(d) === gateway.id);
        if (!linked.length) {
          const empty = document.createElement('p');
          empty.className = 'readout-hint';
          empty.textContent = state.discoveryMode
            ? 'No observed nodes received through this gateway yet.'
            : 'No configured tracker packets received through this gateway yet.';
          body.appendChild(empty);
        }
        linked.forEach((device) => body.appendChild(deviceRow(device)));
        els.gateways.appendChild(details);
      });
      const unassigned = devices.filter((d) => {
        const gid = deviceGatewayId(d);
        return !gid || !gatewayIds.has(gid);
      });
      if (unassigned.length) {
        const details = document.createElement('details');
        details.className = 'menu-section live-gateway';
        details.open = true;
        details.innerHTML =
          '<summary class="collapsible-header"><span class="panel-label">Live devices</span></summary><div class="collapsible-body"></div>';
        const body = details.querySelector('.collapsible-body');
        unassigned.forEach((device) => body.appendChild(deviceRow(device)));
        els.gateways.appendChild(details);
      }
    }

    function optionLabel(device) {
      const parts = [device.label || device.name || device.address];
      if (device.address && parts[0] !== device.address) parts.push(device.address);
      if (device.manufacturer) parts.push(device.manufacturer);
      if (Number.isFinite(device.rssi)) parts.push(`${device.rssi} dBm`);
      if (device.candidate) parts.push('candidate');
      return parts.filter(Boolean).join(' · ');
    }

    function renderDiscovery(devices) {
      state.discovered = devices || [];
      if (!els.deviceSelect) return;
      els.deviceSelect.replaceChildren();
      els.deviceSelect.hidden = !state.discovered.length;
      state.discovered.forEach((device) => {
        const opt = document.createElement('option');
        opt.value = device.address || device.id;
        opt.textContent = optionLabel(device);
        els.deviceSelect.appendChild(opt);
      });
      const preferred = state.discovered.find((d) => d.candidate) || state.discovered[0];
      if (preferred) {
        els.deviceSelect.value = preferred.address || preferred.id;
        els.address.value = preferred.address || preferred.id || '';
      }
    }

    function updateTransportUi() {
      const transport = els.transport?.value || 'bluetooth';
      state.discovered = [];
      if (els.deviceSelect) {
        els.deviceSelect.hidden = true;
        els.deviceSelect.replaceChildren();
      }
      if (transport === 'bluetooth') {
        if (els.address) els.address.placeholder = 'Scan and select a Bluetooth gateway, or paste address';
        if (els.discover) {
          els.discover.hidden = false;
          els.discover.textContent = 'Scan';
        }
        setDiscoverStatus('Bluetooth scan will list nearby peripherals.');
      } else if (transport === 'serial') {
        if (els.address) els.address.placeholder = 'Scan and select a serial port, or enter /dev/ttyUSB0';
        if (els.discover) {
          els.discover.hidden = false;
          els.discover.textContent = 'Refresh';
        }
        setDiscoverStatus('Serial refresh will list connected ports.');
      } else {
        if (els.address) els.address.placeholder = 'Meshtastic TCP host or URL';
        if (els.discover) els.discover.hidden = true;
        setDiscoverStatus('');
      }
    }

    async function discoverDevices() {
      const transport = els.transport.value;
      if (!['bluetooth', 'serial'].includes(transport)) return [];
      setDiscoverStatus(transport === 'bluetooth' ? 'Scanning Bluetooth...' : 'Refreshing serial ports...');
      const res = await liveFetch(`/api/live/discover?transport=${encodeURIComponent(transport)}`, { cache: 'no-store' });
      const body = await res.json().catch(() => ({}));
      if (!res.ok || body.ok === false) throw new Error(body.error || `discovery failed (${res.status})`);
      renderDiscovery(body.devices || []);
      const count = body.devices?.length || 0;
      setDiscoverStatus(count ? `${count} ${transport} device${count === 1 ? '' : 's'} found.` : `No ${transport} devices found.`, count ? 'ok' : 'warn');
      return body.devices || [];
    }

    async function ensureGatewayAddress() {
      const transport = els.transport.value;
      if (!['bluetooth', 'serial'].includes(transport)) return true;
      if (els.address.value.trim()) return true;
      const devices = state.discovered.length ? state.discovered : await discoverDevices();
      if (devices.length === 1) {
        els.address.value = devices[0].address || devices[0].id || '';
        return !!els.address.value.trim();
      }
      if (devices.length > 1) {
        renderDiscovery(devices);
        setDiscoverStatus(`Select one ${transport} device before connecting.`, 'warn');
        return false;
      }
      setDiscoverStatus(`No ${transport} devices found to connect.`, 'err');
      return false;
    }

    async function registerGateway(e) {
      e.preventDefault();
      if (!await ensureGatewayAddress()) return;
      const name = els.gatewayName.value.trim() || 'Meshtastic gateway';
      const body = {
        name,
        protocol: 'meshtastic',
        transport: els.transport.value,
        address: els.address.value.trim(),
        connect: true,
      };
      setStatus('Registering and connecting gateway...');
      const res = await liveFetch('/api/live/gateways', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(payload.error || `gateway registration failed (${res.status})`);
      if (payload.gateway) {
        const idx = state.gateways.findIndex((g) => g.id === payload.gateway.id);
        if (idx >= 0) state.gateways[idx] = payload.gateway;
        else state.gateways.push(payload.gateway);
      } else {
        state.gateways = payload.registry?.gateways || state.gateways;
      }
      renderMenus();
      setStatus(gatewaySuccessMessage(payload, body, state.gateways), 'ok');
    }

    function updateSseErrorStatus(now = Date.now()) {
      state.liveStreamErrorCount += 1;
      const lastOkMs = Number.isFinite(state.liveStreamLastOkMs)
        ? state.liveStreamLastOkMs
        : (Number.isFinite(state.liveStreamOpenedAtMs) ? state.liveStreamOpenedAtMs : now);
      const status = sseErrorStatus({
        errorCount: state.liveStreamErrorCount,
        disconnectedMs: Math.max(0, now - lastOkMs),
      });
      setStatus(status.text, status.tone);
    }

    function handleSseJsonFrame(eventName, data, handler) {
      const parsed = safeParseSseJson(data, eventName);
      if (!parsed.ok) {
        setStatus(parsed.message, 'err');
        return;
      }
      recordSseSuccess(state);
      handler(parsed.value);
    }

    async function connectStream() {
      closeActiveEventSource(state);
      state.liveStreamConnecting = true;
      try {
        const first = await liveFetch(liveTelemetryUrl('/api/live/latest'), { cache: 'no-store' });
        if (first.ok) hydrate(await first.json());
      } catch (_err) { /* SSE below may still connect */ }
      try {
        const stream = new EventSource(liveUrl(liveTelemetryUrl('/api/live/stream')));
        state.liveStream = stream;
        state.liveStreamErrorCount = 0;
        state.liveStreamOpenedAtMs = Date.now();
        state.liveStreamLastOkMs = state.liveStreamOpenedAtMs;
        stream.onopen = () => recordSseSuccess(state);
        stream.addEventListener('snapshot', (e) => handleSseJsonFrame('snapshot', e.data, hydrate));
        stream.addEventListener('live', (e) => {
          handleSseJsonFrame('live', e.data, (event) => {
            const device = applyEvent(event, false) || event;
            renderMenus();
            setStatus(`${device.label || device.device_id}: ${freshnessLabel(device)}.`, freshnessClass(device));
          });
        });
        stream.onerror = () => updateSseErrorStatus();
      } catch (_err) {
        setStatus('This browser cannot open the live telemetry stream.', 'err');
      } finally {
        state.liveStreamConnecting = false;
      }
    }

    // The browser's native EventSource only auto-reconnects when it *detects*
    // a failed connection. A half-open socket (laptop sleep, a frozen
    // background tab) leaves readyState OPEN with no error, and a non-200 on a
    // reconnect attempt fails the stream permanently (readyState CLOSED) — in
    // both cases native recovery never fires and the view stays frozen while
    // the server keeps streaming. This watchdog rebuilds the stream from those
    // states so a stale tab heals without a manual refresh.
    function ensureLiveStream({ force = false } = {}) {
      if (typeof EventSource === 'undefined') return;
      if (state.liveStreamConnecting) return;
      const readyState = state.liveStream ? state.liveStream.readyState : EventSource.CLOSED;
      // OPEN looks healthy and CONNECTING is the browser's own retry already in
      // flight — only CLOSED is unambiguously dead. `force` overrides for the
      // case readyState cannot reveal: a long-backgrounded tab whose OPEN
      // socket may secretly be half-open.
      if (!force && readyState !== EventSource.CLOSED) return;
      const now = Date.now();
      if (!force && Number.isFinite(state.liveStreamReconnectAtMs)
        && now - state.liveStreamReconnectAtMs < LIVE_RECONNECT_MIN_INTERVAL_MS) return;
      state.liveStreamReconnectAtMs = now;
      connectStream();
    }

    async function loadReplayDays() {
      const res = await liveFetch('/api/live/days', { cache: 'no-store' });
      const body = await parseReplayJsonResponse(res, 'replay days');
      stopReplayPlayback(state, els.replayPlay);
      setReplayControlsEnabled(false);
      els.replayDay.replaceChildren();
      (body.days || []).forEach((day) => {
        const opt = document.createElement('option');
        opt.value = day;
        opt.textContent = day;
        els.replayDay.appendChild(opt);
      });
      if (body.days?.length) {
        els.replayDay.value = body.days[body.days.length - 1];
        await loadReplayEvents();
      } else {
        clearReplayData();
        els.replayStatus.textContent = 'No telemetry days recorded yet.';
      }
    }

    let replayLoadToken = 0;
    async function loadReplayEvents() {
      const day = els.replayDay.value;
      if (!day) return;
      const token = ++replayLoadToken;
      stopReplayPlayback(state, els.replayPlay);
      setReplayControlsEnabled(false);
      clearReplayData();
      state.replayMode = replayVisible();
      const res = await liveFetch(`/api/live/history?date=${encodeURIComponent(day)}`, { cache: 'no-store' });
      const body = await parseReplayJsonResponse(res, 'replay events');
      // A newer day load started while we awaited — discard this stale result so
      // fast day-switching can't leave one day's events under another day's label.
      if (token !== replayLoadToken) return;
      state.replayEvents = sortReplayEvents(body.events || []);
      state.replaySamplesByDevice = buildReplaySamples(state.replayEvents);
      state.replayTrackPoints = buildReplayTrackPoints(state.replaySamplesByDevice);
      state.replayLabelsByDevice = buildReplayLabels(state.replayEvents);
      state.replayIndex = 0;
      state.replayFloatIndex = 0;
      state.replayClockMs = replayTimeForFloatIndex(0);
      state.replayPovDeviceId = null;
      disposeReplayTracks();
      els.replayProgress.max = String(Math.max(0, state.replayEvents.length - 1));
      els.replayProgress.value = '0';
      els.replayStatus.textContent = state.replayEvents.length
        ? `${state.replayEvents.length} events loaded for ${day}.`
        : `No events for ${day}.`;
      if (!state.replayEvents.length) setReplayPov(false);
      setReplayControlsEnabled(state.replayEvents.length > 0);
      renderReplayAt(0);
    }

    function eventTimeMs(event) {
      const ms = new Date(event?.observed_at || event?.received_at || '').valueOf();
      return Number.isFinite(ms) ? ms : null;
    }

    function sortReplayEvents(events) {
      return [...events].sort((a, b) => {
        const ams = eventTimeMs(a);
        const bms = eventTimeMs(b);
        if (ams === null && bms === null) return 0;
        if (ams === null) return 1;
        if (bms === null) return -1;
        return ams - bms;
      });
    }

    function sampleFromEvent(event) {
      const local = localFromPosition(event.position);
      const ms = eventTimeMs(event);
      if (!local || ms === null) return null;
      return {
        event,
        ms,
        local,
      };
    }

    function buildReplaySamples(events) {
      const samplesByDevice = new Map();
      events.forEach((event) => {
        if (event.kind !== 'position' || !event.position) return;
        const sample = sampleFromEvent(event);
        if (!sample) return;
        if (!samplesByDevice.has(event.device_id)) samplesByDevice.set(event.device_id, []);
        samplesByDevice.get(event.device_id).push(sample);
      });
      samplesByDevice.forEach((samples) => samples.sort((a, b) => a.ms - b.ms));
      return samplesByDevice;
    }

    function buildReplayTrackPoints(samplesByDevice) {
      const trackPoints = new Map();
      samplesByDevice.forEach((samples, deviceId) => {
        const points = [];
        if (samples.length === 1) {
          const point = worldFromLocal(samples[0].local);
          if (point) points.push({ ms: samples[0].ms, point });
        }
        for (let i = 0; i < samples.length - 1; i += 1) {
          const a = samples[i];
          const b = samples[i + 1];
          const dist = Math.hypot(b.local.x - a.local.x, b.local.yNorth - a.local.yNorth);
          const steps = Math.max(2, Math.min(24, Math.ceil(dist / 5)));
          for (let step = i === 0 ? 0 : 1; step <= steps; step += 1) {
            const t = a.ms + ((b.ms - a.ms) * step) / steps;
            const point = worldFromLocal(interpolateLocal(a, b, t));
            if (point) points.push({ ms: t, point });
          }
        }
        trackPoints.set(deviceId, points);
      });
      return trackPoints;
    }

    function buildReplayLabels(events) {
      const labelsByDevice = new Map();
      events.forEach((event) => {
        const ms = eventTimeMs(event);
        if (ms === null || !event?.device_id || !event.label || event.label === event.device_id) return;
        if (!labelsByDevice.has(event.device_id)) labelsByDevice.set(event.device_id, []);
        labelsByDevice.get(event.device_id).push({ ms, label: event.label });
      });
      labelsByDevice.forEach((labels) => labels.sort((a, b) => a.ms - b.ms));
      return labelsByDevice;
    }

    function replayTimeForFloatIndex(floatIndex) {
      if (!state.replayEvents.length) return null;
      const clamped = Math.max(0, Math.min(floatIndex, state.replayEvents.length - 1));
      const lo = Math.floor(clamped);
      const hi = Math.min(state.replayEvents.length - 1, Math.ceil(clamped));
      const loMs = eventTimeMs(state.replayEvents[lo]);
      const hiMs = eventTimeMs(state.replayEvents[hi]);
      if (loMs === null) return hiMs;
      if (hiMs === null || hi === lo) return loMs;
      const u = clamped - lo;
      return loMs + (hiMs - loMs) * u;
    }

    function replaySpeedMultiplier() {
      const speed = Number(els.replaySpeed?.value || 1);
      return Number.isFinite(speed) && speed > 0 ? speed : 1;
    }

    function interpolateLocal(a, b, targetMs) {
      const span = Math.max(1, b.ms - a.ms);
      const u = Math.max(0, Math.min(1, (targetMs - a.ms) / span));
      return {
        x: a.local.x + (b.local.x - a.local.x) * u,
        yNorth: a.local.yNorth + (b.local.yNorth - a.local.yNorth) * u,
      };
    }

    function sampleDistance(a, b) {
      if (!a?.local || !b?.local) return 0;
      return Math.hypot(b.local.x - a.local.x, b.local.yNorth - a.local.yNorth);
    }

    function canInterpolateSamples(a, b) {
      const dt = b.ms - a.ms;
      if (!Number.isFinite(dt) || dt <= 0 || dt > REPLAY_MAX_INTERPOLATE_GAP_MS) return false;
      const speed = sampleDistance(a, b) / Math.max(0.001, dt / 1000);
      return speed <= REPLAY_MAX_INTERPOLATE_SPEED_MPS;
    }

    function replayIndexForTime(targetMs) {
      if (!state.replayEvents.length || targetMs === null) return 0;
      return Math.max(0, sortedTimeIndexFor(state.replayEvents, targetMs, eventTimeMs));
    }

    function replayLabelForDeviceAt(labelsByDevice, deviceId, targetMs) {
      const labels = labelsByDevice.get(deviceId) || [];
      const index = sortedTimeIndexFor(labels, targetMs, (entry) => entry.ms);
      return index >= 0 ? labels[index].label : null;
    }

    function headingFromSamples(previous, next, current) {
      const before = previous?.local;
      const here = current?.local;
      const after = next?.local;
      const from = before || here;
      const to = after || here;
      const dx = to.x - from.x;
      const dn = to.yNorth - from.yNorth;
      if (Math.hypot(dx, dn) >= 0.5) return (Math.atan2(dx, dn) * 180) / Math.PI;
      const packetHeading = current?.event?.motion?.heading_deg ?? previous?.event?.motion?.heading_deg;
      return Number.isFinite(packetHeading) ? packetHeading : null;
    }

    function updateReplayPov(sample, nextSample) {
      if (!state.replayPov || !sample) return;
      const pov = window.__twin?.pov;
      if (!pov?.enterReplayFollow) {
        setReplayPov(false);
        els.replayStatus.textContent = 'POV controller is not available.';
        return;
      }
      const point = worldFromLocal(sample.local);
      if (!point) return;
      const heading = headingFromSamples(sample.previous, nextSample, sample);
      pov.enterReplayFollow(point, heading);
    }

    function drawReplayTrack(deviceId, points) {
      let line = state.replayTracks.get(deviceId);
      if (!points.length) {
        if (line) {
          trackGroup.remove(line);
          line.geometry.dispose();
          line.material.dispose();
          state.replayTracks.delete(deviceId);
        }
        return;
      }
      const geometry = new THREE.BufferGeometry().setFromPoints(points);
      if (!line) {
        line = new THREE.Line(
          geometry,
          new THREE.LineBasicMaterial({
            color: REPLAY,
            transparent: true,
            opacity: 0.78,
            depthTest: false,
          })
        );
        line.renderOrder = 998;
        trackGroup.add(line);
        state.replayTracks.set(deviceId, line);
        return;
      }
      line.geometry.dispose();
      line.geometry = geometry;
      line.visible = true;
    }

    function renderReplayAtTime(targetMs) {
      if (!state.replayEvents.length || targetMs === null) return;
      const activeDevices = new Set();
      let povSample = null;
      let povNext = null;
      state.replaySamplesByDevice.forEach((samples, deviceId) => {
        const previousIndex = sortedTimeIndexFor(samples, targetMs, (sample) => sample.ms);
        const next = samples[previousIndex + 1] || null;
        const trackEntries = state.replayTrackPoints.get(deviceId) || [];
        const trackIndex = sortedTimeIndexFor(trackEntries, targetMs, (entry) => entry.ms);
        const pathPoints = trackIndex >= 0
          ? trackEntries.slice(0, trackIndex + 1).map((entry) => entry.point)
          : [];
        if (pathPoints.length) activeDevices.add(deviceId);
        if (previousIndex < 0) {
          drawReplayTrack(deviceId, pathPoints);
          return;
        }
        const previous = samples[previousIndex];
        let current = previous;
        if (next && targetMs > previous.ms && canInterpolateSamples(previous, next)) {
          current = {
            ...previous,
            previous,
            ms: targetMs,
            local: interpolateLocal(previous, next, targetMs),
          };
        } else {
          current = { ...current, previous };
        }
        const currentWorld = worldFromLocal(current.local);
        if (currentWorld) pathPoints.push(currentWorld);
        const label = replayLabelForDeviceAt(state.replayLabelsByDevice, deviceId, targetMs) || previous.event.label;
        drawReplayTrack(deviceId, pathPoints);
        applyEvent({
          ...previous.event,
          label,
          visible: true,
          _worldPosition: currentWorld,
        }, true);
        activeDevices.add(deviceId);
        if (state.replayPov && (!state.replayPovDeviceId || state.replayPovDeviceId === deviceId)) {
          state.replayPovDeviceId = deviceId;
          povSample = current;
          povNext = next;
        }
      });
      [...state.replayTracks.keys()].forEach((deviceId) => {
        if (!activeDevices.has(deviceId)) drawReplayTrack(deviceId, []);
      });
      updateReplayPov(povSample, povNext);
    }

    function renderReplayAt(index) {
      state.replayFloatIndex = Math.max(0, Math.min(index, state.replayEvents.length - 1));
      state.replayIndex = Math.floor(state.replayFloatIndex);
      els.replayProgress.value = String(state.replayIndex);
      if (!state.replayEvents.length) return;
      state.replayClockMs = replayTimeForFloatIndex(state.replayFloatIndex);
      renderReplayAtTime(state.replayClockMs);
      const current = state.replayEvents[state.replayIndex];
      els.replayStatus.textContent = `${state.replayIndex + 1}/${state.replayEvents.length} ${formatReplayTimestamp(state.replayClockMs, current)}`;
    }

    function renderReplayAtClock(targetMs) {
      if (!state.replayEvents.length || targetMs === null) return;
      const firstMs = replayTimeForFloatIndex(0);
      const lastMs = replayTimeForFloatIndex(state.replayEvents.length - 1);
      if (firstMs === null || lastMs === null) return;
      state.replayClockMs = Math.max(firstMs, Math.min(targetMs, lastMs));
      state.replayIndex = replayIndexForTime(state.replayClockMs);
      state.replayFloatIndex = state.replayIndex;
      els.replayProgress.value = String(state.replayIndex);
      renderReplayAtTime(state.replayClockMs);
      const current = state.replayEvents[state.replayIndex];
      els.replayStatus.textContent = `${state.replayIndex + 1}/${state.replayEvents.length} ${formatReplayTimestamp(state.replayClockMs, current)}`;
    }

    function toggleReplayPlay() {
      if (state.replayTimer) {
        stopReplayPlayback(state, els.replayPlay);
        return;
      }
      if (!state.replayEvents.length) {
        stopReplayPlayback(state, els.replayPlay);
        if (els.replayStatus) els.replayStatus.textContent = 'Load replay data before playing.';
        return;
      }
      els.replayPlay.textContent = 'Pause';
      let lastTick = performance.now();
      state.replayTimer = setInterval(() => {
        const now = performance.now();
        const elapsed = now - lastTick;
        lastTick = now;
        const lastMs = replayTimeForFloatIndex(state.replayEvents.length - 1);
        if (lastMs === null) {
          stopReplayPlayback(state, els.replayPlay);
          return;
        }
        const nextMs = (state.replayClockMs ?? replayTimeForFloatIndex(state.replayFloatIndex)) +
          elapsed * replaySpeedMultiplier();
        if (nextMs >= lastMs) {
          renderReplayAtClock(lastMs);
          toggleReplayPlay();
          return;
        }
        renderReplayAtClock(nextMs);
      }, REPLAY_FRAME_MS);
    }

    async function exportReplay() {
      const day = els.replayDay.value;
      if (!day || !state.replayEvents.length) {
        els.replayStatus.textContent = 'Load replay data before exporting.';
        return;
      }
      els.replayStatus.textContent = 'Appending telemetry to twin store...';
      const body = {
        date: day,
        mode: els.replaySnapshot.checked ? 'snapshot' : 'day',
        at: replayFrameExportTimestamp(state.replayClockMs, state.replayEvents[state.replayIndex]),
      };
      const res = await liveFetch('/api/live/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok || payload.ok === false) {
        els.replayStatus.textContent = payload.error || 'Export failed.';
        return;
      }
      els.replayStatus.textContent = `Appended ${payload.event_count || 0} events for ${payload.device_count || 0} devices.`;
    }

    els.form?.addEventListener('submit', (e) => {
      e.preventDefault();
      actionGuard.run(
        'register-gateway',
        () => registerGateway(e),
        formControls(els.form),
      ).catch((err) => setStatus(err.message, 'err'));
    });
    els.discover?.addEventListener('click', () => discoverDevices().catch((err) => setDiscoverStatus(err.message, 'err')));
    els.accessBtn?.addEventListener('click', () => manageLiveToken());
    refreshLiveAccess();
    els.deviceSelect?.addEventListener('change', () => {
      els.address.value = els.deviceSelect.value;
      const device = state.discovered.find((d) => (d.address || d.id) === els.deviceSelect.value);
      if (device && !els.gatewayName.value.trim()) els.gatewayName.value = device.label || device.name || '';
    });
    els.transport?.addEventListener('change', updateTransportUi);
    els.replayToggle?.addEventListener('click', async () => {
      els.replayBar.hidden = !els.replayBar.hidden;
      state.replayMode = replayVisible();
      if (els.replayBar.hidden) {
        stopReplayPlayback(state, els.replayPlay);
        setReplayPov(false);
        // Drop the orange tracks + replay marker state, then re-sync live markers
        // (back to blue at current positions; replay-only devices pruned) so
        // closing replay doesn't leave stale replay data on the map.
        clearReplayData();
        try {
          const res = await liveFetch(liveTelemetryUrl('/api/live/latest'), { cache: 'no-store' });
          if (res.ok) hydrate(await res.json());
        } catch (_err) { /* the live stream re-syncs markers on its next event */ }
      }
      if (!els.replayBar.hidden) await loadReplayDays().catch((err) => { els.replayStatus.textContent = err.message; });
    });
    els.replayDay?.addEventListener('change', () => loadReplayEvents().catch((err) => { els.replayStatus.textContent = err.message; }));
    els.replayProgress?.addEventListener('input', (e) => {
      if (!state.replayEvents.length) return;
      renderReplayAt(Number(e.target.value));
    });
    els.replayPlay?.addEventListener('click', toggleReplayPlay);
    els.replayPov?.addEventListener('click', () => {
      setReplayPov(!state.replayPov);
      renderReplayAt(state.replayFloatIndex);
    });
    els.replayExport?.addEventListener('click', () => {
      actionGuard.run(
        'export-replay',
        () => exportReplay(),
        els.replayExport,
      ).catch((err) => { els.replayStatus.textContent = err.message; });
    });
    function closeLiveStream() {
      closeActiveEventSource(state);
    }

    function closeLiveActivity() {
      closeLiveStream();
      stopFreshnessTimer();
    }

    function onKeyDown(e) {
      if (e.key === 'Escape' && state.replayPov) setReplayPov(false);
    }

    function onVisibilityChange() {
      if (document.hidden) {
        state.liveHiddenSinceMs = Date.now();
        return;
      }
      const hiddenSince = state.liveHiddenSinceMs;
      state.liveHiddenSinceMs = null;
      const hiddenMs = Number.isFinite(hiddenSince) ? Date.now() - hiddenSince : 0;
      // A pagehide while backgrounded can tear the stream and freshness timer
      // down (e.g. bfcache); make sure both are alive again on return.
      startFreshnessTimer();
      // After a long background stint force a clean rebuild even if the socket
      // still claims OPEN — a frozen tab can leave it half-open. For a brief
      // switch only reconnect if it actually died.
      ensureLiveStream({ force: hiddenMs >= SSE_ERROR_ESCALATE_MS });
    }

    function onOnline() {
      ensureLiveStream({ force: true });
    }

    window.addEventListener('keydown', onKeyDown);
    window.addEventListener('beforeunload', closeLiveActivity);
    window.addEventListener('pagehide', closeLiveActivity);
    document.addEventListener('visibilitychange', onVisibilityChange);
    window.addEventListener('online', onOnline);

    updateTransportUi();
    setReplayControlsEnabled(false);
    startFreshnessTimer();
    connectStream();
    return {
      state,
      selectNear,
      pickAtScreen,
      showMetadata,
      reconnect: () => ensureLiveStream({ force: true }),
      destroy() {
        closeLiveActivity();
        window.removeEventListener('keydown', onKeyDown);
        window.removeEventListener('beforeunload', closeLiveActivity);
        window.removeEventListener('pagehide', closeLiveActivity);
        document.removeEventListener('visibilitychange', onVisibilityChange);
        window.removeEventListener('online', onOnline);
      },
    };
  }

  global.VEILLiveInputs = {
    create,
    _test: {
      confirmDestructiveAction,
      createInFlightGuard,
      closeActiveEventSource,
      clearTimerState,
      computeLiveDeviceFreshness,
      destructiveActionConfirmationMessage,
      buildLiveMetadataHtml,
      gatewaySuccessMessage,
      gatewayLinkedDeviceIds,
      formatReplayTimestamp,
      liveFreshnessLastPacketMs,
      liveFreshnessPositionMs,
      normalizePromptLabel,
      recordSseSuccess,
      parseReplayJsonResponse,
      replayFrameExportTimestamp,
      replayFrameTimestampMs,
      replayLoadErrorMessage,
      responseStatusText,
      resolveRegisteredGateway,
      safeParseSseJson,
      shouldApplyLiveHydrationDevice,
      shouldPruneLiveHydrationDevice,
      stopReplayPlayback,
      sortedTimeIndexFor,
      sseErrorStatus,
    },
  };
})(typeof window !== 'undefined' ? window : globalThis);
