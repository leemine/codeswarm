export type ChatRoute =
  | { kind: 'taskboard'; taskId?: string }
  | { kind: 'chat-new' }
  | { kind: 'chat-session'; sessionId: string }
  | { kind: 'not-found'; pathname: string };

export function parseChatRoute(pathname: string): ChatRoute | null {
  const path = pathname.length > 1 ? pathname.replace(/\/+$/, '') : pathname;
  if (path === '/' || path === '/chat' || path === '/chat/new') return { kind: 'chat-new' };
  if (path === '/taskboard') return { kind: 'taskboard' };
  const taskMatch = path.match(/^\/taskboard\/([a-f0-9-]+)$/);
  if (taskMatch) return { kind: 'taskboard', taskId: taskMatch[1] };
  const match = path.match(/^\/chat\/([^/]+)$/);
  if (!match) return null;
  const sessionId = decodeURIComponent(match[1]);
  return { kind: 'chat-session', sessionId };
}

export function chatRoutePath(route: ChatRoute): string {
  if (route.kind === 'taskboard') return route.taskId ? `/taskboard/${encodeURIComponent(route.taskId)}` : '/taskboard';
  if (route.kind === 'chat-new') return '/chat/new';
  if (route.kind === 'chat-session') return `/chat/${encodeURIComponent(route.sessionId)}`;
  return route.pathname;
}
