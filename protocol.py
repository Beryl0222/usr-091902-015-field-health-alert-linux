"""任务前冻结协议：阈值、设备校准、风险规则与模型版本。

冻结后的协议是不可变对象：任何字段变化都会改变指纹，边缘设备只携带冻结副本
离线评估。阈值更新只能产生新版本，绝不改写已经作出的旧判断（旧判断记录了
当时的协议指纹）。
"""

import copy
from dataclasses import dataclass, field

from store import digest

MODEL_VERSION = "vitals-risk-1.3.0"


@dataclass(frozen=True)
class FrozenProtocol:
    protocol_id: str            # 方案名@版本，如 heat@2026.09.01
    scenario: str               # 场景：高温 / 高原
    model_version: str
    feature_window_ms: int      # 输入窗口长度
    allowed_lateness_ms: int    # 窗口定稿宽限；超过宽限到达的数据只能作补充
    alert_min_confidence: float # 低于该置信度时高危命中降级为现场复核
    calibrations: dict          # channel -> {scale, offset, valid_min, valid_max}
    rules: dict                 # channel -> [{metric, op, value, severity}]
    features: dict              # 特征参数，如 eeg burst_level
    conflict_tolerance: dict    # channel -> 同窗口多设备读数允许的最大极差
    clock_jitter_ms: int        # 序列号递增但时间戳回退的容忍抖动
    penalties: dict             # 数据质量问题对置信度的扣减
    fingerprint: str = field(default="")

    def to_dict(self):
        data = {
            "protocol_id": self.protocol_id,
            "scenario": self.scenario,
            "model_version": self.model_version,
            "feature_window_ms": self.feature_window_ms,
            "allowed_lateness_ms": self.allowed_lateness_ms,
            "alert_min_confidence": self.alert_min_confidence,
            "calibrations": self.calibrations,
            "rules": self.rules,
            "features": self.features,
            "conflict_tolerance": self.conflict_tolerance,
            "clock_jitter_ms": self.clock_jitter_ms,
            "penalties": self.penalties,
        }
        # 指纹只覆盖协议实质内容，不含指纹自身
        return data

    @classmethod
    def freeze(cls, spec):
        spec = copy.deepcopy(spec)
        spec.setdefault("model_version", MODEL_VERSION)
        content = {k: v for k, v in spec.items() if k != "fingerprint"}
        return cls(**spec, fingerprint=digest(content))

    def calibration_fingerprint(self):
        return digest(self.calibrations)

    def threshold_fingerprint(self):
        return digest({"rules": self.rules, "features": self.features})


# ---------------------------------------------------------------------------
# 高温方案：核心体温为主线，血氧与脑电为辅
# ---------------------------------------------------------------------------
HEAT_SPEC = {
    "protocol_id": "heat@2026.09.01",
    "scenario": "高温",
    "model_version": MODEL_VERSION,
    "feature_window_ms": 60_000,
    "allowed_lateness_ms": 15_000,
    "alert_min_confidence": 0.55,
    "calibrations": {
        "core_temp": {"scale": 1.0, "offset": 0.0,
                      "valid_min": 30.0, "valid_max": 43.0},
        "spo2": {"scale": 1.0, "offset": 0.0,
                 "valid_min": 50.0, "valid_max": 100.0},
        "eeg": {"scale": 1.0, "offset": 0.0,
                "valid_min": 0.0, "valid_max": 500.0},
    },
    "rules": {
        "core_temp": [
            {"metric": "mean", "op": ">=", "value": 38.5, "severity": "review"},
            {"metric": "mean", "op": ">=", "value": 39.5, "severity": "cool"},
            {"metric": "max", "op": ">=", "value": 40.5, "severity": "evacuate"},
        ],
        "spo2": [
            {"metric": "mean", "op": "<", "value": 94.0, "severity": "review"},
            {"metric": "min", "op": "<", "value": 90.0, "severity": "cool"},
            {"metric": "min", "op": "<", "value": 85.0, "severity": "evacuate"},
        ],
        "eeg": [
            {"metric": "burst_count", "op": ">=", "value": 3, "severity": "review"},
            {"metric": "burst_count", "op": ">=", "value": 8, "severity": "cool"},
            {"metric": "burst_count", "op": ">=", "value": 15, "severity": "evacuate"},
        ],
    },
    "features": {"eeg": {"burst_level": 80.0}},
    "conflict_tolerance": {"core_temp": 0.8, "spo2": 4.0, "eeg": 40.0},
    "clock_jitter_ms": 2_000,
    "penalties": {
        "sensor_missing": 0.2,
        "reading_conflict": 0.25,
        "clock_drift": 0.15,
        "out_of_range_ratio": 0.3,
        "late_beyond_cutoff": 0.1,
    },
}

# ---------------------------------------------------------------------------
# 高原方案：低氧为主线，脑电提示高原脑病，体温阈值放宽
# ---------------------------------------------------------------------------
ALTITUDE_SPEC = {
    "protocol_id": "altitude@2026.09.01",
    "scenario": "高原",
    "model_version": MODEL_VERSION,
    "feature_window_ms": 60_000,
    "allowed_lateness_ms": 15_000,
    "alert_min_confidence": 0.55,
    "calibrations": {
        "core_temp": {"scale": 1.0, "offset": 0.0,
                      "valid_min": 28.0, "valid_max": 43.0},
        "spo2": {"scale": 1.0, "offset": 0.0,
                 "valid_min": 40.0, "valid_max": 100.0},
        "eeg": {"scale": 1.0, "offset": 0.0,
                "valid_min": 0.0, "valid_max": 500.0},
    },
    "rules": {
        "spo2": [
            {"metric": "mean", "op": "<", "value": 90.0, "severity": "review"},
            {"metric": "min", "op": "<", "value": 85.0, "severity": "cool"},
            {"metric": "min", "op": "<", "value": 80.0, "severity": "evacuate"},
        ],
        "eeg": [
            {"metric": "burst_count", "op": ">=", "value": 3, "severity": "review"},
            {"metric": "burst_count", "op": ">=", "value": 8, "severity": "cool"},
            {"metric": "burst_count", "op": ">=", "value": 12, "severity": "evacuate"},
        ],
        "core_temp": [
            {"metric": "mean", "op": ">=", "value": 38.5, "severity": "review"},
            {"metric": "max", "op": ">=", "value": 40.0, "severity": "cool"},
        ],
    },
    "features": {"eeg": {"burst_level": 80.0}},
    "conflict_tolerance": {"core_temp": 0.8, "spo2": 4.0, "eeg": 40.0},
    "clock_jitter_ms": 2_000,
    "penalties": {
        "sensor_missing": 0.2,
        "reading_conflict": 0.25,
        "clock_drift": 0.15,
        "out_of_range_ratio": 0.3,
        "late_beyond_cutoff": 0.1,
    },
}


class ProtocolRegistry:
    """冻结方案登记簿：新版本不影响任何已冻结方案。"""

    def __init__(self):
        self._protocols = {}

    def freeze(self, spec):
        protocol = FrozenProtocol.freeze(spec)
        existing = self._protocols.get(protocol.protocol_id)
        if existing is not None and existing.fingerprint != protocol.fingerprint:
            raise ValueError(
                f"协议 {protocol.protocol_id} 已冻结且内容不同；阈值更新必须升版本号"
            )
        self._protocols[protocol.protocol_id] = protocol
        return protocol

    def get(self, protocol_id):
        try:
            return self._protocols[protocol_id]
        except KeyError:
            raise KeyError(f"未找到冻结协议：{protocol_id}")

    def ids(self):
        return tuple(self._protocols)


def default_registry():
    registry = ProtocolRegistry()
    registry.freeze(HEAT_SPEC)
    registry.freeze(ALTITUDE_SPEC)
    return registry
