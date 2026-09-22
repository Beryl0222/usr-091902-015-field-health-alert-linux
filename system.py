"""系统门面：把冻结协议登记簿、边缘节点、服务端同步与处置监督装配到一起。"""

from edge import EdgeNode
from oversight import Oversight
from protocol import default_registry
from sync import SyncServer
from store import AppendOnlyStore


class FieldSystem:
    def __init__(self):
        self.registry = default_registry()
        self.nodes = {}
        self.sync = SyncServer(self.registry)
        self.oversight = Oversight()
        self.audit = AppendOnlyStore(namespace="system")

    def register_node(self, node_sn, protocol_id, subject_ids=()):
        if node_sn in self.nodes:
            existing = self.nodes[node_sn]
            if existing.protocol.protocol_id != protocol_id:
                raise ValueError(f"节点 {node_sn} 已绑定另一个冻结协议")
            return existing
        protocol = self.registry.get(protocol_id)
        node = EdgeNode(node_sn, protocol, subject_ids)
        self.nodes[node_sn] = node
        self.audit.append("node_registered", {
            "node_sn": node_sn, "protocol_id": protocol_id,
            "protocol_fingerprint": protocol.fingerprint,
            "subject_ids": list(subject_ids),
        })
        return node

    def node(self, node_sn):
        try:
            return self.nodes[node_sn]
        except KeyError:
            raise KeyError(f"未注册的边缘节点：{node_sn}")

    def flush_node(self, node_sn, max_records=None):
        """导出待同步批次并汇入；新评估立即进入处置监督。返回同步报告。"""
        node = self.node(node_sn)
        batch = node.export_batch(max_records=max_records)
        if batch is None:
            return {"node_sn": node_sn, "applied": 0, "duplicate_replays": 0,
                    "assessments_reconciled": 0, "head_seq": None,
                    "device_coverage": self.sync.device_coverage(node_sn)}
        report = self.sync.apply_batch(batch)
        node.acknowledge(batch["end_seq"])

        actions = []
        for entry in self.sync.synced_payloads(node_sn):
            if entry["record_type"] == "assessment":
                fired = self.oversight.apply_assessment(entry["payload"])
                if fired is not None:
                    actions.append(fired)
        report["actions_triggered"] = [
            {"subject_id": a["subject_id"], "action": a["action"],
             "state": a["state"], "window_start": a["window_start"]}
            for a in actions
        ]
        self.audit.append("batch_synced", report)
        return report

    def replay_remaining(self, node_sn):
        """批次被 max_records 截断时循环汇入直至出站队列清空。"""
        reports = []
        while self.node(node_sn).pending_count() > 0:
            reports.append(self.flush_node(node_sn))
        return reports
