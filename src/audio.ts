/** 所有轨道共用一次启动时刻；切换只改变增益，不重启播放头。 */
export function timelinePosition(
  offset: number,
  elapsed: number,
  duration: number,
  loop?: [number, number],
  stopAt?: number,
) {
  const position = offset + Math.max(0, elapsed);
  if (loop && loop[1] > loop[0] && position >= loop[1])
    return loop[0] + ((position - loop[0]) % (loop[1] - loop[0]));
  return Math.min(position, stopAt ?? duration, duration);
}

/** 把所有候选聚合为同一条二值内容提示，不暴露任一候选的幅度轮廓。 */
export function sharedContentGuide(tracks: Float32Array[], bins = 96) {
  if (!tracks.length || bins <= 0) return [];
  const length = Math.min(...tracks.map((track) => track.length));
  if (!length) return Array(bins).fill(0) as number[];
  const energies = Array.from({ length: bins }, (_, bin) => {
    const start = Math.floor((bin * length) / bins);
    const end = Math.max(start + 1, Math.floor(((bin + 1) * length) / bins));
    const perTrack = tracks
      .map((track) => {
        let sum = 0;
        const limit = Math.min(end, track.length);
        for (let i = start; i < limit; i++) sum += track[i] ** 2;
        return Math.sqrt(sum / Math.max(1, limit - start));
      })
      .sort((a, b) => a - b);
    return perTrack[Math.floor((perTrack.length - 1) / 2)];
  });
  const peak = Math.max(...energies);
  const threshold = Math.max(1e-4, peak * 0.08);
  return energies.map((energy) => (energy >= threshold ? 1 : 0));
}

export class AudioEngine {
  context: AudioContext;
  buffers: AudioBuffer[] = [];
  sources: AudioBufferSourceNode[] = [];
  gains: GainNode[] = [];
  master: GainNode;
  selected = 0;
  offset = 0;
  started = 0;
  playing = false;
  generation = 0;
  loop?: [number, number];
  stopAt?: number;
  constructor(context?: AudioContext) {
    this.context = context || new AudioContext();
    this.master = this.context.createGain();
    this.master.gain.value = 0.7;
    this.master.connect(this.context.destination);
  }
  get duration() {
    return this.buffers[0]?.duration || 0;
  }
  get position() {
    return this.playing
      ? timelinePosition(
          this.offset,
          this.context.currentTime - this.started,
          this.duration,
          this.loop,
          this.stopAt,
        )
      : this.offset;
  }
  async load(urls: string[], signal?: AbortSignal) {
    this.stop();
    this.buffers = await Promise.all(
      urls.map(async (url) => {
        const response = await fetch(url, { signal });
        if (!response.ok) throw new Error('音频加载失败，请检查登录状态和资产完整性');
        return this.context.decodeAudioData(await response.arrayBuffer());
      }),
    );
    this.offset = 0;
    this.selected = 0;
  }
  async play(offset = this.offset, loop?: [number, number], stopAt?: number) {
    if (!this.buffers.length) return;
    this.stop();
    const generation = this.generation;
    await this.context.resume();
    if (generation !== this.generation) return;
    this.loop = loop;
    this.stopAt = loop ? undefined : stopAt;
    this.offset = Math.max(0, Math.min(offset, this.duration));
    if (loop && (this.offset < loop[0] || this.offset >= loop[1])) this.offset = loop[0];
    if (this.offset >= this.duration) this.offset = 0;
    this.started = this.context.currentTime + 0.035;
    this.buffers.forEach((buffer, i) => {
      const source = this.context.createBufferSource();
      const gain = this.context.createGain();
      source.buffer = buffer;
      source.loop = !!loop;
      if (loop) {
        source.loopStart = loop[0];
        source.loopEnd = loop[1];
      }
      gain.gain.value = i === this.selected ? 1 : 0;
      source.connect(gain);
      gain.connect(this.master);
      if (this.stopAt && this.stopAt > this.offset)
        source.start(this.started, this.offset, this.stopAt - this.offset);
      else source.start(this.started, this.offset);
      this.sources.push(source);
      this.gains.push(gain);
    });
    this.playing = true;
  }
  stop() {
    this.generation++;
    if (this.playing) this.offset = this.position;
    this.sources.forEach((source) => {
      try {
        source.stop();
      } catch {
        /* 已结束 */
      }
      source.disconnect();
    });
    this.gains.forEach((gain) => gain.disconnect());
    this.sources = [];
    this.gains = [];
    this.playing = false;
  }
  select(index: number) {
    this.selected = index;
    const t = this.context.currentTime;
    this.gains.forEach((gain, i) => {
      gain.gain.cancelAndHoldAtTime(t);
      gain.gain.linearRampToValueAtTime(i === index ? 1 : 0, t + 0.008);
    });
  }
  seek(time: number) {
    const wasPlaying = this.playing;
    this.stop();
    this.offset = time;
    if (wasPlaying) void this.play(time, this.loop, this.stopAt);
  }
  setVolume(value: number) {
    this.master.gain.setTargetAtTime(value, this.context.currentTime, 0.01);
  }
  dispose() {
    this.stop();
    void this.context.close();
  }
  contentGuide(bins = 96) {
    return sharedContentGuide(
      this.buffers.map((buffer) => buffer.getChannelData(0)),
      bins,
    );
  }
}
