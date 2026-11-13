type ReadableObject = Pick<R2ObjectBody, 'json' | 'text'> & { body: ReadableStream | null };

export interface ReadableObjects {
  get(key: string): Promise<ReadableObject | null>;
}

export type RuntimeObjects = ReadableObjects & Pick<R2Bucket, 'put'>;

async function readCatalogObject(url: URL, relayKey: string): Promise<Response> {
  const request = () => fetch(url, {
    redirect: 'manual', signal: AbortSignal.timeout(45_000),
    headers: { 'x-xphone-relay-key': relayKey },
  });
  try {
    return await request();
  } catch {
    // A read can be replayed if workerd loses a pooled connection.
    return request();
  }
}

export function gatewayObjects(bucket: R2Bucket, catalogOrigin: string, relayKey: string): RuntimeObjects {
  return {
    async get(key) {
      const pack = /^packs\/([^/]+)\/(?:manifest\.json$|(?:documents|runtime|references|maps)\/)/.exec(key)?.[1];
      if (!pack || pack === 'xphone-agent-blank') return bucket.get(key);
      let response: Response;
      try {
        const url = new URL('/gateway/catalog-object', catalogOrigin);
        url.searchParams.set('key', key);
        response = await readCatalogObject(url, relayKey);
      } catch (error) {
        console.warn('Local XPhone catalog connection failed', error instanceof Error ? error.name : 'UnknownError');
        throw Object.assign(new Error('story catalog unavailable'), { status: 503 });
      }
      if (response.status === 404) return null;
      if (!response.ok) throw Object.assign(new Error('story catalog unavailable'), { status: 503 });
      return response;
    },
    put: bucket.put.bind(bucket),
  };
}
