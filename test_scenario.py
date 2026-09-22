"""高温/高原既有事件切片回放：乱序重复一致性、动作阶梯、质量标记、同步审计。"""

import random
import unittest

from scenario import build_altitude, build_heat
from system import FieldSystem


def replay(d, build, mode="forward", seed=0):
    fs = FieldSystem()
    pid = d["protocol_id"]
    node = fs.register_node(f"NODE-{mode}-{seed}", pid, [d["subject_id"]])
    samples = [dict(s) for s in d["samples"]]
    if mode == "reverse":
        samples.reverse()
    elif mode == "shuffle":
        random.Random(seed).shuffle(samples)
    elif mode == "duplicated":
        # 全部样本再发一遍（完全重复）
        samples = samples + [dict(s) for s in samples]
    for s in samples:
        node.feed(d["subject_id"], s)
    node.tick(d["finalize_after_ms"])
    fs.flush_node(f"NODE-{mode}-{seed}")
    return node, fs


def signature(node, sid):
    return [(
        r["window_start"],
        r["recommended_action"],
        r["confidence"],
        tuple(sorted(r["quality_flags"])),
        tuple(r["input_window"]["sample_ids"]),
        tuple(sorted((h["channel"], h["metric"], h["severity"]) for h in r["hits"])),
        r["protocol_fingerprint"],
        r["model_version"],
    ) for r in node.engine(sid).timeline() if r["record_type"] == "assessment"]


class ScenarioReplayTest(unittest.TestCase):
    def assert_scenario(self, build):
        d = build()
        base_node, _ = replay(d, build, "forward")
        base = signature(base_node, d["subject_id"])

        actions = [(r["window_start"] // 60_000, r["recommended_action"])
                   for r in base_node.engine(d["subject_id"]).assessments()]
        expected = [(e["window_index"], e["recommended_action"])
                    for e in d["expected_actions"]]
        self.assertEqual(actions, expected)

        rev_node, _ = replay(d, build, "reverse")
        self.assertEqual(signature(rev_node, d["subject_id"]), base,
                         "逆序回放时间线不一致")

        dup_node, dup_fs = replay(d, build, "duplicated")
        self.assertEqual(signature(dup_node, d["subject_id"]), base,
                         "重复回放时间线不一致")

        for seed in range(5):
            sh_node, _ = replay(d, build, "shuffle", seed)
            self.assertEqual(signature(sh_node, d["subject_id"]), base,
                             f"乱序 seed {seed} 时间线不一致")

        # 同步后服务端记录数与边缘定稿数一致；重复样本不产生重复记录
        server_records = dup_fs.sync.synced_payloads(f"NODE-duplicated-0")
        assessments = [r for r in server_records
                       if r["record_type"] == "assessment"]
        self.assertEqual(len(assessments), 4)

    def test_heat_event(self):
        self.assert_scenario(build_heat)

    def test_altitude_event(self):
        self.assert_scenario(build_altitude)

    def test_quality_flags_match_scenario_design(self):
        for build in (build_heat, build_altitude):
            d = build()
            node, _ = replay(d, build, "forward")
            for r in node.engine(d["subject_id"]).assessments():
                idx = str(r["window_start"] // 60_000)
                expected = sorted(d["expected_quality"].get(idx, []))
                self.assertEqual(sorted(r["quality_flags"]), expected,
                                 f"{d['scenario']} 窗口 {idx} 质量标记不符")

    def test_late_samples_never_change_assessments(self):
        for build in (build_heat, build_altitude):
            d = build()
            node, _ = replay(d, build, "forward")
            supplements = node.engine(d["subject_id"]).supplements()
            self.assertGreaterEqual(len(supplements), 1)
            for s in supplements:
                self.assertEqual(s["record_type"], "late_supplement")
                self.assertIn(s["reason"],
                              ("late_beyond_cutoff", "delivered_after_finalization"))

    def test_full_pipeline_produces_escalating_dispositions(self):
        d = build_heat()
        _, fs = replay(d, build_heat, "forward")
        log = fs.oversight.audit_log(d["subject_id"])
        actions = [r["action"] for r in log]
        self.assertEqual(actions, ["现场复核", "降温补水", "转运"])
        # 所有触发动作都能说明校准与规则依据
        for r in log:
            self.assertTrue(r["trigger"]["basis_refs"])
            self.assertTrue(r["trigger"]["calibration_fingerprint"])
            self.assertTrue(r["trigger"]["threshold_fingerprint"])
        # 指挥员视图不含健康明细
        commander = fs.oversight.view_for("指挥人员", d["subject_id"])[0]
        self.assertNotIn("trigger", commander)
        self.assertEqual(commander["required_action"], "转运")


if __name__ == "__main__":
    unittest.main()
