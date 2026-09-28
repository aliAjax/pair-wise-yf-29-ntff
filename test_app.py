import base64
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

    def _ingest(self, label="E-LOAN"):
        return self.store.ingest_evidence(
            "custodian1", self.case["id"], label, "original.dat",
            base64.b64encode(b"original bytes").decode(), self.retention, "custodian1",
        )

    def test_loan_request_approval_freeze_and_return(self):
        item = self._ingest("E-101")
        due = (date.today() + timedelta(days=14)).isoformat()
        # 只有保管员能提申请
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_loan("analyst1", item["id"], "鉴定中心", "笔迹鉴定", due)
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_loan("custodian1", item["id"], "鉴定中心", "鉴定", due)
        self.assertEqual(ctx.exception.status, 422)
        loan = self.store.request_loan("custodian1", item["id"], "华夏鉴定中心", "笔迹与印章鉴定", due)
        self.assertEqual(loan["status"], "pending")
        # 分析员不能审批；审计员可以
        with self.assertRaises(BusinessError) as ctx:
            self.store.decide_loan("analyst1", loan["id"], True)
        self.assertEqual(ctx.exception.status, 403)
        approved = self.store.decide_loan("auditor1", loan["id"], True)
        self.assertEqual(approved["status"], "active")
        self.assertEqual(approved["original_custodian"], "custodian1")
        detail = self.store.get_evidence("custodian1", item["id"])
        self.assertEqual(detail["status"], "on_loan")
        self.assertEqual(detail["active_loan"]["borrower"], "华夏鉴定中心")
        self.assertEqual(detail["active_loan"]["due_date"], due)
        # 外借期间冻结：开箱、移交、派生、释放
        for call, code in (
            (lambda: self.store.open_evidence("custodian1", item["id"], "证物室"), "invalid_status"),
            (lambda: self.store.transfer("custodian1", item["id"], "custodian2", "法院库"), "evidence_on_loan"),
            (lambda: self.store.release("custodian1", item["id"], "鉴定中心"), "evidence_on_loan"),
            (lambda: self.store.derive("analyst1", item["id"], "数据分析", "D1", "x.bin",
                                       base64.b64encode(b"x").decode()), "evidence_not_opened"),
        ):
            with self.assertRaises(BusinessError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, code)
        # 外借中不能再次申请
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_loan("custodian1", item["id"], "另一机构", "补充鉴定", due)
        self.assertEqual(ctx.exception.code, "loan_open")
        # 报告列出借出对象和到期时间，事件链仍完整
        report = self.store.report("auditor1", self.case["id"])
        self.assertTrue(report["overall_integrity_valid"])
        self.assertEqual(report["active_loan_count"], 1)
        entry = report["active_loans"][0]
        self.assertEqual(entry["borrower"], "华夏鉴定中心")
        self.assertEqual(entry["due_date"], due)
        self.assertEqual(entry["status"], "active")
        # 归还：补记位置，回到原保管人，操作恢复
        returned = self.store.return_loan("custodian2", loan["id"], "A 区证物室 3 号柜", "鉴定完毕归还")
        self.assertEqual(returned["status"], "returned")
        self.assertEqual(returned["current_custodian"], "custodian1")
        detail = self.store.get_evidence("custodian1", item["id"])
        self.assertEqual(detail["status"], "custody")
        self.assertIsNone(detail["active_loan"])
        self.assertEqual(self.store.open_evidence("custodian1", item["id"], "A 区证物室")["status"], "opened")
        self.assertEqual(self.store.report("auditor1", self.case["id"])["active_loan_count"], 0)

    def test_creator_approval_reject_and_overdue(self):
        item = self._ingest("E-102")
        due = (date.today() + timedelta(days=7)).isoformat()
        # 案件创建人（非 auditor 角色也可）审批通过
        loan = self.store.request_loan("custodian1", item["id"], "鉴定机构甲", "电子数据鉴定", due)
        out = self.store.decide_loan("custodian1", loan["id"], True)
        self.assertEqual(out["status"], "active")
        # 驳回流程
        item2 = self._ingest("E-103")
        loan2 = self.store.request_loan("custodian1", item2["id"], "鉴定机构乙", "伤情复核", due)
        rejected = self.store.decide_loan("custodian1", loan2["id"], False)
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(self.store.get_evidence("custodian1", item2["id"])["status"], "custody")
        # 驳回后可以重新申请
        loan2b = self.store.request_loan("custodian1", item2["id"], "鉴定机构乙", "伤情复核鉴定", due)
        self.assertEqual(loan2b["status"], "pending")
        # 到期未还：读取/报告时标成逾期
        import app as app_module
        future = date.today() + timedelta(days=30)
        original_today = app_module.today
        app_module.today = lambda: future
        try:
            detail = self.store.get_evidence("auditor1", item["id"])
            self.assertEqual(detail["active_loan"]["status"], "overdue")
            report = self.store.report("auditor1", self.case["id"])
            self.assertEqual(report["overdue_loan_count"], 1)
            # 逾期未处理前不能再申请
            with self.assertRaises(BusinessError) as ctx:
                self.store.request_loan("custodian1", item["id"], "鉴定机构丙", "复检鉴定", due)
            self.assertEqual(ctx.exception.code, "loan_open")
            # 归还即解除逾期、恢复操作
            result = self.store.return_loan("custodian1", loan["id"], "A 区证物室")
            self.assertEqual(result["current_custodian"], "custodian1")
            future_due = (date.today() + timedelta(days=60)).isoformat()
            self.store.request_loan("custodian1", item["id"], "鉴定机构丙", "复检鉴定", future_due)
        finally:
            app_module.today = original_today

    def test_pending_blocks_second_request_and_not_freeze(self):
        item = self._ingest("E-104")
        due = (date.today() + timedelta(days=5)).isoformat()
        self.store.request_loan("custodian1", item["id"], "鉴定机构丁", "文件形成时间鉴定", due)
        with self.assertRaises(BusinessError) as ctx:
            self.store.request_loan("custodian1", item["id"], "鉴定机构戊", "其他用途鉴定", due)
        self.assertEqual(ctx.exception.code, "loan_pending")
        # 待审批期间证据仍在保管中，正常操作不受限
        self.assertEqual(self.store.open_evidence("custodian1", item["id"], "证物室")["status"], "opened")
        # 已开箱后批准应失败
        import app as app_module
        loan_id = self.store.get_evidence("custodian1", item["id"])["loans"][0]["id"]
        with self.assertRaises(BusinessError) as ctx:
            self.store.decide_loan("auditor1", loan_id, True)
        self.assertEqual(ctx.exception.code, "evidence_not_in_custody")


if __name__ == "__main__":
    unittest.main()
