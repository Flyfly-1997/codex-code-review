import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parent.parent / "plugins/code-review/skills/code-review/scripts"
prepare = runpy.run_path(str(SCRIPTS / "prepare-review.py"))
roles = runpy.run_path(str(SCRIPTS / "review-roles.py"))


class ReviewTests(unittest.TestCase):
    def test_partition_keeps_all_original_blocks(self):
        blocks = [[f"### src/module{n}.py — SOURCE", f"  1 +value = {n}"] for n in range(6)]
        result = prepare["balance_code_packs"]([("code", blocks)], 3)
        self.assertEqual(len(result), 3)
        self.assertCountEqual([block for pack in result for block in prepare["section_blocks"](pack)], blocks)

    def test_workflow_signals_for_generic_changes(self):
        with tempfile.TemporaryDirectory() as name:
            path = "worker.py"
            rows = [{"path": path, "diff": "@@ -0,0 +1,3 @@\n+wait_for(task, timeout)\n"
                                          "+expiresAt = renewed_deadline\n+outbox.append(event)\n"}]
            plan = prepare["workflow_pack"](Path(name), rows, [{"kind": "code", "files": [path]}])
            self.assertIn("异步失败与最终状态", plan["checks"])
            self.assertIn("提交、回执与恢复", plan["checks"])
            self.assertIn("生命周期与一次性约束", plan["checks"])

    def test_ledger_routes_and_generates_report(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory / "intent.md").write_text("Review two synthetic changes.", encoding="utf-8")
            plan = {"status": "ready", "directory": name, "snapshot": name, "cap": 2,
                    "kind": "branch", "target": "example", "head": "head", "base": "base", "pr": None,
                    "discussion": False, "history_paths": [], "guidelines": str(directory / "guidelines.md"),
                    "files": 2, "jobs": [
                        {"kind": "code", "lines": 2, "pack": str(directory / f"review-0{n}.md"),
                         "files": [f"src/module{n}.py"]} for n in (1, 2)
                    ]}
            (directory / "plan.json").write_text(json.dumps(plan), encoding="utf-8")

            def ledger(*args, data=None):
                result = subprocess.run([sys.executable, str(SCRIPTS / "review-ledger.py"), str(directory / "ledger.json"),
                                         *map(str, args)], input=data, capture_output=True, text=True, check=True)
                return json.loads(result.stdout)

            def candidate(n, covered):
                return (f"- file: src/module{n}.py\nline: 12\ncategory: bug\nsummary: Incorrect setting {n}\n"
                        f"evidence: Fixture {n}\n" + (f"COVERED: review-0{n}\n" if covered else ""))

            def verified(n):
                return (f"candidate: src/module{n}.py:12\nscore: 90\nverdict: confirmed\nseverity: medium\n"
                        f"trigger: proven\nentry: src/module{n}.py:5\nevidence: Verified through the entry.\n")

            initial = ledger("init", directory / "plan.json")
            self.assertEqual(initial["shards"], 2)
            self.assertNotIn("prompt", initial["readers"][0])
            ledger("record", "reader", "review-01", "reader-a", data=candidate(1, False))
            routed = ledger("record", "reader", "review-02", "reader-b", "--idle", "reader-a", "--idle", "reader-b",
                            data=candidate(2, True))
            self.assertEqual([task["worker"]["actor"] for task in routed["ready"]], ["reader-b", "reader-a"])
            ledger("record", "verifier", "verify-01", "reader-b", data=verified(1))
            waiting = ledger("record", "verifier", "verify-02", "reader-a", data=verified(2))
            self.assertNotIn("report", waiting)
            complete = ledger("record", "reader", "review-01", "reader-a", data=candidate(1, True))
            self.assertEqual(complete["independent"], 2)
            self.assertIn("⚠️ 修复后合并", Path(complete["report"]).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
