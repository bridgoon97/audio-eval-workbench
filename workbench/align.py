"""恒定整数采样延迟估计与活动段 RMS 匹配的纯算法模块。

口径为 0.6 系列工程判据，不得随测试结果放宽：
- 延迟：±1600 samples 搜索；前/中/后三窗口归一化互相关；窗口极差 ≤4；
  聚合峰 ≥0.55；峰旁瓣比 ≥1.5；lag 不触边界。正 lag 表示候选相对参考晚到
  N samples，应用时前移 N samples（端部补零/裁切，长度不变）。
- 响度：参考轨活动掩码（帧长 1024 / 跳步 512，阈值 = 参考帧 RMS 90 分位
  ×0.25，且 ≥1e-4 满刻度）；活动时长与覆盖按活动帧并集内非静音样本数计
  （帧重叠不重复计数），共同有效活动 ≥0.5 s 且覆盖 ≥10%；固定增益
  ≤±12 dB；应用后预测峰值 ≤−1 dBFS。全部计算以 1.0 = 0 dBFS 的 float64。
- 处理顺序固定：先整数采样对齐，再固定增益。不做时间拉伸、漂移修复、
  逐帧增益、重采样或任何逐帧调节。
"""

import numpy as np

MAX_LAG = 1600
MIN_WINDOW = 8000
WINDOW_COUNT = 3
LAG_TOLERANCE = 4
PEAK_MIN = 0.55
SIDELOBE_MARGIN = 8
SIDELOBE_MIN = 1.5

FRAME = 1024
HOP = 512
ACTIVITY_QUANTILE = 0.9
ACTIVITY_FACTOR = 0.25
ACTIVITY_FLOOR = 1e-4
MIN_COMMON_ACTIVE_SECONDS = 0.5
MIN_COVERAGE = 0.10
MAX_GAIN_DB = 12.0
MAX_PEAK = 10 ** (-1 / 20)
ENERGY_FLOOR = 1e-4
RMS_FLOOR = 1e-12

OK = "OK"
ERR_TOO_SHORT = "ERR_TOO_SHORT"
ERR_SILENT = "ERR_SILENT"
ERR_LOW_ENERGY = "ERR_LOW_ENERGY"
ERR_LOW_CORRELATION = "ERR_LOW_CORRELATION"
ERR_SIDELOBE = "ERR_SIDELOBE"
ERR_DRIFT = "ERR_DRIFT"
ERR_BOUNDARY = "ERR_BOUNDARY"
ERR_ACTIVITY_INSUFFICIENT = "ERR_ACTIVITY_INSUFFICIENT"
ERR_GAIN_TOO_LARGE = "ERR_GAIN_TOO_LARGE"
ERR_CLIPPING_RISK = "ERR_CLIPPING_RISK"
ERR_DELAY_NOT_APPLICABLE = "ERR_DELAY_NOT_APPLICABLE"

REASONS = {
    OK: "满足自动应用判据",
    ERR_TOO_SHORT: "有效材料不足（三窗口合计需 ≥1.5 秒、每窗口 ≥0.5 秒）",
    ERR_SILENT: "候选或参考存在全静音，无法分析",
    ERR_LOW_ENERGY: "部分窗口能量过低，有效活动不足",
    ERR_LOW_CORRELATION: "聚合归一化相关峰低于 0.55，两轨内容相关性不足",
    ERR_SIDELOBE: "相关峰旁瓣比不足，强周期信号存在多峰，恒定延迟不可靠",
    ERR_DRIFT: "各窗口 lag 极差超过 4 samples，疑似漂移或非恒定延迟",
    ERR_BOUNDARY: "估计延迟触及 ±1600 samples 搜索边界，真实延迟可能超出范围",
    ERR_ACTIVITY_INSUFFICIENT: "共同有效活动不足（需累计 ≥0.5 秒且覆盖 ≥10%）",
    ERR_GAIN_TOO_LARGE: "建议增益超过 ±12 dB 上限",
    ERR_CLIPPING_RISK: "应用增益后预测峰值超过 −1 dBFS，削波余量不足",
    ERR_DELAY_NOT_APPLICABLE: "对齐判据未通过，活动段 RMS 的比较位置不可靠",
}

LAG_SIGN_DEFINITION = "候选相对参考晚到 +N samples；应用时前移 N samples（端部补零/裁切，长度不变）"
ALGORITHM_NAME = "恒定整数采样对齐 + 活动段 RMS 匹配 v1"
PROCESSING_ORDER = "先整数采样对齐，再固定增益"


class Verdict:
    """单个判据组的结论；可序列化，reason_code 供测试与导出断言。"""

    def __init__(self, applicable, reason_code, **metrics):
        self.applicable = applicable
        self.reason_code = reason_code
        self.reason = REASONS[reason_code]
        self.metrics = metrics

    def as_dict(self):
        out = {"可应用": self.applicable, "拒绝码": None if self.applicable else self.reason_code}
        if not self.applicable:
            out["原因"] = self.reason
        out.update(self.metrics)
        return out


def _fail(code, **metrics):
    return Verdict(False, code, **metrics)


def shift_samples(x, lag):
    """候选相对参考晚到 +lag samples；应用时前移 lag samples，长度不变。"""
    x = np.asarray(x)
    out = np.zeros_like(x)
    n = len(x)
    if lag > 0:
        out[: n - lag] = x[lag:]
    elif lag < 0:
        out[-lag:] = x[: n + lag]
    else:
        out[:] = x
    return out


def apply_gain(x, gain_db):
    return np.asarray(x, dtype=np.float64) * (10 ** (gain_db / 20.0))


def _window_edges(n, max_lag):
    span = n - 2 * max_lag
    width = span // WINDOW_COUNT
    edges = [max_lag + i * width for i in range(WINDOW_COUNT)]
    edges.append(max_lag + WINDOW_COUNT * width)
    return edges, width


def estimate_delay(ref, cand, max_lag=MAX_LAG):
    """归一化互相关恒定延迟估计。返回 Verdict，metrics 含 lag 与诊断值。"""
    ref = np.asarray(ref, dtype=np.float64)
    cand = np.asarray(cand, dtype=np.float64)
    n = min(len(ref), len(cand))
    ref, cand = ref[:n], cand[:n]
    if n == 0 or not np.any(ref) or not np.any(cand):
        return _fail(ERR_SILENT)
    if n - 2 * max_lag < WINDOW_COUNT * MIN_WINDOW:
        return _fail(ERR_TOO_SHORT, 有效秒数=round((n - 2 * max_lag) / 16000, 3))
    edges, _ = _window_edges(n, max_lag)
    corr_sum = np.zeros(2 * max_lag + 1)
    norms_sum = np.zeros(2 * max_lag + 1)
    window_lags = []
    window_peaks = []
    window_ratios = []
    for w in range(WINDOW_COUNT):
        a, b = edges[w], edges[w + 1]
        ref_w = ref[a:b]
        if float(np.sqrt((ref_w @ ref_w) / len(ref_w))) < ENERGY_FLOOR:
            return _fail(ERR_LOW_ENERGY, 窗口=w + 1)
        seg = cand[a - max_lag : b + max_lag]
        windows = np.lib.stride_tricks.sliding_window_view(seg, len(ref_w))
        corr = windows @ ref_w
        norms = np.sqrt((windows * windows).sum(axis=1)) * np.sqrt(ref_w @ ref_w)
        corr_sum += corr
        norms_sum += norms
        per_window = corr / np.maximum(norms, 1e-30)
        k = int(np.argmax(per_window))
        window_lags.append(k - max_lag)
        window_peaks.append(float(per_window[k]))
        mask = np.ones(len(per_window), dtype=bool)
        mask[max(0, k - SIDELOBE_MARGIN) : k + SIDELOBE_MARGIN + 1] = False
        sidelobe_w = float(np.max(per_window[mask])) if mask.any() else 0.0
        window_ratios.append(window_peaks[-1] / max(sidelobe_w, 1e-12))
    ncc = corr_sum / np.maximum(norms_sum, 1e-30)
    k = int(np.argmax(ncc))
    lag = k - max_lag
    peak = float(ncc[k])
    mask = np.ones(len(ncc), dtype=bool)
    mask[max(0, k - SIDELOBE_MARGIN) : k + SIDELOBE_MARGIN + 1] = False
    sidelobe = float(np.max(ncc[mask])) if mask.any() else 0.0
    ratio = peak / max(sidelobe, 1e-12)
    drift = int(max(window_lags) - min(window_lags))
    metrics = {
        "lag": lag,
        "相关峰": round(peak, 4),
        "峰旁瓣比": round(ratio, 3),
        "窗口lag": window_lags,
        "窗口相关峰": [round(v, 4) for v in window_peaks],
        "窗口旁瓣比": [round(v, 3) for v in window_ratios],
        "极差": drift,
    }
    # 判定顺序：逐窗口先证明确实存在强相关内容（否则低相关信号的随机峰
    # 位置会被误报为漂移）；再以逐窗口旁瓣比排除强周期多峰（多峰是窗口内
    # 特性，聚合口径会被其他窗口的峰污染）；随后检查窗口位置一致性（漂移）；
    # 最后聚合峰与搜索边界。
    if min(window_peaks) < PEAK_MIN:
        return _fail(ERR_LOW_CORRELATION, **metrics)
    if min(window_ratios) < SIDELOBE_MIN:
        return _fail(ERR_SIDELOBE, **metrics)
    if drift > LAG_TOLERANCE:
        return _fail(ERR_DRIFT, **metrics)
    if peak < PEAK_MIN:
        return _fail(ERR_LOW_CORRELATION, **metrics)
    if abs(lag) >= max_lag:
        return _fail(ERR_BOUNDARY, **metrics)
    return Verdict(True, OK, **metrics)


def _frame_rms(x):
    if len(x) < FRAME:
        return np.zeros(0)
    count = 1 + (len(x) - FRAME) // HOP
    out = np.empty(count)
    for i in range(count):
        seg = x[i * HOP : i * HOP + FRAME]
        out[i] = np.sqrt((seg @ seg) / FRAME)
    return out


def estimate_gain(ref, cand, lag, delay_applicable):
    """参考活动掩码下的活动段 RMS 匹配增益（dB）。需要可靠的对齐位置。"""
    ref = np.asarray(ref, dtype=np.float64)
    cand = np.asarray(cand, dtype=np.float64)
    n = min(len(ref), len(cand))
    ref, cand = ref[:n], cand[:n]
    if not delay_applicable:
        return _fail(ERR_DELAY_NOT_APPLICABLE)
    aligned = shift_samples(cand, lag)
    ref_rms_frames = _frame_rms(ref)
    if not len(ref_rms_frames):
        return _fail(ERR_ACTIVITY_INSUFFICIENT)
    quantile = float(np.quantile(ref_rms_frames, ACTIVITY_QUANTILE))
    threshold = max(quantile * ACTIVITY_FACTOR, ACTIVITY_FLOOR)
    active = ref_rms_frames >= threshold
    # 不重复样本掩码：活动帧区间求并集（帧长 1024、跳步 512，重叠只计一次），
    # 剔除帧内静音样本（|样本| ≤ 1e-6），并限制在可比较区间内。
    mask = np.zeros(n, dtype=bool)
    for i in np.nonzero(active)[0]:
        start = i * HOP
        mask[start : start + FRAME] = True
    lo = max(0, -lag)
    hi = n - max(0, lag)
    comparable_span = max(0, hi - lo)
    span_mask = np.zeros(n, dtype=bool)
    if comparable_span:
        span_mask[lo:hi] = True
    effective = mask & span_mask & (np.abs(ref) > 1e-6)
    union_count = int(effective.sum())
    coverage = union_count / comparable_span if comparable_span else 0.0
    common_seconds = union_count / 16000.0
    metrics = {
        "建议增益db": 0.0,
        "活动门限": round(threshold, 6),
        "有效活动样本数": union_count,
        "可比较样本数": comparable_span,
        "覆盖": round(coverage, 4),
        "共同活动秒": round(common_seconds, 3),
    }
    if common_seconds < MIN_COMMON_ACTIVE_SECONDS or coverage < MIN_COVERAGE:
        return _fail(ERR_ACTIVITY_INSUFFICIENT, **metrics)
    positions = np.nonzero(effective)[0]
    ref_energy = float(ref[positions] @ ref[positions])
    cand_energy = float(aligned[positions] @ aligned[positions])
    gain_db = float(
        10 * np.log10(max(ref_energy, RMS_FLOOR) / max(cand_energy, RMS_FLOOR))
    )  # 不做钳制：超出 ±12 dB 必须原样进入拒绝判定。
    predicted = float(np.max(np.abs(aligned))) * (10 ** (gain_db / 20.0)) if len(aligned) else 0.0
    metrics["建议增益db"] = round(gain_db, 3)
    metrics["参考活动RMSdbfs"] = round(
        20 * np.log10(max(np.sqrt(ref_energy / union_count), RMS_FLOOR)), 2
    )
    metrics["候选活动RMSdbfs"] = round(
        20 * np.log10(max(np.sqrt(cand_energy / union_count), RMS_FLOOR)), 2
    )
    metrics["预测峰值"] = round(predicted, 4)
    if abs(gain_db) > MAX_GAIN_DB:
        return _fail(ERR_GAIN_TOO_LARGE, **metrics)
    if predicted > MAX_PEAK:
        return _fail(ERR_CLIPPING_RISK, **metrics)
    return Verdict(True, OK, **metrics)
