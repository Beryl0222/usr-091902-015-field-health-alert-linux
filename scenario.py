"""既有高温、高原事件切片：确定性生成，供离线回放与一致性测试。

每个切片是同一批生命体征样本（含乱序、重复、失联、矛盾、时钟漂移、越界、
迟到的真实形态），生成结果固定，落盘为 fixtures 供反复回放。
"""

import argparse
import json
import os

from protocol import HEAT_SPEC, ALTITUDE_SPEC, FrozenProtocol

WINDOW_MS = 60_000
STEP_MS = 10_000
RX_DELAY_MS = 500  # 正常传输与处理延迟（小于定稿宽限 15s）


def _sample(device_sn, seq, ts, channels, rx_delay=RX_DELAY_MS):
    s = {"device_sn": device_sn, "seq": seq, "ts": ts,
         "rx_ts": ts + rx_delay, "channels": channels}
    return s


def _window_rows(base, seq_counter, rows):
    """rows: [(offset, device_sn, channels, rx_delay)]"""
    out = []
    for offset, device_sn, channels, rx_delay in rows:
        seq_counter[device_sn] = seq_counter.get(device_sn, 0) + 1
        out.append(_sample(device_sn, seq_counter[device_sn],
                           base + offset, channels, rx_delay))
    return out


def build_heat():
    """高温演训切片：体温逐级升高，脑电晚期暴发，伴随多种数据质量问题。"""
    p = FrozenProtocol.freeze(HEAT_SPEC)
    subject = "P-HEAT-01"
    A, B, E = "W-TEMP-A", "W-TEMP-B", "W-EEG-A"
    counter = {}
    samples = []

    # 窗口0（0-60s）：各项正常
    for off in range(0, WINDOW_MS, STEP_MS):
        samples += _window_rows(0, counter, [
            (off, A, {"core_temp": 37.1 + off / 600_000, "spo2": 97.5}, 500),
            (off, B, {"core_temp": 37.2}, 600),
            (off, E, {"eeg": 28.0}, 700),
        ])

    # 窗口1（60-120s）：体温均值越过 38.5 → 现场复核
    for i, off in enumerate(range(0, WINDOW_MS, STEP_MS)):
        samples += _window_rows(WINDOW_MS, counter, [
            (off, A, {"core_temp": 38.6 + i * 0.05, "spo2": 95.8}, 500),
            (off, B, {"core_temp": 38.5 + i * 0.05}, 800),
            (off, E, {"eeg": 35.0}, 700),
        ])
    # 迟到样本：物理上属于窗口1，但 rx 超过其定稿截止 15s 宽限
    counter[A] += 1
    samples.append(_sample(A, counter[A], WINDOW_MS + 55_000,
                           {"core_temp": 38.9, "spo2": 95.5},
                           rx_delay=40_000))

    # 窗口2（120-180s）：体温 39.6 级 + 血氧低值 + 脑电暴发 → 降温补水；
    # 含多设备矛盾（B 偏低 1.4°C）与一个越界读数
    for i, off in enumerate(range(0, WINDOW_MS, STEP_MS)):
        rows = [
            (off, A, {"core_temp": 39.6 + i * 0.03,
                      "spo2": 92.0 if i < 4 else 89.0}, 500),
            (off, B, {"core_temp": 38.2}, 900),   # 与 A 极差约 1.4 > 容差 0.8
            (off, E, {"eeg": 95.0 if i >= 1 else 40.0}, 700),  # 5 次暴发
        ]
        if i == 5:
            rows[1] = (off, B, {"core_temp": 45.0}, 900)       # 越界，剔除
        samples += _window_rows(2 * WINDOW_MS, counter, rows)

    # 窗口3（180-240s）：峰值体温 40.7 + 血氧 84 → 转运；
    # 脑电设备全程失联；A 设备出现一次时钟回退
    for i, off in enumerate(range(0, WINDOW_MS, STEP_MS)):
        ts_a = 3 * WINDOW_MS + off
        if i == 4:
            ts_a -= 15_000  # seq 增加而 ts 回退 15s > 抖动 2s → 时钟漂移
        counter[A] += 1
        samples.append(_sample(A, counter[A], ts_a,
                               {"core_temp": 40.2 + i * 0.1,
                                "spo2": 88.0 if i < 3 else 84.0}, 500))
        counter[B] += 1
        samples.append(_sample(B, counter[B], 3 * WINDOW_MS + off,
                               {"core_temp": 40.3 + i * 0.08}, 600))
        # E 设备本窗口无任何样本 → 传感器失联

    # 重复样本：重放窗口2的一条（边缘端应幂等忽略）
    dup = dict(samples[14])
    samples.append(dup)

    expected = [
        {"window_index": 0, "recommended_action": None},
        {"window_index": 1, "recommended_action": "现场复核"},
        {"window_index": 2, "recommended_action": "降温补水"},
        {"window_index": 3, "recommended_action": "转运"},
    ]
    return {
        "scenario": "高温",
        "protocol_id": p.protocol_id,
        "protocol_fingerprint": p.fingerprint,
        "subject_id": subject,
        "window_ms": WINDOW_MS,
        "finalize_after_ms": 4 * WINDOW_MS + p.allowed_lateness_ms,
        "samples": samples,
        "expected_actions": expected,
        "expected_quality": {
            "1": [],
            "2": ["reading_conflict", "out_of_range"],
            "3": ["sensor_missing", "clock_drift"],
        },
    }


def build_altitude():
    """高原演训切片：血氧逐级走低，脑电提示高原脑病风险。"""
    p = FrozenProtocol.freeze(ALTITUDE_SPEC)
    subject = "P-ALT-01"
    A, B, E = "W-SPO2-A", "W-SPO2-B", "W-EEG-A"
    counter = {}
    samples = []

    # 窗口0：血氧 93-96，无命中
    for off in range(0, WINDOW_MS, STEP_MS):
        samples += _window_rows(0, counter, [
            (off, A, {"spo2": 95.0, "core_temp": 36.9}, 500),
            (off, B, {"spo2": 94.5}, 600),
            (off, E, {"eeg": 30.0}, 700),
        ])
    # 窗口0 的迟到补充（rx 晚于截止 20s）
    counter[A] += 1
    samples.append(_sample(A, counter[A], 55_000,
                           {"spo2": 94.0, "core_temp": 37.0},
                           rx_delay=30_000))

    # 窗口1：血氧均值 <90、最低 86 → 现场复核（均值规则 review）
    for i, off in enumerate(range(0, WINDOW_MS, STEP_MS)):
        samples += _window_rows(WINDOW_MS, counter, [
            (off, A, {"spo2": 90.0 if i < 2 else 88.0,
                      "core_temp": 37.4}, 500),
            (off, B, {"spo2": 89.5 if i < 2 else 87.5}, 700),
            (off, E, {"eeg": 45.0}, 700),
        ])

    # 窗口2：最低 83 → 降温补水（<85），脑电 9 次暴发；双机血氧矛盾
    for i, off in enumerate(range(0, WINDOW_MS, STEP_MS)):
        rows = [
            (off, A, {"spo2": 86.0 if i < 3 else 83.0,
                      "core_temp": 37.6}, 500),
            (off, B, {"spo2": 91.0}, 800),   # 与 A 极差约 6 > 容差 4
            (off, E, {"eeg": 100.0 if i >= 2 else 40.0}, 700),  # 4 次？见下
        ]
        samples += _window_rows(2 * WINDOW_MS, counter, rows)
    # 追加脑电暴发使 burst_count 达 9（cool 阈值 8）；时间戳在窗口末端递增，
    # 避免制造虚假的时钟回退
    for j in range(5):
        counter[E] += 1
        samples.append(_sample(E, counter[E],
                               2 * WINDOW_MS + 51_000 + j * 1_000,
                               {"eeg": 120.0}, 700))

    # 窗口3：最低 78 → 转运；脑电 4 次暴发为复核辅证；体温设备失联；B 时钟漂移
    for i, off in enumerate(range(0, WINDOW_MS, STEP_MS)):
        counter[A] += 1
        # A 的体温通道本窗口缺席（只报血氧）→ core_temp 传感器失联
        samples.append(_sample(A, counter[A], 3 * WINDOW_MS + off,
                               {"spo2": 82.0 if i < 2 else 78.0}, 500))
        ts_b = 3 * WINDOW_MS + off
        if i == 3:
            ts_b -= 15_000  # seq 增加而 ts 回退超过抖动 → 时钟漂移
        counter[B] += 1
        samples.append(_sample(B, counter[B], ts_b, {"spo2": 81.0}, 600))
        counter[E] += 1
        eeg = 110.0 if i < 4 else 40.0
        samples.append(_sample(E, counter[E], 3 * WINDOW_MS + off,
                               {"eeg": eeg}, 700))

    expected = [
        {"window_index": 0, "recommended_action": None},
        {"window_index": 1, "recommended_action": "现场复核"},
        {"window_index": 2, "recommended_action": "降温补水"},
        {"window_index": 3, "recommended_action": "转运"},
    ]
    return {
        "scenario": "高原",
        "protocol_id": p.protocol_id,
        "protocol_fingerprint": p.fingerprint,
        "subject_id": subject,
        "window_ms": WINDOW_MS,
        "finalize_after_ms": 4 * WINDOW_MS + p.allowed_lateness_ms,
        "samples": samples,
        "expected_actions": expected,
        "expected_quality": {
            "1": [],
            "2": ["reading_conflict"],
            "3": ["sensor_missing", "clock_drift"],
        },
    }


SCENARIOS = {"heat": build_heat, "altitude": build_altitude}


def emit_fixtures(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for name, builder in SCENARIOS.items():
        data = builder()
        path = os.path.join(out_dir, f"{name}_event.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        print(f"写出 {path}（{len(data['samples'])} 条样本）")
    protocols = {
        "heat": FrozenProtocol.freeze(HEAT_SPEC).to_dict(),
        "altitude": FrozenProtocol.freeze(ALTITUDE_SPEC).to_dict(),
    }
    path = os.path.join(out_dir, "frozen_protocols.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(protocols, f, ensure_ascii=False, indent=2, sort_keys=True)
    print(f"写出 {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成事件切片 fixtures")
    parser.add_argument("--emit-fixtures", metavar="DIR", default=None)
    args = parser.parse_args()
    if args.emit_fixtures:
        emit_fixtures(args.emit_fixtures)
    else:
        for name, builder in SCENARIOS.items():
            d = builder()
            print(name, len(d["samples"]), "samples")
