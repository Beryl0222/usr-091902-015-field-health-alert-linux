"""边缘节点：断网期间离线采集评估，恢复连接后按序汇入。

节点内部维护一条只追加出站链：每个定稿评估/迟到补充按 node_seq 顺序入链，
任何重放、丢包或中途篡改在服务端都能被发现。重复批次重放是幂等的。

典型用法：断网期间 feed() 收集样本（可乱序、可重复），按接收钟 tick()
推进定稿；也可以在回放一整个切片后一次性 tick(截止时刻) —— 定稿窗口的
内容只取决于样本集合与各自接收时刻，与 feed 顺序无关。
"""

from engine import RiskEngine
from store import AppendOnlyStore, digest


class EdgeNode:
    def __init__(self, node_sn, protocol, subject_ids=()):
        self.node_sn = node_sn
        self.protocol = protocol
        self.outbox = AppendOnlyStore(namespace=f"node:{node_sn}")
        self.engines = {
            sid: RiskEngine(protocol, sid,
                            store=AppendOnlyStore(namespace=f"assessment:{sid}"))
            for sid in subject_ids
        }
        self._record_index = {}       # 引擎记录哈希 -> node_seq
        self._acked_seq = 0

    def engine(self, subject_id):
        if subject_id not in self.engines:
            self.engines[subject_id] = RiskEngine(
                self.protocol, subject_id,
                store=AppendOnlyStore(namespace=f"assessment:{subject_id}"),
            )
        return self.engines[subject_id]

    def feed(self, subject_id, sample):
        """喂入一条样本；重复样本幂等，迟到样本入补充链。不触发窗口定稿。"""
        engine = self.engine(subject_id)
        before = len(engine.store)
        result = engine.ingest(sample)
        self._harvest(engine, before)
        return result

    def feed_batch(self, subject_id, samples):
        return [self.feed(subject_id, s) for s in samples]

    def tick(self, rx_now_ms):
        """推进所有在管人员的接收钟，定稿截止已过的窗口并收获记录。"""
        for subject_id, engine in self.engines.items():
            before = len(engine.store)
            engine.tick(rx_now_ms)
            self._harvest(engine, before)

    def close_all(self):
        """任务结束：强制定稿全部尚存窗口。"""
        for subject_id, engine in self.engines.items():
            before = len(engine.store)
            engine.close_pending()
            self._harvest(engine, before)

    def _harvest(self, engine, before):
        for record in engine.store.records()[before:]:
            payload = record["payload"]
            record_hash = record["hash"]
            if record_hash in self._record_index:
                continue
            self.outbox.append("field_record", {
                "subject_id": payload["subject_id"],
                "record_type": payload["record_type"],
                "window_start": payload["window_start"],
                "protocol_id": payload.get("protocol_id", self.protocol.protocol_id),
                "source_hash": record_hash,
                "payload": payload,
            })
            self._record_index[record_hash] = len(self.outbox)

    def pending_count(self):
        return len(self.outbox) - self._acked_seq

    def export_batch(self, max_records=None):
        """导出自上次确认之后的出站链段（含完整哈希链记录）。"""
        records = self.outbox.records()[self._acked_seq:]
        if max_records is not None:
            records = records[:max_records]
        if not records:
            return None
        batch = {
            "node_sn": self.node_sn,
            "protocol_id": self.protocol.protocol_id,
            "protocol_fingerprint": self.protocol.fingerprint,
            "start_seq": records[0]["seq"],
            "end_seq": records[-1]["seq"],
        }
        batch["records"] = records
        batch["batch_id"] = digest(
            {k: batch[k] for k in ("node_sn", "start_seq", "end_seq")}
        )
        return batch

    def acknowledge(self, through_seq):
        self._acked_seq = max(self._acked_seq, through_seq)
