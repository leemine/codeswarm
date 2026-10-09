import {
  useCallback,
  useEffect,
  useState,
  useTranslation,
} from '../../../channels/web/frontend/src/applicationPlugins/ui';
import { Button } from '../../../channels/web/frontend/src/components/ui';
import { evaluationRequest as request } from './client';
import type { Catalog, TaskRef } from './types';
import TaskLibrary from './TaskLibrary';
import Experiments from './Experiments';
import './evaluation.css';

export default function EvaluationApp() {
  const { t } = useTranslation();
  const [section, setSection] = useState('runs');
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [selected, setSelected] = useState<TaskRef[]>([]);
  const [error, setError] = useState('');
  const updateCatalog = useCallback((value: Catalog) => setCatalog(value), []);
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError('');
    void request<Catalog>('catalog')
      .then((value) => {
        if (active) setCatalog(value);
      })
      .catch((err) => {
        if (active) {
          setCatalog(null);
          setSelected([]);
          setError(err.code || err.message);
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [refreshVersion]);
  if (!catalog) {
    return (
      <main className="evaluation-app" data-testid="evaluation-app">
        <header className="evaluation-header" data-testid="evaluation-header">
          <h1 data-testid="evaluation-title">{t('evaluation.title')}</h1>
          <Button
            data-testid="evaluation-refresh"
            disabled={loading}
            onClick={() => setRefreshVersion((current) => current + 1)}
          >
            {t('evaluation.refresh')}
          </Button>
        </header>
        {error === 'FORBIDDEN' ? (
          <p role="status" data-testid="evaluation-unavailable">
            {t('evaluation.unavailable')}
          </p>
        ) : error ? (
          <p role="alert" className="evaluation-error" data-testid="evaluation-error">
            {error}
          </p>
        ) : null}
      </main>
    );
  }
  return (
    <main className="evaluation-app" data-testid="evaluation-app">
      <header className="evaluation-header" data-testid="evaluation-header">
        <div>
          <h1 data-testid="evaluation-title">{t('evaluation.title')}</h1>
          <p data-testid="evaluation-shared-environment">{t('evaluation.overview')}</p>
        </div>
        <div className="evaluation-actions" role="tablist" data-testid="evaluation-workspace-tabs">
          <Button
            data-testid="evaluation-runs-tab"
            role="tab"
            aria-selected={section === 'runs'}
            onClick={() => setSection('runs')}
          >
            {t('evaluation.runs')}
          </Button>
          <Button
            data-testid="evaluation-library-tab"
            role="tab"
            aria-selected={section === 'library'}
            onClick={() => setSection('library')}
          >
            {t('evaluation.library')}
          </Button>
        </div>
      </header>
      {error && (
        <p role="alert" data-testid="evaluation-error">
          {error}
        </p>
      )}
      <div hidden={section !== 'runs'} data-testid="evaluation-runs-workspace">
        <Experiments
          catalog={catalog}
          selected={selected}
          onSelect={setSelected}
          onLibrary={() => setSection('library')}
        />
      </div>
      {section === 'library' && <TaskLibrary selected={selected} onSelect={setSelected} onCatalog={updateCatalog} />}
    </main>
  );
}
