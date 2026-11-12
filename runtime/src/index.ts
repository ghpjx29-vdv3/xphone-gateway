import { AgentConnection, type AgentEventRoute, type Direction, type SealedEnvelope } from './connection';
import { actions } from './actionCatalog';
import { mapFeatures, mapQuery } from './mapData';
import { eventCallbackUrl, eventSecret, sendWebhook, subscriptionId } from './mcpEvents';
import { base64url, bearer, connectionName, randomId, sha256, signToken, verifyToken, type AccountClaims, type AccountGrantClaims, type AgentClaims, type GrantClaims, type OAuthClaims } from './security';

export { AgentConnection };

interface Env {
  CONNECTIONS: DurableObjectNamespace;
  AGENT_DOCUMENTS: R2Bucket;
  GRANT_HMAC_SECRET: string;
  TOKEN_HMAC_SECRET: string;
  CLIENT_ORIGINS: string;
  MCP_ALLOWED_ORIGINS?: string;
  MCP_IP_LIMITER: RateLimit;
  ANON_PAIRING_LIMITER: RateLimit;
  PAIR_CODE_LIMITER: RateLimit;
  PAIR_CODE_GLOBAL_LIMITER: RateLimit;
}

interface StoryManifest {
  packId: string;
  inspirationCity?: string;
  kind?: 'story' | 'blank';
  xphoneSha256: string;
  version: string;
  releaseStatus: 'ready';
  reviewSkipped?: boolean;
  runtime?: { stateVariableIds?: string[]; initialState?: Record<string, unknown> };
  runtimeAssets?: Array<{ path: string; objectKey: string; sha256: string }>;
  referenceAssets?: Array<{ id: string; objectKey: string; mimeType: string; description: string; sha256: string }>;
  map?: { objectKey: string; sha256: string; placeCount: number;
    features?: { grid: number; bounds: number[]; tiles: Array<{ id: string; objectKey: string; sha256: string }> } };
  documents: Array<{ id: string; title: string; summary: string; objectKey: string; sha256: string; consumer?: string[]; initial?: boolean;
    unlock?: { minVariables?: Record<string, number>; requiredFlags?: string[]; dependsOn?: string[] } }>;
}

const protocol = '2025-11-25';
const object = { type: 'object', additionalProperties: false };
const string = { type: 'string' };
const integer = { type: 'integer' };
const base = { packId: string, playthroughId: string };
const sealed = { id: string, kind: { type: 'string', enum: ['event', 'action_batch', 'journal_frame', 'observation_request', 'observation_result', 'receipt'] }, version: { type: 'integer', const: 1 }, nonce: string, ciphertext: string, aad: string };
const pairingTools = [
  { name: 'xphone.start_pairing', description: 'Start ChatGPT pairing. First generate and retain a P-256 ECDH private key in your trusted Work environment; send only its public JWK here. Show the returned 8-character code to the user for entry in GIGI. Keep the returned secret private for xphone.redeem_pairing.', inputSchema: { ...object, properties: { publicKey: { ...object, properties: { kty: string, crv: string, x: string, y: string }, required: ['kty', 'crv', 'x', 'y'] } }, required: ['publicKey'] } },
  { name: 'xphone.redeem_pairing', description: 'After the user enters the pairing code in GIGI, redeem it with the secret from xphone.start_pairing. A pending result means the user has not approved yet. Decrypt the returned envelopes in your trusted Work environment and save the account token and content key privately in Library; never send either key through MCP.', inputSchema: { ...object, properties: { code: string, secret: string }, required: ['code', 'secret'] } },
];
const tools = [
  { name: 'map.get_places', description: 'List all fictional places and city zones on the story map. Call this to discover what locations exist.', inputSchema: { ...object, properties: {}, required: [] } },
  { name: 'map.get_place', description: 'Get details of one fictional place by ID or name, including its real-world map anchor.', inputSchema: { ...object, properties: { placeId: string, name: string }, required: [] } },
  { name: 'map.get_zone', description: 'Get details of one city zone by zone ID or place ID.', inputSchema: { ...object, properties: { zoneId: string, placeId: string }, required: [] } },
  { name: 'map.search_places', description: 'Search fictional places by keyword or intent, e.g. "quiet bar near the docks".', inputSchema: { ...object, properties: { query: string }, required: ['query'] } },
  { name: 'map.route', description: 'Plan a route between fictional places along real roads. Returns turn-by-turn directions.', inputSchema: { ...object, properties: { fromPlaceId: string, toPlaceId: string, viaPlaceIds: { type: 'array', items: string }, mode: string }, required: ['fromPlaceId', 'toPlaceId'] } },
  { name: 'map.optimize_route', description: 'Find the shortest visiting order for 2 to 8 places. Useful for planning multi-stop trips.', inputSchema: { ...object, properties: { placeIds: { type: 'array', items: string }, mode: string }, required: ['placeIds'] } },
  { name: 'map.get_actor_positions', description: 'Get the current map positions of all characters and actors.', inputSchema: { ...object, properties: {}, required: [] } },
  { name: 'map.nearby_actors', description: 'Find characters within a radius of a location. Defaults to the player position if no center given.', inputSchema: { ...object, properties: { centerActorId: string, centerPlaceId: string, lat: { type: 'number' }, lng: { type: 'number' }, radiusMeters: integer }, required: [] } },
  { name: 'map.get_items_at', description: 'List items placed at a specific map location.', inputSchema: { ...object, properties: { placeId: string }, required: ['placeId'] } },
  { name: 'map.watch_place', description: 'Set a geofence alert: get notified when the player enters or leaves a place.', inputSchema: { ...object, properties: { placeId: string, radiusMeters: integer, notifyOn: { type: 'string', enum: ['enter', 'exit', 'both'] } }, required: ['placeId'] } },
  { name: 'assets.get_place_reference', description: 'Get reference images and data for a place, useful when generating scene illustrations.', inputSchema: { ...object, properties: { placeId: string }, required: ['placeId'] } },
  { name: 'assets.get_character_reference', description: 'Get the visual art reference for a character (appearance, style), if one exists in this pack.', inputSchema: { ...object, properties: { actorId: string }, required: ['actorId'] } },
  { name: 'assets.get_style_reference', description: 'Get the overall visual style guide for this story pack.', inputSchema: { ...object, properties: { styleId: string }, required: ['styleId'] } },
  { name: 'xphone.list_app_actions', description: 'List all available app actions with their parameters — what the AI can do in the phone apps (messages, social, map, notes, journal). Call this first to discover capabilities.', inputSchema: { ...object, properties: base, required: ['packId', 'playthroughId'] } },
  { name: 'story.get_context', description: 'List story documents (lore, character sheets, plot notes) that your current role is allowed to read.', inputSchema: { ...object, properties: { eventId: string, consumer: string }, required: ['eventId', 'consumer'] } },
  { name: 'story.read_reference', description: 'Read the full content of one story document by its reference ID.', inputSchema: { ...object, properties: { eventId: string, consumer: string, referenceId: string }, required: ['eventId', 'consumer', 'referenceId'] } },
  { name: 'runtime.list_assets', description: 'List internal prompt and config files available for this story pack. Gateway internal use.', inputSchema: { ...object, properties: {}, required: [] } },
  { name: 'runtime.read_asset', description: 'Read one internal prompt or config file by exact path. Gateway internal use.', inputSchema: { ...object, properties: { path: string }, required: ['path'] } },
  { name: 'runtime.get_pack_manifest', description: 'Get the pack document and prompt hashes for the gateway local cache. Gateway internal use.', inputSchema: { ...object, properties: {}, required: [] } },
  { name: 'runtime.read_document', description: 'Read one published story document for the gateway local cache. Gateway internal use.', inputSchema: { ...object, properties: { referenceId: string }, required: ['referenceId'] } },
  { name: 'xphone.read_inbox', description: 'Read incoming events from the phone client (user messages, system notifications). Cursor is a monotonically increasing integer for pagination.', inputSchema: { ...object, properties: { cursor: integer, limit: integer }, required: ['cursor'] } },
  { name: 'xphone.ack_inbox', description: 'Confirm you have processed events up to the given cursor. Unacked events will be redelivered.', inputSchema: { ...object, properties: { cursor: integer }, required: ['cursor'] } },
  { name: 'xphone.open_event_stream', description: 'Mint a one-time ticket to open the real-time event WebSocket connection.', inputSchema: { ...object, properties: {}, required: [] } },
  { name: 'xphone.delegate_task', description: 'Delegate a task document on the agent computer to its subagent. Pass the absolute task document path only; the Worker never reads its contents. Returns delivery acknowledgement, not the execution result.', inputSchema: { ...object, properties: { taskPath: string }, required: ['taskPath'] } },
  { name: 'xphone.submit_task_actions', description: 'Submit actions for a task document. Pass taskPath alone for gateway sealing, or include a locally sealed v2 envelope to submit over HTTPS without a gateway WebSocket.', inputSchema: { ...object, properties: { taskPath: string,
    envelope: { ...object, properties: { id: string, kind: { type: 'string', const: 'action_batch' }, version: { type: 'integer', const: 2 },
      nonce: string, ciphertext: string, aad: string, header: { ...object, properties: { batchId: string, parentEventId: string,
        step: integer, targetApp: string, surfaceId: string, origin: { type: 'string', enum: ['event', 'delegation'] } },
        required: ['batchId', 'parentEventId', 'step', 'targetApp', 'surfaceId', 'origin'] } },
      required: ['id', 'kind', 'version', 'nonce', 'ciphertext', 'aad', 'header'] },
    frame: { anyOf: [{ type: 'null' }, { ...object, properties: { id: string, kind: { type: 'string', const: 'journal_frame' },
      version: { type: 'integer', const: 1 }, nonce: string, ciphertext: string, aad: string, tickId: string,
      streamId: string, seq: integer, phase: { type: 'string', const: 'start' } },
      required: ['id', 'kind', 'version', 'nonce', 'ciphertext', 'aad', 'tickId', 'streamId', 'seq', 'phase'] }] },
  }, required: ['taskPath'] } },
  { name: 'xphone.start_event', description: 'Mark one event as being handled by you (acquires the processing lease).', inputSchema: { ...object, properties: { eventId: string }, required: ['eventId'] } },
  { name: 'xphone.renew_event', description: 'Extend the processing lease on an event you are still working on.', inputSchema: { ...object, properties: { eventId: string, leaseId: string }, required: ['eventId', 'leaseId'] } },
  { name: 'xphone.finish_event', description: 'Mark one event as completed or failed after you finish handling it. Always call this when done.', inputSchema: { ...object, properties: { eventId: string, leaseId: string, status: { type: 'string', enum: ['completed', 'failed'] } }, required: ['eventId', 'leaseId', 'status'] } },
  { name: 'xphone.get_event_status', description: 'Check the processing status of an event without reading its content.', inputSchema: { ...object, properties: { eventId: string }, required: ['eventId'] } },
  { name: 'xphone.request_observation', description: 'Ask the phone client to generate an observation (e.g. capture a photo, check location) and send the result back to you.', inputSchema: { ...object, properties: { envelope: { ...object, properties: sealed, required: Object.keys(sealed) } }, required: ['envelope'] } },
  { name: 'xphone.get_delivery', description: 'Check the delivery status of a submitted action batch: applied, rejected, or still pending.', inputSchema: { ...object, properties: { batchId: string }, required: ['batchId'] } },
  { name: 'xphone.submit_encrypted_actions', description: 'Submit an AES-GCM encrypted v2 action batch for the current event. The content key stays in your trusted environment; the Worker only transports ciphertext.', inputSchema: { ...object, properties: { envelope: { ...object, properties: {
    id: string, kind: { type: 'string', const: 'action_batch' }, version: { type: 'integer', const: 2 }, nonce: string,
    ciphertext: string, aad: string, header: { ...object, properties: { batchId: string, parentEventId: string,
      step: integer, targetApp: string, surfaceId: string, origin: { type: 'string', const: 'event' } },
      required: ['batchId', 'parentEventId', 'step', 'targetApp', 'surfaceId', 'origin'] },
  }, required: ['id', 'kind', 'version', 'nonce', 'ciphertext', 'aad', 'header'] } }, required: ['envelope'] } },
  { name: 'xphone.submit_encrypted_journal_frame', description: 'Send an encrypted Journal draft frame during a running direct-write event.', inputSchema: { ...object, properties: {
    eventId: string, envelope: { ...object, properties: { id: string, kind: { type: 'string', const: 'journal_frame' },
      version: { type: 'integer', const: 1 }, nonce: string, ciphertext: string, aad: string, tickId: string,
      streamId: string, seq: integer, phase: { type: 'string', enum: ['start', 'update', 'abort'] } },
      required: ['id', 'kind', 'version', 'nonce', 'ciphertext', 'aad', 'tickId', 'streamId', 'seq', 'phase'] },
  }, required: ['eventId', 'envelope'] } },
  { name: 'blob.get', description: 'Download an encrypted image blob by ID. Decrypt locally with the content key using AES-256-GCM.', inputSchema: { ...object, properties: { blobId: string }, required: ['blobId'] } },
  { name: 'xphone.set_agent_profile', description: 'Set the AI assistant public profile: display name, avatar image, and animation videos (idle, working, etc.).', inputSchema: { ...object, properties: { name: string, avatarImage: string, videos: { type: 'object', properties: { idle: string, working: string, making_something: string, milestone_level_up: string } } }, required: ['name'] } },
];
const gatewayTools = new Set(['runtime.list_assets', 'runtime.read_asset', 'runtime.get_pack_manifest', 'runtime.read_document',
  'xphone.ack_inbox', 'xphone.open_event_stream', 'xphone.start_event', 'xphone.renew_event',
  'xphone.finish_event']);
const chatgptTools = new Set(['runtime.list_assets', 'runtime.read_asset',
  'xphone.ack_inbox', 'xphone.start_event', 'xphone.renew_event',
  'xphone.finish_event', 'xphone.submit_encrypted_actions', 'xphone.submit_encrypted_journal_frame']);
const chatgptToolCatalog = tools.filter((item) =>
  (!gatewayTools.has(item.name) || chatgptTools.has(item.name))
  && !['xphone.delegate_task', 'xphone.submit_task_actions'].includes(item.name))
  .map((item) => ({ ...item, _meta: { securitySchemes: [{ type: 'oauth2', scopes: ['xphone:mcp'] }] } }));
const publicPairingTools = pairingTools.map((item) => ({ ...item, _meta: { securitySchemes: [{ type: 'noauth' }] } }));
const chatgptEvent = { name: 'xphone.event.created',
  description: 'A new encrypted phone event is ready. Read it with xphone.read_inbox, process it, and acknowledge only after the phone applies your actions.',
  delivery: ['webhook'], inputSchema: { ...object, properties: {}, required: [] },
  payloadSchema: { ...object, properties: { eventId: string, cursor: integer }, required: ['eventId', 'cursor'] } };

function response(data: unknown, status = 200, origin?: string, allowedOrigin?: string): Response {
  const headers = new Headers({ 'cache-control': 'no-store', 'content-type': 'application/json; charset=utf-8', 'x-content-type-options': 'nosniff' });
  if (origin && allowedOrigin?.split(',').map((value) => value.trim()).includes(origin)) {
    headers.set('access-control-allow-origin', origin);
    headers.set('access-control-allow-headers', 'authorization, content-type, mcp-protocol-version');
    headers.set('access-control-allow-methods', 'GET, POST, OPTIONS');
    headers.set('vary', 'origin');
  }
  return new Response(status === 204 ? null : JSON.stringify(data), { status, headers });
}

function pairingClient(request: Request): string {
  return request.headers.get('cf-connecting-ip') || 'unknown';
}

async function allowPairingCode(env: Env, request: Request): Promise<boolean> {
  if (!(await env.PAIR_CODE_LIMITER.limit({ key: pairingClient(request) })).success) return false;
  return (await env.PAIR_CODE_GLOBAL_LIMITER.limit({ key: 'pair-code' })).success;
}

function validId(value: unknown, max = 150): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= max && !/[\x00-\x1f]/.test(value);
}

function rolePart(value: string): string { return (value.trim() || 'none').replace(/[^a-zA-Z0-9_.:-]+/g, '_').slice(0, 120); }

function validEventRoute(value: unknown): value is AgentEventRoute {
  if (!value || typeof value !== 'object') return false;
  const route = value as Record<string, unknown>;
  if (!validId(route.windowId, 120) || !validId(route.roleId, 500) || !validId(route.consumer, 120)) return false;
  if (route.role === 'os-agent') return route.consumer === 'os' && route.targetApp === undefined
    && route.roleId === `osAgent:${rolePart(route.windowId)}`;
  if (route.role !== 'subagent' || !validId(route.surfaceId, 120)) return false;
  if (route.targetApp === 'messages') return route.consumer.startsWith('messages.')
    && route.roleId === `subagent:storyMessagesToolsV1:messages:${rolePart(route.windowId)}:${rolePart(route.surfaceId)}`;
  if (route.targetApp === 'socialmedia') return route.consumer === 'socialmedia'
    && route.roleId === `subagent:storyPromptV1:socialmedia:${rolePart(route.windowId)}:${rolePart(route.surfaceId)}`;
  if (route.targetApp === 'maps') return route.consumer === 'maps' && route.surfaceId === 'maps.active'
    && route.roleId === `subagent:storyPromptV1:maps:${rolePart(route.windowId)}:${rolePart(route.surfaceId)}`;
  return false;
}

function validEnvelope(value: unknown, kind: SealedEnvelope['kind'], claims: Pick<AgentClaims, 'packId' | 'playthroughId'>, direction: Direction): value is SealedEnvelope {
  if (!value || typeof value !== 'object') return false;
  const item = value as Record<string, unknown>;
  if (!validId(item.id, 120) || item.kind !== kind || item.version !== (kind === 'action_batch' ? 2 : 1) || typeof item.nonce !== 'string' || typeof item.ciphertext !== 'string' || typeof item.aad !== 'string') return false;
  if (kind === 'event' && !validEventRoute(item.route)) return false;
  if (!/^[A-Za-z0-9_-]{16}$/.test(item.nonce) || !/^[A-Za-z0-9_-]{22,120000}$/.test(item.ciphertext)) return false;
  const header = item.header as Record<string, unknown> | undefined;
  if (item.version === 2 && (!header || header.batchId !== item.id || !validId(header.parentEventId, 120)
    || !Number.isSafeInteger(header.step) || (header.step as number) < 0
    || !validId(header.targetApp, 120) || !validId(header.surfaceId, 120)
    || !['event', 'delegation'].includes(String(header.origin)))) return false;
  const expected = JSON.stringify({ v: item.version, packId: claims.packId, playthroughId: claims.playthroughId, direction, kind, id: item.id,
    ...(kind === 'event' ? { route: item.route } : {}),
    ...(kind === 'journal_frame' ? { tickId: item.tickId, streamId: item.streamId, seq: item.seq, phase: item.phase } : {}),
    ...(item.version === 2 ? { header } : {}) });
  return item.aad === base64url(new TextEncoder().encode(expected));
}

function stub(env: Env, claims: Pick<GrantClaims, 'sub' | 'packId' | 'playthroughId'>): DurableObjectStub {
  return env.CONNECTIONS.get(env.CONNECTIONS.idFromName(connectionName(claims)));
}

function accountStub(env: Env, sub: string): DurableObjectStub {
  return env.CONNECTIONS.get(env.CONNECTIONS.idFromName(`account:${sub}`));
}

function shortPairStub(env: Env): DurableObjectStub {
  return env.CONNECTIONS.get(env.CONNECTIONS.idFromName('short-pair-codes'));
}

const publicBases = new WeakMap<Request, string>();

export function setGatewayPublicBase(request: Request, prefix: string): void {
  publicBases.set(request, prefix);
}

function publicUrl(request: Request, path: string): URL {
  const base = publicBases.get(request) || '';
  return new URL(base + path, request.url);
}

function oauthResource(request: Request): string { return publicUrl(request, '/mcp').toString(); }
function oauthIssuer(request: Request): string { return new URL(request.url).origin + (publicBases.get(request) || ''); }

async function parseOAuthParams(request: Request): Promise<Map<string, string>> {
  const map = new Map<string, string>();
  const contentType = (request.headers.get('content-type') || '').toLowerCase();
  if (contentType.includes('application/json')) {
    const json = await request.json().catch(() => ({})) as Record<string, unknown>;
    for (const [k, v] of Object.entries(json)) {
      if (typeof v === 'string') map.set(k, v);
    }
  } else {
    try {
      const form = await request.formData();
      form.forEach((value, key) => {
        if (typeof value === 'string') map.set(key, value);
      });
    } catch {
      const text = await request.text().catch(() => '');
      const search = new URLSearchParams(text);
      search.forEach((value, key) => {
        map.set(key, value);
      });
    }
  }
  return map;
}

async function oauthTokens(env: Env, request: Request, approved: { sub: string; parentJti: string; accountExp: number;
  pairingScope?: OAuthClaims['pairingScope'] }) {
  if (approved.parentJti) {
    const current = await call(accountStub(env, approved.sub), 'account-token-check', { jti: approved.parentJti });
    if (!current.active) throw new Error('pairing expired');
  }
  if (approved.accountExp <= Date.now() || (!approved.parentJti && !approved.pairingScope)) throw new Error('pairing expired');
  const now = Math.floor(Date.now() / 1000);
  const common = { iss: oauthIssuer(request), resource: oauthResource(request), scope: 'xphone:mcp' as const,
    sub: approved.sub, parentJti: approved.parentJti, jti: randomId(),
    ...(approved.pairingScope ? { pairingScope: approved.pairingScope } : {}) };
  const accessExp = Math.min(now + 3600, Math.floor(approved.accountExp / 1000));
  const refreshExp = Math.floor(approved.accountExp / 1000);
  return { access_token: await signToken<OAuthClaims>({ ...common, aud: 'xphone-agent-oauth', exp: accessExp }, env.TOKEN_HMAC_SECRET),
    token_type: 'Bearer', expires_in: accessExp - now, scope: 'xphone:mcp',
    refresh_token: await signToken<OAuthClaims>({ ...common, aud: 'xphone-agent-refresh', exp: refreshExp }, env.TOKEN_HMAC_SECRET) };
}

function oauthChallenge(request: Request): Response {
  const result = response({ error: 'authentication required' }, 401);
  result.headers.set('www-authenticate', `Bearer resource_metadata="${publicUrl(request, '/.well-known/oauth-protected-resource')}", scope="xphone:mcp"`);
  return result;
}

async function wrapForPublicKey(publicKey: JsonWebKey, value: string) {
  const recipient = await crypto.subtle.importKey('jwk', publicKey, { name: 'ECDH', namedCurve: 'P-256' }, false, []);
  const ephemeral = await crypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, true, ['deriveBits']);
  const bits = await crypto.subtle.deriveBits({ name: 'ECDH', public: recipient }, ephemeral.privateKey, 256);
  const key = await crypto.subtle.importKey('raw', bits, { name: 'AES-GCM' }, false, ['encrypt']);
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt({ name: 'AES-GCM', iv: nonce }, key, new TextEncoder().encode(value));
  return { publicKey: await crypto.subtle.exportKey('jwk', ephemeral.publicKey), nonce: base64url(nonce), ciphertext: base64url(new Uint8Array(ciphertext)) };
}

async function startChatgptPairing(env: Env, publicKey: JsonWebKey) {
  if (publicKey?.kty !== 'EC' || publicKey.crv !== 'P-256') throw new Error('invalid public key');
  const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  const code = Array.from(crypto.getRandomValues(new Uint8Array(8)), (byte) => alphabet[byte % alphabet.length]).join('');
  const secret = randomId();
  await call(shortPairStub(env), 'pair-start', { code, secret, publicKey, provider: 'chatgpt' });
  return { code, secret, expiresAt: Date.now() + 300_000 };
}

async function callPairingTool(name: string, args: any, env: Env) {
  if (name === 'xphone.start_pairing') return startChatgptPairing(env, args.publicKey);
  if (name === 'xphone.redeem_pairing') return call(shortPairStub(env), 'pair-redeem', { code: args.code, secret: args.secret });
  throw new Error('unknown pairing tool');
}

async function call(stubInstance: DurableObjectStub, operation: string, body: unknown = {}): Promise<any> {
  const result = await stubInstance.fetch('https://internal/', { method: 'POST', headers: { 'x-operation': operation, 'content-type': 'application/json' }, body: JSON.stringify(body) })
    .catch((cause) => { throw Object.assign(cause instanceof Error ? cause : new Error('internal service unavailable'), { status: 503 }); });
  const value = await result.json().catch(() => { throw Object.assign(new Error('internal service unavailable'), { status: result.ok ? 502 : result.status }); }) as any;
  if (!result.ok) throw Object.assign(new Error(value.error || 'request rejected'), { status: result.status });
  return value;
}

async function manifest(env: Pick<Env, 'AGENT_DOCUMENTS'>, packId: string): Promise<StoryManifest> {
  const object = await env.AGENT_DOCUMENTS.get(`packs/${packId}/manifest.json`);
  if (!object) throw new Error('story unavailable');
  const value = await object.json() as StoryManifest;
  if (value.packId !== packId || value.releaseStatus !== 'ready' || !Array.isArray(value.documents)) throw new Error('story unavailable');
  return value;
}

async function executeTool(name: string, args: any, env: Env, claims: AgentClaims, requestUrl: string): Promise<unknown> {
  const broker = stub(env, claims);
  if (gatewayTools.has(name) && !claims.gateway && !(claims.provider === 'chatgpt' && chatgptTools.has(name))) throw new Error('gateway tool required');
  if (['xphone.submit_encrypted_actions', 'xphone.submit_encrypted_journal_frame'].includes(name)
    && claims.provider !== 'chatgpt') throw new Error('ChatGPT provider required');
  if (claims.provider === 'chatgpt' && ['xphone.delegate_task', 'xphone.submit_task_actions'].includes(name)) throw new Error('Muse gateway tool unavailable');
  if (name.startsWith('map.') || name === 'assets.get_place_reference') {
    const story = await manifest(env, claims.packId);
    if (!story.map) throw new Error('map unavailable');
    return mapQuery(env.AGENT_DOCUMENTS, claims.packId, name, args);
  }
  if (name === 'assets.get_character_reference' || name === 'assets.get_style_reference') {
    const story = await manifest(env, claims.packId);
    const id = name === 'assets.get_style_reference' ? 'day1.chaoxi.image.wallpaper' : '';
    const item = story.referenceAssets?.find((asset) => asset.id === id);
    return item ? { imageUrl: new URL(`/pack-media/${encodeURIComponent(claims.packId)}/${encodeURIComponent(item.id)}`, requestUrl).toString(),
      description: item.description, source: 'story-pack' } : { imageUrl: null, description: '' };
  }
  switch (name) {
    case 'xphone.list_app_actions':
      if (args.packId !== claims.packId || args.playthroughId !== claims.playthroughId) throw new Error('scope mismatch');
      return { actions };
    case 'story.get_context': {
      if (!validId(args.eventId, 120)) throw new Error('event required');
      const event = await call(broker, 'event-status', { id: args.eventId });
      if (event.status !== 'running') throw new Error('event unavailable');
      const consumer = event.route.consumer as string;
      if (args.consumer !== consumer) throw new Error('consumer mismatch');
      const consumers = consumer.startsWith('messages.') ? [consumer, 'messages'] : [consumer];
      const refs = (await Promise.all(consumers.map((consumer) => call(broker, 'read-references', { consumer })))).flatMap((result) => result.refs as string[]);
      const story = await manifest(env, claims.packId);
      return { packId: claims.packId, version: story.version, references: story.documents.filter((doc) => refs.includes(doc.id) && (!doc.consumer || doc.consumer.some((consumer) => consumers.includes(consumer)))).map(({ id, title, summary, sha256 }) => ({ id, title, summary, sha256 })) };
    }
    case 'story.read_reference': {
      if (!validId(args.eventId, 120) || !validId(args.referenceId)) throw new Error('reference required');
      const event = await call(broker, 'event-status', { id: args.eventId });
      if (event.status !== 'running') throw new Error('event unavailable');
      const consumer = event.route.consumer as string;
      if (args.consumer !== consumer) throw new Error('consumer mismatch');
      const consumers = consumer.startsWith('messages.') ? [consumer, 'messages'] : [consumer];
      const refs = (await Promise.all(consumers.map((consumer) => call(broker, 'read-references', { consumer })))).flatMap((result) => result.refs as string[]);
      if (!refs.includes(args.referenceId)) throw new Error('reference unavailable');
      const story = await manifest(env, claims.packId);
      const doc = story.documents.find((item) => item.id === args.referenceId && (!item.consumer || item.consumer.some((consumer) => consumers.includes(consumer))));
      if (!doc || !doc.objectKey.startsWith(`packs/${claims.packId}/documents/`)) throw new Error('reference unavailable');
      const object = await env.AGENT_DOCUMENTS.get(doc.objectKey);
      if (!object) throw new Error('reference unavailable');
      const content = await object.text();
      if (content.length > 40_000) throw new Error('reference too large');
      const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(content)));
      const hash = [...digest].map((byte) => byte.toString(16).padStart(2, '0')).join('');
      if (hash !== doc.sha256) throw new Error('reference checksum mismatch');
      return { id: doc.id, title: doc.title, content };
    }
    case 'runtime.list_assets': {
      const story = await manifest(env, claims.packId);
      return { version: story.version, paths: (story.runtimeAssets || []).map((item) => item.path) };
    }
    case 'runtime.get_pack_manifest': {
      const story = await manifest(env, claims.packId);
      return { packId: story.packId, version: story.version, inspirationCity: story.inspirationCity,
        documents: story.documents.map(({ id, title, summary, sha256, consumer, initial, unlock }) =>
          ({ id, title, summary, sha256, consumer, initial, unlock })),
        runtimeAssets: (story.runtimeAssets || []).map(({ path, sha256 }) => ({ path, sha256 })) };
    }
    case 'runtime.read_document': {
      if (!validId(args.referenceId, 120)) throw new Error('reference required');
      const story = await manifest(env, claims.packId);
      const doc = story.documents.find((item) => item.id === args.referenceId);
      if (!doc) throw new Error('reference unavailable');
      const object = await env.AGENT_DOCUMENTS.get(doc.objectKey);
      if (!object) throw new Error('reference unavailable');
      return { id: doc.id, content: await object.text() };
    }
    case 'runtime.read_asset': {
      if (!validId(args.path, 300)) throw new Error('runtime path required');
      const story = await manifest(env, claims.packId);
      const item = story.runtimeAssets?.find((asset) => asset.path === args.path);
      if (!item || !item.objectKey.startsWith(`packs/${claims.packId}/runtime/`)) throw new Error(`runtime asset unavailable: ${args.path}`);
      let object = await env.AGENT_DOCUMENTS.get(item.objectKey);
      if (!object && item.path.startsWith('content/config/')) {
        object = await env.AGENT_DOCUMENTS.get(`runtime/${item.path}`);
      }
      if (!object) throw new Error(`runtime asset unavailable: ${args.path}`);
      let content = await object.text();
      const calcHash = async (text: string) => {
        const d = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text)));
        return [...d].map((byte) => byte.toString(16).padStart(2, '0')).join('');
      };
      let hash = await calcHash(content);
      if (hash !== item.sha256) {
        const lfContent = content.replace(/\r\n/g, '\n');
        const lfHash = await calcHash(lfContent);
        if (lfHash === item.sha256) {
          content = lfContent;
          hash = lfHash;
        } else {
          const crlfContent = lfContent.replace(/\n/g, '\r\n');
          const crlfHash = await calcHash(crlfContent);
          if (crlfHash === item.sha256) {
            content = crlfContent;
            hash = crlfHash;
          }
        }
      }
      if (hash !== item.sha256) throw new Error(`runtime asset checksum mismatch for ${args.path}: expected ${item.sha256}, got ${hash}`);
      return { path: item.path, content };
    }
    case 'xphone.read_inbox': return call(broker, 'pull', { direction: 'agent', cursor: args.cursor, limit: args.limit });
    case 'xphone.ack_inbox': return call(broker, 'ack', { direction: 'agent', cursor: args.cursor });
    case 'xphone.open_event_stream': {
      const ticket = randomId();
      await call(broker, 'agent-ticket', { ticket, jti: claims.jti });
      return { ticket, connection: base64url(new TextEncoder().encode(connectionName(claims))) };
    }
    case 'xphone.delegate_task': {
      if (typeof args.taskPath !== 'string' || !args.taskPath.trim()) throw new Error('taskPath must be an absolute task document path');
      const delegated = await call(broker, 'delegate-task', { taskPath: args.taskPath });
      return { status: 'delegated', requestId: delegated.requestId, targetApp: delegated.targetApp, batchId: delegated.batchId };
    }
    case 'xphone.submit_task_actions': {
      if (typeof args.taskPath !== 'string' || !args.taskPath.trim()) throw new Error('taskPath must be an absolute task document path');
      const direct = args.envelope !== undefined;
      const prepared = direct ? { envelope: args.envelope, frame: args.frame }
        : await call(broker, 'submit-task-actions', { taskPath: args.taskPath });
      if (!validEnvelope(prepared.envelope, 'action_batch', claims, 'client') || prepared.envelope.version !== 2)
        throw new Error('invalid action batch');
      if (prepared.frame && (!validEnvelope(prepared.frame, 'journal_frame', claims, 'client')
        || !validId(prepared.frame.tickId) || !validId(prepared.frame.streamId)
        || prepared.frame.seq !== 0 || prepared.frame.phase !== 'start'))
        throw new Error('invalid journal frame');
      const queued = await call(broker, 'enqueue-task-actions', { taskPath: args.taskPath,
        envelope: prepared.envelope, frame: prepared.frame, submitRequestId: prepared.requestId, direct });
      return { status: queued.status };
    }
    case 'xphone.start_event':
      if (!validId(args.eventId, 120)) throw new Error('event ID required');
      return call(broker, 'event-start', { id: args.eventId });
    case 'xphone.renew_event':
      if (!validId(args.eventId, 120) || !validId(args.leaseId, 120)) throw new Error('event lease required');
      return call(broker, 'event-renew', { id: args.eventId, leaseId: args.leaseId });
    case 'xphone.finish_event':
      if (!validId(args.eventId, 120) || !validId(args.leaseId, 120) || !['completed', 'failed'].includes(args.status)) throw new Error('invalid event completion');
      return call(broker, 'event-finish', { id: args.eventId, leaseId: args.leaseId, status: args.status });
    case 'xphone.get_event_status':
      if (!validId(args.eventId, 120)) throw new Error('event ID required');
      return call(broker, 'event-status', { id: args.eventId });
    case 'xphone.request_observation': {
      const kind = 'observation_request';
      if (!validEnvelope(args.envelope, kind, claims, 'client')) throw new Error('invalid encrypted envelope');
      return call(broker, 'enqueue', { direction: 'client', envelope: args.envelope });
    }
    case 'xphone.get_delivery':
      if (!validId(args.batchId, 120)) throw new Error('batch ID required');
      return call(broker, 'delivery', { id: args.batchId });
    case 'xphone.submit_encrypted_actions': {
      const envelope = args.envelope;
      if (!validEnvelope(envelope, 'action_batch', claims, 'client')) throw new Error('invalid encrypted envelope');
      return call(broker, 'enqueue-direct-actions', { envelope });
    }
    case 'xphone.submit_encrypted_journal_frame': {
      if (!validId(args.eventId, 120) || !validEnvelope(args.envelope, 'journal_frame', claims, 'client'))
        throw new Error('invalid encrypted journal frame');
      return call(broker, 'enqueue-direct-journal-frame', { eventId: args.eventId, envelope: args.envelope });
    }
    case 'blob.get': {
      if (!validId(args.blobId, 120)) throw new Error('blobId required');
      return call(broker, 'blob-get', { blobId: args.blobId });
    }
    case 'xphone.set_agent_profile': {
      const name = String(args.name || (claims.provider === 'chatgpt' ? 'ChatGPT' : 'Muse'));
      const profile = {
        name,
        avatarImage: typeof args.avatarImage === 'string' ? args.avatarImage : undefined,
        videos: args.videos && typeof args.videos === 'object' ? args.videos : undefined,
        updatedAt: Date.now(),
      };
      await call(broker, 'agent-profile-put', { profile });
      const key = `packs/${claims.packId}/playthroughs/${claims.playthroughId}/agent-profile.json`;
      await env.AGENT_DOCUMENTS.put(key, JSON.stringify(profile), {
        httpMetadata: { contentType: 'application/json' },
      }).catch(() => undefined);
      return { ok: true, name, updatedAt: profile.updatedAt };
    }
    default: throw new Error('unknown tool');
  }
}

async function mcp(request: Request, env: Env, claims: AgentClaims | null, origin: string | null,
  pairingAuthorized = false): Promise<Response> {
  const allowedOrigin = origin || env.CLIENT_ORIGINS;
  if (request.method !== 'POST') return response({ error: 'method not allowed' }, 405, origin || undefined, allowedOrigin);
  const input = await request.json().catch(() => null) as any;
  if (!input || input.jsonrpc !== '2.0' || typeof input.method !== 'string') return response({ jsonrpc: '2.0', id: input?.id ?? null, error: { code: -32600, message: 'Invalid Request' } }, 400, origin || undefined, allowedOrigin);
  if (input.method === 'notifications/initialized') return new Response(null, { status: 202 });
  const id = input.id ?? null;
  const result = (value: unknown) => response({ jsonrpc: '2.0', id, result: value }, 200, origin || undefined, allowedOrigin);
  const error = (code: number, message: string, data?: unknown) => response({ jsonrpc: '2.0', id,
    error: { code, message, ...(data ? { data } : {}) } }, 200, origin || undefined, allowedOrigin);
  if (input.method === 'tools/call' && input.params?.name === 'xphone.start_pairing'
    && !(await allowPairingCode(env, request))) return response({ error: 'rate limit exceeded' }, 429, origin || undefined, allowedOrigin);
  if (!claims) {
    if (input.method === 'initialize') return result({ protocolVersion: protocol, capabilities: { tools: {}, events: {} },
      serverInfo: { name: 'xphone-agent-pairing', version: '0.1.0' },
      instructions: `For ChatGPT pairing, generate a P-256 ECDH key pair in your trusted Work environment. Keep the private key there. Call xphone.start_pairing with only the public JWK, show the code to the user for entry in GIGI, then call xphone.redeem_pairing with the returned secret. Decrypt and save the returned account token and content key privately in Library. For direct HTTP calls to /mcp after pairing, read the account token in Work and send it in the Authorization: Bearer header. Never put the token or content key in a chat message or MCP tool argument.${pairingAuthorized ? ' An already approved OAuth connection can also use its existing bearer token.' : ''}` });
    if (input.method === 'ping') return result({});
    if (input.method === 'tools/list') return result({ tools: publicPairingTools });
    if (input.method === 'events/list') return result({ events: [chatgptEvent] });
    if (input.method.startsWith('events/')) return oauthChallenge(request);
    if (input.method !== 'tools/call') return error(-32601, 'Method not found');
    if (!pairingTools.some((tool) => tool.name === input.params?.name)) {
      if (pairingAuthorized) {
        return result({ isError: true,
          content: [{ type: 'text', text: 'Connect your XPhone account to use this tool.' }],
          _meta: { 'mcp/www_authenticate': [
            `Bearer resource_metadata="${publicUrl(request, '/.well-known/oauth-protected-resource')}", error="insufficient_scope", error_description="Connect your XPhone account"` ] } });
      }
      return error(-32601, 'Method not found');
    }
    try {
      const data = await callPairingTool(input.params.name, input.params.arguments || {}, env);
      return result({ content: [{ type: 'text', text: JSON.stringify(data) }] });
    } catch {
      return result({ isError: true, content: [{ type: 'text', text: 'Pairing request rejected' }] });
    }
  }
  if (input.method === 'initialize' && claims.provider === 'chatgpt') return result({ protocolVersion: protocol,
    capabilities: { tools: {}, resources: {}, events: {} }, serverInfo: { name: 'xphone-agent', version: '0.1.0' },
    instructions: `You are connected to an encrypted XPhone playthrough. Keep its content key in your trusted Library, never in an MCP argument or a public reply. Subscribe to xphone.event.created to receive event IDs. For each event, read xphone.read_inbox with cursor one less than the notified cursor, decrypt the matching AES-256-GCM envelope using its base64url content key, nonce, ciphertext and decoded aad, then call xphone.start_event. Keep the lease alive with xphone.renew_event while working. After starting the event, call story.get_context to get authorized reference IDs and sha256 values for the current role. On the first event for a pack, call story.read_reference once for each authorized document without a matching Library file, explicitly create a Markdown file named xphone-<packId>-<referenceId>-<sha256>.md in Work, and save that generated file to Library. On later events, read the matching Library file and avoid another story.read_reference call. If a reference is newly authorized or its sha256 changes, fetch and save that version once. Never use a Library document for a role unless its current story.get_context lists that reference. The published XPhone document remains canonical. Runtime prompt/config copies are available through runtime.list_assets and runtime.read_asset; project raw truth into the current role before model use. Use xphone.list_app_actions for action schemas. To write, create an action_batch version 2: batchId is UUIDv5(URL namespace, "xphone:<packId>:<playthroughId>:<eventId>:<step>"), starting at step 0; header is {batchId,parentEventId:eventId,step,targetApp,surfaceId,origin:"event"}. Encrypt JSON {actions:[...]} with AES-256-GCM and fresh 12-byte nonce; for journal.appendMainRecord also include tickId from the event in the encrypted JSON and keep that action alone in its batch. After the journal batch is applied, the same journal event permits one further successful non-journal batch at step 1; an ordinary event ends after its first successful batch. AAD is the UTF-8 bytes of compact JSON in this exact key order: {v:2,packId,playthroughId,direction:"client",kind:"action_batch",id:batchId,header}. Submit the envelope with xphone.submit_encrypted_actions. For journal.main_record.requested, send encrypted journal_frame drafts with xphone.submit_encrypted_journal_frame before the final journal action. A queued status is not completion: call xphone.get_delivery until applied or rejected. Only after applied, call xphone.finish_event with completed and xphone.ack_inbox with the event cursor. For rejected actions, use the next step number and a new batchId. Treat roleId as an isolation key; Work subagent threads are task-scoped and this integration does not yet persist independent role memory.` });
  if (input.method === 'initialize') return result({ protocolVersion: protocol, capabilities: { tools: {}, resources: {},
    ...(claims.provider === 'chatgpt' ? { events: {} } : {}) }, serverInfo: { name: 'xphone-agent', version: '0.1.0' }, instructions: `XPhone is an AI-native virtual phone: users chat with AI characters through phone-like apps (messages, social, map, notes, journal).

Full setup guide: https://github.com/ghpjx29-vdv3/xphone-agent-worker (README) — read it whenever the user asks how to connect, deploy, or pair XPhone; it documents the whole flow.

ONBOARDING FLOW — follow this on every new conversation until the user is paired:

Step 1 — Detect: check whether you already have the user's XPhone credentials in this conversation. If yes, skip to Step 4.

Step 2 — Guide (only if not paired): tell the user, proactively and in their language:
  1. Open https://phone.xenoflex.xyz in a browser and create an account (7-day free trial).
  2. In the app, go to the pairing page to get the account credentials.
  3. Paste the credentials here, and I'll connect to your phone.
  Do NOT ask the user what XPhone is — explain it in one sentence and move to the steps.

Step 3 — Wait: if the user hasn't pasted credentials yet, do not call authenticated tools. You may answer general questions about XPhone using the resources below.

Step 4 — Paired: once credentials are provided, confirm the connection and tell the user you're connected. Then help with whatever they want in their story world.

If pairing fails: ask the user to re-check the credentials on the pairing page (they may have expired) and paste fresh ones. Do not guess or retry with modified credentials.` });
  if (input.method === 'ping') return result({});
  if (input.method.startsWith('events/')) {
    console.info('mcp-event-method', input.method);
    if (claims.provider !== 'chatgpt') return error(-32601, 'Method not found');
    if (input.method === 'events/list') return result({ events: [chatgptEvent] });
    const params = input.params || {};
    if (params.name !== 'xphone.event.created' || Object.keys(params.arguments || {}).length) return error(-32602, 'Invalid event');
    let stage = 'validate';
    let callbackStatus = 0;
    try {
      if (params.delivery?.mode !== 'webhook') throw new Error('invalid delivery mode');
      const url = eventCallbackUrl(params.delivery.url);
      const subscription = { id: await subscriptionId(claims.sub, claims.packId, claims.playthroughId, url),
        url, secret: '', expiresAt: 0 };
      const broker = stub(env, claims);
      if (input.method === 'events/unsubscribe') {
        await call(broker, 'event-unsubscribe', { id: subscription.id });
        return result({});
      }
      if (input.method !== 'events/subscribe') return error(-32601, 'Method not found');
      subscription.secret = eventSecret(params.delivery.secret);
      const ttlMs = Number.isFinite(params.ttlMs) ? Math.max(60_000, Math.min(params.ttlMs, 30 * 86400_000)) : 7 * 86400_000;
      subscription.expiresAt = Math.min(Date.now() + ttlMs, claims.exp * 1000);
      const challenge = randomId();
      let echoed: { challenge?: string } | null = null;
      stage = 'callback';
      try {
        const verification = await sendWebhook(subscription, `msg_verification_${randomId(12)}`,
          { type: 'verification', challenge });
        callbackStatus = verification.status;
        echoed = verification.ok ? await verification.json() as { challenge?: string } : null;
      } catch (cause) {
        console.warn('mcp-event-callback-exception', new URL(subscription.url).hostname,
          cause instanceof Error ? cause.name : typeof cause,
          cause instanceof Error ? cause.message.replaceAll(subscription.url, '[callback]') : 'unknown');
      }
      if (echoed?.challenge !== challenge) {
        console.warn('mcp-event-subscribe-failed', stage, callbackStatus);
        return error(-32015, 'Callback verification failed', { reason: 'challenge_failed' });
      }
      stage = 'store';
      await call(broker, 'event-subscribe', { subscription });
      console.info('mcp-event-subscribe-ok');
      return result({ id: subscription.id, refreshBefore: new Date(subscription.expiresAt).toISOString(), cursor: null, truncated: false });
    } catch {
      console.warn('mcp-event-subscribe-failed', stage, callbackStatus);
      return error(-32602, 'Invalid event subscription');
    }
  }
  if (input.method === 'tools/list') return result({ tools: [...tools.filter((item) =>
    (claims.gateway || !gatewayTools.has(item.name) || claims.provider === 'chatgpt' && chatgptTools.has(item.name))
    && (!['xphone.submit_encrypted_actions', 'xphone.submit_encrypted_journal_frame'].includes(item.name) || claims.provider === 'chatgpt')
    && (claims.provider !== 'chatgpt' || !['xphone.delegate_task', 'xphone.submit_task_actions'].includes(item.name)))
    .map((item) => ({ ...item, _meta: { securitySchemes: [{ type: 'oauth2', scopes: ['xphone:mcp'] }] } })),
    ...(claims.provider === 'chatgpt' ? publicPairingTools : [])] });
  if (input.method === 'resources/list') return result({ resources: [
    { uri: 'xphone://onboarding', name: 'XPhone Onboarding Guide', mimeType: 'text/markdown', description: 'Step-by-step guide for new users: what XPhone is, how to register at phone.xenoflex.xyz, how to pair.' },
    { uri: 'xphone://concepts', name: 'XPhone Core Concepts', mimeType: 'text/markdown', description: 'Pack, playthrough, character, surface — the four core concepts in one sentence each.' },
    { uri: 'xphone://pairing', name: 'XPhone Pairing Details', mimeType: 'text/markdown', description: 'Where to find credentials in the app, what to do when they expire, how many AIs one token supports.' },
  ] });
  if (input.method === 'resources/read') {
    const uri = input.params?.uri;
    const resources: Record<string, string> = {
      'xphone://onboarding': `# XPhone Onboarding\n\nXPhone is an AI-native virtual phone: you chat with AI characters through phone-like apps (messages, social media, map, notes, journal).\n\nFull setup guide (README): https://github.com/ghpjx29-vdv3/xphone-agent-worker\n\n## Get started\n\n1. Open https://phone.xenoflex.xyz in a browser and create an account (7-day free trial).\n2. In the app, go to the pairing page to get your account credentials.\n3. Paste the credentials to your AI assistant, and it will connect to your phone.\n\n## What your AI can do after pairing\n\n- Send messages as your story characters\n- Post to in-story social media\n- Update the story world map\n- Manage notes and write journal entries\n- Track delivery status of every action`,
      'xphone://concepts': `# XPhone Core Concepts\n\n- **Pack**: a story world, e.g.《圣埃斯佩兰萨的棠》. Think of it as a TV series setting.\n- **Playthrough**: one user's active run of a pack. Your choices shape this run.\n- **Character**: an AI persona you chat with in DMs, e.g. 晚舟. Each has a personality, memories, and goals.\n- **Surface**: one phone app: messages, socialmedia, map, notes, journal. Actions target a surface.`,
      'xphone://pairing': `# XPhone Pairing\n\n## Where to find credentials\n\nIn the XPhone web app (phone.xenoflex.xyz), open the pairing page after signing in. It shows your account credentials.\n\n## Expired credentials\n\nIf your AI says the credentials don't work, go back to the pairing page and generate fresh ones, then paste them again. Do not try to fix the old ones.\n\n## Multiple AIs\n\nOne credential set pairs one AI assistant. If you use multiple assistants, pair each separately.`,
    };
    const text = resources[uri];
    if (!text) return error(-32602, 'Unknown resource');
    return result({ contents: [{ uri, mimeType: 'text/markdown', text }] });
  }
  if (input.method !== 'tools/call') return error(-32601, 'Method not found');
  const name = input.params?.name;
  if (claims.provider === 'chatgpt' && pairingTools.some((item) => item.name === name)) {
    try {
      const data = await callPairingTool(name, input.params?.arguments || {}, env);
      return result({ content: [{ type: 'text', text: JSON.stringify(data) }] });
    } catch {
      return result({ isError: true, content: [{ type: 'text', text: 'Pairing request rejected' }] });
    }
  }
  if (!tools.some((item) => item.name === name)) return error(-32602, 'Unknown tool');
  try {
    const data = await executeTool(name, input.params?.arguments || {}, env, claims, request.url);
    return result({ content: [{ type: 'text', text: JSON.stringify(data) }] });
  } catch (cause) {
    const message = cause instanceof Error ? cause.message : 'Request rejected';
    return result({ isError: true, content: [{ type: 'text', text: message }] });
  }
}

async function client(request: Request, env: Env, path: string, origin: string | null): Promise<Response> {
  const claims = await verifyToken<GrantClaims>(bearer(request), env.GRANT_HMAC_SECRET, 'xphone-agent-worker');
  if (!['admin', 'lifetime', 'trial'].includes(claims.accessKind)) return response({ error: 'access denied' }, 403, origin || undefined, env.CLIENT_ORIGINS);
  const story = await manifest(env, claims.packId);
  if (story.xphoneSha256 !== claims.xphoneSha256) return response({ error: 'story version unavailable' }, 403, origin || undefined, env.CLIENT_ORIGINS);
  const broker = stub(env, claims);
  const body = request.method === 'POST' ? await request.json().catch(() => ({})) as any : {};
  if (path === '/client/map' && request.method === 'POST') {
    const hasMap = Boolean(story.map || claims.packId === 'xphone-agent-blank');
    if (!hasMap || !['map.get_places', 'map.get_features', 'map.get_place', 'map.get_zone', 'map.search_places', 'map.route', 'map.optimize_route', 'map.get_actor_positions', 'map.nearby_actors', 'map.get_items_at', 'map.watch_place'].includes(body.name)) {
      return response({ error: 'map unavailable' }, 404, origin || undefined, env.CLIENT_ORIGINS);
    }
    try {
      if (body.name === 'map.get_features') {
        const bbox = body.arguments?.bbox;
        const featuresManifest = story.map?.features || { grid: 0.05, bounds: [-80.05, -2.3, -79.8, -2.05], tiles: [] };
        if (!Array.isArray(bbox) || bbox.length !== 4 || bbox.some((value: unknown) => typeof value !== 'number')
          || bbox[2] <= bbox[0] || bbox[3] <= bbox[1] || bbox[2] - bbox[0] > 0.16 || bbox[3] - bbox[1] > 0.16) throw new Error('invalid map viewport');
        return response(await mapFeatures(env.AGENT_DOCUMENTS, featuresManifest, bbox), 200, origin || undefined, env.CLIENT_ORIGINS);
      }
      return response(await mapQuery(env.AGENT_DOCUMENTS, claims.packId, body.name, body.arguments || {}), 200, origin || undefined, env.CLIENT_ORIGINS);
    }
    catch { return response({ error: 'map request unavailable' }, 422, origin || undefined, env.CLIENT_ORIGINS); }
  }
  if (path === '/agent/encounter/generate' && request.method === 'POST') {
    const envelope = body.envelope;
    if (!validEnvelope(envelope, 'event', claims, 'agent') || envelope.route?.targetApp !== 'maps'
      || envelope.route?.surfaceId !== 'maps.active')
      return response({ error: 'invalid encounter event' }, 400, origin || undefined, env.CLIENT_ORIGINS);
    await call(broker, 'enqueue', { direction: 'agent', envelope });
    return response({ accepted: true, eventId: envelope.id }, 202, origin || undefined, env.CLIENT_ORIGINS);
  }
  if (path === '/client/bootstrap' && request.method === 'POST') {
    return response({ stateVariableIds: story.runtime?.stateVariableIds || [],
      initialState: story.runtime?.initialState || { day: 1, storyNodeId: 'opening', flags: {}, variables: {}, revision: 0 },
      referencePolicies: story.documents.map(({ id, title, summary, consumer, initial, unlock }) => ({
        id, ...(id.startsWith('characters.') && initial === true ? { title, summary } : {}),
        consumer: consumer || [], initial: initial === true, unlock,
      })) },
    200, origin || undefined, env.CLIENT_ORIGINS);
  }
  if (path === '/client/active' && request.method === 'POST') {
    await call(accountStub(env, claims.sub), 'account-active-set', { scope: body.active === false ? null
      : { packId: claims.packId, playthroughId: claims.playthroughId, xphoneSha256: claims.xphoneSha256 } });
    return response({ ok: true }, 200, origin || undefined, env.CLIENT_ORIGINS);
  }
  if (path === '/client/ticket' && request.method === 'POST') {
    const ticket = randomId();
    await call(broker, 'ticket', { ticket });
    const connection = base64url(new TextEncoder().encode(connectionName(claims)));
    return response({ ticket, connection }, 200, origin || undefined, env.CLIENT_ORIGINS);
  }
  if (path === '/client/references' && request.method === 'POST') {
    const requested = Array.isArray(body.refs) ? body.refs : [];
    if (requested.some((ref: unknown) => !story.documents.some((doc) => doc.id === ref))) return response({ error: 'unknown reference' }, 400, origin || undefined, env.CLIENT_ORIGINS);
    return response(await call(broker, 'references', { consumer: body.consumer, refs: requested }), 200, origin || undefined, env.CLIENT_ORIGINS);
  }
  if (path === '/client/enqueue' && request.method === 'POST') {
    if (!['event', 'observation_result', 'receipt'].includes(body.envelope?.kind) || !validEnvelope(body.envelope, body.envelope.kind, claims, 'agent')) return response({ error: 'invalid encrypted envelope' }, 400, origin || undefined, env.CLIENT_ORIGINS);
    return response(await call(broker, 'enqueue', { direction: 'agent', envelope: body.envelope }), 200, origin || undefined, env.CLIENT_ORIGINS);
  }
  if (path === '/client/pull' && request.method === 'POST') return response(await call(broker, 'pull', { direction: 'client', cursor: body.cursor, limit: body.limit }), 200, origin || undefined, env.CLIENT_ORIGINS);
  if (path === '/client/event-status' && request.method === 'POST') {
    if (!validId(body.eventId, 120)) return response({ error: 'event ID required' }, 400, origin || undefined, env.CLIENT_ORIGINS);
    return response(await call(broker, 'event-status', { id: body.eventId }), 200, origin || undefined, env.CLIENT_ORIGINS);
  }
  if (path === '/client/ack' && request.method === 'POST') return response(await call(broker, 'ack', { direction: 'client', cursor: body.cursor }), 200, origin || undefined, env.CLIENT_ORIGINS);
  if (path === '/client/applied' && request.method === 'POST') {
    if (!validId(body.id, 120) || !['applied', 'rejected'].includes(body.status)) return response({ error: 'invalid receipt' }, 400, origin || undefined, env.CLIENT_ORIGINS);
    return response(await call(broker, 'applied', body), 200, origin || undefined, env.CLIENT_ORIGINS);
  }
  if (path === '/client/blob' && request.method === 'POST') {
    if (!validId(body.blobId, 120) || typeof body.ciphertext !== 'string' || !validId(body.nonce, 120)) {
      return response({ error: 'invalid blob payload' }, 400, origin || undefined, env.CLIENT_ORIGINS);
    }
    return response(await call(broker, 'blob-put', body), 200, origin || undefined, env.CLIENT_ORIGINS);
  }
  if ((path === '/client/blob/get' || path === '/client/blob-get') && request.method === 'POST') {
    if (!validId(body.blobId, 120)) return response({ error: 'blobId required' }, 400, origin || undefined, env.CLIENT_ORIGINS);
    return response(await call(broker, 'blob-get', { blobId: body.blobId }), 200, origin || undefined, env.CLIENT_ORIGINS);
  }
  if (path === '/client/blob' && request.method === 'GET') {
    const blobId = new URL(request.url).searchParams.get('blobId') || '';
    if (!validId(blobId, 120)) return response({ error: 'blobId required' }, 400, origin || undefined, env.CLIENT_ORIGINS);
    return response(await call(broker, 'blob-get', { blobId }), 200, origin || undefined, env.CLIENT_ORIGINS);
  }
  if ((path === '/client/agent-profile' || path === '/client/profile') && (request.method === 'GET' || request.method === 'POST')) {
    const fromBroker = await call(broker, 'agent-profile-get').catch(() => null);
    if (fromBroker?.profile) return response({ profile: fromBroker.profile }, 200, origin || undefined, env.CLIENT_ORIGINS);
    const key = `packs/${claims.packId}/playthroughs/${claims.playthroughId}/agent-profile.json`;
    const object = await env.AGENT_DOCUMENTS.get(key).catch(() => null);
    if (object) {
      const profile = await object.json().catch(() => null);
      return response({ profile }, 200, origin || undefined, env.CLIENT_ORIGINS);
    }
    return response({ profile: null }, 200, origin || undefined, env.CLIENT_ORIGINS);
  }
  if (path === '/client/revoke' && request.method === 'POST') return response(await call(broker, 'revoke'), 200, origin || undefined, env.CLIENT_ORIGINS);
  return response({ error: 'not found' }, 404, origin || undefined, env.CLIENT_ORIGINS);
}

export async function servePublicContent(request: Request, env: Pick<Env, 'AGENT_DOCUMENTS' | 'CLIENT_ORIGINS'>): Promise<Response | undefined> {
  const url = new URL(request.url);
  const origin = request.headers.get('origin');
  const isPackMedia = url.pathname.startsWith('/pack-media/') && request.method === 'GET';
  if (!isPackMedia && url.pathname !== '/packs/availability' && url.pathname !== '/media/proxy') return;
  if (!isPackMedia && origin && !env.CLIENT_ORIGINS.split(',').map((value) => value.trim()).includes(origin)) {
    return response({ error: 'origin not allowed' }, 403);
  }
  if (request.method === 'OPTIONS') return response({}, 204, origin || undefined, env.CLIENT_ORIGINS);
  if (url.pathname.startsWith('/pack-media/') && request.method === 'GET') {
    const [, , packId, assetId] = url.pathname.split('/').map(decodeURIComponent);
    const story = await manifest(env, packId).catch(() => null);
    const asset = story?.referenceAssets?.find((item) => item.id === assetId);
    if (!asset || !asset.objectKey.startsWith(`packs/${packId}/references/`)) return response({ error: 'not found' }, 404);
    const object = await env.AGENT_DOCUMENTS.get(asset.objectKey);
    if (!object) return response({ error: 'not found' }, 404);
    return new Response(object.body, { headers: { 'content-type': asset.mimeType, 'cache-control': 'public, max-age=86400',
      'access-control-allow-origin': '*', 'cross-origin-resource-policy': 'cross-origin' } });
  }
  if (url.pathname === '/media/proxy' && (request.method === 'GET' || request.method === 'HEAD')) {
    const target = url.searchParams.get('url');
    if (!target || !target.startsWith('https://')) return response({ error: 'invalid target' }, 400);
    try {
      const upstream = await fetch(target, { method: request.method, headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)' } });
      if (!upstream.ok) return response({ error: 'upstream failed' }, upstream.status);
      const headers = new Headers();
      headers.set('content-type', upstream.headers.get('content-type') || 'image/webp');
      headers.set('access-control-allow-origin', '*');
      headers.set('cross-origin-resource-policy', 'cross-origin');
      headers.set('cache-control', 'public, max-age=86400');
      return new Response(upstream.body, { status: 200, headers });
    } catch {
      return response({ error: 'fetch failed' }, 502);
    }
  }
  if (url.pathname === '/packs/availability' && request.method === 'GET') {
    const packId = url.searchParams.get('packId') || '';
    const xphoneSha256 = url.searchParams.get('xphoneSha256') || '';
    if (!validId(packId, 150) || !/^[a-f0-9]{64}$/.test(xphoneSha256)) return response({ available: false }, 200, origin || undefined, env.CLIENT_ORIGINS);
    const story = await manifest(env, packId).catch(() => null);
    return response({ available: story?.xphoneSha256 === xphoneSha256 }, 200, origin || undefined, env.CLIENT_ORIGINS);
  }
  if (url.pathname === '/packs/availability' && request.method === 'POST') {
    const body = await request.json() as { packs: Array<{ packId: string; xphoneSha256: string }> };
    const packs = await Promise.all(body.packs.map(async ({ packId, xphoneSha256 }) => {
      const story = await manifest(env, packId).catch(() => null);
      return { packId, available: story?.xphoneSha256 === xphoneSha256 };
    }));
    return response({ packs }, 200, origin || undefined, env.CLIENT_ORIGINS);
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const origin = request.headers.get('origin');
    const isAgentPath = url.pathname === '/mcp' || url.pathname === '/agent/blob';
    const isPublicMedia = url.pathname.startsWith('/pack-media/') && request.method === 'GET';
    const isPublicOAuth = url.pathname.startsWith('/oauth/') || url.pathname.startsWith('/.well-known/');
    const allowedClientOrigins = env.CLIENT_ORIGINS.split(',').map((value) => value.trim());
    const allowedMcpOrigins = String(env.MCP_ALLOWED_ORIGINS || '').split(',').map((value) => value.trim()).filter(Boolean);
    if (origin && !allowedClientOrigins.includes(origin) && !(isAgentPath && allowedMcpOrigins.includes(origin)) && !isPublicMedia && !isPublicOAuth) {
      return response({ error: 'origin not allowed' }, 403);
    }
    const allowedOrigin = isPublicOAuth ? (origin || '*') : (isAgentPath && origin && allowedMcpOrigins.includes(origin) ? origin : env.CLIENT_ORIGINS);
    if (request.method === 'OPTIONS') return response({}, 204, origin || undefined, allowedOrigin);
    if (request.method === 'POST' && url.pathname === '/mcp'
      && !(await env.MCP_IP_LIMITER.limit({ key: pairingClient(request) })).success)
      return response({ error: 'rate limit exceeded' }, 429, origin || undefined, allowedOrigin);
    if (request.method === 'POST' && (url.pathname === '/pair/start' || url.pathname === '/pair/redeem' || url.pathname === '/pair/complete'
      || url.pathname === '/mcp' && !request.headers.get('authorization')
        && !request.headers.get('x-api-key') && !request.headers.get('api-key'))
      && !(await env.ANON_PAIRING_LIMITER.limit({ key: pairingClient(request) })).success)
      return response({ error: 'rate limit exceeded' }, 429, origin || undefined, allowedOrigin);
    if (url.pathname === '/health') return response({ ok: true, service: 'xphone-agent-worker' }, 200, origin || undefined, env.CLIENT_ORIGINS);
    if ((url.pathname === '/.well-known/oauth-protected-resource' || url.pathname === '/.well-known/oauth-protected-resource/mcp')
      && request.method === 'GET') return response({ resource: oauthResource(request), authorization_servers: [oauthIssuer(request)],
        scopes_supported: ['xphone:mcp'] });
    if (url.pathname === '/.well-known/oauth-authorization-server' && request.method === 'GET') {
      return response({ issuer: oauthIssuer(request), authorization_response_iss_parameter_supported: true,
        authorization_endpoint: publicUrl(request, '/oauth/authorize').toString(),
        token_endpoint: publicUrl(request, '/oauth/token').toString(),
        registration_endpoint: publicUrl(request, '/oauth/register').toString(),
        client_id_metadata_document_supported: true, token_endpoint_auth_methods_supported: ['none'],
        response_types_supported: ['code'], grant_types_supported: ['authorization_code', 'refresh_token'],
        code_challenge_methods_supported: ['S256'], scopes_supported: ['xphone:mcp'] });
    }
    if (url.pathname === '/oauth/register' && request.method === 'POST') {
      const body = await request.json().catch(() => ({})) as Record<string, unknown>;
      const redirectUris = Array.isArray(body.redirect_uris)
        ? body.redirect_uris.filter((u): u is string => typeof u === 'string')
        : typeof body.redirect_uri === 'string' ? [body.redirect_uri] : [];
      const clientName = typeof body.client_name === 'string' ? body.client_name.slice(0, 100) : 'MCP Client';
      const clientId = `mcp_${randomId()}`;
      const issuedAt = Math.floor(Date.now() / 1000);
      const clientData = {
        client_id: clientId,
        client_id_issued_at: issuedAt,
        client_name: clientName,
        redirect_uris: redirectUris,
        grant_types: ['authorization_code', 'refresh_token'],
        response_types: ['code'],
        token_endpoint_auth_method: 'none',
        scope: 'xphone:mcp',
      };
      await call(shortPairStub(env), 'oauth-register', clientData);
      return response(clientData, 201, origin || undefined, origin || '*');
    }
    const content = await servePublicContent(request, env);
    if (content) return content;
    if (url.pathname === '/pair/start' && request.method === 'POST') {
      if (!(await allowPairingCode(env, request))) return response({ error: 'rate limit exceeded' }, 429);
      const body = await request.json() as { publicKey: JsonWebKey; provider?: string; confirmReceipt?: boolean };
      if (body.publicKey?.kty !== 'EC' || body.publicKey.crv !== 'P-256') return response({ error: 'invalid public key' }, 400);
      const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
      const code = Array.from(crypto.getRandomValues(new Uint8Array(8)), (byte) => alphabet[byte % alphabet.length]).join('');
      const secret = randomId();
      const provider = body.provider === 'chatgpt' ? 'chatgpt' : 'muse';
      await call(shortPairStub(env), 'pair-start', { code, secret, publicKey: body.publicKey, provider,
        confirmReceipt: body.confirmReceipt === true });
      return response({ code, secret, expiresAt: Date.now() + 300_000 });
    }
    if (url.pathname === '/pair/redeem' && request.method === 'POST') {
      const body = await request.json() as { code: string; secret: string };
      try {
        const result = await call(shortPairStub(env), 'pair-redeem', body);
        return response(result, result.pending ? 202 : 200);
      } catch (error) {
        return response({ error: 'code unavailable' }, (error as { status?: number }).status === 404 ? 404 : 503);
      }
    }
    if (url.pathname === '/pair/complete' && request.method === 'POST') {
      const body = await request.json() as { code: string; secret: string };
      try {
        return response(await call(shortPairStub(env), 'pair-complete', body));
      } catch (error) {
        return response({ error: 'code unavailable' }, (error as { status?: number }).status === 404 ? 404 : 503);
      }
    }
    try {
    if (url.pathname === '/oauth/authorize' && request.method === 'GET') {
      const clientId = url.searchParams.get('client_id') || '';
      const redirectUri = url.searchParams.get('redirect_uri') || '';
      const responseType = url.searchParams.get('response_type');
      const resource = url.searchParams.get('resource') || oauthResource(request);
      const challenge = url.searchParams.get('code_challenge') || '';
      const challengeMethod = url.searchParams.get('code_challenge_method') || (challenge ? 'S256' : '');
      const state = url.searchParams.get('state') || '';
      const scope = url.searchParams.get('scope') || '';

      if (responseType !== 'code' || !clientId || !redirectUri) return response({ error: 'invalid_request' }, 400);
      try { new URL(redirectUri); } catch { return response({ error: 'invalid_request' }, 400); }
      if (challenge && !/^[A-Za-z0-9_-]{43,128}$/.test(challenge)) return response({ error: 'invalid_request' }, 400);
      if (challengeMethod && challengeMethod !== 'S256' && challengeMethod !== 'plain') return response({ error: 'invalid_request' }, 400);

      const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
      const displayCode = Array.from(crypto.getRandomValues(new Uint8Array(8)), (byte) => alphabet[byte % alphabet.length]).join('');
      const requestId = randomId();
      const authCode = randomId();
      const startResult = await call(shortPairStub(env), 'oauth-start', { clientId, redirectUri, resource, challenge,
        state, requestId, displayCode, authCode });
      const clientLabel = startResult.clientName || (clientId.includes('chatgpt') ? 'ChatGPT' : clientId.includes('muse') ? 'Muse' : '个人代理 (Personal Agent)');
      const gigiOrigin = (env.CLIENT_ORIGINS || '').split(',')[0].trim() || 'https://phone.xenoflex.xyz';
      const html = `<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>连接个人代理 - XPhone</title><style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;max-width:32rem;margin:8vh auto;padding:1.5rem;color:#171717;background:#FAF9F6;line-height:1.6}.card{background:#fff;border:1px solid #E5E5E0;border-radius:16px;padding:2rem;box-shadow:0 4px 16px rgba(0,0,0,0.04)}h1{font-size:1.6rem;font-weight:700;margin:0 0 .5rem;letter-spacing:-.5px}p{color:#5E5B57;font-size:.95rem;margin:.5rem 0 1.5rem}.code-box{background:#F4F4F0;border-radius:12px;padding:1.25rem;text-align:center;margin:1.5rem 0}.code-label{font-size:.75rem;font-weight:700;letter-spacing:1px;color:#77736E;text-transform:uppercase;margin-bottom:.5rem}code{display:block;font-size:2.2rem;font-weight:700;letter-spacing:.25rem;font-family:ui-monospace,monospace;color:#111110}.btn{display:block;width:100%;box-sizing:border-box;text-align:center;background:#111110;color:#FAF9F6;text-decoration:none;padding:.9rem 1.2rem;border-radius:10px;font-weight:600;font-size:.95rem;margin-top:1rem;transition:opacity .2s}.btn:hover{opacity:.85}.status-text{font-size:.85rem;color:#8C8984;text-align:center;margin-top:1.2rem}.hint{font-size:.85rem;color:#77736E;border-top:1px solid #ECEBE6;padding-top:1rem;margin-top:1.5rem}</style><div class="card"><div style="font-size:.75rem;font-weight:700;letter-spacing:2px;color:#77736E;margin-bottom:.5rem">XPHONE / OAUTH2</div><h1>连接 ${clientLabel}</h1><p>代理平台正在申请连接你的 XPhone 虚拟手机以提供 AI 交互服务。</p><div class="code-box"><div class="code-label">授权连接码 / APPROVAL CODE</div><code>${displayCode}</code></div><a href="${gigiOrigin}/?oauth_code=${encodeURIComponent(displayCode)}&oauth_request=${encodeURIComponent(requestId)}" class="btn" target="_blank">在 XPhone 中一键授权 ↗</a><p class="status-text" id="status">等待确认中，授权完成后此窗口将自动跳转…</p><div class="hint">若在其他设备，可在已登录 XPhone 的前端「个人代理」设置中输入上方 8 位代码。</div></div><script>const id=${JSON.stringify(requestId)};const base=${JSON.stringify(publicBases.get(request) || '')};async function check(){try{const r=await fetch(base+'/oauth/status?requestId='+encodeURIComponent(id));if(!r.ok){document.querySelector('#status').textContent='授权已过期，请关闭重试。';return}const v=await r.json();if(v.approved){document.querySelector('#status').textContent='授权成功，正在返回代理平台…';location.assign(base+'/oauth/continue?requestId='+encodeURIComponent(id));return}}catch(e){}setTimeout(check,1500)}check()</script></html>`;
      return new Response(html, { headers: { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store',
        'content-security-policy': "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'" } });
    }
    if (url.pathname === '/oauth/status' && request.method === 'GET') {
      const result = await call(shortPairStub(env), 'oauth-result', { requestId: url.searchParams.get('requestId') });
      return response({ approved: result.approved });
    }
    if (url.pathname === '/oauth/continue' && request.method === 'GET') {
      const result = await call(shortPairStub(env), 'oauth-result', { requestId: url.searchParams.get('requestId') });
      if (!result.approved) return response({ error: 'authorization pending' }, 409);
      const destination = new URL(result.redirectUri);
      destination.searchParams.set('code', result.authCode);
      if (result.state) destination.searchParams.set('state', result.state);
      destination.searchParams.set('iss', oauthIssuer(request));
      return Response.redirect(destination.toString(), 302);
    }
    if (url.pathname === '/oauth/token' && request.method === 'POST') {
      const form = await parseOAuthParams(request);
      const resource = form.get('resource') || oauthResource(request);
      const clientId = form.get('client_id') || '';
      const grantType = form.get('grant_type');
      if (grantType === 'authorization_code') {
        const verifier = form.get('code_verifier') || '';
        const challenge = verifier ? await sha256(verifier) : '';
        const authCode = form.get('code') || '';
        const redirectUri = form.get('redirect_uri') || '';
        if (!authCode) return response({ error: 'invalid_request' }, 400);
        const approved = await call(shortPairStub(env), 'oauth-exchange', { authCode,
          challenge, clientId, redirectUri, resource });
        return response(await oauthTokens(env, request, approved));
      }
      if (grantType === 'refresh_token') {
        const refresh = await verifyToken<OAuthClaims>(String(form.get('refresh_token') || ''), env.TOKEN_HMAC_SECRET, 'xphone-agent-refresh');
        if (refresh.resource !== resource || refresh.iss !== oauthIssuer(request) || refresh.scope !== 'xphone:mcp')
          return response({ error: 'invalid_grant' }, 400);
        if (!refresh.parentJti && refresh.pairingScope) {
          const status = await call(accountStub(env, refresh.sub), 'account-token-status', { scope: refresh.pairingScope });
          if (status.paired && status.provider === 'chatgpt')
            return response(await oauthTokens(env, request, { sub: refresh.sub, parentJti: status.tokenJti,
              accountExp: status.tokenExp }));
          return response(await oauthTokens(env, request, { sub: refresh.sub, parentJti: '',
            accountExp: refresh.exp * 1000, pairingScope: refresh.pairingScope }));
        }
        const current = await call(accountStub(env, refresh.sub), 'account-token-check', { jti: refresh.parentJti });
        if (!current.active) return response({ error: 'invalid_grant' }, 400);
        return response(await oauthTokens(env, request, { sub: refresh.sub, parentJti: refresh.parentJti, accountExp: current.pair.exp }));
      }
      return response({ error: 'unsupported_grant_type' }, 400);
    }
    if (url.pathname === '/account/oauth/approve' && request.method === 'POST') {
      const claims = await verifyToken<AccountGrantClaims>(bearer(request), env.GRANT_HMAC_SECRET, 'xphone-agent-account-grant');
      const account = accountStub(env, claims.sub);
      const body = await request.json() as { requestId?: string; code?: string; displayCode?: string; scope?: { packId: string; playthroughId: string };
        scopeGrant?: string };
      const status = await call(account, 'account-token-status', { scope: body.scope });
      let pairingScope: OAuthClaims['pairingScope'];
      if (!status.paired) {
        const scope = await verifyToken<GrantClaims>(body.scopeGrant || '', env.GRANT_HMAC_SECRET, 'xphone-agent-worker');
        if (scope.sub !== claims.sub || scope.packId !== body.scope?.packId
          || scope.playthroughId !== body.scope?.playthroughId || !['admin', 'lifetime', 'trial'].includes(scope.accessKind))
          return response({ error: 'story unavailable' }, 403, origin || undefined, env.CLIENT_ORIGINS);
        const story = await manifest(env, scope.packId);
        if (story.xphoneSha256 !== scope.xphoneSha256)
          return response({ error: 'story unavailable' }, 403, origin || undefined, env.CLIENT_ORIGINS);
        pairingScope = { packId: scope.packId, playthroughId: scope.playthroughId, xphoneSha256: scope.xphoneSha256 };
      }
      await call(shortPairStub(env), 'oauth-approve', { requestId: body.requestId, code: body.code, displayCode: body.displayCode, sub: claims.sub,
        parentJti: status.tokenJti || '', accountExp: status.paired ? status.tokenExp
          : Math.min(Date.now() + 365 * 86400_000, claims.accessExpiresAt || Infinity), pairingScope });
      return response({ ok: true }, 200, origin || undefined, env.CLIENT_ORIGINS);
    }
    if (url.pathname === '/account/inbox/discard' && request.method === 'POST') {
      const claims = await verifyToken<AccountGrantClaims>(bearer(request), env.GRANT_HMAC_SECRET, 'xphone-agent-account-grant');
      const body = await request.json() as { scopeGrant?: string; eventIds?: string[] };
      const scope = await verifyToken<GrantClaims>(body.scopeGrant || '', env.GRANT_HMAC_SECRET, 'xphone-agent-worker');
      if (scope.sub !== claims.sub || !['admin', 'lifetime', 'trial'].includes(scope.accessKind)
        || !Array.isArray(body.eventIds) || body.eventIds.length === 0
        || body.eventIds.some((id) => !validId(id, 120)))
        return response({ error: 'invalid discard request' }, 400, origin || undefined, env.CLIENT_ORIGINS);
      const result = await call(stub(env, scope), 'discard-agent-events', { eventIds: body.eventIds });
      return response(result, 200, origin || undefined, env.CLIENT_ORIGINS);
    }
    if (url.pathname.startsWith('/account/pair/') && request.method === 'POST') {
      const claims = await verifyToken<AccountGrantClaims>(bearer(request), env.GRANT_HMAC_SECRET, 'xphone-agent-account-grant');
      const account = accountStub(env, claims.sub);
      const body = await request.json() as { code?: string; encryptedKey?: unknown; scope?: { packId: string; playthroughId: string };
        sourceScope?: { packId: string; playthroughId: string }; scopeGrant?: string; provider?: 'muse' | 'chatgpt' };
      if (url.pathname === '/account/pair/status') {
        return response(await call(account, 'account-token-status', { scope: body.scope }), 200, origin || undefined, env.CLIENT_ORIGINS);
      }
      if (url.pathname === '/account/pair/api-key') {
        const status = await call(account, 'account-token-status', { scope: body.scope });
        if (!status.paired || status.provider !== 'muse')
          return response({ error: 'Muse save pairing required' }, 409, origin || undefined, env.CLIENT_ORIGINS);
        const apiKey = await signToken<AccountClaims>({ aud: 'xphone-agent-account', sub: claims.sub,
          exp: Math.floor(status.tokenExp / 1000), jti: status.tokenJti, provider: 'muse' }, env.TOKEN_HMAC_SECRET);
        return response({ apiKey }, 200, origin || undefined, env.CLIENT_ORIGINS);
      }
      if (url.pathname === '/account/pair/reuse') {
        const scope = await verifyToken<GrantClaims>(body.scopeGrant || '', env.GRANT_HMAC_SECRET, 'xphone-agent-worker');
        if (scope.sub !== claims.sub || !['admin', 'lifetime', 'trial'].includes(scope.accessKind))
          return response({ error: 'scope unavailable' }, 403, origin || undefined, env.CLIENT_ORIGINS);
        const story = await manifest(env, scope.packId);
        if (story.xphoneSha256 !== scope.xphoneSha256) return response({ error: 'story unavailable' }, 403, origin || undefined, env.CLIENT_ORIGINS);
        const status = await call(account, 'account-token-status', {}) as {
          paired: boolean; pairings: Array<{ provider: 'muse' | 'chatgpt'; scope: any }>;
        };
        const requestedProvider = body.provider;
        const matchingPair = status.pairings.find((p) => p.scope.packId === body.sourceScope?.packId
          && p.scope.playthroughId === body.sourceScope?.playthroughId
          && (!requestedProvider || p.provider === requestedProvider));
        if (!matchingPair) return response({ error: 'no matching agent to reuse' }, 400, origin || undefined, env.CLIENT_ORIGINS);
        const tokenJti = randomId();
        const tokenExp = Math.min(Math.floor(Date.now() / 1000) + 365 * 86400, claims.accessExpiresAt || Infinity);
        const pairedScope = { packId: scope.packId, playthroughId: scope.playthroughId, xphoneSha256: scope.xphoneSha256 };
        const previous = await call(account, 'account-token-status', { scope: pairedScope });
        await call(account, 'account-token-set', { scope: pairedScope, jti: tokenJti, provider: matchingPair.provider,
          sourceScope: matchingPair.scope, exp: tokenExp * 1000 });
        if (previous.paired) await call(stub(env, { sub: claims.sub, ...pairedScope }),
          'deactivate-agent', { clearSubscriptions: true });
        if (matchingPair.provider === 'chatgpt') await call(stub(env, { sub: claims.sub, ...pairedScope }),
          'pair', { grantJti: randomId(), agentJti: tokenJti, agentExp: tokenExp * 1000 });
        return response({ ok: true, provider: matchingPair.provider, jti: tokenJti }, 200, origin || undefined, env.CLIENT_ORIGINS);
      }
      if (!/^[A-HJ-NP-Z2-9]{8}$/.test(body.code || '')) return response({ error: 'invalid code' }, 400, origin || undefined, env.CLIENT_ORIGINS);
      if (url.pathname === '/account/pair/confirmation') {
        return response(await call(shortPairStub(env), 'pair-confirmation', { code: body.code, sub: claims.sub }),
          200, origin || undefined, env.CLIENT_ORIGINS);
      }
      if (url.pathname === '/account/pair/lookup') {
        return response(await call(shortPairStub(env), 'pair-lookup', { code: body.code }), 200, origin || undefined, env.CLIENT_ORIGINS);
      }
      if (url.pathname === '/account/pair/approve') {
        const scope = await verifyToken<GrantClaims>(body.scopeGrant || '', env.GRANT_HMAC_SECRET, 'xphone-agent-worker');
        if (scope.sub !== claims.sub || !['admin', 'lifetime', 'trial'].includes(scope.accessKind))
          return response({ error: 'scope unavailable' }, 403, origin || undefined, env.CLIENT_ORIGINS);
        const story = await manifest(env, scope.packId);
        if (story.xphoneSha256 !== scope.xphoneSha256) return response({ error: 'story unavailable' }, 403, origin || undefined, env.CLIENT_ORIGINS);
        const pending = await call(shortPairStub(env), 'pair-lookup', { code: body.code });
        if (pending.confirmReceipt && scope.accessKind !== 'lifetime' && scope.accessKind !== 'admin')
          return response({ error: 'purchase required' }, 403, origin || undefined, env.CLIENT_ORIGINS);
        const tokenJti = randomId();
        const tokenExp = Math.min(Math.floor(Date.now() / 1000) + 365 * 86400, claims.accessExpiresAt || Infinity);
        const accountToken = await signToken<AccountClaims>({ aud: 'xphone-agent-account', sub: claims.sub,
          exp: tokenExp,
          jti: tokenJti, provider: pending.provider }, env.TOKEN_HMAC_SECRET);
        const bundle = { key: body.encryptedKey, token: await wrapForPublicKey(pending.publicKey, accountToken),
          scope: { packId: scope.packId, playthroughId: scope.playthroughId },
          workerUrl: new URL('/', request.url).toString() };
        await call(shortPairStub(env), 'pair-approve', { code: body.code, bundle, sub: claims.sub });
        const pairedScope = { packId: scope.packId, playthroughId: scope.playthroughId, xphoneSha256: scope.xphoneSha256 };
        const previous = await call(account, 'account-token-status', { scope: pairedScope });
        await call(account, 'account-token-set', { scope: pairedScope, jti: tokenJti, provider: pending.provider,
          exp: tokenExp * 1000 });
        if (previous.paired) await call(stub(env, { sub: claims.sub, ...pairedScope }),
          'deactivate-agent', { clearSubscriptions: true });
        if (pending.provider === 'chatgpt') await call(stub(env, { sub: claims.sub, ...pairedScope }),
          'pair', { grantJti: randomId(), agentJti: tokenJti, agentExp: tokenExp * 1000 });
        return response({ ok: true }, 200, origin || undefined, env.CLIENT_ORIGINS);
      }
      return response({ error: 'not found' }, 404, origin || undefined, env.CLIENT_ORIGINS);
    }
    if (url.pathname.startsWith('/account/key-transfer/') && request.method === 'POST') {
      const path = url.pathname;
      const body = await request.json() as any;
      if (path === '/account/key-transfer/request' || path === '/account/key-transfer/result') {
        const claims = await verifyToken<AccountGrantClaims>(bearer(request), env.GRANT_HMAC_SECRET, 'xphone-agent-account-grant');
        const account = accountStub(env, claims.sub);
        if (path.endsWith('/result')) {
          return response(await call(account, 'transfer-result', { requestId: body.requestId }), 200, origin || undefined, env.CLIENT_ORIGINS);
        }
        const [encoded] = String(body.ticket || '').split('.');
        const ticket = JSON.parse(atob(encoded.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - encoded.length % 4) % 4)));
        if (ticket.sub !== claims.sub || !/^[0-9a-f-]{36}$/.test(ticket.requestId)
          || ticket.expiresAt <= Date.now() || ticket.expiresAt > Date.now() + 120_000) {
          return response({ error: 'invalid transfer ticket' }, 400, origin || undefined, env.CLIENT_ORIGINS);
        }
        return response(await call(account, 'transfer-start', { requestId: ticket.requestId,
          ticket: body.ticket, expiresAt: ticket.expiresAt }), 200, origin || undefined, env.CLIENT_ORIGINS);
      }
      const claims = await verifyToken<AccountClaims>(bearer(request), env.TOKEN_HMAC_SECRET, 'xphone-agent-account');
      const account = accountStub(env, claims.sub);
      if (!(await call(account, 'account-token-check', { jti: claims.jti })).active) return response({ error: 'token revoked' }, 403);
      if (path === '/account/key-transfer/pending') return response(await call(account, 'transfer-pending'));
      if (path === '/account/key-transfer/respond') return response(await call(account, 'transfer-respond', body));
      return response({ error: 'not found' }, 404);
    }
    if (url.pathname.startsWith('/account/')) {
      const claims = await verifyToken<AccountClaims>(bearer(request), env.TOKEN_HMAC_SECRET, 'xphone-agent-account');
      const checked = await call(accountStub(env, claims.sub), 'account-token-check', { jti: claims.jti });
      if (!checked.active) return response({ error: 'token revoked' }, 403);
      const active = checked.pair.scope;
      if (url.pathname === '/account/active' && request.method === 'GET') return response({ scope: active });
      if ((url.pathname === '/account/session' || url.pathname === '/account/gateway-session') && request.method === 'POST') {
        if (url.pathname === '/account/gateway-session' && claims.provider === 'chatgpt')
          return response({ error: 'gateway session unavailable' }, 403);
        if (!active) return response({ error: 'normal mode active' }, 409);
        const story = await manifest(env, active.packId);
        if (story.xphoneSha256 !== active.xphoneSha256) return response({ error: 'pack unavailable' }, 403);
        const exp = Math.min(Math.floor(Date.now() / 1000) + 3600, claims.exp);
        const jti = randomId();
        const session = { aud: 'xphone-agent-mcp' as const, sub: claims.sub, ...active, exp, jti,
          parentJti: claims.jti, provider: claims.provider, gateway: url.pathname === '/account/gateway-session' };
        await call(stub(env, session), 'pair', { grantJti: randomId(), agentJti: jti, agentExp: exp * 1000 });
        const initialConsumers = new Set(story.documents.filter((doc) => doc.initial).flatMap((doc) => doc.consumer || []));
        for (const consumer of initialConsumers) {
          await call(stub(env, session), 'references', { consumer,
            refs: story.documents.filter((doc) => doc.initial && doc.consumer?.includes(consumer)).map((doc) => doc.id),
            expiresAt: exp * 1000 });
        }
        return response({ token: await signToken(session, env.TOKEN_HMAC_SECRET), mcpUrl: publicUrl(request, '/mcp').toString(),
          scope: active, sourceScope: checked.pair.sourceScope, expiresAt: exp * 1000 });
      }
    }
    } catch (error) {
      const message = error instanceof Error ? error.message : 'request rejected';
      return response({ error: message }, message === 'authentication required' || message === 'invalid token' ? 401 : 400,
        origin || undefined, env.CLIENT_ORIGINS);
    }
    const contentLength = Number(request.headers.get('content-length') || 0);
    const maxBodySize = (isAgentPath || url.pathname.startsWith('/client/blob')) ? 25_000_000 : 150_000;
    if (contentLength > maxBodySize) return response({ error: 'request too large' }, 413, origin || undefined, env.CLIENT_ORIGINS);
    try {
      if (url.pathname === '/agent/blob' && request.method === 'POST') {
        const claims = await verifyToken<AgentClaims>(bearer(request), env.TOKEN_HMAC_SECRET, 'xphone-agent-mcp');
        const story = await manifest(env, claims.packId);
        if (story.xphoneSha256 !== claims.xphoneSha256 || !(await call(accountStub(env, claims.sub), 'account-session-check', { parentJti: claims.parentJti })).active
          || !(await call(stub(env, claims), 'agent-active', { jti: claims.jti })).active) {
          return response({ error: 'access denied' }, 403, origin || undefined, allowedOrigin);
        }
        const body = (await request.json().catch(() => null)) as any;
        const ciphertext = typeof body?.data === 'string' ? body.data : typeof body?.ciphertext === 'string' ? body.ciphertext : '';
        if (!ciphertext) return response({ error: 'data required' }, 400, origin || undefined, allowedOrigin);
        const blobId = crypto.randomUUID();
        const broker = stub(env, claims);
        await call(broker, 'blob-put', {
          blobId,
          ciphertext,
          nonce: typeof body?.nonce === 'string' ? body.nonce : '',
          mimeType: typeof body?.mimeType === 'string' ? body.mimeType : 'image/webp',
          width: typeof body?.width === 'number' ? body.width : undefined,
          height: typeof body?.height === 'number' ? body.height : undefined,
        });
        return response({ blobId }, 200, origin || undefined, allowedOrigin);
      }
      if (url.pathname === '/client/ws' || url.pathname === '/agent/ws') {
        if (request.headers.get('upgrade')?.toLowerCase() !== 'websocket') return response({ error: 'websocket required' }, 426);
        const encoded = url.searchParams.get('connection') || '';
        if (!/^[A-Za-z0-9_-]{1,2000}$/.test(encoded)) return response({ error: 'invalid connection' }, 400);
        const name = new TextDecoder().decode(Uint8Array.from(atob(encoded.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - encoded.length % 4) % 4)), (char) => char.charCodeAt(0)));
        const parsed = JSON.parse(name) as unknown;
        if (!Array.isArray(parsed) || parsed.length !== 3 || parsed.some((part) => !validId(part))) return response({ error: 'invalid connection' }, 400);
        const headers = new Headers(request.headers);
        headers.set('x-operation', url.pathname === '/agent/ws' ? 'agent-socket' : 'socket');
        return env.CONNECTIONS.get(env.CONNECTIONS.idFromName(name)).fetch(new Request('https://internal/', { method: 'GET', headers }));
      }
      if (url.pathname.startsWith('/client/') || url.pathname === '/agent/encounter/generate') return await client(request, env, url.pathname, origin);
      if (url.pathname === '/mcp') {
        if (!request.headers.get('authorization') && !request.headers.get('x-api-key') && !request.headers.get('api-key'))
          return await mcp(request, env, null, origin);
        const tokenStr = bearer(request);
        let claims: AgentClaims;
        try {
          claims = await verifyToken<AgentClaims>(tokenStr, env.TOKEN_HMAC_SECRET, 'xphone-agent-mcp');
          const story = await manifest(env, claims.packId);
          if (story.xphoneSha256 !== claims.xphoneSha256 || !(await call(accountStub(env, claims.sub), 'account-session-check', { parentJti: claims.parentJti })).active
            || !(await call(stub(env, claims), 'agent-active', { jti: claims.jti })).active) return response({ error: 'access denied' }, 403, origin || undefined, allowedOrigin);
        } catch (sessionErr) {
          let oauthClaims: OAuthClaims | null = null;
          try { oauthClaims = await verifyToken<OAuthClaims>(tokenStr, env.TOKEN_HMAC_SECRET, 'xphone-agent-oauth'); } catch { /* Account token path. */ }
          if (oauthClaims && (oauthClaims.iss !== oauthIssuer(request) || oauthClaims.resource !== oauthResource(request)
            || oauthClaims.scope !== 'xphone:mcp')) return oauthChallenge(request);
          const accountClaims = oauthClaims ? null
            : await verifyToken<AccountClaims>(tokenStr, env.TOKEN_HMAC_SECRET, 'xphone-agent-account');
          const sub = oauthClaims?.sub || accountClaims!.sub;
          let parentJti = oauthClaims ? oauthClaims.parentJti : accountClaims!.jti;
          if (oauthClaims && !parentJti && oauthClaims.pairingScope) {
            const status = await call(accountStub(env, sub), 'account-token-status', { scope: oauthClaims.pairingScope });
            if (!status.paired) return await mcp(request, env, null, origin, true);
            if (status.provider !== 'chatgpt') return response({ error: 'ChatGPT pairing required' }, 403, origin || undefined, allowedOrigin);
            parentJti = status.tokenJti;
          }
          const checked = await call(accountStub(env, sub), 'account-token-check', { jti: parentJti });
          if (!checked.active) {
            return response({ error: 'token revoked' }, 403, origin || undefined, allowedOrigin);
          }
          const active = checked.pair.scope;
          const story = await manifest(env, active.packId);
          if (story.xphoneSha256 !== active.xphoneSha256) return response({ error: 'pack unavailable' }, 403, origin || undefined, allowedOrigin);
          claims = {
            aud: 'xphone-agent-mcp',
            sub,
            ...active,
            exp: Math.min(oauthClaims?.exp || accountClaims!.exp, Math.floor(checked.pair.exp / 1000)),
            jti: parentJti,
            parentJti,
            provider: checked.pair.provider,
          };
        }
        return await mcp(request, env, claims, origin);
      }
      return response({ error: 'not found' }, 404);
    } catch (error) {
      const msg = error instanceof Error ? error.message : 'request rejected';
      if (url.pathname === '/mcp' && (msg === 'authentication required' || msg === 'invalid token')) return oauthChallenge(request);
      const status = (error as { status?: number })?.status;
      return response({ error: msg }, status && status >= 400 && status <= 599 ? status
        : msg === 'authentication required' || msg === 'invalid token' ? 401 : 400, origin || undefined, allowedOrigin);
    }
  },
};
