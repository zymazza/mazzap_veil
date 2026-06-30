/* VEIL new-twin setup — default shell enhancement.
   Presentation-only. /init.js owns all behaviour; this file only reflects the
   state it already exposes — status text, the build log, the point count, the
   layer dialog, and the per-layer `veil-scan` CustomEvents — into the stepper,
   status pill, build manifest, on-map hint, and the live scan feed. It never
   drives the build itself. Loaded by init.html after /init.js. */
(function () {
  const $ = (id) => document.getElementById(id);

  const hint = $('map-hint');
  const pointCount = $('point-count');
  const statusLabel = $('status-label');
  const statusPill = $('status-pill');
  const viewerLink = $('viewer-link');
  const log = $('log');
  const dialog = $('layer-dialog');
  const steps = Array.from(document.querySelectorAll('.step'));
  const manifestItems = Array.from(document.querySelectorAll('#manifest-list li'));

  // scan feed
  const scanCard = $('scan-feed') && document.querySelector('.scan-card');
  const scanFeed = $('scan-feed');
  const scanDone = $('scan-done');
  const scanTotal = $('scan-total');
  const scanBar = $('scan-bar-fill');
  const scanSummary = $('scan-summary');

  // build feedback
  const buildCurrent = $('build-current');
  const buildElapsed = $('build-elapsed');

  const STEP_ORDER = ['locate', 'layers', 'build'];

  function statusText() {
    return (statusLabel ? statusLabel.textContent : '').trim();
  }
  function dialogOpen() {
    return !!dialog && (dialog.open || dialog.hasAttribute('open'));
  }

  function readPhase() {
    const s = statusText().toLowerCase();
    const complete = s.includes('complete') || (viewerLink && !viewerLink.hidden);
    const running = s.includes('building') || s.includes('starting build');
    const error = s.includes('failed');
    let phase = 'locate';
    if (complete || running) phase = 'build';
    else if (dialogOpen() || s.includes('scanning')) phase = 'layers';
    return { phase, complete, running, error };
  }

  function syncStepper(state) {
    const active = STEP_ORDER.indexOf(state.phase);
    steps.forEach((li, i) => {
      const done = i < active || (state.complete && i <= active);
      li.classList.toggle('is-done', done);
      li.classList.toggle('is-active', i === active && !state.complete);
    });
  }

  function syncPill(state) {
    if (!statusPill) return;
    statusPill.classList.toggle('is-running', state.running && !state.complete && !state.error);
    statusPill.classList.toggle('is-done', state.complete);
    statusPill.classList.toggle('is-error', state.error && !state.complete);
    document.body.classList.toggle('is-building', state.running || state.complete);
  }

  function syncManifest(state) {
    if (!log) return;
    const text = (log.textContent || '').toLowerCase();
    let firstPending = -1;
    manifestItems.forEach((li, i) => {
      const re = li.getAttribute('data-match') || '';
      const matched = state.complete || (text && new RegExp(re).test(text));
      li.classList.toggle('is-done', !!matched);
      li.classList.remove('is-active');
      if (!matched && firstPending === -1) firstPending = i;
    });
    if (state.running && !state.complete && firstPending >= 0) {
      manifestItems[firstPending].classList.add('is-active');
    }
  }

  function syncHint() {
    if (!hint || !pointCount) return;
    const drawn = !/^0\b/.test((pointCount.textContent || '').trim());
    hint.classList.toggle('is-hidden', drawn);
  }

  // ---- build "current step" caption + elapsed timer --------------------
  let buildStartMs = null;
  function lastLogLine() {
    const lines = (log && log.textContent ? log.textContent : '').split('\n').filter((l) => l.trim());
    return lines.length ? lines[lines.length - 1] : '';
  }
  function fmtElapsed(ms) {
    const s = Math.floor(ms / 1000);
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  }
  function syncBuild(state) {
    if (state.running && buildStartMs === null) buildStartMs = Date.now();
    if (!state.running && !state.complete) buildStartMs = null;

    if (buildCurrent) {
      const line = state.running ? lastLogLine() : '';
      buildCurrent.textContent = line;
      buildCurrent.hidden = !line;
    }
    if (buildElapsed) {
      buildElapsed.textContent = (buildStartMs !== null && (state.running || state.complete))
        ? `· ${fmtElapsed(Date.now() - buildStartMs)}` : '';
    }
  }

  function syncAll() {
    const state = readPhase();
    syncStepper(state);
    syncPill(state);
    syncManifest(state);
    syncHint();
    syncBuild(state);
  }

  // ---- live scan feed (driven by /init.js's veil-scan events) ----------
  let scanCount = 0;
  let scanHits = 0;

  function resetScan(total) {
    scanCount = 0;
    scanHits = 0;
    if (scanFeed) scanFeed.textContent = '';
    if (scanDone) scanDone.textContent = '0';
    if (scanTotal) scanTotal.textContent = String(total || 0);
    if (scanBar) scanBar.style.width = '0%';
    if (scanSummary) scanSummary.textContent = '';
    if (scanCard) scanCard.hidden = false;
  }

  function badgeFor(layer) {
    const status = layer.status;
    if (status === 'file_download' || status === 'big_download') {
      const label = layer.download_class ? `${layer.download_class} download` : 'download';
      return { cls: 'manual', text: layer.download_size ? `${label} ${layer.download_size}` : label };
    }
    if (status === 'downloadable') return { cls: 'manual', text: 'download later' };
    if (status === 'manual' || status === 'not_interactive') return { cls: 'manual', text: 'manual source' };
    if (status === 'error') return { cls: 'err', text: 'error' };
    if (status === 'ok' && layer.intersects) {
      const n = layer.feature_count;
      return { cls: 'hit', text: typeof n === 'number' ? `${n.toLocaleString()} feature${n === 1 ? '' : 's'}` : 'coverage' };
    }
    if (status === 'ok') return { cls: 'miss', text: 'no features' };
    return { cls: 'miss', text: status || 'skipped' };
  }

  function addScanRow(layer) {
    if (!scanFeed) return;
    const row = document.createElement('li');
    row.className = 'scan-row';
    const name = document.createElement('span');
    name.className = 'scan-name';
    name.textContent = layer.label || layer.id || 'layer';
    if (layer.category) {
      const cat = document.createElement('span');
      cat.className = 'scan-cat';
      cat.textContent = layer.category;
      name.append(' ', cat);
    }
    const badge = badgeFor(layer);
    const b = document.createElement('span');
    b.className = `scan-badge ${badge.cls}`;
    b.textContent = badge.text;
    row.append(name, b);
    scanFeed.appendChild(row);
    if (badge.cls === 'hit') scanHits += 1;
    scanCount += 1;
    if (scanDone) scanDone.textContent = String(scanCount);
    const total = Number(scanTotal && scanTotal.textContent) || 0;
    if (scanBar && total) scanBar.style.width = `${Math.min(100, Math.round((scanCount / total) * 100))}%`;
  }

  function finishScan() {
    if (scanBar) scanBar.style.width = '100%';
    if (scanSummary) {
      scanSummary.textContent = scanHits
        ? `${scanHits} optional layer${scanHits === 1 ? '' : 's'} intersect this area — choose which to import.`
        : 'No optional national layers reported features here. The base twin still builds.';
    }
  }

  window.addEventListener('veil-scan', (e) => {
    const d = e.detail || {};
    if (d.type === 'start') {
      document.body.classList.add('is-scanning');
      resetScan(d.total);
    } else if (d.type === 'layer' && d.layer) {
      addScanRow(d.layer);
    } else if (d.type === 'fallback') {
      if (scanCard) scanCard.hidden = false;
      if (scanSummary) scanSummary.textContent = 'Live scan unavailable — checking all layers in one pass…';
    } else if (d.type === 'done') {
      document.body.classList.remove('is-scanning');
      finishScan();
    }
    syncAll();
  });

  // React to the DOM mutations /init.js makes, instead of duplicating its polling.
  const mo = new MutationObserver(syncAll);
  [statusLabel, log, pointCount].forEach((el) => {
    if (el) mo.observe(el, { childList: true, characterData: true, subtree: true });
  });
  if (dialog) mo.observe(dialog, { attributes: true, attributeFilter: ['open'] });
  if (viewerLink) mo.observe(viewerLink, { attributes: true, attributeFilter: ['hidden'] });

  // Heartbeat: covers state changes that don't mutate a watched node, and keeps
  // the elapsed timer ticking during a build.
  setInterval(syncAll, 1000);
  syncAll();
}());
