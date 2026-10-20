import { build } from 'esbuild';
import { mkdir, copyFile, readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
await mkdir('dist', { recursive: true });
for (const file of ['index.html', 'style.css', 'audio-worklet.js', 'ops.html', 'ops.css', 'ops.js']) await copyFile(`frontend/${file}`, `dist/${file}`);
await build({ entryPoints: ['frontend/app.js'], bundle: true, outfile: 'dist/app.js', format: 'esm', platform: 'browser', target: 'es2022', minify: true });
const version = createHash('sha256').update(await readFile('dist/app.js')).digest('hex').slice(0, 16);
await writeFile('dist/index.html', (await readFile('dist/index.html', 'utf8')).replace('src="/app.js"', `src="/app.js?v=${version}"`));
