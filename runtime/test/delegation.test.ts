import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { AgentConnection } from '../src/connection';

test('durable delegation gates parent completion across a lost response and a slow subagent', async () => {
  const values = new Map<string, unknown>();
  const storage = {
    get: async (key: string) => values.get(key),
    put: async (key: string, value: unknown) => { values.set(key, value); },
  };
  const database = { ...storage, transaction: async <T>(run: (tx: typeof storage) => Promise<T>) => run(storage) };
  const notices: Array<{ type: string; jobId: string }> = [];
  const notifications: Promise<unknown>[] = [];
  const socket = {
    readyState: 1,
    deserializeAttachment: () => ({ kind: 'agent' }),
    send: (message: string) => notices.push(JSON.parse(message)),
  } as unknown as WebSocket;
  const connection = (online: boolean) =>
    new AgentConnection({ storage: database, getWebSockets: () => online ? [socket] : [],
      waitUntil: (pending: Promise<unknown>) => notifications.push(pending),
    } as unknown as DurableObjectState);
  let broker = connection(false);
  const call = (operation: string, body: Record<string, unknown>) => broker.fetch(new Request('https://local/', {
    method: 'POST', headers: { 'x-operation': operation }, body: JSON.stringify(body),
  }));

  const eventId = 'parent';
  values.set(`event:${eventId}`, { id: eventId, status: 'running', leaseId: 'lease', route: { role: 'os-agent' } });
  const notes = '/content/pack/play/tasks/delegations/parent/123e4567-e89b-42d3-a456-426614174000.md';
  const characters = '/content/pack/play/tasks/delegations/parent/123e4567-e89b-42d3-a456-426614174001.md';
  const noteJob = await (await call('delegate-task', { taskPath: notes })).json() as { jobId: string; status: string };
  // Simulate a caller disconnect and a new runtime instance sharing the persisted storage.
  broker = connection(true);
  const retry = await (await call('delegate-task', { taskPath: notes })).json();
  await Promise.all(notifications.splice(0));
  assert.deepEqual(retry, noteJob);
  assert.equal(noteJob.status, 'accepted');
  assert.equal(notices.at(-1)?.type, 'delegate-task');
  const characterJob = await (await call('delegate-task', { taskPath: characters })).json() as { jobId: string };
  const finish = () => call('event-finish', { id: eventId, leaseId: 'lease', status: 'completed' });
  assert.equal((await finish()).status, 409);

  const submit = (taskPath: string, batchId: string, targetApp: string, step: number) =>
    call('enqueue-task-actions', { taskPath, envelope: { id: batchId, kind: 'action_batch', header: {
      batchId, parentEventId: eventId, step, targetApp, surfaceId: `${targetApp}.active`, origin: 'delegation',
    } } });
  const prepare = (jobId: string, batchId: string, targetApp: string, step: number) =>
    broker.webSocketMessage(socket, JSON.stringify({ type: 'delegate-prepared', jobId, batchId, step,
      targetApp, surfaceId: `${targetApp}.active` }));

  await prepare(noteJob.jobId, 'note', 'notes', 1);
  assert.equal((await submit(notes, 'note', 'notes', 1)).status, 200);
  await call('applied', { id: 'note', status: 'applied' });
  assert.equal((await finish()).status, 409);
  assert.equal((await submit(characters, 'character', 'characters', 2)).status, 400);

  await prepare(characterJob.jobId, 'character', 'characters', 2);
  assert.equal(notices.at(-1)?.type, 'delegate-ready');
  assert.equal((await submit(characters, 'character', 'characters', 2)).status, 200);
  assert.equal((await finish()).status, 409);
  await call('applied', { id: 'character', status: 'applied' });
  assert.equal((await finish()).status, 200);
});
