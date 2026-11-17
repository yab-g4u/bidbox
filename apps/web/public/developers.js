'use strict';
const $ = s => document.querySelector(s);
const E = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const pre = text => `<pre class="doc-pre"><code>${E(text)}</code></pre>`;
const ORIGIN = location.origin;
const PAGES = {
  '': ['Quickstart', () => `
    <h2>Quickstart</h2>
    <p>BidBox is a FastAPI web service plus an MCP server over the same downloaded OCDS records. Python 3.12 or newer.</p>
    ${pre('uv venv --python 3.12 .venv\nuv pip install --python .venv/bin/python -e ".[dev]"\n\n# download live Kenya PPIP records into data/bidbox.duckdb\n.venv/bin/bidbox ingest --fy 2026-2027\n\n# web app and REST API\n.venv/bin/uvicorn bidbox.web:app --port 8000')}
    <h3>Model</h3>
    <p>Reviews call an OpenAI-compatible endpoint. Set the key in the host environment, never in a file.</p>
    ${pre('LLM_BASE_URL=https://api.groq.com/openai/v1\nLLM_MODEL=qwen/qwen3.8-27b\nLLM_API_KEY=<from your secret store>')}
    <h3>MCP server</h3>
    <p>The MCP server runs over stdio. It exposes the review tools (search_awards, inspect_tender, investigate_supplier, benchmark_award, flag_finding, create_investigation_file) plus one tool per installed connector action.</p>
    ${pre('{\n  "mcpServers": {\n    "bidbox": {\n      "command": "/path/to/bidbox/.venv/bin/bidbox-mcp",\n      "env": { "BIDBOX_DB_PATH": "/path/to/bidbox/data/bidbox.duckdb" }\n    }\n  }\n}')}
    <p>Inspect it with <code>make inspect</code>.</p>`],
  manifest: ['Manifest', async () => {
    const list = await api('/api/integrations'), items = Array.isArray(list) ? list : list.integrations || [];
    const name = new URLSearchParams(location.search).get('name') || 'ocds_source';
    let yaml = ''; try { yaml = (await api(`/api/connectors/${encodeURIComponent(name)}/manifest`)).yaml; } catch (e) { yaml = e.message; }
    return `<h2>Connector manifest</h2>
    <p>A connector is a folder under <code>bidbox/connectors/&lt;name&gt;/</code> with a <code>connector.yaml</code> and a Python class. Fields:</p>
    <div class="card doc"><table>
      <tr><th>name</th><td>Folder name; lowercase letters, digits, underscores.</td></tr>
      <tr><th>kind</th><td>The connector family, for example data_source, messaging, payments, records, registry or file_drop.</td></tr>
      <tr><th>target_mode</th><td>live or sandbox: what the connector is meant to reach. The status badge shows what was verified.</td></tr>
      <tr><th>credentials</th><td>Names of environment variables only. Never values.</td></tr>
      <tr><th>data_class</th><td>public or personal. Personal connectors stay off until the host enables them.</td></tr>
      <tr><th>actions</th><td>Each action has a name, description, consequential flag and JSON input schema. Consequential actions need a named approval.</td></tr>
      <tr><th>limits</th><td>timeout_s, retries, rate_per_min.</td></tr>
      <tr><th>field_allowlist</th><td>Fields allowed to leave BidBox.</td></tr>
    </table></div>
    <h3>Installed manifest: ${E(name)}</h3>
    <p>${items.map(c => `<a href="/developers/manifest?name=${encodeURIComponent(c.name)}">${E(c.name)}</a>`).join(' · ')}</p>
    ${pre(yaml)}`;
  }],
  cli: ['CLI', () => `
    <h2>Command line</h2>
    <div class="card doc"><table>
      <tr><th><code>bidbox ingest [source] [--fy FY | --all-fy] [--source-url URL] [--refresh]</code></th><td>Import real OCDS JSON, JSONL or gzip from a publisher URL or local file. Upserts into DuckDB.</td></tr>
      <tr><th><code>bidbox measure</code></th><td>Measure field coverage of the downloaded live data.</td></tr>
      <tr><th><code>bidbox connector new &lt;name&gt;</code></th><td>Scaffold a connector folder from a template.</td></tr>
      <tr><th><code>bidbox connector test &lt;name&gt;</code></th><td>Health check plus one sample read.</td></tr>
      <tr><th><code>bidbox-mcp</code></th><td>Run the MCP server over stdio.</td></tr>
      <tr><th><code>bidbox-approve &lt;run_id&gt; --approver NAME --role ROLE</code></th><td>Record a named approval from a terminal.</td></tr>
      <tr><th><code>bidbox-download</code></th><td>Download publisher data.</td></tr>
      <tr><th><code>bidbox-eval</code></th><td>Run the evaluation suite.</td></tr>
    </table></div>`],
  api: ['API reference', async () => {
    const spec = await api('/openapi.json');
    const rows = Object.entries(spec.paths || {}).flatMap(([path, ops]) => Object.entries(ops).map(([method, op]) => `<tr><td><span class="method">${E(method.toUpperCase())}</span></td><td><code>${E(path)}</code></td><td>${E(op.summary || '')}</td></tr>`));
    return `<h2>API reference</h2><p>Rendered from <a href="/openapi.json">/openapi.json</a> (${E(spec.info?.title)} ${E(spec.info?.version)}).</p><div class="card doc"><table><tr><th>Method</th><th>Path</th><th>Summary</th></tr>${rows.join('')}</table></div>`;
  }],
  samples: ['Samples', async () => {
    let yaml = ''; try { yaml = (await api('/api/connectors/rest_connector/manifest')).yaml; } catch (e) { yaml = e.message; }
    return `<h2>Samples</h2>
    <h3>connector.yaml (installed rest_connector)</h3>${pre(yaml)}
    <h3>curl</h3>
    ${pre(`# search downloaded records\ncurl "${ORIGIN}/api/data/search?q=tyres"\n\n# start a price and supplier review on one exact process\ncurl -X POST "${ORIGIN}/api/cases" -H "content-type: application/json" \\\n  -d '{"ocid": "<ocid from search>", "mode": "both"}'\n\n# follow the run\ncurl "${ORIGIN}/api/runs/<run_id>"\ncurl -N "${ORIGIN}/api/runs/<run_id>/events"\n\n# data source status\ncurl "${ORIGIN}/api/data/status"`)}`;
  }],
};
async function api(url) {
  const response = await fetch(url);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `The server returned status ${response.status}.`);
  return body;
}
async function route() {
  const page = location.pathname.replace(/^\/developers\/?/, '').replace(/\/$/, '');
  const [label, render] = PAGES[page] || PAGES[''];
  document.querySelectorAll('#space-nav a').forEach(a => a.dataset.nav === page ? a.setAttribute('aria-current', 'page') : a.removeAttribute('aria-current'));
  $('#crumb-page').textContent = label; document.title = `${label} · Developer docs · BidBox`;
  $('#doc').className = 'doc';
  try { $('#doc').innerHTML = await render(); } catch (error) { $('#doc').innerHTML = `<p class="er">${E(error.message)}</p>`; }
}
route();
