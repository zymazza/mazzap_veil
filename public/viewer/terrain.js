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

  function buildTerrainMesh(grid) {
    const geometry = new THREE.BufferGeometry();
    const positions = new Float32Array(grid.width * grid.height * 3);
    const colors = new Float32Array(grid.width * grid.height * 3);
    const uvs = new Float32Array(grid.width * grid.height * 2);
    const elevationRange = Math.max(1, grid.maxElevation - grid.minElevation);
    const xStep =
      grid.width > 1 ? (grid.maxX - grid.minX) / (grid.width - 1) : grid.xStep || 1;
    const yStep =
      grid.height > 1 ? (grid.maxY - grid.minY) / (grid.height - 1) : grid.yStep || 1;

    for (let index = 0; index < grid.heights.length; index += 1) {
      const column = index % grid.width;
      const row = Math.floor(index / grid.width);
      const localX = grid.minX + column * xStep;
      const localY = grid.maxY - row * yStep;
      const elevation = grid.heights[index];
      const valid = Number.isFinite(elevation);
      const safeElevation = valid ? elevation : grid.minElevation;
      positions[index * 3] = localX;
      positions[index * 3 + 1] = safeElevation - grid.minElevation;
      positions[index * 3 + 2] = -localY;
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

    function isValidIndex(index) {
      return Number.isFinite(grid.heights[index]);
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
    sampleTerrainHeightAtLocal,
    hasValidTerrainAtLocal,
  };
})(window);
