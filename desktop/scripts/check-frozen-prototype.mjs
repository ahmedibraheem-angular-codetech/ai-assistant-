// The Tauri window runs with `freezePrototype: true` (tauri.conf.json): a
// security setting that makes Object.prototype read-only. A library that writes
// to it at load time stops the whole app before any of our code runs, with no
// visible error. This check loads every runtime dependency under the same
// freeze. Run it after adding a package: npm run check:freeze
//
// Example of what it catches: echarts fails with "Cannot assign to read only
// property 'constructor'", which is why the dashboard draws its own SVG charts.

import { readFileSync } from 'node:fs';

Object.freeze(Object.prototype);

const { dependencies } = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'));
const skip = new Set(['tslib']); // helper library, loaded through the others
const extra = ['@tauri-apps/api/core']; // entry points we import directly

// Angular's packages are partly compiled and need the compiler loaded first
// when imported straight into Node (the app build does this ahead of time).
const first = ['@angular/compiler'];
const names = Object.keys(dependencies).filter((n) => !skip.has(n) && !first.includes(n) && !n.startsWith('@fontsource/'));

let failed = 0;
for (const name of [...first, ...names, ...extra]) {
  try {
    await import(name);
    console.log(`ok    ${name}`);
  } catch (error) {
    failed += 1;
    console.log(`FAIL  ${name}: ${String(error.message).slice(0, 200)}`);
  }
}
if (failed) {
  console.error(`\n${failed} package(s) break under Tauri's frozen prototype.`);
  process.exit(1);
}
