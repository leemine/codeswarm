import { build } from 'esbuild';
import { spawnSync } from 'node:child_process';
await build({
  entryPoints: [
    'src/components/ArtifactsPanel/index.tsx',
    'src/components/ArtifactsPanel/ArtifactOwnerContext.tsx',
    'src/components/ArtifactsPanel/ownerDownload.ts',
    'src/services/webClient.ts',
    'src/stores/index.ts',
  ],
  bundle: true,
  splitting: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  outbase: 'src',
  outdir: 'node_modules/.cache/owner-artifact-download',
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
  define: { 'import.meta.env': '{}' },
  plugins: [
    {
      name: 'decorative-svg',
      setup(builder) {
        builder.onResolve({ filter: /\.svg\?react$/ }, ({ path }) => ({ path, namespace: 'svg-stub' }));
        builder.onLoad({ filter: /.*/, namespace: 'svg-stub' }, () => ({
          contents: 'export default function SvgStub() { return null; }',
          loader: 'js',
        }));
      },
    },
  ],
});
const result = spawnSync(process.execPath, ['tests/ownerArtifactDownload.test.mjs'], { stdio: 'inherit' });
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
