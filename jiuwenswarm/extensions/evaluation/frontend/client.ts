import { webRequest } from '../../../channels/web/frontend/src/services/webClient';
export function evaluationRequest<T>(method: string, params: Record<string, unknown> = {}) {
  return webRequest<T>(`evaluation.${method}`, params);
}
