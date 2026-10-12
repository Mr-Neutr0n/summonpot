import assert from 'node:assert/strict';
import { sanitizeAnalyticsPayload } from '../src/analytics/posthog.js';

const input = {
  event: '$pageview',
  properties: {
    $current_url: 'https://summonpot.test/playground/?customer_id=CANARY_QUERY#CANARY_HASH',
    $initial_url: 'https://summonpot.test/playground/?secret=CANARY_INITIAL',
    $referrer: 'https://source.test/docs/?private=CANARY_REFERRER#frag',
    $referring_domain: 'source.test',
    $set: { $initial_referrer: 'https://source.test/?token=CANARY_SET', $initial_referring_domain: 'source.test', arbitrary_utm: 'CANARY_UTM' },
    $set_once: { $initial_url: 'https://summonpot.test/?secret=CANARY_ONCE', $initial_utm_source: 'CANARY_INITIAL_UTM', $session_entry_utm_source: 'CANARY_SESSION_UTM', $session_entry_gclid: 'CANARY_SESSION_GCLID', gad_source: 'CANARY_GAD', mc_cid: 'CANARY_MC', igshid: 'CANARY_IG', rdt_cid: 'CANARY_RDT', _kx: 'CANARY_KX' },
    destination: 'https://example.test/path?customer=CANARY_DEST#frag',
    timestamp: new Date('2026-01-01T00:00:00Z'),
    $browser: 'Chrome',
    token: 'phc_public_sdk_token',
    uuid: 'sdk-uuid',
    distinct_id: 'sdk-distinct',
    '$session_id': 'sdk-session',
    arbitrary_url: 'https://example.test/path?secret=CANARY_ANY_URL#frag',
    '$autocapture': null,
  },
};
const result = sanitizeAnalyticsPayload(input);
const serialized = JSON.stringify(result);
for (const forbidden of ['CANARY_QUERY', 'CANARY_HASH', 'CANARY_INITIAL', 'CANARY_REFERRER', 'CANARY_SET', 'CANARY_UTM', 'CANARY_ONCE', 'CANARY_INITIAL_UTM', 'CANARY_SESSION_UTM', 'CANARY_SESSION_GCLID', 'CANARY_GAD', 'CANARY_MC', 'CANARY_IG', 'CANARY_RDT', 'CANARY_KX', 'CANARY_DEST', 'secret=', 'private=', 'token=']) {
  assert.equal(serialized.includes(forbidden), false, `must remove ${forbidden} from complete envelope`);
}
assert.equal(result.properties.$current_url, 'https://summonpot.test/playground/');
assert.equal(result.properties.$initial_url, 'https://summonpot.test/playground/');
assert.equal(result.properties.$referrer, 'https://source.test');
assert.equal(result.properties.$referring_domain, 'source.test');
assert.equal(result.properties.$set.$initial_referrer, 'https://source.test');
assert.equal(result.properties.$set.$initial_referring_domain, 'source.test');
assert.equal(result.properties.$set_once.$initial_url, 'https://summonpot.test/');
assert.equal(result.properties.timestamp, input.properties.timestamp);
assert.equal(result.properties.token, 'phc_public_sdk_token');
assert.equal(result.properties.uuid, 'sdk-uuid');
assert.equal(result.properties.distinct_id, 'sdk-distinct');
assert.equal(result.properties.$session_id, 'sdk-session');
assert.equal(result.properties.arbitrary_url, 'https://example.test/path');
assert.equal(result.properties.$autocapture, null);
import { posthogScript } from '../src/analytics/posthog.js';
assert.ok(posthogScript.includes(`before_send:(${sanitizeAnalyticsPayload.toString()})`), 'rendered SDK config must use the exact tested function source');
console.log('Analytics envelope sanitizer: canonical URLs, nested acquisition, metadata, Date');

// Real CDN SDK proof lives in analytics-sdk-privacy-browser.mjs; keep the
// fast deterministic unit regression runnable without a browser installation.
