(function attachOverlayHelpers(global) {
  const { THREE, VEILTerrain } = global;
  const HYDROLOGY_RENDER_LIMITS = {
    maxFillGridCells: 12000,
  };
  const LOCAL_GEOMETRY_SANITY_LIMITS = {
    maxCoordinateAbsMeters: 1000000,
    maxSegmentLengthMeters: 20000,
  };
  const FLOW_TEXTURE_CONFIG = {
    heightPx: 8,
    repeatX: 6,
    widthPx: 128,
  };
  const OVERLAY_DENSIFY_STEP_M = {
    flowline: 5,
    polygonOutline: 6,
    sampledSurfaceRing: 8,
  };

  function debugLog(event, payload = {}) {
    global.__VEIL_DEBUG__?.log?.(event, payload);
  }

  function toClipBounds(boundsOrGrid) {
    if (!boundsOrGrid) {
      return null;
    }
    return {
      minX: boundsOrGrid.minX,
      minY: boundsOrGrid.minY,
      maxX: boundsOrGrid.maxX,
      maxY: boundsOrGrid.maxY,
    };
  }

  function isWithinGridBounds(boundsOrGrid, x, y) {
    const bounds = toClipBounds(boundsOrGrid);
    return (
      bounds &&
      Number.isFinite(x) &&
      Number.isFinite(y) &&
      x >= bounds.minX &&
      x <= bounds.maxX &&
      y >= bounds.minY &&
      y <= bounds.maxY
    );
  }

  function createGridClipPlanes(boundsOrGrid) {
    const bounds = toClipBounds(boundsOrGrid);
    if (!bounds) {
      return [];
    }
    return [
      new THREE.Plane(new THREE.Vector3(1, 0, 0), -bounds.minX),
      new THREE.Plane(new THREE.Vector3(-1, 0, 0), bounds.maxX),
      new THREE.Plane(new THREE.Vector3(0, 0, 1), bounds.maxY),
      new THREE.Plane(new THREE.Vector3(0, 0, -1), -bounds.minY),
    ];
  }

  function createFlowTexture() {
    const canvas = document.createElement('canvas');
    canvas.width = FLOW_TEXTURE_CONFIG.widthPx;
    canvas.height = FLOW_TEXTURE_CONFIG.heightPx;
    const context = canvas.getContext('2d');
    const gradient = context.createLinearGradient(0, 0, canvas.width, 0);
    gradient.addColorStop(0, 'rgba(255,255,255,0)');
    gradient.addColorStop(0.2, 'rgba(255,255,255,0.55)');
    gradient.addColorStop(0.5, 'rgba(255,255,255,0.1)');
    gradient.addColorStop(0.8, 'rgba(255,255,255,0.55)');
    gradient.addColorStop(1, 'rgba(255,255,255,0)');
    context.fillStyle = gradient;
    context.fillRect(0, 0, canvas.width, canvas.height);
    const texture = new THREE.CanvasTexture(canvas);
    texture.wrapS = THREE.RepeatWrapping;
    texture.wrapT = THREE.RepeatWrapping;
    texture.repeat.set(FLOW_TEXTURE_CONFIG.repeatX, 1);
    return texture;
  }

  function isRenderableLocalPoint(point) {
    return (
      Array.isArray(point) &&
      point.length >= 2 &&
      Number.isFinite(point[0]) &&
      Number.isFinite(point[1]) &&
      Math.abs(point[0]) <= LOCAL_GEOMETRY_SANITY_LIMITS.maxCoordinateAbsMeters &&
      Math.abs(point[1]) <= LOCAL_GEOMETRY_SANITY_LIMITS.maxCoordinateAbsMeters
    );
  }

  function sanitizeLineLikePoints(points, label = 'line') {
    if (!Array.isArray(points) || points.length < 2) {
      return [];
    }
    const cleaned = [];
    for (let index = 0; index < points.length; index += 1) {
      const point = points[index];
      if (!isRenderableLocalPoint(point)) {
        debugLog('overlay-invalid-point', { label, point });
        return [];
      }
      if (cleaned.length) {
        const previous = cleaned[cleaned.length - 1];
        const distance = Math.hypot(point[0] - previous[0], point[1] - previous[1]);
        if (
          !Number.isFinite(distance) ||
          distance > LOCAL_GEOMETRY_SANITY_LIMITS.maxSegmentLengthMeters
        ) {
          debugLog('overlay-invalid-segment', {
            distance,
            from: previous,
            label,
            to: point,
          });
          return [];
        }
      }
      cleaned.push([point[0], point[1]]);
    }
    return cleaned;
  }

  function densifyLine(points, maxSegmentLength) {
    const sanitized = sanitizeLineLikePoints(points, 'densify');
    if (sanitized.length < 2) {
      return sanitized;
    }

    const densified = [];
    for (let index = 0; index < sanitized.length - 1; index += 1) {
      const start = sanitized[index];
      const end = sanitized[index + 1];
      const dx = end[0] - start[0];
      const dy = end[1] - start[1];
      const distance = Math.hypot(dx, dy);
      const segments = Math.max(1, Math.ceil(distance / maxSegmentLength));
      for (let step = 0; step < segments; step += 1) {
        const t = step / segments;
        densified.push([start[0] + dx * t, start[1] + dy * t]);
      }
    }
    densified.push(sanitized[sanitized.length - 1]);
    return densified;
  }

  function buildRibbonGeometry(points, grid, width, zOffset) {
    if (!Array.isArray(points) || points.length < 2) {
      return null;
    }

    const vertices = [];
    const uvs = [];
    const indices = [];
    let distanceCursor = 0;

    for (let index = 0; index < points.length; index += 1) {
      const current = points[index];
      const prev = points[Math.max(0, index - 1)];
      const next = points[Math.min(points.length - 1, index + 1)];
      const directionX = next[0] - prev[0];
      const directionY = next[1] - prev[1];
      const length = Math.max(1e-6, Math.hypot(directionX, directionY));
      const normalX = -directionY / length;
      const normalY = directionX / length;
      if (index > 0) {
        distanceCursor += Math.hypot(current[0] - points[index - 1][0], current[1] - points[index - 1][1]);
      }

      const leftLocalX = current[0] + normalX * width * 0.5;
      const leftLocalY = current[1] + normalY * width * 0.5;
      const rightLocalX = current[0] - normalX * width * 0.5;
      const rightLocalY = current[1] - normalY * width * 0.5;
      const leftTerrainY = VEILTerrain.sampleTerrainHeightAtLocal(grid, leftLocalX, leftLocalY) + zOffset;
      const rightTerrainY = VEILTerrain.sampleTerrainHeightAtLocal(grid, rightLocalX, rightLocalY) + zOffset;

      vertices.push(
        leftLocalX,
        leftTerrainY,
        -leftLocalY,
        rightLocalX,
        rightTerrainY,
        -rightLocalY
      );
      uvs.push(distanceCursor / Math.max(width, 1), 0, distanceCursor / Math.max(width, 1), 1);

      if (index < points.length - 1) {
        const base = index * 2;
        indices.push(base, base + 1, base + 2, base + 2, base + 1, base + 3);
      }
    }

    const geometry = new THREE.BufferGeometry();
    geometry.setIndex(indices);
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(vertices, 3));
    geometry.setAttribute('uv', new THREE.Float32BufferAttribute(uvs, 2));
    geometry.computeVertexNormals();
    return geometry;
  }

  function clipSegmentToBounds(start, end, boundsOrGrid) {
    const bounds = toClipBounds(boundsOrGrid);
    if (!bounds) {
      return null;
    }

    const dx = end[0] - start[0];
    const dy = end[1] - start[1];
    const p = [-dx, dx, -dy, dy];
    const q = [
      start[0] - bounds.minX,
      bounds.maxX - start[0],
      start[1] - bounds.minY,
      bounds.maxY - start[1],
    ];
    let t0 = 0;
    let t1 = 1;

    for (let index = 0; index < 4; index += 1) {
      if (Math.abs(p[index]) < 1e-9) {
        if (q[index] < 0) {
          return null;
        }
        continue;
      }
      const ratio = q[index] / p[index];
      if (p[index] < 0) {
        t0 = Math.max(t0, ratio);
      } else {
        t1 = Math.min(t1, ratio);
      }
      if (t0 > t1) {
        return null;
      }
    }

    return [
      [start[0] + dx * t0, start[1] + dy * t0],
      [start[0] + dx * t1, start[1] + dy * t1],
    ];
  }

  function clipDensifiedLineToGrid(points, boundsOrGrid) {
    const segments = [];
    let current = null;

    for (let index = 0; index < (points || []).length - 1; index += 1) {
      const clipped = clipSegmentToBounds(points[index], points[index + 1], boundsOrGrid);
      if (!clipped) {
        if (current && current.length >= 2) {
          segments.push(current);
        }
        current = null;
        continue;
      }

      const [clippedStart, clippedEnd] = clipped;
      if (!current) {
        current = [clippedStart, clippedEnd];
        continue;
      }

      const previous = current[current.length - 1];
      if (
        Math.abs(previous[0] - clippedStart[0]) <= 1e-6 &&
        Math.abs(previous[1] - clippedStart[1]) <= 1e-6
      ) {
        current.push(clippedEnd);
      } else {
        if (current.length >= 2) {
          segments.push(current);
        }
        current = [clippedStart, clippedEnd];
      }
    }

    if (current && current.length >= 2) {
      segments.push(current);
    }

    return segments;
  }

  function closeRing(points) {
    if (!Array.isArray(points) || !points.length) {
      return [];
    }
    const closed = points.slice();
    const first = closed[0];
    const last = closed[closed.length - 1];
    if (Math.abs(first[0] - last[0]) > 1e-6 || Math.abs(first[1] - last[1]) > 1e-6) {
      closed.push([first[0], first[1]]);
    }
    return closed;
  }

  function clipRingToBounds(ring, boundsOrGrid) {
    const bounds = toClipBounds(boundsOrGrid);
    if (!bounds || !Array.isArray(ring) || ring.length < 4) {
      return [];
    }

    const openRing = ring.slice(0, -1);
    const edges = [
      {
        inside: ([x]) => x >= bounds.minX,
        intersect: (start, end) => {
          const t = (bounds.minX - start[0]) / Math.max(1e-12, end[0] - start[0]);
          return [bounds.minX, start[1] + (end[1] - start[1]) * t];
        },
      },
      {
        inside: ([x]) => x <= bounds.maxX,
        intersect: (start, end) => {
          const t = (bounds.maxX - start[0]) / Math.max(1e-12, end[0] - start[0]);
          return [bounds.maxX, start[1] + (end[1] - start[1]) * t];
        },
      },
      {
        inside: ([, y]) => y >= bounds.minY,
        intersect: (start, end) => {
          const t = (bounds.minY - start[1]) / Math.max(1e-12, end[1] - start[1]);
          return [start[0] + (end[0] - start[0]) * t, bounds.minY];
        },
      },
      {
        inside: ([, y]) => y <= bounds.maxY,
        intersect: (start, end) => {
          const t = (bounds.maxY - start[1]) / Math.max(1e-12, end[1] - start[1]);
          return [start[0] + (end[0] - start[0]) * t, bounds.maxY];
        },
      },
    ];

    let output = openRing.slice();
    edges.forEach(({ inside, intersect }) => {
      const input = output.slice();
      output = [];
      if (!input.length) {
        return;
      }

      let previous = input[input.length - 1];
      for (let index = 0; index < input.length; index += 1) {
        const current = input[index];
        const currentInside = inside(current);
        const previousInside = inside(previous);

        if (currentInside) {
          if (!previousInside) {
            output.push(intersect(previous, current));
          }
          output.push(current);
        } else if (previousInside) {
          output.push(intersect(previous, current));
        }

        previous = current;
      }
    });

    if (output.length < 3) {
      return [];
    }

    return closeRing(output);
  }

  function collectPolylines(featureCollection) {
    const polylines = [];

    (featureCollection?.features || []).forEach((feature) => {
      const geometry = feature.geometry;
      if (!geometry) {
        return;
      }
      if (geometry.type === 'LineString') {
        const sanitized = sanitizeLineLikePoints(geometry.coordinates, 'polyline');
        if (sanitized.length >= 2) {
          polylines.push(sanitized);
        }
      } else if (geometry.type === 'MultiLineString') {
        geometry.coordinates.forEach((line) => {
          const sanitized = sanitizeLineLikePoints(line, 'multiline');
          if (sanitized.length >= 2) {
            polylines.push(sanitized);
          }
        });
      }
    });

    return polylines;
  }

  function collectPolygons(featureCollection) {
    const polygons = [];

    (featureCollection?.features || []).forEach((feature) => {
      const geometry = feature.geometry;
      if (!geometry) {
        return;
      }
      if (geometry.type === 'Polygon') {
        const sanitized = (geometry.coordinates || [])
          .map((ring) => sanitizeLineLikePoints(ring, 'polygon-ring'))
          .filter((ring) => ring.length >= 4);
        if (sanitized.length) {
          polygons.push(sanitized);
        }
      } else if (geometry.type === 'MultiPolygon') {
        geometry.coordinates.forEach((polygon) => {
          const sanitized = (polygon || [])
            .map((ring) => sanitizeLineLikePoints(ring, 'multipolygon-ring'))
            .filter((ring) => ring.length >= 4);
          if (sanitized.length) {
            polygons.push(sanitized);
          }
        });
      }
    });

    return polygons;
  }

  function pointInRing(point, ring) {
    let inside = false;
    for (let index = 0, prev = ring.length - 1; index < ring.length; prev = index, index += 1) {
      const xi = ring[index][0];
      const yi = ring[index][1];
      const xj = ring[prev][0];
      const yj = ring[prev][1];
      const intersects =
        yi > point[1] !== yj > point[1] &&
        point[0] < ((xj - xi) * (point[1] - yi)) / Math.max(1e-12, yj - yi) + xi;
      if (intersects) {
        inside = !inside;
      }
    }
    return inside;
  }

  function pointInPolygon(point, polygonRings) {
    if (!polygonRings.length || !pointInRing(point, polygonRings[0])) {
      return false;
    }
    for (let holeIndex = 1; holeIndex < polygonRings.length; holeIndex += 1) {
      if (pointInRing(point, polygonRings[holeIndex])) {
        return false;
      }
    }
    return true;
  }

  function buildHydrologySurfaceGeometry(polygonRings, grid, clipBounds, yOffset) {
    if (!grid || !polygonRings?.length) {
      return null;
    }

    const xStep =
      grid.width > 1 ? (grid.maxX - grid.minX) / (grid.width - 1) : grid.xStep || 1;
    const yStep =
      grid.height > 1 ? (grid.maxY - grid.minY) / (grid.height - 1) : grid.yStep || 1;
    const indexAt = (column, row) => row * grid.width + column;
    const positions = [];

    function pushVertex(column, row) {
      const index = indexAt(column, row);
      const elevation = grid.heights[index];
      if (!Number.isFinite(elevation)) {
        return false;
      }
      const localX = grid.minX + column * xStep;
      const localY = grid.maxY - row * yStep;
      positions.push(localX, elevation - grid.minElevation + yOffset, -localY);
      return true;
    }

    for (let row = 0; row < grid.height - 1; row += 1) {
      for (let column = 0; column < grid.width - 1; column += 1) {
        const centerX = grid.minX + (column + 0.5) * xStep;
        const centerY = grid.maxY - (row + 0.5) * yStep;

        if (!isWithinGridBounds(clipBounds, centerX, centerY) || !pointInPolygon([centerX, centerY], polygonRings)) {
          continue;
        }

        const baseLength = positions.length;
        const corners = [
          [column, row],
          [column, row + 1],
          [column + 1, row],
          [column + 1, row],
          [column, row + 1],
          [column + 1, row + 1],
        ];
        let validCell = true;
        corners.forEach(([cornerColumn, cornerRow]) => {
          if (!pushVertex(cornerColumn, cornerRow)) {
            validCell = false;
          }
        });
        if (!validCell) {
          positions.length = baseLength;
        }
      }
    }

    if (!positions.length) {
      return null;
    }

    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
    geometry.computeVertexNormals();
    return geometry;
  }

  function buildSampledSurfaceGeometry(polygonRings, grid, clipBounds, yOffset) {
    if (!grid || !polygonRings?.length) {
      return null;
    }

    function normalizeRing(ring) {
      if (!Array.isArray(ring) || ring.length < 4) {
        return [];
      }
      const dense = densifyLine(ring, OVERLAY_DENSIFY_STEP_M.sampledSurfaceRing);
      const normalized = dense.map(([x, y]) => new THREE.Vector2(x, y));
      if (
        normalized.length > 1 &&
        Math.abs(normalized[0].x - normalized[normalized.length - 1].x) <= 1e-6 &&
        Math.abs(normalized[0].y - normalized[normalized.length - 1].y) <= 1e-6
      ) {
        normalized.pop();
      }
      return normalized;
    }

    const clippedOuter = clipRingToBounds(polygonRings[0], clipBounds);
    const outer = normalizeRing(clippedOuter);
    if (outer.length < 3) {
      return null;
    }

    if (!THREE.ShapeUtils.isClockWise(outer)) {
      outer.reverse();
    }

    const shape = new THREE.Shape(outer);
    for (let holeIndex = 1; holeIndex < polygonRings.length; holeIndex += 1) {
      const clippedHole = clipRingToBounds(polygonRings[holeIndex], clipBounds);
      const hole = normalizeRing(clippedHole);
      if (hole.length < 3) {
        continue;
      }
      if (THREE.ShapeUtils.isClockWise(hole)) {
        hole.reverse();
      }
      shape.holes.push(new THREE.Path(hole));
    }

    const geometry = new THREE.ShapeGeometry(shape);
    const positions = geometry.getAttribute('position');
    for (let index = 0; index < positions.count; index += 1) {
      const localX = positions.getX(index);
      const localY = positions.getY(index);
      const elevation = VEILTerrain.sampleTerrainHeightAtLocal(grid, localX, localY) + yOffset;
      positions.setXYZ(index, localX, elevation, -localY);
    }

    positions.needsUpdate = true;
    geometry.computeVertexNormals();
    return geometry;
  }

  function polygonBounds(polygonRings) {
    let minX = Number.POSITIVE_INFINITY;
    let minY = Number.POSITIVE_INFINITY;
    let maxX = Number.NEGATIVE_INFINITY;
    let maxY = Number.NEGATIVE_INFINITY;
    (polygonRings || []).forEach((ring) => {
      (ring || []).forEach(([x, y]) => {
        minX = Math.min(minX, x);
        minY = Math.min(minY, y);
        maxX = Math.max(maxX, x);
        maxY = Math.max(maxY, y);
      });
    });
    return Number.isFinite(minX) ? { minX, minY, maxX, maxY } : null;
  }

  function estimatePolygonGridCells(polygonRings, grid, clipBounds) {
    const bounds = polygonBounds(polygonRings);
    if (!bounds || !grid) {
      return 0;
    }
    const clipped = clipBounds
      ? {
          minX: Math.max(bounds.minX, clipBounds.minX),
          minY: Math.max(bounds.minY, clipBounds.minY),
          maxX: Math.min(bounds.maxX, clipBounds.maxX),
          maxY: Math.min(bounds.maxY, clipBounds.maxY),
        }
      : bounds;
    const width = Math.max(0, clipped.maxX - clipped.minX);
    const height = Math.max(0, clipped.maxY - clipped.minY);
    const xStep =
      Number(grid.xStep) > 0 ? Number(grid.xStep) : grid.width > 1 ? (grid.maxX - grid.minX) / (grid.width - 1) : 1;
    const yStep =
      Number(grid.yStep) > 0 ? Number(grid.yStep) : grid.height > 1 ? (grid.maxY - grid.minY) / (grid.height - 1) : 1;
    return Math.ceil(width / Math.max(0.01, xStep)) * Math.ceil(height / Math.max(0.01, yStep));
  }

  class OverlayRenderer {
    constructor(scene) {
      this.scene = scene;
      this.group = new THREE.Group();
      this.scene.add(this.group);
      this.disposed = false;
      this.layers = {
        buildings: new THREE.Group(),
        hydrology: new THREE.Group(),
        roads: new THREE.Group(),
        soils: new THREE.Group(),
        trails: new THREE.Group(),
      };
      this.group.add(
        this.layers.buildings,
        this.layers.hydrology,
        this.layers.roads,
        this.layers.soils,
        this.layers.trails
      );
      this.flowTexture = createFlowTexture();
      this.flowMaterials = [];
      this.buildingLinesLocal = [];
      this.buildingPolygonsLocal = [];
      this.hydrologyLinesLocal = [];
      this.roadLinesLocal = [];
      this.gridClipPlanes = [];
      this.renderStats = {
        buildings: 0,
        hydrology: 0,
        roads: 0,
        soils: 0,
        trails: 0,
      };
    }

    clearGroup(group) {
      if (!group) {
        return;
      }
      group.traverse((child) => {
        if (child.geometry) {
          child.geometry.dispose();
        }
        if (child.material) {
          if (Array.isArray(child.material)) {
            child.material.forEach((material) => material.dispose());
          } else if (child.material !== this.flowTexture) {
            child.material.dispose();
          }
        }
      });
      group.clear();
    }

    clear() {
      this.flowMaterials = [];
      this.buildingLinesLocal = [];
      this.buildingPolygonsLocal = [];
      this.hydrologyLinesLocal = [];
      this.roadLinesLocal = [];
      this.renderStats = {
        buildings: 0,
        hydrology: 0,
        roads: 0,
        soils: 0,
        trails: 0,
      };
      this.clearGroup(this.layers.buildings);
      this.clearGroup(this.layers.hydrology);
      this.clearGroup(this.layers.roads);
      this.clearGroup(this.layers.soils);
      this.clearGroup(this.layers.trails);
    }

    setContext(grid, clipBounds) {
      this.grid = grid;
      this.clipBounds = toClipBounds(clipBounds || grid);
      this.gridClipPlanes = createGridClipPlanes(this.clipBounds);
    }

    clearLayer(layerId) {
      if (!this.layers[layerId]) {
        return;
      }
      this.clearGroup(this.layers[layerId]);
      if (layerId === 'buildings') {
        this.buildingLinesLocal = [];
        this.buildingPolygonsLocal = [];
        this.renderStats.buildings = 0;
        return;
      }
      if (layerId === 'hydrology') {
        this.hydrologyLinesLocal = [];
        this.renderStats.hydrology = 0;
        return;
      }
      if (layerId === 'roads') {
        this.roadLinesLocal = [];
        this.renderStats.roads = 0;
        return;
      }
      if (layerId === 'soils') {
        this.renderStats.soils = 0;
        return;
      }
      if (layerId === 'trails') {
        this.renderStats.trails = 0;
      }
    }

    loadLayer(layerId, featureCollection, grid = this.grid, clipBounds = this.clipBounds) {
      if (this.disposed) {
        return;
      }
      if (!this.layers[layerId]) {
        return;
      }
      this.setContext(grid, clipBounds);
      this.clearLayer(layerId);
      if (!featureCollection) {
        return;
      }
      if (layerId === 'buildings') {
        this.renderPolygonOutlines(this.layers.buildings, featureCollection, this.grid, {
          color: 0xeab464,
          offset: 0.16,
          opacity: 0.82,
          target: 'buildings',
        });
      } else if (layerId === 'hydrology') {
        this.renderHydrology(featureCollection, this.grid);
      } else if (layerId === 'roads') {
        this.renderLines(this.layers.roads, featureCollection, this.grid, {
          color: 0xa7754d,
          offset: 0.14,
          width: 2.6,
        });
      } else if (layerId === 'soils') {
        this.renderSoils(featureCollection, this.grid);
      } else if (layerId === 'trails') {
        this.renderLines(this.layers.trails, featureCollection, this.grid, {
          color: 0xeab464,
          offset: 0.18,
        });
      }
      debugLog('overlay-layer-rendered', {
        layerId,
        rendered_count: this.renderStats[layerId] || 0,
      });
    }

    load({ buildings, hydrology, roads, soils, trails, grid, clipBounds }) {
      if (this.disposed) {
        return;
      }
      this.clear();
      this.setContext(grid, clipBounds);
      this.loadLayer('buildings', buildings, grid, clipBounds);
      this.loadLayer('hydrology', hydrology, grid, clipBounds);
      this.loadLayer('roads', roads, grid, clipBounds);
      this.loadLayer('soils', soils, grid, clipBounds);
      this.loadLayer('trails', trails, grid, clipBounds);
    }

    renderPolygonOutlines(group, featureCollection, grid, style) {
      if (!featureCollection) {
        return;
      }

      let renderedCount = 0;
      collectPolygons(featureCollection).forEach((polygon) => {
        this.buildingPolygonsLocal.push(polygon);
        const clippedSegments = clipDensifiedLineToGrid(
          densifyLine(polygon[0], OVERLAY_DENSIFY_STEP_M.polygonOutline),
          this.clipBounds
        );
        if (clippedSegments.length && style.target === 'buildings') {
          renderedCount += 1;
        }
        clippedSegments.forEach((segment) => {
          if (style.target === 'buildings') {
            this.buildingLinesLocal.push(segment);
          }
          const points = segment.map(([x, y]) => {
            const terrainY = VEILTerrain.sampleTerrainHeightAtLocal(grid, x, y) + style.offset;
            return new THREE.Vector3(x, terrainY, -y);
          });
          const geometry = new THREE.BufferGeometry().setFromPoints(points);
          const material = new THREE.LineBasicMaterial({
            clippingPlanes: this.gridClipPlanes,
            color: style.color,
            depthTest: false,
            transparent: true,
            opacity: style.opacity,
          });
          const outline = new THREE.Line(geometry, material);
          outline.renderOrder = 9;
          outline.frustumCulled = false;
          group.add(outline);
        });
      });
      if (style.target === 'buildings') {
        this.renderStats.buildings = renderedCount;
      }
    }

    renderHydrology(featureCollection, grid) {
      if (!featureCollection) {
        return;
      }

      let renderedCount = 0;
      const hydrologySegments = collectPolylines(featureCollection)
        .flatMap((line) =>
          clipDensifiedLineToGrid(densifyLine(line, OVERLAY_DENSIFY_STEP_M.flowline), this.clipBounds)
        );
      const hasFlowlines = hydrologySegments.length > 0;
      this.hydrologyLinesLocal = hydrologySegments;
      hydrologySegments.forEach((line) => {
        const points = line.map(([x, y]) => {
          const terrainY = VEILTerrain.sampleTerrainHeightAtLocal(grid, x, y) + 0.12;
          return new THREE.Vector3(x, terrainY, -y);
        });
        const geometry = new THREE.BufferGeometry().setFromPoints(points);
        const material = new THREE.LineBasicMaterial({
          clippingPlanes: this.gridClipPlanes,
          color: '#4fa3c7',
          depthTest: true,
          transparent: true,
          opacity: 0.92,
        });
        const polyline = new THREE.Line(geometry, material);
        polyline.renderOrder = 11;
        polyline.frustumCulled = false;
        renderedCount += 1;
        this.layers.hydrology.add(polyline);
      });

      collectPolygons(featureCollection).forEach((polygon) => {
        const ring = densifyLine(polygon[0], OVERLAY_DENSIFY_STEP_M.sampledSurfaceRing);
        const polyBounds = polygonBounds(polygon);
        const estimatedFillCells = estimatePolygonGridCells(polygon, grid, this.clipBounds);
        const clipWidth = this.clipBounds.maxX - this.clipBounds.minX;
        const clipHeight = this.clipBounds.maxY - this.clipBounds.minY;
        const polygonWidth = polyBounds ? polyBounds.maxX - polyBounds.minX : 0;
        const polygonHeight = polyBounds ? polyBounds.maxY - polyBounds.minY : 0;
        const isSmallWaterbody =
          polyBounds &&
          polygonWidth <= clipWidth * 1.35 &&
          polygonHeight <= clipHeight * 1.35;
        const isOversizedWaterbody =
          !isSmallWaterbody &&
          polyBounds &&
          (polygonWidth >= clipWidth * 0.85 || polygonHeight >= clipHeight * 0.85);

        const shouldRenderFill =
          !hasFlowlines &&
          !isOversizedWaterbody &&
          estimatedFillCells > 0 &&
          estimatedFillCells <= HYDROLOGY_RENDER_LIMITS.maxFillGridCells;
        const fillGeometry = shouldRenderFill
          ? buildHydrologySurfaceGeometry(
            polygon,
            grid,
            this.clipBounds,
            0.04
          )
          : null;
        debugLog('overlay-hydrology-polygon', {
          estimated_fill_cells: estimatedFillCells,
          has_flowlines: hasFlowlines,
          is_oversized: Boolean(isOversizedWaterbody),
          is_small: Boolean(isSmallWaterbody),
          rendered_fill: Boolean(fillGeometry),
        });
        if (fillGeometry) {
          renderedCount += 1;
          const fillMaterial = new THREE.MeshStandardMaterial({
            clippingPlanes: this.gridClipPlanes,
            color: '#5da9bf',
            depthWrite: false,
            transparent: true,
            opacity: isSmallWaterbody ? 0.55 : 0.38,
            roughness: 0.55,
            metalness: 0.01,
          });
          const fillMesh = new THREE.Mesh(fillGeometry, fillMaterial);
          fillMesh.renderOrder = 8;
          fillMesh.frustumCulled = false;
          this.layers.hydrology.add(fillMesh);
        }

        if (hasFlowlines && isOversizedWaterbody) {
          return;
        }

        clipDensifiedLineToGrid(ring, this.clipBounds).forEach((segment) => {
          renderedCount += 1;
          const points = segment.map(([x, y]) => {
            const terrainY = VEILTerrain.sampleTerrainHeightAtLocal(grid, x, y) + 0.08;
            return new THREE.Vector3(x, terrainY, -y);
          });
          const geometry = new THREE.BufferGeometry().setFromPoints(points);
          const material = new THREE.LineBasicMaterial({
            clippingPlanes: this.gridClipPlanes,
            color: '#5da9bf',
            depthTest: true,
            transparent: true,
            opacity: isSmallWaterbody ? 0.95 : 0.7,
          });
          const shoreline = new THREE.Line(geometry, material);
          shoreline.renderOrder = 10;
          shoreline.frustumCulled = false;
          this.layers.hydrology.add(shoreline);
        });
      });
      this.renderStats.hydrology = renderedCount;
    }

    renderSoils(featureCollection, grid) {
      if (!featureCollection) {
        return;
      }

      let renderedCount = 0;

      (featureCollection.features || []).forEach((feature) => {
        const color = feature.properties?.color || '#b8895a';
        collectPolygons({ type: 'FeatureCollection', features: [feature] }).forEach((polygon) => {
          const fillGeometry = buildSampledSurfaceGeometry(polygon, grid, this.clipBounds, 0.08);
          if (fillGeometry) {
            renderedCount += 1;
            const fillMaterial = new THREE.MeshBasicMaterial({
              clippingPlanes: this.gridClipPlanes,
              color,
              depthTest: false,
              depthWrite: false,
              polygonOffset: true,
              polygonOffsetFactor: -2,
              polygonOffsetUnits: -2,
              transparent: true,
              opacity: 0.46,
              side: THREE.DoubleSide,
            });
            const fillMesh = new THREE.Mesh(fillGeometry, fillMaterial);
            fillMesh.renderOrder = 10;
            fillMesh.frustumCulled = false;
            this.layers.soils.add(fillMesh);
          }

          clipDensifiedLineToGrid(
            densifyLine(polygon[0], OVERLAY_DENSIFY_STEP_M.sampledSurfaceRing),
            this.clipBounds
          ).forEach((segment) => {
            const points = segment.map(([x, y]) => {
              const terrainY = VEILTerrain.sampleTerrainHeightAtLocal(grid, x, y) + 0.06;
              return new THREE.Vector3(x, terrainY, -y);
            });
            const geometry = new THREE.BufferGeometry().setFromPoints(points);
            const material = new THREE.LineBasicMaterial({
              clippingPlanes: this.gridClipPlanes,
              color,
              depthTest: false,
              transparent: true,
              opacity: 0.7,
            });
            const outline = new THREE.Line(geometry, material);
            outline.renderOrder = 11;
            outline.frustumCulled = false;
            this.layers.soils.add(outline);
          });
        });
      });

      this.renderStats.soils = renderedCount;
    }

    renderLines(group, featureCollection, grid, style) {
      if (!featureCollection) {
        return;
      }

      let renderedCount = 0;
      collectPolylines(featureCollection).forEach((line) => {
        const clippedSegments = clipDensifiedLineToGrid(
          densifyLine(line, OVERLAY_DENSIFY_STEP_M.polygonOutline),
          this.clipBounds
        );
        if (group === this.layers.roads) {
          this.roadLinesLocal.push(...clippedSegments);
        }
        if (clippedSegments.length) {
          renderedCount += 1;
        }
        clippedSegments.forEach((segment) => {
          if (style.width) {
            const geometry = buildRibbonGeometry(segment, grid, style.width, style.offset);
            if (!geometry) {
              return;
            }
            const material = new THREE.MeshStandardMaterial({
              clippingPlanes: this.gridClipPlanes,
              color: style.color,
              depthWrite: false,
              transparent: true,
              opacity: 0.72,
              roughness: 0.6,
              metalness: 0.02,
            });
            const mesh = new THREE.Mesh(geometry, material);
            mesh.renderOrder = 7;
            mesh.frustumCulled = false;
            group.add(mesh);
            return;
          }

          const points = segment.map(([x, y]) => {
            const terrainY = VEILTerrain.sampleTerrainHeightAtLocal(grid, x, y) + style.offset;
            return new THREE.Vector3(x, terrainY, -y);
          });
          const geometry = new THREE.BufferGeometry().setFromPoints(points);
          const material = new THREE.LineBasicMaterial({
            clippingPlanes: this.gridClipPlanes,
            color: style.color,
            transparent: true,
            opacity: 0.92,
          });
          group.add(new THREE.Line(geometry, material));
        });
      });
      if (group === this.layers.roads) {
        this.renderStats.roads = renderedCount;
      }
      if (group === this.layers.trails) {
        this.renderStats.trails = renderedCount;
      }
    }

    setLayerVisible(layerId, visible) {
      if (this.disposed) {
        return;
      }
      if (this.layers[layerId]) {
        this.layers[layerId].visible = visible;
      }
    }

    tick(deltaSeconds) {
      if (this.disposed) {
        return;
      }
      if (!deltaSeconds) {
        return;
      }
      const speed = deltaSeconds * 0.22;
      this.flowMaterials.forEach((material) => {
        if (material.map) {
          material.map.offset.x = (material.map.offset.x - speed) % 1;
        }
      });
    }

    getVegetationAvoidance() {
      return {
        buildingLines: this.buildingLinesLocal,
        buildingPolygons: this.buildingPolygonsLocal,
        hydrologyLines: this.hydrologyLinesLocal,
        roadLines: this.roadLinesLocal,
        clipBounds: this.clipBounds,
      };
    }

    getRenderStats() {
      return { ...this.renderStats };
    }

    dispose() {
      if (this.disposed) {
        return;
      }

      this.clear();
      this.scene.remove(this.group);
      this.flowTexture?.dispose();
      this.flowTexture = null;
      this.disposed = true;
    }
  }

  global.VEILOverlays = {
    create(scene) {
      return new OverlayRenderer(scene);
    },
  };
})(window);
