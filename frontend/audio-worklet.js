class Microphone extends AudioWorkletProcessor {
  constructor() { super(); this.samples = new Int16Array(1600); this.offset = 0; }
  process(inputs) {
    const input = inputs[0]?.[0];
    if (input) for (const sample of input) {
      this.samples[this.offset++] = Math.max(-1, Math.min(1, sample)) * (sample < 0 ? 32768 : 32767);
      if (this.offset === this.samples.length) {
        this.port.postMessage(this.samples.buffer, [this.samples.buffer]);
        this.samples = new Int16Array(1600); this.offset = 0;
      }
    }
    return true;
  }
}
registerProcessor('microphone', Microphone);
