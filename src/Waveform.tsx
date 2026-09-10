import { useEffect, useRef } from 'react';
import type { Analysis } from './types';

export function Waveform({
  data,
  spectrum,
  selected,
  duration,
  region,
  contentGuide,
  onRegion,
  onSeek,
}: {
  data?: Analysis;
  spectrum: boolean;
  selected: boolean;
  duration: number;
  region: [number, number];
  contentGuide?: number[];
  onRegion: (r: [number, number]) => void;
  onSeek: (time: number) => void;
}) {
  const ref = useRef<HTMLCanvasElement>(null);
  const down = useRef<number | null>(null);
  const moved = useRef(false);
  const visibleRegion =
    duration > 0 && region[1] > region[0] && (region[0] > 0.001 || region[1] < duration - 0.001);
  useEffect(() => {
    const canvas = ref.current!;
    const draw = () => {
      const bounds = canvas.getBoundingClientRect();
      const scale = window.devicePixelRatio || 1;
      canvas.width = bounds.width * scale;
      canvas.height = bounds.height * scale;
      const ctx = canvas.getContext('2d')!;
      ctx.scale(scale, scale);
      const w = bounds.width,
        h = bounds.height;
      ctx.fillStyle = '#101c28';
      ctx.fillRect(0, 0, w, h);
      ctx.strokeStyle = '#243444';
      ctx.lineWidth = 1;
      for (let i = 0; i <= 8; i++) {
        ctx.beginPath();
        ctx.moveTo((i * w) / 8, 0);
        ctx.lineTo((i * w) / 8, h);
        ctx.stroke();
      }
      ctx.beginPath();
      ctx.moveTo(0, h / 2);
      ctx.lineTo(w, h / 2);
      ctx.stroke();
      if (data && spectrum && data.spectrogram.length) {
        const image = document.createElement('canvas');
        image.width = data.spectrogram.length;
        image.height = 257;
        const ic = image.getContext('2d')!;
        const pixels = ic.createImageData(image.width, image.height);
        data.spectrogram.forEach((frame, x) =>
          frame.forEach((db, y) => {
            const v = Math.max(0, Math.min(1, (db + 100) / 100));
            const p = ((256 - y) * image.width + x) * 4;
            pixels.data[p] = Math.round(14 + 240 * v ** 2);
            pixels.data[p + 1] = Math.round(24 + 200 * v);
            pixels.data[p + 2] = Math.round(42 + 90 * Math.sin(v * Math.PI));
            pixels.data[p + 3] = 255;
          }),
        );
        ic.putImageData(pixels, 0, 0);
        ctx.drawImage(image, 0, 0, w, h);
      } else if (data) {
        ctx.strokeStyle = selected ? '#51d8c5' : '#82aab6';
        data.peaks.forEach(([min, max], i) => {
          ctx.beginPath();
          ctx.moveTo((i / data.peaks.length) * w, h / 2 - max * h * 0.47);
          ctx.lineTo((i / data.peaks.length) * w, h / 2 - min * h * 0.47);
          ctx.stroke();
        });
      } else if (contentGuide?.length) {
        ctx.fillStyle = '#5e7f91';
        const gap = Math.min(2, w / contentGuide.length / 3);
        contentGuide.forEach((active, i) => {
          if (!active) return;
          const x = (i / contentGuide.length) * w;
          const bw = w / contentGuide.length;
          ctx.fillRect(x + gap / 2, h * 0.3, Math.max(1, bw - gap), h * 0.4);
        });
        ctx.fillStyle = '#9db1c3';
        ctx.font = '12px sans-serif';
        ctx.fillText('共享内容提示 · 不显示候选波形', 12, 18);
      } else {
        ctx.fillStyle = '#8da2b7';
        ctx.font = '14px sans-serif';
        ctx.fillText('独立试听 · 图形与指标已隐藏', 18, h / 2 + 5);
      }
      if (visibleRegion) {
        const x = (region[0] / duration) * w,
          rw = ((region[1] - region[0]) / duration) * w;
        ctx.fillStyle = '#5ee6cf20';
        ctx.fillRect(x, 0, rw, h);
        ctx.strokeStyle = '#60dac4';
        ctx.strokeRect(x, 0, rw, h);
      }
    };
    draw();
    const observer = new ResizeObserver(draw);
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [data, spectrum, selected, duration, region, contentGuide, visibleRegion]);
  const timeAt = (event: React.PointerEvent) => {
    const r = ref.current!.getBoundingClientRect();
    return Math.max(0, Math.min(duration, ((event.clientX - r.left) / r.width) * duration));
  };
  return (
    <canvas
      aria-label={
        contentGuide?.length
          ? '共享内容提示；单击跳转，拖动选择，双击清除选区'
          : '音频时间轴；单击跳转，拖动选择，双击清除选区'
      }
      data-selection-visible={visibleRegion ? 'true' : 'false'}
      ref={ref}
      className="waveform"
      onPointerDown={(e) => {
        down.current = timeAt(e);
        moved.current = false;
        e.currentTarget.setPointerCapture(e.pointerId);
      }}
      onPointerMove={(e) => {
        if (down.current !== null) {
          const current = timeAt(e);
          if (Math.abs(current - down.current) > 0.01) moved.current = true;
          if (moved.current)
            onRegion([Math.min(down.current, current), Math.max(down.current, current)]);
        }
      }}
      onPointerUp={(e) => {
        if (down.current !== null) {
          const end = timeAt(e);
          if (moved.current) onRegion([Math.min(end, down.current), Math.max(end, down.current)]);
          else onSeek(end);
        }
        down.current = null;
        moved.current = false;
      }}
      onPointerCancel={() => {
        down.current = null;
        moved.current = false;
      }}
      onDoubleClick={() => onRegion([0, duration])}
    />
  );
}
