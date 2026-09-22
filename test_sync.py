"""安全汇入测试：哈希链防篡改、序列缺口、幂等重放、评估对账、设备缺口报告。"""

import copy
import unittest

from edge import EdgeNode
from protocol import default_registry
from sync import SyncError, SyncServer

W = 60_000
LATENESS = 15_000


def build_node(protocol_id="heat@2026.09.01"):
    registry = default_registry()
    protocol = registry.get(protocol_id)
    node = EdgeNode("EDGE-X", protocol, ["P-01"])
    return registry, node


def feed_window(node, w, temp):
    sid = "P-01"
    for i in range(6):
        node.feed(sid, {"device_sn": "DEV-A", "seq": w * 6 + i + 1,
                        "ts": w * W + i * 10_000,
                        "rx_ts": w * W + i * 10_000 + 500,
                        "channels": {"core_temp": temp, "spo2": 96.0,
                                     "eeg": 30.0}})
    node.tick(w * W + W + LATENESS)


class SyncSecurityTest(unittest.TestCase):
    def setUp(self):
        self.registry, self.node = build_node()
        self.server = SyncServer(self.registry)
        feed_window(self.node, 0, 39.7)

    def _batch(self):
        return self.node.export_batch()

    def test_happy_path_reconciles_and_advances_head(self):
        report = self.server.apply_batch(self._batch())
        self.assertEqual(report["applied"], 1)
        self.assertEqual(report["assessments_reconciled"], 1)
        self.assertEqual(report["head_seq"], 1)
        self.assertEqual(self.server.synced_payloads()[0]["record_type"],
                         "assessment")

    def test_full_replay_is_idempotent(self):
        batch = self._batch()
        self.server.apply_batch(batch)
        again = self.server.apply_batch(batch)
        self.assertEqual(again["applied"], 0)
        self.assertEqual(again["duplicate_replays"], 1)
        self.assertEqual(len(self.server.synced_payloads()), 1)

    def test_replay_with_altered_content_rejected(self):
        self.server.apply_batch(self._batch())
        bad = copy.deepcopy(self._batch())
        bad["records"][0]["payload"]["payload"]["recommended_action"] = "转运"
        with self.assertRaises(SyncError):
            self.server.apply_batch(bad)

    def test_sequence_gap_rejected_and_not_fabricated(self):
        # 服务端先收到 seq 1
        first = self.node.export_batch(max_records=1)
        self.server.apply_batch(first)
        self.node.acknowledge(1)
        # 再产生两个窗口：出站 seq 2、3；发送时抽掉 seq 2
        feed_window(self.node, 1, 37.0)
        feed_window(self.node, 2, 37.0)
        later = self.node.export_batch()
        self.assertEqual([r["seq"] for r in later["records"]], [2, 3])
        later["records"] = later["records"][1:]
        later["start_seq"] = 3
        with self.assertRaises(SyncError):
            self.server.apply_batch(later)

    def test_tampered_hash_chain_rejected(self):
        batch = self._batch()
        batch["records"][0]["payload"]["payload"]["features"]["core_temp"]["mean"] = 30.0
        with self.assertRaises(SyncError):
            self.server.apply_batch(batch)

    def test_tampered_score_rejected_by_reconciliation(self):
        batch = self._batch()
        # 把动作改成转运但保留有效哈希结构（直接改 hash 链内一致无法骗过，
        # 因此同时改 prev/hash 也没用——这里验证规则对账拒绝伪造命中）
        payload = batch["records"][0]["payload"]["payload"]
        payload["hits"].append({"channel": "core_temp", "metric": "max",
                                "op": ">=", "threshold": 40.5,
                                "severity": "evacuate", "observed": 40.5})
        with self.assertRaises(SyncError):
            self.server.apply_batch(batch)

    def test_wrong_protocol_fingerprint_rejected(self):
        batch = self._batch()
        batch["protocol_fingerprint"] = "deadbeef"
        with self.assertRaises(SyncError):
            self.server.apply_batch(batch)

    def test_batch_acceptance_is_atomic(self):
        feed_window(self.node, 1, 37.0)
        batch = self.node.export_batch()
        # 破坏第二条，整批必须回滚
        batch["records"][1]["payload"]["payload"]["severity"] = "evacuate"
        with self.assertRaises(SyncError):
            self.server.apply_batch(batch)
        self.assertEqual(len(self.server.synced_payloads()), 0)

    def test_device_sequence_gaps_are_reported_not_filled(self):
        # 独立节点：DEV-G 缺 seq 3，DEV-H 连续，窗口可正常定稿
        registry, node = build_node()
        server = SyncServer(registry)
        for seq, ts in ((1, 0), (2, 10_000), (4, 30_000)):
            node.feed("P-01", {"device_sn": "DEV-G", "seq": seq, "ts": ts,
                               "rx_ts": ts + 500,
                               "channels": {"core_temp": 37.0, "spo2": 97.0,
                                            "eeg": 30.0}})
        for i in range(6):
            ts = i * 10_000
            node.feed("P-01", {"device_sn": "DEV-H", "seq": i + 1, "ts": ts,
                               "rx_ts": ts + 500,
                               "channels": {"core_temp": 37.0, "spo2": 97.0,
                                            "eeg": 30.0}})
        node.tick(W + LATENESS)
        report = server.apply_batch(node.export_batch())
        coverage = report["device_coverage"]
        self.assertEqual(coverage["DEV-G"]["gaps"], [[3, 3]])
        self.assertEqual(coverage["DEV-G"]["segments"], [[1, 2], [4, 4]])
        self.assertEqual(coverage["DEV-H"]["gaps"], [])


if __name__ == "__main__":
    unittest.main()
