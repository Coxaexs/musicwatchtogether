// Auto DJ: plans and performs beat-matched, stem-aware transitions.
(function () {
'use strict';
const DJ = window.DJ;

const STYLES = {
  smart: 'Smart (picks per pair)',
  stems: 'Stem swap',
  blend: 'Bass-swap blend',
  filter: 'Filter fade',
  echo: 'Echo out',
  cut: 'Cut on the bar',
};
DJ.TRANSITION_STYLES = STYLES;

function tempoDistance(bpmA, bpmB) {
  if (!bpmA || !bpmB) return Infinity;
  return Math.min(...[0.5, 1, 2].map(m => Math.abs(bpmB * m / bpmA - 1)));
}
DJ.tempoDistance = tempoDistance;

function ramp(param, from, to, t0, t1, exp) {
  param.setValueAtTime(from, t0);
  if (exp) param.exponentialRampToValueAtTime(Math.max(0.0001, to), t1);
  else param.linearRampToValueAtTime(to, t1);
}
function step(param, from, to, at) {
  param.setValueAtTime(from, Math.max(0, at - 0.04));
  param.linearRampToValueAtTime(to, at + 0.04);
}

function stemGroup(name) {
  if (name === 'vocals' || name === 'drums' || name === 'bass') return name;
  return 'other';
}

class AutoDJ {
  constructor(engine, getTracks, loadDeck) {
    this.engine = engine;
    this.getTracks = getTracks;
    this.loadDeck = loadDeck;
    this.enabled = false;
    this.queue = [];
    this.history = [];
    this.style = 'smart';
    this.bars = 16;
    this.smart = true;
    this.energy = 'keep';
    this.current = 0;
    this.transition = null;
    this.plannedId = null;
    this.reason = '';
    this.busy = false;
    this.cooldownUntil = 0;
    this.mixNowRequested = false;
    this.listeners = new Set();
    this.timer = setInterval(() => this.tick().catch(e => this.fail(e)), 150);
  }

  on(fn) { this.listeners.add(fn); }
  emit() { this.listeners.forEach(fn => fn(this)); }
  fail(error) { this.busy = false; console.warn('Auto DJ', error); this.status = error.message || String(error); this.emit(); }

  get decks() { return this.engine.decks; }
  get outDeck() { return this.decks[this.current]; }
  get inDeck() { return this.decks[1 - this.current]; }

  enqueue(id, front) {
    this.queue = this.queue.filter(x => x !== id);
    front ? this.queue.unshift(id) : this.queue.push(id);
    if (front) this.plannedId = null;
    this.emit();
  }
  dequeue(id) { this.queue = this.queue.filter(x => x !== id); if (this.plannedId === id) this.plannedId = null; this.emit(); }
  move(id, delta) {
    const i = this.queue.indexOf(id), j = i + delta;
    if (i < 0 || j < 0 || j >= this.queue.length) return;
    [this.queue[i], this.queue[j]] = [this.queue[j], this.queue[i]];
    this.plannedId = null; this.emit();
  }

  start() {
    this.enabled = true;
    this.status = '';
    const playing = this.decks.findIndex(d => d.playing);
    if (playing >= 0) this.current = playing;
    this.engine.setCrossfader(0);
    this.decks.forEach(d => d.setFader(1));
    this.kickoff = playing < 0; // nothing on air: start something now
    this.emit();
  }
  stop() {
    if (this.transition) this.finish(true);
    this.enabled = false;
    this.emit();
  }
  mixNow() { if (this.enabled) { this.mixNowRequested = true; this.emit(); } }

  ready(track) { return track && track.audio === 'ready' && track.analysis; }

  // Pick the next track: the queue first, then a smart choice.
  pickNext() {
    const tracks = this.getTracks();
    for (const id of this.queue) {
      const t = tracks.get(id);
      if (this.ready(t)) return { track: t, reason: 'Up next in your queue' };
    }
    if (!this.smart) return null;
    const ref = this.outDeck.track ? this.outDeck : null;
    const refKey = ref && ref.key;
    const refBpm = ref && ref.effectiveBpm;
    const refEnergy = ref && ref.track.analysis && ref.track.analysis.energy;
    const recent = new Set(this.history.slice(-40));
    this.decks.forEach(d => d.track && recent.add(d.track.id));
    let best = null;
    for (const t of tracks.values()) {
      if (!this.ready(t) || recent.has(t.id)) continue;
      let score = Math.random() * 0.6;
      const why = [];
      const harmonic = refKey && t.key ? DJ.harmonicScore(refKey, t.key.camelot) : 0;
      score += harmonic;
      if (harmonic === 3) why.push('same key'); else if (harmonic === 2) why.push('harmonic key'); else if (harmonic === 1) why.push('energy-shift key');
      const d = tempoDistance(refBpm, t.analysis.bpm);
      if (d < 0.08) { score += 2.5 * (1 - d / 0.08); why.push(d < 0.015 ? 'same tempo' : `${Math.round(d * 100)}% tempo away`); }
      if (refEnergy != null && t.analysis.energy != null) {
        const delta = t.analysis.energy - refEnergy;
        const want = { keep: 0, build: 0.12, chill: -0.12 }[this.energy];
        score += Math.max(0, 1 - Math.abs(delta - want) * 4);
        if (this.energy === 'build' && delta > 0.05) why.push('lifts the energy');
        if (this.energy === 'chill' && delta < -0.05) why.push('brings it down');
      }
      if (t.stems && t.stems.status === 'ready') score += 0.4;
      if (!best || score > best.score) best = { track: t, score, why };
    }
    if (!best) return null;
    const reason = best.why.length ? best.why.join(' · ') : 'fresh pick from your library';
    return { track: best.track, reason: reason[0].toUpperCase() + reason.slice(1) };
  }

  resolveStyle(out, inc) {
    const matched = tempoDistance(out.bpm, inc.bpm) <= 0.08;
    const stems = out.stems.vocals && out.stems.drums && inc.stems.vocals && inc.stems.drums;
    let style = this.style;
    if (style === 'smart') style = matched ? (stems ? 'stems' : 'blend') : 'echo';
    if (style === 'stems' && !stems) style = matched ? 'blend' : 'filter';
    return { style, matched };
  }

  async tick() {
    if (!this.enabled || this.busy) return;
    const ctx = this.engine.ctx;
    const out = this.outDeck, inc = this.inDeck;

    if (this.transition) {
      if (ctx.currentTime >= this.transition.endTime) this.finish();
      return;
    }

    if (this.kickoff) {
      this.kickoff = false;
      const queued = this.queue.some(id => this.ready(this.getTracks().get(id)));
      if (!queued && out.track) { out.play(); this.emit(); return; }
      if (queued) { out.pause(); out.unload(); }
    }

    // Nothing on air: start the first track.
    if (!out.track || (!out.playing && out.position >= out.duration - 0.1)) {
      if (inc.track && this.plannedId === inc.track.id && !inc.playing) {
        this.current = 1 - this.current;
        this.decks[this.current].play();
        this.afterSwap(out);
        return;
      }
      const next = this.pickNext();
      if (!next) { this.status = 'Add tracks to the queue (or turn on Smart continue).'; this.emit(); return; }
      this.busy = true;
      this.status = `Loading ${next.track.title}…`; this.emit();
      try { await this.loadDeck(this.current, next.track); } finally { this.busy = false; }
      this.reason = next.reason;
      this.queue = this.queue.filter(id => id !== next.track.id);
      const deck = this.outDeck;
      deck.setFader(1);
      deck.play(undefined, deck.firstBeat());
      this.status = ''; this.emit();
      return;
    }
    if (!out.playing) return; // the DJ paused it; wait

    // Preload the next track into the other deck.
    if (ctx.currentTime >= this.cooldownUntil && (!inc.track || inc.track.id !== this.plannedId) && !inc.playing) {
      const next = this.pickNext();
      if (!next) return;
      this.busy = true;
      try { await this.loadDeck(1 - this.current, next.track); } finally { this.busy = false; }
      this.plannedId = next.track.id;
      this.reason = next.reason;
      this.emit();
      return;
    }
    if (!inc.track || inc.track.id !== this.plannedId || inc.playing) return;

    const plan = this.plan(out, inc);
    if (this.mixNowRequested || out.position >= plan.mixOutPos - 0.3) {
      this.mixNowRequested = false;
      this.begin(out, inc, plan);
    }
  }

  plan(out, inc) {
    const { style, matched } = this.resolveStyle(out, inc);
    const beats = style === 'echo' || style === 'cut' ? 4 : this.bars * 4;
    const wallBeat = out.bpm ? out.beatSeconds() : 0.5;
    const seconds = out.bpm ? beats * wallBeat : (style === 'cut' ? 0.1 : Math.min(16, this.bars * 0.75));
    const a = out.track.analysis || {};
    const end = Math.min(out.duration, a.audible_end || out.duration);
    let mixOutPos = end - seconds * out.rate - (style === 'echo' ? 2 * out.beatLen() : 0);
    if (out.bpm) mixOutPos = out.posOfBeat(Math.floor(out.beatAt(mixOutPos) / 4) * 4);
    return { style, matched, seconds, mixOutPos: Math.max(1, mixOutPos) };
  }

  begin(out, inc, plan) {
    const ctx = this.engine.ctx;
    const now = ctx.currentTime;
    // Start on the outgoing track's next bar line (sample accurate).
    let dt = 0.08;
    if (out.bpm) {
      const pos = out.position;
      let barPos = out.posOfBeat(Math.ceil(out.beatAt(pos) / 4 - 1e-3) * 4);
      dt = (barPos - pos) / out.rate;
      if (dt < 0.06) dt += 4 * out.beatSeconds();
    }
    const T0 = now + dt;
    const D = plan.seconds;
    const T1 = T0 + D;
    const at = f => T0 + D * f;
    if (plan.matched) inc.syncTo(out, false);
    let inPos = inc.firstBeat();
    if (inc.bpm) inPos = inc.posOfBeat(Math.ceil(inc.beatAt(inPos) / 4 - 1e-3) * 4);

    for (const deck of [out, inc]) {
      this.resetAutomation(deck, now);
      for (const p of [deck.eqLow.gain, deck.lp.frequency, deck.hp.frequency]) {
        const v = p.value; p.cancelScheduledValues(now); p.setValueAtTime(v, now);
      }
    }
    const style = plan.style;
    if (style === 'blend') {
      inc.autoGain.gain.setValueAtTime(0, now);
      ramp(inc.autoGain.gain, 0, 1, T0, at(0.45));
      inc.eqLow.gain.setValueAtTime(-30, now);
      step(inc.eqLow.gain, -30, inc.eqLow.gain.value, at(0.5));
      step(out.eqLow.gain, out.eqLow.gain.value, -30, at(0.5));
      ramp(out.autoGain.gain, 1, 0, at(0.55), T1);
    } else if (style === 'stems') {
      const inSched = { drums: [[0, 1]], other: [[0.25, 0], [0.5, 1]], bass: [[0, 0], [0.5, 1, true]], vocals: [[0.72, 0], [0.9, 1]] };
      const outSched = { vocals: [[0.1, 1], [0.3, 0]], bass: [[0, 1], [0.5, 0, true]], other: [[0.5, 1], [0.8, 0]], drums: [[0.8, 1], [1, 0]] };
      this.scheduleStems(inc, inSched, at, now, 0);
      this.scheduleStems(out, outSched, at, now, 1);
    } else if (style === 'filter') {
      inc.autoGain.gain.setValueAtTime(0, now);
      ramp(inc.autoGain.gain, 0, 1, T0, at(0.5));
      inc.lp.frequency.setValueAtTime(300, now);
      ramp(inc.lp.frequency, 300, 22000, T0, at(0.65), true);
      ramp(out.hp.frequency, 10, 7000, at(0.3), T1, true);
      ramp(out.autoGain.gain, 1, 0, at(0.7), T1);
    } else if (style === 'echo') {
      setTimeout(() => out.echoOut('all', 0.75), Math.max(0, (T0 - now) * 1000 - 10));
    } else if (style === 'cut') {
      out.autoGain.gain.setValueAtTime(1, now);
      out.autoGain.gain.setValueAtTime(0, T0);
    }
    inc.setFader(1);
    inc.play(T0, inPos);
    this.transition = { out, inc, style, startAt: T0, endTime: T1 + 0.15 };
    this.status = `Mixing · ${STYLES[style]}`;
    this.emit();
  }

  scheduleStems(deck, schedule, at, now, initial) {
    for (const [name, stem] of Object.entries(deck.stems)) {
      const points = schedule[stemGroup(name)] || [];
      const param = stem.auto.gain;
      let prev = points.length ? points[0][1] : initial;
      param.cancelScheduledValues(now);
      param.setValueAtTime(prev, now);
      for (const [f, v, hard] of points) {
        if (hard) {
          param.setValueAtTime(prev, at(f) - 0.03);
          param.linearRampToValueAtTime(v, at(f) + 0.03);
        } else {
          param.linearRampToValueAtTime(v, at(f));
        }
        prev = v;
      }
    }
  }

  resetAutomation(deck, now) {
    const params = [deck.autoGain.gain, ...Object.values(deck.stems).map(s => s.auto.gain)];
    for (const p of params) { p.cancelScheduledValues(now); p.setValueAtTime(1, now); }
  }

  restore(deck) {
    const now = this.engine.ctx.currentTime;
    this.resetAutomation(deck, now);
    deck.setEq('low', deck.eq.low); deck.setEq('mid', deck.eq.mid); deck.setEq('high', deck.eq.high);
    deck.setFilter(deck.filter);
  }

  finish(abort) {
    const t = this.transition; if (!t) return;
    this.transition = null;
    if (t.style !== 'echo' || abort) t.out.pause();
    this.restore(t.out);
    this.restore(t.inc);
    this.current = this.decks.indexOf(t.inc);
    this.afterSwap(t.out);
    this.cooldownUntil = this.engine.ctx.currentTime + (t.style === 'echo' ? 9 * t.out.beatSeconds() : 1);
  }

  afterSwap(previous) {
    if (previous.track) this.history.push(previous.track.id);
    const now = this.outDeck.track;
    if (now) this.queue = this.queue.filter(id => id !== now.id);
    this.plannedId = null;
    this.status = '';
    this.emit();
  }

  // ---- performance pads (also used by Easy mode) ----
  buildUp(deck, bars = 4) {
    if (!deck.playing) return;
    const ctx = this.engine.ctx;
    const now = ctx.currentTime;
    const D = bars * 4 * deck.beatSeconds();
    deck.hp.frequency.cancelScheduledValues(now);
    ramp(deck.hp.frequency, 10, 1800, now, now + D, true);
    deck.fx.setType('echo'); deck.fx.beats = 0.5; deck.fx.param = 0.55; deck.fx.wet = 0.5; deck.fx.target = 'all';
    deck.fx.retime(); deck.fx.applyParam(); deck.fx.setOn(true);
    deck.emit('fx');
    clearTimeout(deck.buildTimer);
    deck.buildTimer = setTimeout(() => {
      deck.fx.setOn(false);
      deck.setFilter(deck.filter);
      deck.emit('fx');
    }, D * 1000);
  }
  wash(deck) {
    deck.fx.setType('reverb'); deck.fx.wet = 0.75; deck.fx.param = 0.5; deck.fx.target = 'all';
    deck.fx.applyParam(); deck.fx.setOn(true); deck.emit('fx');
    clearTimeout(deck.washTimer);
    deck.washTimer = setTimeout(() => { deck.fx.setOn(false); deck.emit('fx'); }, deck.beatSeconds() * 4 * 1000);
  }
}

DJ.AutoDJ = AutoDJ;
})();
