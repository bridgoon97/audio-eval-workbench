"""生成匿名试听资产和固定尺度的分析数据，不改变相对电平。"""

import hashlib
import io
import struct

import numpy as np
import soundfile as sf


def decode_audio(raw: bytes, channel: int = 0):
    try:
        info = sf.info(io.BytesIO(raw))
        if info.format not in ("WAV", "WAVEX"):
            raise ValueError("首版只接受 WAV 文件")
        if info.samplerate != 16000:
            raise ValueError("首版要求 16 kHz；请在外部显式转换并记录处理")
        if info.channels not in (1, 4) or not 0 <= channel < info.channels:
            raise ValueError("支持单通道或 4 通道 WAV，请正确选择通道")
        if not 160 <= info.frames <= 16000 * 120:
            raise ValueError("每个片段需为 0.01–120 秒，请先切分长录音")
        x, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
    except (sf.LibsndfileError, RuntimeError) as exc:
        raise ValueError("无法解码 WAV 文件") from exc
    x = x[:, channel]
    if not np.isfinite(x).all():
        raise ValueError("音频含 NaN 或 Inf，无法导入")
    # 显式编码单通道 IEEE float WAV，避免编码器 PEAK 块写入当前时间。
    # fact 块保存帧数；只含固定格式信息与原始浮点采样，输出可重复。
    payload = x.astype("<f4").tobytes()
    chunks = (
        b"fmt "
        + struct.pack("<IHHIIHH", 16, 3, 1, sr, sr * 4, 4, 32)
        + b"fact"
        + struct.pack("<II", 4, len(x))
        + b"data"
        + struct.pack("<I", len(payload))
        + payload
    )
    asset = b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks
    return asset, {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "asset_sha256": hashlib.sha256(asset).hexdigest(),
        "samples": len(x),
        "sample_rate": sr,
        "channel": channel,
        "source_channels": info.channels,
        "subtype": info.subtype,
        "peak": float(np.max(np.abs(x))),
        "rms_dbfs": float(
            20 * np.log10(max(float(np.sqrt(np.mean(x.astype(float) ** 2))), 1e-12))
        ),
        "clipped_samples": int(np.sum(np.abs(x) >= 0.999)),
        "dc_mean": float(np.mean(x)),
    }


def analyze(path):
    x, sr = sf.read(path, dtype="float32")
    blocks = np.array_split(x, min(900, len(x)))
    peaks = [[float(b.min()), float(b.max())] for b in blocks]
    # 因果 480 点 Hann 窗、512 点 FFT、160 点 hop；仅显示压缩时间分辨率。
    if len(x) < 480:
        x = np.pad(x, (0, 480 - len(x)))
    frames = np.lib.stride_tricks.sliding_window_view(x, 480)[::160]
    spectrum = np.abs(np.fft.rfft(frames * np.hanning(480), n=512, axis=1)) / 240
    db = 20 * np.log10(np.maximum(spectrum, 1e-6))
    step = max(1, int(np.ceil(len(db) / 500)))
    spec = [np.mean(db[i : i + step], axis=0) for i in range(0, len(db), step)]
    return {
        "peaks": peaks,
        "spectrogram": np.clip(spec, -100, 0).astype(int).tolist(),
        "sample_rate": sr,
        "n_fft": 512,
        "win": 480,
        "hop": 160,
        "display_step": step,
        "db_range": [-100, 0],
        "frame_time": "窗起点，未居中",
    }


def demo_wav(variant: int, sample: int):
    """纯数学合成音，不是真人录音，也不用于算法效果结论。"""
    sr = 16000
    t = np.arange(sr * 8) / sr
    rng = np.random.default_rng(107 + sample)
    envelope = np.maximum(0, np.sin(2 * np.pi * 0.72 * t)) ** 1.4
    f0 = 135 + sample * 37 + 12 * np.sin(2 * np.pi * 0.4 * t)
    phase = 2 * np.pi * np.cumsum(f0) / sr
    voice = sum(np.sin(k * phase) / k for k in range(1, 12)) * envelope * 0.14
    noise = rng.normal(0, 0.023, len(t))
    x = voice + noise
    if variant == 1:
        x = voice + noise * 0.25
        x[(t > 3.2) & (t < 3.65)] *= 0.35
    if variant == 2:
        x = voice * 0.95 + noise * 0.5
    result = io.BytesIO()
    sf.write(result, x, sr, format="WAV", subtype="PCM_16")
    return result.getvalue()
