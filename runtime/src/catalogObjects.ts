type ReadableObject = Pick<R2ObjectBody, 'json' | 'text'> & { body: ReadableStream | null };

export interface ReadableObjects {
  get(key: string): Promise<ReadableObject | null>;
}

export type RuntimeObjects = ReadableObjects & Pick<R2Bucket, 'put'>;

export function gatewayObjects(bucket: R2Bucket, catalogOrigin: string, relayKey: string): RuntimeObjects {
  const endpoint = new URL('/gateway/catalog-object', catalogOrigin);
  return {
    async get(key) {
      const pack = /^packs\/([^/]+)\/(?:manifest\.json$|(?:documents|runtime|references|maps)\/)/.exec(key)?.[1];
      if (!pack || pack === 'xphone-agent-blank') return bucket.get(key);
      const response = await fetch(endpoint, {
        method: 'POST', redirect: 'manual', signal: AbortSignal.timeout(12_000),
        headers: { 'content-type': 'application/json', 'x-xphone-relay-key': relayKey },
        body: JSON.stringify({ key }),
      });
      if (response.status === 404) return null;
      if (!response.ok) throw new Error('story catalog unavailable');
      return response;
    },
    put: bucket.put.bind(bucket),
  };
}
