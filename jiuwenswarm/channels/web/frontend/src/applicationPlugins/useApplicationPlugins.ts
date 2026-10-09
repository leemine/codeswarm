import { useCallback, useEffect, useRef, useState } from 'react';

import { fetchApplicationPlugins } from './manifest';
import type { ApplicationPluginContribution } from './types';

export interface ApplicationPluginsState {
  plugins: ApplicationPluginContribution[];
  loading: boolean;
  loaded: boolean;
  error: string;
  refresh: () => Promise<void>;
}

export function useApplicationPlugins(isGatewayConnected: boolean): ApplicationPluginsState {
  const [plugins, setPlugins] = useState<ApplicationPluginContribution[]>([]);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState('');
  const pending = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    if (!isGatewayConnected) return;
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setLoading(true);
    setError('');
    try {
      const nextPlugins = await fetchApplicationPlugins(controller.signal);
      if (controller.signal.aborted) return;
      setPlugins(nextPlugins);
      setLoaded(true);
    } catch (refreshError) {
      if (controller.signal.aborted) return;
      console.warn('Application plugin discovery failed:', refreshError);
      setError(refreshError instanceof Error ? refreshError.message : 'Application plugin discovery failed');
    } finally {
      if (pending.current === controller) {
        pending.current = null;
        setLoading(false);
      }
    }
  }, [isGatewayConnected]);

  useEffect(() => {
    const onRefresh = () => {
      void refresh();
    };
    window.addEventListener('jiuwen:application-plugins-refresh', onRefresh);
    return () => window.removeEventListener('jiuwen:application-plugins-refresh', onRefresh);
  }, [refresh]);

  useEffect(() => {
    if (!isGatewayConnected) {
      setPlugins([]);
      setLoaded(false);
      setLoading(false);
      setError('');
      return;
    }
    void refresh();
    return () => {
      pending.current?.abort();
      pending.current = null;
    };
  }, [isGatewayConnected, refresh]);

  return { plugins, loading, loaded, error, refresh };
}
