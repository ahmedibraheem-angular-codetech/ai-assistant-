// Guards the production build against styles that the app's CSP would block.
// Run after `npm run build`: `npm run check:csp`.
//
// 1. index.html must have no inline event handlers (onload=...). Angular's
//    critical-CSS inlining loads styles.css with onload="this.media='all'",
//    which `script-src 'self'` blocks, so the page stays unstyled.
// 2. Tauri must not add nonces to style-src. A nonce makes the browser ignore
//    'unsafe-inline', and Angular injects component styles as <style> tags.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const root = new URL('..', import.meta.url);
const read = (path) => readFileSync(fileURLToPath(new URL(path, root)), 'utf8');
const problems = [];

const html = read('dist/desktop/browser/index.html');
const handler = html.match(/\son[a-z]+\s*=/i);
if (handler) problems.push(`index.html has an inline event handler (${handler[0].trim()}).`);
if (/media=["']print["']/.test(html)) problems.push('index.html loads a stylesheet with media="print".');

const config = JSON.parse(read('src-tauri/tauri.conf.json'));
const security = config.app?.security ?? {};
const disabled = security.dangerousDisableAssetCspModification;
if (!(disabled === true || (Array.isArray(disabled) && disabled.includes('style-src')))) {
  problems.push('tauri.conf.json must list "style-src" in dangerousDisableAssetCspModification.');
}
if (!/style-src[^;]*'unsafe-inline'/.test(security.csp ?? '')) {
  problems.push("The production CSP must allow 'unsafe-inline' in style-src.");
}

if (problems.length) {
  console.error('CSP check failed:\n- ' + problems.join('\n- '));
  process.exit(1);
}
console.log('CSP check passed.');
