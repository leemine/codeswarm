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
