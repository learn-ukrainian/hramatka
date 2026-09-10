// GIS may invoke the callback with an empty payload and later a JWT. An empty
// first event is not a terminal failure. Wait for a credential or a real GIS
// error. Never persist the JWT; never log it, emails, or IPs.

const SENSITIVE_KEY_RE =
  /credential|token|jwt|email|sub\b|ip\b|client.?id|nonce|authorization|secret|password|subject/i;

const TERMINAL_GIS_TYPES = new Set([
  'popup_blocked',
  'popup_closed',
  'popup_failed_to_open',
]);

export type CredentialLenBucket = '0' | '1-100' | '101-500' | '501-2000' | '2000+';

export type GisResponseShape = {
  kind: 'undefined' | 'null' | 'string' | 'object' | 'other';
  has_credential: boolean;
  credential_len_bucket: CredentialLenBucket;
  extra_keys: string[];
  has_error: boolean;
  error_kind: 'none' | 'string' | 'object' | 'other';
};

export function googleCredentialFromCallback(response: unknown): string {
  if (typeof response === 'string') return response.trim();
  if (response && typeof response === 'object') {
    const credential = (response as { credential?: unknown }).credential;
    if (typeof credential === 'string') return credential.trim();
  }
  return '';
}

export function credentialLenBucket(length: number): CredentialLenBucket {
  if (length <= 0) return '0';
  if (length <= 100) return '1-100';
  if (length <= 500) return '101-500';
  if (length <= 2000) return '501-2000';
  return '2000+';
}

function responseKind(response: unknown): GisResponseShape['kind'] {
  if (response === undefined) return 'undefined';
  if (response === null) return 'null';
  if (typeof response === 'string') return 'string';
  if (typeof response === 'object') return 'object';
  return 'other';
}

function errorKind(value: unknown): GisResponseShape['error_kind'] {
  if (value == null || value === '') return 'none';
  if (typeof value === 'string') return 'string';
  if (typeof value === 'object') return 'object';
  return 'other';
}

function extraKeysOf(response: object): string[] {
  return Object.keys(response)
    .filter((key) => !SENSITIVE_KEY_RE.test(key))
    .sort();
}

export function gisResponseShape(response: unknown): GisResponseShape {
  const credential = googleCredentialFromCallback(response);
  const hasError = response != null
    && typeof response === 'object'
    && errorKind((response as { error?: unknown }).error) !== 'none';
  return {
    kind: responseKind(response),
    has_credential: credential.length > 0,
    credential_len_bucket: credentialLenBucket(credential.length),
    extra_keys: response && typeof response === 'object' ? extraKeysOf(response) : [],
    has_error: hasError,
    error_kind: response && typeof response === 'object'
      ? errorKind((response as { error?: unknown }).error)
      : 'none',
  };
}

export function logGisResponseShape(
  response: unknown,
  write: (label: string, shape: GisResponseShape) => void = (label, shape) => {
    console.info(label, shape);
  },
): GisResponseShape {
  const shape = gisResponseShape(response);
  write('gis_callback_shape', shape);
  return shape;
}

export function isTerminalGisError(response: unknown): boolean {
  if (!response || typeof response !== 'object') return false;
  const record = response as { error?: unknown; type?: unknown };
  const error = record.error;
  if (typeof error === 'string') return error.trim().length > 0;
  if (error && typeof error === 'object') return true;
  return typeof record.type === 'string' && TERMINAL_GIS_TYPES.has(record.type);
}

export function postGoogleCredentialSameOrigin(credential: string): void {
  // GIS returns the JWT to this page. A same-origin form POST lets the
  // complete 303 land on /teacher/ or /teacher/?google=failed. Never persist.
  const form = document.createElement('form');
  form.method = 'POST';
  form.action = '/api/auth/google/complete';
  const field = document.createElement('input');
  field.type = 'hidden';
  field.name = 'credential';
  field.value = credential;
  form.appendChild(field);
  document.body.appendChild(form);
  form.submit();
}

export function createGoogleCredentialDelivery(
  postCredential: (credential: string) => void = postGoogleCredentialSameOrigin,
  assignFailed: (url: string) => void = (url) => { window.location.assign(url); },
  logShape: (response: unknown) => void = logGisResponseShape,
): (response: unknown) => void {
  let posted = false;
  let failed = false;
  return (response: unknown) => {
    logShape(response);
    if (posted || failed) return;
    const credential = googleCredentialFromCallback(response);
    if (credential) {
      posted = true;
      postCredential(credential);
      return;
    }
    if (isTerminalGisError(response)) {
      failed = true;
      assignFailed('/teacher/?google=failed');
    }
  };
}
