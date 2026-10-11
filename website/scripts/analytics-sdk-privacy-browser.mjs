import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { resolve, extname } from 'node:path';
import { gunzipSync } from 'node:zlib';
import { chromium } from 'playwright';

const root = new URL('../dist/', import.meta.url);
const server = createServer(async (req, res) => {
  let path = resolve(new URL(`.${req.url.split('?')[0]}`, root).pathname);
  if (!path.startsWith(new URL(root).pathname)) { res.writeHead(403).end(); return; }
  try { if ((await stat(path)).isDirectory()) path = resolve(path, 'index.html'); const data = await readFile(path); const type = ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' })[extname(path)] ?? 'application/octet-stream'; res.writeHead(200, { 'content-type': type }); res.end(data); }
  catch { res.writeHead(404).end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ headless: true });
try {
  const context = await browser.newContext({ userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36' });
  await context.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => false });
    Object.defineProperty(navigator, 'userAgentData', { get: () => ({ brands: [{ brand: 'Chromium', version: '134' }, { brand: 'Google Chrome', version: '134' }], mobile: false, platform: 'macOS', getHighEntropyValues: async () => ({ brands: [{ brand: 'Chromium', version: '134' }], mobile: false, platform: 'macOS' }) }) });
    localStorage.setItem('ph_phc_qXkp5FBQfrqHQwkqf3ys8iSoGoMYw2tpTHXGugXJhP8V_posthog', JSON.stringify({ distinct_id: 'test-distinct', $set: { $initial_utm_source: 'CANARY_PERSISTED', $session_entry_utm_campaign: 'CANARY_SESSION', $session_entry_gclid: 'CANARY_GCLID' } }));
  });
  const requests = [];
  const network = [];
  await context.route('**/*posthog.com/**', async route => {
    const req = route.request();
    if (req.method() !== 'POST') { console.log('intercepted-posthog-get', req.method(), new URL(req.url()).pathname); await route.continue(); return; }
    if (req.method() === 'POST') {
      const raw = req.postDataBuffer() ?? Buffer.alloc(0);
      let body = raw.toString('utf8');
      try { if (raw[0] === 0x1f && raw[1] === 0x8b) body = gunzipSync(raw).toString('utf8'); } catch {}
      let parsedPayload;
      let eventNames = [];
      try { parsedPayload = JSON.parse(body); eventNames = (Array.isArray(parsedPayload?.batch) ? parsedPayload.batch : Array.isArray(parsedPayload) ? parsedPayload : [parsedPayload]).map(x => x.event).filter(Boolean); } catch {}
      requests.push({ method: req.method(), path: new URL(req.url()).pathname, body, eventNames, payload: parsedPayload });
      console.log('intercepted-posthog-post', req.method(), new URL(req.url()).pathname, JSON.stringify(eventNames));
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{}' });
  });
  await context.route(/https:\/\/us-assets\.i\.posthog\.com\/.*/, async route => route.continue());
  const page = await context.newPage();
  page.on('request', req => network.push(`request:${req.method()}:${req.url()}`));
  page.on('response', res => network.push(`response:${res.status()}:${res.url()}`));
  page.on('requestfailed', req => network.push(`failed:${req.url()}:${req.failure()?.errorText}`));
  page.on('console', msg => console.log('browser-console', msg.type(), msg.text()));
  page.on('requestfailed', req => console.log('request-failed', req.url(), req.failure()?.errorText));
  await page.goto(`${origin}/playground/?utm_source=CANARY_QUERY&gclid=CANARY_QUERY_GCLID#CANARY_HASH`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(5000);
  const init = await page.evaluate(() => ({ config: window.posthog?.config, recording: window.posthog?.config?.disable_session_recording }));
  await page.goto(`${origin}/quick-start/?utm_source=CANARY_NAV&gclid=CANARY_NAV_GCLID#CANARY_NAV_HASH`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(4000);
  const afterNavigation = requests.flatMap(x => (Array.isArray(x.payload?.batch) ? x.payload.batch : Array.isArray(x.payload) ? x.payload : [x.payload]).filter(Boolean));
  const pageviews = afterNavigation.filter(x => x.event === '$pageview');
  assert.ok(pageviews.some(x => x.properties?.$current_url === `${origin}/playground/`), 'decoded SDK pageview must contain canonical first URL');
  assert.ok(pageviews.some(x => x.properties?.$current_url === `${origin}/quick-start/`), 'real second navigation must emit canonical pageview');
  await page.evaluate(() => window.posthog.opt_out_capturing());
  const countAfterOptOut = requests.length;
  await page.evaluate(() => window.posthog.capture('__should_not_send', { sentinel: 'CANARY_OPTOUT' }, { send_instantly: true }));
  await page.waitForTimeout(1000);
  assert.equal(requests.length, countAfterOptOut, 'opted-out SDK must not issue ingestion requests');
  console.log('sdk-state', JSON.stringify(await page.evaluate(() => ({ loaded: window.posthog?.__loaded, capturing: window.posthog?.is_capturing?.(), optedOut: window.posthog?.has_opted_out_capturing?.() }))));
  assert.ok(requests.length, 'real SDK should issue intercepted ingestion requests');
  const full = JSON.stringify(requests);
  for (const token of ['CANARY_QUERY', 'CANARY_QUERY_GCLID', 'CANARY_HASH', 'CANARY_SESSION', 'CANARY_PERSISTED', 'CANARY_GCLID', 'CANARY_NAV']) assert.ok(!full.includes(token), `leaked sentinel ${token}`);
  const events = requests.flatMap(x => x.eventNames);
  assert.ok(events.includes('$pageview'), `expected pageview; received ${JSON.stringify(events)}`);
  assert.ok(requests.some(x => x.path === '/e/'), `expected /e/ ingestion; received ${JSON.stringify(requests.map(x => x.path))}`);
  console.log(JSON.stringify({ intercepted: requests.length, events, init, payloadUrlsClean: requests.every(r => !/CANARY/.test(r.path)), bodiesClean: !/CANARY/.test(full) }, null, 2));
  await context.close();
} finally { await browser.close(); server.close(); }
