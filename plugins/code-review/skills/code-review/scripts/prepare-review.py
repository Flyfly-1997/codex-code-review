#!/usr/bin/env python3
"""Prepare a pinned, read-only Git review in a temporary directory."""

import argparse
import json
import math
import os
from pathlib import Path
import re
import runpy
import shutil
import statistics
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

SCRIPTS = Path(__file__).resolve().parent
SPLITTER = runpy.run_path(str(SCRIPTS / "split-diff.py"))
PACKER = SCRIPTS / "pack-review.py"
PACKER_API = runpy.run_path(str(PACKER))
PACK_LIMIT = 2200
BALANCED_PACK_LIMIT = 2640
FIELDS = (
    "number,title,body,state,isDraft,author,baseRefName,baseRefOid,headRefName,"
    "headRefOid,mergeStateStatus,mergeable,url,comments,reviews"
)
SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
PR_NUMBER = re.compile(r"(?:PR\s*#?\s*|#)?([1-9][0-9]*)\Z", re.I)
PR_PATH = re.compile(r"/[\w.-]+/[\w.-]+/pull/[1-9][0-9]*/?")
DOC = re.compile(r"^(?:specs|docs|designs)/", re.I)
PACK_HEADER = re.compile(r"^### (.+) — (?:SOURCE|TEST|RAW)$")
FENCE = re.compile(r"^```(?:text|diff)?$")
HUNK_RANGE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
UI_PATH = re.compile(r"(?:^|/)(?:ui|components|views|screens|pages)(?:/|$)", re.I)
WORKFLOW_SIGNALS = {
    "文本匹配与编辑边界": re.compile(
        r"word(?:Start|End|Boundary)|tokeniz\w*|normaliz\w*|\b(?:prefix|suffix|substring)\b|"
        r"\b(?:offset|cursor|index)\b.*(?:advance|match|validate)", re.I),
    "异步失败与最终状态": re.compile(
        r"Promise\.race|wait_for|withTimeout|timeout|cancelled|canceled|abortSignal|"
        r"\b(?:stream|queue|job|task)\w*.*(?:finish|drain|abort|close|error)", re.I),
    "生命周期与一次性约束": re.compile(
        r"\bsetnx\b|expiresAt|\bttl\b|replay|dedup\w*|singleUse|"
        r"\b(?:refresh|touch|expire|renew)\b.*(?:key|token|lease|record|session)", re.I),
    "输入来源与后续编辑": re.compile(
        r"provenance|sourceMapping|inputSource|contentVersion|optimisticLock|"
        r"\b(?:overwrite|replace|edit|import)\w*.*(?:source|version|record|document)", re.I),
    "确认、取消与时间边界": re.compile(
        r"acknowledge\w*|(?:confirmation|cancel|stop|decision)Window|"
        r"deadline|expired|expiry|expiresAt|\b(?:cooldown|gracePeriod|windowTimer)\b", re.I),
    "异常兜底与语言": re.compile(
        r"fallback(?:Message|Response|Text|Locale)|"
        r"(?:message|text|locale|language)Fallback|\b(?:locale|localization)\b.*(?:return|\?\?|\?)|"
        r"""\?\?\s*["'][^"']{8,}""", re.I),
    "提交、回执与恢复": re.compile(
        r"receipt\w*|idempoten\w*|outbox|onDelete|\bCASCADE\b|SET NULL|"
        r"recover\w*\(|\b(?:rollback|retry|compensate)\w*\(|(?:afterCommit|postCommit)\w*", re.I),
    "高频入口与外部调用": re.compile(
        r"\b(?:dao|store|repository)\.(?:get|find|load|query)\w*\(|"
        r"\b(?:request|fetch|query)\w*\(.*(?:transaction|lock)|\bN\+1\b", re.I),
}


def command(args, cwd=None, required=True, env=None):
    result = subprocess.run(
        args, cwd=cwd, env=env, capture_output=True, encoding="utf-8",
        errors="surrogateescape", stdin=subprocess.DEVNULL, check=False,
    )
    if result.returncode == 0:
        return result.stdout
    if required:
        raise ValueError(result.stderr.strip() or f"{args[0]} exited {result.returncode}")
    return None


def git(repo, *args, required=True, clean=False):
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    if clean:
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return command(
        ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
         "-c", "core.quotePath=false", "--literal-pathspecs", "-C", str(repo), *args],
        required=required, env=env,
    )


def write(path, content):
    path.write_text(content, encoding="utf-8", errors="backslashreplace")
    return path


def commit(repo, ref, required=True):
    result = git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}",
                 required=required)
    return result.strip() if result is not None else None


def parse_pr(target):
    number = PR_NUMBER.fullmatch(target)
    if number:
        return number[1]
    url = urlsplit(target)
    if (url.scheme == "https" and url.hostname and url.netloc == url.hostname
            and PR_PATH.fullmatch(url.path)):
        return f"https://{url.hostname}{url.path.rstrip('/')}"
    return None


def pr_view(selector, cwd, fields=FIELDS):
    return json.loads(command(["gh", "pr", "view", selector, "--json", fields], cwd=cwd))


def marker(head):
    return f"<!-- codex-code-review:{head} -->"


def already_reviewed(pr):
    for comment in pr.get("comments", []):
        lines = (comment.get("body") or "").splitlines()
        if comment.get("viewerDidAuthor") and marker(pr["headRefOid"]) in lines:
            return True
    return False


def snapshot_pr(checkout, source, pr, existing=False):
    selector = parse_pr(pr["url"])
    if selector is None or not selector.startswith("https://"):
        raise ValueError("GitHub did not return a usable pull request URL.")
    remote = selector.rsplit("/pull/", 1)[0] + ".git"
    refs = (("headRefOid", f"refs/pull/{int(pr['number'])}/head"),
            ("baseRefOid", f"refs/heads/{pr['baseRefName']}"))
    for field, ref in refs:
        revision = pr[field]
        if not SHA.fullmatch(revision):
            raise ValueError(f"Invalid GitHub commit ID: {field}")
        if commit(checkout, revision, required=False) is None:
            if existing:
                raise ValueError(f"Existing checkout is missing the pinned {field} commit: {revision}")
            git(checkout, "check-ref-format", ref)
            fetch = ("-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
                     "fetch", "--no-tags", "--no-recurse-submodules", "--", remote)
            fetched = git(checkout, *fetch, revision, required=False)
            if fetched is None:
                git(checkout, *fetch, ref)
            if commit(checkout, revision, required=False) is None:
                raise ValueError("The PR changed during preparation. Review the latest head again.")
    revision = pr["headRefOid"]
    base = git(checkout, "merge-base", pr["baseRefOid"], revision).strip()
    if existing:
        return base, revision, None, None
    diff = command(["gh", "pr", "diff", selector], cwd=source, required=False)
    discussion = command(["gh", "pr", "view", selector, "--comments"], cwd=source,
                         required=False)
    return base, revision, diff, discussion


def require_clean_head(checkout, head):
    if commit(checkout, "HEAD") != head or git(checkout, "status", "--porcelain=v1").strip():
        raise ValueError(f"An existing checkout must be clean and already at {head}: {checkout}")


def default_base(repo, target, explicit):
    if explicit:
        return commit(repo, explicit)
    symbolic = git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", required=False)
    refs = ([symbolic.strip()] if symbolic else []) + [
        "refs/remotes/origin/main", "refs/remotes/origin/master", "refs/heads/main",
        "refs/heads/master",
    ]
    for ref in refs:
        found = commit(repo, ref, required=False)
        if found:
            return found
    if not target or target == "HEAD":
        found = commit(repo, "HEAD^", required=False)
        if found:
            return found
    raise ValueError("Default base unavailable. Specify --base or an explicit A..B range.")


def path_target(source, target, head, base):
    path = Path(target)
    if path.is_absolute():
        try:
            path = path.relative_to(source)
        except ValueError:
            return None
    if ".." in path.parts:
        return None
    literal = path.as_posix()
    if literal == ".":
        return literal
    if any(git(source, "ls-tree", revision, "--", literal).strip() for revision in (head, base)):
        return literal
    return None


def local_target(repo, args):
    head = commit(repo, "HEAD")
    target = args.target
    if not args.path and ".." in target:
        separator = "..." if "..." in target else ".."
        left, right = target.split(separator, 1)
        base, head = commit(repo, left or "HEAD"), commit(repo, right or "HEAD")
        if separator == "...":
            base = git(repo, "merge-base", base, head).strip()
        return "range", base, head, None
    branch = commit(repo, target, required=False) if target and not args.path else None
    if branch and re.fullmatch(r"[0-9a-fA-F]{7,64}", target):
        parent = commit(repo, f"{branch}^", required=False)
        if parent is None:
            return "commit", None, branch, None
        return "commit", parent, branch, None
    base = default_base(repo, target if branch else "", args.base)
    path = None
    if args.path:
        path = path_target(repo, args.path, head, base)
    elif target and not branch:
        path = path_target(repo, target, head, base)
    if (args.path or target and not branch) and path is None:
        raise ValueError("Target not found inside the repository.")
    head = branch or head
    base = git(repo, "merge-base", base, head).strip()
    return "path" if path else "branch", base, head, path


def changed_files(stats, diff):
    rows = []
    for item in stats.split("\0"):
        if not item:
            continue
        added, removed, path = item.split("\t", 2)
        rows.append({"path": path, "added": added, "removed": removed})
    sections = re.split(r"(?m)^(?=diff --git )", diff)[1:]
    if len(rows) != len(sections):
        raise ValueError("Git patch and path inventory disagree. Review coverage unavailable.")
    for row, section in zip(rows, sections):
        row["diff"] = section
        row["lines"] = len(section.splitlines())
    return rows


def gather_guidelines(checkout, rows, base, head, output):
    names = ("AGENTS.md", "CLAUDE.md", "DESIGN.md", "LOCALIZATION.md")
    paths = {Path(row["path"]) for row in rows}
    ancestors = {parent for path in paths for parent in path.parents} | {Path(".")}
    rules = set()
    for ancestor in ancestors:
        for name in names if ancestor == Path(".") else names[:2]:
            rules.add((ancestor / name).as_posix())
    tree = git(checkout, "ls-tree", "-r", "-z", head, "--", "docs/agents")
    for entry in tree.split("\0"):
        if entry and entry.split("\t", 1)[1].endswith(".md"):
            rules.add(entry.split("\t", 1)[1])
    changed = {p.as_posix() for p in paths}
    catalog = []
    for rule in sorted(rules):
        file = checkout / rule
        if file.is_file() and not file.is_symlink() and file.resolve().is_relative_to(checkout):
            catalog.append(f"## {rule} @ head\n\n" + file.read_text(errors="replace"))
        if base and rule in changed:
            old = git(checkout, "show", f"{base}:{rule}", required=False)
            if old is not None:
                catalog.append(f"## {rule} @ base\n\n" + old)
    return write(output / "guidelines.md", "\n\n".join(catalog))


def safe_pack_path(row, checkout):
    path = row["path"]
    header = row["diff"].partition("\n")[0]
    if not re.fullmatch(r"[^\s\"\\\x00-\x1f]+", path) or header != f"diff --git a/{path} b/{path}":
        return False
    file = checkout / path
    return (not file.is_symlink() and file.resolve().is_relative_to(checkout)
            and (not file.exists() or file.is_file()))


def section_blocks(lines):
    blocks = []
    in_fence = False
    for line in lines:
        if not in_fence and PACK_HEADER.fullmatch(line):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
        if FENCE.fullmatch(line):
            in_fence = not in_fence
    return blocks


def raw_block(row):
    label = json.dumps(row["path"], ensure_ascii=False)
    return [f"### {label} — RAW", "```diff", *row["diff"].splitlines(), "```"]


def packed_paths(lines):
    paths = []
    for block in section_blocks(lines):
        heading = block[0]
        path = PACK_HEADER.fullmatch(heading)[1]
        paths.append(json.loads(path) if heading.endswith(" — RAW") else path)
    return list(dict.fromkeys(paths))


def pack_units(blocks, limit):
    groups, pending = [], []
    for block in blocks:
        if len(block) > limit:
            if pending:
                groups.append(pending)
                pending = []
            header, *body = block
            width = limit - 8
            old = new = None
            fence = None
            for start in range(0, len(body), width):
                segment = body[start:start + width]
                label = f"(excerpt {start + 1}–{start + len(segment)})"
                if old is not None:
                    label += f" — next old line {old}, next new line {new}"
                part = [header, label, *([fence] if fence else []), *segment]
                for line in segment:
                    if FENCE.fullmatch(line):
                        fence = None if fence else line
                    if header.endswith(" — RAW"):
                        hunk = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
                        if hunk:
                            old, new = map(int, hunk.groups())
                        elif old is not None:
                            old += line.startswith((" ", "-"))
                            new += line.startswith((" ", "+"))
                if fence:
                    part.append("```")
                groups.append(part)
        else:
            if pending and len(pending) + len(block) > limit:
                groups.append(pending)
                pending = []
            pending.extend(block)
    if pending:
        groups.append(pending)
    return groups


def balance_code_packs(batches, slots):
    """Remove a spill wave when intact file blocks fit across the available workers."""
    sections = {}
    for _, blocks in batches:
        for block in blocks:
            path, = packed_paths(block)
            sections.setdefault(path, []).extend(block)
    limit = BALANCED_PACK_LIMIT - 4
    if len(sections) < slots or sum(map(len, sections.values())) > slots * limit:
        return None
    packs = [[] for _ in range(slots)]
    for lines in sorted(sections.values(), key=len, reverse=True):
        lightest = min(packs, key=len)
        if len(lightest) + len(lines) > limit:
            return None
        lightest.extend(lines)
    return packs


def build_packs(output, checkout, rows, requested, cap):
    skipped = {row["path"] for row in rows if SPLITTER["SKIP"].search(row["path"])}
    binary = {row["path"] for row in rows if row["added"] == "-"} - skipped
    excluded = skipped | binary
    relevant = [row for row in rows if row["path"] not in excluded]
    docs = [row for row in relevant if DOC.search(row["path"]) or
            (row["path"].lower().endswith(".md") and row["lines"] > 320)]
    doc_paths = {row["path"] for row in docs}
    source = [row for row in relevant if row["path"] not in doc_paths]
    eligible = [row for row in source if safe_pack_path(row, checkout)]
    by_path = {row["path"]: row for row in eligible}
    raw = [row for row in source if row["path"] not in by_path]
    chunks = output / "chunks"
    chunks.mkdir(exist_ok=True)
    if eligible:
        source_diff = write(output / "code.diff", "".join(row["diff"] for row in eligible))
        command([sys.executable, str(SCRIPTS / "split-diff.py"), str(source_diff), str(chunks),
                 "--chunks", str(requested)])
        command([sys.executable, str(PACKER), str(output / "change.diff"), str(checkout),
                 str(chunks), str(output / "packs")])

    batches = []
    for chunk in sorted(chunks.glob("chunk-*.diff")):
        number = chunk.stem.split("-")[1]
        blocks = []
        for pack in sorted((output / "packs").glob(f"pack-{number}*.md"),
                           key=lambda path: int(path.stem.rsplit("-", 1)[-1])):
            for block in section_blocks(pack.read_text().splitlines()):
                name = PACK_HEADER.fullmatch(block[0])[1]
                row = by_path[name]
                blocks.append(block)
                if "```diff" in block:
                    if not any(line.startswith("@@ ") for line in block):
                        blocks.append(raw_block(row))
                    continue
                expected = {number for _, added, _ in PACKER_API["scan_hunks"](row["diff"])
                            for number, _ in added}
                numbered = {int(m[1]) for line in block if (m := re.match(r"^ *(\d+) \+", line))}
                if row["removed"] != "0" or expected - numbered:
                    blocks.append(raw_block(row))
        batches.append(("code", blocks))
    if raw:
        raw_blocks = [raw_block(row) for row in raw]
        if requested == 1 and batches:
            batches[0][1].extend(raw_blocks)
        else:
            batches.append(("code", raw_blocks))
    if docs:
        batches.append(("docs", [raw_block(row) for row in docs]))

    # Capacity plans use actual file lines, including complete raw hunks where the excerpt is short.
    code_sizes = [sum(map(len, blocks)) for kind, blocks in batches if kind == "code" and blocks]
    typical = statistics.median(code_sizes) if code_sizes else PACK_LIMIT
    prepared = []
    for kind, blocks in batches:
        total = sum(map(len, blocks))
        if kind == "code" and len(code_sizes) > 1 and total > 1.8 * typical:
            limit = min(PACK_LIMIT - 4, max(700, math.ceil(total / 2)))
        else:
            limit = PACK_LIMIT - 4
        prepared.extend((kind, lines) for lines in pack_units(blocks, limit))
    if requested > 1:
        doc_packs = [(kind, lines) for kind, lines in prepared if kind == "docs"]
        code_slots = cap - len(doc_packs)
        if 1 < code_slots < len(prepared) - len(doc_packs):
            balanced = balance_code_packs([(kind, blocks) for kind, blocks in batches if kind == "code"], code_slots)
            if balanced:
                prepared = [("code", lines) for lines in balanced] + doc_packs
    jobs = []
    for kind, lines in prepared:
        path = write(output / f"review-{len(jobs) + 1:02d}.md", "\n".join(lines) + "\n")
        jobs.append({"pack": str(path), "kind": kind, "lines": len(lines), "files": packed_paths(lines)})
    return sorted(jobs, key=lambda job: -job["lines"]), skipped, binary


def finish_packs(plan, rows):
    output = Path(plan["directory"])
    checkout = Path(plan["snapshot"])
    if plan.get("existing_checkout"):
        require_clean_head(checkout, plan["head"])
    jobs, skipped, binary = build_packs(output, checkout, rows, plan["requested_code_chunks"], plan["cap"])
    top = sorted((row for row in rows if row["path"] not in skipped
                  and row["path"] not in binary and not SPLITTER["TEST_PATH"].search(row["path"])),
                 key=lambda row: -row["lines"])[:10]
    plan.update(status="ready", jobs=jobs, excluded_generated=sorted(skipped),
                binary_paths=sorted(binary), history_paths=[row["path"] for row in top])
    plan["workflow"] = workflow_pack(output, rows, jobs)
    write(output / "plan.json", json.dumps(plan, ensure_ascii=True, indent=2))
    return plan


def changed_lines(patch):
    base = head = None
    for line in patch.splitlines():
        hunk = HUNK_RANGE.match(line)
        if hunk:
            base, head = map(int, hunk.groups())
        elif head is not None and line.startswith("+"):
            yield "head", head, line[1:]
            head += 1
        elif base is not None and line.startswith("-"):
            yield "base", base, line[1:]
            base += 1
        elif head is not None and line.startswith(" "):
            base += 1
            head += 1


def workflow_pack(output, rows, jobs):
    """Index changed lines for a bounded pass across callers and user-visible results."""
    code_files = {path for job in jobs if job["kind"] == "code" for path in job["files"]}
    hits = {label: {} for label in WORKFLOW_SIGNALS}
    for row in rows:
        path = row["path"]
        if path not in code_files or SPLITTER["TEST_PATH"].search(path):
            continue
        for side, number, line in changed_lines(row["diff"]):
            cue = line.strip()
            if cue.startswith("import ") or re.fullmatch(r"\w+,?", cue):
                continue
            for label, pattern in WORKFLOW_SIGNALS.items():
                matches = pattern.findall(cue)
                if matches:
                    hits[label].setdefault(path, []).append(
                        (side, number, cue[:220], {text.lower() for text in matches})
                    )

    active = [label for label, files in hits.items() if files]
    if not active:
        return None
    sections = ["# 跨文件操作路径线索", "",
                "下列是 diff 中的定位提示。复读实际代码后再判断行为。"]
    anchors = {}
    for label in active:
        files = hits[label]
        remaining = {path: set().union(*(entry[3] for entry in entries)) for path, entries in files.items()}
        seen = set()
        sections.extend(("", f"## {label}"))
        count = min(4, len(remaining))
        has_ui = False
        for slot in range(count):
            pool = list(remaining)
            if slot == count - 1 and not has_ui and label in (
                "输入来源与后续编辑", "确认、取消与时间边界", "异常兜底与语言", "文本匹配与编辑边界"
            ):
                pool = [path for path in pool if UI_PATH.search(path)] or pool
            path = min(pool, key=lambda name: (-len(remaining[name] - seen), -len(remaining[name]), name))
            has_ui |= bool(UI_PATH.search(path))
            new_signals = remaining.pop(path) - seen
            seen.update(new_signals)
            anchors.setdefault(path, []).append(label)
            cues = files[path]
            used = set()
            for _ in range(min(2, len(cues))):
                cue = min(cues, key=lambda item: (
                    -len(item[3].intersection(new_signals) - used), -len(item[3] - used),
                    item[0] != "head", item[1]
                ))
                side, number, line, terms = cue
                cues.remove(cue)
                used.update(terms)
                sections.append(f"- {json.dumps(path, ensure_ascii=False)}:{number} ({side}) {line}")
    return {
        "path": str(write(output / "workflow.md", "\n".join(sections) + "\n")),
        "checks": active,
        "files": sorted(anchors),
        "file_checks": anchors,
        "cross_file": len(active) >= 2 and len(anchors) >= 2,
    }


def pack(args):
    plan = json.loads(Path(args.plan).read_text())
    if plan["status"] == "ready":
        return plan
    output = Path(plan["directory"])
    intent = (output / "intent.md").read_text().splitlines()
    if plan["status"] != "needs-intent" or not 3 <= sum(bool(line.strip()) for line in intent) <= 6:
        raise ValueError("Write 3–6 lines of review intent to intent.md before creating the packs.")
    rows = changed_files((output / "numstat").read_bytes().decode("utf-8", "surrogateescape"),
                         (output / "change.diff").read_bytes().decode("utf-8", "surrogateescape"))
    return finish_packs(plan, rows)


def prepare(args):
    root = git(args.repo, "rev-parse", "--show-toplevel", required=False)
    source = Path(root.strip()).resolve() if root else None
    selector = parse_pr(args.target) if not args.path else None
    if args.existing_checkout and source is None:
        raise ValueError("--existing-checkout requires an existing local Git repository.")
    if source is None and not (selector and selector.startswith("https://")):
        raise ValueError("Open a Git repository or pass a complete GitHub PR URL.")
    pr = pr_view(selector, source) if selector else None
    if args.expected_head and not (pr and pr["headRefOid"] == args.expected_head):
        raise ValueError(f"The pull request is not at the expected head {args.expected_head}.")
    if args.comment and not pr:
        raise ValueError("--comment requires a GitHub pull request.")
    if pr and not args.rereview:
        reason = "draft" if pr["isDraft"] else None
        if pr["state"] != "OPEN":
            reason = "closed"
        if reason is None and already_reviewed(pr):
            reason = "already-reviewed"
        if reason:
            return {"status": "skipped", "reason": reason, "pr": pr["url"]}

    temp_root = Path(tempfile.gettempdir()).resolve()
    if source and temp_root.is_relative_to(source):
        raise ValueError("Temporary directory must be outside the repository.")
    output = Path(tempfile.mkdtemp(prefix="codex-code-review-", dir=temp_root)).resolve()
    try:
        checkout = output / "head"
        if args.existing_checkout:
            checkout = source
        elif source:
            git(output, "clone", "--shared", "--no-checkout", "--template=", "--",
                str(source), str(checkout), clean=True)
        else:
            checkout.mkdir()
            git(checkout, "init", "--template=", clean=True)
        gh_diff, discussion = None, None
        if pr:
            base, head, gh_diff, discussion = snapshot_pr(checkout, source, pr, args.existing_checkout)
            kind, path = "pr", None
        else:
            kind, base, head, path = local_target(source, args)
        if args.existing_checkout:
            require_clean_head(checkout, head)
        else:
            git(checkout, "checkout", "--detach", "--no-recurse-submodules", head, clean=True)
        if base is None:
            base = git(checkout, "hash-object", "-t", "tree", "--stdin", clean=True).strip()
        diff_args = ("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--no-color",
                     "--ignore-submodules=dirty", "--src-prefix=a/", "--dst-prefix=b/")
        scope = (base, head, "--", path) if path else (base, head, "--")
        diff = git(checkout, *diff_args, *scope, clean=True)
        stat = git(checkout, *diff_args, "--numstat", "-z", *scope, clean=True)
        use_github_diff = bool(gh_diff and gh_diff == diff)
        if use_github_diff:
            diff = gh_diff
        rows = changed_files(stat, diff)
        (output / "change.diff").write_bytes(diff.encode("utf-8", "surrogateescape"))
        (output / "numstat").write_bytes(stat.encode("utf-8", "surrogateescape"))
        requested = 1
        diff_lines = len(diff.splitlines())
        if args.effort != "low" and (len(rows) > 25 or diff_lines > 1200):
            requested = min(16, max(2, args.cap - 2), max(2, math.ceil(diff_lines / 900)))
        rules = gather_guidelines(checkout, rows, base, head, output)
        log = git(checkout, "log", "--format=%h %s", "-20", f"{base}..{head}", required=False)
        if log is None:
            log = git(checkout, "log", "--format=%h %s", "-1", head)
        write(output / "commits.txt", log)
        if pr:
            write(output / "pr.json", json.dumps(pr, ensure_ascii=False, indent=2))
            if discussion:
                write(output / "discussion.txt", discussion)
        dirty = bool(source and kind != "pr" and git(source, "status", "--porcelain=v1").strip())
        plan = {
            "status": "needs-intent", "directory": str(output), "snapshot": str(checkout),
            "kind": kind, "target": args.target or args.path or "HEAD", "path": path,
            "base": base, "head": head, "source_repo": str(source) if source else None,
            "existing_checkout": args.existing_checkout,
            "pr": pr["url"] if pr else None, "comment_requested": args.comment,
            "discussion": bool(pr and (pr.get("comments") or pr.get("reviews"))),
            "diff_source": "github" if use_github_diff else "git",
            "source_has_uncommitted_changes": dirty, "guidelines": str(rules),
            "files": len(rows), "added": sum(int(row["added"]) for row in rows if row["added"] != "-"),
            "removed": sum(int(row["removed"]) for row in rows if row["removed"] != "-"),
            "diff_lines": diff_lines, "cap": args.cap, "requested_code_chunks": requested,
        }
        if not args.defer_packs:
            return finish_packs(plan, rows)
        write(output / "plan.json", json.dumps(plan, ensure_ascii=False, indent=2))
        return plan
    except BaseException:
        shutil.rmtree(output)
        raise


def publish(args):
    plan = json.loads(Path(args.plan).read_text())
    selector = parse_pr(plan.get("pr") or "")
    if (plan.get("status") != "ready" or not plan.get("comment_requested") or
            not selector or not selector.startswith("https://")):
        raise ValueError("Posting requires an explicit --comment on a GitHub PR review.")
    pr = pr_view(selector, None, "state,isDraft,headRefOid,comments")
    if pr["state"] != "OPEN" or pr["isDraft"] or pr["headRefOid"] != plan["head"]:
        raise ValueError("The PR is closed, a draft, or its head changed. Comment was not sent.")
    if already_reviewed(pr):
        return {"status": "already-posted"}
    path = Path(args.report)
    body = path.read_text().rstrip()
    if not body:
        raise ValueError("The review comment is empty.")
    with tempfile.TemporaryDirectory(prefix="codex-review-comment-") as tmp:
        comment = write(Path(tmp) / "comment.md", body + "\n\n" + marker(plan["head"]) + "\n")
        result = command(["gh", "pr", "comment", selector, "--body-file", str(comment)])
    return {"status": "posted", "url": result.strip()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="action", required=True)
    prep = modes.add_parser("prepare")
    prep.add_argument("target", nargs="?", default="")
    prep.add_argument("--repo", default=os.getcwd())
    prep.add_argument("--path")
    prep.add_argument("--base")
    prep.add_argument("--cap", type=int, choices=range(1, 65), default=6, metavar="1–64")
    prep.add_argument("--effort", choices=("low", "medium", "high", "max"), default="medium")
    prep.add_argument("--comment", action="store_true")
    prep.add_argument("--rereview", action="store_true")
    prep.add_argument("--defer-packs", action="store_true")
    prep.add_argument("--existing-checkout", action="store_true")
    prep.add_argument("--expected-head", help="Require this full PR head commit ID.")
    build = modes.add_parser("pack")
    build.add_argument("plan")
    send = modes.add_parser("post")
    send.add_argument("plan")
    send.add_argument("report")
    args = parser.parse_args()
    try:
        result = {"prepare": prepare, "pack": pack, "post": publish}[args.action](args)
        print(json.dumps(result, ensure_ascii=True))
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
