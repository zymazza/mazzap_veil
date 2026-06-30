import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

function loadAppTestApi() {
  const source = fs.readFileSync(new URL('../public/app.js', import.meta.url), 'utf8');
  const window = { __VEIL_APP_TEST__: true };
  const document = { getElementById: () => ({}) };
  vm.runInNewContext(source, { document, window });
  return window.VEILApp._test;
}

test('identify property rows escape uploaded keys and values', () => {
  const api = loadAppTestApi();
  const html = api.propRows({
    'bad_<img>': '"><svg onload=alert(1)>',
    safe_value: 7,
  });

  assert.match(html, /Bad &lt;Img&gt;/);
  assert.match(html, /&quot;&gt;&lt;svg onload=alert\(1\)&gt;/);
  assert.doesNotMatch(html, /<svg/i);
  assert.doesNotMatch(html, /<img/i);
});

test('identify cards escape layer labels, titles, and legacy html text', () => {
  const api = loadAppTestApi();
  const html = api.identifyResultsHtml([{
    layer: { label: 'Layer <b onclick=alert(1)>' },
    title: 'Title <img src=x onerror=alert(1)>',
    html: '<img src=x onerror=alert(1)>',
  }], '', '');

  assert.match(html, /Layer &lt;b onclick=alert\(1\)&gt;/);
  assert.match(html, /Title &lt;img src=x onerror=alert\(1\)&gt;/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.doesNotMatch(html, /<img/i);
  assert.doesNotMatch(html, /<b/i);
});

test('identify cards allow only internally generated bodyHtml markup', () => {
  const api = loadAppTestApi();
  const bodyHtml = api.propRows({ note: '<b>field</b>' });
  const html = api.identifyResultsHtml([{
    layer: { label: 'Survey' },
    title: 'Marker',
    bodyHtml,
  }], '', '');

  assert.match(html, /<div class="info-row">/);
  assert.match(html, /&lt;b&gt;field&lt;\/b&gt;/);
  assert.doesNotMatch(html, /<b>field<\/b>/);
});

test('identify species card escapes uploaded species names', () => {
  const api = loadAppTestApi();
  const html = api.speciesCardHtml(['Clean name', 'Bad <script>alert(1)</script>']);

  assert.match(html, /Clean name/);
  assert.match(html, /Bad &lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script/i);
});

test('identify photo src only accepts encoded relative data paths', () => {
  const api = loadAppTestApi();

  assert.equal(
    api.safeDataAssetSrc('surveys/photos/trail cam "east".jpg'),
    '/data/surveys/photos/trail%20cam%20%22east%22.jpg',
  );
  assert.equal(api.safeDataAssetSrc('../secret.jpg'), '');
  assert.equal(api.safeDataAssetSrc('/surveys/photo.jpg'), '');
  assert.equal(api.safeDataAssetSrc('https://example.test/photo.jpg'), '');
  assert.equal(api.safeDataAssetSrc('surveys\\photo.jpg'), '');
  assert.equal(api.safeDataAssetSrc('surveys/photo.jpg?x=1'), '');
  assert.equal(
    api.safeDataAssetSrc('surveys/photo.jpg" onerror="alert(1)'),
    '/data/surveys/photo.jpg%22%20onerror%3D%22alert(1)',
  );

  const img = api.photoHtml('surveys/photos/trail cam "east".jpg');
  assert.match(img, /src="\/data\/surveys\/photos\/trail%20cam%20%22east%22\.jpg"/);
  assert.doesNotMatch(img, /onerror/i);
  const encodedAttackImg = api.photoHtml('surveys/photo.jpg" onerror="alert(1)');
  assert.match(encodedAttackImg, /src="\/data\/surveys\/photo\.jpg%22%20onerror%3D%22alert\(1\)"/);
  assert.doesNotMatch(encodedAttackImg, /"\s+onerror=/i);
  assert.equal(api.photoHtml('../secret.jpg'), '');
});
