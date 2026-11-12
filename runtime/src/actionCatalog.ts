// Mirrors the client-side allowlist in GIGI src/agentruntime/remoteAgentActions.ts.
// This is an Agent-facing contract; the client remains the action validator.
export const actions = [
  { type: 'characters.create', targetApp: 'characters', role: 'characters subagent', required: { name: 'display name', description: 'character description' }, optional: { avatar: 'encrypted blob or image URL', voiceId: 'existing Fish Audio voice ID' }, note: 'The client creates the actor ID. Wait for applied receipt and refreshed actor directory before sending messages.' },
  { type: 'characters.update', targetApp: 'characters', role: 'characters subagent', required: { character: 'existing character display name' }, optional: { name: 'new display name', description: 'character description', avatar: 'encrypted blob or image URL', voiceId: 'existing Fish Audio voice ID' } },
  { type: 'photos.import', targetApp: 'photos', role: 'photos subagent', required: { photoId: 'unique photo ID', imageUrl: 'selected encrypted blob or image URL' }, note: 'Use only when the user explicitly asks to import selected photos.' },
  { type: 'map.replace_catalogue', targetApp: 'maps', role: 'maps subagent', required: { catalogue: '{city,center:[lng,lat],zoom,zones,places} with places containing placeId,fictionName,type,tags,zoneId,description,realAnchor:{name,lat,lng}' }, note: 'Replaces the blank phone map catalogue in its encrypted save.' },
  {
    type: 'agent.reply', targetApp: 'messages', role: 'os-agent',
    required: {},
    optional: { text: 'OS conversation reply, 1-5000 characters', imageUrl: 'direct image URL or blobId', messageType: 'text | image' },
    note: 'Writes only to the fixed OS conversation inside Messages.',
  },
  {
    type: 'messages.append', targetApp: 'messages', role: 'messages subagent',
    required: {},
    optional: { text: '1-5000 characters', imageUrl: 'direct image URL or blobId', messageType: 'text | image | location', location: '{placeId,name,lat,lon,address?,imageUrl?} for location cards' },
    note: 'The client resolves the target Messages surface to its exact thread and registered actor name.',
  },
  { type: 'map.set_actor_position', targetApp: 'maps', role: 'maps subagent', required: { actor: 'character display name or 我', place: 'fictional place name' } },
  { type: 'map.place_item', targetApp: 'maps', role: 'maps subagent', required: { place: 'fictional place name', name: 'item name' }, optional: { description: 'item description' } },
  { type: 'map.remove_item', targetApp: 'maps', role: 'maps subagent', required: { item: 'existing item name' } },
  { type: 'map.applyevent', targetApp: 'maps', role: 'maps subagent', required: { title: 'event title', story: 'event story text' }, note: 'The client uses the current arrival event to resolve its trip and place.' },
  { type: 'map.encounter_offer', targetApp: 'maps', role: 'maps subagent',
    required: { name: 'AI-generated character name', personality: 'AI-generated personality', description: 'AI-generated character description', openingMessage: 'AI-generated first message shown on the friend request card' },
    optional: { scenario: 'AI-generated story context' },
    note: 'Only for map.encounter_requested. The client binds the offer to the event location; the user must accept before the character is created.' },
  { type: 'map.set_place_image', targetApp: 'maps', role: 'maps subagent', required: { place: 'fictional place name', url: 'generated image URL from personal Agent' } },
  {
    type: 'socialmedia.applyBatch', targetApp: 'socialmedia', role: 'socialmedia subagent',
    required: { actions: 'array of 1-20 social actions' },
    actionVariants: [
      { platform: 'instagram', operation: 'appendPost', required: { displayName: 'known actor name', caption: '1-10000 characters' }, optional: ['username', 'imageUrl'] },
      { platform: 'x', operation: 'appendPost', required: { authorName: 'known actor name', text: '1-10000 characters' }, optional: ['authorHandle', 'imageUrl'] },
      { platform: 'reddit', operation: 'appendPost', required: { author: 'known actor name', subreddit: '1-100 characters', title: '1-300 characters', body: '1-10000 characters' }, optional: ['imageUrl'] },
      { platform: 'instagram|x|reddit', operation: 'appendComment', required: { author: 'known actor name', text: '1-5000 characters' }, optional: ['imageUrl'] },
    ],
    note: 'The client creates IDs for new posts and uses the current post event as the comment target.',
  },
  {
    type: 'notes.create', targetApp: 'notes', role: 'notes subagent',
    required: { body: '1-20000 characters' }, optional: { title: '1-200 characters' },
  },
  {
    type: 'notes.update', targetApp: 'notes', role: 'notes subagent',
    required: { note: 'existing note title' },
    optional: { title: '1-200 characters', body: '1-20000 characters' },
  },
  {
    type: 'world.applyStatePatch', targetApp: 'world', role: 'world subagent',
    required: { changes: '1-20 declared state variables mapped to number, boolean, or string values' },
    note: 'The client fills the current state revision and rejects unknown variables.',
  },
  {
    type: 'journal.appendMainRecord', targetApp: 'g5_dashboard', role: 'os-agent direct writer',
    required: {
      recordId: 'same record ID as streamed Journal draft',
      markdown: 'exact streamed Markdown, 1-40000 characters',
      options: 'array of exactly 3 numbered options: [{ index: 1, text: "..." }, { index: 2, text: "..." }, { index: 3, text: "..." }]'
    },
    note: 'Submit alone in a batch with the same tickId after start/update frames. Options are required in all modes (fixed and free).',
  },
] as const;
