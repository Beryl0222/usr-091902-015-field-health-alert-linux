"""处置阶梯、军医覆盖留痕、角色最小视图与"新版本不重写旧判断"测试。"""

import unittest

from domain import (
    ROLE_COMMANDER,
    ROLE_FIELD_MEDIC,
    ROLE_MEDIC,
    ROLE_WEARER,
    STATE_ALERTED,
    STATE_CLEARED,
    STATE_INTERVENING,
    STATE_MONITORING,
    STATE_REVIEW,
)
from oversight import ACTION_CLEAR, Oversight, OversightError


def assessment(window, action, severity=None, confidence=0.9, hits=None,
               extra_flags=None):
    severity = severity or {
        "现场复核": "review", "降温补水": "cool", "转运": "evacuate"
    }.get(action)
    return {
        "record_type": "assessment",
        "subject_id": "P-01",
        "window_start": window,
        "window_end": window + 60_000,
        "protocol_id": "heat@2026.09.01",
        "model_version": "vitals-risk-1.3.0",
        "protocol_fingerprint": "FP-OLD",
        "calibration_fingerprint": "CAL-OLD",
        "threshold_fingerprint": "TH-OLD",
        "severity": severity,
        "recommended_action": action,
        "confidence": confidence,
        "confidence_downgraded": False,
        "quality_flags": extra_flags or [],
        "hits": hits if hits is not None else [],
        "basis_refs": ["规则 core_temp.mean>=39.5（cool）"],
    }


class EscalationTest(unittest.TestCase):
    def setUp(self):
        self.o = Oversight()

    def test_actions_escalate_in_frozen_order(self):
        self.o.apply_assessment(assessment(0, "现场复核"))
        self.assertEqual(self.o.status_of("P-01")["state"], STATE_REVIEW)
        self.o.apply_assessment(assessment(60_000, "降温补水"))
        self.assertEqual(self.o.status_of("P-01")["state"], STATE_INTERVENING)
        fired = self.o.apply_assessment(assessment(120_000, "转运"))
        self.assertEqual(fired["action"], "转运")
        self.assertEqual(self.o.status_of("P-01")["state"], STATE_ALERTED)

    def test_lower_or_equal_signals_do_not_downgrade(self):
        self.o.apply_assessment(assessment(0, "转运"))
        self.assertIsNone(self.o.apply_assessment(assessment(60_000, "现场复核")))
        self.assertIsNone(self.o.apply_assessment(assessment(120_000, "降温补水")))
        self.assertIsNone(self.o.apply_assessment(assessment(180_000, "转运")))
        self.assertEqual(self.o.status_of("P-01")["action"], "转运")

    def test_same_window_processed_once(self):
        a = assessment(0, "转运")
        self.o.apply_assessment(a)
        # 重复同步同一窗口（重放）不能产生第二条处置
        self.assertIsNone(self.o.apply_assessment(a))
        self.assertEqual(len(self.o.audit_log("P-01")), 1)

    def test_every_action_records_calibration_and_rule_basis(self):
        a = assessment(0, "降温补水", hits=[
            {"channel": "core_temp", "metric": "mean", "observed": 39.7,
             "threshold": 39.5, "op": ">=", "severity": "cool"}])
        fired = self.o.apply_assessment(a)
        basis = fired["trigger"]
        self.assertEqual(basis["calibration_fingerprint"], "CAL-OLD")
        self.assertEqual(basis["threshold_fingerprint"], "TH-OLD")
        self.assertEqual(basis["model_version"], "vitals-risk-1.3.0")
        self.assertEqual(basis["hits"][0]["observed"], 39.7)
        self.assertTrue(basis["basis_refs"])


class OverrideTest(unittest.TestCase):
    def setUp(self):
        self.o = Oversight()
        self.o.apply_assessment(assessment(0, "转运"))

    def test_override_requires_medic_identity_and_reason(self):
        with self.assertRaises(OversightError):
            self.o.override("P-01", "", "降温补水", "理由")
        with self.assertRaises(OversightError):
            self.o.override("P-01", "DOC-1", "降温补水", "   ")
        with self.assertRaises(OversightError):
            self.o.override("P-01", "DOC-1", "降温补水", None)

    def test_override_is_immutable_and_audited(self):
        rec = self.o.override("P-01", "DOC-7", "降温补水",
                              "物理降温 15 分钟后体温 38.8，暂缓转运")
        self.assertEqual(rec["source"], "medic_override")
        self.assertEqual(rec["state"], STATE_INTERVENING)
        self.assertEqual(rec["previous"]["action"], "转运")
        status = self.o.status_of("P-01")
        self.assertEqual(status["action"], "降温补水")
        self.assertEqual(status["reason"], "物理降温 15 分钟后体温 38.8，暂缓转运")
        self.assertEqual(status["actor"], "DOC-7")
        # 审计链同时保留自动升级与覆盖，覆盖不能抹掉旧记录
        kinds = [r["record_type"] for r in self.o.audit_log("P-01")]
        self.assertEqual(kinds, ["auto_action", "medic_override"])
        self.assertTrue(self.o.store.verify())

    def test_clear_requires_reason_and_sets_cleared(self):
        rec = self.o.override("P-01", "DOC-7", ACTION_CLEAR, "症状缓解，血氧恢复")
        self.assertEqual(rec["state"], STATE_CLEARED)
        self.assertIsNone(rec["action"])

    def test_new_high_risk_after_clear_can_reescalate(self):
        self.o.override("P-01", "DOC-7", ACTION_CLEAR, "症状缓解")
        fired = self.o.apply_assessment(assessment(60_000, "现场复核"))
        self.assertIsNotNone(fired)
        self.assertEqual(fired["action"], "现场复核")
        self.assertEqual(self.o.status_of("P-01")["state"], STATE_REVIEW)


class ThresholdVersionTest(unittest.TestCase):
    def test_old_judgments_keep_old_fingerprint_after_threshold_update(self):
        o = Oversight()
        o.apply_assessment(assessment(0, "降温补水"))
        # 任务结束后阈值方案升级：新窗口的更高风险评估带新指纹
        new = dict(assessment(60_000, "转运", severity="evacuate"))
        new["protocol_fingerprint"] = "FP-NEW"
        new["calibration_fingerprint"] = "CAL-NEW"
        new["threshold_fingerprint"] = "TH-NEW"
        o.apply_assessment(new)
        log = o.audit_log("P-01")
        self.assertEqual(log[0]["trigger"]["protocol_fingerprint"], "FP-OLD")
        self.assertEqual(log[1]["trigger"]["protocol_fingerprint"], "FP-NEW")
        # 旧记录的阈值/校准指纹没有被新版本覆盖
        self.assertEqual(log[0]["trigger"]["threshold_fingerprint"], "TH-OLD")
        self.assertEqual(log[0]["trigger"]["calibration_fingerprint"], "CAL-OLD")


class RoleViewTest(unittest.TestCase):
    def setUp(self):
        self.o = Oversight()
        self.o.apply_assessment(assessment(0, "降温补水", hits=[
            {"channel": "core_temp", "metric": "mean", "observed": 39.7,
             "threshold": 39.5, "op": ">=", "severity": "cool"}]))

    def test_commander_sees_only_duty_status(self):
        view = self.o.view_for(ROLE_COMMANDER)[0]
        self.assertEqual(set(view),
                         {"subject_id", "state", "required_action",
                          "since_window", "overridden"})
        self.assertNotIn("hits", view)
        self.assertNotIn("quality_flags", view)
        self.assertNotIn("audit", view)
        self.assertEqual(view["required_action"], "降温补水")

    def test_field_medic_sees_review_points_but_not_full_record(self):
        view = self.o.view_for(ROLE_FIELD_MEDIC)[0]
        self.assertEqual(view["review_points"][0]["channel"], "core_temp")
        self.assertIn("basis_refs", view)
        self.assertNotIn("audit", view)

    def test_medic_sees_full_audit_and_trigger(self):
        view = self.o.view_for(ROLE_MEDIC)[0]
        self.assertIn("audit", view)
        self.assertIn("trigger", view)
        self.assertEqual(view["current"]["state"], STATE_INTERVENING)

    def test_wearer_gets_only_instruction(self):
        view = self.o.view_for(ROLE_WEARER)[0]
        self.assertEqual(set(view), {"subject_id", "state", "instruction"})


if __name__ == "__main__":
    unittest.main()
