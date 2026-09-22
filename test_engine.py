"""边缘评估引擎测试：乱序/重复一致性、延迟上界、只降置信度不补造、迟到不重写。"""

import random
import unittest

from domain import (
    Q_CLOCK_DRIFT,
    Q_OUT_OF_RANGE,
    Q_READING_CONFLICT,
    Q_SENSOR_MISSING,
)
from engine import RiskEngine
from protocol import FrozenProtocol, HEAT_SPEC
from store import ChainViolation

W = 60_000
LATENESS = 15_000


def make_engine():
    return RiskEngine(FrozenProtocol.freeze(HEAT_SPEC), "P-TEST-01")


def sample(seq, ts, channels, device_sn="DEV-A", rx_delay=500):
    return {"device_sn": device_sn, "seq": seq, "ts": ts,
            "rx_ts": ts + rx_delay, "channels": channels}


def feed_all(engine, samples):
    for s in samples:
        engine.ingest(s)
    engine.tick(W + LATENESS)


def one_window(engine, samples):
    feed_all(engine, samples)
    assessments = engine.assessments()
    assert len(assessments) == 1
    return assessments[0]


class EngineBasicsTest(unittest.TestCase):
    def test_normal_window_no_alert(self):
        samples = [sample(i + 1, i * 10_000,
                          {"core_temp": 37.0, "spo2": 97.0, "eeg": 30.0})
                   for i in range(6)]
        a = one_window(make_engine(), samples)
        self.assertIsNone(a["recommended_action"])
        self.assertEqual(a["confidence"], 1.0)
        self.assertEqual(a["quality_flags"], [])

    def test_alert_carries_model_version_window_and_basis(self):
        samples = [sample(i + 1, i * 10_000,
                          {"core_temp": 39.7, "spo2": 97.0, "eeg": 30.0})
                   for i in range(6)]
        a = one_window(make_engine(), samples)
        self.assertEqual(a["recommended_action"], "降温补水")
        self.assertEqual(a["model_version"],
                         FrozenProtocol.freeze(HEAT_SPEC).model_version)
        self.assertEqual(a["input_window"]["start_ms"], 0)
        self.assertEqual(a["input_window"]["end_ms"], W)
        self.assertEqual(a["input_window"]["window_ms"], W)
        self.assertTrue(a["protocol_fingerprint"])
        self.assertTrue(a["calibration_fingerprint"])
        self.assertTrue(a["threshold_fingerprint"])
        # 每条依据可指回规则阈值与校准
        joined = "\n".join(a["basis_refs"])
        self.assertIn("core_temp.mean>=39.5", joined)
        self.assertIn("校准 core_temp", joined)

    def test_calibration_scale_offset_applied(self):
        spec = {**HEAT_SPEC,
                "calibrations": {**HEAT_SPEC["calibrations"],
                                 "core_temp": {**HEAT_SPEC["calibrations"]["core_temp"],
                                               "scale": 1.0, "offset": 1.0}}}
        # 原始 37.6，加偏移 1.0 后 38.6 → 触发现场复核
        engine = RiskEngine(FrozenProtocol.freeze(spec), "P-TEST-01")
        samples = [sample(i + 1, i * 10_000,
                          {"core_temp": 37.6, "spo2": 97.0})
                   for i in range(6)]
        a = one_window(engine, samples)
        self.assertEqual(a["recommended_action"], "现场复核")
        self.assertAlmostEqual(a["features"]["core_temp"]["mean"], 38.6, places=2)


class OrderIndependenceTest(unittest.TestCase):
    """核心验收：乱序与重复送入，风险时间线在限定延迟内保持一致。"""

    def _dataset(self):
        rng = random.Random(7)
        samples = []
        seq = {"A": 0, "B": 0, "E": 0}
        for w in range(3):
            for i in range(6):
                ts = w * W + i * 10_000
                temp = 37.2 + w * 1.3
                seq["A"] += 1
                samples.append(sample(seq["A"], ts,
                                      {"core_temp": temp, "spo2": 96 - w * 5},
                                      "DEV-A"))
                seq["B"] += 1
                samples.append(sample(seq["B"], ts, {"core_temp": temp + 0.1},
                                      "DEV-B"))
                seq["E"] += 1
                samples.append(sample(seq["E"], ts, {"eeg": 30.0}, "DEV-E"))
        # 重复若干条
        samples += [dict(samples[2]), dict(samples[10])]
        return samples

    def _timeline(self, samples, mode, seed=0):
        engine = make_engine()
        data = [dict(s) for s in samples]
        if mode == "reverse":
            data.reverse()
        elif mode == "shuffle":
            random.Random(seed).shuffle(data)
        elif mode == "chunked":
            # 打乱块顺序、块内再逆序，模拟弱网分段到达
            chunks = [data[i:i + 4] for i in range(0, len(data), 4)]
            random.Random(seed).shuffle(chunks)
            data = [x for c in chunks for x in reversed(c)]
        for s in data:
            engine.ingest(s)
        engine.tick(3 * W + LATENESS)
        return [(r["window_start"], r["recommended_action"], r["confidence"],
                 tuple(r["quality_flags"]),
                 tuple(r["input_window"]["sample_ids"]),
                 [(h["channel"], h["metric"], h["severity"]) for h in r["hits"]])
                for r in engine.assessments()]

    def test_all_permutations_produce_identical_timeline(self):
        dataset = self._dataset()
        canonical = self._timeline(dataset, "forward")
        self.assertEqual(len(canonical), 3)
        self.assertEqual(self._timeline(dataset, "reverse"), canonical)
        for seed in range(8):
            self.assertEqual(self._timeline(dataset, "shuffle", seed), canonical,
                             f"shuffle seed {seed} 时间线不一致")
            self.assertEqual(self._timeline(dataset, "chunked", seed), canonical,
                             f"chunked seed {seed} 时间线不一致")

    def test_every_window_finalized_within_bounded_latency(self):
        engine = make_engine()
        samples = self._dataset()
        results = []
        for s in samples:
            results.append(engine.ingest(s))
        # 窗口必须在 事件窗末 + allowed_lateness 时已经定稿
        for w in range(3):
            engine.tick(w * W + W + LATENESS)
            starts = {a["window_start"] for a in engine.assessments()}
            self.assertIn(w * W, starts)
            a = next(x for x in engine.assessments() if x["window_start"] == w * W)
            self.assertEqual(a["finalized_event_ms"], w * W + W + LATENESS)
        self.assertEqual({r["status"] for r in results if r["status"] == "duplicate"},
                         {"duplicate"})
        self.assertEqual(sum(1 for r in results if r["status"] == "duplicate"), 2)


class QualityDegradesOnlyTest(unittest.TestCase):
    def test_missing_sensor_lowers_confidence_and_is_not_fabricated(self):
        # 只有体温；血氧、脑电窗口内完全无样本
        samples = [sample(i + 1, i * 10_000, {"core_temp": 39.7})
                   for i in range(6)]
        a = one_window(make_engine(), samples)
        self.assertIn(Q_SENSOR_MISSING, a["quality_flags"])
        self.assertLess(a["confidence"], 1.0)
        # 缺失通道绝不允许出现在特征里——没有就没有，不插值
        self.assertNotIn("spo2", a["features"])
        self.assertNotIn("eeg", a["features"])
        self.assertEqual(a["quality_detail"][Q_SENSOR_MISSING], ["spo2", "eeg"])

    def test_conflicting_readings_flagged_not_silently_chosen(self):
        samples = []
        for i in range(6):
            ts = i * 10_000
            samples.append(sample(100 + i, ts, {"core_temp": 39.8}, "DEV-A"))
            samples.append(sample(200 + i, ts, {"core_temp": 37.5}, "DEV-B"))
        a = one_window(make_engine(), samples)
        self.assertIn(Q_READING_CONFLICT, a["quality_flags"])
        detail = a["quality_detail"][Q_READING_CONFLICT][0]
        self.assertIn("DEV-A", detail["device_means"])
        self.assertIn("DEV-B", detail["device_means"])
        # 两个设备读数都被保留在特征里（均值约 38.65），没有挑"顺眼"的
        self.assertAlmostEqual(a["features"]["core_temp"]["mean"], 38.65, places=1)
        self.assertAlmostEqual(a["features"]["core_temp"]["max"], 39.8, places=1)

    def test_out_of_range_excluded_and_penalized(self):
        samples = [sample(i + 1, i * 10_000,
                          {"core_temp": 45.0 if i == 5 else 39.7,
                           "spo2": 97.0})
                   for i in range(6)]
        a = one_window(make_engine(), samples)
        self.assertIn(Q_OUT_OF_RANGE, a["quality_flags"])
        self.assertEqual(a["features"]["core_temp"]["count"], 5)

    def test_clock_drift_order_independent(self):
        samples = [sample(i + 1, i * 10_000,
                          {"core_temp": 39.7, "spo2": 97.0}) for i in range(6)]
        # 序号 5 的样本时间戳回退到序号 3 之前（超过抖动 2s）
        samples[4]["ts"] = 20_000

        def run(order):
            engine = make_engine()
            data = list(samples)
            if order == "reverse":
                data.reverse()
            for s in data:
                engine.ingest(s)
            engine.tick(W + LATENESS)
            a = engine.assessments()[0]
            return a["quality_flags"]

        self.assertEqual(run("forward"), run("reverse"))
        self.assertIn(Q_CLOCK_DRIFT, run("forward"))

    def test_low_confidence_downgrades_severe_action_to_review(self):
        # 体温 41.0 本应转运，但脑电与血氧失联（各扣 0.2）且双机体温矛盾
        # （扣 0.25）：置信度 0.35 < 0.55，高危建议只能降级为现场复核
        samples = []
        for i in range(6):
            ts = i * 10_000
            samples.append(sample(100 + i, ts, {"core_temp": 41.0}, "DEV-A"))
            samples.append(sample(200 + i, ts, {"core_temp": 37.0}, "DEV-B"))
        a = one_window(make_engine(), samples)
        self.assertIn(Q_READING_CONFLICT, a["quality_flags"])
        self.assertIn(Q_SENSOR_MISSING, a["quality_flags"])
        self.assertEqual(a["severity"], "evacuate")
        self.assertEqual(a["recommended_action"], "现场复核")
        self.assertTrue(a["confidence_downgraded"])
        self.assertLess(a["confidence"],
                        FrozenProtocol.freeze(HEAT_SPEC).alert_min_confidence)


class LateDataTest(unittest.TestCase):
    def test_late_sample_appends_supplement_without_rewriting(self):
        engine = make_engine()
        for i in range(6):
            engine.ingest(sample(i + 1, i * 10_000,
                                 {"core_temp": 39.7, "spo2": 97.0}))
        engine.tick(W + LATENESS)
        original = engine.assessments()[0]
        original_hash = engine.store.records("assessment")[0]["hash"]

        # 一条本可改变结论的高温迟到样本
        late = sample(99, 55_000, {"core_temp": 42.0}, rx_delay=40_000)
        result = engine.ingest(late)
        self.assertEqual(result["status"], "late")

        # 原评估内容与哈希原样不变
        self.assertEqual(engine.assessments()[0]["recommended_action"], "降温补水")
        self.assertEqual(engine.store.records("assessment")[0]["hash"], original_hash)
        supplements = engine.supplements()
        self.assertEqual(len(supplements), 1)
        self.assertEqual(supplements[0]["seq"], 99)
        self.assertEqual(supplements[0]["record_type"], "late_supplement")
        # 时间线中原评估在前、补充在后且不重排
        timeline = engine.timeline()
        self.assertEqual(timeline[0]["record_type"], "assessment")
        self.assertTrue(timeline[-1]["record_type"] in
                        ("late_supplement", "assessment"))


class ChainTest(unittest.TestCase):
    def test_hash_chain_detects_tampering(self):
        engine = make_engine()
        feed_all(engine, [sample(i + 1, i * 10_000, {"core_temp": 39.7})
                          for i in range(6)])
        self.assertTrue(engine.store.verify())
        # 篡改旧判断
        engine.store.records("assessment")[0]["payload"]["recommended_action"] = "转运"
        with self.assertRaises(ChainViolation):
            engine.store.verify()


if __name__ == "__main__":
    unittest.main()
