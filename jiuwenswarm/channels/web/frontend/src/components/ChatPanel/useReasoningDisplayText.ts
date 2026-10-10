import { useEffect, useRef, useState } from 'react';

// Keep the complete source in the store; only coalesce the expensive text layout.
const DISPLAY_INTERVAL_MS = 100;

export function useReasoningDisplayText(id: string, text: string, closed: boolean): string {
  const latest = useRef({ id, text });
  latest.current = { id, text };
  const [displayed, setDisplayed] = useState(latest.current);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (closed || displayed.id !== id) {
      if (timer.current !== null) clearTimeout(timer.current);
      timer.current = null;
      if (displayed.id !== id || displayed.text !== text) setDisplayed(latest.current);
    } else if (displayed.text !== text && timer.current === null) {
      timer.current = setTimeout(() => {
        timer.current = null;
        setDisplayed(latest.current);
      }, DISPLAY_INTERVAL_MS);
    }
  }, [id, text, closed, displayed]);

  useEffect(
    () => () => {
      if (timer.current !== null) clearTimeout(timer.current);
    },
    [],
  );

  // Terminal/history content and a newly mounted segment must never show a stale tail.
  return closed || displayed.id !== id ? text : displayed.text;
}
