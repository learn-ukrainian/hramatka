import type { QualifiedModelChoice } from './app-helpers';
import type { ChromeKey } from './i18n';

export type { QualifiedModelChoice };

interface Props {
  models: QualifiedModelChoice[];
  selectedId: string;
  unavailableMessage: string | null;
  disabled?: boolean;
  onChange: (id: string) => void;
  t: (key: ChromeKey) => string;
}

export default function QualifiedModelPicker({
  models,
  selectedId,
  unavailableMessage,
  disabled = false,
  onChange,
  t,
}: Props) {
  return (
    <div className="field" data-testid="qualified-model-picker">
      <label htmlFor="logical-model">{t('paste.model')}</label>
      {models.length > 0 ? (
        <>
          <select
            id="logical-model"
            className="inputbox"
            value={selectedId}
            onChange={event => onChange(event.target.value)}
            disabled={disabled}
          >
            {models.map(model => (
              <option key={model.id} value={model.id}>{model.label}</option>
            ))}
          </select>
          {models.find(model => model.id === selectedId)?.description && (
            <p className="hint" data-testid="qualified-model-description">
              {models.find(model => model.id === selectedId)!.description}
            </p>
          )}
          {unavailableMessage && (
            <p className="hint" role="status" data-testid="partially-qualified-models">
              {unavailableMessage}
            </p>
          )}
        </>
      ) : (
        <div className="banner honest" role="status" data-testid="no-qualified-models">
          <span className="ic">ℹ︎</span>
          <span>{unavailableMessage || t('paste.modelUnavailable')}</span>
        </div>
      )}
    </div>
  );
}
