(function attachSceneViewer(global) {
  const { THREE, VEILCamera, VEILTerrain, VEILVegetation, VEILOverlays } = global;
  const VEGETATION_CHUNK_CONFIG = {
    defaultLoadRadiusHintChunks: 2,
    defaultSizeMeters: 96,
    farCameraRingDistanceMeters: 420,
    minLoadRadiusHintChunks: 1,
    minSizeMeters: 24,
  };
  const VIEWER_CAMERA_DEFAULTS = {
    far: 10000,
    fov: 50,
    maxDevicePixelRatio: 2,
    near: 0.1,
    position: { x: 140, y: 120, z: 140 },
  };
  const GRID_HELPER_CONFIG = {
    clipBoundsHalfSpanMeters: 300,
    divisions: 24,
    initialSizeMeters: 600,
    majorColor: 0x2a6041,
    minSizeMeters: 120,
    minorColor: 0x8d98a7,
    sizePaddingMultiplier: 1.08,
    sizeSnapIncrementMeters: 20,
  };
  const LAYER_JOB_TYPE_BY_ID = {
    buildings: 'buildings_fetch',
    hydrology: 'hydrology_fetch',
    parcels: 'parcels_fetch',
    roads: 'roads_fetch',
    soils: 'site_snapshot_compute',
    trails: 'trails_fetch',
    vegetation: 'vegetation_extract',
  };

  function debugLog(event, payload = {}) {
    global.__VEIL_DEBUG__?.log?.(event, payload);
  }

  function parseResourceUrl(url) {
    if (typeof url !== 'string' || !url) {
      return null;
    }

    try {
      return new URL(url, global.location?.origin || 'http://localhost');
    } catch (_error) {
      return null;
    }
  }

  function readResourceVersionToken(url) {
    const value = parseResourceUrl(url)?.searchParams.get('v');
    return typeof value === 'string' && value.trim() ? value.trim() : null;
  }

  function appendResourceVersionToken(url, versionToken) {
    if (typeof url !== 'string' || !url || !versionToken) {
      return url;
    }

    const parsed = parseResourceUrl(url);
    if (parsed) {
      if (!parsed.searchParams.has('v')) {
        parsed.searchParams.set('v', versionToken);
      }
      return `${parsed.pathname}${parsed.search}`;
    }

    const separator = url.includes('?') ? '&' : '?';
    return `${url}${separator}v=${encodeURIComponent(versionToken)}`;
  }

  function createAbortError() {
    try {
      return new DOMException('The operation was aborted.', 'AbortError');
    } catch (_error) {
      const error = new Error('The operation was aborted.');
      error.name = 'AbortError';
      return error;
    }
  }

  function isAbortError(error) {
    return error?.name === 'AbortError';
  }

  function linkAbortSignal(signal, controller) {
    if (!signal) {
      return () => {};
    }
    if (signal.aborted) {
      controller.abort(signal.reason);
      return () => {};
    }

    const onAbort = () => {
      controller.abort(signal.reason);
    };

    signal.addEventListener('abort', onAbort, { once: true });
    return () => signal.removeEventListener('abort', onAbort);
  }

  function getVegetationItemCount(payload) {
    if (Array.isArray(payload)) {
      return payload.length;
    }
    if (payload && (Array.isArray(payload.items) || ArrayBuffer.isView(payload.items)) && Number(payload.stride) > 0) {
      return Math.floor(payload.items.length / Number(payload.stride));
    }
    return 0;
  }

  function createVegetationChunkState() {
    return {
      availableKeys: {
        shrubs: new Set(),
        trees: new Set(),
      },
      chunkSize: VEGETATION_CHUNK_CONFIG.defaultSizeMeters,
      lastSignature: null,
      loaded: {
        shrubs: new Map(),
        trees: new Map(),
      },
      loadRadiusHint: VEGETATION_CHUNK_CONFIG.defaultLoadRadiusHintChunks,
      manifest: null,
      pending: new Map(),
      requestVersion: 0,
    };
  }

  function readSceneComputeJob(scenePayload, layerId) {
    const jobType = LAYER_JOB_TYPE_BY_ID[layerId];
    if (!jobType || !Array.isArray(scenePayload?.compute_status?.jobs)) {
      return null;
    }

    return scenePayload.compute_status.jobs.find((job) => job?.id === jobType) || null;
  }

  function normalizeLayerMessage(...candidates) {
    const value = candidates.find((candidate) => typeof candidate === 'string' && candidate.trim());
    return value ? value.trim() : '';
  }

  function resolveDeferredLayerState(scenePayload, layerId, options = {}) {
    const job = readSceneComputeJob(scenePayload, layerId);
    const layerBlock = options.layerBlock && typeof options.layerBlock === 'object' ? options.layerBlock : {};
    const layerLabel = options.layerLabel || layerId;
    const rawStatus =
      job?.status === 'error' || job?.status === 'canceled'
        ? 'error'
        : options.layerStatus || layerBlock.status || job?.status || 'queued';
    const message = normalizeLayerMessage(
      job?.message,
      layerBlock.message,
      layerBlock.error_message,
      layerBlock.degraded_reason
    );

    if (rawStatus === 'error') {
      return {
        message: message || `${layerLabel} layer failed.`,
        status: 'error',
      };
    }

    if (rawStatus === 'ready') {
      return {
        message: message || `${layerLabel} layer is marked ready, but no data URL is available.`,
        status: 'error',
      };
    }

    if (['degraded', 'fallback', 'polygon_only', 'unavailable'].includes(rawStatus)) {
      return {
        message: message || `${layerLabel} layer unavailable.`,
        status: rawStatus,
      };
    }

    if (rawStatus === 'waiting') {
      return {
        message: message || options.waitingMessage || `Waiting for ${layerLabel.toLowerCase()} layer`,
        status: 'loading',
      };
    }

    return {
      message: message || options.waitingMessage || `Waiting for ${layerLabel.toLowerCase()} layer`,
      status: 'queued',
    };
  }

  function getPackedItemsArray(payload) {
    if (payload && (Array.isArray(payload.items) || ArrayBuffer.isView(payload.items))) {
      return payload.items;
    }
    return [];
  }

  function mergePackedVegetationPayload(kind, payloads) {
    const stride = kind === 'tree' ? 4 : 3;
    const totalLength = payloads.reduce((sum, payload) => sum + getPackedItemsArray(payload).length, 0);
    const items = new Float32Array(totalLength);
    let offset = 0;

    payloads.forEach((payload) => {
      const source = getPackedItemsArray(payload);
      items.set(source, offset);
      offset += source.length;
    });

    return {
      count: Math.floor(totalLength / stride),
      format: 'packed-vegetation-v1',
      items,
      kind,
      stride,
    };
  }

  function getChunkKeyForLocalPoint(x, y, chunkSize) {
    return `${Math.floor(Number(x) / chunkSize)}_${Math.floor(Number(y) / chunkSize)}`;
  }

  class WorkspaceViewer {
    constructor(rootEl) {
      this.rootEl = rootEl;
      this.destroyed = false;
      this.scene = new THREE.Scene();
      this.scene.background = new THREE.Color('#dcccbb');

      this.camera = new THREE.PerspectiveCamera(
        VIEWER_CAMERA_DEFAULTS.fov,
        1,
        VIEWER_CAMERA_DEFAULTS.near,
        VIEWER_CAMERA_DEFAULTS.far
      );
      this.camera.position.set(
        VIEWER_CAMERA_DEFAULTS.position.x,
        VIEWER_CAMERA_DEFAULTS.position.y,
        VIEWER_CAMERA_DEFAULTS.position.z
      );

      this.renderer = new THREE.WebGLRenderer({ antialias: true });
      this.renderer.localClippingEnabled = true;
      this.renderer.setPixelRatio(
        Math.min(window.devicePixelRatio || 1, VIEWER_CAMERA_DEFAULTS.maxDevicePixelRatio)
      );
      this.rootEl.replaceChildren(this.renderer.domElement);

      this.controls = VEILCamera.create(this.camera, this.renderer.domElement);

      this.ambientLight = new THREE.AmbientLight(0xffffff, 0.85);
      this.sunLight = new THREE.DirectionalLight(0xffffff, 1.15);
      this.sunLight.position.set(180, 240, 220);
      this.scene.add(this.ambientLight, this.sunLight);

      this.gridHelper = new THREE.GridHelper(
        GRID_HELPER_CONFIG.initialSizeMeters,
        GRID_HELPER_CONFIG.divisions,
        GRID_HELPER_CONFIG.majorColor,
        GRID_HELPER_CONFIG.minorColor
      );
      this.scene.add(this.gridHelper);
      this.overlayClipBounds = {
        minX: -GRID_HELPER_CONFIG.clipBoundsHalfSpanMeters,
        minY: -GRID_HELPER_CONFIG.clipBoundsHalfSpanMeters,
        maxX: GRID_HELPER_CONFIG.clipBoundsHalfSpanMeters,
        maxY: GRID_HELPER_CONFIG.clipBoundsHalfSpanMeters,
      };

      this.terrainMesh = null;
      this.terrainDrapeOverlayMesh = null;
      this.aoiBoundary = null;
      this.parcelGroup = null;
      this.scenePayload = null;
      this.terrainGrid = null;
      this.elevationMaterial = null;
      this.textureLoader = new THREE.TextureLoader();
      this.textureCache = new Map();
      this.textureLoadPromises = new Map();
      this.activeTerrainMode = 'elevation';
      this.layerVisibility = new Map();
      this.vegetationRenderer = VEILVegetation.create(this.scene);
      this.overlayRenderer = VEILOverlays.create(this.scene);
      this.renderStats = {
        aoi: 0,
        buildings: 0,
        hydrology: 0,
        parcels: 0,
        roads: 0,
        shrubs: 0,
        soils: 0,
        trails: 0,
        trees: 0,
      };
      this.overlayData = {
        buildings: { type: 'FeatureCollection', features: [] },
        hydrology: { type: 'FeatureCollection', features: [] },
        roads: { type: 'FeatureCollection', features: [] },
        soils: { type: 'FeatureCollection', features: [] },
        trails: { type: 'FeatureCollection', features: [] },
      };
      this.vegetationData = {
        treeInstances: [],
        shrubPoints: [],
      };
      this.vegetationChunkState = createVegetationChunkState();
      this.lastVegetationChunkSyncAt = 0;
      this.loadToken = 0;
      this.animationFrame = null;
      this.activeFetchControllers = new Set();
      this.keyboardPanState = VEILCamera.createKeyboardPanState();
      this.unbindKeyboardPan = VEILCamera.bindKeyboardPan(this.keyboardPanState);
      this.lastFrameTime = performance.now();

      this.animate = this.animate.bind(this);
      this.resize = this.resize.bind(this);
      window.addEventListener('resize', this.resize);
      this.resize();
      this.animate();
    }

    updateScenePayload(scenePayload) {
      if (this.destroyed) {
        return;
      }
      this.scenePayload = scenePayload;
    }

    syncDeferredLayerStates(callbacks = {}) {
      if (this.destroyed || !this.scenePayload) {
        return;
      }

      const vegetation = this.scenePayload.vegetation || {};
      const deferredLayerSpecs = [
        ['parcels', !this.scenePayload.parcels?.features_url, {
          layerBlock: this.scenePayload.parcels,
          layerLabel: 'Parcel',
          waitingMessage: 'Waiting for parcel layer',
        }],
        ['buildings', !this.scenePayload.buildings?.footprints_url, {
          layerBlock: this.scenePayload.buildings,
          layerLabel: 'Buildings',
        }],
        ['hydrology', !this.scenePayload.hydrology?.features_url, {
          layerBlock: this.scenePayload.hydrology,
          layerLabel: 'Hydrology',
        }],
        ['roads', !this.scenePayload.roads_trails?.roads_url, {
          layerBlock: this.scenePayload.roads_trails,
          layerLabel: 'Roads',
        }],
        ['soils', !this.scenePayload.soils?.features_url, {
          layerBlock: this.scenePayload.soils,
          layerLabel: 'Soils',
        }],
        ['trails', !this.scenePayload.roads_trails?.trails_url, {
          layerBlock: this.scenePayload.roads_trails,
          layerLabel: 'Trails',
        }],
        ['vegetation', !vegetation.tree_instances_url && !vegetation.shrub_points_url && !vegetation.chunks_url, {
          layerBlock: vegetation,
          layerLabel: 'Vegetation',
        }],
      ];

      deferredLayerSpecs.forEach(([layerId, shouldReport, options]) => {
        if (!shouldReport) {
          return;
        }

        const nextState = resolveDeferredLayerState(this.scenePayload, layerId, options);
        callbacks.onLayerState?.(layerId, nextState.status, { message: nextState.message });
      });
    }

    abortPendingFetches() {
      this.activeFetchControllers.forEach((controller) => controller.abort());
      this.activeFetchControllers.clear();
    }

    async fetchJson(url, options = {}) {
      if (this.destroyed) {
        throw createAbortError();
      }

      const startedAt = performance.now();
      const controller = new AbortController();
      const unlinkAbortSignal = linkAbortSignal(options.signal, controller);
      this.activeFetchControllers.add(controller);

      try {
        const response = await fetch(url, { signal: controller.signal });
        if (!response.ok) {
          debugLog('viewer-fetch-error', {
            duration_ms: Math.round(performance.now() - startedAt),
            status: response.status,
            url,
          });
          throw new Error(`Failed to load ${url}: ${response.status}`);
        }
        const payload = await response.json();
        debugLog('viewer-fetch-success', {
          duration_ms: Math.round(performance.now() - startedAt),
          status: response.status,
          url,
        });
        return payload;
      } catch (error) {
        if (isAbortError(error)) {
          debugLog('viewer-fetch-abort', {
            duration_ms: Math.round(performance.now() - startedAt),
            url,
          });
        }
        throw error;
      } finally {
        unlinkAbortSignal();
        this.activeFetchControllers.delete(controller);
      }
    }

    resetLoadedLayerState() {
      this.overlayData = {
        buildings: { type: 'FeatureCollection', features: [] },
        hydrology: { type: 'FeatureCollection', features: [] },
        roads: { type: 'FeatureCollection', features: [] },
        soils: { type: 'FeatureCollection', features: [] },
        trails: { type: 'FeatureCollection', features: [] },
      };
      this.vegetationData = {
        treeInstances: [],
        shrubPoints: [],
      };
      this.vegetationChunkState = createVegetationChunkState();
      this.lastVegetationChunkSyncAt = 0;
    }

    disposeTextureCache() {
      this.textureLoadPromises.clear();
      this.textureCache.forEach((texture) => texture.dispose());
      this.textureCache.clear();
    }

    disposeTerrainDrapeOverlay() {
      if (!this.terrainDrapeOverlayMesh) {
        return;
      }

      this.scene.remove(this.terrainDrapeOverlayMesh);
      if (this.terrainDrapeOverlayMesh.material) {
        this.terrainDrapeOverlayMesh.material.dispose();
      }
      this.terrainDrapeOverlayMesh = null;
    }

    disposeActiveTerrainMaterial() {
      if (this.terrainMesh?.material && this.terrainMesh.material !== this.elevationMaterial) {
        this.terrainMesh.material.dispose();
      }
    }

    async loadTerrainTexture(textureUrl) {
      if (this.destroyed) {
        return null;
      }

      let texture = this.textureCache.get(textureUrl);
      if (!texture) {
        let pending = this.textureLoadPromises.get(textureUrl);
        if (!pending) {
          const loadPromise = this.textureLoader.loadAsync(textureUrl)
            .then((loadedTexture) => {
              if (this.destroyed) {
                loadedTexture.dispose?.();
                return null;
              }
              loadedTexture.colorSpace = THREE.SRGBColorSpace;
              this.textureCache.set(textureUrl, loadedTexture);
              return loadedTexture;
            })
            .finally(() => {
              if (this.textureLoadPromises.get(textureUrl) === loadPromise) {
                this.textureLoadPromises.delete(textureUrl);
              }
            });
          this.textureLoadPromises.set(textureUrl, loadPromise);
          pending = loadPromise;
        }
        texture = await pending;
      }
      return texture;
    }

    prefetchJson(url) {
      if (!url) {
        return Promise.resolve({ error: null, payload: null });
      }

      return this.fetchJson(url)
        .then((payload) => ({ error: null, payload }))
        .catch((error) => ({ error, payload: null }));
    }

    warmTerrainTextureCache(imagery = this.scenePayload?.imagery || {}) {
      const textureUrls = [imagery.hillshade_url, imagery.false_color_url, imagery.drape_url].filter(Boolean);
      if (!textureUrls.length || this.destroyed) {
        return;
      }

      Promise.allSettled(textureUrls.map((url) => this.loadTerrainTexture(url)))
        .then((results) => {
          if (this.destroyed) {
            return;
          }
          debugLog('viewer-terrain-textures-warmed', {
            attempted_count: textureUrls.length,
            failed_count: results.filter((result) => result.status === 'rejected').length,
            workspace_id: this.scenePayload?.workspace_id || null,
          });
        });
    }

    buildVegetationChunkUrl(kind, key) {
      const versionToken = readResourceVersionToken(this.scenePayload?.vegetation?.chunks_url);
      return appendResourceVersionToken(
        `/api/workspaces/${this.scenePayload.workspace_id}/vegetation/chunks/${kind}/${encodeURIComponent(key)}.json`,
        versionToken
      );
    }

    rerenderOverlayLayers() {
      this.overlayRenderer.load({
        buildings: this.overlayData.buildings,
        grid: this.terrainGrid,
        clipBounds: this.overlayClipBounds,
        hydrology: this.overlayData.hydrology,
        roads: this.overlayData.roads,
        soils: this.overlayData.soils,
        trails: this.overlayData.trails,
      });
      Object.assign(this.renderStats, this.overlayRenderer.getRenderStats());
    }

    rerenderOverlayLayer(layerId) {
      this.overlayRenderer.loadLayer(
        layerId,
        this.overlayData[layerId],
        this.terrainGrid,
        this.overlayClipBounds
      );
      Object.assign(this.renderStats, this.overlayRenderer.getRenderStats());
      debugLog('viewer-overlay-rerender', {
        layerId,
        rendered_count: this.renderStats[layerId] || 0,
        workspace_id: this.scenePayload?.workspace_id || null,
      });
    }

    rerenderVegetation() {
      this.vegetationRenderer.load({
        grid: this.terrainGrid,
        shrubPoints: this.vegetationData.shrubPoints,
        treeInstances: this.vegetationData.treeInstances,
      });
      Object.assign(this.renderStats, this.vegetationRenderer.getRenderStats());
      debugLog('viewer-vegetation-rerender', {
        loaded_shrub_count: getVegetationItemCount(this.vegetationData.shrubPoints),
        loaded_tree_count: getVegetationItemCount(this.vegetationData.treeInstances),
        rendered_shrubs: this.renderStats.shrubs || 0,
        rendered_trees: this.renderStats.trees || 0,
        workspace_id: this.scenePayload?.workspace_id || null,
      });
    }

    setVegetationChunkManifest(manifest) {
      const nextState = createVegetationChunkState();
      nextState.requestVersion = this.vegetationChunkState.requestVersion + 1;
      nextState.manifest = manifest || null;
      nextState.chunkSize = Math.max(
        VEGETATION_CHUNK_CONFIG.minSizeMeters,
        Number(manifest?.chunk_size) || VEGETATION_CHUNK_CONFIG.defaultSizeMeters
      );
      nextState.loadRadiusHint = Math.max(
        VEGETATION_CHUNK_CONFIG.minLoadRadiusHintChunks,
        Number(manifest?.load_radius_hint_chunks) ||
          VEGETATION_CHUNK_CONFIG.defaultLoadRadiusHintChunks
      );
      nextState.availableKeys.trees = new Set((manifest?.trees?.chunks || []).map((chunk) => String(chunk.key)));
      nextState.availableKeys.shrubs = new Set((manifest?.shrubs?.chunks || []).map((chunk) => String(chunk.key)));
      this.vegetationChunkState = nextState;
      debugLog('viewer-vegetation-manifest', {
        chunk_size: nextState.chunkSize,
        load_radius_hint: nextState.loadRadiusHint,
        shrub_chunks: nextState.availableKeys.shrubs.size,
        tree_chunks: nextState.availableKeys.trees.size,
        workspace_id: this.scenePayload?.workspace_id || null,
      });
    }

    rebuildVegetationDataFromLoadedChunks() {
      this.vegetationData = {
        shrubPoints: mergePackedVegetationPayload(
          'shrub',
          Array.from(this.vegetationChunkState.loaded.shrubs.values())
        ),
        treeInstances: mergePackedVegetationPayload(
          'tree',
          Array.from(this.vegetationChunkState.loaded.trees.values())
        ),
      };
      this.rerenderVegetation();
      debugLog('viewer-vegetation-chunks-merged', {
        loaded_shrub_chunks: this.vegetationChunkState.loaded.shrubs.size,
        loaded_tree_chunks: this.vegetationChunkState.loaded.trees.size,
        workspace_id: this.scenePayload?.workspace_id || null,
      });
    }

    getVegetationChunkViewState() {
      if (!this.terrainGrid || !this.vegetationChunkState.manifest) {
        return null;
      }

      const chunkSize = this.vegetationChunkState.chunkSize;
      const centerX = Number(this.controls.target.x || 0);
      const centerY = Number(-this.controls.target.z || 0);
      const cameraDistance = this.camera.position.distanceTo(this.controls.target);
      const ring =
        cameraDistance > VEGETATION_CHUNK_CONFIG.farCameraRingDistanceMeters
          ? this.vegetationChunkState.loadRadiusHint
          : Math.max(
            VEGETATION_CHUNK_CONFIG.minLoadRadiusHintChunks,
            this.vegetationChunkState.loadRadiusHint - 1
          );
      const centerKey = getChunkKeyForLocalPoint(centerX, centerY, chunkSize);

      return {
        centerKey,
        centerX,
        centerY,
        ring,
        signature: `${centerKey}:${ring}`,
      };
    }

    getDesiredVegetationChunkKeys(kind, viewState) {
      const keys = new Set();
      const [chunkX, chunkY] = String(viewState.centerKey)
        .split('_')
        .map((value) => Number(value));
      const available = this.vegetationChunkState.availableKeys[kind];

      for (let offsetX = -viewState.ring; offsetX <= viewState.ring; offsetX += 1) {
        for (let offsetY = -viewState.ring; offsetY <= viewState.ring; offsetY += 1) {
          const key = `${chunkX + offsetX}_${chunkY + offsetY}`;
          if (available.has(key)) {
            keys.add(key);
          }
        }
      }

      return keys;
    }

    async updateVegetationChunks(options = {}) {
      const manifest = this.vegetationChunkState.manifest;
      if (!manifest || !this.scenePayload?.workspace_id) {
        return;
      }

      const viewState = this.getVegetationChunkViewState();
      if (!viewState) {
        return;
      }

      if (!options.force && viewState.signature === this.vegetationChunkState.lastSignature) {
        return;
      }
      this.vegetationChunkState.lastSignature = viewState.signature;
      debugLog('viewer-vegetation-chunk-sync', {
        center_key: viewState.centerKey,
        ring: viewState.ring,
        signature: viewState.signature,
        workspace_id: this.scenePayload?.workspace_id || null,
      });

      const desired = {
        shrubs: this.getDesiredVegetationChunkKeys('shrubs', viewState),
        trees: this.getDesiredVegetationChunkKeys('trees', viewState),
      };

      let changed = false;
      ['trees', 'shrubs'].forEach((kind) => {
        Array.from(this.vegetationChunkState.loaded[kind].keys()).forEach((key) => {
          if (!desired[kind].has(key)) {
            this.vegetationChunkState.loaded[kind].delete(key);
            changed = true;
          }
        });
      });

      if (changed) {
        this.rebuildVegetationDataFromLoadedChunks();
      }

      const requestVersion = this.vegetationChunkState.requestVersion;
      const sceneLoadToken = options.sceneLoadToken ?? this.loadToken;
      const fetches = [];

      ['trees', 'shrubs'].forEach((kindPlural) => {
        const kindSingular = kindPlural === 'trees' ? 'tree' : 'shrub';
        desired[kindPlural].forEach((key) => {
          if (this.vegetationChunkState.loaded[kindPlural].has(key)) {
            return;
          }
          const pendingKey = `${kindSingular}:${key}`;
          if (this.vegetationChunkState.pending.has(pendingKey)) {
            if (options.waitForPending) {
              fetches.push(this.vegetationChunkState.pending.get(pendingKey));
            }
            return;
          }

          const request = this.fetchJson(this.buildVegetationChunkUrl(kindSingular, key))
            .then((payload) => {
              if (
                requestVersion !== this.vegetationChunkState.requestVersion ||
                sceneLoadToken !== this.loadToken
              ) {
                return false;
              }
              this.vegetationChunkState.loaded[kindPlural].set(key, payload);
              debugLog('viewer-vegetation-chunk-loaded', {
                count: payload?.count || 0,
                key,
                kind: kindSingular,
                workspace_id: this.scenePayload?.workspace_id || null,
              });
              return true;
            })
            .catch((error) => {
              if (
                this.destroyed ||
                requestVersion !== this.vegetationChunkState.requestVersion ||
                sceneLoadToken !== this.loadToken ||
                isAbortError(error)
              ) {
                return false;
              }
              debugLog('viewer-vegetation-chunk-error', {
                error: error.message,
                key,
                kind: kindSingular,
                workspace_id: this.scenePayload?.workspace_id || null,
              });
              return false;
            })
            .finally(() => {
              this.vegetationChunkState.pending.delete(pendingKey);
            });

          this.vegetationChunkState.pending.set(pendingKey, request);
          fetches.push(request);
        });
      });

      if (options.waitForPending) {
        const results = await Promise.all(fetches);
        if (results.some(Boolean)) {
          this.rebuildVegetationDataFromLoadedChunks();
        }
        return;
      }

      if (fetches.length) {
        Promise.all(fetches).then((results) => {
          if (results.some(Boolean)) {
            this.rebuildVegetationDataFromLoadedChunks();
          }
        });
      }
    }

    async streamLoad(scenePayload, callbacks = {}) {
      if (this.destroyed) {
        return;
      }
      this.abortPendingFetches();
      this.clearScene();
      const loadToken = ++this.loadToken;
      const vegetation = scenePayload.vegetation || {};
      const reportSuccess = (layerId, detail = null) => {
        callbacks.onLayerState?.(layerId, 'ready', detail);
      };
      const reportError = (layerId, error) => {
        if (this.destroyed || loadToken !== this.loadToken || isAbortError(error)) {
          return;
        }
        callbacks.onLayerState?.(layerId, 'error', { message: error.message });
      };
      const reportLoading = (layerId, detail = null) => {
        callbacks.onLayerState?.(layerId, 'loading', detail);
      };

      this.resetLoadedLayerState();
      this.scenePayload = scenePayload;
      this.syncDeferredLayerStates(callbacks);
      debugLog('viewer-stream-load-start', {
        load_token: loadToken,
        workspace_id: scenePayload?.workspace_id || null,
      });
      const shouldLoadFullVegetation =
        Boolean(vegetation.tree_instances_url) || Boolean(vegetation.shrub_points_url);
      const gridPromise = this.prefetchJson(scenePayload.terrain.grid_url);
      const aoiBoundaryPromise = this.prefetchJson(scenePayload.aoi_boundary?.geojson_url);
      const parcelsPromise = this.prefetchJson(scenePayload.parcels?.features_url);
      const overlayRequests = new Map(
        [
          ['buildings', scenePayload.buildings?.footprints_url],
          ['hydrology', scenePayload.hydrology?.features_url],
          ['roads', scenePayload.roads_trails?.roads_url],
          ['soils', scenePayload.soils?.features_url],
          ['trails', scenePayload.roads_trails?.trails_url],
        ]
          .filter(([, url]) => Boolean(url))
          .map(([layerId, url]) => [layerId, this.prefetchJson(url)])
      );
      const vegetationChunkManifestPromise =
        !shouldLoadFullVegetation && vegetation.chunks_url ? this.prefetchJson(vegetation.chunks_url) : null;
      const treeInstancesPromise =
        shouldLoadFullVegetation && vegetation.tree_instances_url
          ? this.prefetchJson(vegetation.tree_instances_url)
          : Promise.resolve({ error: null, payload: [] });
      const shrubPointsPromise =
        shouldLoadFullVegetation && vegetation.shrub_points_url
          ? this.prefetchJson(vegetation.shrub_points_url)
          : Promise.resolve({ error: null, payload: [] });
      reportLoading('terrain');
      const gridResult = await gridPromise;
      if (gridResult.error) {
        throw gridResult.error;
      }
      const grid = gridResult.payload;
      if (loadToken !== this.loadToken) {
        return;
      }
      this.terrainGrid = grid;
      const terrain = VEILTerrain.buildTerrainMesh(grid);
      this.terrainMesh = terrain.mesh;
      this.elevationMaterial = terrain.mesh.material;
      this.scene.add(this.terrainMesh);
      debugLog('viewer-terrain-ready', {
        grid_height: grid.height,
        grid_width: grid.width,
        max_x: grid.maxX,
        max_y: grid.maxY,
        min_x: grid.minX,
        min_y: grid.minY,
        workspace_id: scenePayload?.workspace_id || null,
      });
      this.updateGridHelper(grid);
      this.setTerrainRenderMode('elevation');
      this.setCameraFromGrid(grid);
      reportSuccess('terrain', { count: 1 });
      callbacks.onTerrainReady?.();
      this.warmTerrainTextureCache(scenePayload.imagery);

      reportLoading('aoi');
      aoiBoundaryPromise.then(({ error, payload: aoiBoundary }) => {
        if (error) {
          reportError('aoi', error);
          return;
        }
        if (loadToken !== this.loadToken) {
          return;
        }
        this.addAoiBoundary(aoiBoundary, this.terrainGrid);
        this.renderStats.aoi = aoiBoundary.features?.length ? 1 : 0;
        reportSuccess('aoi', { count: this.renderStats.aoi, rendered_count: this.renderStats.aoi });
      });

      if (!scenePayload.parcels.features_url) {
        const nextState = resolveDeferredLayerState(scenePayload, 'parcels', {
          layerBlock: scenePayload.parcels,
          layerLabel: 'Parcel',
          waitingMessage: 'Waiting for parcel layer',
        });
        callbacks.onLayerState?.('parcels', nextState.status, { message: nextState.message });
      } else {
        reportLoading('parcels');
        parcelsPromise.then(({ error, payload: parcelFeatures }) => {
          if (error) {
            reportError('parcels', error);
            return;
          }
          if (loadToken !== this.loadToken) {
            return;
          }
          this.addParcelOutlines(parcelFeatures, this.terrainGrid);
          reportSuccess('parcels', {
            count: parcelFeatures.features?.length || 0,
            rendered_count: this.renderStats.parcels,
          });
        });
      }

      [
        ['buildings', scenePayload.buildings?.footprints_url],
        ['hydrology', scenePayload.hydrology?.features_url],
        ['roads', scenePayload.roads_trails?.roads_url],
        ['soils', scenePayload.soils?.features_url],
        ['trails', scenePayload.roads_trails?.trails_url],
      ].forEach(([layerId, url]) => {
        if (!url) {
          const layerBlock =
            layerId === 'buildings'
              ? scenePayload.buildings
              : layerId === 'hydrology'
                ? scenePayload.hydrology
                : layerId === 'soils'
                  ? scenePayload.soils
                  : scenePayload.roads_trails;
          const nextState = resolveDeferredLayerState(scenePayload, layerId, {
            layerBlock,
            layerLabel: layerId.charAt(0).toUpperCase() + layerId.slice(1),
          });
          callbacks.onLayerState?.(layerId, nextState.status, { message: nextState.message });
          return;
        }
        reportLoading(layerId);
        overlayRequests.get(layerId)
          .then(({ error, payload: featureCollection }) => {
            if (error) {
              reportError(layerId, error);
              return;
            }
            if (loadToken !== this.loadToken) {
              return;
            }
            this.overlayData[layerId] = featureCollection;
            this.rerenderOverlayLayer(layerId);
            reportSuccess(layerId, {
              count: featureCollection.features?.length || 0,
              legend_entries: featureCollection.legend_entries || null,
              predominant_soil: featureCollection.predominant_soil || null,
              rendered_count: this.renderStats[layerId] || 0,
            });
          });
      });

      if (!vegetation.tree_instances_url && !vegetation.shrub_points_url && !vegetation.chunks_url) {
        const nextState = resolveDeferredLayerState(scenePayload, 'vegetation', {
          layerBlock: vegetation,
          layerLabel: 'Vegetation',
        });
        callbacks.onLayerState?.('vegetation', nextState.status, { message: nextState.message });
        debugLog('viewer-vegetation-waiting', {
          workspace_id: scenePayload?.workspace_id || null,
        });
        return;
      }
      reportLoading('vegetation');

      if (!shouldLoadFullVegetation && vegetation.chunks_url) {
        vegetationChunkManifestPromise
          .then(async ({ error, payload: chunkManifest }) => {
            if (error) {
              reportError('vegetation', error);
              return;
            }
            if (loadToken !== this.loadToken) {
              return;
            }
            this.setVegetationChunkManifest(chunkManifest);
            await this.updateVegetationChunks({ force: true, sceneLoadToken: loadToken, waitForPending: true });
            reportSuccess('vegetation', {
              count:
                Number(chunkManifest?.trees?.total_count || 0) + Number(chunkManifest?.shrubs?.total_count || 0),
              rendered_count: (this.renderStats.trees || 0) + (this.renderStats.shrubs || 0),
              trees: this.renderStats.trees || 0,
              shrubs: this.renderStats.shrubs || 0,
            });
            debugLog('viewer-vegetation-ready', {
              mode: 'chunked',
              rendered_shrubs: this.renderStats.shrubs || 0,
              rendered_trees: this.renderStats.trees || 0,
              workspace_id: scenePayload?.workspace_id || null,
            });
          });
        return;
      }

      Promise.all([
        treeInstancesPromise,
        shrubPointsPromise,
      ])
        .then(([treeInstancesResult, shrubPointsResult]) => {
          if (treeInstancesResult.error) {
            throw treeInstancesResult.error;
          }
          if (shrubPointsResult.error) {
            throw shrubPointsResult.error;
          }
          if (loadToken !== this.loadToken) {
            return;
          }
          const treeInstances = treeInstancesResult.payload;
          const shrubPoints = shrubPointsResult.payload;
          this.vegetationData = {
            treeInstances,
            shrubPoints,
          };
          this.rerenderVegetation();
          reportSuccess('vegetation', {
            count: getVegetationItemCount(treeInstances) + getVegetationItemCount(shrubPoints),
            rendered_count: (this.renderStats.trees || 0) + (this.renderStats.shrubs || 0),
            trees: this.renderStats.trees || 0,
            shrubs: this.renderStats.shrubs || 0,
          });
          debugLog('viewer-vegetation-ready', {
            mode: 'full-payload',
            rendered_shrubs: this.renderStats.shrubs || 0,
            rendered_trees: this.renderStats.trees || 0,
            workspace_id: scenePayload?.workspace_id || null,
          });
        })
        .catch((error) => reportError('vegetation', error));
    }

    addAoiBoundary(featureCollection, grid) {
      if (!featureCollection.features.length) {
        return;
      }

      const feature = featureCollection.features[0];
      const polygon = feature.geometry.type === 'Polygon'
        ? feature.geometry.coordinates
        : feature.geometry.coordinates[0];

      if (!Array.isArray(polygon) || !polygon.length) {
        return;
      }

      const outerRing = polygon[0];
      const drapedRing = this.densifyRingForTerrain(outerRing, 4);
      const points = drapedRing.map(([x, y]) => {
        const terrainY = VEILTerrain.sampleTerrainHeightAtLocal(grid, x, y) + 0.18;
        return new THREE.Vector3(x, terrainY, -y);
      });
      const lineGeometry = new THREE.BufferGeometry().setFromPoints(points);
      const lineMaterial = new THREE.LineBasicMaterial({
        color: 0xeab464,
        depthTest: false,
        opacity: 0.98,
        transparent: true,
        linewidth: 2,
      });
      this.aoiBoundary = new THREE.LineLoop(lineGeometry, lineMaterial);
      this.aoiBoundary.renderOrder = 12;
      this.aoiBoundary.frustumCulled = false;
      this.scene.add(this.aoiBoundary);
    }

    addParcelOutlines(featureCollection, grid) {
      if (!featureCollection.features.length) {
        return;
      }

      const group = new THREE.Group();
      let renderedCount = 0;

      featureCollection.features.forEach((feature) => {
        const coords =
          feature.geometry.type === 'Polygon'
            ? [feature.geometry.coordinates]
            : feature.geometry.coordinates;

        coords.forEach((polygon) => {
          const ring = this.densifyRingForTerrain(polygon[0], 6);
          const points = ring.map(([x, y]) => {
            const terrainY = VEILTerrain.sampleTerrainHeightAtLocal(grid, x, y) + 0.12;
            return new THREE.Vector3(x, terrainY, -y);
          });
          const geometry = new THREE.BufferGeometry().setFromPoints(points);
          const material = new THREE.LineBasicMaterial({
            color: feature.properties?.in_aoi ? 0xeab464 : 0xfcfaf7,
            depthTest: false,
            opacity: feature.properties?.in_aoi ? 0.94 : 0.42,
            transparent: true,
          });
          group.add(new THREE.LineLoop(geometry, material));
          renderedCount += 1;
        });
      });

      this.parcelGroup = group;
      this.parcelGroup.visible = this.isLayerVisible('parcels');
      this.renderStats.parcels = renderedCount;
      this.scene.add(group);
    }

    densifyRingForTerrain(ring, maxSegmentLength) {
      if (!Array.isArray(ring) || ring.length < 2) {
        return Array.isArray(ring) ? ring : [];
      }

      const densified = [];

      for (let index = 0; index < ring.length - 1; index += 1) {
        const start = ring[index];
        const end = ring[index + 1];
        const dx = end[0] - start[0];
        const dy = end[1] - start[1];
        const distance = Math.hypot(dx, dy);
        const segments = Math.max(1, Math.ceil(distance / maxSegmentLength));

        for (let step = 0; step < segments; step += 1) {
          const t = step / segments;
          densified.push([start[0] + dx * t, start[1] + dy * t]);
        }
      }

      densified.push(ring[ring.length - 1]);
      return densified;
    }

    async setTerrainRenderMode(mode) {
      if (this.destroyed) {
        return false;
      }
      if (!this.terrainMesh || !this.scenePayload) {
        return false;
      }

      if (mode === 'elevation') {
        this.disposeTerrainDrapeOverlay();
        this.disposeActiveTerrainMaterial();
        this.terrainMesh.material = this.elevationMaterial;
        this.activeTerrainMode = mode;
        return true;
      }

      const imagery = this.scenePayload.imagery || {};
      const urlMap = {
        false_color: imagery.false_color_url,
        hillshade: imagery.hillshade_url,
        ortho: imagery.drape_url,
      };
      const textureUrl = urlMap[mode];

      if (!textureUrl) {
        return false;
      }

      if (mode === 'ortho' && imagery.hillshade_url) {
        // Keep hillshade as the base terrain material and float the ortho drape above it.
        const [hillshadeTexture, orthoTexture] = await Promise.all([
          this.loadTerrainTexture(imagery.hillshade_url),
          this.loadTerrainTexture(imagery.drape_url),
        ]);
        if (this.destroyed || !hillshadeTexture || !orthoTexture) {
          return false;
        }

        this.disposeTerrainDrapeOverlay();
        this.disposeActiveTerrainMaterial();
        this.terrainMesh.material = new THREE.MeshStandardMaterial({
          map: hillshadeTexture,
          metalness: 0.06,
          roughness: 0.9,
        });

        const drapeMaterial = new THREE.MeshBasicMaterial({
          depthWrite: false,
          map: orthoTexture,
          opacity: 0.85,
          polygonOffset: true,
          polygonOffsetFactor: -1,
          polygonOffsetUnits: -1,
          transparent: true,
        });
        this.terrainDrapeOverlayMesh = new THREE.Mesh(this.terrainMesh.geometry, drapeMaterial);
        this.terrainDrapeOverlayMesh.renderOrder = (this.terrainMesh.renderOrder || 0) + 1;
        this.scene.add(this.terrainDrapeOverlayMesh);
        this.activeTerrainMode = mode;
        return true;
      }

      const texture = await this.loadTerrainTexture(textureUrl);
      if (this.destroyed || !texture) {
        return false;
      }

      this.disposeTerrainDrapeOverlay();
      this.disposeActiveTerrainMaterial();
      this.terrainMesh.material = new THREE.MeshStandardMaterial({
        map: texture,
        metalness: 0.06,
        roughness: 0.9,
      });
      this.activeTerrainMode = mode;
      return true;
    }

    setCameraFromGrid(grid) {
      if (this.destroyed) {
        return;
      }
      VEILCamera.frameGrid(this.camera, this.controls, grid);
    }

    disposeGridHelper() {
      if (!this.gridHelper) {
        return;
      }

      this.scene.remove(this.gridHelper);
      this.gridHelper.geometry.dispose();
      if (Array.isArray(this.gridHelper.material)) {
        this.gridHelper.material.forEach((material) => material.dispose());
      } else {
        this.gridHelper.material.dispose();
      }
      this.gridHelper = null;
    }

    updateGridHelper(grid) {
      if (!this.gridHelper || !grid) {
        return;
      }

      const width = Math.max(1, grid.maxX - grid.minX);
      const height = Math.max(1, grid.maxY - grid.minY);
      const span = Math.max(width, height);
      const helperSize = Math.max(
        GRID_HELPER_CONFIG.minSizeMeters,
        Math.ceil((span * GRID_HELPER_CONFIG.sizePaddingMultiplier) / GRID_HELPER_CONFIG.sizeSnapIncrementMeters) *
          GRID_HELPER_CONFIG.sizeSnapIncrementMeters
      );
      const halfSize = helperSize / 2;
      const centerX = (grid.minX + grid.maxX) * 0.5;
      const centerY = (grid.minY + grid.maxY) * 0.5;

      this.disposeGridHelper();
      this.gridHelper = new THREE.GridHelper(
        helperSize,
        GRID_HELPER_CONFIG.divisions,
        GRID_HELPER_CONFIG.majorColor,
        GRID_HELPER_CONFIG.minorColor
      );
      this.gridHelper.position.set(centerX, 0, -centerY);
      this.scene.add(this.gridHelper);

      this.overlayClipBounds = {
        minX: centerX - halfSize,
        minY: centerY - halfSize,
        maxX: centerX + halfSize,
        maxY: centerY + halfSize,
      };
    }

    clearScene() {
      this.overlayRenderer?.clear?.();
      this.vegetationRenderer?.clear?.();
      this.disposeTerrainDrapeOverlay();

      if (this.terrainMesh) {
        this.scene.remove(this.terrainMesh);
        this.terrainMesh.geometry.dispose();
        this.disposeActiveTerrainMaterial();
        this.terrainMesh = null;
      }
      if (this.elevationMaterial) {
        this.elevationMaterial.dispose();
      }
      this.disposeTextureCache();

      if (this.aoiBoundary) {
        this.scene.remove(this.aoiBoundary);
        this.aoiBoundary.geometry.dispose();
        this.aoiBoundary.material.dispose();
        this.aoiBoundary = null;
      }

      if (this.parcelGroup) {
        this.scene.remove(this.parcelGroup);
        this.parcelGroup.traverse((child) => {
          if (child.geometry) {
            child.geometry.dispose();
          }
          if (child.material) {
            child.material.dispose();
          }
        });
        this.parcelGroup = null;
      }

      this.scenePayload = null;
      this.terrainGrid = null;
      this.elevationMaterial = null;
      this.activeTerrainMode = 'elevation';
      this.renderStats = {
        aoi: 0,
        buildings: 0,
        hydrology: 0,
        parcels: 0,
        roads: 0,
        shrubs: 0,
        soils: 0,
        trails: 0,
        trees: 0,
      };
      this.resetLoadedLayerState();
    }

    setVegetationDensity(kind, density) {
      if (this.destroyed) {
        return;
      }
      this.vegetationRenderer.setDensity(kind, density);
    }

    isLayerVisible(layerId) {
      return this.layerVisibility.get(layerId) !== false;
    }

    setLayerVisibility(layerId, visible) {
      if (this.destroyed) {
        return;
      }
      this.layerVisibility.set(layerId, Boolean(visible));
      if (layerId === 'vegetation') {
        this.vegetationRenderer.setVisible(visible);
        return;
      }
      if (layerId === 'buildings' && this.buildingModelsGroup) {
        this.buildingModelsGroup.visible = visible;
      }
      if (layerId === 'parcels') {
        if (this.parcelGroup) {
          this.parcelGroup.visible = visible;
        }
        return;
      }
      this.overlayRenderer.setLayerVisible(layerId, visible);
    }

    resize() {
      if (this.destroyed) {
        return;
      }
      const width = Math.max(1, this.rootEl.clientWidth);
      const height = Math.max(1, this.rootEl.clientHeight);
      this.camera.aspect = width / height;
      this.camera.updateProjectionMatrix();
      this.renderer.setSize(width, height, false);
    }

    animate() {
      if (this.destroyed) {
        this.animationFrame = null;
        return;
      }
      this.animationFrame = window.requestAnimationFrame(this.animate);
      const now = performance.now();
      const deltaSeconds = Math.min(0.05, Math.max(0, (now - this.lastFrameTime) / 1000));
      this.lastFrameTime = now;
      VEILCamera.applyKeyboardPan(this.camera, this.controls, this.keyboardPanState, deltaSeconds);
      if (this.vegetationChunkState.manifest && now - this.lastVegetationChunkSyncAt >= 250) {
        this.lastVegetationChunkSyncAt = now;
        this.updateVegetationChunks().catch(() => {});
      }
      this.overlayRenderer.tick(deltaSeconds);
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
    }

    destroy() {
      if (this.destroyed) {
        return;
      }

      this.destroyed = true;
      this.loadToken += 1;
      this.abortPendingFetches();
      if (this.animationFrame) {
        window.cancelAnimationFrame(this.animationFrame);
        this.animationFrame = null;
      }
      window.removeEventListener('resize', this.resize);
      this.unbindKeyboardPan?.();
      this.unbindKeyboardPan = null;
      this.clearScene();
      this.overlayRenderer?.dispose?.();
      this.vegetationRenderer?.dispose?.();
      this.disposeGridHelper();
      this.controls?.dispose?.();
      this.renderer?.renderLists?.dispose?.();
      this.renderer?.dispose?.();
      this.renderer?.forceContextLoss?.();
      this.rootEl.replaceChildren();
    }

    getRenderStats() {
      return { ...this.renderStats };
    }
  }

  global.VEILViewer = {
    create(rootEl) {
      return new WorkspaceViewer(rootEl);
    },
  };
})(window);
