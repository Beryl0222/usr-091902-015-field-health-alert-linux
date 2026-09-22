"""只追加哈希链存储。

任何定稿记录（风险评估、迟到补充、处置动作、军医覆盖、同步批次）都追加到
链上；记录一旦写入不可修改，整条链可随时重新校验，防止旧判断被重写或篡改。
"""

import hashlib
import json

GENESIS = "0" * 64


def canonical(obj):
    """规范化 JSON 序列化：键排序、无空白，作为指纹与签名的稳定字节源。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(obj):
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


class ChainViolation(Exception):
    """哈希链校验失败：记录被改写、删除或顺序被调换。"""


class AppendOnlyStore:
    """内存态只追加日志，seq 从 1 开始，每条记录锚定前一条的哈希。"""

    def __init__(self, namespace="default"):
        self.namespace = namespace
        self._records = []

    def append(self, kind, payload):
        prev_hash = self._records[-1]["hash"] if self._records else GENESIS
        seq = len(self._records) + 1
        body = {"namespace": self.namespace, "seq": seq, "kind": kind, "payload": payload}
        record = dict(body)
        record["prev_hash"] = prev_hash
        record["hash"] = hashlib.sha256(
            (prev_hash + digest(body)).encode("utf-8")
        ).hexdigest()
        self._records.append(record)
        return record

    def records(self, kind=None):
        if kind is None:
            return list(self._records)
        return [r for r in self._records if r["kind"] == kind]

    def payloads(self, kind=None):
        return [r["payload"] for r in self.records(kind)]

    def __len__(self):
        return len(self._records)

    def verify(self):
        """重算整条链，任何篡改都抛出 ChainViolation。"""
        prev_hash = GENESIS
        for i, record in enumerate(self._records, start=1):
            if record["prev_hash"] != prev_hash:
                raise ChainViolation(f"{self.namespace}: 第 {i} 条前向哈希不匹配")
            body = {
                "namespace": record["namespace"],
                "seq": record["seq"],
                "kind": record["kind"],
                "payload": record["payload"],
            }
            expected = hashlib.sha256(
                (prev_hash + digest(body)).encode("utf-8")
            ).hexdigest()
            if record["hash"] != expected or record["seq"] != i:
                raise ChainViolation(f"{self.namespace}: 第 {i} 条内容哈希不匹配")
            prev_hash = record["hash"]
        return True

    def export(self):
        return {"namespace": self.namespace, "records": list(self._records)}
