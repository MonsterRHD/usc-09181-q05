"""海外保单风险处置台 HTTP 入口（标准库，无第三方依赖）。

事件日志默认追加到 EVENT_STORE_PATH（默认 data/eventstore.jsonl），
服务重启后重放恢复，事故仍按发生时间排序输出。
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

from .domain import DeskService, DomainError, EventStore

STORE_PATH = os.getenv("EVENT_STORE_PATH", "data/eventstore.jsonl")


def _split(path: str) -> list[str]:
    return [p for p in path.rstrip("/").split("/") if p]


class Handler(BaseHTTPRequestHandler):
    service = DeskService(EventStore(STORE_PATH))

    # ---- 工具 --------------------------------------------------------------
    def _send(self, status: int, body) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def log_message(self, *_):
        pass

    # ---- 路由 --------------------------------------------------------------
    def do_GET(self):
        parts = _split(self.path)
        try:
            if parts == ["health"]:
                self._send(200, {"status": "ok"})
            elif parts == ["incidents"]:
                order = "occurrence"
                if "?" in self.path:
                    from urllib.parse import parse_qs
                    q = parse_qs(self.path.split("?", 1)[1])
                    order = q.get("order", ["occurrence"])[0]
                self._send(200, self.service.list_incidents(order))
            elif len(parts) == 2 and parts[0] == "incidents":
                self._send(200, self.service.get_incident(parts[1]))
            elif parts == ["audit"]:
                self._send(200, self.service.audit_trail().as_dict())
            else:
                self._send(404, {"code": "not_found", "message": self.path})
        except DomainError as exc:
            self._send(422, {"code": exc.code, "message": exc.message})

    def do_POST(self):
        parts = _split(self.path.split("?", 1)[0])
        try:
            b = self._body()
            s = self.service

            if parts == ["policies"]:
                self._send(201, s.register_policy(
                    b["policy_id"], coverages=b["coverages"], regions=b["regions"],
                    deductibles=b.get("deductibles"),
                    notice_limits_hours=b.get("notice_limits_hours"),
                    currency=b.get("currency", "USD")))

            elif len(parts) == 3 and parts[0] == "policies" and parts[2] == "endorsements":
                self._send(201, s.record_endorsement(
                    parts[1], b["endorsement_id"], effective_from=b["effective_from"],
                    coverages_added=b.get("coverages_added"),
                    regions_added=b.get("regions_added"), note=b.get("note", "")))

            elif parts == ["incidents"]:
                self._send(201, s.report_incident(
                    b["incident_id"], policy_id=b["policy_id"], loss_type=b["loss_type"],
                    occurred_at=b["occurred_at"], notified_at=b["notified_at"],
                    reporter=b.get("reporter", ""), region=b.get("region", ""),
                    vessel_or_ref=b.get("vessel_or_ref", ""),
                    client_event_id=b.get("client_event_id")))

            elif len(parts) == 3 and parts[0] == "incidents" and parts[2] == "scope":
                self._send(201, s.decide_scope(parts[1]))

            elif len(parts) == 3 and parts[0] == "incidents" and parts[2] == "materials":
                self._send(201, s.receive_material(
                    parts[1], b["material_id"], kind=b["kind"], summary=b["summary"],
                    received_at=b["received_at"]))

            elif (len(parts) == 5 and parts[0] == "incidents"
                  and parts[2] == "materials" and parts[4] == "corrections"):
                self._send(201, s.correct_material(
                    parts[1], parts[3], corrected_summary=b["corrected_summary"],
                    reason=b["reason"]))

            elif len(parts) == 3 and parts[0] == "incidents" and parts[2] == "partial-loss":
                self._send(201, s.declare_partial_loss(
                    parts[1], claimed_amount=b["claimed_amount"], note=b.get("note", "")))

            elif len(parts) == 3 and parts[0] == "incidents" and parts[2] == "adjust":
                self._send(201, s.adjust_loss(
                    parts[1], adjusted_amount=b["adjusted_amount"],
                    adjuster=b["adjuster"]))

            elif len(parts) == 3 and parts[0] == "incidents" and parts[2] == "advances":
                self._send(201, s.authorize_advance(
                    parts[1], amount=b["amount"], cap=b["cap"],
                    authorizer=b["authorizer"], authorized_at=b["authorized_at"]))

            elif len(parts) == 3 and parts[0] == "incidents" and parts[2] == "payments":
                self._send(201, s.pay(
                    parts[1], b["payment_id"], amount=b["amount"], paid_at=b["paid_at"],
                    kind=b.get("kind", "indemnity"), reference=b.get("reference", ""),
                    pending=b.get("pending", False)))

            elif len(parts) == 3 and parts[0] == "incidents" and parts[2] == "acknowledgements":
                self._send(201, s.receive_acknowledgement(
                    parts[1], from_=b.get("from", b.get("from_", "")),
                    received_at=b["received_at"], kind=b.get("kind", "underwriter_receipt"),
                    reference=b.get("reference", "")))

            elif len(parts) == 3 and parts[0] == "incidents" and parts[2] == "investigation":
                self._send(201, s.log_investigation(
                    parts[1], node=b["node"], at=b["at"],
                    owner=b.get("owner", ""), note=b.get("note", "")))

            elif len(parts) == 3 and parts[0] == "incidents" and parts[2] == "subrogation":
                self._send(201, s.open_subrogation(
                    parts[1], responsible_party=b["responsible_party"],
                    target_amount=b["target_amount"], opened_at=b["opened_at"],
                    owner=b.get("owner", "")))

            elif len(parts) == 3 and parts[0] == "incidents" and parts[2] == "recoveries":
                self._send(201, s.receive_recovery(
                    parts[1], amount=b["amount"],
                    from_=b.get("from", b.get("from_", "")),
                    received_at=b["received_at"], late=b.get("late", False),
                    reference=b.get("reference", "")))

            elif len(parts) == 3 and parts[0] == "incidents" and parts[2] == "close":
                self._send(201, s.close_incident(parts[1], note=b.get("note", "")))

            elif len(parts) == 3 and parts[0] == "incidents" and parts[2] == "corrections":
                self._send(201, s.correct_status(
                    parts[1], to_status=b["to_status"], reason=b["reason"]))

            else:
                self._send(404, {"code": "not_found", "message": self.path})
        except DomainError as exc:
            self._send(422, {"code": exc.code, "message": exc.message})
        except (KeyError, ValueError) as exc:
            self._send(400, {"code": "bad_request", "message": str(exc)})


def make_server(host: str = "0.0.0.0", port: int | None = None) -> HTTPServer:
    return HTTPServer((host, port or int(os.getenv("PORT", "8000"))), Handler)


def run() -> None:
    server = make_server()
    server.serve_forever()


if __name__ == "__main__":
    run()
