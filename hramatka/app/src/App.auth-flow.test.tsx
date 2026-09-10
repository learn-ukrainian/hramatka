import '@testing-library/jest-dom';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import { LangProvider } from './i18n';

const teacher = {
  teacher: { id: 'teacher-1', display_name: 'Марія' },
  expires_at: '2030-01-01T00:00:00Z',
  csrf_token: 'csrf-token',
};

const response = (body: unknown): Response => ({ ok: true, status: 200, json: async () => body } as Response);
const errorResponse = (status: number, body: unknown): Response => ({ ok: false, status, json: async () => body } as Response);

function renderApp() {
  return render(<LangProvider><App /></LangProvider>);
}

function installAuthenticatedFetch() {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/api/session')) return response(teacher);
    if (url.endsWith('/api/teacher/preferences')) return response({ default_duration: 45 });
    if (url.endsWith('/api/lesson-models')) {
      return response({ registry_version: 'test', models: [{ id: 'flash', label: 'Flash', description: '' }], unavailable_message: null });
    }
    if (url.endsWith('/api/lessons')) return response({ lessons: [] });
    return response({});
  }));
}

describe('teacher passkey recovery codes (#324)', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    sessionStorage.clear();
    window.history.replaceState(null, '', '#/');
    Object.defineProperty(navigator, 'credentials', { configurable: true, value: {} });
  });

  afterEach(() => {
    Reflect.deleteProperty(navigator, 'credentials');
    Reflect.deleteProperty(navigator, 'clipboard');
  });

  it('shows enrollment codes in an ephemeral dialog and clears them after confirmation', async () => {
    installAuthenticatedFetch();
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:test') });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    Object.defineProperty(navigator.credentials, 'create', {
      configurable: true,
      value: vi.fn(async () => ({
        id: 'credential-id', rawId: new Uint8Array([1]).buffer, type: 'public-key',
        response: { clientDataJSON: new Uint8Array([2]).buffer, attestationObject: new Uint8Array([3]).buffer },
      })),
    });
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/session')) return response(teacher);
      if (url.endsWith('/api/teacher/preferences')) return response({ default_duration: 45 });
      if (url.endsWith('/api/lesson-models')) return response({ registry_version: 'test', models: [], unavailable_message: null });
      if (url.endsWith('/api/lessons')) return response({ lessons: [] });
      if (url.endsWith('/api/passkeys/enrollment/options')) {
        return response({ challenge_id: 'challenge-1', publicKey: { challenge: 'AQ', user: { id: 'Ag', name: 'Марія', displayName: 'Марія' } } });
      }
      if (url.endsWith('/api/passkeys/enrollment/challenge-1')) return response({ recovery_codes: ['sample-code-one', 'sample-code-two'] });
      return response({});
    });

    renderApp();
    fireEvent.click(await screen.findByTestId('passkey-enroll-btn'));

    expect(await screen.findByTestId('recovery-codes-dialog')).toBeInTheDocument();
    expect(screen.getByTestId('recovery-codes-list')).toHaveTextContent('sample-code-one');
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);

    // These codes are shown only once, so an accidental backdrop click must
    // not discard them. Explicit save confirmation is the only UI dismissal.
    fireEvent.click(screen.getByTestId('recovery-codes-dialog').parentElement!);
    expect(screen.getByTestId('recovery-codes-dialog')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Скопіювати коди' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('sample-code-one\nsample-code-two'));
    fireEvent.click(screen.getByRole('button', { name: 'Завантажити коди' }));
    expect(URL.createObjectURL).toHaveBeenCalledOnce();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:test');
    expect(click).toHaveBeenCalledOnce();

    fireEvent.click(screen.getByTestId('recovery-codes-saved-btn'));
    expect(screen.queryByTestId('recovery-codes-dialog')).not.toBeInTheDocument();
  });

  it('clears recovery codes when the authenticated session is logged out', async () => {
    installAuthenticatedFetch();
    Object.defineProperty(navigator.credentials, 'create', {
      configurable: true,
      value: vi.fn(async () => ({
        id: 'credential-id', rawId: new Uint8Array([1]).buffer, type: 'public-key',
        response: { clientDataJSON: new Uint8Array([2]).buffer, attestationObject: new Uint8Array([3]).buffer },
      })),
    });
    vi.mocked(fetch).mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/session')) return response(teacher);
      if (url.endsWith('/api/teacher/preferences')) return response({ default_duration: 45 });
      if (url.endsWith('/api/lesson-models')) return response({ registry_version: 'test', models: [], unavailable_message: null });
      if (url.endsWith('/api/lessons')) return response({ lessons: [] });
      if (url.endsWith('/api/passkeys/enrollment/options')) {
        return response({ challenge_id: 'challenge-2', publicKey: { challenge: 'AQ', user: { id: 'Ag', name: 'Марія', displayName: 'Марія' } } });
      }
      if (url.endsWith('/api/passkeys/enrollment/challenge-2')) return response({ recovery_codes: ['sample-code-only'] });
      return response({});
    });

    renderApp();
    fireEvent.click(await screen.findByTestId('passkey-enroll-btn'));
    expect(await screen.findByTestId('recovery-codes-dialog')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('logout-btn'));
    await waitFor(() => expect(screen.queryByTestId('recovery-codes-dialog')).not.toBeInTheDocument());
  });
});

describe('teacher recovery-code sign-in (#324)', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    sessionStorage.clear();
    window.history.replaceState(null, '', '#/');
  });

  it('redeems a code into an ordinary teacher session without browser storage', async () => {
    let sessionReads = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/api/session')) {
        sessionReads += 1;
        return sessionReads === 1 ? errorResponse(401, { code: 'session_required' }) : response(teacher);
      }
      if (url.endsWith('/api/recovery-codes/redeem')) {
        expect(init?.method).toBe('POST');
        expect(init?.body).toBe(JSON.stringify({ code: 'entered-sample-code' }));
        return response(teacher);
      }
      if (url.endsWith('/api/teacher/preferences')) return response({ default_duration: 45 });
      if (url.endsWith('/api/lesson-models')) return response({ registry_version: 'test', models: [], unavailable_message: null });
      if (url.endsWith('/api/lessons')) return response({ lessons: [] });
      return response({});
    }));

    renderApp();
    fireEvent.change(await screen.findByTestId('recovery-code-input'), { target: { value: 'entered-sample-code' } });
    fireEvent.click(screen.getByTestId('recovery-code-submit'));

    expect(await screen.findByTestId('anchor-text-input')).toBeInTheDocument();
    expect(screen.queryByDisplayValue('entered-sample-code')).not.toBeInTheDocument();
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });

  it('handles an invalid or replayed recovery code with generic failure copy', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/api/session')) return errorResponse(401, { code: 'session_required' });
      if (url.endsWith('/api/recovery-codes/redeem')) {
        return errorResponse(401, { code: 'session_required', message: 'do not expose replay details' });
      }
      return response({});
    }));

    renderApp();
    fireEvent.change(await screen.findByTestId('recovery-code-input'), { target: { value: 'already-used-sample-code' } });
    fireEvent.click(screen.getByTestId('recovery-code-submit'));

    expect(await screen.findByRole('alert')).toHaveTextContent('Не вдалося увійти за кодом відновлення.');
    expect(screen.getByRole('alert')).not.toHaveTextContent('do not expose replay details');
  });
});
