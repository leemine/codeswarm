import { build } from 'esbuild';
await build({
  entryPoints: [
    'src/multi-session/sidebar/ConversationSidebar.tsx',
    'src/features/workspace/archivedTaskClient.ts',
    'src/stores/workspaceStore.ts',
    'src/stores/cronStore.ts',
    'src/components/ui/Toast/toastStore.ts',
    'src/multi-session/state/useSideConversationDeletion.ts',
  ],
  bundle: true,
  splitting: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  outdir: 'node_modules/.cache/single-session-delete',
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
