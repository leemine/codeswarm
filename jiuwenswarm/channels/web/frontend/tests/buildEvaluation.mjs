import { build } from 'esbuild';
await build({
  entryPoints: ['../../../extensions/evaluation/frontend/EvaluationApp.tsx', 'src/services/webClient.ts'],
  jsx: 'automatic',
  bundle: true,
  splitting: true,
  packages: 'external',
  external: ['react', 'react/*', 'react-i18next'],
  platform: 'node',
  format: 'esm',
  outdir: 'node_modules/.cache/evaluation',
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
  define: { 'import.meta.env': '{}' },
  plugins: [
    {
      name: 'test-icons',
      setup(builder) {
        builder.onResolve({ filter: /\.svg\?react$/ }, ({ path }) => ({ path, namespace: 'icon' }));
        builder.onLoad({ filter: /.*/, namespace: 'icon' }, () => ({
          contents: 'export default function Icon() { return null; }',
        }));
      },
    },
  ],
});
await build({
  entryPoints: ['src/applicationPlugins/ExperimentsContainer.tsx'],
  jsx: 'automatic',
  bundle: true,
  packages: 'external',
  external: ['react', 'react/*'],
  platform: 'node',
  format: 'esm',
  outfile: 'node_modules/.cache/evaluation-container/ExperimentsContainer.mjs',
  loader: { '.css': 'empty' },
  plugins: [
    {
      name: 'host-outlets',
      setup(builder) {
        builder.onResolve({ filter: /RsiPage|ApplicationPluginOutlet/ }, ({ path }) => ({
          path,
          namespace: 'test-outlet',
        }));
        builder.onLoad({ filter: /.*/, namespace: 'test-outlet' }, ({ path }) => ({
          loader: 'js',
          contents: path.includes('RsiPage')
            ? `import React from 'react'; export function RsiPage() { return React.createElement('div',{'data-testid':'original-rsi-outlet'},'original RSI'); }`
            : `import React from 'react'; export function ApplicationPluginOutlet({contribution}) { return React.createElement('div',{'data-testid':'plugin-outlet'},contribution.nav_key); }`,
        }));
      },
    },
  ],
});
await build({
  entryPoints: ['src/applicationPlugins/useApplicationPlugins.ts'],
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  outfile: 'node_modules/.cache/evaluation-container/useApplicationPlugins.mjs',
});
