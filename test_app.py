import base64
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import BusinessError, CustodyStore


class CustodyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CustodyStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.case = self.store.create_case("custodian1", "CASE-2026-001", "跨境资金调查")
        self.store.add_member("custodian1", self.case["id"], "custodian2", "custodian")
        self.store.add_member("custodian1", self.case["id"], "analyst1", "analyst")
        self.store.add_member("custodian1", self.case["id"], "auditor1", "auditor")
        self.retention = (date.today() + timedelta(days=3650)).isoformat()

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_custody_analysis_release_and_integrity_report(self):
        raw = b"bank statement original bytes"
        item = self.store.ingest_evidence(
            "custodian1", self.case["id"], "E-001", "statement.csv",
            base64.b64encode(raw).decode(), self.retention, "custodian1",
        )
        opened = self.store.open_evidence("custodian1", item["id"], "A 区证物室", "两名人员在场开箱")
        self.assertEqual(opened["status"], "opened")
        child = self.store.derive(
            "analyst1", item["id"], "CSV 提取交易记录", "E-001-D1", "transactions.json",
            base64.b64encode(b'[{"amount": 100}]').decode(),
        )
        self.store.transfer("custodian2", item["id"], "custodian2", "法院证物库", "封存后移交")
        self.store.release("custodian2", item["id"], "检察机关", "按调取令释放原件")
        report = self.store.report("auditor1", self.case["id"])
        self.assertTrue(report["overall_integrity_valid"])
        self.assertEqual(report["evidence_count"], 2)
        original = next(x for x in report["evidence"] if x["id"] == item["id"])
        self.assertEqual(original["status"], "released")
        self.assertTrue(original["chain_valid"])
        self.assertEqual(child["parent_id"], item["id"])

    def test_permissions_and_legal_hold_block_release(self):
        item = self.store.ingest_evidence(
            "custodian1", self.case["id"], "E-002", "raw.bin",
            base64.b64encode(b"evidence").decode(), self.retention,
        )
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_evidence("outsider", item["id"])
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.release("analyst1", item["id"], "外部机构")
        self.assertEqual(ctx.exception.status, 403)
        self.store.set_hold("auditor1", item["id"], True, "诉讼保全要求")
        with self.assertRaises(BusinessError) as ctx:
            self.store.release("custodian1", item["id"], "外部机构")
        self.assertEqual(ctx.exception.code, "legal_hold_active")

    def _make_item(self, label="E-LOAN", custodian="custodian1"):
        return self.store.ingest_evidence(
            "custodian1", self.case["id"], label, "original.bin",
            base64.b64encode(b"original bytes for appraisal").decode(),
            self.retention, custodian,
        )

    def test_loan_approve_return_restores_custodian_and_unblocks(self):
        item = self._make_item("E-010", custodian="custodian2")
        due = (date.today() + timedelta(days=14)).isoformat()
        loan = self.store.apply_loan("custodian2", item["id"], "中正司法鉴定中心", "原件真伪鉴定", due)
        self.assertEqual(loan["status"], "pending")
        approved = self.store.approve_loan("auditor1", loan["id"])
        self.assertEqual(approved["status"], "active")
        self.assertEqual(approved["original_custodian"], "custodian2")

        detail = self.store.get_evidence("custodian1", item["id"])
        self.assertEqual(detail["status"], "on_loan")
        self.assertEqual(detail["current_custodian"], "中正司法鉴定中心")
        self.assertEqual(detail["current_loan"]["due_date"], due)
        self.assertFalse(detail["current_loan"]["overdue"])

        # 外借中四类操作全部暂停
        for actor, fn in (
            ("custodian2", lambda: self.store.open_evidence("custodian2", item["id"], "证物室")),
            ("custodian2", lambda: self.store.transfer("custodian2", item["id"], "custodian1", "证物库")),
            ("analyst1", lambda: self.store.derive(
                "analyst1", item["id"], "鉴定比对分析", "D1", "r.bin", base64.b64encode(b"x").decode())),
            ("custodian2", lambda: self.store.release("custodian2", item["id"], "外部机构")),
        ):
            with self.assertRaises(BusinessError) as ctx:
                fn()
            self.assertEqual(ctx.exception.code, "loan_active")

        # 待审批/外借中不能重复申请
        with self.assertRaises(BusinessError):
            self.store.apply_loan("custodian2", item["id"], "其他机构", "其他用途", due)

        # 归还：补记位置、回到原保管人、恢复封存保管
        returned = self.store.return_loan("custodian2", item["id"], "A 区证物室保险柜", "鉴定完毕归还")
        self.assertEqual(returned["status"], "custody")
        self.assertEqual(returned["current_custodian"], "custodian2")
        self.assertFalse(returned["was_overdue"])

        detail = self.store.get_evidence("custodian1", item["id"])
        self.assertEqual(detail["status"], "custody")
        self.assertEqual(detail["current_custodian"], "custodian2")
        self.assertIsNone(detail["current_loan"])
        self.assertEqual(detail["loan_history"][0]["status"], "returned")
        # 暂停的操作恢复（开箱可用）
        self.assertEqual(self.store.open_evidence("custodian2", item["id"], "A 区证物室")["status"], "opened")

        report = self.store.report("auditor1", self.case["id"])
        self.assertTrue(report["overall_integrity_valid"])
        row = next(x for x in report["evidence"] if x["id"] == item["id"])
        self.assertIsNone(row["current_loan"])
        self.assertEqual(row["loan_history"][0]["borrower"], "中正司法鉴定中心")
        types = [e["event_type"] for e in row["events"]]
        self.assertIn("LOAN_OUT", types)
        self.assertIn("LOAN_RETURN", types)
        self.assertLess(types.index("LOAN_OUT"), types.index("LOAN_RETURN"))

    def test_loan_approval_permissions(self):
        item = self._make_item("E-011")
        due = (date.today() + timedelta(days=7)).isoformat()
        loan = self.store.apply_loan("custodian1", item["id"], "鉴定机构", "文书鉴定", due)
        # 分析员和非创建人保管员不能批准
        with self.assertRaises(BusinessError) as ctx:
            self.store.approve_loan("analyst1", loan["id"])
        self.assertEqual(ctx.exception.status, 403)
        # 案件创建人可以批准
        self.assertEqual(self.store.approve_loan("custodian1", loan["id"])["status"], "active")
        # 已批准不能再次批准
        with self.assertRaises(BusinessError) as ctx:
            self.store.approve_loan("auditor1", loan["id"])
        self.assertEqual(ctx.exception.code, "loan_not_pending")

    def test_loan_requires_sealed_custody(self):
        item = self._make_item("E-012")
        self.store.open_evidence("custodian1", item["id"], "证物室")
        due = (date.today() + timedelta(days=5)).isoformat()
        with self.assertRaises(BusinessError) as ctx:
            self.store.apply_loan("custodian1", item["id"], "鉴定机构", "鉴定原件", due)
        self.assertEqual(ctx.exception.code, "loan_not_in_custody")

    def test_pending_loan_blocks_open_transfer_release(self):
        item = self._make_item("E-014")
        due = (date.today() + timedelta(days=5)).isoformat()
        self.store.apply_loan("custodian1", item["id"], "鉴定机构", "鉴定原件", due)
        for call in (
            lambda: self.store.open_evidence("custodian1", item["id"], "证物室"),
            lambda: self.store.transfer("custodian1", item["id"], "custodian2", "证物库"),
            lambda: self.store.release("custodian1", item["id"], "外部机构"),
        ):
            with self.assertRaises(BusinessError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, "loan_pending")

    def test_overdue_loan_blocks_reapply_until_returned(self):
        item = self._make_item("E-013")
        loan = self.store.apply_loan(
            "custodian1", item["id"], "鉴定机构", "痕迹鉴定",
            (date.today() + timedelta(days=3)).isoformat(),
        )
        self.store.approve_loan("auditor1", loan["id"])
        # 将到期日改到过去，模拟逾期未还
        db = self.store.db_path
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE loans SET due_date=? WHERE id=?",
                         ((date.today() - timedelta(days=1)).isoformat(), loan["id"]))
        detail = self.store.get_evidence("custodian1", item["id"])
        self.assertTrue(detail["current_loan"]["overdue"])

        due2 = (date.today() + timedelta(days=10)).isoformat()
        with self.assertRaises(BusinessError) as ctx:
            self.store.apply_loan("custodian1", item["id"], "另一家机构", "再次借阅", due2)
        self.assertEqual(ctx.exception.code, "loan_overdue_open")

        # 逾期处理（归还）后可重新申请
        self.store.return_loan("custodian1", item["id"], "证物室")
        again = self.store.apply_loan("custodian1", item["id"], "另一家机构", "补充鉴定", due2)
        self.assertEqual(again["status"], "pending")


if __name__ == "__main__":
    unittest.main()
