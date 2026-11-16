// FE 조판 모듈(TypeScript)을 타입만 지운 ES 모듈로 옮긴다 — typeset_browser_measure.py 가 부른다. 개발용, 운영 경로 아님.
//
//   node strip_fe_modules.mjs <FE 체크아웃 루트> <출력 폴더>
//
// 코드 본문은 바꾸지 않는다: Node 내장 `module.stripTypeScriptTypes`(mode: 'strip')는 타입 표기만 공백으로 지우고 위치를 보존한다.
// 바꾸는 것은 import 경로의 확장자(`./x.ts` → `./x.js`)뿐이다. 타입 전용 import(`import type …`)는 strip 이 지운다.
// 지운 뒤 남은 값 import 가 옮긴 모듈 밖을 가리키면 실패한다(앱 셸 · React 를 끌어오지 않는다).
// 출력: <출력 폴더>/<모듈>.js 와 manifest.json(원본 경로 · SHA-256 · 출력 SHA-256 · Node 버전).
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import module from 'node:module';
import { join } from 'node:path';

export const MODULES = ['typeset-rules', 'typeset-measurement', 'typeset-final-output', 'layout-edit'];

const [, , feRoot, outDir] = process.argv;
if (!feRoot || !outDir) {
  console.error('usage: node strip_fe_modules.mjs <FE root> <out dir>');
  process.exit(2);
}
mkdirSync(outDir, { recursive: true });
const sha = (s) => createHash('sha256').update(s).digest('hex');
const manifest = { node: process.version, modules: {} };
for (const name of MODULES) {
  const rel = `lib/n5/${name}.ts`;
  const src = readFileSync(join(feRoot, rel), 'utf8');
  let js = module.stripTypeScriptTypes(src, { mode: 'strip' });
  js = js.replace(/(from\s+['"]\.\/[\w-]+)\.ts(['"])/g, '$1.js$2');
  for (const m of js.matchAll(/^\s*import\s[^;]*?from\s+['"]([^'"]+)['"]/gm)) {
    const target = m[1].replace(/^\.\//, '').replace(/\.js$/, '');
    if (!m[1].startsWith('./') || !MODULES.includes(target)) {
      console.error(`${rel}: 옮기지 않는 모듈을 값으로 import 한다 — ${m[1]}`);
      process.exit(1);
    }
  }
  writeFileSync(join(outDir, `${name}.js`), js);
  manifest.modules[name] = { source: rel, source_sha256: sha(src), output_sha256: sha(js) };
}
writeFileSync(join(outDir, 'manifest.json'), JSON.stringify(manifest, null, 1));
console.log(JSON.stringify(manifest));
