"""恒定延迟与活动段 RMS 匹配判据的纯算法验证；信号全部运行时合成。"""

import numpy as np

from workbench.align import (
    ERR_ACTIVITY_INSUFFICIENT,
    ERR_BOUNDARY,
    ERR_CLIPPING_RISK,
    ERR_DELAY_NOT_APPLICABLE,
    ERR_DRIFT,
    ERR_GAIN_TOO_LARGE,
    ERR_LOW_CORRELATION,
    ERR_SIDELOBE,
    ERR_SILENT,
    OK,
    apply_gain,
    estimate_delay,
    estimate_gain,
    shift_samples,
)
from workbench.audio import encode_float_wav


def broadband(seed=7, n=96000, peak=0.5):
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    x = 0.35 * rng.standard_normal(n) + 0.35 * np.sin(
        2 * np.pi * (50 * t / 16000 + 1800 * (t / 16000) ** 2 / 2)
    )
    x /= np.max(np.abs(x))
    return x * peak


def shifted(ref, lag, gain_db=0.0, noise=0.0, seed=11):
    rng = np.random.default_rng(seed)
    out = np.zeros_like(ref)
    if lag > 0:
        out[lag:] = ref[:-lag] * (10 ** (gain_db / 20))
    else:
        out[: len(ref) + lag] = ref[-lag:] * (10 ** (gain_db / 20))
    return out + noise * rng.standard_normal(len(ref))


def test_positive_lag_detected_and_direction_fixed():
    ref = broadband()
    cand = shifted(ref, 320, 0.0, noise=0.001)
    v = estimate_delay(ref, cand)
    assert v.applicable and v.reason_code == OK
    assert abs(v.metrics["lag"] - 320) <= 1
    # 正 lag 表示候选晚到；应用＝前移 320 samples，重叠区归一化误差显著下降。
    aligned = shift_samples(cand, v.metrics["lag"])
    valid = slice(320, len(ref) - 320)
    before = np.linalg.norm(cand[valid] - ref[valid]) / np.linalg.norm(ref[valid])
    after = np.linalg.norm(aligned[valid] - ref[valid]) / np.linalg.norm(ref[valid])
    assert after < before * 0.2
    assert after < 0.05


def test_negative_lag_detected():
    ref = broadband(seed=8)
    cand = shifted(ref, -157, 0.0)
    v = estimate_delay(ref, cand)
    assert v.applicable
    assert abs(v.metrics["lag"] - (-157)) <= 1
    aligned = shift_samples(cand, v.metrics["lag"])
    valid = slice(157, len(ref) - 157)
    assert np.linalg.norm(aligned[valid] - ref[valid]) / np.linalg.norm(ref[valid]) < 0.05


def test_drift_windows_rejected_by_range_not_median():
    ref = broadband(seed=9)
    cand = np.zeros_like(ref)
    for start, end, lag in ((0, 31200, 100), (31200, 60800, 104), (60800, 96000, 109)):
        cand[start + lag : end] = ref[start : end - lag]
    v = estimate_delay(ref, cand)
    assert not v.applicable and v.reason_code == ERR_DRIFT
    assert v.metrics["极差"] == 9
    assert sorted(v.metrics["窗口lag"]) == [100, 104, 109]


def test_periodic_tone_rejected_by_sidelobe():
    t = np.arange(96000)
    sine = 0.5 * np.sin(2 * np.pi * 100 * t / 16000)
    cand = np.zeros_like(sine)
    cand[250:] = sine[:-250]
    v = estimate_delay(sine, cand)
    assert not v.applicable and v.reason_code == ERR_SIDELOBE


def test_low_correlation_noise_rejected():
    rng = np.random.default_rng(3)
    v = estimate_delay(rng.standard_normal(96000) * 0.5, rng.standard_normal(96000) * 0.5)
    assert not v.applicable and v.reason_code == ERR_LOW_CORRELATION


def test_silent_candidate_rejected():
    ref = broadband()
    v = estimate_delay(ref, np.zeros_like(ref))
    assert not v.applicable and v.reason_code == ERR_SILENT


def test_boundary_lag_rejected():
    ref = broadband(seed=5)
    cand = np.zeros_like(ref)
    cand[1600:] = ref[:-1600]
    v = estimate_delay(ref, cand)
    assert not v.applicable and v.reason_code == ERR_BOUNDARY
    assert v.metrics["lag"] == 1600


def test_beyond_search_range_rejected():
    ref = broadband(seed=6)
    cand = np.zeros_like(ref)
    cand[2000:] = ref[:-2000]
    v = estimate_delay(ref, cand)
    assert not v.applicable and v.reason_code == ERR_LOW_CORRELATION


def test_loudness_known_6db_measured_and_applied():
    ref = broadband(seed=10)
    cand = shifted(ref, 0, 6.0, noise=0.0005)
    g = estimate_gain(ref, cand, 0, True)
    assert g.applicable
    assert abs(g.metrics["建议增益db"] - (-6.0)) <= 0.2
    applied = apply_gain(cand, g.metrics["建议增益db"])
    frames = np.arange(1024, len(ref) - 1024, 512)
    ref_rms = np.sqrt(np.mean([ref[i : i + 1024] @ ref[i : i + 1024] / 1024 for i in frames]))
    cand_rms = np.sqrt(
        np.mean([applied[i : i + 1024] @ applied[i : i + 1024] / 1024 for i in frames])
    )
    assert abs(20 * np.log10(ref_rms / cand_rms)) <= 0.2


def test_activity_coverage_below_10pct_rejected():
    ref = np.zeros(96000)
    # 4 个 1024 样本的帧对齐突发：共同活动 8 帧（≥0.5 秒）但覆盖 <10%。
    for start in (24064, 44544, 65024, 85496):
        ref[start : start + 1024] = broadband(seed=start, n=1024, peak=0.4)
    cand = shifted(ref, 0, -1.0)
    d = estimate_delay(ref, cand)
    assert d.applicable
    g = estimate_gain(ref, cand, d.metrics["lag"], True)
    assert not g.applicable and g.reason_code == ERR_ACTIVITY_INSUFFICIENT
    assert g.metrics["覆盖"] < 0.10
    assert g.metrics["共同活动秒"] >= 0.5


def test_gain_over_12db_rejected():
    ref = broadband(seed=12, peak=0.15)
    cand = shifted(ref, 0, -13.0)
    d = estimate_delay(ref, cand)
    assert d.applicable
    g = estimate_gain(ref, cand, d.metrics["lag"], True)
    assert not g.applicable and g.reason_code == ERR_GAIN_TOO_LARGE
    assert abs(g.metrics["建议增益db"]) > 12


def test_clipping_risk_rejected():
    ref = broadband(seed=13, peak=0.05)
    cand = shifted(ref, 0, -11.5)
    cand[48000] = 0.5  # 孤立尖峰：活动段 RMS 很低但应用增益后会削波
    d = estimate_delay(ref, cand)
    assert d.applicable
    g = estimate_gain(ref, cand, d.metrics["lag"], True)
    assert not g.applicable and g.reason_code == ERR_CLIPPING_RISK
    assert g.metrics["预测峰值"] > 10 ** (-1 / 20)


def test_loudness_requires_applicable_delay():
    ref = broadband(seed=14)
    g = estimate_gain(ref, shifted(ref, 0, -2.0), 0, False)
    assert not g.applicable and g.reason_code == ERR_DELAY_NOT_APPLICABLE


def test_shift_preserves_length_and_pads_edges():
    x = np.arange(10, dtype=float)
    assert len(shift_samples(x, 3)) == 10
    # 晚到 +3：应用＝前移 3 samples，前端裁掉、末端补零。
    assert np.array_equal(shift_samples(x, 3), [3, 4, 5, 6, 7, 8, 9, 0, 0, 0])
    # 早到 -3：应用＝后移 3 samples，前端补零、末端裁掉。
    assert np.array_equal(shift_samples(x, -3), [0, 0, 0, 0, 1, 2, 3, 4, 5, 6])
    assert np.array_equal(shift_samples(x, 0), x)


def test_derived_encoding_is_deterministic():
    x = broadband(seed=15, n=16000)
    a = encode_float_wav(apply_gain(shift_samples(x, 10), -1.5))
    b = encode_float_wav(apply_gain(shift_samples(x, 10), -1.5))
    assert a == b


def test_loudness_uses_reference_mask_only():
    """活动掩码只来自参考轨：候选在参考静音区的额外响内容不得改变增益。"""
    rng = np.random.default_rng(21)
    n = 96000
    ref = np.zeros(n)
    cand = np.zeros(n)
    # 参考约 50% 时间活动（1 秒突发），候选为参考 ×2（目标增益 −6 dB）。
    for start in range(0, n - 16000, 32000):
        ref[start : start + 16000] = rng.standard_normal(16000) * 0.15
        cand[start + 320 : start + 16000] = ref[start : start + 15680] * 2
    # 候选在参考静音区放置更响的额外内容：参考掩码下不计入。
    cand[20000:26000] = rng.standard_normal(6000) * 0.28
    d = estimate_delay(ref, cand)
    assert d.applicable
    g = estimate_gain(ref, cand, d.metrics["lag"], True)
    assert g.applicable
    assert abs(g.metrics["建议增益db"] - (-6.0)) <= 0.2
