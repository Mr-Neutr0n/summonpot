import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { chromium } from 'playwright';

const root = resolve(import.meta.dirname, '../..');
const source = resolve(root, 'docs/assets/validated-producer-choice.svg');
const svg = await readFile(source, 'utf8');
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
  await page.setContent(`<html><body style="margin:0;width:1440px;height:900px">${svg}</body></html>`, { waitUntil: 'load' });
  const image = page.locator('svg');
  await image.evaluate(element => document.fonts.ready);
  const bounds = await image.locator('text').evaluateAll(nodes => nodes.map(node => {
    const box = node.getBBox();
    return { text: node.textContent.trim(), x: box.x, y: box.y, right: box.x + box.width, bottom: box.y + box.height };
  }));
  const outside = bounds.filter(box => box.x < 0 || box.y < 0 || box.right > 1440 || box.bottom > 900);
  if (outside.length) throw new Error(`SVG text outside viewBox: ${JSON.stringify(outside)}`);
  for (const path of ['docs/assets/validated-producer-choice.png', 'website/public/diagrams/validated-producer-choice.png']) {
    await image.screenshot({ path: resolve(root, path), animations: 'disabled' });
    console.log(`Rendered ${path}`);
  }
} finally {
  await browser.close();
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  // The script intentionally writes only the new diagram derivatives.
}
