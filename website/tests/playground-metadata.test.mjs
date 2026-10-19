import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const html=await readFile(new URL('../dist/playground/index.html',import.meta.url),'utf8');
test('standalone playground publishes one canonical and complete sharing metadata',()=>{
 const canonical=html.match(/<link\b[^>]*rel="canonical"[^>]*>/g)??[];
 assert.equal(canonical.length,1);
 assert.match(canonical[0],/href="https:\/\/summonpot\.modepot\.io\/playground\/"/);
 for(const property of ['og:type','og:title','og:description','og:url','og:image']){
  assert.equal((html.match(new RegExp(`property="${property}"`,'g'))??[]).length,1,property);
 }
 for(const name of ['twitter:card','twitter:title','twitter:description','twitter:image']){
  assert.equal((html.match(new RegExp(`name="${name}"`,'g'))??[]).length,1,name);
 }
 assert.match(html,/<meta\b[^>]*property="og:url"[^>]*content="https:\/\/summonpot\.modepot\.io\/playground\/"/);
 assert.match(html,/<meta\b[^>]*name="twitter:card"[^>]*content="summary_large_image"/);
 assert.match(html,/<meta\b[^>]*property="og:image"[^>]*content="https:\/\/summonpot\.modepot\.io\/social-card-v2\.png"/);
 assert.match(html,/<link\b[^>]*rel="icon"[^>]*href="\/favicon\.svg"/);
});
