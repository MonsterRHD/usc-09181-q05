"""HTTP 接口冒烟测试：保单 -> 报案 -> 处置链查询。"""

import http.client
import json
import threading
import unittest

from service.desk import Desk, EventStore
from service.desk.api import create_server

TERMS = {
    "insured": "示例出口企业",
    "coverages": ["cargo"],
    "regions": ["DE"],
    "deductible": 10_000,
    "limit": 1_000_000,
    "currency": "USD",
    "notice_days": 30,
    "valid_from": "2026-01-01T00:00:00+00:00",
    "valid_to": "2026-12-31T23:59:59+00:00",
    "timezone": "UTC",
}


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = create_server(Desk(EventStore()), 0, host="127.0.0.1")
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        return resp.status, data

    def test_health(self):
        status, data = self.request("GET", "/health")
        self.assertEqual((status, data["status"]), (200, "ok"))

    def test_policy_report_chain_flow(self):
        status, policy = self.request("POST", "/policies",
                                      {**TERMS, "policy_id": "API-P1", "actor": "ops"})
        self.assertEqual(status, 200)
        self.assertEqual(policy["policy_id"], "API-P1")

        status, view = self.request("POST", "/reports", {
            "actor": "portal", "incident_ref": "API-INC-1", "policy_id": "API-P1",
            "occurred_at": "2026-03-01T10:00:00+00:00",
            "reported_at": "2026-03-02T10:00:00+00:00",
            "peril": "cargo", "region": "DE", "handler": "理赔员甲",
        })
        self.assertEqual(status, 200)
        self.assertEqual(view["status"], "reported")
        case_id = view["case_id"]

        status, view = self.request("POST", f"/cases/{case_id}/prepayments",
                                    {"actor": "理赔员甲", "amount": 5_000})
        self.assertEqual(status, 409)
        self.assertIn("授权人", view["detail"])

        status, view = self.request("POST", f"/cases/{case_id}/prepayments", {
            "actor": "理赔员甲", "amount": 5_000,
            "authorizer": "风控总监", "limit": 20_000,
        })
        self.assertEqual(status, 200)
        self.assertEqual(view["status"], "prepaid")

        status, chain = self.request("GET", f"/cases/{case_id}/chain")
        self.assertEqual(status, 200)
        # 被拒绝的预付尝试也留在处置链中，保证可审计
        self.assertEqual([e["type"] for e in chain["chain"]],
                         ["incident_reported", "prepayment_rejected",
                          "prepayment_authorized"])

        status, listing = self.request("GET", "/cases")
        self.assertEqual(status, 200)
        self.assertEqual(listing["cases"][0]["responsible"], "理赔员甲")

    def test_unknown_route_and_case(self):
        status, _ = self.request("GET", "/nope")
        self.assertEqual(status, 404)
        status, data = self.request("GET", "/cases/no-such-case")
        self.assertEqual((status, data["error"]), (404, "case_not_found"))


if __name__ == "__main__":
    unittest.main()
