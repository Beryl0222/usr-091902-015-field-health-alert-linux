"""恢复连接后的安全汇入。

服务端对每个边缘节点维护独立的只追加账本，汇入时依次校验：

1. 批次声明的协议指纹必须仍是任务前冻结版本（阈值更新不会替换旧版本）；
2. 节点出站链逐记录重算哈希并比对前向链接，发现篡改立即拒绝整批；
3. node_seq 必须与服务端已收头部连续，出现缺口只报错、不猜测补缺；
4. 同一 source_hash 重放为幂等跳过；内容被改动但序号相同则拒绝；
5. 对评估做规则对账：命中项、严重级别、建议动作必须能由冻结规则重算复现。

全程不 interpolate 任何缺失数据；缺口与矛盾只形成报告。
"""

import hashlib

from domain import SEVERITY_LEVEL, SEVERITY_ACTION
from store import GENESIS, AppendOnlyStore, digest


class SyncError(ValueError):
    """批次未通过安全校验，整批拒绝。"""


class SyncServer:
    def __init__(self, registry):
        self.registry = registry
        self.ledger = AppendOnlyStore(namespace="sync-server")
        # node_sn -> {"head_seq": int, "head_hash": str, "hashes": set}
        self._nodes = {}

    # ------------------------------------------------------------------
    def apply_batch(self, batch):
        node_sn = batch.get("node_sn")
        records = batch.get("records")
        if not node_sn or not records:
            raise SyncError("批次缺少 node_sn 或 records")

        protocol = self.registry.get(batch.get("protocol_id"))
        if protocol.fingerprint != batch.get("protocol_fingerprint"):
            raise SyncError(
                f"节点 {node_sn} 使用的协议指纹与冻结登记簿不一致，拒绝汇入"
            )

        node = self._nodes.setdefault(
            node_sn, {"head_seq": 0, "head_hash": GENESIS, "hashes": set(),
                      "seq_hashes": {}}
        )

        applied = 0
        duplicates = 0
        reconciled = 0
        staged = []
        # 期望中的前向哈希：允许批次从更早的 seq 重放，重放段用已存链锚点校验
        prev_hash = node["head_hash"]
        expected_next = node["head_seq"] + 1

        for record in records:
            seq = record.get("seq")

            if seq <= node["head_seq"]:
                # 重放段：哈希字段必须与首次汇入一致，且内容必须能重算出该哈希
                if node["seq_hashes"].get(seq) != record.get("hash"):
                    raise SyncError(
                        f"节点 {node_sn} 第 {seq} 条重放哈希与首次汇入不一致，拒绝"
                    )
                self._rehash_check(record, record.get("prev_hash"),
                                   node_sn, seq)
                duplicates += 1
                continue

            if seq != expected_next:
                raise SyncError(
                    f"节点 {node_sn} 序列缺口：期望 {expected_next}，"
                    f"收到 {seq}；缺口数据不会被补造"
                )
            if record.get("prev_hash") != prev_hash:
                raise SyncError(
                    f"节点 {node_sn} 第 {expected_next} 条链断裂，疑似丢包或篡改"
                )
            self._rehash_check(record, prev_hash, node_sn, expected_next)

            payload = record["payload"]
            inner = payload["payload"]
            source_hash = payload.get("source_hash", record["hash"])
            if payload.get("protocol_id") != protocol.protocol_id:
                raise SyncError(
                    f"节点 {node_sn} 第 {expected_next} 条记录协议标识不匹配"
                )
            # 评估必须带与冻结登记簿一致的协议指纹；补充记录只锚定协议标识
            if (inner.get("record_type") == "assessment"
                    and inner.get("protocol_fingerprint") != protocol.fingerprint):
                raise SyncError(
                    f"节点 {node_sn} 第 {expected_next} 条评估协议指纹不匹配"
                )

            self._reconcile(inner, protocol, node_sn, expected_next)
            if payload["record_type"] == "assessment":
                reconciled += 1
            staged.append((record, payload, source_hash))

            expected_next += 1
            prev_hash = record["hash"]

        # 全部通过校验后才提交
        for record, payload, source_hash in staged:
            node["hashes"].add(source_hash)
            node["seq_hashes"][record["seq"]] = record["hash"]
            self.ledger.append("synced_record", {
                "node_sn": node_sn,
                "node_seq": record["seq"],
                "source_hash": source_hash,
                "subject_id": payload["subject_id"],
                "record_type": payload["record_type"],
                "window_start": payload["window_start"],
                "payload": payload["payload"],
            })
            node["head_seq"] = record["seq"]
            node["head_hash"] = record["hash"]
            applied += 1

        self.ledger.verify()
        return {
            "node_sn": node_sn,
            "applied": applied,
            "duplicate_replays": duplicates,
            "assessments_reconciled": reconciled,
            "head_seq": node["head_seq"],
            "device_coverage": self.device_coverage(node_sn),
        }

    # ------------------------------------------------------------------
    def _rehash_check(self, record, prev_hash, node_sn, seq):
        body = {
            "namespace": record.get("namespace"),
            "seq": record["seq"],
            "kind": record.get("kind"),
            "payload": record.get("payload"),
        }
        expected = hashlib.sha256(
            (prev_hash + digest(body)).encode("utf-8")
        ).hexdigest()
        if record.get("hash") != expected:
            raise SyncError(f"节点 {node_sn} 第 {seq} 条内容哈希不匹配，拒绝汇入")

    def _reconcile(self, payload, protocol, node_sn, seq):
        """用冻结协议重算评估结论，确认现场分数未被设备侧篡改。"""
        rtype = payload.get("record_type")
        if rtype == "late_supplement":
            if payload.get("protocol_id") != protocol.protocol_id:
                raise SyncError(f"{node_sn}#{seq} 迟到补充协议标识异常")
            return
        if rtype != "assessment":
            raise SyncError(f"{node_sn}#{seq} 未知记录类型：{rtype}")

        if payload.get("model_version") != protocol.model_version:
            raise SyncError(f"{node_sn}#{seq} 模型版本与冻结协议不一致")
        if payload.get("calibration_fingerprint") != protocol.calibration_fingerprint():
            raise SyncError(f"{node_sn}#{seq} 校准指纹不匹配")
        if payload.get("threshold_fingerprint") != protocol.threshold_fingerprint():
            raise SyncError(f"{node_sn}#{seq} 阈值指纹不匹配")

        # 每条命中必须真实存在于冻结规则，且观测值确实越过阈值
        for hit in payload.get("hits", []):
            rules = protocol.rules.get(hit["channel"], [])
            match = next((r for r in rules
                          if r["metric"] == hit["metric"]
                          and r["op"] == hit["op"]
                          and r["value"] == hit["threshold"]
                          and r["severity"] == hit["severity"]), None)
            if match is None:
                raise SyncError(
                    f"{node_sn}#{seq} 命中了冻结协议中不存在的规则：{hit}"
                )
            observed = payload["features"][hit["channel"]][hit["metric"]]
            if not self._compare(observed, hit["op"], hit["threshold"]):
                raise SyncError(
                    f"{node_sn}#{seq} 命中证据不成立：{observed} {hit['op']} "
                    f"{hit['threshold']}"
                )

        # 最高级别与建议动作必须与命中集合一致
        hits = payload.get("hits", [])
        top = max((SEVERITY_LEVEL[h["severity"]] for h in hits), default=None)
        expected_severity = None
        if top is not None:
            expected_severity = next(
                s for s, lvl in SEVERITY_LEVEL.items() if lvl == top
            )
        if payload.get("severity") != expected_severity:
            raise SyncError(f"{node_sn}#{seq} 严重级别与命中集合不一致")
        expected_action = (
            SEVERITY_ACTION[expected_severity] if expected_severity else None
        )
        if payload.get("confidence", 1.0) >= protocol.alert_min_confidence:
            if payload.get("recommended_action") != expected_action:
                raise SyncError(f"{node_sn}#{seq} 建议动作无法由规则复现")

    @staticmethod
    def _compare(value, op, threshold):
        return {
            ">=": lambda v: v >= threshold,
            ">": lambda v: v > threshold,
            "<": lambda v: v < threshold,
            "<=": lambda v: v <= threshold,
        }[op](value)

    # ------------------------------------------------------------------
    def device_coverage(self, node_sn):
        """按设备序列号汇总已汇入样本覆盖段，缺口显式报告、绝不补造。"""
        seen = {}  # (device_sn) -> {sample_id}
        for entry in self.ledger.payloads("synced_record"):
            if entry["node_sn"] != node_sn:
                continue
            payload = entry["payload"]
            if payload["record_type"] != "assessment":
                continue
            for sample_id in payload["input_window"]["sample_ids"]:
                sn, _, sample_seq = sample_id.partition(":")
                seen.setdefault(sn, set()).add(int(sample_seq))
        coverage = {}
        for sn, seqs in sorted(seen.items()):
            ordered = sorted(seqs)
            segments, gaps = [], []
            start = prev = ordered[0]
            for n in ordered[1:]:
                if n == prev + 1:
                    prev = n
                else:
                    segments.append([start, prev])
                    gaps.append([prev + 1, n - 1])
                    start = prev = n
            segments.append([start, prev])
            coverage[sn] = {"segments": segments, "gaps": gaps,
                            "sample_count": len(ordered)}
        return coverage

    def synced_payloads(self, node_sn=None, record_type=None):
        result = self.ledger.payloads("synced_record")
        if node_sn is not None:
            result = [r for r in result if r["node_sn"] == node_sn]
        if record_type is not None:
            result = [r for r in result if r["record_type"] == record_type]
        return result
