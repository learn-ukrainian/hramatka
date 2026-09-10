import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { translate, type ChromeKey } from './i18n';
import QualifiedModelPicker, { resolveQualifiedModelId } from './QualifiedModelPicker';

const t = (key: ChromeKey) => translate('uk', key);

describe('QualifiedModelPicker', () => {
  it('replaces a retired recovery value with a current qualified choice', () => {
    const models = [
      { id: 'gemini-3.7-flash', label: 'Gemini 3.7 Flash', description: 'Швидко.' },
    ];
    expect(resolveQualifiedModelId(models, 'retired-model')).toBe('gemini-3.7-flash');
    expect(resolveQualifiedModelId([], 'retired-model')).toBe('');
  });

  it('renders only the qualified choices supplied by the server', () => {
    const onChange = vi.fn();
    render(
      <QualifiedModelPicker
        models={[
          { id: 'gemini-3.7-flash', label: 'Gemini 3.7 Flash', description: 'Швидко.' },
          { id: 'gemma-4-31b', label: 'Gemma 4 31B', description: 'Резервний маршрут.' },
        ]}
        selectedId="gemini-3.7-flash"
        unavailableMessage="Показано лише кваліфіковані моделі."
        onChange={onChange}
        t={t}
      />,
    );

    const options = screen.getAllByRole('option');
    expect(options.map(option => option.textContent)).toEqual([
      'Gemini 3.7 Flash',
      'Gemma 4 31B',
    ]);
    expect(screen.queryByText(/DeepSeek/i)).toBeNull();
    expect(screen.getByTestId('partially-qualified-models').textContent).toContain(
      'Показано лише кваліфіковані моделі.',
    );
    fireEvent.change(screen.getByLabelText('Модель для уроку'), {
      target: { value: 'gemma-4-31b' },
    });
    expect(onChange).toHaveBeenCalledWith('gemma-4-31b');
  });

  it('explains a fail-closed empty qualification set without a selector', () => {
    render(
      <QualifiedModelPicker
        models={[]}
        selectedId=""
        unavailableMessage="Кваліфікація ще не завершена."
        onChange={() => undefined}
        t={t}
      />,
    );
    expect(screen.queryByRole('combobox')).toBeNull();
    expect(screen.getByTestId('no-qualified-models').textContent).toContain(
      'Кваліфікація ще не завершена.',
    );
  });
});
