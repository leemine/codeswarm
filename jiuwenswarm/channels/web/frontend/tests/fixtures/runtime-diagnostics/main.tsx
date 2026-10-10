// Component replay only: no Provider request and no benchmark result.
import { Profiler, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import '../../../src/i18n';
import { ChatTimelineList } from '../../../src/components/ChatPanel/MessageList';
import { useChatStore } from '../../../src/stores/chatStore';
import '../../../src/styles/foundation.css';
import '../../../src/styles/themes/default/light.css';
import '../../../src/index.css';
const id = 'synthetic-runtime-diagnostic';
const start = Date.now();
const messages = [
  {
    id: 'input',
    role: 'user' as const,
    content: 'Component replay; no model request.',
    timestamp: new Date(start).toISOString(),
  },
];
useChatStore.getState().ensureRuntime(id);
useChatStore.getState().setActiveSessionId(id);
function Fixture() {
  const [sample, setSample] = useState('reasoning diagnostic line\n'.repeat(10000));
  const [running, setRunning] = useState(false);
  const [clicks, setClicks] = useState(0);
  const [metrics, setMetrics] = useState('');
  const stats = useRef({ maxLag: 0, maxCommit: 0, longTasks: 0, maxLongTask: 0, ticks: 0 });
  useEffect(() => {
    const observer = new PerformanceObserver((entries) => {
      for (const entry of entries.getEntries()) {
        stats.current.longTasks += 1;
        stats.current.maxLongTask = Math.max(stats.current.maxLongTask, entry.duration);
      }
    });
    observer.observe({ entryTypes: ['longtask'] });
    const timer = window.setInterval(() => {
      const body = document.querySelector('[data-testid="chat-panel-reasoning-panel-body"]');
      setMetrics(
        JSON.stringify({
          ...stats.current,
          renderedCharacters: body?.textContent?.length ?? 0,
          sourceCharacters: useChatStore.getState().getRuntime(id)?.reasoningSegments.at(-1)?.text.length ?? 0,
        }),
      );
    }, 500);
    return () => {
      observer.disconnect();
      window.clearInterval(timer);
    };
  }, []);
  useEffect(() => {
    if (!running) return;
    let offset = 25000;
    let previous = performance.now();
    useChatStore.getState().removeRuntime(id);
    useChatStore.getState().ensureRuntime(id);
    useChatStore.getState().setProcessing(id, true);
    useChatStore.getState().appendReasoning(id, sample.slice(0, offset), { atMs: start + 1 });
    const timer = window.setInterval(() => {
      const now = performance.now();
      stats.current.maxLag = Math.max(stats.current.maxLag, now - previous - 20);
      stats.current.ticks += 1;
      previous = now;
      const chunk = sample.slice(offset % sample.length, (offset % sample.length) + 160);
      offset += chunk.length;
      useChatStore.getState().appendReasoning(id, chunk);
    }, 20);
    return () => window.clearInterval(timer);
  }, [running, sample]);
  return (
    <main>
      <h1>Original Code timeline — streaming replay</h1>
      <p>Component diagnostics only. The scroll root and store subscription match the chat timeline.</p>
      <button
        data-testid="chat-panel-diagnostic-load"
        onClick={async () => setSample(await (await fetch('./captured.json')).json())}
      >
        Load captured reasoning
      </button>
      <button data-testid="chat-panel-diagnostic-stream" onClick={() => setRunning((v) => !v)}>
        {running ? 'Stop stream' : 'Start stream'}
      </button>
      <button data-testid="chat-panel-diagnostic-probe" onClick={() => setClicks((n) => n + 1)}>
        Probe interaction
      </button>
      <output data-testid="chat-panel-diagnostic-metrics">
        sample={sample.length}; clicks={clicks}; running={String(running)}; {metrics}
      </output>
      <div
        data-timeline-scroll-root
        data-testid="chat-panel-diagnostic-scroll"
        style={{ width: 780, maxWidth: '100%', height: 480, overflowY: 'auto', position: 'relative' }}
      >
        <Profiler
          id="timeline"
          onRender={(_id, _phase, duration) => {
            stats.current.maxCommit = Math.max(stats.current.maxCommit, duration);
          }}
        >
          <ChatTimelineList messages={messages} sessionId={id} mode="code" />
        </Profiler>
      </div>
    </main>
  );
}
createRoot(document.getElementById('root')!).render(<Fixture />);
