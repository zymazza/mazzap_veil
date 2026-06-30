import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

function loadAppTestApi() {
  const source = fs.readFileSync(new URL('../public/app.js', import.meta.url), 'utf8');
  const document = {
    title: '',
    getElementById() {
      return {
        classList: { add() {}, toggle() {} },
        querySelector() { return null; },
      };
    },
  };
  const window = { __VEIL_APP_TEST__: true };
  vm.runInNewContext(source, { console, document, window });
  return window.VEILApp?._test;
}

test('raster layer load requires both image and grid data', async () => {
  const api = loadAppTestApi();
  const layer = {
    id: 'landcover',
    type: 'raster',
    image: 'atlas/local/landcover.png',
    grid: 'atlas/local/landcover.grid.json',
  };

  const data = await api.loadLayerData(layer, {
    loadImage: async (url) => ({ kind: 'image', url }),
    fetchJson: async (url) => ({ kind: 'grid', url }),
  });

  assert.deepEqual(JSON.parse(JSON.stringify(data)), {
    image: { kind: 'image', url: '/data/atlas/local/landcover.png' },
    grid: { kind: 'grid', url: '/data/atlas/local/landcover.grid.json' },
  });
});

test('raster image load failure rejects before grid fetch', async () => {
  const api = loadAppTestApi();
  const layer = {
    id: 'landcover',
    type: 'raster',
    image: 'atlas/local/missing.png',
    grid: 'atlas/local/landcover.grid.json',
  };
  let gridFetches = 0;

  await assert.rejects(
    api.loadLayerData(layer, {
      loadImage: async () => {
        throw new Error('image 404');
      },
      fetchJson: async () => {
        gridFetches += 1;
        return {};
      },
    }),
    /image 404/,
  );
  assert.equal(gridFetches, 0);
});

test('raster grid load failure rejects instead of returning image-only data', async () => {
  const api = loadAppTestApi();
  const layer = {
    id: 'landcover',
    label: 'Land Cover',
    type: 'raster',
    image: 'atlas/local/landcover.png',
    grid: 'atlas/local/missing.grid.json',
  };

  await assert.rejects(
    api.loadLayerData(layer, {
      loadImage: async (url) => ({ url }),
      fetchJson: async () => {
        throw new Error('/data/atlas/local/missing.grid.json: 404');
      },
    }),
    /missing\.grid\.json: 404/,
  );
  assert.match(
    api.layerLoadFailureMessage(layer, new Error('grid 404')),
    /Land Cover failed to load: grid 404/,
  );
});

test('vector layer load still fetches the feature file', async () => {
  const api = loadAppTestApi();
  const layer = {
    id: 'roads',
    type: 'line',
    file: 'atlas/local/roads.geojson',
  };

  const data = await api.loadLayerData(layer, {
    fetchJson: async (url) => ({ type: 'FeatureCollection', url }),
  });

  assert.deepEqual(JSON.parse(JSON.stringify(data)), {
    type: 'FeatureCollection',
    url: '/data/atlas/local/roads.geojson',
  });
});

test('scene layer defaults keep parcel outlines hidden at boot', () => {
  const api = loadAppTestApi();

  assert.equal(api.sceneLayerDefaultVisible('parcels'), false);
  assert.equal(api.sceneLayerDefaultVisible('buildings'), true);
  assert.equal(api.sceneLayerDefaultVisible('hydrology'), true);
  assert.equal(api.sceneLayerDefaultVisible('roads'), true);
  assert.equal(api.sceneLayerDefaultVisible('vegetation'), true);
});
