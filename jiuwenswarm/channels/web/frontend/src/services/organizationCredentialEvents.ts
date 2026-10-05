/** Cache invalidation only. Messages carry no identity or credential authority. */
const CHANNEL = 'jiuwen:organization-credentials';
const EVENT = 'jiuwen:organization-credentials-changed';

function openChannel(): BroadcastChannel | null {
  try {
    return typeof BroadcastChannel === 'undefined' ? null : new BroadcastChannel(CHANNEL);
  } catch {
    // Restricted browser contexts still have local events and visibility cleanup.
    return null;
  }
}

export function notifyOrganizationCredentialChange(): void {
  window.dispatchEvent(new window.Event(EVENT));
  const channel = openChannel();
  try {
    channel?.postMessage('changed');
  } catch {
    // Notification failure must not undo successful server-side login/logout.
  } finally {
    channel?.close();
  }
}

export function onOrganizationCredentialChange(clear: () => void): () => void {
  window.addEventListener(EVENT, clear);
  const channel = openChannel();
  if (channel) channel.onmessage = () => clear();
  return () => {
    window.removeEventListener(EVENT, clear);
    channel?.close();
  };
}
