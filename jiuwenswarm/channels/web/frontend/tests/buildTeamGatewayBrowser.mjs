import { build } from 'esbuild';
import { readFile } from 'node:fs/promises';
import { transform } from '@svgr/core';
import { resolve, dirname } from 'node:path';

await build({
  entryPoints: ['tests/teamGatewayBrowser.entry.tsx'],
  bundle: true,
  jsx: 'automatic',
  format: 'esm',
  platform: 'browser',
  target: 'chrome107',
  define: { 'import.meta.env': '{"DEV":false}' },
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl', '.woff2': 'dataurl' },
  outfile: process.argv[2],
  plugins: [
    {
      name: 'fixture-svg',
      setup(builder) {
        builder.onResolve({ filter: /\.svg\?react$/ }, ({ path, resolveDir }) => ({
          path: resolve(resolveDir, path.replace(/\?react$/, '')),
          namespace: 'fixture-svg',
        }));
        builder.onLoad({ filter: /.*/, namespace: 'fixture-svg' }, async ({ path }) => ({
          contents: await transform(await readFile(path.replace(/\?react$/, ''), 'utf8'), {
            plugins: ['@svgr/plugin-jsx'],
            jsxRuntime: 'automatic',
          }),
          loader: 'jsx',
          resolveDir: dirname(path),
        }));
      },
    },
  ],
});
