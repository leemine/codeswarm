import { useCallback, useEffect, useRef, type Dispatch, type MutableRefObject, type SetStateAction } from 'react';
import { archivedTaskClient } from '../../features/workspace/archivedTaskClient';

/** A deletion receipt belongs to the requested side Session, never the latest open pane. */
export function useSideConversationDeletion<T extends { session: { session_id: string } }>(
  sideRef: MutableRefObject<T | null>,
  setSide: Dispatch<SetStateAction<T | null>>,
  removeLocal: (sessionId: string) => void,
): (sessionId: string) => Promise<void> {
  const mounted = useRef(true);
  const inFlight = useRef(new Map<string, Promise<void>>());
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  return useCallback(
    (sessionId: string) => {
      const pending = inFlight.current.get(sessionId);
      if (pending) return pending;
      const deletion = (async () => {
        await archivedTaskClient.deleteSession(sessionId);
        if (!mounted.current) return;
        removeLocal(sessionId);
        if (sideRef.current?.session.session_id === sessionId) {
          sideRef.current = null;
          setSide(null);
        }
      })().finally(() => {
        inFlight.current.delete(sessionId);
      });
      inFlight.current.set(sessionId, deletion);
      return deletion;
    },
    [removeLocal, setSide, sideRef],
  );
}
