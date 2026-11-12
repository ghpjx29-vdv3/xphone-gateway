export interface GrantClaims {
  aud: 'xphone-agent-worker';
  sub: string;
  packId: string;
  playthroughId: string;
  xphoneSha256: string;
  accessKind: 'admin' | 'lifetime' | 'trial';
  accessExpiresAt?: number;
  exp: number;
  jti: string;
}

export interface AgentClaims {
  aud: 'xphone-agent-mcp';
  sub: string;
  packId: string;
  playthroughId: string;
  xphoneSha256: string;
  exp: number;
  jti: string;
  parentJti?: string;
  gateway?: boolean;
  provider?: 'muse' | 'chatgpt';
}

export interface AccountClaims {
  aud: 'xphone-agent-account';
  sub: string;
  exp: number;
  jti: string;
  provider?: 'muse' | 'chatgpt';
}

export interface AccountGrantClaims {
  aud: 'xphone-agent-account-grant';
  sub: string;
  accessExpiresAt?: number;
  exp: number;
  jti: string;
}

export interface OAuthClaims {
  aud: 'xphone-agent-oauth' | 'xphone-agent-refresh';
  iss: string;
  resource: string;
  scope: 'xphone:mcp';
  sub: string;
  parentJti: string;
  pairingScope?: { packId: string; playthroughId: string; xphoneSha256: string };
  exp: number;
  jti: string;
}

const encoder = new TextEncoder();

export function base64url(bytes: Uint8Array): string {
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function unbase64url(value: string): Uint8Array {
  const clean = value.replace(/\s+/g, '');
  if (!/^[A-Za-z0-9_-]+$/.test(clean)) throw new Error('invalid token');
  const binary = atob(clean.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - clean.length % 4) % 4));
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

async function hmac(secret: string, message: string): Promise<Uint8Array> {
  const key = await crypto.subtle.importKey('raw', encoder.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return new Uint8Array(await crypto.subtle.sign('HMAC', key, encoder.encode(message)));
}

export async function signToken<T extends GrantClaims | AgentClaims | AccountClaims | AccountGrantClaims | OAuthClaims>(claims: T, secret: string): Promise<string> {
  if (!secret) throw new Error('signing secret unavailable');
  const header = base64url(encoder.encode(JSON.stringify({ alg: 'HS256', typ: 'JWT' })));
  const payload = base64url(encoder.encode(JSON.stringify(claims)));
  return `${header}.${payload}.${base64url(await hmac(secret, `${header}.${payload}`))}`;
}

export async function verifyToken<T extends GrantClaims | AgentClaims | AccountClaims | AccountGrantClaims | OAuthClaims>(token: string, secret: string, audience: T['aud']): Promise<T> {
  if (!secret) throw new Error('signing secret unavailable');
  const clean = token.replace(/\s+/g, '');
  const parts = clean.split('.');
  if (parts.length !== 3 || parts.some((part) => part.length > 4000)) throw new Error('invalid token');
  const header = JSON.parse(new TextDecoder().decode(unbase64url(parts[0])));
  if (header?.alg !== 'HS256' || header?.typ !== 'JWT') throw new Error('invalid token');
  const supplied = unbase64url(parts[2]);
  if (supplied.length !== 32) throw new Error('invalid token');
  const key = await crypto.subtle.importKey('raw', encoder.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['verify']);
  const valid = await crypto.subtle.verify('HMAC', key, new Uint8Array(supplied), encoder.encode(`${parts[0]}.${parts[1]}`));
  if (!valid) throw new Error('invalid token');
  const claims = JSON.parse(new TextDecoder().decode(unbase64url(parts[1]))) as T;
  const now = Math.floor(Date.now() / 1000);
  if (claims.aud !== audience || !claims.sub || (['xphone-agent-worker', 'xphone-agent-mcp'].includes(audience) && (!('packId' in claims) || !claims.packId || !claims.playthroughId || !/^[a-f0-9]{64}$/.test(claims.xphoneSha256))) || !claims.jti
    || !Number.isSafeInteger(claims.exp) || claims.exp <= now) throw new Error('invalid token');
  return claims;
}

export function randomId(bytes = 24): string {
  return base64url(crypto.getRandomValues(new Uint8Array(bytes)));
}

export async function sha256(value: string): Promise<string> {
  return base64url(new Uint8Array(await crypto.subtle.digest('SHA-256', encoder.encode(value))));
}

export function bearer(request: Request): string {
  const header = (request.headers.get('authorization') || request.headers.get('x-api-key') || request.headers.get('api-key') || '').trim();
  const match = /^(?:[Bb]earer\s+)?([A-Za-z0-9._-]+)$/.exec(header);
  if (!match) throw new Error('authentication required');
  return match[1];
}

export function connectionName(claims: Pick<GrantClaims, 'sub' | 'packId' | 'playthroughId'>): string {
  return JSON.stringify([claims.sub, claims.packId, claims.playthroughId]);
}
