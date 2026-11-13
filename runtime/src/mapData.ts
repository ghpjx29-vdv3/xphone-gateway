import type { ReadableObjects } from './catalogObjects';

export interface MapPlace {
  placeId: string;
  fictionName: string;
  realAnchor: { name: string; lat: number; lng: number; osmId?: string };
  type: string;
  tags: string[];
  zoneId: string;
  description: string;
}

interface MapPack {
  city: string;
  center: [number, number];
  zoom: number;
  initialActors?: Array<{ actorId: string; label: string; placeId: string }>;
  zones: Array<{ id: string; name: string; description: string }>;
  places: MapPlace[];
}

export async function readMapPack(bucket: ReadableObjects, packId: string): Promise<MapPack> {
  let object = await bucket.get(`packs/${packId}/maps/places.json`);
  if (!object && (packId === 'xphone-agent-blank' || !packId)) {
    object = await bucket.get('packs/圣埃斯佩兰萨的棠-456f3930/maps/places.json');
  }
  if (!object) throw new Error('map unavailable');
  return object.json<MapPack>();
}

export async function mapFeatures(bucket: ReadableObjects, featureManifest: { grid: number; bounds: number[]; tiles: Array<{ id: string; objectKey: string }> }, bbox: number[]): Promise<unknown> {
  const [west, south, east, north] = bbox;
  const grid = featureManifest.grid;
  const cells = new Set<string>();
  for (let x = Math.floor(west / grid); x <= Math.floor(east / grid); x += 1) {
    for (let y = Math.floor(south / grid); y <= Math.floor(north / grid); y += 1) cells.add(`${x}_${y}`);
  }
  const selected = featureManifest.tiles.filter((tile) => cells.has(tile.id));
  const data = await Promise.all(selected.map(async (tile) => {
    const object = await bucket.get(tile.objectKey);
    if (!object) throw new Error('map geometry unavailable');
    return object.json<{ features: any[] }>();
  }));
  const features = new Map<string, any>();
  for (const collection of data) for (const feature of collection?.features || []) features.set(String(feature.id), feature);
  return { type: 'FeatureCollection', features: [...features.values()] };
}

function place(data: MapPack, placeId: string): MapPlace {
  const result = data.places.find((item) => item.placeId === placeId);
  if (!result) throw new Error('place unavailable');
  return result;
}

function terms(input: string): string[] {
  return [...new Intl.Segmenter('zh', { granularity: 'word' }).segment(input.toLowerCase())]
    .filter((part) => part.isWordLike && !['的', '了', '在', '想', '去', '找', '一个'].includes(part.segment))
    .map((part) => part.segment);
}

function search(data: MapPack, query: string): MapPlace[] {
  const words = terms(query);
  const scored = data.places.map((item) => {
    const zone = data.zones.find((candidate) => candidate.id === item.zoneId)?.name || '';
    const fields = [item.fictionName, item.type, zone, item.description, ...item.tags].map((value) => value.toLowerCase());
    let score = words.reduce((sum, word) => sum + fields.reduce((total, field) => total + (field.includes(word) ? 1 : 0), 0), 0);
    if (query.includes('咖啡') && item.type === 'cafe') score += 3;
    if ((query.includes('海边') || query.includes('水边')) && item.tags.includes('河边')) score += 2;
    if (query.includes('安静') && item.tags.includes('安静')) score += 2;
    return { item, score };
  });
  return scored.filter(({ score }) => score > 0).sort((a, b) => b.score - a.score).map(({ item }) => item);
}

function coordinate(item: MapPlace): string {
  return `${item.realAnchor.lng},${item.realAnchor.lat}`;
}

async function osrm(mode: string, service: 'route' | 'table', points: MapPlace[]): Promise<any> {
  const profile = mode === 'cycling' ? 'bike' : mode === 'driving' || mode === 'transit' ? 'car' : 'foot';
  const query = service === 'route' ? '?overview=full&geometries=geojson&steps=true' : '?annotations=distance,duration';
  const urls = [
    `https://routing.openstreetmap.de/routed-${profile}/${service}/v1/driving/${points.map(coordinate).join(';')}${query}`,
    `https://router.project-osrm.org/${service}/v1/${profile === 'bike' ? 'bicycle' : profile === 'car' ? 'driving' : 'foot'}/${points.map(coordinate).join(';')}${query}`,
    `https://router.project-osrm.org/${service}/v1/driving/${points.map(coordinate).join(';')}${query}`,
  ];
  const headers = { 'User-Agent': 'xphone-maps/1.0 (https://phone.xenoflex.xyz)' };
  for (const url of urls) {
    try {
      const response = await fetch(url, { headers });
      if (!response.ok) continue;
      const result = await response.json() as any;
      if (result.code === 'Ok') return result;
    } catch {
      continue;
    }
  }
  // Robust fallback if external servers are unreachable or rate-limited
  const speed = mode === 'driving' ? 10 : mode === 'cycling' ? 4.5 : 1.4; // m/s
  if (service === 'table') {
    const durations: number[][] = [];
    for (let i = 0; i < points.length; i++) {
      durations[i] = [];
      for (let j = 0; j < points.length; j++) {
        if (i === j) {
          durations[i][j] = 0;
        } else {
          const dy = (points[j].realAnchor.lat - points[i].realAnchor.lat) * 111_320;
          const dx = (points[j].realAnchor.lng - points[i].realAnchor.lng) * 111_320 * Math.cos(((points[i].realAnchor.lat + points[j].realAnchor.lat) * Math.PI) / 360);
          const dist = Math.hypot(dx, dy) * 1.3;
          durations[i][j] = Math.round(dist / speed);
        }
      }
    }
    return { code: 'Ok', durations };
  }
  const coords: number[][] = [];
  let totalDist = 0;
  const legs: any[] = [];
  for (let i = 0; i < points.length - 1; i++) {
    const p1 = points[i];
    const p2 = points[i + 1];
    const dy = (p2.realAnchor.lat - p1.realAnchor.lat) * 111_320;
    const dx = (p2.realAnchor.lng - p1.realAnchor.lng) * 111_320 * Math.cos(((p1.realAnchor.lat + p2.realAnchor.lat) * Math.PI) / 360);
    const legDist = Math.round(Math.hypot(dx, dy) * 1.3);
    const legDur = Math.round(legDist / speed);
    totalDist += legDist;
    const steps = 8;
    for (let s = 0; s < steps; s++) {
      const t = s / steps;
      coords.push([
        p1.realAnchor.lng + (p2.realAnchor.lng - p1.realAnchor.lng) * t,
        p1.realAnchor.lat + (p2.realAnchor.lat - p1.realAnchor.lat) * t,
      ]);
    }
    legs.push({ distance: legDist, duration: legDur });
  }
  coords.push([points[points.length - 1].realAnchor.lng, points[points.length - 1].realAnchor.lat]);
  return {
    code: 'Ok',
    routes: [{
      distance: totalDist,
      duration: Math.round(totalDist / speed),
      geometry: { type: 'LineString', coordinates: coords },
      legs,
    }],
  };
}

async function route(data: MapPack, args: { fromPlaceId: string; toPlaceId: string; viaPlaceIds?: string[]; mode?: string }): Promise<unknown> {
  const mode = args.mode || 'walking';
  if (!['walking', 'cycling', 'driving', 'transit'].includes(mode)) throw new Error('travel mode unavailable');
  const points = [args.fromPlaceId, ...(args.viaPlaceIds || []), args.toPlaceId].map((id) => place(data, id));
  const result = await osrm(mode, 'route', points);
  const selected = result.routes[0];
  return { fromPlaceId: args.fromPlaceId, toPlaceId: args.toPlaceId, mode,
    distanceMeters: selected.distance, durationSeconds: mode === 'transit' ? Math.round(selected.duration * 1.6 + 600) : selected.duration,
    estimated: mode === 'transit',
    geometry: selected.geometry, waypoints: points.map((item, index) => ({ placeId: item.placeId, fictionName: item.fictionName,
      coordinate: [item.realAnchor.lng, item.realAnchor.lat], legDistanceMeters: selected.legs[index - 1]?.distance || 0,
      legDurationSeconds: mode === 'transit' ? Math.round((selected.legs[index - 1]?.duration || 0) * 1.6) : selected.legs[index - 1]?.duration || 0 })) };
}

async function optimize(data: MapPack, args: { placeIds: string[]; mode?: string }): Promise<unknown> {
  if (!Array.isArray(args.placeIds) || args.placeIds.length < 2 || args.placeIds.length > 8) throw new Error('choose 2 to 8 places');
  const mode = args.mode || 'walking';
  if (!['walking', 'cycling', 'driving', 'transit'].includes(mode)) throw new Error('travel mode unavailable');
  const points = args.placeIds.map((id) => place(data, id));
  const matrix = (await osrm(mode, 'table', points)).durations as number[][];
  const memo = new Map<string, { duration: number; order: number[] }>();
  const visit = (last: number, mask: number): { duration: number; order: number[] } => {
    if (mask === (1 << points.length) - 1) return { duration: 0, order: [] };
    const key = `${last}:${mask}`;
    const known = memo.get(key);
    if (known) return known;
    let best = { duration: Infinity, order: [] as number[] };
    for (let next = 1; next < points.length; next += 1) {
      if (mask & (1 << next) || matrix[last]?.[next] == null) continue;
      const rest = visit(next, mask | (1 << next));
      const duration = matrix[last][next] + rest.duration;
      if (duration < best.duration) best = { duration, order: [next, ...rest.order] };
    }
    memo.set(key, best);
    return best;
  };
  const chosen = visit(0, 1);
  if (!Number.isFinite(chosen.duration)) throw new Error('route unavailable');
  const ordered = [points[0], ...chosen.order.map((index) => points[index])];
  const result = await route(data, { fromPlaceId: ordered[0].placeId,
    viaPlaceIds: ordered.slice(1, -1).map((item) => item.placeId), toPlaceId: ordered.at(-1)!.placeId, mode });
  return { order: ordered.map((item) => item.placeId), route: result };
}

export async function mapQuery(bucket: ReadableObjects, packId: string, name: string, args: any): Promise<unknown> {
  const data = await readMapPack(bucket, packId);
  switch (name) {
    case 'map.get_places': return { city: data.city, center: data.center, zoom: data.zoom, initialActors: data.initialActors || [],
      zones: data.zones, places: data.places.map(({ realAnchor, ...item }) => ({ ...item, coordinate: [realAnchor.lng, realAnchor.lat] })) };
    case 'map.get_place': return args.placeId ? place(data, String(args.placeId))
      : data.places.find((item) => item.fictionName === args.name || item.realAnchor.name === args.name) || null;
    case 'map.get_zone': {
      const zoneId = args.placeId ? place(data, String(args.placeId)).zoneId : args.zoneId;
      return data.zones.find((item) => item.id === zoneId) || null;
    }
    case 'map.search_places': return { places: search(data, String(args.query || '')).slice(0, 20) };
    case 'map.route': return route(data, args);
    case 'map.optimize_route': return optimize(data, args);
    case 'assets.get_place_reference': {
      const item = place(data, String(args.placeId));
      return { placeId: item.placeId, fictionName: item.fictionName, realAnchor: item.realAnchor,
        description: item.description, tags: item.tags, referenceUrls: [] };
    }
    case 'map.get_actor_positions': {
      const actors = (data.initialActors || []).map((actor) => {
        const p = data.places.find((item) => item.placeId === actor.placeId);
        return {
          actorId: actor.actorId,
          label: actor.label,
          placeId: actor.placeId,
          fictionName: p?.fictionName || null,
          coordinate: p ? [p.realAnchor.lng, p.realAnchor.lat] : undefined,
          lat: p?.realAnchor.lat,
          lng: p?.realAnchor.lng,
        };
      });
      return { actors };
    }
    case 'map.nearby_actors': {
      const actors = (data.initialActors || []).map((actor) => {
        const p = data.places.find((item) => item.placeId === actor.placeId);
        return {
          actorId: actor.actorId,
          label: actor.label,
          placeId: actor.placeId,
          fictionName: p?.fictionName || null,
          lat: p?.realAnchor.lat || 0,
          lng: p?.realAnchor.lng || 0,
        };
      });
      let centerLat = 0;
      let centerLng = 0;
      let centerName = 'center';
      if (typeof args.lat === 'number' && typeof args.lng === 'number') {
        centerLat = args.lat;
        centerLng = args.lng;
        centerName = 'custom coordinate';
      } else if (args.centerPlaceId) {
        const p = place(data, String(args.centerPlaceId));
        centerLat = p.realAnchor.lat;
        centerLng = p.realAnchor.lng;
        centerName = p.fictionName;
      } else {
        const targetActorId = args.centerActorId || 'user';
        const target = actors.find((a) => a.actorId === targetActorId) || actors[0];
        if (target) {
          centerLat = target.lat;
          centerLng = target.lng;
          centerName = target.label;
        }
      }
      const radius = typeof args.radiusMeters === 'number' ? args.radiusMeters : 5000;
      const nearby = actors
        .map((a) => {
          const dy = (a.lat - centerLat) * 111_320;
          const dx = (a.lng - centerLng) * 111_320 * Math.cos(((centerLat + a.lat) * Math.PI) / 360);
          const distanceMeters = Math.round(Math.hypot(dx, dy));
          return { ...a, distanceMeters };
        })
        .filter((a) => a.distanceMeters <= radius)
        .sort((a, b) => a.distanceMeters - b.distanceMeters);
      return {
        center: { lat: centerLat, lng: centerLng, label: centerName },
        radiusMeters: radius,
        actors: nearby,
      };
    }
    case 'map.get_items_at': {
      const placeId = String(args.placeId || '');
      const p = place(data, placeId);
      return {
        placeId: p.placeId,
        fictionName: p.fictionName,
        items: [],
      };
    }
    case 'map.watch_place': {
      const placeId = String(args.placeId || '');
      const p = place(data, placeId);
      return {
        status: 'active',
        watchId: `watch_${p.placeId}_${Date.now()}`,
        placeId: p.placeId,
        fictionName: p.fictionName,
        radiusMeters: typeof args.radiusMeters === 'number' ? args.radiusMeters : 200,
        notifyOn: args.notifyOn || 'enter',
        message: `Watching place ${p.fictionName}`,
      };
    }
    default: throw new Error('unknown map query');
  }
}
