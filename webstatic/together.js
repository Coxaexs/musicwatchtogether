/* Shared client for Watch Together and Reels Together (Hoffle activities).
 *
 * Hoffle frames the page with a one-time ticket in the URL hash. The ticket is
 * traded for a session id (kept in sessionStorage for this tab, never in a
 * cookie, so a frame from another site works too) that opens the websocket.
 * The frame posts Hoffle's theme tokens in; when the session runs out the page
 * asks Hoffle for a fresh ticket.
 */
(function () {
  'use strict';
  const T = (window.Together = {});
  const params = new URLSearchParams(location.search);
  T.room = params.get('room') || '';
  T.mode = T.room.startsWith('r') ? 'reels' : 'watch';
  T.embedded = window.parent !== window;
  const ticket = new URLSearchParams(location.hash.slice(1)).get('ticket') || '';
  if (location.hash) history.replaceState(null, '', location.pathname + location.search);
  const storeKey = 'together:' + T.room;

  // ------------------------------------------------------------ DOM helpers
  /** h('div.class#id', {attrs}, children...) - text children are always text, never HTML. */
  T.h = function h(spec, attrs, ...children) {
    const m = /^([a-z0-9]+)?((?:[.#][\w-]+)*)$/i.exec(spec) || [];
    const el = document.createElement(m[1] || 'div');
    (m[2] || '').replace(/([.#])([\w-]+)/g, (_x, kind, name) => {
      if (kind === '.') el.classList.add(name); else el.id = name;
    });
    if (attrs) {
      for (const [key, value] of Object.entries(attrs)) {
        if (value == null || value === false) continue;
        if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
        else if (key === 'style' && typeof value === 'object') Object.assign(el.style, value);
        else if (key === 'dataset') Object.assign(el.dataset, value);
        else el.setAttribute(key, value === true ? '' : String(value));
      }
    }
    for (const child of children.flat(3)) {
      if (child == null || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  };
  const h = T.h;
  T.$ = (selector, root) => (root || document).querySelector(selector);

  const ICONS = {
    play: '<polygon points="6 3 20 12 6 21 6 3"/>',
    pause: '<rect x="14" y="4" width="4" height="16" rx="1"/><rect x="6" y="4" width="4" height="16" rx="1"/>',
    skip: '<polygon points="5 4 15 12 5 20 5 4"/><line x1="19" x2="19" y1="5" y2="19"/>',
    volume: '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>',
    mute: '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="22" x2="16" y1="9" y2="15"/><line x1="16" x2="22" y1="9" y2="15"/>',
    expand: '<path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/>',
    search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    list: '<path d="M12 12H3"/><path d="M16 6H3"/><path d="M12 18H3"/><path d="m16 12 5 3-5 3v-6Z"/>',
    users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    sliders: '<line x1="21" x2="14" y1="4" y2="4"/><line x1="10" x2="3" y1="4" y2="4"/><line x1="21" x2="12" y1="12" y2="12"/><line x1="8" x2="3" y1="12" y2="12"/><line x1="21" x2="16" y1="20" y2="20"/><line x1="12" x2="3" y1="20" y2="20"/><line x1="14" x2="14" y1="2" y2="6"/><line x1="8" x2="8" y1="10" y2="14"/><line x1="16" x2="16" y1="18" y2="22"/>',
    plus: '<path d="M5 12h14"/><path d="M12 5v14"/>',
    x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    trash: '<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>',
    grip: '<circle cx="9" cy="12" r="1"/><circle cx="9" cy="5" r="1"/><circle cx="9" cy="19" r="1"/><circle cx="15" cy="12" r="1"/><circle cx="15" cy="5" r="1"/><circle cx="15" cy="19" r="1"/>',
    heart: '<path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z"/>',
    less: '<path d="M17 14V2"/><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z"/>',
    up: '<path d="m18 15-6-6-6 6"/>',
    down: '<path d="m6 9 6 6 6-6"/>',
    smile: '<path d="M22 11v1a10 10 0 1 1-9-10"/><path d="M8 14s1.5 2 4 2 4-2 4-2"/><line x1="9" x2="9.01" y1="9" y2="9"/><line x1="15" x2="15.01" y1="9" y2="9"/><path d="M16 5h6"/><path d="M19 2v6"/>',
    sparkles: '<path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/>',
    globe: '<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>',
    bookmark: '<path d="m19 21-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v16z"/>',
    crown: '<path d="M11.562 3.266a.5.5 0 0 1 .876 0L15.39 8.87a1 1 0 0 0 1.516.294L21.183 5.5a.5.5 0 0 1 .798.519l-2.834 10.246a1 1 0 0 1-.956.734H5.81a1 1 0 0 1-.957-.734L2.02 6.02a.5.5 0 0 1 .798-.519l4.276 3.664a1 1 0 0 0 1.516-.294z"/><path d="M5 21h14"/>',
    shield: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>',
    loader: '<path d="M21 12a9 9 0 1 1-6.219-8.56"/>',
    userplus: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><line x1="19" x2="19" y1="8" y2="14"/><line x1="22" x2="16" y1="11" y2="11"/>',
    chevrons: '<path d="m7 6 5 5 5-5"/><path d="m7 13 5 5 5-5"/>',
    next: '<polyline points="15 10 20 15 15 20"/><path d="M4 4v7a4 4 0 0 0 4 4h12"/>',
    tv: '<rect width="20" height="15" x="2" y="7" rx="2" ry="2"/><polyline points="17 2 12 7 7 2"/>',
    pointer: '<path d="M4.037 4.688a.495.495 0 0 1 .651-.651l16 6.5a.5.5 0 0 1-.063.947l-6.124 1.58a2 2 0 0 0-1.438 1.435l-1.579 6.126a.5.5 0 0 1-.947.063z"/>',
    film: '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M7 3v18"/><path d="M3 7.5h4"/><path d="M3 12h18"/><path d="M3 16.5h4"/><path d="M17 3v18"/><path d="M17 7.5h4"/><path d="M17 16.5h4"/>',
    alert: '<circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y1="16" y2="16"/>',
    refresh: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>',
    lock: '<rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
  };
  T.icon = function icon(name, extra) {
    const span = document.createElement('span');
    span.innerHTML = `<svg class="i ${extra || ''}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ''}</svg>`;
    return span.firstChild;
  };

  T.fmt = function fmt(seconds) {
    if (seconds == null || !isFinite(seconds)) return '–:––';
    seconds = Math.max(0, Math.floor(seconds));
    const hrs = Math.floor(seconds / 3600), min = Math.floor((seconds % 3600) / 60), sec = seconds % 60;
    return (hrs ? hrs + ':' + String(min).padStart(2, '0') : min) + ':' + String(sec).padStart(2, '0');
  };
  T.views = function views(n) {
    if (!n) return '';
    if (n >= 1e9) return (n / 1e9).toFixed(1).replace(/\.0$/, '') + 'B views';
    if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M views';
    if (n >= 1e3) return Math.round(n / 1e3) + 'K views';
    return n + ' views';
  };

  const PALETTE = ['#7c5cbf', '#c2549a', '#d0703a', '#3f8f6b', '#3d78c4', '#9b6bdf', '#c94f4f', '#2f8f9d'];
  T.avatar = function avatar(person, size) {
    person = person || {};
    const el = h('span.avatar' + (size ? '.' + size : ''), { title: person.name || '' });
    let hash = 0;
    for (const ch of String(person.id || person.name || '?')) hash = (hash * 31 + ch.charCodeAt(0)) | 0;
    el.style.background = person.color || PALETTE[Math.abs(hash) % PALETTE.length];
    if (person.avatar_url) {
      const img = h('img', { alt: '', src: person.avatar_url, loading: 'lazy', referrerpolicy: 'no-referrer' });
      img.onerror = () => { img.remove(); el.textContent = person.avatar || (person.name || '?').slice(0, 1).toUpperCase(); };
      el.append(img);
    } else {
      el.textContent = person.avatar || (person.name || '?').slice(0, 1).toUpperCase();
    }
    return el;
  };
  T.stack = function stack(people, max, size) {
    const el = h('span.stack');
    (people || []).slice(0, max || 5).forEach((p) => el.append(T.avatar(p, size || 'small')));
    if (people && people.length > (max || 5)) el.append(h('span.more', null, '+' + (people.length - (max || 5))));
    return el;
  };

  // ------------------------------------------------------------ toasts and reactions
  const toastBox = h('div.toasts', { role: 'status', 'aria-live': 'polite' });
  document.addEventListener('DOMContentLoaded', () => document.body.append(toastBox));
  T.toast = function toast(text, opts) {
    opts = opts || {};
    const el = h('div.toast' + (opts.by ? '' : '.plain') + (opts.kind === 'error' ? '.error' : ''), null,
      opts.by ? T.avatar(opts.by, 'small') : null, h('span.ellipsis', null, text));
    toastBox.append(el);
    while (toastBox.childElementCount > 3) toastBox.firstChild.remove();
    setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 320); }, opts.ms || 2600);
  };
  T.float = function float(layer, emoji, by) {
    if (!layer) return;
    const el = h('div.floater', null, h('span.e', null, emoji), by ? h('span.n', null, by.name) : null);
    el.style.left = (55 + Math.random() * 35) + '%';
    el.style.setProperty('--drift', (Math.random() * 60 - 30) + 'px');
    layer.append(el);
    setTimeout(() => el.remove(), 2700);
  };
  T.reactionPicker = function reactionPicker(reactions, onPick) {
    return h('div.picker', { role: 'menu' }, (reactions || []).map((emoji) =>
      h('button', { type: 'button', title: emoji, 'aria-label': 'React ' + emoji, onclick: () => onPick(emoji) }, emoji)));
  };

  // ------------------------------------------------------------ theme from Hoffle
  const THEME_KEYS = {
    '--paper': '--bg', '--panel': '--panel', '--chat-bg': '--panel2', '--rail-bg': '--rail',
    '--ink': '--ink', '--muted': '--muted', '--faint': '--faint', '--line': '--line',
    '--lavender': '--accent', '--lavender-soft': '--accent-soft', '--accent-strong': '--accent-strong',
    '--on-accent': '--on-accent', '--coral': '--coral', '--mint': '--mint', '--ui-corners': '--radius',
    '--font-ui': '--font',
  };
  const loadedFonts = new Set(['Instrument Sans']);
  function applyTheme(data) {
    const root = document.documentElement;
    for (const [from, to] of Object.entries(THEME_KEYS)) {
      const value = data.vars && data.vars[from];
      // Only plain values: no way to inject rules through a token.
      if (typeof value === 'string' && value.length < 300 && !/[;{}<>]|url\(|expression|@import/i.test(value)) {
        if (value.trim()) root.style.setProperty(to, value.trim());
      }
    }
    if (data.scheme === 'light' || data.scheme === 'dark') root.style.colorScheme = data.scheme;
    root.dataset.scheme = data.scheme || 'dark';
    const font = String((data.vars && data.vars['--font-ui']) || '').split(',')[0].replace(/["']/g, '').trim();
    if (font && /^[\w ]{2,40}$/.test(font) && !loadedFonts.has(font)) {
      loadedFonts.add(font);
      document.head.append(h('link', { rel: 'stylesheet',
        href: 'https://fonts.googleapis.com/css2?family=' + encodeURIComponent(font).replace(/%20/g, '+') + ':wght@400;500;600;700&display=swap' }));
    }
  }
  window.addEventListener('message', (event) => {
    if (event.source !== window.parent || !event.data || typeof event.data !== 'object') return;
    if (event.data.type === 'hoffle-theme') applyTheme(event.data);
  });
  T.tellHoffle = function tellHoffle(type, extra) {
    if (T.embedded) window.parent.postMessage(Object.assign({ type: 'hoffle-together:' + type, room: T.room }, extra || {}), '*');
  };

  // ------------------------------------------------------------ session and socket
  let ws = null, retry = 0, handlers = null, pingTimer = null, closedForGood = false;
  let offset = 0, bestRtt = Infinity, rttSamples = [];
  T.me = null;
  T.serverNow = () => Date.now() + offset;
  T.connected = false;

  function dead(title, text, canRetry) {
    closedForGood = true;
    const box = h('div.dead', null, T.icon('alert'), h('h2', null, title), h('p', null, text),
      canRetry ? h('button.btn.primary', { type: 'button', onclick: () => location.reload() }, T.icon('refresh'), 'Try again') : null);
    document.body.append(box);
  }

  async function session() {
    let saved = '';
    try { saved = sessionStorage.getItem(storeKey) || ''; } catch (_e) { /* storage blocked */ }
    if (!ticket && !saved) return '';
    const response = await fetch('/watch/api/session', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ room: T.room, ticket, session: saved || undefined }),
    });
    if (!response.ok) return '';
    const data = await response.json();
    try { sessionStorage.setItem(storeKey, data.session); } catch (_e) { /* fine */ }
    return data.session;
  }

  function connect(sid) {
    const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    ws = new WebSocket(proto + location.host + '/watch/ws?room=' + encodeURIComponent(T.room));
    ws.onopen = () => {
      ws.send(JSON.stringify({ t: 'hello', session: sid }));
    };
    ws.onmessage = (event) => {
      let data;
      try { data = JSON.parse(event.data); } catch (_e) { return; }
      if (data.t === 'welcome') {
        retry = 0;
        T.connected = true;
        T.me = data.you;
        offset = data.s - Date.now();
        clearInterval(pingTimer);
        ping();
        pingTimer = setInterval(ping, 10000);
        document.documentElement.classList.remove('offline');
      } else if (data.t === 'pong') {
        const rtt = Date.now() - data.c;
        rttSamples.push({ rtt, off: data.s - (data.c + rtt / 2) });
        if (rttSamples.length > 8) rttSamples.shift();
        const best = rttSamples.reduce((a, b) => (b.rtt < a.rtt ? b : a));
        bestRtt = best.rtt;
        offset = best.off;
        return;
      } else if (data.t === 'state' && T.me) {
        const mine = (data.members || []).find((m) => m.id === T.me.id);
        if (mine) T.me.role = mine.role;
      } else if (data.t === 'notice') {
        if (data.kind !== 'join' && data.kind !== 'leave' || !data.by || data.by.id !== (T.me && T.me.id)) {
          T.toast(data.text, { by: data.by, kind: data.kind });
        }
      }
      handlers.onMessage(data);
    };
    ws.onclose = (event) => {
      T.connected = false;
      clearInterval(pingTimer);
      document.documentElement.classList.add('offline');
      if (closedForGood) return;
      if (event.code === 4001) {
        try { sessionStorage.removeItem(storeKey); } catch (_e) { /* fine */ }
        T.tellHoffle('expired');
        dead('This activity needs a refresh', 'Close it and open it again from the voice room.', false);
        return;
      }
      if (event.code === 4004) {
        dead('This room was closed', 'An administrator closed it.', false);
        return;
      }
      if (event.code === 4503) T.toast(event.reason || 'The server is busy', { kind: 'error' });
      const wait = Math.min(10000, 800 * 2 ** retry++);
      setTimeout(() => connect(sid), wait);
    };
  }
  function ping() { T.send({ t: 'ping', c: Date.now() }); }

  T.send = function send(message) {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify(message));
  };
  T.can = function can(policy, settings) {
    const role = T.me && T.me.role;
    const manager = role === 'host' || role === 'moderator';
    if (!policy) return manager;
    const value = (settings || {})[policy] || 'everyone';
    return value === 'everyone' || value === 'vote' || manager;
  };
  T.isManager = () => T.can(null);

  T.boot = async function boot(opts) {
    handlers = opts;
    T.tellHoffle('ready');
    if (!/^[wr]h_[\w-]{1,80}$/.test(T.room)) {
      dead('Open this from Hoffle', 'Watch Together and Reels Together are activities of a Hoffle voice room.', false);
      return;
    }
    let sid = '';
    try { sid = await session(); } catch (_e) {
      dead('Can’t reach the bot', 'The Music + Watch bot did not answer. It may be restarting.', true);
      return;
    }
    if (!sid) {
      T.tellHoffle('expired');
      dead('Open this from Hoffle', 'Start it from your voice room’s activities: this link has expired.', false);
      return;
    }
    connect(sid);
  };
})();
