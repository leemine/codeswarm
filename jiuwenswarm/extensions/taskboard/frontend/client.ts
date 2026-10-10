import { webRequest } from '../../../channels/web/frontend/src/services/webClient';
import type { Page, Task, Status } from './types';
export const taskboardClient = {
  list: (status: Status, query: string, projectId: string, cursor?: string | null) =>
    webRequest<Page>('taskboard.list', {
      status,
      query,
      ...(projectId ? { project_id: projectId } : {}),
      ...(cursor ? { cursor } : {}),
    }),
  get: (id: string) => webRequest<{ task: Task }>('taskboard.get', { task_id: id }),
  create: (values: Record<string, unknown>, key: string) =>
    webRequest<{ task: Task }>('taskboard.create', { ...values, client_create_id: key }),
  update: (task: Task, patch: Record<string, unknown>) =>
    webRequest<{ task: Task }>('taskboard.update', { task_id: task.task_id, expected_version: task.version, patch }),
};
