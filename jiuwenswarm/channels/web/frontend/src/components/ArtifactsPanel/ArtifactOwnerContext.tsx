import { createContext, useContext, useLayoutEffect, useRef } from 'react';
import { useChatStore } from '../../stores';
import { webClient } from '../../services/webClient';
import { onOrganizationCredentialChange } from '../../services/organizationCredentialEvents';
import { OwnerDownloadScope } from './ownerDownload';

export const ArtifactOwnerContext = createContext({ organizationAuth: false, sessionId: '' });

export function useArtifactOwnerDownload() {
  const context = useContext(ArtifactOwnerContext);
  const current = useRef(context);
  current.current = context;
  const scope = useRef(new OwnerDownloadScope()).current;
  useLayoutEffect(() => {
    const invalidate = () => scope.invalidate();
    const unsubscribeIdentity = onOrganizationCredentialChange(invalidate);
    const unsubscribeConnection = webClient.onStateChange(invalidate);
    const unsubscribeSession = useChatStore.subscribe((s) => s.activeSessionId, invalidate);
    return () => {
      invalidate();
      unsubscribeIdentity();
      unsubscribeConnection();
      unsubscribeSession();
    };
  }, [scope, context.organizationAuth, context.sessionId]);
  return {
    ...context,
    download: (artifact: Parameters<OwnerDownloadScope['download']>[0]) => {
      const sessionId = context.sessionId;
      return scope.download(
        artifact,
        sessionId,
        () =>
          current.current.organizationAuth &&
          current.current.sessionId === sessionId &&
          useChatStore.getState().activeSessionId === sessionId &&
          webClient.getState() === 'ready',
      );
    },
  };
}
