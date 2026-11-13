import worker, { AgentConnection, setGatewayPublicBase } from './index';
import { gatewayObjects } from './catalogObjects';

export { AgentConnection };

type WorkerEnv = Parameters<typeof worker.fetch>[1];
type Env = WorkerEnv & {
  AGENT_DOCUMENTS: R2Bucket;
  XPHONE_CATALOG_ORIGIN: string;
  XPHONE_GATEWAY_RELAY_SECRET: string;
  XPHONE_RELAY_ORIGIN: string;
};

function matchesSecret(supplied: string, secret: string): boolean {
  if (supplied.length !== secret.length || secret.length !== 43) return false;
  let mismatch = 0;
  for (let index = 0; index < secret.length; index++) {
    mismatch |= supplied.charCodeAt(index) ^ secret.charCodeAt(index);
  }
  return mismatch === 0;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const supplied = request.headers.get('x-xphone-relay-key') || '';
    if (!matchesSecret(supplied, env.XPHONE_GATEWAY_RELAY_SECRET || '')) {
      return new Response(null, { status: 404 });
    }
    try {
      const url = new URL(request.headers.get('x-xphone-public-url') || '');
      const localUrl = new URL(request.url);
      if (url.origin !== env.XPHONE_RELAY_ORIGIN || url.pathname !== localUrl.pathname || url.search !== localUrl.search) {
        return new Response(null, { status: 400 });
      }
      const headers = new Headers(request.headers);
      headers.delete('x-xphone-relay-key');
      headers.delete('x-xphone-public-url');
      headers.delete('x-xphone-public-prefix');
      const forwarded = new Request(url, new Request(request, { headers }));
      const prefix = request.headers.get('x-xphone-public-prefix');
      if (prefix && /^\/g\/[a-f0-9-]{36}$/.test(prefix)) setGatewayPublicBase(forwarded, prefix);
      return await worker.fetch(forwarded, {
        ...env, AGENT_DOCUMENTS: gatewayObjects(env.AGENT_DOCUMENTS, env.XPHONE_CATALOG_ORIGIN,
          env.XPHONE_GATEWAY_RELAY_SECRET),
      });
    } catch {
      return Response.json({ error: 'Gateway request failed' }, { status: 502 });
    }
  },
};
