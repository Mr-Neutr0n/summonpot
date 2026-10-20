import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

for (const [name, path] of [
  ['homepage', '../dist/index.html'],
  ['operation reference', '../dist/reference/operations/index.html'],
]) {
  test(`${name} states the 0.11.0 chain boundary without stale publication claims`, async () => {
    const html = await readFile(new URL(path, import.meta.url), 'utf8');
    assert.match(html, /0\.11\.0/);
    assert.doesNotMatch(html, /not yet published|Unreleased source/);
    assert.match(html, /agent-mediated/);
    assert.match(html, /128/);
    assert.match(html, /65,536/);
  });
}
