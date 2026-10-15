/* Offline probes of the actual TS sources. No Worker deployment or cloud calls.
 * First: (cd packages/edge-worker/worker && npm ci)
 * Then: node docs/audits/2026-10-15-round3/reproduce-worker.cjs
 * Assertions describe vulnerable behavior, not secure behavior.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {createRequire} = require('node:module');
const root = path.resolve(__dirname, '../../..');
const worker = path.join(root, 'packages/edge-worker/worker');
const ts = createRequire(path.join(worker, 'package.json'))('typescript');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ge-audit-worker-'));

(async () => {
  try {
    for (const name of ['text', 'db', 'keys', 'dialect', 'index']) {
      const source = fs.readFileSync(path.join(worker, 'src', name + '.ts'), 'utf8');
      fs.writeFileSync(path.join(tmp, name + '.js'), ts.transpileModule(source, {
        compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}
      }).outputText);
    }
    const {Hrana} = require(path.join(tmp, 'db.js'));
    const {Site} = require(path.join(tmp, 'index.js'));
    const seen = [];
    const fake = async (url, opts) => {
      seen.push({url, opts});
      return new Response(JSON.stringify({results: [
        {type: 'ok', response: {result: {cols: [{name:'one'}], rows: [[{type:'integer', value:'1'}]]}}},
        {type: 'ok', response: {}}
      ]}), {headers: {'Content-Type': 'application/json'}});
    };
    await new Hrana('https://localhost.db.invalid', 'FAKE-audit-token', fake).execute('SELECT 1');
    assert.equal(seen[0].url, 'http://localhost.db.invalid/v2/pipeline');
    assert.equal(seen[0].opts.headers.Authorization, 'Bearer FAKE-audit-token');
    process.stdout.write('CONFIRMED: TS non-loopback HTTPS downgraded; fake Bearer attached to HTTP request\n');

    for (const id of ['source:123', 'source.123']) {
      let queries = 0;
      const site = new Site({execute: async () => { queries++; throw Error('route should not reach DB'); }});
      const response = await site.handle(new Request('https://edge.invalid/work/' + id));
      assert.equal(response.status, 404);
      assert.equal(queries, 0);
    }
    process.stdout.write('CONFIRMED: TS detail routes reject pack-valid IDs containing colon or dot\n');

    const w = {id: 'audit-1', title: 'Audit', cover_key: 'audit.svg', pack_id: 'ge-audit'};
    const db = {execute: async () => ({columns: Object.keys(w), rows: [Object.values(w)]})};
    const svg = '<svg xmlns="http://www.w3.org/2000/svg" onload="document.documentElement.setAttribute(\'data-audit\',\'executed\')"/>';
    const site = new Site(db, undefined, false, false, 'https://images.invalid',
      async () => new Response(svg, {headers: {'Content-Type': 'image/svg+xml'}}));
    const response = await site.handle(new Request('https://edge.invalid/cover/audit-1.svg'));
    assert.equal(response.status, 200);
    assert.equal(await response.text(), svg);
    assert.match(response.headers.get('Content-Security-Policy'), /script-src 'unsafe-inline'/);
    assert.doesNotMatch(response.headers.get('Content-Security-Policy'), /sandbox/);
    process.stdout.write('OBSERVED: upstream SVG active content returned with inline-script CSP; browser execution not tested\n');
  } finally {
    fs.rmSync(tmp, {recursive: true, force: true});
  }
})().catch(err => { process.stderr.write(String(err) + '\n'); process.exitCode = 1; });
