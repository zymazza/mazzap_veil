/* Survey companion panel (docs/survey.md): download the QField survey
   package, upload a finished outing (the zipped QField project folder) to
   POST /api/survey-upload, and report the ingest result. app.js passes in
   refreshSurveyLayers so a successful upload appears on the terrain without
   a reload. Token: if the server has a .survey_token, the first 403 prompts
   once and the token is remembered in localStorage. */
(function surveyPanel() {
  'use strict';

  const TOKEN_KEY = 'veil-survey-token';

  function create(refreshSurveyLayers) {
    const panel = document.getElementById('survey-panel');
    if (!panel) return null;
    const form = document.getElementById('survey-form');
    const nameInput = document.getElementById('survey-name');
    const fileInput = document.getElementById('survey-file');
    const uploadBtn = document.getElementById('survey-upload');
    const statusEl = document.getElementById('survey-status');
    const pkgLink = document.getElementById('survey-package-link');

    function disablePackageLink(text, title) {
      if (!pkgLink) return;
      pkgLink.classList.add('disabled');
      pkgLink.setAttribute('aria-disabled', 'true');
      pkgLink.removeAttribute('href');
      pkgLink.textContent = text;
      pkgLink.title = title || text;
    }

    function enablePackageLink(href, text) {
      if (!pkgLink) return;
      pkgLink.classList.remove('disabled');
      pkgLink.removeAttribute('aria-disabled');
      pkgLink.setAttribute('href', href);
      pkgLink.textContent = text;
      pkgLink.title = 'Download the QField survey package';
    }

    // the package link only works once build-survey-package has run
    if (pkgLink) {
      const packageHref = pkgLink.getAttribute('href');
      const packageText = pkgLink.textContent;
      disablePackageLink('Checking survey package…', 'Checking whether the survey package is available.');
      if (!packageHref) {
        disablePackageLink('Survey package unavailable', 'The survey package link is missing.');
      } else {
        fetch(packageHref, { method: 'HEAD' }).then((r) => {
          if (r.ok) {
            enablePackageLink(packageHref, packageText);
            return;
          }
          disablePackageLink(
            'Survey package unavailable',
            'No package yet — run: npm run build-survey-package',
          );
        }).catch(() => {
          disablePackageLink(
            'Survey package unavailable',
            'Could not check the survey package because the network request failed.',
          );
        });
      }
    }

    function setStatus(text, kind) {
      statusEl.textContent = text;
      statusEl.className = `survey-status${kind ? ` ${kind}` : ''}`;
    }

    function summarize(ingest) {
      const layers = ingest?.results?.[0]?.layers || {};
      const totals = { created: 0, updated: 0, moved: 0, retired: 0, unchanged: 0 };
      Object.values(layers).forEach((c) => {
        Object.keys(totals).forEach((k) => { totals[k] += c[k] || 0; });
      });
      const bits = Object.entries(totals)
        .filter(([, n]) => n > 0)
        .map(([k, n]) => `${n} ${k}`);
      return bits.length ? bits.join(', ') : 'no changes';
    }

    async function upload(file, name, token, retried) {
      const url = `/api/survey-upload?name=${encodeURIComponent(name)}`;
      const headers = token ? { 'X-Survey-Token': token } : {};
      const r = await fetch(url, { method: 'POST', headers, body: file });
      if (r.status === 403 && !retried) {
        const entered = window.prompt('This twin requires a survey upload token:');
        if (!entered) throw new Error('upload token required');
        localStorage.setItem(TOKEN_KEY, entered.trim());
        return upload(file, name, entered.trim(), true);
      }
      let body;
      try { body = await r.json(); } catch (_e) { body = {}; }
      if (!r.ok) throw new Error(body.error || `upload failed (${r.status})`);
      return body;
    }

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const file = fileInput.files && fileInput.files[0];
      if (!file) { setStatus('Choose the zipped QField project folder first.'); return; }
      uploadBtn.disabled = true;
      setStatus('Uploading…');
      try {
        const body = await upload(file, nameInput.value.trim() || file.name,
          localStorage.getItem(TOKEN_KEY), false);
        if (body.ok) {
          setStatus(`Ingested: ${summarize(body.ingest)}.`, 'ok');
          fileInput.value = '';
          await refreshSurveyLayers?.();
        } else if (body.saved) {
          setStatus('Upload saved; the store ingest is deferred to the next export '
            + `(${body.error || 'ingest unavailable'}).`, 'warn');
        } else {
          setStatus(`Upload failed: ${body.error || 'unknown error'}.`, 'err');
        }
      } catch (err) {
        setStatus(`Upload failed: ${err.message}.`, 'err');
      } finally {
        uploadBtn.disabled = false;
      }
    });

    return { setStatus };
  }

  window.VEILSurvey = { create };
})();
