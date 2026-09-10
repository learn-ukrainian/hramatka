import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  createGoogleCredentialDelivery,
  credentialLenBucket,
  gisResponseShape,
  googleCredentialFromCallback,
  isTerminalGisError,
  logGisResponseShape,
} from './google-sign-in';

const JWT = 'aaa.bbb.ccc-jwt-must-never-appear-in-shape-logs';

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('gisResponseShape (#595)', () => {
  it('records coarse keys and buckets without the JWT or extra secret values', () => {
    const shape = gisResponseShape({
      credential: JWT,
      select_by: 'btn',
      email: 'teacher@example.test',
      client_id: '123456789-test.apps.googleusercontent.com',
    });
    expect(shape).toEqual({
      kind: 'object',
      has_credential: true,
      credential_len_bucket: '1-100',
      extra_keys: ['select_by'],
      has_error: false,
      error_kind: 'none',
    });
    expect(JSON.stringify(shape)).not.toContain(JWT);
    expect(JSON.stringify(shape)).not.toContain('teacher@');
    expect(JSON.stringify(shape)).not.toContain('apps.googleusercontent');
  });

  it('describes an empty first callback without treating it as a credential', () => {
    expect(gisResponseShape({})).toEqual({
      kind: 'object',
      has_credential: false,
      credential_len_bucket: '0',
      extra_keys: [],
      has_error: false,
      error_kind: 'none',
    });
    expect(gisResponseShape(undefined).kind).toBe('undefined');
    expect(googleCredentialFromCallback({})).toBe('');
    expect(credentialLenBucket(0)).toBe('0');
    expect(credentialLenBucket(501)).toBe('501-2000');
  });
});

describe('logGisResponseShape (#595)', () => {
  it('writes the shape label and never the JWT', () => {
    const info = vi.spyOn(console, 'info').mockImplementation(() => {});
    logGisResponseShape({ credential: JWT, select_by: 'fedcm' });
    expect(info).toHaveBeenCalledTimes(1);
    expect(info.mock.calls[0]?.[0]).toBe('gis_callback_shape');
    const logged = JSON.stringify(info.mock.calls[0]);
    expect(logged).toContain('has_credential');
    expect(logged).toContain('credential_len_bucket');
    expect(logged).toContain('select_by');
    expect(logged).not.toContain(JWT);
    expect(logged).not.toContain('aaa.bbb');
  });
});

describe('createGoogleCredentialDelivery (#595)', () => {
  it('does not fail an empty callback after 250ms and still POSTs a later credential', () => {
    vi.useFakeTimers();
    const postCredential = vi.fn();
    const assignFailed = vi.fn();
    const deliver = createGoogleCredentialDelivery(postCredential, assignFailed);

    deliver({});
    vi.advanceTimersByTime(250);
    expect(assignFailed).not.toHaveBeenCalled();
    expect(postCredential).not.toHaveBeenCalled();

    vi.advanceTimersByTime(10_000);
    expect(assignFailed).not.toHaveBeenCalled();
    expect(postCredential).not.toHaveBeenCalled();

    deliver({ credential: JWT });
    expect(postCredential).toHaveBeenCalledWith(JWT);
    expect(assignFailed).not.toHaveBeenCalled();
  });

  it('POSTs a credential to the same-origin complete path', () => {
    const postCredential = vi.fn();
    const deliver = createGoogleCredentialDelivery(postCredential, vi.fn());
    deliver({ credential: JWT, select_by: 'btn' });
    expect(postCredential).toHaveBeenCalledWith(JWT);
  });

  it('fails only after a real terminal GIS error, not an empty first event', () => {
    const postCredential = vi.fn();
    const assignFailed = vi.fn();
    const deliver = createGoogleCredentialDelivery(postCredential, assignFailed);

    deliver({});
    expect(assignFailed).not.toHaveBeenCalled();
    expect(isTerminalGisError({})).toBe(false);

    deliver({ error: 'popup_closed_by_user' });
    expect(assignFailed).toHaveBeenCalledWith('/teacher/?google=failed');
    expect(postCredential).not.toHaveBeenCalled();
    expect(isTerminalGisError({ error: 'popup_closed_by_user' })).toBe(true);
  });
});
