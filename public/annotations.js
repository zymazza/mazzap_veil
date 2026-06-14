/* LLM map drawings: orange polygons and point markers an LLM places on the
   3D scene via the MCP server's draw_polygon / draw_point tools (built-in
   chat panel or any external MCP client — both write the same file). The
   viewer polls data/annotations.json (scene-local meters, written by
   scripts/twin_query.py) and renders terrain-hugging outlines, markers and
   label sprites. The chat panel's "Clear drawings" button empties the file
   via POST /api/annotations/clear. */
(function attachAnnotations(global) {
  'use strict';

  const POLL_MS = 4000;
  const ORANGE = 0xff8c1a;

  function create(viewer, _scene) {
    const { THREE } = global;
    const grid = viewer.terrainGrid;
    if (!grid) return null;

    const group = new THREE.Group();
    group.renderOrder = 998;
    viewer.scene.add(group);
    const markerMat = new THREE.MeshBasicMaterial({ color: ORANGE });
    const lineMat = new THREE.LineBasicMaterial({ color: ORANGE });

    const clearBtn = document.getElementById('chat-clear-drawings');
    const state = { count: 0, stamp: null };
    const disposables = [];

    function groundY(x, yNorth) {
      return global.VEILTerrain.sampleTerrainHeightAtLocal(grid, x, yNorth) + 1.4;
    }

    function track(obj) {
      group.add(obj);
      disposables.push(obj);
      return obj;
    }

    // small always-on-top text sprite so labeled drawings stay referable
    // ("the orange polygon labeled X") without leaving the scene
    function addLabel(text, x, yNorth, lift) {
      const pad = 8;
      const canvas = document.createElement('canvas');
      const ctx = canvas.getContext('2d');
      const font = '600 26px system-ui, sans-serif';
      ctx.font = font;
      canvas.width = Math.ceil(ctx.measureText(text).width) + pad * 2;
      canvas.height = 40;
      ctx.font = font;
      ctx.fillStyle = 'rgba(20, 14, 4, 0.78)';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = '#ff8c1a';
      ctx.textBaseline = 'middle';
      ctx.fillText(text, pad, canvas.height / 2);
      const tex = new THREE.CanvasTexture(canvas);
      const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
        map: tex, depthTest: false, transparent: true,
      }));
      const h = 7;
      sprite.scale.set((h * canvas.width) / canvas.height, h, 1);
      sprite.position.set(x, groundY(x, yNorth) + lift, -yNorth);
      sprite.renderOrder = 1000;
      track(sprite);
    }

    function addPoint(ann) {
      const m = new THREE.Mesh(new THREE.SphereGeometry(2.4, 14, 10), markerMat);
      m.position.set(ann.x, groundY(ann.x, ann.y) + 1, -ann.y);
      m.renderOrder = 999;
      track(m);
      if (ann.label) addLabel(ann.label, ann.x, ann.y, 9);
    }

    function addPolygon(ann) {
      const v = ann.vertices || [];
      if (v.length < 3) return;
      const ring = v.concat([v[0]]);
      const pts = [];
      for (let i = 1; i < ring.length; i += 1) {
        const [x1, y1] = ring[i - 1];
        const [x2, y2] = ring[i];
        const steps = Math.max(1, Math.ceil(Math.hypot(x2 - x1, y2 - y1) / 8));
        for (let s = 0; s <= steps; s += 1) {
          const x = x1 + ((x2 - x1) * s) / steps;
          const y = y1 + ((y2 - y1) * s) / steps;
          pts.push(new THREE.Vector3(x, groundY(x, y), -y));
        }
      }
      const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), lineMat);
      line.renderOrder = 999;
      track(line);
      if (ann.label) {
        const cx = v.reduce((a, p) => a + p[0], 0) / v.length;
        const cy = v.reduce((a, p) => a + p[1], 0) / v.length;
        addLabel(ann.label, cx, cy, 9);
      }
    }

    function clearScene() {
      disposables.forEach((obj) => {
        group.remove(obj);
        obj.geometry?.dispose();
        if (obj.material?.map) { obj.material.map.dispose(); obj.material.dispose(); }
      });
      disposables.length = 0;
    }

    function rebuild(annotations) {
      clearScene();
      annotations.forEach((ann) => {
        if (ann.type === 'point') addPoint(ann);
        else if (ann.type === 'polygon') addPolygon(ann);
      });
      state.count = annotations.length;
      if (clearBtn) clearBtn.disabled = !annotations.length;
    }

    async function refresh(initial) {
      let annotations = [];
      let layerViews = [];
      let stamp = 'absent';
      try {
        const res = await fetch('/data/annotations.json', { cache: 'no-store' });
        if (res.ok) {
          const text = await res.text();
          stamp = text;
          const doc = JSON.parse(text);
          annotations = doc.annotations || [];
          layerViews = doc.layer_views || [];
        }
      } catch (_err) { /* unreadable = unchanged; keep what's shown */ return; }
      if (stamp === state.stamp) return;
      state.stamp = stamp;
      rebuild(annotations);
      // app.js owns the atlas drape, so hand it the layer-view overrides the
      // MCP server set (set_layer_visibility / filter_layer / reset_layer_views).
      // On the initial boot poll we only take this file as the baseline: any
      // layer_views persisted from a prior session are ignored so the app
      // always opens with atlas layers at their hidden default. Only live
      // changes during this session drive the drape.
      if (!initial) global.__twin?.applyLayerViews?.(layerViews);
    }

    async function clear() {
      try {
        await fetch('/api/annotations/clear', { method: 'POST' });
      } catch (_err) { /* server gone; the poll will reconcile */ }
      await refresh();
    }

    // Each viewer open starts from a clean map: wipe any drawings (and the
    // layer-view overrides) an LLM left in data/annotations.json during a prior
    // session, so stale orange shapes never greet the next visit. The clear()
    // POST empties the file server-side and the follow-up refresh repaints the
    // now-empty scene; if the server is unreachable we fall back to a passive
    // read of whatever's on disk (initial-poll semantics: ignore layer_views).
    async function clearOnOpen() {
      try {
        const res = await fetch('/api/annotations/clear', { method: 'POST' });
        if (res.ok) { await refresh(); return; }
      } catch (_err) { /* server gone — show whatever's on disk */ }
      refresh(true);
    }

    if (clearBtn) {
      clearBtn.disabled = true;
      clearBtn.addEventListener('click', clear);
    }
    clearOnOpen();
    setInterval(refresh, POLL_MS);

    return { state, refresh, clear };
  }

  global.VEILAnnotations = { create };
})(typeof window !== 'undefined' ? window : globalThis);
