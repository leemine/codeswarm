// Synthetic component diagnostic, not a Provider run or a benchmark score.
import { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import '../../../src/i18n';
import { ChatTimelineList } from '../../../src/components/ChatPanel/MessageList';
import { useChatStore } from '../../../src/stores/chatStore';
import '../../../src/styles/foundation.css';
import '../../../src/styles/themes/default/light.css';
import '../../../src/index.css';
const id = 'synthetic-runtime-diagnostic';
const start = Date.now();
const line = 'reasoning diagnostic line\n';
const messages = [{ id: 'input', role: 'user' as const, content: 'Synthetic long reasoning; no model request.', timestamp: new Date(start).toISOString() }];
function Fixture() {
  const [running, setRunning] = useState(false);
  const [length, setLength] = useState(221501);
  const [clicks, setClicks] = useState(0);
  const [lag, setLag] = useState(0);
  const maxLag = useRef(0);
  useEffect(() => { useChatStore.getState().setProcessing(id, running); }, [running]);
  useEffect(() => {
    if (!running) return;
    let previous = performance.now();
    const timer = window.setInterval(() => {
      const now = performance.now();
      maxLag.current = Math.max(maxLag.current, now - previous - 20);
      previous = now;
      setLength(n => n + 80);
      setLag(Math.round(maxLag.current));
    }, 20);
    return () => window.clearInterval(timer);
  }, [running]);
  return <main>
    <h1>Synthetic diagnostic — original Code timeline component</h1>
    <p>No model call, no experiment result. Initial text matches the largest recorded reasoning length.</p>
    <button onClick={() => setRunning(v => !v)}>{running ? 'Stop stream' : 'Start stream'}</button>
    <button onClick={() => setClicks(n => n + 1)}>Probe interaction</button>
    <output>characters={length}; clicks={clicks}; max timer lag={lag}ms; running={String(running)}</output>
    <ChatTimelineList messages={messages} sessionId={id} mode="code" reasoningSegments={[{
      id: 'reasoning', text: line.repeat(Math.ceil(length / line.length)).slice(0,length),
      startedAt: start + 1, updatedAt: Date.now(), closed: false,
    }]} />
  </main>;
}
createRoot(document.getElementById('root')!).render(<Fixture />);
