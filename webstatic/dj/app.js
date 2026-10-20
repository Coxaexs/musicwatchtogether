// DJ booth UI: Easy / Moderate / Advanced modes over one audio engine.
(function () {
'use strict';
const DJ = window.DJ;
const { clamp, STEM_LABELS, STEM_COLORS } = DJ;

// ---------- small helpers ----------
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(3)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const $ = sel => document.querySelector(sel);
// replaceChildren() would print null/false as text; drop them instead.
const fill = (el, ...kids) => el.replaceChildren(...kids.flat(3).filter(k => k != null && k !== false));
const setArt = (el, url) => { el.style.backgroundImage = url ? `url("${url}")` : ''; el.classList.toggle('noart', !url); };
const fmt = s => {
  if (!Number.isFinite(s)) return '–:––';
  const neg = s < 0; s = Math.abs(s);
  const m = Math.floor(s / 60), r = Math.floor(s % 60);
  return `${neg ? '-' : ''}${m}:${String(r).padStart(2, '0')}`;
};
const store = {
  get(k, d) { try { const v = localStorage.getItem('dj_' + k); return v == null ? d : JSON.parse(v); } catch (_) { return d; } },
  set(k, v) { try { localStorage.setItem('dj_' + k, JSON.stringify(v)); } catch (_) {} },
};
let toastTimer;
function toast(text, kind) {
  const el = $('#toast');
  el.textContent = text; el.className = 'show ' + (kind || '');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.className = ''; }, 3400);
}

// ---------- API ----------
const BASE = location.pathname.replace(/dj\/?$/, '');
const params = new URLSearchParams(location.search);
const ROOM = params.get('guild_id') || '';
const LINK_TOKEN = new URLSearchParams(location.hash.slice(1)).get('token') || '';
const api = {
  async req(path, opts = {}) {
    const init = Object.assign({ headers: {} }, opts);
    if (opts.body && !(opts.body instanceof FormData)) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    const r = await fetch(BASE + path, init);
    if (r.status === 401) { showLogin(); throw new Error('Sign in first.'); }
    let data = {};
    try { data = await r.json(); } catch (_) {}
    if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
    return data;
  },
  media: (id, name) => `${BASE}api/dj/tracks/${id}/media/${name}`,
  saveTrack(id, patch) {
    return api.req(`api/dj/tracks/${id}`, { method: 'PATCH', body: patch })
      .then(r => { if (r.track) mergeTrack(r.track); return r; })
      .catch(e => toast(e.message, 'bad'));
  },
};

function showLogin() { $('#login').hidden = false; }
async function signIn(event) {
  event && event.preventDefault();
  const password = $('#password').value;
  const r = await fetch(BASE + 'api/session', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password }) });
  if (!r.ok) { $('#loginError').textContent = 'That password did not work.'; return; }
  $('#login').hidden = true;
  boot();
}
async function exchangeLink() {
  if (!LINK_TOKEN || !ROOM) return;
  await fetch(BASE + 'api/session', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: LINK_TOKEN, guild_id: ROOM }) });
  history.replaceState(null, '', location.pathname + '?guild_id=' + encodeURIComponent(ROOM));
}

// ---------- state ----------
const S = {
  mode: store.get('mode', 'easy'),
  caps: { stems: false, live: false, stem_names: [] },
  tracks: new Map(),
  crates: [],
  version: null,
  filter: { q: '', crate: 'all', sort: 'added', compat: 'off' },
  split: store.get('split', true),
  live: 'off',
  midi: { map: store.get('midi', {}), learning: false, target: null, access: null },
};
let engine, autodj;
const controls = new Map(); // MIDI-learnable controls

function mergeTrack(t) {
  const old = S.tracks.get(t.id);
  // Deck objects hold references; keep them current in place.
  if (old) { Object.assign(old, t); } else S.tracks.set(t.id, t);
  scheduleLibraryRender();
}

async function refreshLibrary() {
  try {
    const data = await api.req('api/dj/library' + (S.version != null ? `?since=${S.version}` : ''));
    if (data.unchanged) return;
    S.version = data.version;
    const seen = new Set();
    for (const t of data.tracks) { mergeTrack(t); seen.add(t.id); }
    for (const id of [...S.tracks.keys()]) if (!seen.has(id)) S.tracks.delete(id);
    S.crates = data.crates || [];
    scheduleLibraryRender();
    autodj && autodj.emit();
  } catch (e) { if (!/Sign in/.test(e.message)) console.warn(e); }
}

// ---------- controls ----------
function reg(id, control) {
  controls.set(id, control);
  if (control.el) {
    control.el.dataset.ctl = id;
    if (S.midi.map && Object.values(S.midi.map).includes(id)) control.el.classList.add('midi-bound');
  }
  return control.el;
}

function button(id, label, opts = {}) {
  const el = h('button', { class: 'btn ' + (opts.cls || ''), title: opts.title, type: 'button' }, label);
  const press = e => { if (S.midi.learning) return; opts.press && opts.press(e); };
  const release = e => { if (S.midi.learning) return; opts.release && opts.release(e); };
  el.addEventListener('pointerdown', e => { if (e.button !== 0) return; el.setPointerCapture && el.setPointerCapture(e.pointerId); if (opts.release) press(e); });
  el.addEventListener('pointerup', e => { if (opts.release) release(e); });
  el.addEventListener('pointercancel', e => { if (opts.release) release(e); });
  el.addEventListener('click', e => { if (S.midi.learning) { learnTarget(id, el); return; } if (!opts.release) press(e); });
  if (id) reg(id, { el, kind: 'button', press: () => press({}), release: () => release({}) });
  return el;
}

function knob(id, label, opts) {
  const min = opts.min ?? -1, max = opts.max ?? 1, def = opts.def ?? 0;
  let value = opts.value ?? def;
  const arc = h('div', { class: 'knob-dial' }, h('div', { class: 'knob-pointer' }));
  const valueEl = h('span', { class: 'knob-value' });
  const el = h('div', { class: 'knob ' + (opts.cls || ''), title: `${label} (double-click to reset)` }, arc, h('span', { class: 'knob-label' }, label), valueEl);
  const paint = () => {
    const f = (value - min) / (max - min);
    arc.style.setProperty('--angle', `${-135 + f * 270}deg`);
    arc.style.setProperty('--fill', `${f * 270}deg`);
    const from = (def - min) / (max - min);
    arc.style.setProperty('--from', `${from * 270}deg`);
    valueEl.textContent = opts.format ? opts.format(value) : '';
  };
  el.set = (v, silent) => { value = clamp(v, min, max); paint(); if (!silent) opts.onInput(value); };
  el.get = () => value;
  let drag = null;
  el.addEventListener('pointerdown', e => {
    if (S.midi.learning) { learnTarget(id, el); return; }
    drag = { y: e.clientY, v: value }; el.setPointerCapture(e.pointerId); el.classList.add('active');
  });
  el.addEventListener('pointermove', e => {
    if (!drag) return;
    const fine = e.shiftKey ? 0.2 : 1;
    el.set(drag.v + (drag.y - e.clientY) / 160 * (max - min) * fine);
  });
  const end = () => { drag = null; el.classList.remove('active'); };
  el.addEventListener('pointerup', end); el.addEventListener('pointercancel', end);
  el.addEventListener('dblclick', () => el.set(def));
  el.addEventListener('wheel', e => { e.preventDefault(); el.set(value - Math.sign(e.deltaY) * (max - min) / 50); }, { passive: false });
  paint();
  if (id) reg(id, { el, kind: 'range', set01: f => el.set(min + f * (max - min)) });
  return el;
}

function slider(id, opts) {
  const el = h('input', { type: 'range', class: 'slider ' + (opts.cls || ''), min: opts.min ?? 0, max: opts.max ?? 1, step: opts.step ?? 0.001, value: opts.value ?? 0, title: opts.title });
  el.addEventListener('input', () => opts.onInput(+el.value));
  el.addEventListener('dblclick', () => { if (opts.def != null) { el.value = opts.def; opts.onInput(opts.def); } });
  el.addEventListener('pointerdown', e => { if (S.midi.learning) { e.preventDefault(); learnTarget(id, el); } });
  if (id) reg(id, { el, kind: 'range', set01: f => { el.value = +el.min + f * (+el.max - +el.min); opts.onInput(+el.value); } });
  return el;
}

function select(options, value, onChange, cls) {
  const el = h('select', { class: cls || '' }, Object.entries(options).map(([k, v]) => h('option', { value: k, selected: k === String(value) }, v)));
  el.addEventListener('change', () => onChange(el.value));
  return el;
}

// ---------- waveforms ----------
class Waveform {
  constructor(deck, opts = {}) {
    this.deck = deck;
    this.color = deck.index === 0 ? [90, 169, 255] : [255, 122, 89];
    this.lanes = !!opts.lanes;
    this.zoomSeconds = store.get('zoom', 8);
    this.overview = h('canvas', { class: 'wf-overview', title: 'Click to jump' });
    this.zoom = h('canvas', { class: 'wf-zoom', title: 'Drag to scrub · wheel to zoom' });
    this.base = null;
    this.overview.addEventListener('pointerdown', e => {
      if (!deck.track) return;
      const r = this.overview.getBoundingClientRect();
      deck.seek((e.clientX - r.left) / r.width * deck.duration);
    });
    let scrub = null;
    this.zoom.addEventListener('pointerdown', e => {
      if (!deck.track) return;
      scrub = { x: e.clientX, pos: deck.position, wasPlaying: deck.playing };
      if (deck.playing) deck.pause();
      this.zoom.setPointerCapture(e.pointerId);
    });
    this.zoom.addEventListener('pointermove', e => {
      if (!scrub) return;
      const r = this.zoom.getBoundingClientRect();
      deck.seek(scrub.pos - (e.clientX - scrub.x) / r.width * this.zoomSeconds);
    });
    const end = () => { if (scrub && scrub.wasPlaying) deck.play(); scrub = null; };
    this.zoom.addEventListener('pointerup', end); this.zoom.addEventListener('pointercancel', end);
    this.zoom.addEventListener('wheel', e => {
      e.preventDefault();
      this.zoomSeconds = clamp(this.zoomSeconds * (e.deltaY > 0 ? 1.15 : 1 / 1.15), 2, 40);
      store.set('zoom', this.zoomSeconds);
    }, { passive: false });
  }

  fit(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const w = Math.round(canvas.clientWidth * dpr), hgt = Math.round(canvas.clientHeight * dpr);
    if (canvas.width !== w || canvas.height !== hgt) { canvas.width = w; canvas.height = hgt; return true; }
    return false;
  }

  bandColors(alpha = 1) {
    const [r, g, b] = this.color;
    return [`rgba(${r},${g},${b},${alpha})`, `rgba(${Math.min(255, r + 80)},${Math.min(255, g + 80)},${Math.min(255, b + 80)},${alpha * 0.9})`, `rgba(255,255,255,${alpha * 0.85})`];
  }

  drawBands(ctx, x0, w, H, getBin) {
    const colors = this.bandColors();
    const mid = H / 2;
    for (let x = 0; x < w; x++) {
      const v = getBin(x);
      if (!v) continue;
      for (let b = 0; b < 3; b++) {
        const amp = v[b] / 255 * mid * [1, 0.72, 0.42][b];
        if (amp < 0.5) continue;
        ctx.fillStyle = colors[b];
        ctx.fillRect(x0 + x, mid - amp, 1, amp * 2);
      }
    }
  }

  buildBase() {
    const deck = this.deck, c = this.overview;
    this.fit(c);
    const off = document.createElement('canvas');
    off.width = c.width; off.height = c.height;
    const ctx = off.getContext('2d');
    if (deck.peaks && deck.duration) {
      const peaks = deck.peaks, bins = peaks.length / 3;
      const per = bins / off.width;
      const tmp = [0, 0, 0];
      this.drawBands(ctx, 0, off.width, off.height, x => {
        const a = Math.floor(x * per), b = Math.max(a + 1, Math.floor((x + 1) * per));
        tmp[0] = tmp[1] = tmp[2] = 0;
        for (let i = a; i < b && i < bins; i++) for (let k = 0; k < 3; k++) if (peaks[i * 3 + k] > tmp[k]) tmp[k] = peaks[i * 3 + k];
        return tmp;
      });
    }
    this.base = off;
    this.basePeaks = deck.peaks;
  }

  draw() {
    const deck = this.deck;
    if (this.fit(this.overview) || !this.base || this.basePeaks !== deck.peaks) this.buildBase();
    const oc = this.overview.getContext('2d');
    const W = this.overview.width, H = this.overview.height;
    oc.clearRect(0, 0, W, H);
    oc.drawImage(this.base, 0, 0);
    if (deck.track && deck.duration) {
      const pos = deck.position;
      const px = pos / deck.duration * W;
      oc.fillStyle = 'rgba(6,8,14,.55)'; oc.fillRect(0, 0, px, H);
      if (deck.loop) { oc.fillStyle = 'rgba(80,255,170,.25)'; oc.fillRect(deck.loop.start / deck.duration * W, 0, Math.max(2, (deck.loop.end - deck.loop.start) / deck.duration * W), H); }
      deck.hotCues.forEach((c, i) => { if (c != null) { oc.fillStyle = HOTCUE_COLORS[i]; oc.fillRect(c / deck.duration * W - 1, 0, 2, H * 0.35); } });
      oc.fillStyle = '#fff'; oc.fillRect(px - 1, 0, 2, H);
      const a = deck.track.analysis || {};
      if (deck.duration - pos < 30 && deck.playing) { oc.fillStyle = `rgba(255,70,90,${0.25 + 0.2 * Math.sin(performance.now() / 120)})`; oc.fillRect(0, H - 4, W, 4); }
      if (a.audible_end) { oc.fillStyle = 'rgba(255,255,255,.3)'; oc.fillRect(a.audible_end / deck.duration * W, 0, 1, H); }
    }
    this.drawZoom();
  }

  drawZoom() {
    const deck = this.deck, c = this.zoom;
    this.fit(c);
    const ctx = c.getContext('2d');
    const W = c.width, H = c.height;
    ctx.clearRect(0, 0, W, H);
    if (!deck.track || !deck.peaks) return;
    const pos = deck.position;
    const span = this.zoomSeconds;
    const t0 = pos - span / 2;
    const toX = t => (t - t0) / span * W;
    // loop
    if (deck.loop) { ctx.fillStyle = 'rgba(80,255,170,.16)'; ctx.fillRect(toX(deck.loop.start), 0, toX(deck.loop.end) - toX(deck.loop.start), H); }
    const lanes = this.lanes && Object.keys(deck.activity).length ? deck.stemNames : null;
    if (lanes) {
      const laneH = H / lanes.length;
      lanes.forEach((name, li) => {
        const act = deck.activity[name]; if (!act) return;
        const muted = deck.stems[name] && deck.stems[name].muted;
        ctx.fillStyle = muted ? 'rgba(120,130,150,.25)' : STEM_COLORS[name];
        const mid = laneH * (li + 0.5);
        for (let x = 0; x < W; x++) {
          const t = t0 + x / W * span; if (t < 0) continue;
          const v = act[Math.floor(t * 25)] || 0;
          const amp = v * laneH * 0.46;
          if (amp > 0.4) ctx.fillRect(x, mid - amp, 1, amp * 2);
        }
        ctx.fillStyle = 'rgba(255,255,255,.55)'; ctx.font = `${Math.round(10 * (window.devicePixelRatio || 1))}px system-ui`;
        ctx.fillText(STEM_LABELS[name], 6, laneH * li + 13 * (window.devicePixelRatio || 1));
      });
    } else {
      const peaks = deck.peaks, bins = peaks.length / 3;
      const tmp = [0, 0, 0];
      this.drawBands(ctx, 0, W, H, x => {
        const t = t0 + x / W * span; if (t < 0) return null;
        const i = Math.floor(t * 50); if (i >= bins) return null;
        tmp[0] = peaks[i * 3]; tmp[1] = peaks[i * 3 + 1]; tmp[2] = peaks[i * 3 + 2];
        return tmp;
      });
    }
    // beat grid
    if (deck.bpm) {
      const first = Math.ceil(deck.beatAt(t0)), last = Math.floor(deck.beatAt(t0 + span));
      for (let b = first; b <= last; b++) {
        const x = toX(deck.posOfBeat(b));
        const bar = ((b % 4) + 4) % 4 === 0;
        ctx.fillStyle = bar ? 'rgba(255,255,255,.55)' : 'rgba(255,255,255,.16)';
        ctx.fillRect(Math.round(x), 0, bar ? 2 : 1, bar ? H : H * 0.18);
        if (!bar) ctx.fillRect(Math.round(x), H * 0.82, 1, H * 0.18);
      }
    }
    // cues
    const tri = (x, color) => { ctx.fillStyle = color; ctx.beginPath(); ctx.moveTo(x - 6, 0); ctx.lineTo(x + 6, 0); ctx.lineTo(x, 9); ctx.fill(); ctx.fillRect(x - 0.5, 0, 1, H); };
    tri(toX(deck.cuePoint), '#ffb020');
    deck.hotCues.forEach((cue, i) => { if (cue != null) tri(toX(cue), HOTCUE_COLORS[i]); });
    ctx.fillStyle = '#fff'; ctx.fillRect(W / 2 - 1, 0, 2, H);
  }
}
const HOTCUE_COLORS = ['#ff4d6d', '#ffb020', '#3ddc84', '#4dabff', '#b18cff', '#ff7ae0', '#2de2e6', '#f4f4f4'];

// ---------- deck view (Moderate / Advanced) ----------
class DeckView {
  constructor(deck, advanced) {
    this.deck = deck;
    this.adv = advanced;
    this.id = 'deck' + deck.name;
    this.wave = new Waveform(deck, { lanes: advanced });
    this.el = this.build();
    this.off = deck.on(what => this.update(what));
    this.update('loaded');
  }
  dispose() { this.off(); }

  build() {
    const d = this.deck, id = this.id;
    this.art = h('div', { class: 'deck-art' });
    this.title = h('div', { class: 'deck-title' }, 'Empty deck');
    this.artist = h('div', { class: 'deck-artist' }, 'Load a track from the library');
    this.bpmEl = h('span', { class: 'chip bpm' });
    this.keyEl = h('span', { class: 'chip key' });
    this.timeEl = h('span', { class: 'deck-time' });
    this.remainEl = h('span', { class: 'deck-remain' });
    this.progress = h('div', { class: 'deck-loading' });
    this.phaseEl = h('div', { class: 'phase-meter' }, ...Array.from({ length: 4 }, () => h('i')));

    this.playBtn = button(`${id}.play`, '▶', { cls: 'play big', title: 'Play / pause', press: () => d.toggle() });
    const cueBtn = button(`${id}.cue`, 'CUE', { cls: 'cue big', title: 'Cue (hold to preview)', press: () => d.cueDown(), release: () => d.cueUp() });
    this.syncBtn = button(`${id}.sync`, 'SYNC', { cls: 'sync', title: 'Match tempo and beats to the other deck (hold Shift for sync lock)', press: e => {
      const other = engine.decks[1 - d.index];
      if (e && e.shiftKey) { d.syncLock = !d.syncLock; this.update('tempo'); }
      if (!d.syncTo(other)) toast('Sync needs a beat grid on both decks.', 'bad');
    } });
    this.tempoSlider = slider(`${id}.tempo`, { min: -1, max: 1, value: 0, def: 0, cls: 'tempo', title: 'Tempo (double-click to reset)', onInput: v => d.setTempo(-v) });
    this.tempoVal = h('span', { class: 'tempo-val' });
    const tempoBox = [this.tempoSlider, this.tempoVal];

    const cues = h('div', { class: 'pads cues' }, Array.from({ length: this.adv ? 8 : 4 }, (_, i) => {
      const b = button(`${id}.hot${i + 1}`, String(i + 1), { cls: 'pad', title: 'Hot cue (Shift+click clears)', press: e => d.hotCue(i, e && e.shiftKey) });
      b.style.setProperty('--pad', HOTCUE_COLORS[i]);
      return b;
    }));
    this.cuePads = cues;
    const loopSizes = this.adv ? [0.25, 0.5, 1, 2, 4, 8, 16, 32] : [1, 4, 8, 16];
    this.loopPads = h('div', { class: 'pads loops' }, loopSizes.map(b => button(`${id}.loop${b}`, b < 1 ? `1/${1 / b}` : String(b), { cls: 'pad loop', title: `${b}-beat loop`, press: () => d.autoLoop(b) })));
    this.loopPads.querySelectorAll('button').forEach((el, i) => { el.dataset.beats = loopSizes[i]; });

    // Stems
    this.stemBox = h('div', { class: 'stems' });

    const body = [h('div', { class: 'transport' }, cueBtn, this.playBtn, this.syncBtn)];
    const blocks = [
      h('div', { class: 'block' }, h('div', { class: 'block-title' }, 'Hot cues'), cues),
      h('div', { class: 'block' }, h('div', { class: 'block-title' }, 'Loops'), this.loopPads,
        this.adv && h('div', { class: 'pads small' },
          button(`${id}.loopin`, 'IN', { cls: 'pad', press: () => d.loopIn() }),
          button(`${id}.loopout`, 'OUT', { cls: 'pad', press: () => d.loopOut() }),
          button(`${id}.loophalf`, '½', { cls: 'pad', press: () => d.resizeLoop(0.5) }),
          button(`${id}.loopdouble`, '×2', { cls: 'pad', press: () => d.resizeLoop(2) }),
          button(`${id}.loopexit`, 'EXIT', { cls: 'pad', press: () => d.setLoop(null) }))),
      h('div', { class: 'block stems-block' }, h('div', { class: 'block-title' }, 'Stems', h('span', { class: 'muted stems-note' })), this.stemBox),
    ];
    if (this.adv) {
      blocks.push(h('div', { class: 'block' }, h('div', { class: 'block-title' }, 'Loop roll · beat jump'),
        h('div', { class: 'pads small' }, [0.125, 0.25, 0.5, 1].map(b => button(`${id}.roll${b}`, b < 1 ? `1/${1 / b}` : '1', { cls: 'pad roll', title: `Roll 1/${1 / b} beat (hold)`, press: () => d.rollDown(b), release: () => d.rollUp() }))),
        h('div', { class: 'pads small' }, [-16, -4, -1, 1, 4, 16].map(b => button(`${id}.jump${b}`, (b > 0 ? '+' : '') + b, { cls: 'pad', title: `Jump ${b} beats`, press: () => d.beatJump(b) })))));
      blocks.push(this.buildFx());
    }
    const tempoTools = h('div', { class: 'tempo-tools' },
      this.adv && button(`${id}.nudgedown`, '◀◀', { cls: 'mini', title: 'Nudge slower (hold)', press: () => d.nudge(-1), release: () => d.nudge(0) }),
      this.adv && button(`${id}.nudgeup`, '▶▶', { cls: 'mini', title: 'Nudge faster (hold)', press: () => d.nudge(1), release: () => d.nudge(0) }),
      this.adv && select({ 0.08: '±8%', 0.16: '±16%', 0.5: '±50%' }, d.tempoRange, v => { d.setTempoRange(+v); this.update('tempo'); }, 'mini-select'),
      this.adv && (this.keyLockBtn = button(`${id}.keylock`, '🔒 Key', { cls: 'mini toggle', title: 'Key lock: keep the key when tempo changes', press: () => d.setKeyLock(!d.keyLock) })),
      this.adv && h('span', { class: 'keyshift' },
        button(`${id}.keydown`, '♭', { cls: 'mini', title: 'Key shift down a semitone', press: () => d.setKeyShift(d.keyShift - 1) }),
        this.keyShiftEl = h('span', { class: 'mini-val' }, '0'),
        button(`${id}.keyup`, '♯', { cls: 'mini', title: 'Key shift up a semitone', press: () => d.setKeyShift(d.keyShift + 1) })),
      this.adv && (this.quantBtn = button(`${id}.quant`, 'Q', { cls: 'mini toggle', title: 'Quantize cues and loops to the beat grid', press: () => { d.quantize = !d.quantize; this.update('tempo'); } })),
      this.adv && button(null, 'Grid', { cls: 'mini', title: 'Beat grid tools', press: () => d.track && openTrackDialog(d.track, d.index) }),
    );

    const el = h('section', { class: `deck deck-${d.name}` },
      h('div', { class: 'deck-head' },
        h('div', { class: 'deck-letter' }, d.name),
        this.art,
        h('div', { class: 'deck-meta' }, this.title, this.artist, h('div', { class: 'chips' }, this.bpmEl, this.keyEl, this.phaseEl)),
        h('div', { class: 'deck-clock' }, this.timeEl, this.remainEl)),
      h('div', { class: 'waves' }, this.wave.overview, this.wave.zoom, this.progress),
      h('div', { class: 'deck-body' },
        h('div', { class: 'deck-left' }, body, h('div', { class: 'tempo-box' }, tempoBox), tempoTools),
        h('div', { class: 'deck-pads' }, blocks)));
    return el;
  }

  buildFx() {
    const d = this.deck, fx = d.fx, id = this.id;
    this.fxType = select(Object.fromEntries(Object.entries(DJ.FX_TYPES).map(([k, v]) => [k, v.label])), fx.type, v => { fx.setType(v); this.update('fx'); });
    this.fxBeats = select(Object.fromEntries(DJ.FX_BEATS.map(b => [b, b < 1 ? (b === 0.75 ? '3/4' : `1/${1 / b}`) : String(b)])), fx.beats, v => { fx.beats = +v; fx.retime(); });
    this.fxTarget = select({ all: 'Whole deck' }, 'all', v => { fx.target = v; d.applyStemRouting(); });
    this.fxOn = button(`${id}.fxon`, 'FX ON', { cls: 'pad fxon', press: () => { fx.setOn(!fx.on); this.update('fx'); } });
    const wet = knob(`${id}.fxwet`, 'Wet', { min: 0, max: 1, def: 0.6, value: fx.wet, onInput: v => fx.setWet(v) });
    const param = knob(`${id}.fxparam`, 'Param', { min: 0, max: 1, def: 0.5, value: fx.param, onInput: v => { fx.param = v; fx.applyParam(); } });
    this.fxParamLabel = param.querySelector('.knob-label');
    return h('div', { class: 'block fx-block' }, h('div', { class: 'block-title' }, 'FX'),
      h('div', { class: 'fx-row' }, this.fxType, this.fxBeats, this.fxTarget),
      h('div', { class: 'fx-row' }, this.fxOn, wet, param,
        button(`${id}.echoout`, 'Echo out', { cls: 'pad', title: 'Echo the selected target out on the beat', press: () => d.echoOut(fx.target) }),
        button(`${id}.brake`, 'Brake', { cls: 'pad', title: 'Vinyl brake', press: () => d.brake() })));
  }

  renderStems() {
    const d = this.deck;
    this.stemBox.replaceChildren();
    const note = this.el.querySelector('.stems-note');
    const names = d.stemNames.filter(n => n !== 'full');
    if (!names.length) {
      const t = d.track;
      const s = t && t.stems;
      note.textContent = !t ? '' : s && s.status === 'running' ? ` · separating ${s.progress || 0}%` : s && s.status === 'queued' ? ' · queued' : S.caps.stems ? ' · not split yet' : ' · track only';
      if (t && s && !['running', 'queued'].includes(s.status) && S.caps.stems) {
        this.stemBox.append(button(null, 'Split into stems', { cls: 'pad wide', press: () => prepare(t.id, true) }));
      }
      return;
    }
    note.textContent = ` · ${names.length} stems`;
    for (const name of names) {
      const s = d.stems[name];
      const pad = button(`${this.id}.stem.${name}`, STEM_LABELS[name], { cls: 'pad stem' + (s.muted ? ' muted' : ''), title: `Mute/unmute ${STEM_LABELS[name]}`, press: () => d.toggleStem(name) });
      pad.style.setProperty('--pad', STEM_COLORS[name]);
      if (this.adv) {
        const vol = knob(`${this.id}.stemvol.${name}`, '', { min: 0, max: 1.5, def: 1, value: s.volume, cls: 'tiny', onInput: v => d.setStemVolume(name, v) });
        vol.style.setProperty('--knob', STEM_COLORS[name]);
        this.stemBox.append(h('div', { class: 'stem-strip' }, pad, vol));
      } else this.stemBox.append(pad);
    }
    if (this.adv) {
      this.stemBox.append(h('div', { class: 'stem-presets' }, ['full', 'acapella', 'instrumental', 'rhythm'].map(p =>
        button(null, { full: 'All', acapella: 'Acapella', instrumental: 'Instr.', rhythm: 'Rhythm' }[p], { cls: 'mini', press: () => d.setStemPreset(p) }))));
    }
    if (this.fxTarget) {
      this.fxTarget.replaceChildren(h('option', { value: 'all' }, 'Whole deck'), ...names.map(n => h('option', { value: n, selected: d.fx.target === n }, `${STEM_LABELS[n]} only`)));
    }
  }

  update(what) {
    const d = this.deck, t = d.track;
    if (what === 'loading') {
      this.el.classList.add('is-loading');
      this.title.textContent = t ? t.title : '';
      this.artist.textContent = 'Loading…';
      return;
    }
    if (what === 'loaded' || what === 'key' || what === 'track') {
      this.el.classList.remove('is-loading');
      this.title.textContent = t ? t.title : 'Empty deck';
      this.artist.textContent = t ? (t.artist || '') : 'Load a track from the library';
      setArt(this.art, t && t.thumbnail);
      this.wave.base = null;
      this.renderStems();
    }
    if (what === 'stems' && this.stemBox) {
      for (const [name, s] of Object.entries(d.stems)) {
        const pad = this.stemBox.querySelector(`[data-ctl="${this.id}.stem.${name}"]`);
        if (pad) pad.classList.toggle('muted', s.muted);
      }
    }
    if (what === 'fx' && this.fxOn) {
      this.fxOn.classList.toggle('on', d.fx.on);
      this.fxType.value = d.fx.type;
      this.fxParamLabel.textContent = DJ.FX_TYPES[d.fx.type].param;
    }
    this.playBtn.textContent = d.playing ? '❚❚' : '▶';
    this.playBtn.classList.toggle('on', d.playing);
    this.syncBtn.classList.toggle('on', d.syncLock);
    this.tempoSlider.value = -d.tempo;
    this.tempoVal.textContent = `${d.rate >= 1 ? '+' : ''}${((d.rate - 1) * 100).toFixed(1)}%`;
    this.bpmEl.textContent = d.effectiveBpm ? `${d.effectiveBpm.toFixed(1)} BPM` : '— BPM';
    const key = d.key;
    const other = engine.decks[1 - d.index];
    this.keyEl.textContent = key ? `${key} · ${DJ.keyName(key)}` : (t ? 'key…' : '—');
    const score = key && other.key ? DJ.harmonicScore(key, other.key) : -1;
    this.keyEl.dataset.match = score;
    if (this.keyLockBtn) this.keyLockBtn.classList.toggle('on', d.keyLock);
    if (this.keyShiftEl) this.keyShiftEl.textContent = d.keyShift > 0 ? `+${d.keyShift}` : String(d.keyShift);
    if (this.quantBtn) this.quantBtn.classList.toggle('on', d.quantize);
    d.hotCues.forEach((c, i) => { const p = this.cuePads.children[i]; if (p) p.classList.toggle('set', c != null); });
    const loopLen = d.loop ? (d.loop.end - d.loop.start) / d.beatLen() : 0;
    this.loopPads.querySelectorAll('button').forEach(b => b.classList.toggle('on', d.loop && Math.abs(+b.dataset.beats - loopLen) < 0.01));
    if (what === 'fx' || what === 'loaded') { if (this.fxOn) { this.fxOn.classList.toggle('on', d.fx.on); } }
  }

  frame() {
    const d = this.deck;
    this.wave.draw();
    if (!d.track) { this.timeEl.textContent = ''; this.remainEl.textContent = ''; return; }
    const pos = d.position;
    this.timeEl.textContent = fmt(pos);
    this.remainEl.textContent = fmt(-(d.duration - pos) / d.rate);
    if (d.bpm) {
      const beat = Math.floor(d.beatAt(pos));
      const idx = ((beat % 4) + 4) % 4;
      [...this.phaseEl.children].forEach((el, i) => el.classList.toggle('on', i === idx && d.playing));
    }
    if (d.syncLock && !d.loading && d.bpm) {
      const other = engine.decks[1 - d.index];
      const target = other.effectiveBpm;
      if (target) {
        const mult = [0.5, 1, 2].reduce((best, m) => Math.abs(target / (d.bpm * m) - 1) < Math.abs(target / (d.bpm * best) - 1) ? m : best, 1);
        const rate = target / (d.bpm * mult);
        if (Math.abs(rate - d.rate) > 0.0005 && Math.abs(rate - 1) <= 0.5) d.setRate(rate);
      }
    }
  }
}

// ---------- mixer ----------
class MixerView {
  constructor(advanced) {
    this.adv = advanced;
    this.meters = [];
    this.el = this.build();
  }
  channel(deck) {
    const id = 'mix' + deck.name;
    const fmtDb = v => v === 0 ? '0' : v < 0 ? `${Math.round(v * 30)}dB` : `+${(v * 6).toFixed(1)}dB`;
    const eq = ['high', 'mid', 'low'].map(band => {
      const k = knob(`${id}.eq${band}`, band[0].toUpperCase() + band.slice(1), { value: deck.eq[band], onInput: v => deck.setEq(band, v), format: fmtDb });
      if (!this.adv) return k;
      const kill = button(`${id}.kill${band}`, 'kill', { cls: 'mini toggle' + (deck.kills[band] ? ' on' : ''), press: () => { deck.setKill(band, !deck.kills[band]); kill.classList.toggle('on', deck.kills[band]); } });
      return h('div', { class: 'eq-row' }, k, kill);
    });
    const filter = knob(`${id}.filter`, 'Filter', { value: deck.filter, cls: 'filter', onInput: v => deck.setFilter(v), format: v => Math.abs(v) < 0.02 ? '' : v < 0 ? 'LPF' : 'HPF' });
    const fader = slider(`${id}.fader`, { min: 0, max: 1, value: deck.fader, def: 1, cls: 'fader', onInput: v => deck.setFader(v) });
    const meter = h('div', { class: 'meter' }, h('i'));
    this.meters.push([deck.meter, meter.firstChild]);
    const pfl = this.adv && button(`${id}.pfl`, '🎧', { cls: 'mini toggle', title: 'Pre-listen in headphones', press: () => { deck.setPfl(!deck.pflOn); pfl.classList.toggle('on', deck.pflOn); } });
    const trim = this.adv && knob(`${id}.trim`, 'Gain', { min: 0, max: 2, def: 1, value: 1, cls: 'tiny', onInput: v => deck.setTrim(v), format: v => `${(20 * Math.log10(Math.max(v, 0.001))).toFixed(1)}dB` });
    return h('div', { class: `channel ch-${deck.name}` }, h('div', { class: 'ch-name' }, deck.name), trim, eq, filter, pfl, h('div', { class: 'fader-wrap' }, meter, fader));
  }
  build() {
    const [a, b] = engine.decks;
    const xf = slider('mix.xfader', { min: -1, max: 1, value: engine.xfader, def: 0, cls: 'xfader', title: 'Crossfader', onInput: v => engine.setCrossfader(v) });
    this.xf = xf;
    const masterMeter = h('div', { class: 'meter master' }, h('i'), h('i'));
    this.masterMeter = masterMeter;
    const master = knob('mix.master', 'Master', { min: 0, max: 1.4, def: 1, value: store.get('master', 1), onInput: v => { engine.setMaster(v); store.set('master', v); } });
    engine.setMaster(store.get('master', 1));
    const extra = this.adv ? [
      knob('mix.cuemix', 'Cue mix', { min: 0, max: 1, def: 0.3, value: engine.cueMix, cls: 'tiny', onInput: v => engine.setCueMix(v) }),
      select({ dipless: 'Smooth', power: 'Equal power', sharp: 'Scratch' }, engine.xfCurve, v => engine.setCurve(v), 'mini-select'),
    ] : [];
    return h('section', { class: 'mixer' },
      h('div', { class: 'channels' }, this.channel(a), h('div', { class: 'master-col' }, master, masterMeter, extra), this.channel(b)),
      h('div', { class: 'xf-row' }, h('span', null, 'A'), xf, h('span', null, 'B')));
  }
  frame() {
    for (const [analyser, bar] of this.meters) bar.style.height = `${Math.min(100, engine.level(analyser) * 100)}%`;
    engine.meters.forEach((m, i) => { this.masterMeter.children[i].style.height = `${Math.min(100, engine.level(m) * 100)}%`; });
    if (document.activeElement !== this.xf) this.xf.value = engine.xfader;
  }
}

// ---------- Auto DJ panel ----------
function autoDjPanel(compact) {
  const el = h('section', { class: 'autodj ' + (compact ? 'compact' : '') });
  const render = () => {
    const out = autodj.outDeck;
    const planned = autodj.plannedId && S.tracks.get(autodj.plannedId);
    const toggle = button(null, autodj.enabled ? '⏸ Stop Auto DJ' : '▶ Start Auto DJ', { cls: 'primary', press: async () => { await engine.resume(); autodj.enabled ? autodj.stop() : autodj.start(); } });
    const mix = button('autodj.mixnow', '⏭ Mix now', { cls: '', press: () => autodj.mixNow() });
    mix.disabled = !autodj.enabled;
    const opts = h('div', { class: 'autodj-opts' },
      h('label', null, 'Style ', select(DJ.TRANSITION_STYLES, autodj.style, v => { autodj.style = v; store.set('style', v); })),
      h('label', null, 'Length ', select({ 4: '4 bars', 8: '8 bars', 16: '16 bars', 32: '32 bars' }, autodj.bars, v => { autodj.bars = +v; store.set('bars', +v); })),
      h('label', null, 'Energy ', select({ keep: 'Keep it', build: 'Build up', chill: 'Wind down' }, autodj.energy, v => { autodj.energy = v; store.set('energy', v); })),
      h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: autodj.smart, onchange: e => { autodj.smart = e.target.checked; store.set('smart', autodj.smart); } }), ' Smart continue'));
    const queue = h('ol', { class: 'queue' }, autodj.queue.map(id => {
      const t = S.tracks.get(id); if (!t) return null;
      return h('li', { class: t.audio === 'ready' ? '' : 'pending' },
        h('span', { class: 'q-title' }, t.title, h('small', null, t.artist || '')),
        h('span', { class: 'q-meta' }, trackChips(t)),
        h('span', { class: 'q-actions' },
          button(null, '↑', { cls: 'mini', press: () => autodj.move(id, -1) }),
          button(null, '↓', { cls: 'mini', press: () => autodj.move(id, 1) }),
          button(null, '✕', { cls: 'mini', press: () => autodj.dequeue(id) })));
    }));
    fill(el,
      h('div', { class: 'autodj-head' }, h('h3', null, 'Auto DJ'), toggle, mix),
      autodj.status ? h('div', { class: 'autodj-status' }, autodj.status) : null,
      planned ? h('div', { class: 'next-up' }, h('span', { class: 'label' }, 'Next'), h('b', null, planned.title), h('span', { class: 'muted' }, autodj.reason)) : null,
      opts,
      h('div', { class: 'queue-head' }, `Queue (${autodj.queue.length})`, autodj.queue.length ? button(null, 'Clear', { cls: 'mini', press: () => { autodj.queue = []; autodj.emit(); } }) : null),
      autodj.queue.length ? queue : h('p', { class: 'muted empty' }, 'Use “+ Queue” in the library. With Smart continue on, Auto DJ picks matching tracks by key, tempo and energy when the queue runs out.'),
    );
    void out;
  };
  autodj.on(render);
  render();
  return el;
}

function trackChips(t) {
  const a = t.analysis || {};
  return [
    a.bpm ? h('span', { class: 'chip' }, `${Math.round(a.bpm)}`) : null,
    t.key ? h('span', { class: 'chip key', 'data-match': matchScore(t) }, t.key.camelot) : null,
  ];
}
function matchScore(t) {
  const ref = S.filter.compat === 'A' ? engine.decks[0] : S.filter.compat === 'B' ? engine.decks[1] : autodj && autodj.outDeck;
  return ref && ref.key && t.key ? DJ.harmonicScore(ref.key, t.key.camelot) : -1;
}

// ---------- Easy mode ----------
class EasyView {
  constructor() {
    this.el = this.build();
    this.offs = engine.decks.map(d => d.on(() => this.update()));
    autodj.on(() => this.update());
    this.update();
  }
  dispose() { this.offs.forEach(off => off()); }
  build() {
    this.art = h('div', { class: 'easy-art' });
    this.title = h('h2', null, 'Ready when you are');
    this.artist = h('div', { class: 'easy-artist' }, 'Add some music below, queue it, and press Start Auto DJ.');
    this.chips = h('div', { class: 'chips' });
    this.bar = h('div', { class: 'easy-progress' }, h('i'));
    this.time = h('div', { class: 'easy-time' });
    this.reason = h('div', { class: 'easy-reason' });
    this.vibes = h('div', { class: 'vibes' });
    const deck = () => autodj.outDeck;
    const pads = h('div', { class: 'easy-pads' },
      button('easy.echovocals', '🎤 Echo out vocals', { cls: 'pad', title: 'The vocal leaves on an echo', press: () => deck().stems.vocals ? deck().echoOut('vocals') : toast('Needs stems for this track.', 'bad') }),
      button('easy.build', '📈 Build-up', { cls: 'pad', title: 'Filter riser + echo over 4 bars', press: () => autodj.buildUp(deck(), 4) }),
      button('easy.wash', '🌊 Wash', { cls: 'pad', title: 'Reverb swell for one bar', press: () => autodj.wash(deck()) }),
      button('easy.brake', '🛑 Brake', { cls: 'pad', title: 'Vinyl stop', press: () => deck().brake(1.6) }));
    this.waveHolder = h('div', { class: 'easy-wave' });
    const now = h('section', { class: 'easy-now' },
      this.art,
      h('div', { class: 'easy-info' }, h('div', { class: 'eyebrow' }, 'Now playing'), this.title, this.artist, this.chips, this.reason),
      this.waveHolder,
      h('div', { class: 'easy-progress-row' }, this.bar, this.time),
      h('div', { class: 'easy-section-title' }, 'Vibe'), this.vibes,
      h('div', { class: 'easy-section-title' }, 'Moves'), pads);
    return h('div', { class: 'easy' }, now, autoDjPanel(false));
  }
  update() {
    const d = autodj.outDeck, t = d.track;
    if (this.deck !== d) {
      this.deck = d;
      this.wave = new Waveform(d);
      this.waveHolder.replaceChildren(this.wave.overview);
    }
    if (this.wave && this.waveTrack !== t) { this.wave.base = null; this.waveTrack = t; }
    this.title.textContent = t ? t.title : 'Ready when you are';
    this.artist.textContent = t ? (t.artist || '') : 'Add some music below, queue it, and press Start Auto DJ.';
    setArt(this.art, t && t.thumbnail);
    fill(this.chips, 
      d.effectiveBpm ? h('span', { class: 'chip' }, `${d.effectiveBpm.toFixed(0)} BPM`) : null,
      d.key ? h('span', { class: 'chip key' }, `${d.key} · ${DJ.keyName(d.key)}`) : null,
      t && t.stems && t.stems.status === 'ready' ? h('span', { class: 'chip stems' }, `${t.stems.names.length} stems`) : null);
    this.reason.textContent = t && autodj.enabled && autodj.reason && !autodj.plannedId ? autodj.reason : '';
    const hasStems = d.stemNames.some(n => n !== 'full');
    const vibes = { full: 'Full mix', acapella: 'Acapella', instrumental: 'Instrumental', rhythm: 'Drums + bass', drums: 'Drums only', melody: 'Melody only' };
    const current = hasStems ? this.currentVibe(d) : 'full';
    fill(this.vibes, ...Object.entries(vibes).map(([k, label]) => {
      const b = button(`easy.vibe.${k}`, label, { cls: 'vibe' + (current === k ? ' on' : ''), press: () => hasStems ? d.setStemPreset(k) : toast(S.caps.stems ? 'This track has no stems yet — it is being split or needs “Split”.' : 'Stem separation is not installed on the server.', 'bad') });
      b.disabled = !hasStems && k !== 'full';
      return b;
    }));
  }
  currentVibe(d) {
    const on = Object.entries(d.stems).filter(([, s]) => !s.muted).map(([n]) => n).sort().join(',');
    const names = Object.keys(d.stems);
    const sets = {
      full: names, acapella: ['vocals'], instrumental: names.filter(n => n !== 'vocals'), rhythm: ['bass', 'drums'], drums: ['drums'],
      melody: names.filter(n => !['drums', 'bass', 'vocals'].includes(n)),
    };
    return Object.keys(sets).find(k => sets[k].slice().sort().join(',') === on) || null;
  }
  frame() {
    const d = autodj.outDeck;
    if (this.wave) this.wave.draw();
    if (!d.track) { this.bar.firstChild.style.width = '0'; this.time.textContent = ''; return; }
    const pos = d.position;
    this.bar.firstChild.style.width = `${pos / d.duration * 100}%`;
    this.time.textContent = `${fmt(pos)} / ${fmt(d.duration)}`;
  }
}

// ---------- library ----------
let libraryTimer;
function scheduleLibraryRender() { clearTimeout(libraryTimer); libraryTimer = setTimeout(renderLibrary, 60); }

function statusBadge(t) {
  const j = t.job || {}, s = t.stems || {};
  if (j.stage === 'failed') return h('span', { class: 'badge bad', title: j.error || '' }, 'Failed');
  if (j.stage === 'queued') return h('span', { class: 'badge' }, 'Queued');
  if (j.stage === 'fetching') return h('span', { class: 'badge busy' }, 'Downloading');
  if (j.stage === 'analysing') return h('span', { class: 'badge busy' }, 'Analysing');
  if (s.status === 'running') return h('span', { class: 'badge busy' }, `Stems ${s.progress || 0}%`);
  if (s.status === 'queued') return h('span', { class: 'badge' }, 'Stems queued');
  if (s.status === 'failed') return h('span', { class: 'badge bad', title: s.error || '' }, 'Stems failed');
  if (s.status === 'ready') return h('span', { class: 'badge good' }, `${s.names.length} stems`);
  if (s.status === 'partial') return h('span', { class: 'badge' }, `${s.names.length} stem`);
  if (t.audio === 'ready') return h('span', { class: 'badge' }, 'Track');
  return h('span', { class: 'badge' }, '—');
}

function filteredTracks() {
  const f = S.filter;
  let list = [...S.tracks.values()];
  if (f.crate !== 'all') {
    const crate = S.crates.find(c => c.id === f.crate);
    const ids = new Set(crate ? crate.track_ids : []);
    list = list.filter(t => ids.has(t.id));
  }
  if (f.q) {
    const q = f.q.toLowerCase();
    list = list.filter(t => `${t.title} ${t.artist || ''} ${t.key ? t.key.camelot : ''}`.toLowerCase().includes(q));
  }
  const ref = f.compat === 'A' ? engine.decks[0] : f.compat === 'B' ? engine.decks[1] : null;
  if (ref && ref.track) {
    list = list.filter(t => t.id !== ref.track.id && t.analysis && (
      (ref.key && t.key && DJ.harmonicScore(ref.key, t.key.camelot) >= 2) ||
      DJ.tempoDistance(ref.effectiveBpm, t.analysis.bpm) < 0.06));
  }
  const by = {
    added: (a, b) => b.added_at - a.added_at,
    title: (a, b) => a.title.localeCompare(b.title),
    bpm: (a, b) => ((a.analysis || {}).bpm || 999) - ((b.analysis || {}).bpm || 999),
    key: (a, b) => (a.key ? parseInt(a.key.camelot, 10) * 2 + (a.key.camelot.endsWith('B') ? 1 : 0) : 99) - (b.key ? parseInt(b.key.camelot, 10) * 2 + (b.key.camelot.endsWith('B') ? 1 : 0) : 99),
  }[f.sort];
  return list.sort(by);
}

function renderLibrary() {
  const body = $('#libRows'); if (!body) return;
  const list = filteredTracks();
  $('#libCount').textContent = `${list.length} of ${S.tracks.size} tracks`;
  const crateSel = $('#crateSel');
  const want = ['all', ...S.crates.map(c => c.id)].join(',');
  if (crateSel.dataset.ids !== want) {
    crateSel.dataset.ids = want;
    crateSel.replaceChildren(h('option', { value: 'all' }, 'All tracks'), ...S.crates.map(c => h('option', { value: c.id }, `${c.name} (${c.track_ids.length})`)));
    crateSel.value = S.filter.crate;
  }
  $('#crateTools').hidden = S.filter.crate === 'all';
  body.replaceChildren(...list.slice(0, 400).map(t => {
    const ready = t.audio === 'ready';
    const row = h('tr', { class: ready ? '' : 'not-ready' },
      h('td', { class: 'c-art' }, h('div', { class: 'thumb' + (t.thumbnail ? '' : ' noart'), style: t.thumbnail ? { backgroundImage: `url("${t.thumbnail}")` } : null })),
      h('td', { class: 'c-title' }, h('div', { class: 'tt' }, t.title), h('div', { class: 'ta' }, t.artist || '')),
      h('td', { class: 'c-bpm' }, t.analysis && t.analysis.bpm ? t.analysis.bpm.toFixed(1) : '–'),
      h('td', { class: 'c-key' }, t.key ? h('span', { class: 'chip key', 'data-match': matchScore(t) }, t.key.camelot) : '–'),
      h('td', { class: 'c-time' }, fmt(t.duration)),
      h('td', { class: 'c-status' }, statusBadge(t)),
      h('td', { class: 'c-actions' },
        button(null, 'A', { cls: 'mini load-a', title: 'Load on deck A', press: () => loadToDeck(0, t) }),
        button(null, 'B', { cls: 'mini load-b', title: 'Load on deck B', press: () => loadToDeck(1, t) }),
        button(null, '+ Queue', { cls: 'mini', title: 'Add to Auto DJ queue (Shift: play next)', press: e => { autodj.enqueue(t.id, e && e.shiftKey); if (!ready) prepare(t.id, S.split); toast(`Queued ${t.title}`); } }),
        button(null, '⋯', { cls: 'mini', title: 'Details', press: () => openTrackDialog(t) })));
    row.addEventListener('dblclick', () => loadToDeck(engine.decks[0].playing && !engine.decks[1].playing ? 1 : 0, t));
    return row;
  }));
  if (!list.length) body.append(h('tr', null, h('td', { colspan: 7, class: 'empty' }, S.tracks.size ? 'Nothing matches the filter.' : 'Your crate is empty. Search a song, paste a YouTube link or a Spotify playlist above, or upload audio files.')));
}

async function prepare(id, stems, resplit) {
  try { const r = await api.req(`api/dj/tracks/${id}/prepare`, { method: 'POST', body: { stems, resplit: !!resplit } }); mergeTrack(r.track); }
  catch (e) { toast(e.message, 'bad'); }
}

const pendingLoad = {};
async function loadToDeck(index, track) {
  const deck = engine.decks[index];
  if (track.audio !== 'ready') {
    toast('That track is still being prepared — it will be ready shortly.');
    if (!['queued', 'fetching', 'analysing'].includes((track.job || {}).stage)) prepare(track.id, S.split);
    return;
  }
  if (deck.playing && pendingLoad[index] !== track.id) {
    pendingLoad[index] = track.id;
    setTimeout(() => { if (pendingLoad[index] === track.id) delete pendingLoad[index]; }, 2500);
    toast(`Deck ${deck.name} is playing — press again to replace it.`);
    return;
  }
  delete pendingLoad[index];
  await engine.resume();
  try {
    await deck.load(track, api, f => {
      const bar = document.querySelector(`.deck-${deck.name} .deck-loading`);
      if (bar) bar.style.width = `${Math.round(f * 100)}%`;
    });
    if (autodj.enabled && index === 1 - autodj.current) autodj.plannedId = track.id;
  } catch (e) {
    toast(e.message, 'bad');
    deck.unload();
  }
}

function buildLibrary() {
  const addInput = h('input', { id: 'addInput', placeholder: 'Search a song, paste a YouTube link, or a Spotify playlist/album link…', autocomplete: 'off' });
  const add = async () => {
    const query = addInput.value.trim(); if (!query) return;
    addBtn.disabled = true; addBtn.textContent = 'Adding…';
    try {
      const r = await api.req('api/dj/library', { method: 'POST', body: { query, stems: S.split } });
      addInput.value = '';
      toast(r.crate ? `Imported ${r.added.length} tracks into “${r.crate.name}”` : 'Added — downloading and analysing now.', 'good');
      if (r.crate) { S.filter.crate = r.crate.id; }
      S.version = null; await refreshLibrary();
    } catch (e) { toast(e.message, 'bad'); }
    finally { addBtn.disabled = false; addBtn.textContent = 'Add'; }
  };
  addInput.addEventListener('keydown', e => { if (e.key === 'Enter') add(); });
  const addBtn = h('button', { class: 'btn primary', type: 'button', onclick: add }, 'Add');
  const fileInput = h('input', { type: 'file', multiple: true, accept: 'audio/*,.flac,.aiff,.aif,.m4a,.opus', hidden: true, onchange: () => upload(fileInput.files) });
  const split = h('label', { class: 'check', title: S.caps.stems ? 'Separate vocals, drums, bass and more for every added track' : 'Stem separation is not installed on the server' },
    h('input', { type: 'checkbox', checked: S.split && S.caps.stems, disabled: !S.caps.stems, onchange: e => { S.split = e.target.checked; store.set('split', S.split); } }), ' Split stems');
  const filter = h('input', { class: 'filter', placeholder: 'Filter…', value: S.filter.q, oninput: e => { S.filter.q = e.target.value; scheduleLibraryRender(); } });
  const crateSel = h('select', { id: 'crateSel', onchange: e => { S.filter.crate = e.target.value; scheduleLibraryRender(); } });
  const lib = h('section', { id: 'library' },
    h('div', { class: 'lib-add' }, addInput, addBtn,
      h('button', { class: 'btn', type: 'button', onclick: () => fileInput.click(), title: 'Upload your own audio files' }, '⬆ Upload'),
      h('button', { class: 'btn', type: 'button', onclick: openLocalDialog, title: 'Pick from the bot’s music folder' }, '📁 Local'),
      split, fileInput),
    h('div', { class: 'lib-tools' },
      filter, crateSel,
      h('span', { id: 'crateTools', hidden: true },
        button(null, 'Prepare all', { cls: 'mini', title: 'Download, analyse and split every track in this crate', press: async () => {
          try { const r = await api.req(`api/dj/crates/${S.filter.crate}/prepare`, { method: 'POST', body: { stems: S.split } }); toast(`Preparing ${r.count} tracks`, 'good'); S.version = null; refreshLibrary(); } catch (e) { toast(e.message, 'bad'); }
        } }),
        button(null, 'Queue all', { cls: 'mini', press: () => { const c = S.crates.find(x => x.id === S.filter.crate); if (c) { c.track_ids.forEach(id => autodj.enqueue(id)); toast(`Queued ${c.track_ids.length} tracks`); } } }),
        button(null, 'Remove crate', { cls: 'mini', press: async () => { await api.req(`api/dj/crates/${S.filter.crate}`, { method: 'DELETE' }); S.filter.crate = 'all'; S.version = null; refreshLibrary(); } })),
      h('label', null, 'Sort ', select({ added: 'Recently added', title: 'Title', bpm: 'BPM', key: 'Key (Camelot)' }, S.filter.sort, v => { S.filter.sort = v; scheduleLibraryRender(); })),
      h('label', null, 'Matches ', select({ off: 'Off', A: 'Deck A', B: 'Deck B' }, S.filter.compat, v => { S.filter.compat = v; scheduleLibraryRender(); })),
      h('span', { id: 'libCount', class: 'muted' })),
    h('div', { class: 'table-wrap' }, h('table', { class: 'lib' },
      h('thead', null, h('tr', null, h('th'), h('th', null, 'Track'), h('th', null, 'BPM'), h('th', null, 'Key'), h('th', null, 'Time'), h('th', null, 'Status'), h('th'))),
      h('tbody', { id: 'libRows' }))));
  lib.addEventListener('dragover', e => { e.preventDefault(); lib.classList.add('drop'); });
  lib.addEventListener('dragleave', () => lib.classList.remove('drop'));
  lib.addEventListener('drop', e => { e.preventDefault(); lib.classList.remove('drop'); if (e.dataTransfer.files.length) upload(e.dataTransfer.files); });
  return lib;
}

async function upload(files, trackId, stem) {
  if (!files || !files.length) return;
  const form = new FormData();
  form.append('stems', S.split ? '1' : '0');
  for (const f of files) form.append('file', f, f.name);
  const qs = trackId ? `?track=${trackId}&stem=${stem}` : '';
  toast(`Uploading ${files.length} file${files.length > 1 ? 's' : ''}…`);
  try {
    const r = await api.req('api/dj/upload' + qs, { method: 'POST', body: form });
    if (stem === 'auto') {
      const found = (r.stems || []).map(n => STEM_LABELS[n]).join(', ');
      const skipped = (r.skipped || []).length;
      toast(found ? `Stems saved: ${found}${skipped ? ` · ${skipped} file(s) not recognised` : ''}` : 'No stem names recognised in those file names — pick the stem type and upload one at a time.', found ? 'good' : 'bad');
    } else toast(trackId ? `${STEM_LABELS[stem]} stem saved` : `Uploaded ${r.added.length} — analysing now`, 'good');
    S.version = null; refreshLibrary();
  } catch (e) { toast(e.message, 'bad'); }
}

// ---------- dialogs ----------
function dialog(title, ...content) {
  const dlg = $('#dialog');
  dlg.replaceChildren(h('form', { method: 'dialog', class: 'dlg' },
    h('div', { class: 'dlg-head' }, h('h3', null, title), h('button', { class: 'btn mini', value: 'close' }, '✕')),
    ...content));
  if (!dlg.open) dlg.showModal();
  return dlg;
}

function openTrackDialog(t, deckIndex) {
  const a = t.analysis || {};
  const bpm = h('input', { type: 'number', step: '0.01', min: 40, max: 250, value: a.bpm || '', class: 'num' });
  const taps = [];
  const saveBpm = v => api.saveTrack(t.id, { bpm: v }).then(() => { bpm.value = v; engine.decks.forEach(d => d.track === t && d.emit('tempo')); });
  const deck = deckIndex != null ? engine.decks[deckIndex] : engine.decks.find(d => d.track && d.track.id === t.id);
  const stemName = select(Object.fromEntries((S.caps.upload_stem_names || DJ.STEM_ORDER).map(n => [n, STEM_LABELS[n]])), 'vocals', () => {});
  const stemFile = h('input', { type: 'file', accept: 'audio/*,.flac,.aiff,.m4a', hidden: true, onchange: () => upload(stemFile.files, t.id, stemName.value) });
  const packFiles = h('input', { type: 'file', multiple: true, accept: 'audio/*,.flac,.aiff,.aif,.m4a', hidden: true, onchange: () => upload(packFiles.files, t.id, 'auto') });
  dialog(t.title,
    h('p', { class: 'muted' }, [t.artist, a.duration ? fmt(a.duration) : null, a.energy != null ? `energy ${Math.round(a.energy * 100)}%` : null].filter(Boolean).join(' · ')),
    t.job && t.job.error ? h('p', { class: 'error' }, t.job.error) : null,
    t.stems && t.stems.error ? h('p', { class: 'error' }, 'Stems: ' + t.stems.error) : null,
    h('h4', null, 'Beat grid'),
    h('div', { class: 'row' }, 'BPM', bpm,
      button(null, 'Save', { cls: 'mini', press: () => saveBpm(+bpm.value || null) }),
      button(null, '×2', { cls: 'mini', press: () => a.bpm && saveBpm(+(a.bpm * 2).toFixed(3)) }),
      button(null, '÷2', { cls: 'mini', press: () => a.bpm && saveBpm(+(a.bpm / 2).toFixed(3)) }),
      button(null, 'Tap', { cls: 'mini', title: 'Tap along to the beat', press: () => {
        const now = performance.now();
        if (taps.length && now - taps[taps.length - 1] > 2000) taps.length = 0;
        taps.push(now);
        if (taps.length >= 4) bpm.value = (60000 / ((taps[taps.length - 1] - taps[0]) / (taps.length - 1))).toFixed(2);
      } })),
    deck ? h('div', { class: 'row' }, `Deck ${deck.name}:`,
      button(null, 'Set downbeat at playhead', { cls: 'mini', title: 'Move the grid so a bar starts at the current position', press: () => {
        const len = deck.beatLen(); const off = ((deck.position % len) + len) % len;
        api.saveTrack(t.id, { beat_offset: +off.toFixed(4) }).then(() => deck.emit('tempo'));
      } }),
      button(null, '◀ 10ms', { cls: 'mini', press: () => api.saveTrack(t.id, { beat_offset: +(((a.beat_offset || 0) - 0.01)).toFixed(4) }) }),
      button(null, '10ms ▶', { cls: 'mini', press: () => api.saveTrack(t.id, { beat_offset: +(((a.beat_offset || 0) + 0.01)).toFixed(4) }) })) : h('p', { class: 'muted' }, 'Load the track on a deck to adjust where the grid starts.'),
    h('h4', null, 'Stems'),
    h('div', { class: 'row' },
      S.caps.stems ? button(null, t.stems && t.stems.status === 'ready' ? 'Re-split' : 'Split into stems', { cls: 'mini', press: () => {
        prepare(t.id, true, t.stems && t.stems.status === 'ready');
      } }) : h('span', { class: 'muted' }, 'Automatic separation is not installed on the server.'),
      h('span', { class: 'muted' }, 'or upload one you own:'), stemName,
      button(null, 'Choose file…', { cls: 'mini', press: () => stemFile.click() }), stemFile),
    h('div', { class: 'row' },
      button(null, 'Upload stem pack…', { cls: 'mini', title: 'Pick all the stem files at once; names like “Song (Vocals).wav” or “03 DRUMS.flac” are recognised', press: () => packFiles.click() }),
      h('span', { class: 'muted' }, 'several files at once — vocals, drums, bass, keys, guitar and melody are detected from the file names'), packFiles),
    h('h4', null, 'Track'),
    h('div', { class: 'row' },
      t.page_url ? h('a', { href: t.page_url, target: '_blank', rel: 'noopener' }, 'Source page') : null,
      button(null, 'Retry download', { cls: 'mini', press: () => prepare(t.id, S.split) }),
      button(null, 'Delete from library', { cls: 'mini danger', press: async () => {
        await api.req(`api/dj/tracks/${t.id}`, { method: 'DELETE' });
        autodj.dequeue(t.id); $('#dialog').close(); S.version = null; refreshLibrary();
      } })));
}

async function openLocalDialog() {
  const list = h('div', { class: 'local-list' }, h('p', { class: 'muted' }, 'Loading…'));
  const q = h('input', { placeholder: 'Filter the music folder…', class: 'filter' });
  const load = async () => {
    try {
      const r = await api.req('api/dj/local?q=' + encodeURIComponent(q.value));
      list.replaceChildren(...(r.files.length ? r.files.map(f => h('div', { class: 'local-row' }, h('span', null, f.name),
        button(null, 'Add', { cls: 'mini', press: async () => {
          try { await api.req('api/dj/local', { method: 'POST', body: { path: f.path, stems: S.split } }); toast(`Added ${f.name}`, 'good'); S.version = null; refreshLibrary(); }
          catch (e) { toast(e.message, 'bad'); }
        } }))) : [h('p', { class: 'muted' }, 'No audio files found in the bot’s music folder.')]));
    } catch (e) { list.replaceChildren(h('p', { class: 'error' }, e.message)); }
  };
  let t; q.addEventListener('input', () => { clearTimeout(t); t = setTimeout(load, 250); });
  dialog('Local music folder', q, list);
  load();
}

async function openSettings() {
  let outputs = [];
  try {
    outputs = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'audiooutput');
  } catch (_) {}
  const named = outputs.some(o => o.label);
  const devOpts = { '': 'System default', ...Object.fromEntries(outputs.map(o => [o.deviceId, o.label || `Output ${o.deviceId.slice(0, 6)}`])) };
  const phonesOpts = { '': 'Off (no separate headphones)', ...Object.fromEntries(outputs.map(o => [o.deviceId, o.label || `Output ${o.deviceId.slice(0, 6)}`])) };
  const midiStatus = h('span', { class: 'muted' }, S.midi.access ? `${[...S.midi.access.inputs.values()].length} MIDI input(s)` : 'off');
  dialog('Audio & controllers',
    h('h4', null, 'Outputs'),
    !named ? h('p', { class: 'muted' }, button(null, 'Show device names', { cls: 'mini', press: async () => {
      try { const s = await navigator.mediaDevices.getUserMedia({ audio: true }); s.getTracks().forEach(t => t.stop()); openSettings(); } catch (_) { toast('Permission denied', 'bad'); }
    } }), ' (browsers hide names until you allow audio once)') : null,
    h('div', { class: 'row' }, 'Main (speakers)', select(devOpts, store.get('mainDev', ''), async v => { try { await engine.setMainDevice(v); store.set('mainDev', v); } catch (e) { toast(e.message, 'bad'); } })),
    h('div', { class: 'row' }, 'Headphones (cue)', select(phonesOpts, store.get('phonesDev', ''), async v => { try { await engine.setPhonesDevice(v); store.set('phonesDev', v); } catch (e) { toast(e.message, 'bad'); } })),
    h('label', { class: 'check row' }, h('input', { type: 'checkbox', checked: store.get('monitor', true), onchange: e => { engine.setMonitor(e.target.checked); store.set('monitor', e.target.checked); } }), ' Play the master mix on the main output'),
    h('p', { class: 'muted' }, 'Use 🎧 on a mixer channel (Advanced mode) to pre-listen a deck in your headphones before the crowd hears it.'),
    h('h4', null, 'MIDI controller'),
    h('div', { class: 'row' },
      button(null, S.midi.access ? 'MIDI on' : 'Enable MIDI', { cls: 'mini', press: async () => { await enableMidi(); openSettings(); } }), midiStatus,
      button(null, 'Learn mappings', { cls: 'mini', press: () => { $('#dialog').close(); startLearn(); } }),
      button(null, 'Clear mappings', { cls: 'mini', press: () => { S.midi.map = {}; store.set('midi', {}); document.querySelectorAll('.midi-bound').forEach(e => e.classList.remove('midi-bound')); toast('MIDI mappings cleared'); } })),
    h('h4', null, 'Keyboard'),
    h('div', { class: 'keys' },
      h('div', null, h('kbd', null, 'Q'), ' / ', h('kbd', null, 'P'), ' play A / B'),
      h('div', null, h('kbd', null, 'W'), ' / ', h('kbd', null, 'O'), ' cue A / B (hold)'),
      h('div', null, h('kbd', null, '1–4'), ' / ', h('kbd', null, '7–0'), ' hot cues A / B'),
      h('div', null, h('kbd', null, 'E'), ' / ', h('kbd', null, 'I'), ' sync A / B'),
      h('div', null, h('kbd', null, '← →'), ' crossfader'),
      h('div', null, h('kbd', null, 'Space'), ' Auto DJ: mix now')));
}

// ---------- line in ----------
let lineMeterTimer;
async function openLineIn() {
  let inputs = [];
  try { inputs = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'audioinput'); } catch (_) {}
  const named = inputs.some(d => d.label);
  const options = Object.fromEntries(inputs.map(d => [d.deviceId, d.label || `Input ${d.deviceId.slice(0, 6) || 'default'}`]));
  const current = engine.line ? engine.line.deviceId || '' : store.get('lineDev', '');
  const device = select(Object.keys(options).length ? options : { '': 'Default input' }, current, v => store.set('lineDev', v));
  const meter = h('div', { class: 'line-meter' }, h('i'));
  const status = h('span', { class: 'muted' }, engine.line ? `On: ${engine.line.label}` : 'Off');
  const toggle = button(null, engine.line ? 'Stop line in' : 'Start line in', { cls: engine.line ? '' : 'primary', press: async () => {
    if (engine.line) { engine.stopLineIn(); }
    else {
      try {
        await engine.resume();
        await engine.startLineIn(device.value || store.get('lineDev', ''));
        store.set('lineDev', engine.line.deviceId || device.value);
        toast(`Line in: ${engine.line.label}`, 'good');
      } catch (e) { toast(e.name === 'NotAllowedError' ? 'The browser was not allowed to use that input.' : e.message, 'bad'); }
    }
    updateLineButton();
    openLineIn();
  } });
  const gain = knob('line.gain', 'Gain', { min: 0, max: 2, def: 1, value: store.get('lineGain', 1), cls: 'tiny', onInput: v => { engine.setLineGain(v); store.set('lineGain', v); }, format: v => `${(20 * Math.log10(Math.max(v, 0.001))).toFixed(1)}dB` });
  const os = /Mac/.test(navigator.platform) ? 'mac' : /Win/.test(navigator.platform) ? 'win' : 'linux';
  const help = {
    mac: ['Install the free BlackHole 2ch driver.', 'In djay / Serato / rekordbox set the main output to “BlackHole 2ch” (or a Multi-Output Device with BlackHole + your speakers, to hear it too).', 'Pick “BlackHole 2ch” here and press Start.'],
    win: ['Install VB-Audio Virtual Cable (free), or enable “Stereo Mix” in Sound settings → Recording.', 'In djay / Serato / rekordbox / VirtualDJ set the master output to “CABLE Input” (use the app’s headphone output or Windows “Listen to this device” to hear it).', 'Pick “CABLE Output” (or Stereo Mix) here and press Start.'],
    linux: ['Create a loopback: pactl load-module module-null-sink sink_name=djmix sink_properties=device.description=DJ-Mix', 'Send the DJ app (or a browser tab, Mixxx…) to “DJ-Mix” in pavucontrol / your sound settings.', 'Pick “Monitor of DJ-Mix” here and press Start.'],
  }[os];
  dialog('Line in',
    h('p', { class: 'muted' }, 'Bring in audio from a DJ app (djay, Serato, rekordbox, VirtualDJ with TIDAL streaming and their stem features), a controller’s sound card or a turntable. It is mixed with the decks, shown on the meters, recorded by Rec and sent to the room by Go live.'),
    !named ? h('div', { class: 'row' }, button(null, 'Show input names', { cls: 'mini', press: async () => {
      try { const st = await navigator.mediaDevices.getUserMedia({ audio: true }); st.getTracks().forEach(t => t.stop()); openLineIn(); } catch (_) { toast('Permission denied', 'bad'); }
    } }), h('span', { class: 'muted' }, 'browsers hide device names until you allow audio once')) : null,
    h('div', { class: 'row' }, 'Input', device, toggle, status),
    h('div', { class: 'row' }, gain, meter,
      h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: store.get('lineMon', false), onchange: e => { engine.setLineMonitor(e.target.checked); store.set('lineMon', e.target.checked); } }), ' Also play it on this computer (leave off if you already hear the DJ app)')),
    h('h4', null, 'Setup on this computer'),
    h('ol', { class: 'help' }, help.map(step => h('li', null, step))),
    h('p', { class: 'muted' }, 'Then press 📡 Go live to send it to the voice room. Keep the decks paused (or mix them in) as you like.'));
  clearInterval(lineMeterTimer);
  lineMeterTimer = setInterval(() => {
    if (!$('#dialog').open) { clearInterval(lineMeterTimer); return; }
    meter.firstChild.style.width = `${Math.min(100, engine.level(engine.lineMeter) * 100)}%`;
  }, 60);
}
function updateLineButton() {
  const b = $('button.linein'); if (!b) return;
  b.classList.toggle('on', !!engine.line);
  b.textContent = engine.line ? '🎚 Line in · on' : '🎚 Line in';
}

// ---------- MIDI ----------
async function enableMidi() {
  if (S.midi.access) return;
  if (!navigator.requestMIDIAccess) { toast('This browser has no Web MIDI (try Chrome or Edge).', 'bad'); return; }
  try {
    S.midi.access = await navigator.requestMIDIAccess();
    const hook = () => S.midi.access.inputs.forEach(input => { input.onmidimessage = onMidi; });
    hook(); S.midi.access.onstatechange = hook;
    toast('MIDI controller connected', 'good');
  } catch (e) { toast('MIDI permission denied', 'bad'); }
}
function startLearn() {
  S.midi.learning = true; S.midi.target = null;
  document.body.classList.add('learning');
  $('#learnBar').hidden = false;
  enableMidi();
}
function stopLearn() {
  S.midi.learning = false; S.midi.target = null;
  document.body.classList.remove('learning');
  document.querySelectorAll('.learn-target').forEach(e => e.classList.remove('learn-target'));
  $('#learnBar').hidden = true;
}
function learnTarget(id, el) {
  if (!id) return;
  document.querySelectorAll('.learn-target').forEach(e => e.classList.remove('learn-target'));
  S.midi.target = id; el.classList.add('learn-target');
  $('#learnText').textContent = `Now move a knob or press a pad on your controller for “${id}”.`;
}
function onMidi(event) {
  const [status, num, value = 0] = event.data;
  const type = status & 0xf0, channel = status & 0x0f;
  const kind = type === 0xb0 ? 'c' : (type === 0x90 || type === 0x80) ? 'n' : null;
  if (!kind) return;
  const key = `${kind}:${channel}:${num}`;
  if (S.midi.learning && S.midi.target) {
    S.midi.map[key] = S.midi.target; store.set('midi', S.midi.map);
    const c = controls.get(S.midi.target); if (c && c.el) c.el.classList.add('midi-bound');
    toast(`Mapped ${key} → ${S.midi.target}`, 'good');
    S.midi.target = null;
    document.querySelectorAll('.learn-target').forEach(e => e.classList.remove('learn-target'));
    $('#learnText').textContent = 'Click another control to map, or press Done.';
    return;
  }
  const id = S.midi.map[key]; if (!id) return;
  const c = controls.get(id); if (!c) return;
  if (c.kind === 'range') c.set01(value / 127);
  else if (kind === 'n') (type === 0x90 && value > 0) ? c.press() : c.release();
  else value > 63 ? c.press() : c.release();
}

// ---------- keyboard ----------
const KEYMAP = {
  KeyQ: ['deckA.play'], KeyW: ['deckA.cue'], KeyE: ['deckA.sync'],
  KeyP: ['deckB.play'], KeyO: ['deckB.cue'], KeyI: ['deckB.sync'],
  Digit1: ['deckA.hot1'], Digit2: ['deckA.hot2'], Digit3: ['deckA.hot3'], Digit4: ['deckA.hot4'],
  Digit7: ['deckB.hot1'], Digit8: ['deckB.hot2'], Digit9: ['deckB.hot3'], Digit0: ['deckB.hot4'],
};
const deckKeys = {
  'deckA.play': () => engine.decks[0].toggle(), 'deckB.play': () => engine.decks[1].toggle(),
  'deckA.sync': () => engine.decks[0].syncTo(engine.decks[1]), 'deckB.sync': () => engine.decks[1].syncTo(engine.decks[0]),
};
document.addEventListener('keydown', e => {
  if (e.target.closest && e.target.closest('input, select, textarea')) return;
  if (e.repeat) return;
  if (e.code === 'Space') { e.preventDefault(); autodj && autodj.mixNow(); return; }
  if (e.code === 'ArrowLeft' || e.code === 'ArrowRight') { engine.setCrossfader(engine.xfader + (e.code === 'ArrowLeft' ? -0.1 : 0.1)); return; }
  const ids = KEYMAP[e.code]; if (!ids) return;
  const id = ids[0];
  const c = controls.get(id);
  if (c) c.press(); else if (deckKeys[id]) deckKeys[id]();
  else {
    const m = /^deck([AB])\.(hot(\d)|cue)$/.exec(id);
    if (m) { const d = engine.decks[m[1] === 'A' ? 0 : 1]; m[3] ? d.hotCue(+m[3] - 1, e.shiftKey) : d.cueDown(); }
  }
});
document.addEventListener('keyup', e => {
  const ids = KEYMAP[e.code]; if (!ids) return;
  const c = controls.get(ids[0]);
  if (c && c.release) c.release();
  else { const m = /^deck([AB])\.cue$/.exec(ids[0]); if (m) engine.decks[m[1] === 'A' ? 0 : 1].cueUp(); }
});

// ---------- header actions ----------
let recTimer;
async function toggleRecording(btn) {
  await engine.resume();
  if (engine.recorder) {
    clearInterval(recTimer);
    const blob = await engine.stopRecording();
    btn.classList.remove('on'); btn.textContent = '● Rec';
    if (blob) {
      const url = URL.createObjectURL(blob);
      const ext = blob.type.includes('mp4') ? 'm4a' : blob.type.includes('ogg') ? 'ogg' : 'webm';
      const a = h('a', { href: url, download: `dj-mix-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-')}.${ext}` });
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      toast('Mix saved to your downloads', 'good');
    }
    return;
  }
  engine.startRecording();
  btn.classList.add('on');
  recTimer = setInterval(() => { btn.textContent = `● ${fmt((Date.now() - engine.recorder.startedAt) / 1000)}`; }, 500);
}

async function toggleLive(btn) {
  await engine.resume();
  if (engine.live) { engine.stopLive(); setLive('off', btn); return; }
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = `${proto}//${location.host}${BASE}api/dj/live?room=${encodeURIComponent(ROOM)}`;
  setLive('connecting', btn);
  try {
    await engine.goLive(url, st => setLive(st, btn));
    if (store.get('pauseRoom', true)) api.req(`api/guilds/${encodeURIComponent(ROOM)}/action`, { method: 'POST', body: { action: 'pause' } }).catch(() => {});
    toast('You are live in the voice room 🎉', 'good');
  } catch (e) { setLive('off', btn); toast(e.message, 'bad'); }
}
function setLive(state, btn) {
  S.live = state;
  btn.classList.toggle('on', state === 'live' || state.startsWith('lag'));
  btn.textContent = state === 'off' || state === 'error' ? '📡 Go live' : state === 'connecting' ? '📡 Connecting…' : state.startsWith('lag') ? `📡 Live · ${state}` : '📡 Live';
}

// ---------- layout ----------
let views = [];
function renderStage() {
  views.forEach(v => v.dispose && v.dispose());
  views = [];
  controls.clear();
  const stage = $('#stage');
  stage.className = 'mode-' + S.mode;
  document.querySelectorAll('.modes button').forEach(b => b.classList.toggle('on', b.dataset.mode === S.mode));
  if (S.mode === 'easy') {
    const easy = new EasyView();
    views.push(easy);
    stage.replaceChildren(easy.el);
    return;
  }
  const adv = S.mode === 'advanced';
  const a = new DeckView(engine.decks[0], adv), b = new DeckView(engine.decks[1], adv);
  const mixer = new MixerView(adv);
  views.push(a, b, mixer);
  stage.replaceChildren(h('div', { class: 'booth' }, a.el, mixer.el, b.el), h('div', { class: 'side' }, autoDjPanel(true)));
  // Auto DJ panel listeners accumulate; rebuild clears them.
}

function frame() {
  for (const v of views) v.frame && v.frame();
  requestAnimationFrame(frame);
}

function buildShell() {
  const modes = h('nav', { class: 'modes' }, ['easy', 'moderate', 'advanced'].map(m =>
    h('button', { type: 'button', 'data-mode': m, onclick: () => { S.mode = m; store.set('mode', m); autodj.listeners.clear(); renderStage(); } },
      { easy: 'Easy', moderate: 'Moderate', advanced: 'Advanced' }[m])));
  const rec = h('button', { class: 'btn rec', type: 'button', title: 'Record the master mix to a file', onclick: () => toggleRecording(rec) }, '● Rec');
  const line = h('button', { class: 'btn linein', type: 'button', title: 'Bring in audio from a DJ app, controller or turntable', onclick: openLineIn }, '🎚 Line in');
  const liveOk = S.caps.live && ROOM.startsWith('huddle:');
  const live = h('button', { class: 'btn live', type: 'button', disabled: !liveOk,
    title: liveOk ? 'Broadcast the master mix into the Huddle voice room' : 'Open the booth from a Huddle voice room (/dj) to broadcast', onclick: () => toggleLive(live) }, '📡 Go live');
  $('#top').replaceChildren(
    h('div', { class: 'brand' }, h('span', { class: 'logo' }, '◉'), 'DJ Booth', ROOM ? h('span', { class: 'room' }, ROOM.startsWith('huddle:') ? 'Huddle room' : 'Server') : null),
    modes,
    h('div', { class: 'top-actions' },
      S.caps.stems ? h('span', { class: 'cap good', title: `Stem model: ${S.caps.stems_model}` }, `Stems: ${S.caps.stem_names.length}`) : h('span', { class: 'cap', title: 'Install Demucs on the server to split stems (see README)' }, 'Stems off'),
      line, rec, live,
      h('button', { class: 'btn', type: 'button', title: 'Audio outputs, headphones, MIDI', onclick: openSettings }, '⚙')));
}

async function boot() {
  try {
    S.caps = await api.req('api/dj/capabilities');
  } catch (e) { if (!/Sign in/.test(e.message)) toast(e.message, 'bad'); return; }
  if (!engine) {
    engine = new DJ.Engine(api);
    await engine.init();
    engine.setMonitor(store.get('monitor', true));
    engine.setLineGain(store.get('lineGain', 1));
    engine.setLineMonitor(store.get('lineMon', false));
    engine.onLineChange = updateLineButton;
    autodj = new DJ.AutoDJ(engine, () => S.tracks, (i, t) => loadToDeckForce(i, t));
    autodj.style = store.get('style', 'smart'); autodj.bars = store.get('bars', 16);
    autodj.energy = store.get('energy', 'keep'); autodj.smart = store.get('smart', true);
    engine.decks.forEach(d => d.on(what => { if (what === 'loaded' || what === 'key') scheduleLibraryRender(); }));
    document.addEventListener('pointerdown', () => engine.resume(), { once: true });
  }
  buildShell();
  updateLineButton();
  renderStage();
  $('#libraryHolder').replaceChildren(buildLibrary());
  await refreshLibrary();
  if (!boot.started) {
    boot.started = true;
    setInterval(refreshLibrary, 2500);
    requestAnimationFrame(frame);
  }
}

async function loadToDeckForce(index, track) {
  const deck = engine.decks[index];
  const ok = await deck.load(track, api, () => {});
  if (!ok) throw new Error('Load was replaced');
}

// Console/automation access: DJ.session().engine.decks[0].position etc.
DJ.session = () => ({ engine, autodj, state: S });

window.addEventListener('DOMContentLoaded', async () => {
  $('#loginForm').addEventListener('submit', signIn);
  $('#learnDone').addEventListener('click', stopLearn);
  await exchangeLink();
  boot();
});
})();
