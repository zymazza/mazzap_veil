#!/usr/bin/env node
'use strict';

// Minimal zero-dependency static server for a VEIL digital twin.
// Serves ./public (viewer) and ./data (bundled geospatial data). No database,
// no cloud, no build step. POST /api/chat additionally proxies the viewer's
// chat panel to OpenAI with the twin MCP server's tools (see "chat API"
// below) — still no npm dependencies.

const http = require('http');
const fs = require('fs');
const path = require('path');
const { URL } = require('url');
const { spawn } = require('child_process');

const ROOT = __dirname;
// A twin's data dir. Defaults to ./data; set TWIN_DATA_DIR to serve an
// alternate/scratch twin from the same checkout (its /data/* requests are
// served from there, /public is unchanged) without touching ./data.
const DATA_DIR = path.resolve(ROOT, process.env.TWIN_DATA_DIR || 'data');
const PORT = Number(process.env.PORT) || 4173;
const HOST = process.env.HOST || '127.0.0.1';

// ---------------------------------------------------------------- chat API
// POST /api/chat lets the viewer's chat panel ask an LLM about the land.
// The LLM's tools ARE the twin MCP server: we spawn scripts/mcp_server.py
// once, speak JSON-RPC to it over stdio, and hand its tool catalog to
// OpenAI's function calling. Still zero npm dependencies (built-in fetch +
// child_process).

// Provider: 'openai' (hosted Responses API, default) or 'ollama' (a local model
// over Ollama's OpenAI-compatible /v1/chat/completions). CHAT_PROVIDER picks it;
// if unset but OLLAMA_MODEL is given, we infer 'ollama'. Ollama needs no key and
// nothing leaves the machine.
const CHAT_PROVIDER = (process.env.CHAT_PROVIDER
  || (process.env.OLLAMA_MODEL ? 'ollama' : 'openai')).toLowerCase();

const OPENAI_MODEL = process.env.OPENAI_MODEL || 'gpt-5.5';
const OPENAI_REASONING = process.env.OPENAI_REASONING_EFFORT || 'low';
// When set, the server never spends its own key: every /api/chat request must
// carry the caller's key (X-OpenAI-Key). Use this for any public deployment so
// strangers reaching the port bring their own quota, not yours.
const OPENAI_REQUIRE_USER_KEY = process.env.OPENAI_REQUIRE_USER_KEY === '1';

const OLLAMA_HOST = (process.env.OLLAMA_HOST || 'http://127.0.0.1:11434').replace(/\/+$/, '');
const OLLAMA_MODEL = process.env.OLLAMA_MODEL || 'gpt-oss:20b';
// Context window for the local model. gpt-oss:20b uses sliding-window attention,
// so its KV cache is remarkably cheap (~27 MiB per 1k tokens): 13 GB of weights
// + KV + graph is ~15 GB at 32k and only ~17 GB at the full 131072 — context is
// nearly free on VRAM. We default to 96k so a maxed-out tool session (up to
// OLLAMA_MAX_TOOL_ROUNDS rounds, each with a TOOL_RESULT_CAP-sized result) never
// truncates; this fits ~16 GB / 100% GPU on a free 24 GB card. Set to 131072 for
// the model's max if you want; the limit is GPU memory, not the model.
const OLLAMA_NUM_CTX = Number(process.env.OLLAMA_NUM_CTX) || 98304;
// Sampling temperature for the local model. gpt-oss defaults to 1.0, which makes
// agentic tool-calling unreliable — it intermittently emits truncated/malformed
// tool-call JSON or a turn with no answer. 0 makes tool use deterministic and
// dependable; raise only if you want more varied prose.
const OLLAMA_TEMPERATURE = process.env.OLLAMA_TEMPERATURE !== undefined
  ? Number(process.env.OLLAMA_TEMPERATURE) : 0;

// The active chat model label (shown in the panel / echoed in responses).
const CHAT_MODEL = CHAT_PROVIDER === 'ollama' ? OLLAMA_MODEL : OPENAI_MODEL;
const MAX_TOOL_ROUNDS = 8; // OpenAI batches calls per round, so 8 is plenty
// Local reasoning models (gpt-oss) emit ONE tool call per turn after thinking,
// so they need many more rounds to work through a multi-step question.
const MAX_TOOL_ROUNDS_OLLAMA = Number(process.env.OLLAMA_MAX_TOOL_ROUNDS) || 16;
const TOOL_RESULT_CAP = 24000; // chars per tool result sent to the model

function openaiKey() {
  if (process.env.OPENAI_API_KEY) return process.env.OPENAI_API_KEY.trim();
  try {
    return fs.readFileSync(path.join(ROOT, '.openai_key'), 'utf8').trim();
  } catch (_err) {
    return null;
  }
}

// -- minimal MCP stdio client (one persistent python child) ---------------
let mcp = null; // { proc, pending: Map<id,{resolve,reject}>, nextId, tools, init: Promise }

function mcpSpawn() {
  const proc = spawn('python3', [path.join(ROOT, 'scripts', 'mcp_server.py')], {
    cwd: ROOT,
    stdio: ['pipe', 'pipe', 'inherit'],
  });
  const state = { proc, pending: new Map(), nextId: 1, tools: null, buf: '' };
  proc.stdout.setEncoding('utf8');
  proc.stdout.on('data', (chunk) => {
    state.buf += chunk;
    let nl;
    while ((nl = state.buf.indexOf('\n')) >= 0) {
      const line = state.buf.slice(0, nl).trim();
      state.buf = state.buf.slice(nl + 1);
      if (!line) continue;
      let msg;
      try { msg = JSON.parse(line); } catch (_err) { continue; }
      const waiter = state.pending.get(msg.id);
      if (!waiter) continue;
      state.pending.delete(msg.id);
      if (msg.error) waiter.reject(new Error(msg.error.message || 'MCP error'));
      else waiter.resolve(msg.result);
    }
  });
  proc.on('close', () => {
    state.pending.forEach((w) => w.reject(new Error('MCP server exited')));
    if (mcp === state) mcp = null; // respawned lazily on next chat
  });
  state.request = (method, params) => new Promise((resolve, reject) => {
    const id = state.nextId++;
    state.pending.set(id, { resolve, reject });
    proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
    setTimeout(() => {
      if (state.pending.delete(id)) reject(new Error(`MCP ${method} timed out`));
    }, 120000);
  });
  state.notify = (method, params) => {
    proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', method, params }) + '\n');
  };
  state.init = (async () => {
    await state.request('initialize', {
      protocolVersion: '2025-06-18',
      capabilities: {},
      clientInfo: { name: 'veil-chat', version: '1.0' },
    });
    state.notify('notifications/initialized', {});
    const listed = await state.request('tools/list', {});
    state.tools = listed.tools || [];
  })();
  return state;
}

async function mcpReady() {
  if (!mcp) mcp = mcpSpawn();
  await mcp.init;
  return mcp;
}

async function mcpCall(name, args) {
  const client = await mcpReady();
  const result = await client.request('tools/call', { name, arguments: args || {} });
  const text = (result.content || [])
    .filter((c) => c.type === 'text')
    .map((c) => c.text)
    .join('\n');
  return text || JSON.stringify(result);
}

// -- scope context: what the chat panel selected in the 3D scene ----------
// Region scope pre-loads a summary of the drawn polygon; point scope loads
// identify-at plus everything within ~100 m, so the model starts grounded
// and can still call more tools itself.
async function scopeContext(scope) {
  if (!scope || scope.type === 'all' || !scope.type) {
    return 'Scope: the whole twin. Use tools as needed; describe_twin orients you.';
  }
  if (scope.type === 'region') {
    const region = { polygon: scope.polygon };
    const summary = await mcpCall('summarize_region', { region });
    return [
      'Scope: a region the user drew in the 3D viewer.',
      `Region object for spatial tools (scene-local meters): ${JSON.stringify(region)}`,
      'When the user says "here"/"this area", they mean this region. Pass this exact',
      'region object to find_entities / aggregate_entities / canopy_change / summarize_region.',
      `Pre-loaded summarize_region result:\n${cap(summary)}`,
    ].join('\n');
  }
  if (scope.type === 'point') {
    const radius = Number(scope.radius_m) || 100;
    const point = scope.point;
    const region = { within_m: radius, point };
    const [ident, summary] = await Promise.all([
      mcpCall('identify_at', { point }),
      mcpCall('summarize_region', { region }),
    ]);
    return [
      'Scope: a single point the user picked in the 3D viewer.',
      `Point (scene-local meters): ${JSON.stringify(point)}`,
      `Region object for spatial tools (${radius} m around it): ${JSON.stringify(region)}`,
      'When the user says "here"/"this spot", they mean this point and its surroundings.',
      `Pre-loaded identify_at result:\n${cap(ident)}`,
      `Pre-loaded summarize_region (${radius} m radius) result:\n${cap(summary)}`,
    ].join('\n');
  }
  throw new Error(`unknown scope type: ${scope.type}`);
}

function cap(text, n = TOOL_RESULT_CAP) {
  return text.length > n ? `${text.slice(0, n)}\n…[truncated ${text.length - n} chars]` : text;
}

const CHAT_SYSTEM_PROMPT = [
  'You are the resident guide of a VEIL digital twin — a georeferenced 3D model of',
  'a real place with terrain, vegetation, buildings, soils, hydrology, land cover',
  'and habitat data. You answer questions about THIS land using the read-only twin',
  'tools, and you show places on the user\'s live 3D map.',
  '',
  'GROUNDING',
  '- Call describe_twin first to learn where the twin is and what it contains.',
  '- Every figure must come from a tool result — never invent or estimate numbers.',
  '  Keep each number\'s real unit (count, m, m², ha, acres, %); do not relabel a',
  '  percentage as an area or reuse one figure for unrelated things. If a tool did',
  '  not give you something, say so plainly instead of guessing.',
  '- Prefer aggregate_entities and summarize_region for counts and statistics; use',
  '  small limit values on find_entities. Coordinates: tools take {lat,lon} degrees',
  '  or scene-local meters {x,y} (results echo both); distances and heights are m.',
  '',
  'SHOW PLACES ON THE MAP — this is a hard rule, not optional:',
  '- Whenever your answer points at one or more specific places, you MUST call',
  '  draw_point (a spot) for EACH place, with a short label. Prefer draw_point — it',
  '  is reliable. The map drawing IS the deliverable.',
  '- Use REAL coordinates from a tool result (an entity\'s `position` or a feature\'s',
  '  centroid) — never invented numbers. When a place is a relevant feature (water,',
  '  an edge, a road), anchor the marker to that feature\'s coordinates.',
  '- Never write "I\'ll draw…", "I\'ll mark…", "let me plot…" or similar. Drawing is',
  '  a tool call you actually make, not a sentence you say. If you mentioned a place',
  '  but did not call a draw tool for it, you are not done.',
  '- Draw 2-3 places at most, each at a DIFFERENT location, each exactly once. Never',
  '  draw the same coordinate twice. Do NOT call clear_drawings (the user clears the',
  '  map) and do not redraw or second-guess a marker you already placed.',
  '- Don\'t recite raw coordinates in prose — the marker shows the location. Just',
  '  name what you drew, then give your final written answer.',
  '',
  'BE EFFICIENT — you have a limited number of tool turns:',
  '- Gather only the data you actually need, then commit to an answer. Do not repeat',
  '  a query you already ran or keep re-checking the same thing.',
  '- The normal arc is: a few read calls → one draw call per place → your final',
  '  written reply. End with the reply; don\'t keep calling tools after you can answer.',
  '',
  'ANSWER STYLE — the chat panel is small and renders ONLY **bold**, "- " bullet',
  'lines, and short paragraphs:',
  '- Do NOT use markdown tables, headings (#), or code blocks — they show up as raw',
  '  symbols. Use a one- or two-sentence lead answer, then at most a few "- "',
  '  bullets for the key facts, with **bold** on the essentials.',
  '- Be concise and conversational. Do not narrate your tool calls or add a "how I',
  '  got the numbers" / methodology section. Name a data source (LiDAR, gSSURGO,',
  '  LANDFIRE, GAP…) only in a few words when it genuinely matters.',
].join('\n');

// GPT-5.5 function calling lives on the Responses API (chat/completions
// rejects tools+reasoning for it). Tool rounds chain via previous_response_id
// so reasoning state carries across calls.
async function openaiResponses(payload, apiKey) {
  const res = await fetch('https://api.openai.com/v1/responses', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`OpenAI ${res.status}: ${detail.slice(0, 400)}`);
  }
  return res.json();
}

// Ollama's native /api/chat speaks the same tool/function-calling schema and,
// unlike its OpenAI-compatible endpoint, honors options.num_ctx — which we need
// to size the context window so the model fits the GPU. No auth, no network
// egress (the local daemon). Tool rounds carry state by replaying the growing
// message list (there is no previous_response_id here).
async function ollamaChat(payload) {
  let res;
  try {
    res = await fetch(`${OLLAMA_HOST}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } catch (err) {
    throw new Error(`cannot reach Ollama at ${OLLAMA_HOST} — is "ollama serve" running? (${err.message || err})`);
  }
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`Ollama ${res.status}: ${detail.slice(0, 400)}`);
  }
  return res.json();
}

// --- provider runners: each takes the shared {history, toolDefs, instructions}
// and drives the tool loop in its provider's dialect, returning {reply, trace}.

async function runOpenAI({ history, toolDefs, instructions, apiKey }) {
  const tools = toolDefs.map((t) => ({
    type: 'function', name: t.name, description: t.description, parameters: t.parameters,
  }));
  const trace = [];
  let reply = null;
  let previousId = null;
  let input = history;
  for (let round = 0; round <= MAX_TOOL_ROUNDS; round += 1) {
    const data = await openaiResponses({
      model: OPENAI_MODEL,
      reasoning: { effort: OPENAI_REASONING },
      instructions,
      tools,
      input,
      ...(previousId ? { previous_response_id: previousId } : {}),
    }, apiKey);
    previousId = data.id;
    const calls = (data.output || []).filter((o) => o.type === 'function_call');
    if (!calls.length) {
      reply = (data.output || [])
        .filter((o) => o.type === 'message')
        .flatMap((o) => o.content || [])
        .filter((c) => c.type === 'output_text')
        .map((c) => c.text)
        .join('\n');
      break;
    }
    input = [];
    for (const call of calls) {
      let args = {};
      try { args = JSON.parse(call.arguments || '{}'); } catch (_err) { /* ignore */ }
      trace.push({ tool: call.name, args });
      let result;
      try {
        result = await mcpCall(call.name, args);
      } catch (err) {
        result = JSON.stringify({ error: String(err.message || err) });
      }
      input.push({ type: 'function_call_output', call_id: call.call_id, output: cap(result) });
    }
  }
  if (reply === null) reply = '(stopped after too many tool calls — try a narrower question)';
  return { reply, trace };
}

async function runOllama({ history, toolDefs, instructions }) {
  const tools = toolDefs.map((t) => ({
    type: 'function',
    function: { name: t.name, description: t.description, parameters: t.parameters },
  }));
  const messages = [{ role: 'system', content: instructions }, ...history];
  const trace = [];
  let reply = null;
  let lastContent = ''; // gpt-oss often writes its answer in a turn that ALSO calls a tool
  let nudges = 0;        // bounded recoveries from malformed/empty turns
  for (let round = 0; round <= MAX_TOOL_ROUNDS_OLLAMA; round += 1) {
    let data;
    try {
      data = await ollamaChat({
        model: OLLAMA_MODEL,
        messages,
        tools,
        stream: false,
        options: { num_ctx: OLLAMA_NUM_CTX, temperature: OLLAMA_TEMPERATURE },
      });
    } catch (err) {
      // gpt-oss intermittently emits invalid tool-call JSON; Ollama 500s on it.
      // Resampling at temp 0 just repeats the bad output, so instead feed back a
      // correction — that changes the context and breaks the deterministic path.
      if (/parsing tool call/i.test(String(err.message || err)) && nudges < 3) {
        nudges += 1;
        messages.push({ role: 'user', content: 'Your last tool call was not valid JSON. '
          + 'Re-issue it with complete, valid arguments — plain numbers for lat/lon or x/y and a short string label. '
          + 'If you already have what you need, stop calling tools and write your final answer now.' });
        if (process.env.OLLAMA_DEBUG) console.error(`[r${round}] tool-call parse error -> nudge ${nudges}`);
        continue;
      }
      throw err;
    }
    const msg = data.message || {};
    const calls = msg.tool_calls || [];
    if (msg.content && msg.content.trim()) lastContent = msg.content;
    if (process.env.OLLAMA_DEBUG) console.error(`[r${round}] done=${data.done_reason} content=${(msg.content||'').length} thinking=${(msg.thinking||'').length} calls=${calls.length} [${calls.map((c)=>c.function&&c.function.name).join(',')}]`);
    if (!calls.length) {
      // A turn with no tool call should carry the final answer. gpt-oss sometimes
      // ends with an empty "final" channel — nudge it once to actually write it.
      if ((msg.content && msg.content.trim()) || lastContent.trim() || nudges >= 3) {
        reply = msg.content || '';
        break;
      }
      nudges += 1;
      messages.push({ role: 'user', content: 'Now write your final answer for the user, following the answer-style rules.' });
      if (process.env.OLLAMA_DEBUG) console.error(`[r${round}] empty final -> nudge ${nudges}`);
      continue;
    }
    // Echo the assistant's tool-call turn, then answer each call with a tool msg.
    messages.push({ role: 'assistant', content: msg.content || '', tool_calls: calls });
    for (const call of calls) {
      const fn = call.function || {};
      let args = {};
      try { args = typeof fn.arguments === 'string' ? JSON.parse(fn.arguments || '{}') : (fn.arguments || {}); }
      catch (_err) { /* ignore */ }
      trace.push({ tool: fn.name, args });
      let result;
      try {
        result = await mcpCall(fn.name, args);
      } catch (err) {
        result = JSON.stringify({ error: String(err.message || err) });
      }
      messages.push({ role: 'tool', tool_call_id: call.id, content: cap(result) });
    }
  }
  // Recover the answer the model wrote alongside a tool call, or before it ran
  // out of rounds mid-drawing, rather than returning a blank bubble.
  if (reply === null) reply = '';
  if (!reply.trim()) reply = lastContent;
  if (!reply.trim()) reply = '(stopped before answering — try a narrower question)';
  return { reply, trace };
}

async function handleChat(req, res) {
  let body = '';
  req.on('data', (chunk) => { body += chunk; });
  req.on('end', async () => {
    try {
      // OpenAI needs a key (BYOK header wins, else server key); Ollama is local
      // and keyless.
      let apiKey = null;
      if (CHAT_PROVIDER === 'openai') {
        const userKey = String(req.headers['x-openai-key'] || '').trim();
        apiKey = userKey || (OPENAI_REQUIRE_USER_KEY ? null : openaiKey());
        if (!apiKey) {
          return send(res, 400, JSON.stringify({
            error: OPENAI_REQUIRE_USER_KEY
              ? 'This twin requires your own OpenAI key — click "Key" in the chat panel to add one.'
              : 'No OpenAI key: click "Key" in the chat panel to add yours, or set OPENAI_API_KEY / .openai_key on the server.',
          }), { 'Content-Type': 'application/json' });
        }
      }
      const { messages = [], scope = { type: 'all' } } = JSON.parse(body || '{}');
      const history = messages
        .filter((m) => (m.role === 'user' || m.role === 'assistant') && m.content)
        .slice(-30)
        .map((m) => ({ role: m.role, content: String(m.content) }));
      if (!history.length || history[history.length - 1].role !== 'user') {
        return send(res, 400, JSON.stringify({ error: 'last message must be from the user' }),
          { 'Content-Type': 'application/json' });
      }

      const client = await mcpReady();
      const toolDefs = client.tools.map((t) => ({
        name: t.name, description: t.description, parameters: t.inputSchema,
      }));
      const instructions = `${CHAT_SYSTEM_PROMPT}\n\n${await scopeContext(scope)}`;

      const { reply, trace } = CHAT_PROVIDER === 'ollama'
        ? await runOllama({ history, toolDefs, instructions })
        : await runOpenAI({ history, toolDefs, instructions, apiKey });

      send(res, 200, JSON.stringify({ reply, trace, model: CHAT_MODEL, provider: CHAT_PROVIDER }),
        { 'Content-Type': 'application/json' });
    } catch (err) {
      console.error('chat error:', err);
      send(res, 502, JSON.stringify({ error: String(err.message || err) }),
        { 'Content-Type': 'application/json' });
    }
  });
}

// Lets the viewer tailor the chat UI to the active provider (e.g. hide the
// OpenAI "Key" button when running on a local model).
function handleChatConfig(res) {
  send(res, 200, JSON.stringify({
    provider: CHAT_PROVIDER,
    model: CHAT_MODEL,
    needs_key: CHAT_PROVIDER === 'openai',
  }), { 'Content-Type': 'application/json' });
}

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.geojson': 'application/geo+json; charset=utf-8',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.svg': 'image/svg+xml',
  '.tif': 'image/tiff',
  '.tiff': 'image/tiff',
  '.ico': 'image/x-icon',
  '.obj': 'text/plain; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8',
  '.zip': 'application/zip',
};

function send(res, status, body, headers = {}) {
  res.writeHead(status, { 'Cache-Control': 'no-cache', ...headers });
  res.end(body);
}

// ------------------------------------------------------------ survey upload
// POST /api/survey-upload?name=... — the Survey companion write path
// (docs/survey.md). The raw request body is a zipped QField project folder.
// Durable-drop first: the zip is streamed to data/surveys/incoming/ and a
// line appended to uploads.log.jsonl BEFORE any processing (the same
// Node -> Python handoff as the placements log; the server never touches the
// gpkg). Then scripts/ingest_survey.py --pending is spawned for the
// synchronous attempt; if the Python side is sick the upload is already safe
// on disk and the next `npm run export` ingests it via the log cursor.
// Auth: if a gitignored .survey_token file exists at the repo root, the
// X-Survey-Token header must match. Without the file the route is open —
// meant for the localhost/Tailscale posture, not a public bind.

const SURVEY_MAX_BYTES = 512 * 1024 * 1024;
const SURVEY_INGEST_TIMEOUT_MS = 5 * 60 * 1000;

function surveyToken() {
  try {
    return fs.readFileSync(path.join(ROOT, '.survey_token'), 'utf8').trim();
  } catch (_err) {
    return null;
  }
}

function runSurveyIngest(callback) {
  const py = spawn('python3',
    [path.join(ROOT, 'scripts', 'ingest_survey.py'), '--pending', '--json'],
    { cwd: ROOT, env: { ...process.env, TWIN_DATA_DIR: DATA_DIR } });
  let stdout = '';
  let stderr = '';
  let done = false;
  const finish = (err, summary) => {
    if (done) return;
    done = true;
    callback(err, summary);
  };
  py.stdout.on('data', (c) => { stdout += c; });
  py.stderr.on('data', (c) => { stderr += c; });
  py.on('error', (err) => finish(err.message));
  py.on('close', (code) => {
    if (code !== 0) {
      return finish(`ingest exited ${code}: ${stderr.slice(-400).trim()}`);
    }
    // the JSON summary is the last stdout line (the journal note precedes it)
    const lines = stdout.trim().split('\n').filter(Boolean);
    try {
      finish(null, JSON.parse(lines[lines.length - 1]));
    } catch (_err) {
      finish(`unparseable ingest output: ${stdout.slice(-400).trim()}`);
    }
  });
  setTimeout(() => {
    if (!done) { py.kill(); finish('ingest timed out'); }
  }, SURVEY_INGEST_TIMEOUT_MS);
}

function handleSurveyUpload(req, res, query) {
  const token = surveyToken();
  if (token && req.headers['x-survey-token'] !== token) {
    return send(res, 403, JSON.stringify({ ok: false, error: 'bad survey token' }),
      { 'Content-Type': 'application/json' });
  }
  const name = (query.get('name') || 'survey').slice(0, 80);
  const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '') || 'survey';
  const fileName = `${new Date().toISOString().replace(/[:.]/g, '-')}-${slug}.zip`;
  const incomingDir = path.join(DATA_DIR, 'surveys', 'incoming');
  fs.mkdirSync(incomingDir, { recursive: true });
  const filePath = path.join(incomingDir, fileName);
  const out = fs.createWriteStream(filePath);
  let bytes = 0;
  let failed = false;
  const fail = (status, error) => {
    if (failed) return;
    failed = true;
    out.destroy();
    fs.unlink(filePath, () => {});
    send(res, status, JSON.stringify({ ok: false, error }),
      { 'Content-Type': 'application/json' });
    req.destroy();
  };
  req.on('data', (chunk) => {
    bytes += chunk.length;
    if (bytes > SURVEY_MAX_BYTES) fail(413, 'upload exceeds 512 MB');
  });
  req.on('error', () => fail(400, 'upload interrupted'));
  out.on('error', (err) => fail(500, `could not store upload: ${err.message}`));
  out.on('finish', () => {
    if (failed) return;
    const logLine = JSON.stringify(
      { ts: new Date().toISOString(), file: fileName, name, bytes });
    fs.appendFileSync(path.join(DATA_DIR, 'surveys', 'uploads.log.jsonl'),
      logLine + '\n');
    console.log(`survey upload saved: ${fileName} (${bytes} bytes)`);
    runSurveyIngest((err, summary) => {
      if (err) {
        console.error(`survey ingest failed (upload kept for next export): ${err}`);
        return send(res, 200,
          JSON.stringify({ ok: false, saved: true, file: fileName, error: err }),
          { 'Content-Type': 'application/json' });
      }
      send(res, 200, JSON.stringify({ ok: true, file: fileName, ingest: summary }),
        { 'Content-Type': 'application/json' });
    });
  });
  req.pipe(out);
}

// Persist building-model placements tuned with the in-viewer editor
// (public/viewer/building-editor.js) back into the manifest, and append every
// save to placements.log.jsonl — the handoff that scripts/ingest_placements.py
// turns into twin-store observations (the server itself never touches the gpkg).
function saveBuildingPlacements(req, res) {
  let body = '';
  req.on('data', (chunk) => { body += chunk; });
  req.on('end', () => {
    const modelsDir = path.join(DATA_DIR, 'buildings', 'models');
    const manifestPath = path.join(modelsDir, 'manifest.json');
    try {
      const placements = JSON.parse(body);
      const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
      manifest.buildings.forEach((b) => {
        if (placements[b.id]) b.placement = placements[b.id];
      });
      fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2));
      const logLine = JSON.stringify({ ts: new Date().toISOString(), placements });
      fs.appendFileSync(path.join(modelsDir, 'placements.log.jsonl'), logLine + '\n');
      console.log('saved building placements:', JSON.stringify(placements));
      send(res, 200, 'ok');
    } catch (err) {
      send(res, 400, 'invalid placements: ' + err.message);
    }
  });
}

// Empty out the LLM map drawings (data/annotations.json — written by the
// MCP server's draw_polygon/draw_point tools, polled and rendered orange by
// public/annotations.js). The viewer's "Clear drawings" button posts here;
// writing an empty document (rather than unlinking) keeps the file's shape
// stable for whichever MCP process writes next. Layer-view overrides in the
// same file (set_layer_visibility/filter_layer) are cleared too, so the one
// button also hides any atlas layers the agent revealed — the client's
// follow-up poll applies the now-empty layer_views and resets the drape.
function clearAnnotations(res) {
  const annPath = path.join(DATA_DIR, 'annotations.json');
  try {
    fs.writeFileSync(annPath, JSON.stringify({
      version: 1, updated_at: new Date().toISOString(),
      annotations: [], layer_views: [],
    }, null, 1));
    send(res, 200, JSON.stringify({ ok: true }), { 'Content-Type': 'application/json' });
  } catch (err) {
    send(res, 500, JSON.stringify({ ok: false, error: err.message }),
      { 'Content-Type': 'application/json' });
  }
}

const server = http.createServer((req, res) => {
  let pathname;
  let requestUrl;
  try {
    requestUrl = new URL(req.url, `http://${req.headers.host}`);
    pathname = decodeURIComponent(requestUrl.pathname);
  } catch (_err) {
    return send(res, 400, 'Bad request');
  }

  if (req.method === 'POST' && pathname === '/api/building-placements') {
    return saveBuildingPlacements(req, res);
  }

  if (req.method === 'POST' && pathname === '/api/survey-upload') {
    return handleSurveyUpload(req, res, requestUrl.searchParams);
  }

  if (req.method === 'GET' && pathname === '/api/chat/config') {
    return handleChatConfig(res);
  }

  if (req.method === 'POST' && pathname === '/api/chat') {
    return handleChat(req, res);
  }

  if (req.method === 'POST' && pathname === '/api/annotations/clear') {
    return clearAnnotations(res);
  }

  if (pathname === '/') pathname = '/index.html';

  // Map URL -> file. /data/* serves the twin's data bundle (DATA_DIR, which may
  // be an alternate twin via TWIN_DATA_DIR); everything else serves from /public.
  let filePath;
  let baseDir;
  if (pathname.startsWith('/data/')) {
    baseDir = DATA_DIR;
    filePath = path.normalize(path.join(DATA_DIR, pathname.slice('/data/'.length)));
  } else {
    baseDir = path.join(ROOT, 'public');
    filePath = path.normalize(path.join(ROOT, path.posix.join('/public', pathname)));
  }

  // Prevent path traversal outside the served base directory. The +sep
  // matters: a bare prefix check would also accept sibling directories
  // whose names merely start with the base path (e.g. public2/).
  if (filePath !== baseDir && !filePath.startsWith(baseDir + path.sep)) {
    return send(res, 403, 'Forbidden');
  }

  fs.stat(filePath, (err, stat) => {
    if (err || !stat.isFile()) {
      return send(res, 404, `Not found: ${pathname}`);
    }
    const ext = path.extname(filePath).toLowerCase();
    const type = MIME[ext] || 'application/octet-stream';
    res.writeHead(200, { 'Content-Type': type, 'Content-Length': stat.size, 'Cache-Control': 'no-cache' });
    fs.createReadStream(filePath).pipe(res);
  });
});

server.listen(PORT, HOST, () => {
  console.log(`\n  VEIL digital twin`);
  console.log(`  → http://${HOST}:${PORT}\n`);
});
