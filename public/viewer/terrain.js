(function attachTerrainHelpers(global) {
  const { THREE } = global;

  function colorForHeight(ratio) {
    const palette = [
      new THREE.Color('#2a6041'),
      new THREE.Color('#4f7459'),
      new THREE.Color('#8d98a7'),
      new THREE.Color('#dcccbb'),
      new THREE.Color('#eab464'),
    ];
    const scaled = Math.max(0, Math.min(0.9999, ratio)) * (palette.length - 1);
    const lowerIndex = Math.floor(scaled);
    const upperIndex = Math.min(palette.length - 1, lowerIndex + 1);
    const blend = scaled - lowerIndex;
    return palette[lowerIndex].clone().lerp(palette[upperIndex], blend);
  }

  function gridSteps(grid) {
    return {
      x:
        grid.width > 1 ? (grid.maxX - grid.minX) / (grid.width - 1) : grid.xStep || 1,
      y:
        grid.height > 1 ? (grid.maxY - grid.minY) / (grid.height - 1) : grid.yStep || 1,
    };
  }

  function isValidGridIndex(grid, index) {
    return Number.isFinite(grid.heights[index]);
  }

  function localVertexForGridIndex(grid, index, xStep, yStep, yOverride = null) {
    const column = index % grid.width;
    const row = Math.floor(index / grid.width);
    const localX = grid.minX + column * xStep;
    const localY = grid.maxY - row * yStep;
    const elevation = grid.heights[index];
    const safeElevation = Number.isFinite(elevation) ? elevation : grid.minElevation;
    return [
      localX,
      yOverride === null ? safeElevation - grid.minElevation : yOverride,
      -localY,
    ];
  }

  function buildTerrainTriangleIndices(grid) {
    function isValidIndex(index) {
      return isValidGridIndex(grid, index);
    }

    const indices = [];
    for (let row = 0; row < grid.height - 1; row += 1) {
      for (let column = 0; column < grid.width - 1; column += 1) {
        const topLeft = row * grid.width + column;
        const topRight = topLeft + 1;
        const bottomLeft = topLeft + grid.width;
        const bottomRight = bottomLeft + 1;

        if (isValidIndex(topLeft) && isValidIndex(bottomLeft) && isValidIndex(topRight)) {
          indices.push(topLeft, bottomLeft, topRight);
        }
        if (isValidIndex(topRight) && isValidIndex(bottomLeft) && isValidIndex(bottomRight)) {
          indices.push(topRight, bottomLeft, bottomRight);
        }
      }
    }
    return indices;
  }

  function buildTerrainMesh(grid) {
    const geometry = new THREE.BufferGeometry();
    const positions = new Float32Array(grid.width * grid.height * 3);
    const colors = new Float32Array(grid.width * grid.height * 3);
    const uvs = new Float32Array(grid.width * grid.height * 2);
    const elevationRange = Math.max(1, grid.maxElevation - grid.minElevation);
    const { x: xStep, y: yStep } = gridSteps(grid);

    for (let index = 0; index < grid.heights.length; index += 1) {
      const column = index % grid.width;
      const row = Math.floor(index / grid.width);
      const elevation = grid.heights[index];
      const valid = Number.isFinite(elevation);
      const safeElevation = valid ? elevation : grid.minElevation;
      const vertex = localVertexForGridIndex(grid, index, xStep, yStep);
      positions[index * 3] = vertex[0];
      positions[index * 3 + 1] = vertex[1];
      positions[index * 3 + 2] = vertex[2];
      const ratio = (safeElevation - grid.minElevation) / elevationRange;
      const color = colorForHeight(ratio);
      colors[index * 3] = color.r;
      colors[index * 3 + 1] = color.g;
      colors[index * 3 + 2] = color.b;
      // Terrain vertices are sampled at DEM cell centers, while drape imagery is defined on
      // the raster's outer edges. Offset the UVs by half a pixel so textures line up with the
      // true raster footprint instead of stretching edge-to-edge across the center samples.
      uvs[index * 2] = grid.width > 0 ? (column + 0.5) / grid.width : 0;
      uvs[index * 2 + 1] = grid.height > 0 ? 1 - (row + 0.5) / grid.height : 1;
    }

    const indices = buildTerrainTriangleIndices(grid);

    geometry.setIndex(indices);
    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));
    geometry.setAttribute('uv', new THREE.BufferAttribute(uvs, 2));
    geometry.computeVertexNormals();

    const material = new THREE.MeshStandardMaterial({
      vertexColors: true,
      metalness: 0.06,
      roughness: 0.92,
    });

    return {
      elevationMaterial: material,
      geometry,
      mesh: new THREE.Mesh(geometry, material),
    };
  }

  function buildTerrainBaseMesh(grid, options = {}) {
    if (!grid || !Array.isArray(grid.heights) || !grid.heights.length) {
      return null;
    }

    const validBounds = { minColumn: Infinity, maxColumn: -Infinity, minRow: Infinity, maxRow: -Infinity };
    for (let index = 0; index < grid.heights.length; index += 1) {
      if (!isValidGridIndex(grid, index)) {
        continue;
      }
      const column = index % grid.width;
      const row = Math.floor(index / grid.width);
      validBounds.minColumn = Math.min(validBounds.minColumn, column);
      validBounds.maxColumn = Math.max(validBounds.maxColumn, column);
      validBounds.minRow = Math.min(validBounds.minRow, row);
      validBounds.maxRow = Math.max(validBounds.maxRow, row);
    }
    if (!Number.isFinite(validBounds.minColumn) || validBounds.minColumn === validBounds.maxColumn ||
        validBounds.minRow === validBounds.maxRow) {
      return null;
    }

    const floorY = Number.isFinite(options.floorY) ? options.floorY : -0.03;
    const { x: xStep, y: yStep } = gridSteps(grid);
    const positions = [];
    const indices = [];

    function pushVertex(vertex) {
      const index = positions.length / 3;
      positions.push(vertex[0], vertex[1], vertex[2]);
      return index;
    }

    function gridIndex(column, row) {
      return row * grid.width + column;
    }

    function nearestValidIndex(column, row, axis) {
      const start = axis === 'row' ? validBounds.minRow : validBounds.minColumn;
      const end = axis === 'row' ? validBounds.maxRow : validBounds.maxColumn;
      let best = null;
      let bestDistance = Infinity;
      for (let cursor = start; cursor <= end; cursor += 1) {
        const candidate = axis === 'row' ? gridIndex(column, cursor) : gridIndex(cursor, row);
        if (!isValidGridIndex(grid, candidate)) {
          continue;
        }
        const distance = Math.abs(cursor - (axis === 'row' ? row : column));
        if (distance < bestDistance) {
          best = candidate;
          bestDistance = distance;
        }
      }
      return best;
    }

    function perimeterIndex(column, row, axis) {
      const index = gridIndex(column, row);
      return isValidGridIndex(grid, index) ? index : nearestValidIndex(column, row, axis);
    }

    function addWallSegment(firstGridIndex, secondGridIndex) {
      if (firstGridIndex === null || secondGridIndex === null) {
        return;
      }
      const topA = pushVertex(localVertexForGridIndex(grid, firstGridIndex, xStep, yStep));
      const topB = pushVertex(localVertexForGridIndex(grid, secondGridIndex, xStep, yStep));
      const bottomB = pushVertex(localVertexForGridIndex(grid, secondGridIndex, xStep, yStep, floorY));
      const bottomA = pushVertex(localVertexForGridIndex(grid, firstGridIndex, xStep, yStep, floorY));
      indices.push(topA, topB, bottomB, topA, bottomB, bottomA);
    }

    for (let column = validBounds.minColumn; column < validBounds.maxColumn; column += 1) {
      addWallSegment(
        perimeterIndex(column, validBounds.minRow, 'row'),
        perimeterIndex(column + 1, validBounds.minRow, 'row')
      );
      addWallSegment(
        perimeterIndex(column + 1, validBounds.maxRow, 'row'),
        perimeterIndex(column, validBounds.maxRow, 'row')
      );
    }
    for (let row = validBounds.minRow; row < validBounds.maxRow; row += 1) {
      addWallSegment(
        perimeterIndex(validBounds.maxColumn, row, 'column'),
        perimeterIndex(validBounds.maxColumn, row + 1, 'column')
      );
      addWallSegment(
        perimeterIndex(validBounds.minColumn, row + 1, 'column'),
        perimeterIndex(validBounds.minColumn, row, 'column')
      );
    }

    if (!indices.length) {
      return null;
    }

    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
    geometry.setIndex(indices);
    geometry.computeVertexNormals();

    const material = new THREE.MeshBasicMaterial({
      color: options.color || 0xc2b29e,
      side: THREE.DoubleSide,
    });
    const mesh = new THREE.Mesh(geometry, material);
    mesh.renderOrder = -2;
    mesh.name = 'terrain-base-pedestal';
    return mesh;
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function sampleTerrainHeightAtLocal(grid, localX, localY) {
    if (!grid || !Array.isArray(grid.heights) || !grid.heights.length) {
      return 0;
    }

    const widthMeters = Math.max(1e-9, grid.maxX - grid.minX);
    const heightMeters = Math.max(1e-9, grid.maxY - grid.minY);
    const xRatio = clamp((localX - grid.minX) / widthMeters, 0, 0.999999);
    const yRatio = clamp((localY - grid.minY) / heightMeters, 0, 0.999999);
    const xIndex = xRatio * (grid.width - 1);
    const yIndex = (1 - yRatio) * (grid.height - 1);
    const x0 = Math.floor(xIndex);
    const y0 = Math.floor(yIndex);
    const x1 = Math.min(grid.width - 1, x0 + 1);
    const y1 = Math.min(grid.height - 1, y0 + 1);
    const tx = xIndex - x0;
    const ty = yIndex - y0;
    const indexAt = (x, y) => y * grid.width + x;
    const h00 = grid.heights[indexAt(x0, y0)];
    const h10 = grid.heights[indexAt(x1, y0)];
    const h01 = grid.heights[indexAt(x0, y1)];
    const h11 = grid.heights[indexAt(x1, y1)];
    const samples = [
      { value: h00, weight: (1 - tx) * (1 - ty) },
      { value: h10, weight: tx * (1 - ty) },
      { value: h01, weight: (1 - tx) * ty },
      { value: h11, weight: tx * ty },
    ].filter((sample) => Number.isFinite(sample.value));

    if (!samples.length) {
      return 0;
    }

    const totalWeight = samples.reduce((sum, sample) => sum + sample.weight, 0);
    if (totalWeight <= 0) {
      return samples[0].value - grid.minElevation;
    }

    const weighted =
      samples.reduce((sum, sample) => sum + sample.value * sample.weight, 0) / totalWeight;
    return weighted - grid.minElevation;
  }

  // True only where the DEM has a real elevation (inside the rendered terrain).
  // Used to keep vegetation from floating over the nodata area beyond the parcel.
  function hasValidTerrainAtLocal(grid, localX, localY) {
    if (!grid || !Array.isArray(grid.heights) || !grid.heights.length) {
      return false;
    }
    if (localX < grid.minX || localX > grid.maxX || localY < grid.minY || localY > grid.maxY) {
      return false;
    }
    const widthMeters = Math.max(1e-9, grid.maxX - grid.minX);
    const heightMeters = Math.max(1e-9, grid.maxY - grid.minY);
    const col = Math.round(((localX - grid.minX) / widthMeters) * (grid.width - 1));
    const row = Math.round((1 - (localY - grid.minY) / heightMeters) * (grid.height - 1));
    if (col < 0 || col >= grid.width || row < 0 || row >= grid.height) {
      return false;
    }
    return Number.isFinite(grid.heights[row * grid.width + col]);
  }

  global.VEILTerrain = {
    buildTerrainMesh,
    buildTerrainBaseMesh,
    sampleTerrainHeightAtLocal,
    hasValidTerrainAtLocal,
  };
})(window);
