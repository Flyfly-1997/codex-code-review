#!/usr/bin/env python3
"""Collect review results, assign independent workers, and render a report draft."""

import argparse
from collections import Counter
import json
from pathlib import Path
import runpy
import sys
import tempfile

roles = runpy.run_path(str(Path(__file__).with_name("review-roles.py")))
read_json = roles["read_json"]
claim_key = roles["claim_key"]
VERDICTS = ("confirmed", "rejected", "cannot-verify")


def save(path, value):
    stream = tempfile.NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", delete=False)
    temporary = Path(stream.name)
    try:
        with stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def store_prompt(directory, task):
    path = directory / f"{task['id']}.prompt"
    path.write_text(task["prompt"], encoding="utf-8")
    return {key: task[key] for key in ("id", "role", "checks", "worker") if key in task} | {"prompt_path": str(path)}


def inventory(state):
    return [item for reader in state["read_results"].values() for item in reader["candidates"]]


def selected(state):
    candidates = inventory(state)
    indexes = {claim_key(item): index for index, item in enumerate(candidates)}
    ranking = state["ranking"]
    current = {"groups": [[indexes[key] for key in group if key in indexes] for group in ranking.get("groups", [])],
               "self": [indexes[key] for key in ranking.get("self", []) if key in indexes]}
    pinned = {claim["key"] for task in state["verifiers"] for claim in task["claims"]}
    return roles["shortlist"](candidates, current, pinned)


def gaps_and_checks(state):
    gaps = []
    covered = 0
    for task in state["readers"]:
        if task["role"] not in ("reviewer", "workflow"):
            continue
        result = state["read_results"].get(task["id"], {})
        if result.get("malformed", True) or task["id"] not in result.get("covered", []):
            gaps.append(task["id"])
        else:
            covered += task["role"] == "reviewer"
        for check in task.get("checks", []):
            if not any(line.startswith(check) for line in result.get("checked", [])):
                gaps.append(f"{task['id']} / {check}")
    return gaps, covered


def review_state(state):
    shortlist = selected(state)
    independent = {claim_key(item): item for item in shortlist["independent"]}
    self_review = {claim_key(item): item for item in shortlist["self_review"]}
    audit = roles["audit_verifications"](state["verifiers"], list(state["verify_results"].values()))
    audited = {item["key"]: item for item in audit["decisions"] if item["key"] in independent}
    verified = [item for item in audited.values() if item["independent"]]
    self_results = [state["self_results"][key] for key in self_review if key in state["self_results"]]
    pending = [f"{item['file']}:{item['line']}" for key, item in independent.items()
               if key not in audited or not audited[key]["independent"]]
    pending.extend(f"{item['file']}:{item['line']}" for key, item in self_review.items()
                   if key not in state["self_results"])
    gaps, covered = gaps_and_checks(state)
    return {"gaps": gaps, "covered": covered, "pending": pending, "capped_out": shortlist["capped_out"],
            "independent": len(verified), "self_reviewed": len(self_results),
            "decisions": verified + self_results, "items": independent | self_review}


def draft(plan, state, review):
    found = [item for item in review["decisions"] if item["verdict"] == "confirmed"]
    severity = Counter(item["severity"] for item in found)
    needed = review["gaps"] + review["pending"]
    unresolved = needed + [item["candidate"] for item in review["decisions"] if item["verdict"] == "cannot-verify"]
    unresolved.extend(point for result in state["read_results"].values() for point in result["deep"])
    verdict = ("❔ 无法判断" if unresolved else "⛔ 合并前必须修复" if severity["high"]
               else "⚠️ 修复后合并" if severity["medium"] else "✅ 可以合并")
    count = f"{severity['high']} 高、{severity['medium']} 中、{severity['low']} 低"
    lines = [f"**合并结论：{verdict}**（{count}）", "",
             f"验证状态：{review['independent']} 条独立复核，{review['self_reviewed']} 条自复核。",
             "仅代表静态审查。构建、测试、运行时行为与审批状态需另行说明。", "",
             f"## 代码评审 · {plan['target']}", "", "**改动概要**", "", "<!-- 填入改动概要 -->"]
    number = 0
    for level, title in (("high", "🔴 高"), ("medium", "🟡 中"), ("low", "🟢 低")):
        lines.extend(("", f"### {title}严重度（{severity[level]}）", ""))
        if not severity[level]:
            lines.append("无")
        for item in found:
            if item["severity"] == level:
                number += 1
                candidate = review["items"][item["key"]]
                location = item.get("location") or item["candidate"]
                lines.append(f"{number}. **{candidate['summary']}** `{location}`")
                lines.append(f"   - 证据：{' '.join(item['evidence'].split())}")
                lines.append("   - 建议：<!-- 依据已复核证据填写 -->")
    lines.extend(("", "### 未解决", "", *([f"- {point}" for point in dict.fromkeys(unresolved)] or ["无"]),
                  "", "### 审查范围", "",
                  f"- 提交 `{plan['head']}`，变更 {plan['files']} 个文件。",
                  f"- 完整分片 {review['covered']}/{len(plan['jobs'])}。候选裁剪 {review['capped_out']} 条。"))
    return "\n".join(lines) + "\n"


def record(state, args):
    tasks = state["readers"] if args.role == "reader" else state["verifiers"]
    task = next((task for task in tasks if task["id"] == args.task), None)
    if task is None or not args.actor.strip():
        raise ValueError("Record a scheduled task with its actual host worker ID.")
    text = sys.stdin.read()
    if args.role == "reader":
        parsed = roles["reader_result"](text, task["role"], args.actor)
        results = state["read_results"]
        result = parsed | {"actor": args.actor}
    else:
        worker = task["worker"]
        if worker.get("actor", args.actor) != args.actor:
            raise ValueError("Verifier result must come from the assigned worker.")
        results = state["verify_results"]
        used = {item["actor"] for item in state["read_results"].values()}
        used.update(item["actor"] for task_id, item in results.items() if task_id != args.task)
        if worker.get("fresh") and args.actor in used:
            raise ValueError("This task was assigned to a fresh verifier, not a previously recorded worker.")
        result = {"task": args.task, "actor": args.actor, "text": text}
    previous = results.get(args.task)
    if previous and previous["actor"] != args.actor:
        raise ValueError("A completed result cannot be attributed to a different worker.")
    results[args.task] = result


def update(args):
    if args.action == "init":
        if args.ledger.exists():
            raise ValueError("Ledger already exists. Use status to resume it.")
        plan_path = args.plan.resolve()
        plan = read_json(plan_path)
        if plan["status"] != "ready" or args.ledger.resolve().parent != Path(plan["directory"]).resolve():
            raise ValueError("Initialize the ledger in the prepared review directory.")
        tasks = roles["reader_tasks"](plan, args.effort, args.history, args.second)
        readers = [store_prompt(args.ledger.parent, task) for task in tasks]
        state = {"plan": str(plan_path), "readers": readers, "read_results": {}, "verifiers": [],
                 "verify_results": {}, "self_results": {}, "ranking": {}}
        save(args.ledger, state)
        return {"readers": readers, "files": plan["files"], "shards": len(plan["jobs"]), "cap": plan["cap"]}

    state = read_json(args.ledger)
    plan = read_json(Path(state["plan"]))
    if args.action == "record":
        record(state, args)
    elif args.action == "inventory":
        return [{"key": claim_key(item), "file": item["file"], "line": item["line"], "summary": item["summary"][:200]}
                for item in inventory(state)]
    elif args.action == "rank":
        state["ranking"] = json.load(sys.stdin)
    elif args.action == "retry":
        task = next((task for task in state["verifiers"] if task["id"] == args.task), None)
        previous = state["verify_results"].get(args.task)
        if task is None or previous is None:
            raise ValueError("Retry requires a returned verifier task.")
        audit = roles["audit_verifications"]([task], [previous])
        if all(item["independent"] for item in audit["decisions"]):
            raise ValueError("An independently reviewed task does not need an identity or format retry.")
        task["worker"] = {"fresh": True}
        del state["verify_results"][args.task]
        ready = [store_prompt(args.ledger.parent, task)]
    elif args.action == "self":
        item = next((item for item in selected(state)["self_review"] if claim_key(item) == args.key), None)
        evidence = sys.stdin.read().strip()
        if item is None or not evidence:
            raise ValueError("Self-review requires an eligible candidate and actual evidence.")
        state["self_results"][args.key] = {"key": args.key, "candidate": f"{item['file']}:{item['line']}",
                                          "verdict": args.verdict, "severity": "low", "evidence": evidence}

    if args.action in ("record", "rank", "dispatch"):
        active = {task.get("worker", {}).get("actor") for task in state["verifiers"]
                  if task["id"] not in state["verify_results"]}
        idle = getattr(args, "idle", [])
        fresh = getattr(args, "fresh", 0)
        if active.intersection(idle):
            raise ValueError("An assigned verifier is not idle until its result has been recorded.")
        required = (task for task in state["readers"] if task["role"] in ("reviewer", "workflow"))
        result = roles["dispatch_verifiers"](plan, selected(state)["independent"], state["verifiers"],
                                             len(idle) + fresh, all(task["id"] in state["read_results"] for task in required),
                                             {"idle": idle, "fresh": fresh})
        state["verifiers"] = result["tasks"]
        ready = [store_prompt(args.ledger.parent, task) for task in result["ready"]]
    elif args.action != "retry":
        ready = []

    review = review_state(state)
    if args.action in ("record", "rank", "dispatch", "self", "retry"):
        save(args.ledger, state)
    status = {key: review[key] for key in ("gaps", "pending", "covered", "independent", "self_reviewed", "capped_out")}
    status["ready"] = ready
    status["unrecorded_readers"] = [task["id"] for task in state["readers"] if task["id"] not in state["read_results"]]
    status["awaiting_verifiers"] = [
        {"id": task["id"], "worker": task["worker"], "prompt_path": str(args.ledger.parent / f"{task['id']}.prompt")}
        for task in state["verifiers"] if task["id"] not in state["verify_results"]
    ]
    if (not review["gaps"] and not review["pending"]) or (args.action == "draft" and args.partial):
        report = args.ledger.parent / "report-draft.md"
        report.write_text(draft(plan, state, review), encoding="utf-8")
        status["report"] = str(report)
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger", type=Path)
    actions = parser.add_subparsers(dest="action", required=True)
    init = actions.add_parser("init")
    init.add_argument("plan", type=Path)
    init.add_argument("--effort", choices=("medium", "high", "max"), default="medium")
    init.add_argument("--history", action="store_true")
    init.add_argument("--second", action="append", default=[])
    record_result = actions.add_parser("record")
    record_result.add_argument("role", choices=("reader", "verifier"))
    record_result.add_argument("task")
    record_result.add_argument("actor")
    dispatch = actions.add_parser("dispatch")
    for action in (record_result, dispatch):
        action.add_argument("--idle", action="append", default=[])
        action.add_argument("--fresh", type=int, default=0)
    actions.add_parser("status")
    actions.add_parser("inventory")
    actions.add_parser("rank")
    actions.add_parser("retry").add_argument("task")
    self_review = actions.add_parser("self")
    self_review.add_argument("key")
    self_review.add_argument("verdict", choices=VERDICTS)
    actions.add_parser("draft").add_argument("--partial", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(update(args), ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
