// AudioWorklet processors for the DJ booth.

// Master capture for "Go live": stereo float -> interleaved s16le at 48 kHz,
// posted to the page in 40 ms packets that it forwards over a WebSocket.
class DjCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = 48000 / sampleRate;
    this.packetFrames = 1920; // 40 ms at 48 kHz
    this.out = new Int16Array(this.packetFrames * 2);
    this.fill = 0;
    this.phase = 0; // fractional read position for resampling
    this.prevL = 0;
    this.prevR = 0;
    this.active = true;
    this.port.onmessage = (event) => { if (event.data === 'stop') this.active = false; };
  }

  push(l, r) {
    const out = this.out;
    out[this.fill++] = Math.max(-32768, Math.min(32767, Math.round(l * 32767)));
    out[this.fill++] = Math.max(-32768, Math.min(32767, Math.round(r * 32767)));
    if (this.fill >= out.length) {
      const packet = out.slice(0);
      this.port.postMessage(packet.buffer, [packet.buffer]);
      this.fill = 0;
    }
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input.length) return this.active;
    const left = input[0];
    const right = input[1] || input[0];
    if (this.ratio === 1) {
      for (let i = 0; i < left.length; i++) this.push(left[i], right[i]);
      return this.active;
    }
    // Linear resampling for contexts that could not open at 48 kHz.
    const step = 1 / this.ratio;
    for (let i = 0; i < left.length; i++) {
      while (this.phase < 1) {
        const t = this.phase;
        this.push(this.prevL + (left[i] - this.prevL) * t, this.prevR + (right[i] - this.prevR) * t);
        this.phase += step;
      }
      this.phase -= 1;
      this.prevL = left[i];
      this.prevR = right[i];
    }
    return this.active;
  }
}
registerProcessor('dj-capture', DjCapture);

// Key lock: a two-head delay-line pitch shifter. The deck plays faster or
// slower; this shifts pitch back by the inverse ratio so the key holds.
// Heads sit half a window apart and crossfade with sin²/cos², which keeps
// the summed gain flat. At pitch 1 it passes audio straight through.
class DjKeyLock extends AudioWorkletProcessor {
  static get parameterDescriptors() {
    return [{ name: 'pitch', defaultValue: 1, minValue: 0.5, maxValue: 2, automationRate: 'k-rate' }];
  }

  constructor() {
    super();
    this.window = Math.round(sampleRate * 0.06); // 60 ms grains suit music
    this.size = 1 << Math.ceil(Math.log2(this.window * 4));
    this.mask = this.size - 1;
    this.buffers = [new Float32Array(this.size), new Float32Array(this.size)];
    this.write = 0;
    this.delay = 0; // head A's delay in samples, sweeps through [0, window)
  }

  read(buffer, position) {
    const base = Math.floor(position);
    const frac = position - base;
    const a = buffer[base & this.mask];
    const b = buffer[(base + 1) & this.mask];
    return a + (b - a) * frac;
  }

  process(inputs, outputs, parameters) {
    const input = inputs[0];
    const output = outputs[0];
    if (!input || !input.length) return true;
    const pitch = parameters.pitch[0];
    const channels = Math.min(output.length, 2);
    const frames = output[0].length;
    if (Math.abs(pitch - 1) < 0.0005) {
      for (let c = 0; c < output.length; c++) {
        output[c].set(input[Math.min(c, input.length - 1)]);
        // Keep the delay line warm so switching on does not click.
        if (c < 2) {
          const buf = this.buffers[c];
          const src = input[Math.min(c, input.length - 1)];
          for (let i = 0; i < frames; i++) buf[(this.write + i) & this.mask] = src[i];
        }
      }
      this.write = (this.write + frames) & this.mask;
      return true;
    }
    const W = this.window;
    const drift = 1 - pitch; // how fast the delay grows per output sample
    for (let i = 0; i < frames; i++) {
      for (let c = 0; c < channels; c++) {
        this.buffers[c][this.write] = input[Math.min(c, input.length - 1)][i];
      }
      let d1 = this.delay;
      let d2 = d1 + W / 2;
      if (d2 >= W) d2 -= W;
      const g1 = Math.sin(Math.PI * d1 / W) ** 2;
      const g2 = 1 - g1;
      for (let c = 0; c < channels; c++) {
        const buf = this.buffers[c];
        const s1 = this.read(buf, this.write - d1 - 1 + this.size);
        const s2 = this.read(buf, this.write - d2 - 1 + this.size);
        output[c][i] = s1 * g1 + s2 * g2;
      }
      this.write = (this.write + 1) & this.mask;
      this.delay += drift;
      if (this.delay >= W) this.delay -= W;
      else if (this.delay < 0) this.delay += W;
    }
    for (let c = channels; c < output.length; c++) output[c].set(output[0]);
    return true;
  }
}
registerProcessor('dj-keylock', DjKeyLock);
