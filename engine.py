"""边缘离线评估引擎。

设备在断网环境下接收生命体征样本，引擎严格按任务前冻结协议工作：

* 输入按固定窗口切分；乱序到达时，只要落在定稿宽限内就纳入窗口，
  定稿结果只取决于样本集合本身，与到达顺序无关；
* 重复样本按 (设备序列号, 序列号) 幂等去重；
* 时间漂移、传感器失联、读数矛盾、越界值只降低置信度并留痕，
  系统绝不插值、不挑选"顺眼"的设备来补造数据；
* 超过截止宽限才到达的样本不能改写已定稿判断，只追加迟到补充记录；
* 每条评估都带模型版本、输入窗口、协议/校准/阈值指纹和规则依据。
"""

from collections import defaultdict

from domain import (
    CHANNELS,
    Q_CLOCK_DRIFT,
    Q_LATE,
    Q_OUT_OF_RANGE,
    Q_READING_CONFLICT,
    Q_SENSOR_MISSING,
    SEVERITY_LEVEL,
    SEVERITY_ACTION,
)
from store import AppendOnlyStore


class EngineError(ValueError):
    pass


def _window_start(ts, window_ms):
    return (ts // window_ms) * window_ms


def _apply_op(value, op, threshold):
    if op == ">=":
        return value >= threshold
    if op == "<":
        return value < threshold
    if op == ">":
        return value > threshold
    if op == "<=":
        return value <= threshold
    raise EngineError(f"不支持的比较运算符：{op}")


class RiskEngine:
    def __init__(self, protocol, subject_id, store=None):
        self.protocol = protocol
        self.subject_id = subject_id
        self.store = store if store is not None else AppendOnlyStore(
            namespace=f"assessment:{subject_id}"
        )
        # window_start -> 样本列表（去重后、且在定稿截止前接收）
        self._buffers = defaultdict(list)
        self._seen = set()                     # (device_sn, seq)
        # 各窗口已定稿样本的 seq->ts（定稿时冻结），供迟到样本确定性漂移判定
        self._window_clock = defaultdict(lambda: defaultdict(dict))
        self._rx_watermark = None              # 接收钟水位线（由 tick 推进）
        self._finalized = set()

    # ------------------------------------------------------------------
    # 样本接入
    # ------------------------------------------------------------------
    def ingest(self, sample):
        """接入一条样本；返回其处理结果状态。

        accepted：在窗口定稿截止前接收，纳入窗口；
        duplicate：(设备序列号, 序列号) 重复，幂等忽略；
        late：超过截止宽限才接收，或窗口已定稿后才送到——只追加补充，
        不改写旧判断。

        窗口归属只取决于样本自身的接收时刻 rx_ts（边缘设备单调钟，
        默认等于采集时刻），与样本送入引擎的先后顺序无关；定稿本身只由
        tick() 的接收钟推进，不由数据内容触发。
        """
        self._validate(sample)
        device_sn = sample["device_sn"]
        seq = sample["seq"]
        ts = sample["ts"]
        rx_ts = sample.get("rx_ts", ts)
        key = (device_sn, seq)
        if key in self._seen:
            return {"status": "duplicate", "device_sn": device_sn, "seq": seq}

        window_ms = self.protocol.feature_window_ms
        w_start = _window_start(ts, window_ms)
        deadline = w_start + window_ms + self.protocol.allowed_lateness_ms
        self._seen.add(key)

        # 超过截止宽限（按样本自身接收时刻判定，与处理顺序无关）
        if rx_ts > deadline:
            drift = self._drift_against(w_start, device_sn, seq, ts)
            self._append_supplement(sample, w_start, rx_ts, drift,
                                    reason="beyond_cutoff")
            return {"status": "late", "device_sn": device_sn, "seq": seq,
                    "window_start": w_start, "reason": "beyond_cutoff"}

        # 物理上按时接收，但因网络重排在窗口定稿后才送到引擎
        if w_start in self._finalized:
            drift = self._drift_against(w_start, device_sn, seq, ts)
            self._append_supplement(sample, w_start, rx_ts, drift,
                                    reason="delivered_after_finalization")
            return {"status": "late", "device_sn": device_sn, "seq": seq,
                    "window_start": w_start,
                    "reason": "delivered_after_finalization"}

        self._buffers[w_start].append({
            "device_sn": device_sn,
            "seq": seq,
            "ts": ts,
            "rx_ts": rx_ts,
            "channels": dict(sample["channels"]),
        })
        return {"status": "accepted", "device_sn": device_sn, "seq": seq,
                "window_start": w_start}

    @staticmethod
    def _clock_drift(seq_ts, jitter_ms):
        """对一个设备的 {seq: ts} 映射做纯函数判定：序列号递增而时间戳回退。"""
        ordered = sorted(seq_ts.items())
        devices_drift = []
        for (prev_seq, prev_ts), (seq, ts) in zip(ordered, ordered[1:]):
            if seq > prev_seq and ts < prev_ts - jitter_ms:
                devices_drift.append(seq)
        return devices_drift

    def _drift_against(self, w_start, device_sn, seq, ts):
        """迟到样本相对定稿时冻结的相邻序列号做确定性漂移判定。"""
        frozen = self._window_clock[w_start][device_sn]
        jitter = self.protocol.clock_jitter_ms
        earlier = [s for s in frozen if s < seq]
        later = [s for s in frozen if s > seq]
        if earlier and ts < frozen[max(earlier)] - jitter:
            return True
        if later and ts > frozen[min(later)] + jitter:
            return True
        return False

    def tick(self, rx_now_ms):
        """推进边缘设备接收钟：定稿所有截止时刻已过的窗口。

        保证评估延迟上界 = 窗口长度 + 定稿宽限（事件时间口径）。
        """
        self._rx_watermark = rx_now_ms if self._rx_watermark is None else max(
            self._rx_watermark, rx_now_ms
        )
        window_ms = self.protocol.feature_window_ms
        for w_start in sorted(self._buffers):
            if w_start not in self._finalized and self._rx_watermark >= (
                w_start + window_ms + self.protocol.allowed_lateness_ms
            ):
                self._finalize(w_start)

    def ingest_many(self, samples):
        return [self.ingest(s) for s in samples]

    def _validate(self, sample):
        for field_name in ("device_sn", "seq", "ts", "channels"):
            if field_name not in sample:
                raise EngineError(f"样本缺少字段：{field_name}")
        if not isinstance(sample["channels"], dict):
            raise EngineError("channels 必须为对象")
        unknown = set(sample["channels"]) - set(CHANNELS)
        if unknown:
            raise EngineError(f"未知监测通道：{sorted(unknown)}")

    def _append_supplement(self, sample, w_start, rx_ts, drift, reason):
        record = self._build_supplement(sample, w_start, rx_ts, drift, reason)
        self.store.append("late_supplement", record)

    # ------------------------------------------------------------------
    # 窗口定稿
    # ------------------------------------------------------------------
    def close_pending(self):
        """任务结束或批处理收尾：定稿所有尚存窗口。"""
        for w_start in sorted(self._buffers):
            if w_start not in self._finalized:
                self._finalize(w_start)
        return self.timeline()

    def _finalize(self, w_start):
        window_ms = self.protocol.feature_window_ms
        samples = sorted(
            self._buffers.pop(w_start),
            key=lambda s: (s["ts"], s["device_sn"], s["seq"]),
        )
        # 冻结本窗口各设备 seq->ts，供迟到样本顺序无关地比对
        for s in samples:
            self._window_clock[w_start][s["device_sn"]][s["seq"]] = s["ts"]
        drift_map = {}
        for device_sn, seq_ts in self._window_clock[w_start].items():
            bad = self._clock_drift(seq_ts, self.protocol.clock_jitter_ms)
            if bad:
                drift_map[device_sn] = bad
        result = self._evaluate(w_start, w_start + window_ms, samples, drift_map)
        result["finalized_event_ms"] = w_start + window_ms + (
            self.protocol.allowed_lateness_ms
        )
        self._finalized.add(w_start)
        self.store.append("assessment", result)

    # ------------------------------------------------------------------
    # 窗口评估
    # ------------------------------------------------------------------
    def _evaluate(self, w_start, w_end, samples, drift_map=None):
        p = self.protocol
        quality_flags = []
        flag_detail = defaultdict(list)
        drift_map = drift_map or {}

        # 时钟漂移：定稿时基于窗口内样本集合（按序列号排序）判定，与喂入顺序无关
        if drift_map:
            quality_flags.append(Q_CLOCK_DRIFT)
            flag_detail[Q_CLOCK_DRIFT] = {
                sn: {"backward_at_seq": seqs}
                for sn, seqs in sorted(drift_map.items())
            }

        # 校准 + 合理域过滤；按通道收集 {device_sn: [values]}
        per_channel = {}
        for ch in p.calibrations:
            cal = p.calibrations[ch]
            devices = defaultdict(list)
            raw_total = 0
            oor = 0
            for s in samples:
                if ch not in s["channels"]:
                    continue
                raw_total += 1
                value = s["channels"][ch] * cal["scale"] + cal["offset"]
                if value < cal["valid_min"] or value > cal["valid_max"]:
                    oor += 1
                    continue
                devices[s["device_sn"]].append((s["ts"], value))
            per_channel[ch] = {"devices": devices, "raw_total": raw_total, "oor": oor}

        features = {}
        hits = []
        basis_refs = []
        penalty = 0.0

        for ch, info in per_channel.items():
            devices = info["devices"]

            # 传感器失联：窗口内该通道没有任何域内有效值
            if not devices:
                if info["raw_total"] == 0:
                    quality_flags.append(Q_SENSOR_MISSING)
                    flag_detail[Q_SENSOR_MISSING].append(ch)
                    penalty += p.penalties[Q_SENSOR_MISSING]
                else:
                    quality_flags.append(Q_OUT_OF_RANGE)
                    ratio = 1.0
                    flag_detail[Q_OUT_OF_RANGE].append(
                        {"channel": ch, "out_of_range": info["oor"],
                         "total": info["raw_total"]}
                    )
                    penalty += p.penalties["out_of_range_ratio"] * ratio
                continue

            # 越界比例（不补造：越界值剔除，不参与特征）
            if info["oor"]:
                quality_flags.append(Q_OUT_OF_RANGE)
                ratio = info["oor"] / info["raw_total"]
                flag_detail[Q_OUT_OF_RANGE].append(
                    {"channel": ch, "out_of_range": info["oor"],
                     "total": info["raw_total"], "ratio": round(ratio, 4)}
                )
                penalty += p.penalties["out_of_range_ratio"] * ratio

            # 多设备读数矛盾：同窗口各设备均值极差超过冻结容差
            device_means = {sn: sum(v for _, v in rows) / len(rows)
                            for sn, rows in devices.items()}
            if len(device_means) >= 2:
                spread = max(device_means.values()) - min(device_means.values())
                if spread > p.conflict_tolerance[ch]:
                    quality_flags.append(Q_READING_CONFLICT)
                    flag_detail[Q_READING_CONFLICT].append(
                        {"channel": ch, "spread": round(spread, 3),
                         "tolerance": p.conflict_tolerance[ch],
                         "device_means": {k: round(v, 3)
                                          for k, v in sorted(device_means.items())}}
                    )
                    penalty += p.penalties[Q_READING_CONFLICT]

            # 特征基于全部域内值（矛盾时不挑设备，保持可复核）
            all_values = [v for rows in devices.values() for _, v in rows]
            feat = {
                "mean": round(sum(all_values) / len(all_values), 3),
                "min": min(all_values),
                "max": max(all_values),
                "count": len(all_values),
            }
            if ch == "eeg":
                level = p.features["eeg"]["burst_level"]
                feat["burst_count"] = sum(1 for v in all_values if v >= level)
            features[ch] = feat

            # 规则命中
            for rule in p.rules.get(ch, []):
                observed = feat.get(rule["metric"])
                if observed is None:
                    continue
                if _apply_op(observed, rule["op"], rule["value"]):
                    hits.append({"channel": ch, "metric": rule["metric"],
                                 "op": rule["op"], "threshold": rule["value"],
                                 "severity": rule["severity"],
                                 "observed": observed})

        # 置信度：质量问题只做减法，下限为 0
        confidence = round(max(0.0, 1.0 - penalty), 3)

        # 最高严重级别决定建议动作；置信度不足时高危建议降为现场复核
        top = None
        if hits:
            top = max(hits, key=lambda h: SEVERITY_LEVEL[h["severity"]])["severity"]
        recommended = SEVERITY_ACTION[top] if top else None
        downgraded = False
        if top is not None and confidence < p.alert_min_confidence:
            recommended = "现场复核"
            downgraded = True

        # 依据：每条命中都能指回冻结规则与阈值指纹；特征可指回校准指纹
        for h in sorted(hits, key=lambda x: (x["channel"], x["metric"])):
            basis_refs.append(
                f"规则 {h['channel']}.{h['metric']}{h['op']}{h['threshold']}"
                f"（{h['severity']}，观测 {h['observed']}，"
                f"阈值指纹 {p.threshold_fingerprint()[:12]}）"
            )
        for ch in sorted(features):
            cal = p.calibrations[ch]
            basis_refs.append(
                f"校准 {ch} scale={cal['scale']} offset={cal['offset']}"
                f" 合理域[{cal['valid_min']},{cal['valid_max']}]"
                f"（校准指纹 {p.calibration_fingerprint()[:12]}）"
            )

        device_sns = sorted({s["device_sn"] for s in samples})
        seq_range = {sn: [None, None] for sn in device_sns}
        for s in samples:
            lo, hi = seq_range[s["device_sn"]]
            seq_range[s["device_sn"]] = [
                s["seq"] if lo is None else min(lo, s["seq"]),
                s["seq"] if hi is None else max(hi, s["seq"]),
            ]

        return {
            "record_type": "assessment",
            "subject_id": self.subject_id,
            "window_start": w_start,
            "window_end": w_end,
            "protocol_id": p.protocol_id,
            "model_version": p.model_version,
            "protocol_fingerprint": p.fingerprint,
            "input_window": {
                "start_ms": w_start,
                "end_ms": w_end,
                "window_ms": p.feature_window_ms,
                "sample_count": len(samples),
                "device_sns": device_sns,
                "seq_range": seq_range,
                "sample_ids": sorted(
                    f"{s['device_sn']}:{s['seq']}" for s in samples
                ),
            },
            "features": features,
            "hits": hits,
            "severity": top,
            "recommended_action": recommended,
            "confidence": confidence,
            "confidence_downgraded": downgraded,
            "quality_flags": sorted(set(quality_flags)),
            "quality_detail": dict(flag_detail),
            "basis_refs": basis_refs,
            "calibration_fingerprint": p.calibration_fingerprint(),
            "threshold_fingerprint": p.threshold_fingerprint(),
        }

    def _build_supplement(self, sample, w_start, rx_ts, drift, reason):
        cutoff = w_start + self.protocol.feature_window_ms + (
            self.protocol.allowed_lateness_ms
        )
        record = {
            "record_type": "late_supplement",
            "subject_id": self.subject_id,
            "window_start": w_start,
            "device_sn": sample["device_sn"],
            "seq": sample["seq"],
            "ts": sample["ts"],
            "rx_ts": rx_ts,
            "channels": sample["channels"],
            "reason": (Q_LATE if reason == "beyond_cutoff"
                       else "delivered_after_finalization"),
            "cutoff_ms": cutoff,
            "lateness_ms": max(0, rx_ts - cutoff),
            "protocol_id": self.protocol.protocol_id,
            "model_version": self.protocol.model_version,
            "note": ("超过定稿截止宽限才接收，仅补充留痕，不改写原评估"
                     if reason == "beyond_cutoff"
                     else "虽按时接收但网络重排至定稿后送达，仅补充留痕"),
        }
        if drift:
            record["clock_drift"] = True
        return record

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def timeline(self):
        """按窗口时间排序的完整风险时间线（评估 + 迟到补充）。"""
        items = self.store.payloads()
        return sorted(
            items,
            key=lambda r: (
                r.get("window_start", 0),
                0 if r["record_type"] == "assessment" else 1,
                r.get("seq", 0),
            ),
        )

    def assessments(self):
        return self.store.payloads("assessment")

    def supplements(self):
        return self.store.payloads("late_supplement")
