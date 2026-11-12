import { sha256 } from './security';
import { sendWebhook, type EventSubscription } from './mcpEvents';

export type Direction = 'client' | 'agent';

export interface SealedEnvelope {
  id: string;
  kind: 'event' | 'action_batch' | 'journal_frame' | 'observation_request' | 'observation_result' | 'receipt';
  version: 1 | 2;
  nonce: string;
  ciphertext: string;
  aad: string;
  tickId?: string;
  streamId?: string;
  seq?: number;
  phase?: 'start' | 'update' | 'abort';
  route?: AgentEventRoute;
  header?: { batchId: string; parentEventId: string; step: number; targetApp: string;
    surfaceId: string; origin: 'event' | 'delegation' };
}

export interface AgentEventRoute {
  roleId: string;
  role: 'os-agent' | 'subagent';
  windowId: string;
  targetApp?: 'messages' | 'socialmedia' | 'maps';
  surfaceId?: string;
  consumer: string;
}

export interface QueuedEnvelope {
  cursor: number;
  direction: Direction;
  envelope: SealedEnvelope;
  queuedAt: number;
}

type Schedule = { id: string; deliverAt: number; expiresAt: number; envelope: SealedEnvelope };
type EventExecution = { id: string; status: 'accepted' | 'running' | 'completed' | 'failed' | 'discarded';
  attempts: number; route: AgentEventRoute; createdAt: number; updatedAt: number; leaseId?: string;
  delegationIds?: string[] };
type DelegationJob = {
  jobId: string; eventId: string; taskPath: string; createdAt: number;
  status: 'accepted' | 'ready' | 'failed';
  batchId?: string; step?: number; targetApp?: string; surfaceId?: string; error?: string;
};
type SubmissionReply = { ok: boolean; error?: string; envelope?: SealedEnvelope; frame?: SealedEnvelope };
type PendingWebhook = { subscriptionId: string; eventId: string; cursor: number;
  createdAt: number; attempts: number; nextAt: number };
type AccountPair = { scope: { packId: string; playthroughId: string; xphoneSha256: string };
  jti: string; provider: 'muse' | 'chatgpt'; exp: number;
  sourceScope?: { packId: string; playthroughId: string } };
const MAX_QUEUE = 512;
const MAX_SCHEDULE = 32;
const RETENTION_MS = 7 * 86400_000;
const EVENT_LEASE_MS = 90_000;
const DELEGATION_START_MS = 10 * 60_000;

function json(value: unknown, status = 200): Response {
  return Response.json(value, { status, headers: { 'cache-control': 'no-store' } });
}

export class AgentConnection implements DurableObject {
  private submissionWaiters = new Map<string, (reply: SubmissionReply) => void>();
  constructor(private state: DurableObjectState) {
    this.setupAutoResponse();
  }

  private async delegationStatus(storage: Pick<DurableObjectStorage, 'get'>, job: DelegationJob) {
    if (job.batchId) {
      const delivery = await storage.get<{ status: string }>(`delivery:${job.batchId}`);
      if (delivery) return delivery.status;
    }
    if (job.status === 'failed') return 'failed';
    if (job.createdAt + DELEGATION_START_MS <= Date.now()) return 'failed';
    return job.status;
  }

  private async eventDelegations(storage: Pick<DurableObjectStorage, 'get'>, event: EventExecution) {
    const jobs = await Promise.all((event.delegationIds || []).map(async (id) => {
      const job = await storage.get<DelegationJob>(`task:${id}`);
      if (!job) throw new Error('missing delegation job');
      return { job, status: await this.delegationStatus(storage, job) };
    }));
    if (!jobs.length) return 'none';
    const active = jobs.filter(({ job, status }) => !['rejected', 'failed'].includes(status) || !jobs.some((other) =>
      job.surfaceId && other.job.surfaceId === job.surfaceId && (other.job.step || 0) > (job.step || 0)));
    if (active.some(({ status }) => !['applied', 'rejected', 'failed'].includes(status))) return 'pending';
    if (active.some(({ status }) => status === 'failed')) return 'failed';
    return active.some(({ status }) => status === 'rejected') ? 'rejected' : 'applied';
  }

  private notifyDelegation(socket: WebSocket | undefined, job: DelegationJob): void {
    if (!socket) return;
    try {
      socket.send(JSON.stringify({ type: job.status === 'ready' ? 'delegate-ready' : 'delegate-task',
        jobId: job.jobId, taskPath: job.taskPath, deadlineAt: job.createdAt + DELEGATION_START_MS }));
    } catch { /* The next agent connection replays durable jobs. */ }
  }

  private pairKey(scope: AccountPair['scope']): string {
    return `account-pair:${JSON.stringify([scope.packId, scope.playthroughId])}`;
  }

  private async migrateAccountPair(): Promise<void> {
    const jti = await this.state.storage.get<string>('accountToken');
    if (!jti) return;
    const scope = await this.state.storage.get<AccountPair['scope']>('accountLastScope')
      || await this.state.storage.get<AccountPair['scope']>('accountActive');
    if (scope) {
      const record: AccountPair = { scope, jti,
        provider: await this.state.storage.get<AccountPair['provider']>('accountProvider') || 'muse',
        exp: await this.state.storage.get<number>('accountTokenExp') || 0 };
      await this.state.storage.put(this.pairKey(scope), record);
      await this.state.storage.put(`account-pair-token:${jti}`, this.pairKey(scope));
    }
    await this.state.storage.delete('accountToken');
    await this.state.storage.delete('accountProvider');
    await this.state.storage.delete('accountTokenExp');
  }

  private async accountPairByToken(jti: string): Promise<AccountPair | null> {
    await this.migrateAccountPair();
    const key = await this.state.storage.get<string>(`account-pair-token:${jti}`);
    const pair = key ? await this.state.storage.get<AccountPair>(key) : null;
    if (!pair || pair.jti !== jti || pair.exp <= Date.now()) return null;
    const activeScope = await this.state.storage.get<AccountPair['scope']>('accountActive');
    if (activeScope) {
      const activePair = await this.state.storage.get<AccountPair>(this.pairKey(activeScope));
      if (activePair && activePair.provider === pair.provider && activePair.exp > Date.now()) {
        return activePair;
      }
    }
    return pair;
  }

  private setupAutoResponse(): void {
    if (typeof WebSocketRequestResponsePair !== 'undefined' && typeof this.state.setWebSocketAutoResponse === 'function') {
      this.state.setWebSocketAutoResponse(new WebSocketRequestResponsePair('ping', 'pong'));
    }
  }

  private async active(jti: string): Promise<boolean> {
    if (!jti) return false;
    const revokedAll = await this.state.storage.get<boolean>('revokedAll');
    if (revokedAll) return false;
    const revoked = await this.state.storage.get<boolean>(`revoked:${jti}`);
    if (revoked) return false;
    const record = await this.state.storage.get<{ jti: string; exp: number }>('activeAgent');
    if (record && record.jti === jti) return record.exp > Date.now();
    const tokenRecord = await this.state.storage.get<{ jti: string; exp: number }>(`agentToken:${jti}`);
    if (tokenRecord) return tokenRecord.exp > Date.now();
    return true;
  }

  private async enqueue(direction: Direction, envelope: SealedEnvelope, taskPath?: string): Promise<{ status: 'accepted' | 'duplicate'; cursor: number }> {
    const taskKey = taskPath ? `task:${await sha256(taskPath)}` : undefined;
    const result = await this.state.storage.transaction(async (tx) => {
      if (taskKey) {
        const header = envelope.header!;
        const job = await tx.get<DelegationJob>(taskKey);
        const task = job || await tx.get<{ batchId: string; targetApp?: string }>(`task:${taskPath}`);
        if (!task || task.batchId !== envelope.id || task.targetApp !== header.targetApp
          || (job && (job.eventId !== header.parentEventId || job.surfaceId !== header.surfaceId
            || job.step !== header.step || job.status !== 'ready'
            || await this.delegationStatus(tx, job) === 'failed')))
          throw new Error('task association mismatch');
        const execution = await tx.get<EventExecution>(`event:${header.parentEventId}`);
        if (execution?.status !== 'running') throw new Error('event is not running');
      }
      const duplicate = await tx.get<number>(`dedupe:${direction}:${envelope.id}`);
      if (duplicate) return { status: 'duplicate' as const, cursor: duplicate };
      const last = await tx.get<number>(`sequence:${direction}`) || 0;
      const ack = await tx.get<number>(`ack:${direction}`) || 0;
      if (last - ack >= MAX_QUEUE) throw new Error('queue full');
      const cursor = last + 1;
      await tx.put(`sequence:${direction}`, cursor);
      await tx.put(`queue:${direction}:${String(cursor).padStart(12, '0')}`, {
        cursor, direction, envelope, queuedAt: Date.now(),
      } satisfies QueuedEnvelope);
      await tx.put(`dedupe:${direction}:${envelope.id}`, cursor);
      if (direction === 'agent' && envelope.kind === 'event') {
        await tx.put(`event:${envelope.id}`, { id: envelope.id, status: 'accepted', attempts: 0,
          route: envelope.route!, createdAt: Date.now(), updatedAt: Date.now() } satisfies EventExecution);
        const subscriptions = await tx.list<EventSubscription>({ prefix: 'event-subscription:' });
        for (const subscription of subscriptions.values()) {
          if (subscription.expiresAt <= Date.now()) continue;
          await tx.put(`webhook:${subscription.id}:${envelope.id}`, { subscriptionId: subscription.id, eventId: envelope.id,
            cursor, createdAt: Date.now(), attempts: 0, nextAt: Date.now() } satisfies PendingWebhook);
        }
      }
      if (direction === 'client' && envelope.kind === 'action_batch') {
        await tx.put(`delivery:${envelope.id}`, { status: 'queued', cursor });
      }
      return { status: 'accepted' as const, cursor };
    });
    if (result.status === 'accepted') {
      if (direction === 'agent' && envelope.kind === 'event') {
        await this.resetAlarm();
        this.state.waitUntil(this.deliverWebhooks());
      }
      for (const socket of this.state.getWebSockets()) {
        if (socket.deserializeAttachment()?.kind !== direction) continue;
        try { socket.send(JSON.stringify({ type: 'envelope', cursor: result.cursor, envelope })); }
        catch { /* A reconnect will pull the retained envelope. */ }
      }
    }
    return result;
  }

  private async pull(direction: Direction, cursor: number, limit: number): Promise<QueuedEnvelope[]> {
    const all = await this.state.storage.list<QueuedEnvelope>({ prefix: `queue:${direction}:` });
    return [...all.values()].filter((entry) => entry.cursor > cursor).slice(0, limit);
  }

  private async ack(direction: Direction, cursor: number): Promise<void> {
    await this.state.storage.transaction(async (tx) => {
      const last = await tx.get<number>(`sequence:${direction}`) || 0;
      if (cursor < 0 || cursor > last) throw new Error('invalid cursor');
      const previous = await tx.get<number>(`ack:${direction}`) || 0;
      if (cursor <= previous) return;
      await tx.put(`ack:${direction}`, cursor);
      const queued = await tx.list<QueuedEnvelope>({ prefix: `queue:${direction}:` });
      for (const [key, entry] of queued) {
        if (entry.cursor > cursor) continue;
        if (direction === 'agent' && entry.envelope.kind === 'event') {
          const execution = await tx.get<EventExecution>(`event:${entry.envelope.id}`);
          if (execution?.status !== 'completed') throw new Error('event execution incomplete');
        }
        await tx.delete(key);
      }
    });
  }

  private async resetAlarm(): Promise<void> {
    const schedules = await this.state.storage.list<Schedule>({ prefix: 'schedule:' });
    const events = await this.state.storage.list<EventExecution>({ prefix: 'event:' });
    const pairs = await this.state.storage.list<{ expiresAt: number }>({ prefix: 'shortpair:' });
    const oauth = await this.state.storage.list<{ expiresAt: number }>({ prefix: 'oauth:' });
    const transfers = await this.state.storage.list<{ expiresAt: number }>({ prefix: 'keytransfer:' });
    const webhooks = await this.state.storage.list<PendingWebhook>({ prefix: 'webhook:' });
    const subscriptions = await this.state.storage.list<EventSubscription>({ prefix: 'event-subscription:' });
    const nextSchedule = [...schedules.values()].reduce((min, item) => Math.min(min, item.deliverAt), Infinity);
    const nextEvent = [...events.values()].reduce((min, item) => Math.min(min, item.createdAt + RETENTION_MS), Infinity);
    const nextPair = [...pairs.values()].reduce((min, item) => Math.min(min, item.expiresAt), Infinity);
    const nextOauth = [...oauth.values()].reduce((min, item) => Math.min(min, item.expiresAt), Infinity);
    const nextTransfer = [...transfers.values()].reduce((min, item) => Math.min(min, item.expiresAt), Infinity);
    const nextWebhook = [...webhooks.values()].reduce((min, item) => Math.min(min, item.nextAt), Infinity);
    const nextSubscription = [...subscriptions.values()].reduce((min, item) => Math.min(min, item.expiresAt), Infinity);
    const next = Math.min(nextSchedule, nextEvent, nextPair, nextOauth, nextTransfer, nextWebhook, nextSubscription);
    if (Number.isFinite(next)) await this.state.storage.setAlarm(Math.max(Date.now() + 1000, next));
    else await this.state.storage.deleteAlarm();
  }

  private async deliverWebhooks(): Promise<void> {
    const pending = await this.state.storage.list<PendingWebhook>({ prefix: 'webhook:' });
    for (const [key, item] of pending) {
      if (item.nextAt > Date.now()) continue;
      const active = await this.state.storage.get<EventSubscription>(`event-subscription:${item.subscriptionId}`);
      if (!active || active.expiresAt <= Date.now()) { await this.state.storage.delete(key); continue; }
      const payload = { eventId: item.eventId, name: 'xphone.event.created',
        timestamp: new Date(item.createdAt).toISOString(), data: { eventId: item.eventId, cursor: item.cursor }, cursor: null };
      try {
        const response = await sendWebhook(active, item.eventId, payload);
        if (response.ok || response.status === 410 || response.status === 413
          || response.status < 500 && response.status !== 429) {
          await this.state.storage.delete(key);
          continue;
        }
      } catch { /* Retry by alarm. */ }
      if (item.attempts >= 4) await this.state.storage.delete(key);
      else await this.state.storage.put(key, { ...item, attempts: item.attempts + 1,
        nextAt: Date.now() + Math.min(60_000, 1000 * 2 ** item.attempts) });
    }
    await this.resetAlarm();
  }

  private async clearAgentDeliveries(): Promise<void> {
    await this.state.storage.transaction(async (tx) => {
      const last = await tx.get<number>('sequence:client') || 0;
      await tx.put('ack:client', last);
      for (const prefix of ['queue:client:', 'dedupe:client:', 'delivery:', 'schedule:', 'task:']) {
        const entries = await tx.list({ prefix });
        for (const key of entries.keys()) await tx.delete(key);
      }
    });
    await this.resetAlarm();
  }

  async alarm(): Promise<void> {
    const pairs = await this.state.storage.list<{ expiresAt: number }>({ prefix: 'shortpair:' });
    for (const [key, item] of pairs) if (item.expiresAt <= Date.now()) await this.state.storage.delete(key);
    const oauth = await this.state.storage.list<{ expiresAt: number }>({ prefix: 'oauth:' });
    for (const [key, item] of oauth) if (item.expiresAt <= Date.now()) await this.state.storage.delete(key);
    const transfers = await this.state.storage.list<{ expiresAt: number }>({ prefix: 'keytransfer:' });
    for (const [key, item] of transfers) if (item.expiresAt <= Date.now()) await this.state.storage.delete(key);
    const subscriptions = await this.state.storage.list<EventSubscription>({ prefix: 'event-subscription:' });
    for (const [key, item] of subscriptions) if (item.expiresAt <= Date.now()) await this.state.storage.delete(key);
    await this.deliverWebhooks();
    const schedules = await this.state.storage.list<Schedule>({ prefix: 'schedule:' });
    for (const [key, item] of schedules) {
      if (item.deliverAt > Date.now()) continue;
      if (item.expiresAt > Date.now()) await this.enqueue('client', item.envelope);
      await this.state.storage.delete(key);
    }
    const blobs = await this.state.storage.list<any>({ prefix: 'imgblob:' });
    for (const [key, item] of blobs) if ((item.expiresAt || 0) < Date.now()) await this.state.storage.delete(key);
    const cutoff = Date.now() - RETENTION_MS;
    const queues = await this.state.storage.list<QueuedEnvelope>({ prefix: 'queue:' });
    for (const [key, item] of queues) if (item.queuedAt < cutoff) await this.state.storage.delete(key);
    const events = await this.state.storage.list<EventExecution>({ prefix: 'event:' });
    for (const [key, item] of events) if (item.createdAt < cutoff) await this.state.storage.delete(key);
    const tasks = await this.state.storage.list<{ createdAt: number }>({ prefix: 'task:' });
    for (const [key, item] of tasks) if (item.createdAt < cutoff) await this.state.storage.delete(key);
    await this.resetAlarm();
  }

  async fetch(request: Request): Promise<Response> {
    const operation = request.headers.get('x-operation') || '';
    const body = operation === 'socket' ? {} : await request.json().catch(() => ({})) as Record<string, any>;
    try {
      switch (operation) {
        case 'oauth-register': {
          const clientId = body.clientId || body.client_id;
          await this.state.storage.put(`oauth:client:${clientId}`, { ...body, clientId, createdAt: Date.now() });
          return json({ ok: true });
        }
        case 'oauth-start': {
          const clientId = body.clientId || body.client_id;
          const client = await this.state.storage.get<any>(`oauth:client:${clientId}`);
          const clientName = client?.client_name || client?.clientName;
          const entry = { ...body, clientName, expiresAt: Date.now() + 300_000 };
          await this.state.storage.put(`oauth:request:${body.requestId}`, entry);
          if (body.displayCode) await this.state.storage.put(`oauth:display:${body.displayCode}`, { requestId: body.requestId, expiresAt: entry.expiresAt });
          await this.state.storage.put(`oauth:code:${body.authCode}`, { requestId: body.requestId, expiresAt: entry.expiresAt });
          await this.resetAlarm();
          return json({ ok: true, clientName });
        }
        case 'oauth-approve': {
          let requestId = body.requestId;
          if (!requestId && body.code) {
            const display = await this.state.storage.get<any>(`oauth:display:${body.code}`);
            requestId = display?.requestId;
          }
          if (!requestId && body.displayCode) {
            const display = await this.state.storage.get<any>(`oauth:display:${body.displayCode}`);
            requestId = display?.requestId;
          }
          const key = `oauth:request:${requestId}`;
          const entry = await this.state.storage.get<any>(key);
          if (!entry || entry.expiresAt <= Date.now() || entry.approved) return json({ error: 'code unavailable' }, 409);
          await this.state.storage.put(key, { ...entry, approved: { sub: body.sub, parentJti: body.parentJti,
            accountExp: body.accountExp, pairingScope: body.pairingScope } });
          if (entry.displayCode) await this.state.storage.delete(`oauth:display:${entry.displayCode}`);
          return json({ ok: true });
        }
        case 'oauth-result': {
          const entry = await this.state.storage.get<any>(`oauth:request:${body.requestId}`);
          if (!entry || entry.expiresAt <= Date.now()) return json({ error: 'request expired' }, 404);
          return json({ approved: Boolean(entry.approved), authCode: entry.approved ? entry.authCode : undefined,
            redirectUri: entry.approved ? entry.redirectUri : undefined, state: entry.approved ? entry.state : undefined });
        }
        case 'oauth-exchange': {
          const codeKey = `oauth:code:${body.authCode}`;
          const code = await this.state.storage.get<any>(codeKey);
          const entry = await this.state.storage.get<any>(`oauth:request:${code?.requestId}`);
          if (!entry || entry.expiresAt <= Date.now() || !entry.approved
            || (entry.challenge && entry.challenge !== body.challenge)
            || (body.clientId && entry.clientId !== body.clientId)
            || (entry.redirectUri && body.redirectUri && entry.redirectUri !== body.redirectUri)
            || (entry.resource && body.resource && entry.resource !== body.resource)) {
            return json({ error: 'invalid_grant' }, 400);
          }
          await this.state.storage.delete(codeKey);
          await this.state.storage.delete(`oauth:request:${code.requestId}`);
          return json(entry.approved);
        }
        case 'pair-start': {
          const key = `shortpair:${body.code}`;
          if (await this.state.storage.get(key)) return json({ error: 'code exists' }, 409);
          await this.state.storage.put(key, { publicKey: body.publicKey, secret: body.secret,
            provider: body.provider, confirmReceipt: body.confirmReceipt === true, expiresAt: Date.now() + 300_000 });
          await this.resetAlarm();
          return json({ ok: true });
        }
        case 'pair-lookup': {
          const pending = await this.state.storage.get<any>(`shortpair:${body.code}`);
          if (pending?.expiresAt > Date.now() && !pending.completed) return json({ publicKey: pending.publicKey,
            provider: pending.provider, confirmReceipt: pending.confirmReceipt === true });
          const authorization = await this.state.storage.get<any>(`oauth:display:${body.code}`);
          return authorization?.expiresAt > Date.now()
            ? json({ error: 'connection approval code, not pairing code' }, 400)
            : json({ error: 'code expired' }, 404);
        }
        case 'pair-approve': {
          const key = `shortpair:${body.code}`;
          const pending = await this.state.storage.get<any>(key);
          if (!pending || pending.expiresAt <= Date.now() || pending.bundle || pending.completed)
            return json({ error: 'code unavailable' }, 409);
          await this.state.storage.put(key, { ...pending, bundle: body.bundle, sub: body.sub });
          return json({ ok: true });
        }
        case 'pair-redeem': {
          const key = `shortpair:${body.code}`;
          const pending = await this.state.storage.get<any>(key);
          if (!pending || pending.expiresAt <= Date.now() || pending.secret !== body.secret || pending.completed)
            return json({ error: 'code unavailable' }, 404);
          if (!pending.bundle) return json({ pending: true }, 202);
          if (!pending.confirmReceipt) await this.state.storage.delete(key);
          return json({ bundle: pending.bundle });
        }
        case 'pair-complete': {
          const key = `shortpair:${body.code}`;
          const pending = await this.state.storage.get<any>(key);
          if (!pending || pending.expiresAt <= Date.now() || pending.secret !== body.secret
            || !pending.confirmReceipt || (!pending.bundle && !pending.completed))
            return json({ error: 'code unavailable' }, 404);
          if (!pending.completed) {
            const { bundle, ...record } = pending;
            await this.state.storage.put(key, { ...record, completed: true, expiresAt: Date.now() + 300_000 });
            await this.resetAlarm();
          }
          return json({ ok: true });
        }
        case 'pair-confirmation': {
          const pending = await this.state.storage.get<any>(`shortpair:${body.code}`);
          return json({ completed: Boolean(pending?.completed && pending.expiresAt > Date.now() && pending.sub === body.sub) });
        }
        case 'account-token-set': {
          await this.migrateAccountPair();
          const scope = body.scope as AccountPair['scope'];
          const key = this.pairKey(scope);
          const previous = await this.state.storage.get<AccountPair>(key);
          if (previous) await this.state.storage.delete(`account-pair-token:${previous.jti}`);
          await this.state.storage.put(key, { scope, jti: body.jti, provider: body.provider, exp: body.exp,
            sourceScope: body.sourceScope } satisfies AccountPair);
          await this.state.storage.put(`account-pair-token:${body.jti}`, key);
          return json({ ok: true });
        }
        case 'account-token-check': {
          const pair = await this.accountPairByToken(body.jti);
          return json({ active: Boolean(pair), pair });
        }
        case 'account-token-status': {
          await this.migrateAccountPair();
          const stored = await this.state.storage.list<AccountPair>({ prefix: 'account-pair:' });
          const pairings = [...stored.values()].filter((pair) => pair.exp > Date.now());
          const scope = body.scope || await this.state.storage.get('accountActive');
          const current = scope ? pairings.find((pair) => pair.scope.packId === scope.packId
            && pair.scope.playthroughId === scope.playthroughId) : undefined;
          return json({ paired: Boolean(current), provider: current?.provider, tokenJti: current?.jti,
            tokenExp: current?.exp, pairings: pairings.map(({ scope, provider, exp }) => ({ scope, provider, expiresAt: exp })) });
        }
        case 'transfer-start': {
          await this.state.storage.put(`keytransfer:${body.requestId}`, { ticket: body.ticket, expiresAt: body.expiresAt });
          await this.resetAlarm();
          return json({ requestId: body.requestId });
        }
        case 'transfer-pending': {
          const entries = await this.state.storage.list<any>({ prefix: 'keytransfer:' });
          return json({ requests: [...entries.values()].filter((entry) => entry.expiresAt > Date.now() && !entry.envelope)
            .map((entry) => ({ ticket: entry.ticket })) });
        }
        case 'transfer-respond': {
          const key = `keytransfer:${body.requestId}`;
          const entry = await this.state.storage.get<any>(key);
          if (!entry || entry.expiresAt <= Date.now()) return json({ error: 'request expired' }, 404);
          await this.state.storage.put(key, { ...entry, envelope: body.envelope });
          return json({ ok: true });
        }
        case 'transfer-result': {
          const entry = await this.state.storage.get<any>(`keytransfer:${body.requestId}`);
          if (!entry || entry.expiresAt <= Date.now()) return json({ error: 'request expired' }, 404);
          if (!entry.envelope) return json({ pending: true });
          await this.state.storage.delete(`keytransfer:${body.requestId}`);
          return json({ envelope: entry.envelope });
        }
        case 'account-session-check': {
          return json({ active: Boolean(await this.accountPairByToken(body.parentJti)) });
        }
        case 'account-active-set': {
          await this.state.storage.put('accountActive', body.scope || null);
          if (body.scope) await this.state.storage.put('accountLastScope', body.scope);
          return json({ ok: true });
        }
        case 'account-active-get': return json({ scope: await this.state.storage.get('accountActive') || null,
          lastScope: await this.state.storage.get('accountLastScope') || null });
        case 'deactivate-agent': {
          await this.state.storage.put('revokedAll', true);
          if (body.clearSubscriptions) {
            for (const prefix of ['event-subscription:', 'webhook:']) {
              const entries = await this.state.storage.list({ prefix });
              for (const key of entries.keys()) await this.state.storage.delete(key);
            }
          }
          for (const socket of this.state.getWebSockets()) {
            if (socket.deserializeAttachment()?.kind === 'agent') socket.close(1008, 'Pack changed');
          }
          return json({ ok: true });
        }
        case 'pair': {
          const seen = await this.state.storage.get(`grant:${body.grantJti}`);
          if (seen) return json({ error: 'grant already used' }, 409);
          await this.state.storage.put(`grant:${body.grantJti}`, true);
          await this.state.storage.put('activeAgent', { jti: body.agentJti, exp: body.agentExp });
          await this.state.storage.put(`agentToken:${body.agentJti}`, { jti: body.agentJti, exp: body.agentExp });
          await this.state.storage.delete('revokedAll');
          return json({ ok: true });
        }
        case 'agent-active': return json({ active: await this.active(String(body.jti || '')) });
        case 'event-status': {
          const execution = await this.state.storage.get<EventExecution>(`event:${body.id}`);
          if (!execution) return json({ status: 'unknown' });
          const { leaseId: _leaseId, delegationIds: _delegationIds, ...publicStatus } = execution;
          return json({ ...publicStatus, delegationStatus: await this.eventDelegations(this.state.storage, execution) });
        }
        case 'event-start': {
          const key = `event:${body.id}`;
          return json(await this.state.storage.transaction(async (tx) => {
            const execution = await tx.get<EventExecution>(key);
            if (!execution) throw new Error('unknown event');
            if (execution.status === 'completed' || execution.status === 'discarded') return execution;
            if (execution.status === 'running' && execution.updatedAt + EVENT_LEASE_MS > Date.now()) {
              return { ...execution, claimed: false, retryAfterMs: execution.updatedAt + EVENT_LEASE_MS - Date.now() };
            }
            const next = { ...execution, status: 'running' as const, attempts: execution.attempts + 1,
              updatedAt: Date.now(), leaseId: crypto.randomUUID() };
            await tx.put(key, next);
            return { ...next, claimed: true };
          }));
        }
        case 'event-renew': {
          const key = `event:${body.id}`;
          await this.state.storage.transaction(async (tx) => {
            const execution = await tx.get<EventExecution>(key);
            if (!execution || execution.status !== 'running' || execution.leaseId !== body.leaseId) throw new Error('event lease unavailable');
            await tx.put(key, { ...execution, updatedAt: Date.now() });
          });
          return json({ ok: true });
        }
        case 'event-finish': {
          const key = `event:${body.id}`;
          const result = await this.state.storage.transaction(async (tx) => {
            const execution = await tx.get<EventExecution>(key);
            if (!execution) throw new Error('unknown event');
            if (execution.status === 'completed' || execution.status === 'discarded') return execution;
            if (execution.status !== 'running' || execution.leaseId !== body.leaseId
              || !['completed', 'failed'].includes(body.status)) throw new Error('invalid event transition');
            const delegates = await this.eventDelegations(tx, execution);
            if (delegates === 'pending' || (body.status === 'completed' && !['none', 'applied'].includes(delegates)))
              return null;
            const next = { ...execution, status: body.status as 'completed' | 'failed', updatedAt: Date.now(), leaseId: undefined };
            await tx.put(key, next);
            return next;
          });
          if (!result) return json({ error: 'delegations not complete' }, 409);
          for (const socket of this.state.getWebSockets()) {
            if (socket.deserializeAttachment()?.kind !== 'agent') continue;
            try { socket.send(JSON.stringify({ type: 'event-finished', eventId: body.id, status: result.status })); }
            catch { /* A reconnect will pull the retained inbox. */ }
          }
          return json(result);
        }
        case 'revoke': {
          await this.state.storage.delete('activeAgent');
          if (body.jti) {
            await this.state.storage.put(`revoked:${body.jti}`, true);
          } else {
            await this.state.storage.put('revokedAll', true);
          }
          await this.clearAgentDeliveries();
          for (const prefix of ['event-subscription:', 'webhook:']) {
            const entries = await this.state.storage.list({ prefix });
            for (const key of entries.keys()) await this.state.storage.delete(key);
          }
          for (const socket of this.state.getWebSockets()) socket.close(1008, 'Connection revoked');
          return json({ ok: true });
        }
        case 'ticket': {
          const hash = await sha256(String(body.ticket));
          await this.state.storage.put(`ticket:${hash}`, Date.now() + 60_000);
          return json({ ok: true });
        }
        case 'agent-ticket': {
          if (!(await this.active(String(body.jti || '')))) return json({ error: 'agent inactive' }, 403);
          const hash = await sha256(String(body.ticket));
          await this.state.storage.put(`agent-ticket:${hash}`, { jti: body.jti, expiresAt: Date.now() + 60_000 });
          return json({ ok: true });
        }
        case 'socket': {
          const protocol = request.headers.get('sec-websocket-protocol') || '';
          const ticket = protocol.split(',').map((value) => value.trim()).find((value) => value.startsWith('xphone-ticket.'))?.slice('xphone-ticket.'.length);
          if (!ticket) return json({ error: 'ticket required' }, 401);
          const key = `ticket:${await sha256(ticket)}`;
          const expires = await this.state.storage.get<number>(key);
          if (!expires || expires < Date.now()) return json({ error: 'ticket invalid' }, 401);
          await this.state.storage.delete(key);
          const pair = new WebSocketPair();
          this.setupAutoResponse();
          this.state.acceptWebSocket(pair[1], ['client']);
          pair[1].serializeAttachment({ kind: 'client' });
          pair[1].send(JSON.stringify({ type: 'ready' }));
          return new Response(null, { status: 101, webSocket: pair[0], headers: { 'sec-websocket-protocol': `xphone-ticket.${ticket}` } });
        }
        case 'agent-socket': {
          for (const socket of this.state.getWebSockets()) {
            if (socket.deserializeAttachment()?.kind === 'agent') {
              try { socket.close(1000, 'Replaced by new connection'); } catch (_) {}
            }
          }
          const protocol = request.headers.get('sec-websocket-protocol') || '';
          const ticket = protocol.split(',').map((value) => value.trim()).find((value) => value.startsWith('xphone-agent-ticket.'))?.slice('xphone-agent-ticket.'.length);
          if (!ticket) return json({ error: 'agent ticket required' }, 401);
          const key = `agent-ticket:${await sha256(ticket)}`;
          const record = await this.state.storage.get<{ jti: string; expiresAt: number }>(key);
          if (!record || record.expiresAt < Date.now() || !(await this.active(record.jti))) return json({ error: 'agent ticket invalid' }, 401);
          await this.state.storage.delete(key);
          const pair = new WebSocketPair();
          this.setupAutoResponse();
          this.state.acceptWebSocket(pair[1], ['agent']);
          pair[1].serializeAttachment({ kind: 'agent' });
          pair[1].send(JSON.stringify({ type: 'ready' }));
          for (const job of (await this.state.storage.list<DelegationJob>({ prefix: 'task:' })).values()) {
            if (!job.jobId) continue;
            const status = await this.delegationStatus(this.state.storage, job);
            if (status !== 'accepted' && status !== 'ready') continue;
            const parent = await this.state.storage.get<EventExecution>(`event:${job.eventId}`);
            if (parent?.status === 'running') this.notifyDelegation(pair[1], job);
          }
          return new Response(null, { status: 101, webSocket: pair[0], headers: { 'sec-websocket-protocol': `xphone-agent-ticket.${ticket}` } });
        }
        case 'enqueue': return json(await this.enqueue(body.direction, body.envelope));
        case 'event-subscribe': {
          const subscription = body.subscription as EventSubscription;
          await this.state.storage.put(`event-subscription:${subscription.id}`, subscription);
          await this.resetAlarm();
          return json({ ok: true });
        }
        case 'event-unsubscribe': {
          await this.state.storage.delete(`event-subscription:${body.id}`);
          const pending = await this.state.storage.list({ prefix: `webhook:${body.id}:` });
          for (const key of pending.keys()) await this.state.storage.delete(key);
          await this.resetAlarm();
          return json({ ok: true });
        }
        case 'pull': return json({ items: await this.pull(body.direction, Number(body.cursor) || 0, Math.max(1, Math.min(100, Number(body.limit) || 50))) });
        case 'discard-agent-events': {
          const ids = new Set<string>(body.eventIds);
          const discarded = await this.state.storage.transaction(async (tx) => {
            const queued = await tx.list<QueuedEnvelope>({ prefix: 'queue:agent:' });
            const selected = [...queued].filter(([, item]) => ids.has(item.envelope.id));
            if (!ids.size || selected.length !== ids.size) throw new Error('event IDs do not match queued events');
            const cursor = Math.max(...selected.map(([, item]) => item.cursor));
            if ([...queued.values()].some((item) => item.cursor <= cursor && !ids.has(item.envelope.id)))
              throw new Error('earlier queued events must be included');
            for (const [key, item] of selected) {
              const event = await tx.get<EventExecution>(`event:${item.envelope.id}`);
              if (event?.status === 'running') throw new Error('running event cannot be discarded');
              await tx.delete(key);
              if (event) await tx.put(`event:${item.envelope.id}`, { ...event, status: 'discarded', updatedAt: Date.now(), leaseId: undefined });
            }
            const previous = await tx.get<number>('ack:agent') || 0;
            await tx.put('ack:agent', Math.max(previous, cursor));
            return cursor;
          });
          return json({ discarded: ids.size, cursor: discarded });
        }
        case 'ack': {
          await this.ack(body.direction, Number(body.cursor));
          if (body.direction === 'agent') {
            for (const socket of this.state.getWebSockets()) {
              if (socket.deserializeAttachment()?.kind !== 'agent') continue;
              try { socket.send(JSON.stringify({ type: 'inbox-acked', cursor: Number(body.cursor) })); }
              catch { /* A reconnect will pull the retained inbox. */ }
            }
          }
          return json({ ok: true });
        }
        case 'delivery': return json(await this.state.storage.get(`delivery:${body.id}`) || { status: 'unknown' });
        case 'delegate-task': {
          const taskPath = body.taskPath as string;
          const eventId = /^\/.*\/delegations\/([A-Za-z0-9_.:-]{1,120})\/[a-f0-9-]{36}\.md$/.exec(taskPath)?.[1];
          if (!eventId || taskPath.slice(1).split('/').some((segment) => ['', '.', '..'].includes(segment)))
            return json({ error: 'invalid task path' }, 400);
          const jobId = await sha256(taskPath);
          const registration = this.state.storage.transaction(async (tx) => {
            const existing = await tx.get<DelegationJob>(`task:${jobId}`);
            if (existing) return { job: existing, created: false };
            const parent = await tx.get<EventExecution>(`event:${eventId}`);
            if (parent?.status !== 'running' || parent.route.role !== 'os-agent') return null;
            const accepted: DelegationJob = { jobId, eventId, taskPath, status: 'accepted', createdAt: Date.now() };
            await tx.put(`task:${jobId}`, accepted);
            await tx.put(`event:${eventId}`, { ...parent, delegationIds: [...(parent.delegationIds || []), jobId] });
            return { job: accepted, created: true };
          });
          this.state.waitUntil(registration.then(async (registered) => {
            if (!registered) return;
            const { job } = registered;
            const status = await this.delegationStatus(this.state.storage, job);
            if (status === 'accepted' || status === 'ready') {
              const sockets = this.state.getWebSockets().filter((socket) => socket.deserializeAttachment()?.kind === 'agent'
                && (socket.readyState === undefined || socket.readyState === 1));
              this.notifyDelegation(sockets[sockets.length - 1], job);
            }
          }));
          const registered = await registration;
          if (!registered) return json({ error: 'event is not running' }, 409);
          const { job, created } = registered;
          const status = created ? 'accepted' : await this.delegationStatus(this.state.storage, job);
          return json({ jobId, status, batchId: job.batchId });
        }
        case 'delegation-status': {
          const job = await this.state.storage.get<DelegationJob>(`task:${body.jobId}`);
          if (!job) return json({ status: 'unknown' });
          const status = await this.delegationStatus(this.state.storage, job);
          const error = status === 'failed' ? job.error || 'delegation timed out before submitting actions' : undefined;
          return json({ jobId: job.jobId, status, batchId: job.batchId, targetApp: job.targetApp, error });
        }
        case 'submit-task-actions': {
          const agentSockets = this.state.getWebSockets().filter((item) => item.deserializeAttachment()?.kind === 'agent' && (item.readyState === undefined || item.readyState === 1));
          const socket = agentSockets[agentSockets.length - 1];
          if (!socket) return json({ error: 'gateway websocket unavailable' }, 503);
          const requestId = crypto.randomUUID();
          const reply = await new Promise<{ ok: boolean; error?: string; envelope?: SealedEnvelope; frame?: SealedEnvelope }>((resolve) => {
            const timeout = setTimeout(() => {
              this.submissionWaiters.delete(requestId);
              resolve({ ok: false, error: 'gateway acknowledgement timed out' });
            }, 120_000);
            this.submissionWaiters.set(requestId, (value) => {
              clearTimeout(timeout);
              this.submissionWaiters.delete(requestId);
              resolve(value);
            });
            try { socket.send(JSON.stringify({ type: 'submit-task-actions', requestId, taskPath: body.taskPath })); }
            catch { this.submissionWaiters.get(requestId)?.({ ok: false, error: 'gateway websocket unavailable' }); }
          });
          return reply.ok ? json({ requestId, envelope: reply.envelope, frame: reply.frame })
            : json({ error: reply.error || 'submission failed' }, 502);
        }
        case 'enqueue-task-actions': {
          const envelope = body.envelope as SealedEnvelope;
          let taskPath: string | undefined;
          if (envelope.header?.origin === 'delegation') {
            if (body.direct) {
              const execution = await this.state.storage.get<EventExecution>(`event:${envelope.header.parentEventId}`);
              if (!execution || execution.status !== 'running' || execution.route.role !== 'os-agent'
                || envelope.header.step < 1 || !['messages', 'socialmedia', 'maps'].includes(envelope.header.targetApp))
                return json({ error: 'event is not running' }, 400);
            } else {
              taskPath = body.taskPath;
            }
          } else if (envelope.header?.origin === 'event') {
            const execution = await this.state.storage.get<EventExecution>(`event:${envelope.header.parentEventId}`);
            if (!execution || execution.status !== 'running') return json({ error: 'event is not running' }, 400);
          }
          if (body.frame && taskPath) return json({ error: 'delegation cannot submit a journal frame' }, 400);
          if (body.frame) await this.enqueue('client', body.frame as SealedEnvelope);
          const result = await this.enqueue('client', envelope, taskPath);
          console.info('action batch', envelope.header?.parentEventId,
            body.submitRequestId, envelope.id, envelope.header?.targetApp, result.status);
          return json({ ...result, batchId: envelope.id });
        }
        case 'enqueue-direct-actions': {
          const envelope = body.envelope as SealedEnvelope;
          const header = envelope.header!;
          if (header.origin !== 'event') return json({ error: 'event action required' }, 400);
          const execution = await this.state.storage.get<EventExecution>(`event:${header.parentEventId}`);
          if (!execution || execution.status !== 'running') return json({ error: 'event is not running' }, 400);
          const route = execution.route;
          if (route.role === 'subagent' && (header.targetApp !== route.targetApp || header.surfaceId !== route.surfaceId))
            return json({ error: 'event route mismatch' }, 400);
          const result = await this.enqueue('client', envelope);
          return json({ ...result, batchId: envelope.id });
        }
        case 'enqueue-direct-journal-frame': {
          const execution = await this.state.storage.get<EventExecution>(`event:${body.eventId}`);
          if (!execution || execution.status !== 'running' || execution.route.role !== 'os-agent'
            || execution.route.surfaceId !== 'journal.main_record') return json({ error: 'journal event is not running' }, 400);
          return json(await this.enqueue('client', body.envelope as SealedEnvelope));
        }
        case 'applied': {
          const delivery = await this.state.storage.get<{ status: string; cursor: number }>(`delivery:${body.id}`);
          if (!delivery) return json({ error: 'unknown batch' }, 404);
          await this.state.storage.put(`delivery:${body.id}`, { ...delivery, status: body.status === 'rejected' ? 'rejected' : 'applied' });
          console.info('action receipt', body.id, body.status);
          return json({ ok: true });
        }
        case 'references': {
          const consumer = String(body.consumer || '');
          const refs = body.refs as string[];
          if (!consumer || consumer.length > 100 || !Array.isArray(refs) || refs.length > 100 || refs.some((ref) => typeof ref !== 'string' || ref.length > 150)) throw new Error('invalid references');
          const activeAgent = await this.state.storage.get<{ jti: string; exp: number }>('activeAgent');
          const requestedExpiry = Number(body.expiresAt) || activeAgent?.exp || Date.now() + 300_000;
          const exp = Math.min(requestedExpiry, activeAgent?.exp || Infinity, Date.now() + 30 * 86400_000);
          if (exp <= Date.now()) throw new Error('invalid references');
          await this.state.storage.put(`refs:${consumer}`, { refs, exp });
          return json({ ok: true, expiresAt: exp });
        }
        case 'read-references': {
          const allowed = await this.state.storage.get<{ refs: string[]; exp: number }>(`refs:${body.consumer}`);
          return json({ refs: allowed && allowed.exp > Date.now() ? allowed.refs : [] });
        }
        case 'schedule': {
          const all = await this.state.storage.list({ prefix: 'schedule:' });
          if (all.size >= MAX_SCHEDULE) throw new Error('schedule full');
          const item = body as Schedule;
          if (!item.id || !Number.isSafeInteger(item.deliverAt) || !Number.isSafeInteger(item.expiresAt)
            || item.deliverAt < Date.now() || item.deliverAt > Date.now() + RETENTION_MS
            || item.expiresAt <= item.deliverAt) throw new Error('invalid schedule');
          await this.state.storage.put(`schedule:${item.id}`, item);
          await this.resetAlarm();
          return json({ status: 'scheduled' });
        }
        case 'cancel-schedule': {
          await this.state.storage.delete(`schedule:${body.id}`);
          await this.resetAlarm();
          return json({ status: 'cancelled' });
        }
        case 'blob-put': {
          const blobId = String(body.blobId || '');
          const ciphertext = String(body.ciphertext || body.data || '');
          const nonce = String(body.nonce || '');
          const mimeType = String(body.mimeType || 'image/webp');
          if (!blobId || !ciphertext || ciphertext.length > 25_000_000) throw new Error('invalid blob');
          const record = {
            blobId, ciphertext, nonce, mimeType,
            width: Number(body.width) || undefined,
            height: Number(body.height) || undefined,
            createdAt: Date.now(),
            expiresAt: Date.now() + 3600_000,
          };
          await this.state.storage.put('imgblob:' + blobId, record);
          await this.resetAlarm();
          return json({ ok: true, blobId });
        }
        case 'blob-get': {
          const blobId = String(body.blobId || '');
          if (!blobId) throw new Error('blobId required');
          const record = await this.state.storage.get<any>('imgblob:' + blobId);
          if (!record || record.expiresAt < Date.now()) {
            if (record) await this.state.storage.delete('imgblob:' + blobId);
            return json({ error: 'blob not found or expired' }, 404);
          }
          return json({
            blobId: record.blobId,
            ciphertext: record.ciphertext,
            nonce: record.nonce,
            mimeType: record.mimeType,
            width: record.width,
            height: record.height,
            expiresAt: record.expiresAt,
          });
        }
        case 'agent-profile-put': {
          const profile = body.profile || body;
          await this.state.storage.put('agent:profile', {
            name: String(profile.name || 'Muse'),
            avatarImage: typeof profile.avatarImage === 'string' ? profile.avatarImage : undefined,
            videos: profile.videos && typeof profile.videos === 'object' ? profile.videos : undefined,
            updatedAt: Date.now(),
          });
          return json({ ok: true, updatedAt: Date.now() });
        }
        case 'agent-profile-get': {
          const profile = await this.state.storage.get<any>('agent:profile');
          return json({ profile: profile || null });
        }
        case 'list-schedule': {
          const all = await this.state.storage.list<Schedule>({ prefix: 'schedule:' });
          return json({ items: [...all.values()].map((item) => ({ id: item.id, deliverAt: item.deliverAt, expiresAt: item.expiresAt })) });
        }
        default: return json({ error: 'unknown operation' }, 404);
      }
    } catch (error) {
      if (operation === 'enqueue-task-actions' && error instanceof Error
        && ['task association mismatch', 'event is not running'].includes(error.message))
        return json({ error: error.message }, 400);
      return json({ error: 'request rejected' }, 400);
    }
  }

  async webSocketMessage(socket: WebSocket, message: string | ArrayBuffer): Promise<void> {
    if (message === 'ping') { socket.send('pong'); return; }
    if (socket.deserializeAttachment()?.kind !== 'agent' || typeof message !== 'string') return;
    let reply: SubmissionReply & { type?: string; requestId?: string; jobId?: string;
      batchId?: string; step?: number; targetApp?: string; surfaceId?: string };
    try { reply = JSON.parse(message); } catch { return; }
    if (reply?.type === 'submit-actions-ack' && reply.requestId) {
      this.submissionWaiters.get(reply.requestId)?.(reply);
      return;
    }
    if (!reply?.jobId || !['delegate-prepared', 'delegate-failed'].includes(reply.type || '')) return;
    const key = `task:${reply.jobId}`;
    const prepared = await this.state.storage.transaction(async (tx) => {
      const job = await tx.get<DelegationJob>(key);
      if (!job) return;
      const parent = await tx.get<EventExecution>(`event:${job.eventId}`);
      if (parent?.status !== 'running') return;
      const status = await this.delegationStatus(tx, job);
      if (status !== 'accepted' && status !== 'ready') return;
      if (reply.type === 'delegate-failed') {
        await tx.put(key, { ...job, status: 'failed', error: reply.error });
        return;
      }
      if (job.status === 'ready') return job;
      if (!reply.batchId || !reply.targetApp || !reply.surfaceId || !Number.isSafeInteger(reply.step) || reply.step! < 1) return;
      const ready: DelegationJob = { ...job, status: 'ready', batchId: reply.batchId, step: reply.step,
        targetApp: reply.targetApp, surfaceId: reply.surfaceId };
      await tx.put(key, ready);
      return ready;
    });
    if (prepared) this.notifyDelegation(socket, prepared);
    if (reply.type === 'delegate-failed') {
      try { socket.send(JSON.stringify({ type: 'delegation-progress' })); }
      catch { /* A reconnect will read the durable job status. */ }
    }
  }

  webSocketClose(socket: WebSocket): void {
    if (socket.deserializeAttachment()?.kind === 'agent') {
      for (const done of this.submissionWaiters.values()) done({ ok: false, error: 'gateway websocket closed' });
    }
    try { socket.close(1000, 'Closed'); } catch { /* already closed */ }
  }
  webSocketError(socket: WebSocket): void {
    if (socket.deserializeAttachment()?.kind === 'agent') {
      for (const done of this.submissionWaiters.values()) done({ ok: false, error: 'gateway websocket error' });
    }
    try { socket.close(1011, 'Error'); } catch { /* already closed */ }
  }
}
