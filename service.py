"""极端作业健康预警运行入口。

保留原有 /health 契约；新增任务前冻结、边缘离线采集、安全汇入、处置监督与
角色视图接口。全部状态保存在单进程内存中，供本地联调与契约测试。
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from oversight import OversightError
from system import FieldSystem
from sync import SyncError

SERVICE_ID = "field-health-alert"
SERVICE_NAME = "极端作业健康预警"

SYSTEM = FieldSystem()


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    """健康检查与业务接口；未知路径一律 404。"""

    server_version = "FieldHealthAlert/1.0"

    # -- GET ---------------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            if path == "/health":
                return self._json(200, health_payload())
            if path == "/protocols":
                return self._list_protocols()
            if path.startswith("/nodes/") and path.endswith("/timeline"):
                return self._timeline(path.split("/")[2], query)
            if path.startswith("/nodes/") and path.endswith("/coverage"):
                return self._coverage(path.split("/")[2])
            if path == "/view":
                return self._view(query)
            if path == "/audit":
                return self._audit(query)
            if path.startswith("/subjects/") and path.endswith("/status"):
                return self._status(path.split("/")[2])
            self.send_error(404)
        except KeyError as exc:
            self._json(404, {"error": str(exc).strip("'")})
        except (ValueError, OversightError, SyncError) as exc:
            self._json(400, {"error": str(exc)})

    # -- POST --------------------------------------------------------------
    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        body = self._read_body()
        try:
            if path == "/nodes":
                return self._register_node(body)
            if path.endswith("/feed") and path.startswith("/nodes/"):
                return self._feed(path.split("/")[2], body)
            if path.endswith("/tick") and path.startswith("/nodes/"):
                return self._tick(path.split("/")[2], body)
            if path.endswith("/close") and path.startswith("/nodes/"):
                return self._close(path.split("/")[2])
            if path.endswith("/sync") and path.startswith("/nodes/"):
                return self._sync(path.split("/")[2])
            if path == "/dispositions/override":
                return self._override(body)
            self.send_error(404)
        except KeyError as exc:
            self._json(404, {"error": str(exc).strip("'")})
        except (ValueError, OversightError, SyncError) as exc:
            self._json(400, {"error": str(exc)})

    # -- 业务处理 -----------------------------------------------------------
    def _list_protocols(self):
        protocols = []
        for pid in SYSTEM.registry.ids():
            p = SYSTEM.registry.get(pid)
            protocols.append({
                "protocol_id": p.protocol_id,
                "scenario": p.scenario,
                "model_version": p.model_version,
                "fingerprint": p.fingerprint,
                "feature_window_ms": p.feature_window_ms,
                "allowed_lateness_ms": p.allowed_lateness_ms,
                "calibration_fingerprint": p.calibration_fingerprint(),
                "threshold_fingerprint": p.threshold_fingerprint(),
            })
        self._json(200, {"protocols": protocols})

    def _register_node(self, body):
        self._require(body, ("node_sn", "protocol_id"))
        node = SYSTEM.register_node(
            body["node_sn"], body["protocol_id"],
            tuple(body.get("subject_ids", [])),
        )
        self._json(201, {
            "node_sn": node.node_sn,
            "protocol_id": node.protocol.protocol_id,
            "protocol_fingerprint": node.protocol.fingerprint,
            "bound_subjects": list(node.engines),
        })

    def _feed(self, node_sn, body):
        self._require(body, ("subject_id",))
        node = SYSTEM.node(node_sn)
        samples = body.get("samples") or (
            [body["sample"]] if body.get("sample") else []
        )
        if not samples:
            raise ValueError("需要 samples 数组或单个 sample")
        results = [node.feed(body["subject_id"], s) for s in samples]
        self._json(202, {
            "node_sn": node_sn,
            "subject_id": body["subject_id"],
            "processed": results,
            "pending_records": node.pending_count(),
        })

    def _tick(self, node_sn, body):
        self._require(body, ("rx_now_ms",))
        node = SYSTEM.node(node_sn)
        node.tick(int(body["rx_now_ms"]))
        self._json(200, {"node_sn": node_sn,
                         "pending_records": node.pending_count()})

    def _close(self, node_sn):
        node = SYSTEM.node(node_sn)
        node.close_all()
        self._json(200, {"node_sn": node_sn,
                         "pending_records": node.pending_count()})

    def _sync(self, node_sn):
        report = SYSTEM.flush_node(node_sn)
        self._json(200, report)

    def _timeline(self, node_sn, query):
        node = SYSTEM.node(node_sn)
        subject_id = (query.get("subject_id") or [None])[0]
        if subject_id:
            timeline = node.engine(subject_id).timeline()
        else:
            timeline = []
            for engine in node.engines.values():
                timeline.extend(engine.timeline())
            timeline.sort(key=lambda r: (r["window_start"],
                                         r["record_type"], r.get("seq", 0)))
        self._json(200, {"node_sn": node_sn, "timeline": timeline})

    def _coverage(self, node_sn):
        self._json(200, {"node_sn": node_sn,
                         "device_coverage": SYSTEM.sync.device_coverage(node_sn)})

    def _override(self, body):
        self._require(body, ("subject_id", "medic_id", "action", "reason"))
        record = SYSTEM.oversight.override(
            body["subject_id"], body["medic_id"], body["action"],
            body["reason"], ts=body.get("ts"),
        )
        self._json(201, record)

    def _status(self, subject_id):
        status = SYSTEM.oversight.status_of(subject_id)
        if status is None:
            self._json(404, {"error": f"尚无 {subject_id} 的处置状态"})
        else:
            self._json(200, status)

    def _view(self, query):
        role = (query.get("role") or [None])[0]
        subject_id = (query.get("subject_id") or [None])[0]
        if not role:
            raise ValueError("需要 role 查询参数")
        self._json(200, {"role": role,
                         "subjects": SYSTEM.oversight.view_for(role, subject_id)})

    def _audit(self, query):
        subject_id = (query.get("subject_id") or [None])[0]
        self._json(200, {"dispositions": SYSTEM.oversight.audit_log(subject_id)})

    # -- 工具 ---------------------------------------------------------------
    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"请求体不是合法 JSON：{exc}")
        if not isinstance(data, dict):
            raise ValueError("请求体必须为 JSON 对象")
        return data

    @staticmethod
    def _require(body, fields):
        missing = [f for f in fields if not body.get(f)]
        if missing:
            raise ValueError(f"缺少必填字段：{missing}")

    def _json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_error(self, code, message=None, *_args, **_kwargs):
        """统一 JSON 错误体，便于联调端解析。"""
        self._json(code, {"error": message or {
            404: "路径不存在",
            400: "请求有误",
            405: "方法不允许",
        }.get(code, "请求失败")})

    def log_message(self, *_args):
        return


def build_server(port=8000):
    return ThreadingHTTPServer(("0.0.0.0", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        assert set(SYSTEM.registry.ids()) == {"heat@2026.09.01",
                                              "altitude@2026.09.01"}
        print("基础检查通过")
        return
    build_server(args.port).serve_forever()


if __name__ == "__main__":
    main()
