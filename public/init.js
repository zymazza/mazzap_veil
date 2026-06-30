(function () {
  const USA_BOUNDS = L.latLngBounds(
    L.latLng(24.396308, -124.848974),
    L.latLng(49.384358, -66.885444),
  );
  const INITIAL_VIEW_BOUNDS = L.latLngBounds(
    L.latLng(22.5, -151.0),
    L.latLng(51.0, -66.5),
  );
  const map = L.map('map', {
    zoomControl: false,
    maxBounds: INITIAL_VIEW_BOUNDS.pad(0.12),
    maxBoundsViscosity: 1,
    minZoom: 3,
  });
  L.control.zoom({ position: 'bottomright' }).addTo(map);

  const streetLayer = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    noWrap: true,
    attribution: '&copy; OpenStreetMap contributors',
  });
  const orthoLayer = L.tileLayer(
    'https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}',
    {
      maxZoom: 19,
      maxNativeZoom: 16,
      noWrap: true,
      attribution: 'USGS The National Map: Orthoimagery',
    },
  );
  orthoLayer.addTo(map);
  L.control.layers({
    'Ortho imagery': orthoLayer,
    Streets: streetLayer,
  }, null, {
    position: 'topright',
    collapsed: false,
  }).addTo(map);
  const compactLayout = window.matchMedia('(max-width: 700px)').matches;
  map.fitBounds(INITIAL_VIEW_BOUNDS, compactLayout
    ? { padding: [12, 12] }
    : { paddingTopLeft: [410, 30], paddingBottomRight: [30, 30] });

  const nameInput = document.getElementById('twin-name');
  const addressSearchForm = document.getElementById('address-search-form');
  const addressSearchInput = document.getElementById('address-search');
  const addressSearchSubmit = document.getElementById('address-search-submit');
  const addressSearchResults = document.getElementById('address-search-results');
  const pointCount = document.getElementById('point-count');
  const areaLabel = document.getElementById('area-label');
  const undoButton = document.getElementById('undo-point');
  const clearButton = document.getElementById('clear-aoi');
  const setButton = document.getElementById('set-aoi');
  const statusLabel = document.getElementById('status-label');
  const viewerLink = document.getElementById('viewer-link');
  const log = document.getElementById('log');
  const layerDialog = document.getElementById('layer-dialog');
  const layerDialogSummary = document.getElementById('layer-dialog-summary');
  const layerList = document.getElementById('layer-list');
  const closeLayerDialog = document.getElementById('close-layer-dialog');
  const selectAllLayers = document.getElementById('select-all-layers');
  const selectNoLayers = document.getElementById('select-no-layers');
  const buildWithoutLayers = document.getElementById('build-without-layers');
  const buildWithLayers = document.getElementById('build-with-layers');

  let points = [];
  let markers = [];
  let shape = null;
  let pollTimer = null;
  let pendingCoordinates = null;
  let addressMarker = null;
  let addressSearchAbort = null;

  function drawnCoordinates() {
    return points.map((p) => [Number(p.lng.toFixed(7)), Number(p.lat.toFixed(7))]);
  }

  function ringAreaSqM(latLngs) {
    if (latLngs.length < 3) return 0;
    const meanLat = latLngs.reduce((sum, p) => sum + p.lat, 0) / latLngs.length;
    const mPerDegLat = 111320;
    const mPerDegLng = 111320 * Math.cos(meanLat * Math.PI / 180);
    const xy = latLngs.map((p) => ({ x: p.lng * mPerDegLng, y: p.lat * mPerDegLat }));
    let area = 0;
    for (let i = 0; i < xy.length; i += 1) {
      const j = (i + 1) % xy.length;
      area += xy[i].x * xy[j].y - xy[j].x * xy[i].y;
    }
    return Math.abs(area) / 2;
  }

  function formatArea(areaSqM) {
    if (!areaSqM) return 'Draw at least 3 points';
    const acres = areaSqM / 4046.8564224;
    if (acres < 10) return `${acres.toFixed(2)} acres`;
    if (acres < 1000) return `${acres.toFixed(1)} acres`;
    return `${(acres / 640).toFixed(2)} sq mi`;
  }

  function redraw() {
    markers.forEach((m) => m.remove());
    markers = points.map((p) => L.marker(p, {
      icon: L.divIcon({ className: 'aoi-point', iconSize: [12, 12] }),
      interactive: false,
    }).addTo(map));
    if (shape) shape.remove();
    if (points.length >= 3) {
      shape = L.polygon(points, {
        color: '#62c981',
        weight: 2,
        fillColor: '#62c981',
        fillOpacity: 0.22,
      }).addTo(map);
    } else if (points.length >= 2) {
      shape = L.polyline(points, { color: '#62c981', weight: 2 }).addTo(map);
    } else {
      shape = null;
    }
    pointCount.textContent = `${points.length} point${points.length === 1 ? '' : 's'}`;
    areaLabel.textContent = formatArea(ringAreaSqM(points));
    undoButton.disabled = points.length === 0;
    clearButton.disabled = points.length === 0;
    setButton.disabled = points.length < 3;
  }

  function clearAddressResults() {
    if (!addressSearchResults) return;
    addressSearchResults.textContent = '';
    addressSearchResults.hidden = true;
  }

  function focusAddressResult(result) {
    const lat = Number(result.lat);
    const lon = Number(result.lon);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
    const latlng = L.latLng(lat, lon);
    if (!USA_BOUNDS.contains(latlng)) {
      statusLabel.textContent = 'Address is outside the supported USA map';
      return;
    }
    if (addressMarker) addressMarker.remove();
    addressMarker = L.marker(latlng, { title: result.label || 'Address match' })
      .addTo(map)
      .bindPopup(result.label || 'Address match');
    map.flyTo(latlng, Math.max(map.getZoom(), 17), { duration: 0.7 });
    addressMarker.openPopup();
    statusLabel.textContent = 'Address located';
    clearAddressResults();
  }

  function renderAddressResults(results) {
    if (!addressSearchResults) return;
    addressSearchResults.textContent = '';
    if (!results.length) {
      const empty = document.createElement('div');
      empty.className = 'address-empty';
      empty.textContent = 'No address matches found. Try a fuller street address with city and state.';
      addressSearchResults.appendChild(empty);
      addressSearchResults.hidden = false;
      return;
    }
    results.forEach((result) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'address-result';
      const title = document.createElement('span');
      title.className = 'address-result-title';
      title.textContent = result.label || 'Address match';
      const coords = document.createElement('span');
      coords.className = 'address-result-coords';
      coords.textContent = `${Number(result.lat).toFixed(5)}, ${Number(result.lon).toFixed(5)}`;
      button.append(title, coords);
      button.addEventListener('click', () => focusAddressResult(result));
      addressSearchResults.appendChild(button);
    });
    addressSearchResults.hidden = false;
  }

  async function runAddressSearch() {
    if (!addressSearchInput) return;
    const query = addressSearchInput.value.trim();
    if (query.length < 3) {
      statusLabel.textContent = 'Enter an address to search';
      clearAddressResults();
      return;
    }
    if (addressSearchAbort) addressSearchAbort.abort();
    const controller = new AbortController();
    addressSearchAbort = controller;
    clearAddressResults();
    statusLabel.textContent = 'Searching address';
    if (addressSearchSubmit) addressSearchSubmit.disabled = true;
    try {
      const res = await fetch(`/api/init-address-search?q=${encodeURIComponent(query)}`, {
        signal: controller.signal,
      });
      const payload = await res.json();
      if (!res.ok || !payload.ok) throw new Error(payload.error || 'Address search failed');
      const results = payload.results || [];
      renderAddressResults(results);
      if (results.length === 1) focusAddressResult(results[0]);
      else statusLabel.textContent = results.length ? 'Choose an address match' : 'No address matches';
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      statusLabel.textContent = 'Address search failed';
      if (addressSearchResults) {
        addressSearchResults.textContent = err.message || 'Address search failed';
        addressSearchResults.hidden = false;
      }
    } finally {
      if (addressSearchAbort === controller) {
        addressSearchAbort = null;
        if (addressSearchSubmit) addressSearchSubmit.disabled = false;
      }
    }
  }

  function setStatus(job) {
    statusLabel.textContent = job.status === 'done' ? 'Complete'
      : job.status === 'running' ? 'Building twin'
        : job.status === 'error' ? 'Build failed'
          : 'Idle';
    log.textContent = (job.logs || []).join('\n');
    log.scrollTop = log.scrollHeight;
    viewerLink.hidden = job.status !== 'done';
  }

  async function pollStatus() {
    const res = await fetch('/api/init-status');
    const job = await res.json();
    setStatus(job);
    if (job.running) {
      pollTimer = window.setTimeout(pollStatus, 1500);
    } else {
      pollTimer = null;
    }
  }

  function selectedLayerIds() {
    return Array.from(layerList.querySelectorAll('input[type="checkbox"]:checked'))
      .map((input) => input.value);
  }

  function formatBytes(bytes) {
    const n = Number(bytes);
    if (!Number.isFinite(n) || n <= 0) return null;
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let value = n;
    let idx = 0;
    while (value >= 1000 && idx < units.length - 1) {
      value /= 1000;
      idx += 1;
    }
    return idx === 0 ? `${Math.round(value)} ${units[idx]}` : `${value.toFixed(1)} ${units[idx]}`;
  }

  function updateLayerSelectionSummary() {
    const base = layerDialogSummary.dataset.baseText || layerDialogSummary.textContent || '';
    const checked = Array.from(layerList.querySelectorAll('input[type="checkbox"]:checked'));
    const downloadBytes = checked.reduce((sum, input) => sum + (Number(input.dataset.downloadBytes) || 0), 0);
    let processedBytes = checked.reduce((sum, input) => sum + (Number(input.dataset.processedBytes) || 0), 0);
    const overheadGroups = new Map();
    checked.forEach((input) => {
      if (!input.dataset.processedGroup) return;
      const overhead = Number(input.dataset.processedOverheadBytes) || 0;
      overheadGroups.set(input.dataset.processedGroup, Math.max(overheadGroups.get(input.dataset.processedGroup) || 0, overhead));
    });
    overheadGroups.forEach((bytes) => { processedBytes += bytes; });
    const downloadText = formatBytes(downloadBytes) || '0 B';
    const processedText = formatBytes(processedBytes) || 'not estimated';
    layerDialogSummary.textContent = `${base} Selected downloads: ${downloadText}; clipped output estimate: ${processedText}.`;
  }

  function layerCountText(layer) {
    if (layer.status === 'file_download' || layer.status === 'big_download') {
      const label = layer.download_class ? `${layer.download_class} download` : 'download';
      return layer.download_size ? `${label}, ${layer.download_size}` : label;
    }
    if (layer.processed_size_estimate) {
      return `clipped output ~${layer.processed_size_estimate}`;
    }
    if (typeof layer.feature_count === 'number') {
      return `${layer.feature_count.toLocaleString()} feature${layer.feature_count === 1 ? '' : 's'}`;
    }
    return 'raster coverage';
  }

  function layerTextBody(layer, headingTag = 'h3') {
    const body = document.createElement('div');
    const title = document.createElement(headingTag);
    title.textContent = layer.label || layer.id;
    const meta = document.createElement('div');
    meta.className = 'layer-meta';
    meta.textContent = `${layer.category || 'National layer'} · ${layerCountText(layer)}`;
    const desc = document.createElement('p');
    desc.textContent = layer.description || '';
    const uses = document.createElement('p');
    uses.className = 'uses';
    uses.textContent = layer.uses ? `Useful for: ${layer.uses}` : '';
    body.append(title, meta, desc);
    if (uses.textContent) body.appendChild(uses);
    return body;
  }

  function renderLayerDialog(scan) {
    const layers = (scan.layers || []).filter((layer) => layer.intersects);
    const downloadable = (scan.layers || []).filter((layer) => layer.status === 'downloadable').length;
    const manual = (scan.layers || []).filter((layer) => layer.status === 'manual' || layer.status === 'not_interactive').length;
    layerList.textContent = '';
    if (!layers.length) {
      layerDialogSummary.textContent = (downloadable || manual)
        ? `No interactive optional layers reported features in this AOI. ${downloadable} downloadable source${downloadable === 1 ? '' : 's'} and ${manual} manual source${manual === 1 ? '' : 's'} can be added later.`
        : 'No optional national layers reported features in this AOI.';
      const empty = document.createElement('div');
      empty.className = 'empty-layers';
      empty.textContent = 'Build the base twin, then drop downloaded files in manual_layers/ and run ingest-manual-layers if needed.';
      layerList.appendChild(empty);
    } else {
      layerDialogSummary.textContent = `${layers.length} optional national layer${layers.length === 1 ? '' : 's'} intersect this AOI. Select the ones to import as clickable atlas layers.`;
      layers.forEach((layer) => {
        if (Array.isArray(layer.layer_options) && layer.layer_options.length) {
          const group = document.createElement('div');
          group.className = 'layer-option layer-group';
          const spacer = document.createElement('span');
          spacer.className = 'layer-group-marker';
          const body = layerTextBody(layer);
          const choices = document.createElement('div');
          choices.className = 'layer-suboptions';
          layer.layer_options.forEach((option) => {
            const child = document.createElement('label');
            child.className = 'layer-suboption';
            const box = document.createElement('input');
            box.type = 'checkbox';
            box.value = option.id;
            box.checked = option.default_checked !== false;
            if (option.download_bytes) box.dataset.downloadBytes = option.download_bytes;
            if (option.processed_bytes_estimate) box.dataset.processedBytes = option.processed_bytes_estimate;
            if (option.processed_group) box.dataset.processedGroup = option.processed_group;
            if (option.processed_overhead_bytes) box.dataset.processedOverheadBytes = option.processed_overhead_bytes;
            const childBody = layerTextBody({
              ...layer,
              ...option,
              status: option.status || layer.status,
              download_size: option.download_size || layer.download_size,
              download_class: option.download_class || layer.download_class,
              processed_size_estimate: option.processed_size_estimate || layer.processed_size_estimate,
              feature_count: option.feature_count ?? layer.feature_count,
            }, 'h4');
            child.append(box, childBody);
            choices.appendChild(child);
          });
          body.appendChild(choices);
          group.append(spacer, body);
          layerList.appendChild(group);
        } else {
          const row = document.createElement('label');
          row.className = 'layer-option';
          const box = document.createElement('input');
          box.type = 'checkbox';
          box.value = layer.id;
          box.checked = true;
          if (layer.download_bytes) box.dataset.downloadBytes = layer.download_bytes;
          if (layer.processed_bytes_estimate) box.dataset.processedBytes = layer.processed_bytes_estimate;
          row.append(box, layerTextBody(layer));
          layerList.appendChild(row);
        }
      });
    }
    layerDialogSummary.dataset.baseText = layerDialogSummary.textContent;
    updateLayerSelectionSummary();
    layerList.onchange = updateLayerSelectionSummary;
    buildWithLayers.disabled = false;
    if (typeof layerDialog.showModal === 'function') layerDialog.showModal();
    else layerDialog.setAttribute('open', '');
  }

  // Per-layer scan feedback rides window CustomEvents so the page chrome (the
  // live scan feed in init-shell.js) can render each layer as it resolves
  // without this module knowing anything about that UI.
  function emitScan(type, detail) {
    window.dispatchEvent(new CustomEvent('veil-scan', { detail: { type, ...detail } }));
  }

  async function scanOptionalLayersStreaming(coordinates) {
    const res = await fetch('/api/init-layer-scan-stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ coordinates }),
    });
    if (!res.ok || !res.body) throw new Error('scan stream unavailable');
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let payload = null;
    let streamError = null;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        let evt;
        try { evt = JSON.parse(line); } catch (_e) { continue; }
        if (evt.event === 'start') emitScan('start', { total: evt.total });
        else if (evt.event === 'layer') emitScan('layer', { layer: evt.layer });
        else if (evt.event === 'done') payload = { ok: evt.ok !== false, layers: evt.layers || [] };
        else if (evt.event === 'error') streamError = new Error(evt.error || 'layer scan failed');
      }
    }
    if (streamError) throw streamError;
    if (!payload) throw new Error('scan stream ended without a result');
    return payload;
  }

  async function scanOptionalLayers(coordinates) {
    statusLabel.textContent = 'Scanning optional layers';
    log.textContent = 'Checking national services for AOI intersections…';
    try {
      const payload = await scanOptionalLayersStreaming(coordinates);
      emitScan('done', { payload });
      return payload;
    } catch (streamErr) {
      // Older server / stream failure: fall back to the single buffered probe.
      emitScan('fallback', { reason: String(streamErr && streamErr.message || streamErr) });
      const res = await fetch('/api/init-layer-scan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ coordinates }),
      });
      const payload = await res.json();
      if (!res.ok || !payload.ok) {
        throw new Error(payload.error || 'Could not scan optional layers');
      }
      emitScan('done', { payload });
      return payload;
    }
  }

  async function startBuild(nationalLayers) {
    if (!pendingCoordinates || pendingCoordinates.length < 3) return;
    setButton.disabled = true;
    statusLabel.textContent = 'Starting build';
    log.textContent = '';
    const res = await fetch('/api/init-aoi', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: nameInput.value.trim() || 'VEIL twin',
        coordinates: pendingCoordinates,
        national_layers: nationalLayers || [],
      }),
    });
    const payload = await res.json();
    if (!res.ok) {
      statusLabel.textContent = 'Build failed';
      log.textContent = payload.error || 'Could not start build';
      setButton.disabled = false;
      return;
    }
    setStatus(payload.job);
    if (!pollTimer) pollStatus();
  }

  map.on('click', (event) => {
    if (!USA_BOUNDS.contains(event.latlng)) {
      statusLabel.textContent = 'Choose a point inside the USA';
      return;
    }
    points.push(event.latlng);
    redraw();
  });

  if (addressSearchForm) {
    addressSearchForm.addEventListener('submit', (event) => {
      event.preventDefault();
      runAddressSearch();
    });
  }

  undoButton.addEventListener('click', () => {
    points.pop();
    redraw();
  });

  clearButton.addEventListener('click', () => {
    points = [];
    redraw();
  });

  setButton.addEventListener('click', async () => {
    if (points.length < 3) return;
    setButton.disabled = true;
    pendingCoordinates = drawnCoordinates();
    try {
      const scan = await scanOptionalLayers(pendingCoordinates);
      renderLayerDialog(scan);
    } catch (err) {
      statusLabel.textContent = 'Layer scan failed';
      log.textContent = `${err.message || err}\n\nYou can still build the base twin.`;
      layerDialogSummary.textContent = 'The optional layer scan failed. Build the base twin now, or close this dialog and adjust the AOI.';
      layerList.innerHTML = '<div class="empty-layers">Optional national layers were not scanned, so none will be imported.</div>';
      buildWithLayers.disabled = false;
      if (typeof layerDialog.showModal === 'function') layerDialog.showModal();
      else layerDialog.setAttribute('open', '');
    } finally {
      setButton.disabled = false;
    }
  });

  closeLayerDialog.addEventListener('click', () => {
    layerDialog.close();
  });

  selectAllLayers.addEventListener('click', () => {
    layerList.querySelectorAll('input[type="checkbox"]').forEach((input) => {
      input.checked = true;
    });
  });

  selectNoLayers.addEventListener('click', () => {
    layerList.querySelectorAll('input[type="checkbox"]').forEach((input) => {
      input.checked = false;
    });
  });

  buildWithoutLayers.addEventListener('click', async () => {
    layerDialog.close();
    await startBuild([]);
  });

  buildWithLayers.addEventListener('click', async () => {
    const ids = selectedLayerIds();
    layerDialog.close();
    await startBuild(ids);
  });

  pollStatus();
  redraw();
}());
