"""HTTP 接口：把处置台命令与视图暴露为 JSON API。

仅使用标准库。路由约定：
    GET  /health
    POST /policies                          登记保单（请求体即条款，可带 policy_id）
    GET  /policies/{policy_id}              保单条款与批改
    POST /policies/{policy_id}/endorsements 登记批改 {changes, effective_at, note?}
    POST /reports                           报案 {incident_ref, policy_id, occurred_at,
                                                  peril, region, handler, ...}
    GET  /cases                             案件列表（按事故发生顺序）
    GET  /cases/{case_id}                   案件视图（状态/赔付/责任人/下一步动作）
    GET  /cases/{case_id}/chain             处置链（按业务时间整理的全部事件）
    POST /cases/{case_id}/materials         客户材料
    POST /cases/{case_id}/corrections       材料更正（追加事件，不改写原摘要）
    POST /cases/{case_id}/receipts          承保人回执
    POST /cases/{case_id}/milestones        调查节点
    POST /cases/{case_id}/assessments       损失核定
    POST /cases/{case_id}/prepayments       紧急预付（须含 authorizer 与 limit）
    POST /cases/{case_id}/payouts           赔付
    POST /cases/{case_id}/subrogation-receipts 追偿回执
操作人取自请求体 actor 字段或 X-Actor 请求头。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .core import Desk, DeskError


def make_handler(desk: Desk):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PolicyDesk/0.1"

        # ---------------------------------------------------------- 基础工具

        def _send_json(self, code: int, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError:
                raise DeskError("bad_json", "请求体不是合法 JSON", status=400)
            if not isinstance(data, dict):
                raise DeskError("bad_json", "请求体必须是 JSON 对象", status=400)
            return data

        def log_message(self, *_):  # 保持安静，与原有健康检查一致
            pass

        # ---------------------------------------------------------- 分发

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method: str):
            try:
                action = self._route(method)
                if action is None:
                    self._send_json(404, {"error": "not_found",
                                          "detail": f"未知路径: {method} {self.path}"})
                    return
                self._send_json(200, action())
            except DeskError as exc:
                self._send_json(exc.status, {"error": exc.code, "detail": exc.detail})

        def _route(self, method: str):
            parts = [p for p in self.path.split("?", 1)[0].split("/") if p]
            body = self._read_body() if method == "POST" else {}
            actor = body.pop("actor", None) or self.headers.get("X-Actor") or "anonymous"

            if method == "GET" and parts == ["health"]:
                return lambda: {"status": "ok"}
            if method == "POST" and parts == ["policies"]:
                policy_id = body.pop("policy_id", None)
                return lambda: desk.register_policy(actor=actor, terms=body,
                                                    policy_id=policy_id)
            if method == "GET" and len(parts) == 2 and parts[0] == "policies":
                pid = parts[1]
                return lambda: desk.policy_view(pid)
            if (method == "POST" and len(parts) == 3
                    and parts[0] == "policies" and parts[2] == "endorsements"):
                pid = parts[1]
                return lambda: desk.record_endorsement(pid, actor=actor, **body)
            if method == "POST" and parts == ["reports"]:
                return lambda: desk.report_incident(actor=actor, **body)
            if method == "GET" and parts == ["cases"]:
                return lambda: {"cases": desk.list_cases()}
            if len(parts) >= 2 and parts[0] == "cases":
                cid = parts[1]
                if method == "GET" and len(parts) == 2:
                    return lambda: desk.case_view(cid)
                if method == "GET" and len(parts) == 3 and parts[2] == "chain":
                    return lambda: {"case_id": cid, "chain": desk.case_chain(cid)}
                if method == "POST" and len(parts) == 3:
                    handlers = {
                        "materials": desk.submit_material,
                        "corrections": desk.correct_material,
                        "receipts": desk.record_receipt,
                        "milestones": desk.record_milestone,
                        "assessments": desk.assess_loss,
                        "prepayments": desk.authorize_prepayment,
                        "payouts": desk.record_payout,
                        "subrogation-receipts": desk.record_subrogation,
                    }
                    fn = handlers.get(parts[2])
                    if fn:
                        return lambda: fn(cid, actor=actor, **body)
            return None

    return Handler


def create_server(desk: Desk, port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(desk))
