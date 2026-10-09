import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { RsiPage } from '../features/rsi/RsiPage';
import { ApplicationPluginOutlet } from './ApplicationPluginOutlet';
import type { ApplicationPluginContribution } from './types';
import './experimentsContainer.css';

export function ExperimentsContainer({
  rsiEnabled,
  plugins,
  legacyNav,
}: {
  rsiEnabled: boolean;
  plugins: ApplicationPluginContribution[];
  legacyNav: string;
}) {
  const { t } = useTranslation();
  const [selected, setSelected] = useState(() =>
    legacyNav.startsWith('app:')
      ? legacyNav
      : sessionStorage.getItem('jiuwen:experiments:tab') || plugins[0]?.nav_key || 'rsi',
  );
  const available = [...plugins.map((plugin) => plugin.nav_key), ...(rsiEnabled ? ['rsi'] : [])];
  const effective = available.includes(selected) ? selected : available[0];
  useEffect(() => {
    if (legacyNav.startsWith('app:')) setSelected(legacyNav);
  }, [legacyNav]);
  useEffect(() => {
    if (effective) sessionStorage.setItem('jiuwen:experiments:tab', effective);
  }, [effective]);
  const plugin = plugins.find((item) => item.nav_key === effective);
  return (
    <div className="app-experiments" data-testid="app-experiments">
      <div
        className="app-experiments-tabs"
        role="tablist"
        aria-label={t('nav.experiments')}
        data-testid="app-experiments-tabs"
      >
        {plugins.map((item) => (
          <button
            key={item.nav_key}
            role="tab"
            aria-selected={effective === item.nav_key}
            data-testid="app-experiments-tab"
            data-variant={item.nav_key}
            onClick={() => setSelected(item.nav_key)}
          >
            {item.title_i18n_key ? t(item.title_i18n_key) : item.title}
          </button>
        ))}
        {rsiEnabled && (
          <button
            role="tab"
            aria-selected={effective === 'rsi'}
            data-testid="app-experiments-rsi-tab"
            onClick={() => setSelected('rsi')}
          >
            {t('evaluation.rsiTab')}
          </button>
        )}
      </div>
      <div className="app-experiments-body" role="tabpanel" data-testid="app-experiments-body">
        {effective === 'rsi' ? <RsiPage /> : plugin ? <ApplicationPluginOutlet contribution={plugin} /> : null}
      </div>
    </div>
  );
}
