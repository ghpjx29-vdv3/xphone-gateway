import { base64url, sha256 } from './security';

export interface EventSubscription {
  id: string;
  url: string;
  secret: string;
  expiresAt: number;
}

export function eventCallbackUrl(value: string): string {
  const url = new URL(value);
  const host = url.hostname.toLowerCase();
  if (url.protocol !== 'https:' || url.username || url.password || url.port
    || !(host === 'chatgpt.com' || host.endsWith('.chatgpt.com')
      || host === 'openai.com' || host.endsWith('.openai.com'))) throw new Error('invalid event callback');
  return url.toString();
}

export function eventSecret(value: string): string {
  if (!value.startsWith('whsec_')) throw new Error('invalid event secret');
  const encoded = value.slice(6);
  if (!/^[A-Za-z0-9+/_-]+={0,2}$/.test(encoded)) throw new Error('invalid event secret');
  const bytes = atob(encoded.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - encoded.length % 4) % 4));
  if (bytes.length < 24 || bytes.length > 64) throw new Error('invalid event secret');
  return value;
}

export async function subscriptionId(sub: string, packId: string, playthroughId: string, url: string): Promise<string> {
  return `sub_${(await sha256(`${sub}\n${packId}\n${playthroughId}\nxphone.event.created\n${url}`)).slice(0, 40)}`;
}

export async function sendWebhook(subscription: EventSubscription, eventId: string, payload: unknown): Promise<Response> {
  const body = JSON.stringify(payload);
  const timestamp = String(Math.floor(Date.now() / 1000));
  const encoded = subscription.secret.slice(6);
  const binary = atob(encoded.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - encoded.length % 4) % 4));
  const key = await crypto.subtle.importKey('raw', Uint8Array.from(binary, (char) => char.charCodeAt(0)),
    { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const signature = base64url(new Uint8Array(await crypto.subtle.sign('HMAC', key,
    new TextEncoder().encode(`${eventId}.${timestamp}.${body}`)))).replace(/-/g, '+').replace(/_/g, '/');
  return fetch(subscription.url, { method: 'POST', redirect: 'manual', signal: AbortSignal.timeout(10_000),
    headers: { 'content-type': 'application/json', 'webhook-id': eventId, 'webhook-timestamp': timestamp,
      'webhook-signature': `v1,${signature.padEnd(44, '=')}`, 'X-MCP-Subscription-Id': subscription.id }, body });
}
