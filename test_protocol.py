"""冻结协议与版本治理测试。"""

import copy
import unittest

from protocol import (
    ALTITUDE_SPEC,
    HEAT_SPEC,
    MODEL_VERSION,
    FrozenProtocol,
    ProtocolRegistry,
)


class FrozenProtocolTest(unittest.TestCase):
    def test_fingerprint_covers_substance_only(self):
        p1 = FrozenProtocol.freeze(HEAT_SPEC)
        p2 = FrozenProtocol.freeze({**HEAT_SPEC})
        self.assertEqual(p1.fingerprint, p2.fingerprint)
        self.assertTrue(p1.fingerprint)

    def test_any_threshold_change_changes_fingerprint(self):
        base = FrozenProtocol.freeze(HEAT_SPEC)
        changed = copy.deepcopy(HEAT_SPEC)
        changed["rules"]["core_temp"][0]["value"] = 38.0
        self.assertNotEqual(base.fingerprint, FrozenProtocol.freeze(changed).fingerprint)

        changed2 = copy.deepcopy(HEAT_SPEC)
        changed2["calibrations"]["core_temp"]["offset"] = 0.2
        self.assertNotEqual(base.fingerprint, FrozenProtocol.freeze(changed2).fingerprint)

    def test_calibration_and_threshold_fingerprints_independent(self):
        p = FrozenProtocol.freeze(HEAT_SPEC)
        self.assertNotEqual(p.calibration_fingerprint(), p.threshold_fingerprint())
        self.assertEqual(p.model_version, MODEL_VERSION)

    def test_registry_rejects_same_id_with_changed_content(self):
        registry = ProtocolRegistry()
        registry.freeze(HEAT_SPEC)
        tampered = copy.deepcopy(HEAT_SPEC)
        tampered["rules"]["spo2"][0]["value"] = 90.0
        with self.assertRaises(ValueError):
            registry.freeze(tampered)

    def test_threshold_update_needs_new_version_and_keeps_old_frozen(self):
        registry = ProtocolRegistry()
        old = registry.freeze(HEAT_SPEC)
        updated = copy.deepcopy(HEAT_SPEC)
        updated["protocol_id"] = "heat@2026.12.01"
        updated["rules"]["core_temp"][0]["value"] = 38.2
        new = registry.freeze(updated)
        self.assertNotEqual(old.fingerprint, new.fingerprint)
        # 旧方案仍可原样取回——阈值更新不重写旧判断所依赖的冻结内容
        self.assertEqual(registry.get("heat@2026.09.01").fingerprint, old.fingerprint)
        self.assertEqual(len(registry.ids()), 2)

    def test_heat_and_altitude_thresholds_differ(self):
        heat = FrozenProtocol.freeze(HEAT_SPEC)
        altitude = FrozenProtocol.freeze(ALTITUDE_SPEC)
        self.assertNotEqual(heat.fingerprint, altitude.fingerprint)
        # 高原血氧阈值更严格、高温体温阈值更突出
        self.assertLess(altitude.rules["spo2"][0]["value"],
                        heat.rules["spo2"][0]["value"])


if __name__ == "__main__":
    unittest.main()
