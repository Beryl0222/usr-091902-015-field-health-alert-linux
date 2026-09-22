"""HTTP 接口契约：冻结方案查询、离线喂入、tick、同步、覆盖、角色视图。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from service import Handler, SERVICE_ID, health_payload


def request(base, method, path, body=None, query=None):
    if query:
        path = path + "?" + urlencode(query)
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(base + path, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=3) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class ServiceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        # 本模块专用节点，避免与其他测试共享内存状态
        cls.node_sn = "HTTP-EDGE-1"
        cls.subject = "P-HTTP-01"
        status, _ = request(cls.base, "POST", "/nodes", {
            "node_sn": cls.node_sn, "protocol_id": "heat@2026.09.01",
            "subject_ids": [cls.subject]})
        assert status == 201

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_health_payload_has_stable_identity(self):
        self.assertEqual(health_payload(),
                         {"status": "ok", "service": SERVICE_ID,
                          "name": "极端作业健康预警"})

    def test_health_endpoint_returns_json(self):
        status, payload = request(self.base, "GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload, health_payload())

    def test_unknown_route_is_not_exposed(self):
        status, _ = request(self.base, "GET", "/unknown")
        self.assertEqual(status, 404)

    def test_protocols_listed_with_fingerprints(self):
        status, payload = request(self.base, "GET", "/protocols")
        self.assertEqual(status, 200)
        ids = {p["protocol_id"] for p in payload["protocols"]}
        self.assertEqual(ids, {"heat@2026.09.01", "altitude@2026.09.01"})
        for p in payload["protocols"]:
            self.assertTrue(p["fingerprint"])
            self.assertTrue(p["calibration_fingerprint"])
            self.assertTrue(p["threshold_fingerprint"])

    def test_offline_feed_tick_sync_override_and_views(self):
        base, sid, node = self.base, self.subject, self.node_sn
        # 离线喂入一个高温窗口（乱序），并带一条重复
        samples = [
            {"device_sn": "HTTP-DEV", "seq": i + 1,
             "ts": i * 10_000, "rx_ts": i * 10_000 + 500,
             "channels": {"core_temp": 39.7, "spo2": 96.0, "eeg": 30.0}}
            for i in range(6)
        ]
        ordered = [samples[5], samples[2], samples[0], samples[3],
                   samples[1], samples[4], samples[2]]
        status, payload = request(base, "POST", f"/nodes/{node}/feed",
                                  {"subject_id": sid, "samples": ordered})
        self.assertEqual(status, 202)
        self.assertEqual(sum(1 for r in payload["processed"]
                             if r["status"] == "duplicate"), 1)

        # 定稿时刻未到：尚无评估
        status, payload = request(base, "POST", f"/nodes/{node}/tick",
                                  {"rx_now_ms": 60_000})
        self.assertEqual(status, 200)
        status, timeline = request(base, "GET", f"/nodes/{node}/timeline",
                                  query={"subject_id": sid})
        self.assertEqual(
            [r for r in timeline["timeline"]
             if r["record_type"] == "assessment"], [])

        # 推进至 窗末+宽限：评估出现且带模型版本与输入窗口
        request(base, "POST", f"/nodes/{node}/tick", {"rx_now_ms": 75_000})
        status, timeline = request(base, "GET", f"/nodes/{node}/timeline",
                                  query={"subject_id": sid})
        (assessment,) = [r for r in timeline["timeline"]
                         if r["record_type"] == "assessment"]
        self.assertEqual(assessment["recommended_action"], "降温补水")
        self.assertEqual(assessment["input_window"]["start_ms"], 0)
        self.assertTrue(assessment["model_version"])
        self.assertTrue(assessment["basis_refs"])

        # 恢复连接安全汇入
        status, report = request(base, "POST", f"/nodes/{node}/sync")
        self.assertEqual(status, 200)
        self.assertEqual(report["applied"], 1)
        self.assertEqual(report["assessments_reconciled"], 1)
        self.assertEqual(report["actions_triggered"][0]["action"], "降温补水")

        # 军医无理由覆盖被拒
        status, err = request(base, "POST", "/dispositions/override", {
            "subject_id": sid, "medic_id": "DOC-1",
            "action": "现场复核", "reason": "   "})
        self.assertEqual(status, 400)
        self.assertIn("理由", err["error"])

        # 有理由覆盖成功
        status, rec = request(base, "POST", "/dispositions/override", {
            "subject_id": sid, "medic_id": "DOC-1",
            "action": "转运", "reason": "出现惊厥前驱，后送观察"})
        self.assertEqual(status, 201)
        self.assertEqual(rec["state"], "已预警")

        # 指挥员只见履职状态
        status, view = request(base, "GET", "/view", query={"role": "指挥人员", "subject_id": sid})
        self.assertEqual(status, 200)
        commander = view["subjects"][0]
        self.assertEqual(set(commander),
                         {"subject_id", "state", "required_action",
                          "since_window", "overridden"})
        self.assertTrue(commander["overridden"])

        # 军医视图含审计
        status, view = request(base, "GET", "/view", query={"role": "值班军医", "subject_id": sid})
        kinds = [r["record_type"] for r in view["subjects"][0]["audit"]]
        self.assertEqual(kinds, ["auto_action", "medic_override"])

        # 覆盖审计可独立查询
        status, audit = request(base, "GET", "/audit", query={"subject_id": sid})
        self.assertEqual(len(audit["dispositions"]), 2)

    def test_sync_rejects_unknown_node(self):
        status, _ = request(self.base, "POST", "/nodes/GHOST/sync")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
