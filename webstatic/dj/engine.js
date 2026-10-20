// DJ booth audio engine: two stem-aware decks, mixer, FX and master bus.
(function () {
'use strict';
const DJ = window.DJ = window.DJ || {};

const STEM_ORDER = ['vocals', 'drums', 'bass', 'guitar', 'piano', 'other'];
const STEM_LABELS = { vocals: 'Vocals', drums: 'Drums', bass: 'Bass', guitar: 'Guitar', piano: 'Keys', other: 'Melody', full: 'Track' };
const STEM_COLORS = { vocals: '#ff6fae', drums: '#ffc94d', bass: '#4dd2ff', guitar: '#ff9a4d', piano: '#b48cff', other: '#6dffa8', full: '#9fb4ff' };
DJ.STEM_ORDER = STEM_ORDER;
DJ.STEM_LABELS = STEM_LABELS;
DJ.STEM_COLORS = STEM_COLORS;

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
DJ.clamp = clamp;

// ---------- musical keys ----------
const PITCHES = ['C', 'C♯', 'D', 'E♭', 'E', 'F', 'F♯', 'G', 'A♭', 'A', 'B♭', 'B'];
const CAMELOT_MAJOR = [8, 3, 10, 5, 12, 7, 2, 9, 4, 11, 6, 1];
const CAMELOT_MINOR = [5, 12, 7, 2, 9, 4, 11, 6, 1, 8, 3, 10];

function camelotOf(pc, minor) {
  return `${(minor ? CAMELOT_MINOR : CAMELOT_MAJOR)[((pc % 12) + 12) % 12]}${minor ? 'A' : 'B'}`;
}
function keyFromCamelot(code) {
  const m = /^(\d+)([AB])$/.exec(code || '');
  if (!m) return null;
  const minor = m[2] === 'A';
  const pc = (minor ? CAMELOT_MINOR : CAMELOT_MAJOR).indexOf(+m[1]);
  return { pc, minor };
}
// Shift a Camelot key by semitones (one semitone = 7 steps on the wheel).
DJ.shiftCamelot = function (code, semitones) {
  const key = keyFromCamelot(code);
  if (!key || !Number.isFinite(semitones)) return code;
  const st = Math.round(semitones);
  if (!st) return code;
  return camelotOf(key.pc + st, key.minor);
};
DJ.keyName = function (code) {
  const key = keyFromCamelot(code);
  return key ? PITCHES[key.pc] + (key.minor ? 'm' : '') : '';
};
// 0 = clash, 1 = energy-shift compatible, 2 = harmonic, 3 = same key
DJ.harmonicScore = function (a, b) {
  const ka = /^(\d+)([AB])$/.exec(a || ''), kb = /^(\d+)([AB])$/.exec(b || '');
  if (!ka || !kb) return 0;
  const na = +ka[1], nb = +kb[1];
  const diff = Math.min((na - nb + 12) % 12, (nb - na + 12) % 12);
  if (ka[2] === kb[2]) {
    if (diff === 0) return 3;
    if (diff === 1) return 2;
    if (diff === 2 || diff === 7) return 1;
    return 0;
  }
  return diff === 0 ? 2 : 0;
};

// Krumhansl-Kessler key profiles.
const MAJOR_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88];
const MINOR_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.6, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17];

function fft(re, im) {
  const n = re.length;
  for (let i = 1, j = 0; i < n; i++) {
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) { [re[i], re[j]] = [re[j], re[i]]; [im[i], im[j]] = [im[j], im[i]]; }
  }
  for (let len = 2; len <= n; len <<= 1) {
    const ang = -2 * Math.PI / len;
    const wr = Math.cos(ang), wi = Math.sin(ang);
    for (let i = 0; i < n; i += len) {
      let cr = 1, ci = 0;
      for (let k = 0; k < len / 2; k++) {
        const a = i + k, b = a + len / 2;
        const tr = re[b] * cr - im[b] * ci, ti = re[b] * ci + im[b] * cr;
        re[b] = re[a] - tr; im[b] = im[a] - ti;
        re[a] += tr; im[a] += ti;
        const ncr = cr * wr - ci * wi;
        ci = cr * wi + ci * wr; cr = ncr;
      }
    }
  }
}

function correlate(a, b) {
  const ma = a.reduce((s, v) => s + v, 0) / a.length;
  const mb = b.reduce((s, v) => s + v, 0) / b.length;
  let num = 0, da = 0, db = 0;
  for (let i = 0; i < a.length; i++) {
    num += (a[i] - ma) * (b[i] - mb);
    da += (a[i] - ma) ** 2; db += (b[i] - mb) ** 2;
  }
  return num / Math.sqrt(da * db || 1);
}

// Detect a track's key from its decoded audio (chromagram + key profiles).
DJ.detectKey = async function (buffers) {
  const list = Object.values(buffers);
  if (!list.length) return null;
  const rate = 11025;
  const duration = Math.max(...list.map(b => b.duration));
  const offline = new OfflineAudioContext(1, Math.ceil(duration * rate), rate);
  for (const [name, buffer] of Object.entries(buffers)) {
    if (name === 'drums') continue; // drums smear the chroma
    const src = offline.createBufferSource();
    src.buffer = buffer;
    src.connect(offline.destination);
    src.start();
  }
  const mono = (await offline.startRendering()).getChannelData(0);
  const N = 8192;
  const frames = Math.min(240, Math.floor(mono.length / N));
  if (frames < 8) return null;
  const hop = Math.floor((mono.length - N) / frames);
  const chroma = new Float64Array(12);
  const re = new Float64Array(N), im = new Float64Array(N);
  const hann = new Float64Array(N).map((_, i) => 0.5 - 0.5 * Math.cos(2 * Math.PI * i / (N - 1)));
  const lo = Math.floor(60 * N / rate), hi = Math.ceil(2000 * N / rate);
  const pcOfBin = new Int8Array(hi + 1);
  for (let k = lo; k <= hi; k++) {
    const f = k * rate / N;
    pcOfBin[k] = ((Math.round(12 * Math.log2(f / 440) + 69) % 12) + 12) % 12;
  }
  for (let f = 0; f < frames; f++) {
    const start = f * hop;
    for (let i = 0; i < N; i++) { re[i] = mono[start + i] * hann[i]; im[i] = 0; }
    fft(re, im);
    const frame = new Float64Array(12);
    let total = 0;
    for (let k = lo; k <= hi; k++) {
      const mag = Math.sqrt(re[k] * re[k] + im[k] * im[k]);
      frame[pcOfBin[k]] += mag; total += mag;
    }
    if (total > 0) for (let p = 0; p < 12; p++) chroma[p] += Math.sqrt(frame[p] / total);
    if (f % 24 === 23) await new Promise(r => setTimeout(r, 0)); // stay responsive
  }
  let best = null;
  for (let pc = 0; pc < 12; pc++) {
    for (const minor of [false, true]) {
      const profile = minor ? MINOR_PROFILE : MAJOR_PROFILE;
      const rotated = Array.from({ length: 12 }, (_, i) => profile[(i - pc + 12) % 12]);
      const score = correlate(Array.from(chroma), rotated);
      if (!best || score > best.score) best = { pc, minor, score };
    }
  }
  return best && { camelot: camelotOf(best.pc, best.minor), name: PITCHES[best.pc] + (best.minor ? 'm' : '') };
};

// Fallback waveform (single band) when the server has no peaks yet.
function peaksFromBuffer(buffer, perSecond = 50) {
  const data = buffer.getChannelData(0);
  const step = Math.floor(buffer.sampleRate / perSecond);
  const bins = Math.floor(data.length / step);
  const out = new Uint8Array(bins * 3);
  let loudest = 1e-6;
  const levels = new Float32Array(bins);
  for (let b = 0; b < bins; b++) {
    let peak = 0;
    for (let i = b * step, end = i + step; i < end; i += 4) { const v = Math.abs(data[i]); if (v > peak) peak = v; }
    levels[b] = peak; if (peak > loudest) loudest = peak;
  }
  for (let b = 0; b < bins; b++) {
    const v = Math.min(255, Math.round(255 * (levels[b] / loudest) ** 0.7));
    out[b * 3] = v * 0.8; out[b * 3 + 1] = v * 0.6; out[b * 3 + 2] = v * 0.4;
  }
  return out;
}
DJ.stemActivity = function (buffer, perSecond = 25) {
  const data = buffer.getChannelData(0);
  const step = Math.floor(buffer.sampleRate / perSecond);
  const bins = Math.floor(data.length / step);
  const out = new Float32Array(bins);
  let loudest = 1e-6;
  for (let b = 0; b < bins; b++) {
    let sum = 0, n = 0;
    for (let i = b * step, end = i + step; i < end; i += 8) { sum += data[i] * data[i]; n++; }
    out[b] = Math.sqrt(sum / n); if (out[b] > loudest) loudest = out[b];
  }
  for (let b = 0; b < bins; b++) out[b] = Math.min(1, (out[b] / loudest) ** 0.6);
  return out;
};

// ---------- FX unit ----------
const FX_TYPES = {
  echo: { label: 'Echo', insert: false, param: 'Feedback' },
  reverb: { label: 'Reverb', insert: false, param: 'Wash' },
  flanger: { label: 'Flanger', insert: true, param: 'Depth' },
  sweep: { label: 'Filter sweep', insert: true, param: 'Resonance' },
  gate: { label: 'Trance gate', insert: true, param: 'Shape' },
  crush: { label: 'Bitcrush', insert: true, param: 'Bits' },
};
DJ.FX_TYPES = FX_TYPES;
DJ.FX_BEATS = [0.125, 0.25, 0.5, 0.75, 1, 2, 4, 8];

function makeImpulse(ctx, seconds, decay) {
  const length = Math.floor(ctx.sampleRate * seconds);
  const impulse = ctx.createBuffer(2, length, ctx.sampleRate);
  for (let c = 0; c < 2; c++) {
    const data = impulse.getChannelData(c);
    for (let i = 0; i < length; i++) data[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / length, decay);
  }
  return impulse;
}

class FxUnit {
  constructor(engine, deck) {
    const ctx = this.ctx = engine.ctx;
    this.deck = deck;
    this.type = 'echo';
    this.on = false;
    this.wet = 0.6;
    this.beats = 0.75;
    this.param = 0.5;
    this.target = 'all';
    this.input = ctx.createGain();
    this.inputGate = ctx.createGain(); this.inputGate.gain.value = 0;
    this.output = ctx.createGain(); this.output.gain.value = 0;
    this.input.connect(this.inputGate);
    this.chain = null;
    this.build();
  }

  build() {
    const ctx = this.ctx;
    if (this.chain) { this.chain.nodes.forEach(n => { try { n.disconnect(); } catch (_) {} try { n.stop && n.stop(); } catch (_) {} }); }
    try { this.inputGate.disconnect(); } catch (_) {}
    const nodes = [];
    const add = n => (nodes.push(n), n);
    let head, tail;
    const t = this.type;
    if (t === 'echo') {
      const delay = add(ctx.createDelay(8));
      const fb = add(ctx.createGain());
      const tone = add(ctx.createBiquadFilter()); tone.type = 'lowpass'; tone.frequency.value = 6500;
      const low = add(ctx.createBiquadFilter()); low.type = 'highpass'; low.frequency.value = 120;
      delay.connect(tone); tone.connect(low); low.connect(fb); fb.connect(delay);
      head = delay; tail = low;
      this.chain = { nodes, delay, fb };
    } else if (t === 'reverb') {
      const pre = add(ctx.createBiquadFilter()); pre.type = 'highpass'; pre.frequency.value = 200;
      const conv = add(ctx.createConvolver()); conv.buffer = makeImpulse(ctx, 3.2, 2.6);
      pre.connect(conv); head = pre; tail = conv;
      this.chain = { nodes, pre };
    } else if (t === 'flanger') {
      const delay = add(ctx.createDelay(0.05)); delay.delayTime.value = 0.004;
      const lfo = add(ctx.createOscillator()); lfo.type = 'sine';
      const depth = add(ctx.createGain()); depth.gain.value = 0.003;
      const fb = add(ctx.createGain()); fb.gain.value = 0.6;
      const mix = add(ctx.createGain());
      lfo.connect(depth); depth.connect(delay.delayTime); lfo.start();
      delay.connect(fb); fb.connect(delay);
      delay.connect(mix);
      const dry = add(ctx.createGain()); dry.connect(mix);
      const split = add(ctx.createGain()); split.connect(delay); split.connect(dry);
      head = split; tail = mix;
      this.chain = { nodes, lfo, depth, fb };
    } else if (t === 'sweep') {
      const filter = add(ctx.createBiquadFilter()); filter.type = 'lowpass'; filter.frequency.value = 1200;
      const lfo = add(ctx.createOscillator()); lfo.type = 'triangle';
      const depth = add(ctx.createGain()); depth.gain.value = 1100;
      lfo.connect(depth); depth.connect(filter.frequency); lfo.start();
      head = tail = filter;
      this.chain = { nodes, filter, lfo };
    } else if (t === 'gate') {
      const vca = add(ctx.createGain()); vca.gain.value = 0.5;
      const lfo = add(ctx.createOscillator()); lfo.type = 'square';
      const depth = add(ctx.createGain()); depth.gain.value = 0.5;
      lfo.connect(depth); depth.connect(vca.gain); lfo.start();
      head = tail = vca;
      this.chain = { nodes, lfo, vca, depth };
    } else if (t === 'crush') {
      const shaper = add(ctx.createWaveShaper());
      head = tail = shaper;
      this.chain = { nodes, shaper };
    }
    this.inputGate.connect(head);
    tail.connect(this.output);
    this.retime();
    this.applyParam();
  }

  get insert() { return FX_TYPES[this.type].insert; }

  setType(type) {
    if (!FX_TYPES[type] || type === this.type) return;
    const wasOn = this.on;
    if (wasOn) this.setOn(false, true);
    this.type = type;
    this.build();
    if (wasOn) this.setOn(true);
  }

  retime() {
    const c = this.chain; if (!c) return;
    const now = this.ctx.currentTime;
    const beat = this.deck.beatSeconds(); // real seconds per beat at current tempo
    const period = beat * this.beats;
    if (c.delay) c.delay.delayTime.setTargetAtTime(clamp(period, 0.01, 7.9), now, 0.02);
    if (c.lfo) {
      // Flanger/sweep cycle over 8x the beat value; the gate pulses per value.
      const cycle = this.type === 'gate' ? period : period * 8;
      c.lfo.frequency.setTargetAtTime(1 / Math.max(0.02, cycle), now, 0.02);
    }
  }

  applyParam() {
    const c = this.chain; if (!c) return;
    const now = this.ctx.currentTime, p = this.param;
    if (this.type === 'echo') c.fb.gain.setTargetAtTime(p * 0.88, now, 0.02);
    if (this.type === 'reverb') c.pre.frequency.setTargetAtTime(80 * Math.pow(40, p), now, 0.05);
    if (this.type === 'flanger') { c.fb.gain.setTargetAtTime(0.2 + p * 0.65, now, 0.02); c.depth.gain.setTargetAtTime(0.0008 + p * 0.0035, now, 0.02); }
    if (this.type === 'sweep') c.filter.Q.setTargetAtTime(0.7 + p * 14, now, 0.02);
    if (this.type === 'gate') c.depth.gain.setTargetAtTime(0.2 + p * 0.3, now, 0.02);
    if (this.type === 'crush') {
      const bits = Math.round(2 + (1 - p) * 10);
      const steps = Math.pow(2, bits), n = 4096, curve = new Float32Array(n);
      for (let i = 0; i < n; i++) { const x = i / (n - 1) * 2 - 1; curve[i] = Math.round(x * steps) / steps; }
      c.shaper.curve = curve;
    }
  }

  setOn(on, instant) {
    this.on = on;
    const now = this.ctx.currentTime;
    this.inputGate.gain.cancelScheduledValues(now);
    this.inputGate.gain.setTargetAtTime(on ? 1 : 0, now, 0.005);
    if (on) {
      this.output.gain.cancelScheduledValues(now);
      this.output.gain.setTargetAtTime(this.wet, now, 0.01);
    } else if (this.insert || instant) {
      this.output.gain.cancelScheduledValues(now);
      this.output.gain.setTargetAtTime(0, now, 0.01);
    }
    // Send FX (echo/reverb) keep their output open so tails ring out.
    this.deck.applyStemRouting();
  }

  setWet(v) {
    this.wet = v;
    if (this.on) this.output.gain.setTargetAtTime(v, this.ctx.currentTime, 0.02);
    this.deck.applyStemRouting();
  }

  targets(stem) { return this.target === 'all' || this.target === stem; }
}

// ---------- deck ----------
class Deck {
  constructor(engine, index) {
    this.engine = engine;
    this.index = index;
    this.name = 'AB'[index];
    const ctx = this.ctx = engine.ctx;
    this.track = null;
    this.buffers = {};
    this.stemNames = [];
    this.stems = {}; // name -> {gain, auto, dry, send, volume, muted}
    this.sources = [];
    this.duration = 0;
    this.playing = false;
    this.pausedPos = 0;
    this.anchorTime = 0;
    this.anchorPos = 0;
    this.rate = 1;
    this.tempoRange = 0.08;
    this.tempo = 0; // slider -1..1
    this.keyLock = false;
    this.keyShift = 0;
    this.cuePoint = 0;
    this.hotCues = Array(8).fill(null);
    this.loop = null; // {start, end}
    this.syncLock = false;
    this.quantize = true;
    this.peaks = null;
    this.activity = {};
    this.loading = null;
    this.listeners = new Set();
    this.nudgeFactor = 1;

    this.sum = ctx.createGain();
    this.keylock = engine.keylockReady ? new AudioWorkletNode(ctx, 'dj-keylock', { outputChannelCount: [2] }) : ctx.createGain();
    this.trim = ctx.createGain();
    this.eqLow = ctx.createBiquadFilter(); this.eqLow.type = 'lowshelf'; this.eqLow.frequency.value = 220;
    this.eqMid = ctx.createBiquadFilter(); this.eqMid.type = 'peaking'; this.eqMid.frequency.value = 1100; this.eqMid.Q.value = 0.7;
    this.eqHigh = ctx.createBiquadFilter(); this.eqHigh.type = 'highshelf'; this.eqHigh.frequency.value = 3800;
    this.lp = ctx.createBiquadFilter(); this.lp.type = 'lowpass'; this.lp.frequency.value = 22000; this.lp.Q.value = 0.9;
    this.hp = ctx.createBiquadFilter(); this.hp.type = 'highpass'; this.hp.frequency.value = 10; this.hp.Q.value = 0.9;
    this.autoGain = ctx.createGain(); // Auto DJ automation lives here, not on the fader
    this.channel = ctx.createGain();
    this.xf = ctx.createGain();
    this.pfl = ctx.createGain(); this.pfl.gain.value = 0;
    this.meter = ctx.createAnalyser(); this.meter.fftSize = 1024;
    this.sum.connect(this.keylock).connect(this.trim).connect(this.eqLow).connect(this.eqMid)
      .connect(this.eqHigh).connect(this.lp).connect(this.hp).connect(this.autoGain).connect(this.channel);
    this.channel.connect(this.meter);
    this.channel.connect(this.xf).connect(engine.masterIn);
    this.autoGain.connect(this.pfl).connect(engine.cueBus);
    this.eq = { low: 0, mid: 0, high: 0 };
    this.kills = { low: false, mid: false, high: false };
    this.filter = 0;
    this.fader = 1;
    this.fx = new FxUnit(engine, this);
    this.fx.output.connect(this.sum);
    this.echoOutTimer = null;
  }

  on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  emit(what) { this.listeners.forEach(fn => fn(what, this)); }

  // ---- loading ----
  async load(track, api, onProgress) {
    const token = Symbol('load');
    this.loading = token;
    this.pause();
    this.setLoop(null);
    this.disposeStems();
    this.track = track;
    this.emit('loading');
    const useStems = track.stems && ['ready', 'partial'].includes(track.stems.status) && track.stems.names.length >= 2;
    const names = useStems ? track.stems.names.slice().sort((a, b) => STEM_ORDER.indexOf(a) - STEM_ORDER.indexOf(b)) : ['full'];
    let done = 0;
    const fetchBuffer = async name => {
      const r = await fetch(api.media(track.id, name));
      if (!r.ok) throw new Error(`${STEM_LABELS[name]} is not ready (${r.status})`);
      const data = await r.arrayBuffer();
      const buffer = await this.ctx.decodeAudioData(data);
      done++; onProgress && onProgress(done / (names.length + 1));
      return [name, buffer];
    };
    const peaksPromise = fetch(api.media(track.id, 'peaks')).then(r => r.ok ? r.arrayBuffer() : null).catch(() => null);
    const entries = await Promise.all(names.map(fetchBuffer));
    if (this.loading !== token) return false;
    const peaks = await peaksPromise;
    this.buffers = Object.fromEntries(entries);
    this.stemNames = names;
    this.duration = Math.max(...entries.map(([, b]) => b.duration));
    this.peaks = peaks ? new Uint8Array(peaks) : peaksFromBuffer(entries[0][1]);
    this.activity = {};
    if (useStems) for (const [name, buffer] of entries) this.activity[name] = DJ.stemActivity(buffer);
    for (const name of names) this.createStem(name);
    const a = track.analysis || {};
    this.cuePoint = this.firstBeat();
    this.pausedPos = this.cuePoint;
    this.hotCues = Array.from({ length: 8 }, (_, i) => (track.cues && track.cues[i] != null) ? track.cues[i] : null);
    this.setTempo(this.syncLock ? this.tempo : 0);
    this.loading = null;
    this.emit('loaded');
    onProgress && onProgress(1);
    if (!track.key) {
      DJ.detectKey(this.buffers).then(key => {
        if (key && this.track === track) { track.key = key; this.emit('key'); api.saveTrack(track.id, { key }); }
      }).catch(() => {});
    }
    return true;
  }

  createStem(name) {
    const ctx = this.ctx;
    const gain = ctx.createGain(), auto = ctx.createGain(), dry = ctx.createGain(), send = ctx.createGain();
    send.gain.value = 0;
    gain.connect(auto); auto.connect(dry); dry.connect(this.sum);
    auto.connect(send); send.connect(this.fx.input);
    this.stems[name] = { gain, auto, dry, send, volume: 1, muted: false, killedByFx: false };
  }

  disposeStems() {
    this.stopSources();
    for (const s of Object.values(this.stems)) for (const n of [s.gain, s.auto, s.dry, s.send]) { try { n.disconnect(); } catch (_) {} }
    this.stems = {};
    this.buffers = {};
    this.stemNames = [];
    this.activity = {};
  }

  unload() {
    this.loading = null;
    this.pause();
    this.disposeStems();
    this.track = null;
    this.duration = 0;
    this.peaks = null;
    this.emit('loaded');
  }

  // ---- beat grid ----
  get bpm() { return this.track && this.track.analysis && this.track.analysis.bpm || null; }
  get beatOffset() { const a = this.track && this.track.analysis; return a && a.beat_offset != null ? a.beat_offset : 0; }
  get effectiveBpm() { return this.bpm ? this.bpm * this.rate : null; }
  beatLen() { return this.bpm ? 60 / this.bpm : 0.5; }       // track seconds per beat
  beatSeconds() { return this.beatLen() / this.rate; }       // wall seconds per beat
  beatAt(pos) { return (pos - this.beatOffset) / this.beatLen(); }
  posOfBeat(beat) { return this.beatOffset + beat * this.beatLen(); }
  snap(pos) { return this.bpm && this.quantize ? this.posOfBeat(Math.round(this.beatAt(pos))) : pos; }
  firstBeat() {
    const a = this.track && this.track.analysis || {};
    const start = a.audible_start || 0;
    if (!this.bpm) return start;
    return Math.max(0, this.posOfBeat(Math.ceil(this.beatAt(start) - 0.05)));
  }

  // ---- transport ----
  get position() {
    if (!this.playing) return this.pausedPos;
    let p = this.anchorPos + (this.ctx.currentTime - this.anchorTime) * this.rate * this.nudgeFactor;
    if (this.loop && this.anchorPos <= this.loop.end && p >= this.loop.end) {
      const len = this.loop.end - this.loop.start;
      p = this.loop.start + ((p - this.loop.start) % len);
    }
    return p;
  }

  get key() {
    const key = this.track && this.track.key;
    if (!key) return null;
    const semis = this.keyShift + (this.keyLock ? 0 : 12 * Math.log2(this.rate));
    return DJ.shiftCamelot(key.camelot, semis);
  }

  startSources(pos, when) {
    this.stopSources();
    const ctx = this.ctx;
    const at = when || ctx.currentTime;
    const rate = this.rate * this.nudgeFactor;
    for (const name of this.stemNames) {
      const buffer = this.buffers[name];
      if (!buffer || pos >= buffer.duration) continue;
      const src = ctx.createBufferSource();
      src.buffer = buffer;
      src.playbackRate.value = rate;
      if (this.loop) { src.loop = true; src.loopStart = this.loop.start; src.loopEnd = this.loop.end; }
      src.connect(this.stems[name].gain);
      src.start(at, Math.max(0, pos));
      this.sources.push(src);
    }
    this.anchorTime = at;
    this.anchorPos = pos;
    if (this.sources[0]) this.sources[0].onended = () => { if (this.sources.length && this.playing && !this.loop && this.position >= this.duration - 0.05) this.ended(); };
  }

  stopSources() {
    for (const src of this.sources) { try { src.onended = null; src.stop(); } catch (_) {} try { src.disconnect(); } catch (_) {} }
    this.sources = [];
  }

  ended() {
    this.playing = false;
    this.stopSources();
    this.pausedPos = this.duration;
    this.emit('ended');
    this.emit('state');
  }

  play(when, pos) {
    if (!this.track || this.loading) return;
    if (this.playing) return;
    if (pos != null) this.pausedPos = pos;
    if (this.pausedPos >= this.duration - 0.05) this.pausedPos = this.cuePoint;
    this.playing = true;
    this.startSources(this.pausedPos, when);
    this.emit('state');
  }

  pause() {
    if (!this.playing) return;
    this.pausedPos = Math.min(this.position, this.duration);
    this.playing = false;
    this.stopSources();
    this.emit('state');
  }

  toggle() { this.playing ? this.pause() : this.play(); }

  seek(pos) {
    pos = clamp(pos, 0, Math.max(0, this.duration - 0.01));
    if (this.loop && (pos < this.loop.start || pos > this.loop.end)) this.setLoop(null);
    if (this.playing) this.startSources(pos);
    else this.pausedPos = pos;
    this.emit('seek');
  }

  // Seek keeping the beat phase (used by hot cues/jumps while playing).
  seekQuantized(target) {
    if (!(this.playing && this.quantize && this.bpm)) return this.seek(target);
    const phase = this.beatAt(this.position) % 1;
    const targetBeat = Math.round(this.beatAt(target));
    this.seek(this.posOfBeat(targetBeat + (phase < 0 ? phase + 1 : phase)));
  }

  cueDown() {
    if (!this.track) return;
    if (this.playing && !this.previewing) {
      this.pause(); this.seek(this.cuePoint);
    } else if (Math.abs(this.pausedPos - this.cuePoint) < 0.02) {
      this.previewing = true; this.play();
    } else {
      this.cuePoint = this.snap(this.pausedPos); this.pausedPos = this.cuePoint;
      this.emit('cue');
    }
  }
  cueUp() {
    if (this.previewing) { this.previewing = false; this.pause(); this.seek(this.cuePoint); }
  }

  hotCue(i, erase) {
    if (!this.track) return;
    if (erase) this.hotCues[i] = null;
    else if (this.hotCues[i] == null) this.hotCues[i] = this.snap(this.position);
    else this.seekQuantized(this.hotCues[i]);
    this.emit('cues');
    if (erase || this.hotCues[i] != null) this.engine.api.saveTrack(this.track.id, { cues: this.hotCues });
  }

  beatJump(beats) { if (this.track) this.seekQuantized(this.position + beats * this.beatLen()); }

  // ---- tempo ----
  setTempo(slider) {
    this.tempo = clamp(slider, -1, 1);
    this.setRate(1 + this.tempo * this.tempoRange, true);
  }
  setRate(rate, fromSlider) {
    rate = clamp(rate, 0.5, 2);
    if (!fromSlider) {
      const needed = Math.abs(rate - 1);
      if (needed > this.tempoRange) this.tempoRange = [0.08, 0.16, 0.5].find(r => r >= needed) || 0.5;
      this.tempo = (rate - 1) / this.tempoRange;
    }
    const pos = this.position;
    this.rate = rate;
    if (this.playing) {
      this.anchorPos = pos; this.anchorTime = this.ctx.currentTime;
      const now = this.ctx.currentTime;
      for (const src of this.sources) src.playbackRate.setValueAtTime(rate * this.nudgeFactor, now);
    }
    this.applyKeyLock();
    this.fx.retime();
    this.emit('tempo');
  }
  setTempoRange(range) {
    const rate = this.rate;
    this.tempoRange = range;
    this.tempo = clamp((rate - 1) / range, -1, 1);
    this.setRate(1 + this.tempo * range, true);
  }
  nudge(direction) {
    const pos = this.position;
    this.nudgeFactor = direction ? 1 + 0.04 * direction : 1;
    if (this.playing) {
      this.anchorPos = pos; this.anchorTime = this.ctx.currentTime;
      for (const src of this.sources) src.playbackRate.setValueAtTime(this.rate * this.nudgeFactor, this.ctx.currentTime);
    }
  }
  applyKeyLock() {
    const pitch = (this.keyLock ? 1 / this.rate : 1) * Math.pow(2, this.keyShift / 12);
    const param = this.keylock.parameters && this.keylock.parameters.get('pitch');
    if (param) param.setValueAtTime(clamp(pitch, 0.5, 2), this.ctx.currentTime);
  }
  setKeyLock(on) { this.keyLock = on; this.applyKeyLock(); this.emit('tempo'); }
  setKeyShift(semis) { this.keyShift = clamp(Math.round(semis), -6, 6); this.applyKeyLock(); this.emit('tempo'); }

  // Match tempo (and beat phase) to another deck.
  syncTo(other, phase = true) {
    if (!other || !other.track || !this.bpm || !other.effectiveBpm) return false;
    const target = other.effectiveBpm;
    let best = null;
    for (const mult of [0.5, 1, 2]) {
      const rate = target / (this.bpm * mult);
      if (!best || Math.abs(rate - 1) < Math.abs(best - 1)) best = rate;
    }
    if (Math.abs(best - 1) > 0.5) return false;
    this.setRate(best);
    if (phase && this.playing && other.playing) this.alignPhase(other);
    return true;
  }
  alignPhase(other) {
    const mine = this.beatAt(this.position);
    const theirs = other.beatAt(other.position);
    // Our beat length may be double/half theirs: compare phase within the
    // shorter of the two beats, measured in our beats.
    const ratio = (other.beatLen() / other.rate) / (this.beatLen() / this.rate);
    let diff = ((mine % 1) - ((theirs * ratio) % 1)) % 1;
    if (diff > 0.5) diff -= 1;
    if (diff < -0.5) diff += 1;
    this.seek(this.position - diff * this.beatLen());
  }

  // ---- loops ----
  setLoop(loop) {
    this.loop = loop && loop.end - loop.start > 0.01 ? loop : null;
    const pos = this.position;
    if (this.playing) {
      if (this.loop && (pos < this.loop.start || pos > this.loop.end)) { this.startSources(this.loop.start); }
      else {
        this.anchorPos = pos; this.anchorTime = this.ctx.currentTime;
        for (const src of this.sources) {
          src.loop = !!this.loop;
          if (this.loop) { src.loopStart = this.loop.start; src.loopEnd = this.loop.end; }
        }
      }
    }
    this.emit('loop');
  }
  autoLoop(beats) {
    if (!this.track) return;
    const len = beats * this.beatLen();
    if (this.loop && Math.abs((this.loop.end - this.loop.start) - len) < 0.005) return this.setLoop(null);
    const pos = this.position;
    const start = this.bpm && this.quantize ? this.posOfBeat(Math.floor(this.beatAt(pos) + 0.001)) : pos;
    this.setLoop({ start, end: start + len });
  }
  resizeLoop(factor) {
    if (!this.loop) return;
    const len = (this.loop.end - this.loop.start) * factor;
    if (len < 0.02 || len > 120) return;
    this.setLoop({ start: this.loop.start, end: this.loop.start + len });
  }
  loopIn() { this.loopInPoint = this.snap(this.position); this.emit('loop'); }
  loopOut() {
    if (this.loopInPoint == null) return;
    const end = this.snap(this.position);
    if (end > this.loopInPoint) this.setLoop({ start: this.loopInPoint, end });
    this.loopInPoint = null;
  }
  // Loop roll: loop while held, then continue where the song would have been.
  rollDown(beats) {
    if (!this.playing || !this.track) return;
    this.rollFrom = { pos: this.position, time: this.ctx.currentTime, rate: this.rate };
    const pos = this.position;
    const start = this.bpm ? this.posOfBeat(Math.floor(this.beatAt(pos) / beats) * beats) : pos;
    const len = beats * this.beatLen();
    const startAligned = start + Math.floor((pos - start) / len) * len;
    this.setLoop({ start: startAligned, end: startAligned + len });
  }
  rollUp() {
    if (!this.rollFrom) return;
    const r = this.rollFrom; this.rollFrom = null;
    const resume = r.pos + (this.ctx.currentTime - r.time) * r.rate;
    this.setLoop(null);
    this.seek(resume);
  }

  // ---- mixer ----
  setEq(band, v) {
    this.eq[band] = clamp(v, -1, 1);
    const node = { low: this.eqLow, mid: this.eqMid, high: this.eqHigh }[band];
    const value = this.kills[band] ? -40 : (this.eq[band] < 0 ? this.eq[band] * 30 : this.eq[band] * 6);
    node.gain.cancelScheduledValues(this.ctx.currentTime);
    node.gain.setTargetAtTime(value, this.ctx.currentTime, 0.01);
    this.emit('mixer');
  }
  setKill(band, on) { this.kills[band] = on; this.setEq(band, this.eq[band]); }
  setFilter(v) {
    this.filter = clamp(v, -1, 1);
    const now = this.ctx.currentTime;
    const lp = this.filter < -0.02 ? 22000 * Math.pow(2, this.filter * 9.2) : 22000;
    const hp = this.filter > 0.02 ? 10 * Math.pow(2, this.filter * 10.5) : 10;
    for (const p of [this.lp.frequency, this.hp.frequency]) p.cancelScheduledValues(now);
    this.lp.frequency.setTargetAtTime(lp, now, 0.015);
    this.hp.frequency.setTargetAtTime(hp, now, 0.015);
    const q = Math.abs(this.filter) > 0.02 ? 1.2 + Math.abs(this.filter) * 3 : 0.7;
    this.lp.Q.setTargetAtTime(q, now, 0.02); this.hp.Q.setTargetAtTime(q, now, 0.02);
    this.emit('mixer');
  }
  setFader(v) { this.fader = clamp(v, 0, 1); this.channel.gain.setTargetAtTime(this.fader * this.fader, this.ctx.currentTime, 0.01); this.emit('mixer'); }
  setTrim(v) { this.trim.gain.setTargetAtTime(v, this.ctx.currentTime, 0.02); }
  setPfl(on) { this.pflOn = on; this.pfl.gain.setTargetAtTime(on ? 1 : 0, this.ctx.currentTime, 0.01); this.emit('mixer'); }

  // ---- stems ----
  setStemVolume(name, v) { const s = this.stems[name]; if (!s) return; s.volume = clamp(v, 0, 1.5); this.applyStemRouting(); this.emit('stems'); }
  setStemMuted(name, muted) { const s = this.stems[name]; if (!s) return; s.muted = muted; this.applyStemRouting(); this.emit('stems'); }
  toggleStem(name) { const s = this.stems[name]; if (s) this.setStemMuted(name, !s.muted); }
  // Preset stem mixes ("vibes")
  setStemPreset(preset) {
    const keep = {
      full: () => true,
      acapella: n => n === 'vocals',
      instrumental: n => n !== 'vocals',
      rhythm: n => n === 'drums' || n === 'bass',
      drums: n => n === 'drums',
      melody: n => n !== 'drums' && n !== 'bass' && n !== 'vocals',
    }[preset];
    if (!keep) return;
    for (const name of Object.keys(this.stems)) this.stems[name].muted = name === 'full' ? false : !keep(name);
    this.applyStemRouting();
    this.emit('stems');
  }
  applyStemRouting() {
    const now = this.ctx.currentTime;
    const fx = this.fx;
    for (const [name, s] of Object.entries(this.stems)) {
      s.gain.gain.setTargetAtTime(s.muted ? 0 : s.volume, now, 0.012);
      const targeted = fx.targets(name);
      s.send.gain.setTargetAtTime(fx.on && targeted ? 1 : 0, now, 0.008);
      const insertCut = fx.on && fx.insert && targeted ? fx.wet : 0;
      s.dry.gain.setTargetAtTime(s.killedByFx ? 0 : 1 - insertCut, now, 0.01);
    }
  }

  // Echo out: the targeted stems (or the whole deck) leave on an echo tail.
  echoOut(target = 'all', beats = 0.75) {
    if (!this.track) return;
    const fx = this.fx;
    fx.setType('echo');
    fx.target = target; fx.beats = beats; fx.param = 0.62; fx.wet = 0.85;
    fx.retime(); fx.applyParam();
    fx.setOn(true);
    for (const [name, s] of Object.entries(this.stems)) if (fx.targets(name)) s.killedByFx = true;
    this.applyStemRouting();
    clearTimeout(this.echoOutTimer);
    // Stop feeding the echo after a beat; let the tail ring.
    this.echoOutTimer = setTimeout(() => {
      fx.inputGate.gain.setTargetAtTime(0, this.ctx.currentTime, 0.02);
      fx.on = false;
      this.echoOutTimer = setTimeout(() => {
        for (const [name, s] of Object.entries(this.stems)) {
          if (s.killedByFx) { s.killedByFx = false; if (target !== 'all') s.muted = true; }
        }
        if (target === 'all') { this.pause(); }
        fx.output.gain.setTargetAtTime(0, this.ctx.currentTime, 0.3);
        this.applyStemRouting();
        this.emit('stems'); this.emit('fx');
      }, this.beatSeconds() * 8 * 1000);
      this.emit('fx');
    }, this.beatSeconds() * 1000);
    this.emit('fx');
  }

  // Vinyl brake: the platter winds down and the deck stops.
  brake(seconds = 1.2) {
    if (!this.playing) return;
    const rate = this.rate * this.nudgeFactor;
    const now = this.ctx.currentTime;
    const pos = this.position;
    for (const src of this.sources) {
      src.playbackRate.cancelScheduledValues(now);
      src.playbackRate.setValueAtTime(rate, now);
      src.playbackRate.linearRampToValueAtTime(0.01, now + seconds);
    }
    clearTimeout(this.brakeTimer);
    this.brakeTimer = setTimeout(() => {
      if (!this.playing) return;
      // A linear ramp to ~0 covers half the distance of full speed.
      this.stopSources(); this.playing = false;
      this.pausedPos = Math.min(this.duration, pos + rate * seconds / 2);
      this.emit('state');
    }, seconds * 1000);
  }
}

// ---------- engine ----------
class Engine {
  constructor(api) {
    this.api = api;
    let ctx;
    try { ctx = new AudioContext({ sampleRate: 48000, latencyHint: 'interactive' }); }
    catch (_) { ctx = new AudioContext({ latencyHint: 'interactive' }); }
    this.ctx = ctx;
    this.masterIn = ctx.createGain();
    this.masterGain = ctx.createGain();
    this.limiter = ctx.createDynamicsCompressor();
    this.limiter.threshold.value = -1.5; this.limiter.knee.value = 0; this.limiter.ratio.value = 20;
    this.limiter.attack.value = 0.002; this.limiter.release.value = 0.12;
    this.masterOut = ctx.createGain();
    this.masterIn.connect(this.masterGain).connect(this.limiter).connect(this.masterOut);
    // Program = decks + line in. It feeds the meters, recorder and the live
    // broadcast; the local monitor only plays the decks (a DJ app on line in
    // is already audible on this computer) unless line monitoring is on.
    this.programOut = ctx.createGain();
    this.masterOut.connect(this.programOut);
    this.lineGain = ctx.createGain();
    this.lineMonitor = ctx.createGain(); this.lineMonitor.gain.value = 0;
    this.lineMeter = ctx.createAnalyser(); this.lineMeter.fftSize = 1024;
    this.lineGain.connect(this.programOut);
    this.lineGain.connect(this.lineMeter);
    this.lineGain.connect(this.lineMonitor).connect(ctx.destination);
    this.line = null;
    this.monitor = ctx.createGain();
    this.masterOut.connect(this.monitor).connect(ctx.destination);
    const split = ctx.createChannelSplitter(2);
    this.meters = [ctx.createAnalyser(), ctx.createAnalyser()];
    this.meters.forEach(m => { m.fftSize = 1024; });
    this.programOut.connect(split); split.connect(this.meters[0], 0); split.connect(this.meters[1], 1);
    // Headphones: cue bus mixed with master, to a separate output device.
    this.cueBus = ctx.createGain();
    this.cueMixCue = ctx.createGain(); this.cueMixMaster = ctx.createGain();
    this.phones = ctx.createMediaStreamDestination();
    this.cueBus.connect(this.cueMixCue).connect(this.phones);
    this.programOut.connect(this.cueMixMaster).connect(this.phones);
    this.phonesEl = null;
    this.setCueMix(0.3);
    this.xfader = 0;
    this.xfCurve = 'dipless';
    this.keylockReady = false;
    this.decks = [];
  }

  async init() {
    try {
      await this.ctx.audioWorklet.addModule('static/worklets.js');
      this.keylockReady = true;
    } catch (error) {
      console.warn('AudioWorklets unavailable; key lock and live are off', error);
    }
    this.decks = [new Deck(this, 0), new Deck(this, 1)];
    this.setCrossfader(0);
  }

  async resume() { if (this.ctx.state !== 'running') await this.ctx.resume(); }

  setCrossfader(v) {
    this.xfader = clamp(v, -1, 1);
    const x = (this.xfader + 1) / 2; // 0 = A, 1 = B
    let a, b;
    if (this.xfCurve === 'power') { a = Math.cos(x * Math.PI / 2); b = Math.sin(x * Math.PI / 2); }
    else if (this.xfCurve === 'sharp') { a = x > 0.97 ? (1 - x) / 0.03 : 1; b = x < 0.03 ? x / 0.03 : 1; }
    else { a = clamp(2 - 2 * x, 0, 1); b = clamp(2 * x, 0, 1); }
    const now = this.ctx.currentTime;
    this.decks[0] && this.decks[0].xf.gain.setTargetAtTime(a, now, 0.005);
    this.decks[1] && this.decks[1].xf.gain.setTargetAtTime(b, now, 0.005);
  }
  setCurve(curve) { this.xfCurve = curve; this.setCrossfader(this.xfader); }
  setMaster(v) { this.masterGain.gain.setTargetAtTime(v, this.ctx.currentTime, 0.02); }
  setMonitor(on) { this.monitor.gain.setTargetAtTime(on ? 1 : 0, this.ctx.currentTime, 0.02); }
  setCueMix(v) {
    this.cueMix = clamp(v, 0, 1);
    const now = this.ctx.currentTime;
    this.cueMixCue.gain.setTargetAtTime(Math.cos(this.cueMix * Math.PI / 2), now, 0.02);
    this.cueMixMaster.gain.setTargetAtTime(Math.sin(this.cueMix * Math.PI / 2), now, 0.02);
  }
  async setPhonesDevice(deviceId) {
    if (!deviceId) {
      if (this.phonesEl) { this.phonesEl.pause(); this.phonesEl.srcObject = null; this.phonesEl = null; }
      return;
    }
    if (!this.phonesEl) {
      this.phonesEl = new Audio();
      this.phonesEl.srcObject = this.phones.stream;
    }
    if (typeof this.phonesEl.setSinkId !== 'function') throw new Error('This browser cannot pick a headphone output.');
    await this.phonesEl.setSinkId(deviceId);
    await this.phonesEl.play();
  }
  async setMainDevice(deviceId) {
    if (typeof this.ctx.setSinkId !== 'function') throw new Error('This browser cannot pick a main output.');
    await this.ctx.setSinkId(deviceId || '');
  }

  // ---- line in (a DJ app, a controller's audio interface, a turntable…) ----
  async startLineIn(deviceId) {
    this.stopLineIn();
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        deviceId: deviceId ? { exact: deviceId } : undefined,
        // Music, not a call: no voice processing.
        echoCancellation: false, noiseSuppression: false, autoGainControl: false,
        channelCount: { ideal: 2 }, sampleRate: { ideal: 48000 },
      },
    });
    const source = this.ctx.createMediaStreamSource(stream);
    source.connect(this.lineGain);
    const track = stream.getAudioTracks()[0];
    this.line = { stream, source, label: track ? track.label : 'Input', deviceId };
    if (track) track.addEventListener('ended', () => { if (this.line && this.line.stream === stream) this.stopLineIn(); });
    return this.line;
  }
  stopLineIn() {
    const line = this.line; if (!line) return;
    this.line = null;
    try { line.source.disconnect(); } catch (_) {}
    line.stream.getTracks().forEach(t => t.stop());
    if (this.onLineChange) this.onLineChange();
  }
  setLineGain(v) { this.lineGain.gain.setTargetAtTime(v, this.ctx.currentTime, 0.02); }
  setLineMonitor(on) { this.lineMonitor.gain.setTargetAtTime(on ? 1 : 0, this.ctx.currentTime, 0.02); }

  level(analyser) {
    const data = new Float32Array(analyser.fftSize);
    analyser.getFloatTimeDomainData(data);
    let peak = 0;
    for (let i = 0; i < data.length; i++) { const v = Math.abs(data[i]); if (v > peak) peak = v; }
    return peak;
  }

  // ---- recording ----
  startRecording() {
    if (this.recorder) return;
    const dest = this.ctx.createMediaStreamDestination();
    this.programOut.connect(dest);
    const type = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4'].find(t => MediaRecorder.isTypeSupported(t)) || '';
    const recorder = new MediaRecorder(dest.stream, type ? { mimeType: type, audioBitsPerSecond: 256000 } : {});
    const chunks = [];
    recorder.ondataavailable = e => e.data.size && chunks.push(e.data);
    this.recorder = { recorder, dest, chunks, startedAt: Date.now(), type: recorder.mimeType };
    recorder.start(1000);
  }
  stopRecording() {
    const rec = this.recorder;
    if (!rec) return Promise.resolve(null);
    this.recorder = null;
    return new Promise(resolve => {
      rec.recorder.onstop = () => {
        try { this.programOut.disconnect(rec.dest); } catch (_) {}
        resolve(new Blob(rec.chunks, { type: rec.type || 'audio/webm' }));
      };
      rec.recorder.stop();
    });
  }

  // ---- live broadcast to a Huddle room ----
  async goLive(url, onStatus) {
    if (this.live) return;
    if (!this.keylockReady) throw new Error('This browser has no AudioWorklet support.');
    const socket = new WebSocket(url);
    socket.binaryType = 'arraybuffer';
    const node = new AudioWorkletNode(this.ctx, 'dj-capture', { numberOfInputs: 1, numberOfOutputs: 0, channelCount: 2, channelCountMode: 'explicit' });
    const live = this.live = { socket, node, sent: 0 };
    node.port.onmessage = e => {
      if (socket.readyState === 1 && socket.bufferedAmount < 192000 * 2) { socket.send(e.data); live.sent++; }
    };
    socket.onmessage = e => { try { const m = JSON.parse(e.data); onStatus && onStatus(m.t === 'live' ? 'live' : m.t === 'lag' ? `lag ${m.ms}ms` : m.t); } catch (_) {} };
    socket.onclose = () => { if (this.live === live) { this.stopLive(); onStatus && onStatus('off'); } };
    socket.onerror = () => onStatus && onStatus('error');
    await new Promise((resolve, reject) => {
      socket.addEventListener('open', resolve, { once: true });
      socket.addEventListener('close', () => reject(new Error('The room refused the broadcast.')), { once: true });
    });
    this.programOut.connect(node);
  }
  stopLive() {
    const live = this.live; if (!live) return;
    this.live = null;
    try { this.programOut.disconnect(live.node); } catch (_) {}
    live.node.port.postMessage('stop');
    if (live.socket.readyState <= 1) live.socket.close();
  }
}

DJ.Engine = Engine;
DJ.Deck = Deck;
})();
