#!/usr/bin/env python3
"""Check exactly what the public repository tracks, including hidden manifests."""

import ast
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = "plugins/code-review"
SKILL = f"{PLUGIN}/skills/code-review"
REFERENCES = ("history", "lenses", "prior-discussion", "prompts-README", "report", "reviewer",
              "second-reader", "verifier", "workflow")
SCRIPTS = ("pack-review", "prepare-review", "review-ledger", "review-roles", "split-diff")
FILES = {
    ".agents/plugins/marketplace.json", ".github/workflows/validate.yml", ".gitignore", "README.md",
    "scripts/check_release.py", "tests/test_review.py", f"{PLUGIN}/plugin.json",
    f"{PLUGIN}/.codex-plugin/plugin.json", f"{SKILL}/SKILL.md", f"{SKILL}/agents/openai.yaml",
    *(f"{SKILL}/references/{name}.md" for name in REFERENCES),
    *(f"{SKILL}/scripts/{name}.py" for name in SCRIPTS),
}
SENSITIVE = {
    "local path": re.compile(r"/(?:Users|home)/[^\s/]+/|/private/(?:var)/folders/|[A-Za-z]:\\Users\\[^\s\\]+\\"),
    "chat link": re.compile(r"codex://(?:threads|review)"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "credential-like value": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{24,})\b"
    ),
}


def git(*command):
    return subprocess.check_output(["git", "-C", str(ROOT), *command])


def require(condition, message):
    if not condition:
        raise ValueError(message)


def check():
    files = {}
    for entry in git("ls-files", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        header, raw_path = entry.split(b"\t", 1)
        mode, oid, stage = header.split()
        path = raw_path.decode()
        require(mode in (b"100644", b"100755") and stage == b"0", f"Unsupported Git entry: {path}")
        files[path] = oid.decode()

    require(set(files) == FILES, f"Unexpected paths: {sorted(files.keys() - FILES)}. Missing: {sorted(FILES - files.keys())}")
    text = {}
    for path, oid in sorted(files.items()):
        raw = git("cat-file", "blob", oid)
        require(not (ROOT / path).is_symlink() and (ROOT / path).read_bytes() == raw, f"Working file differs: {path}")
        decoded = raw.decode("utf-8")
        require("\0" not in decoded, f"Non-text file: {path}")
        for name, pattern in SENSITIVE.items():
            require(not pattern.search(decoded), f"Possible {name} in {path}")
        if path.endswith(".py"):
            ast.parse(decoded, filename=path, feature_version=(3, 9))
        text[path] = decoded

    marketplace = json.loads(text[".agents/plugins/marketplace.json"])
    require(marketplace["name"] == "codex-code-review" and marketplace["plugins"] == [
        {"name": "code-review", "source": {"source": "local", "path": f"./{PLUGIN}"}}
    ], "Marketplace must refer to the shipped plugin using a relative path.")
    portable = json.loads(text[f"{PLUGIN}/plugin.json"])
    compatibility = json.loads(text[f"{PLUGIN}/.codex-plugin/plugin.json"])
    require(portable["name"] == compatibility["name"] == "code-review", "Plugin identity differs.")
    require(portable["version"] == compatibility["version"], "Plugin versions differ.")
    require(re.fullmatch(r"\d+\.\d+\.\d+", portable["version"]), "Invalid release version.")
    require(compatibility["skills"] == "./skills/", "Invalid skill path.")
    for name in re.findall(r"\]\((references/[^)]+)\)", text[f"{SKILL}/SKILL.md"]):
        require(f"{SKILL}/{name}" in files, f"Missing skill reference: {name}")
    print(f"Validated {len(files)} tracked text files and Code Review {portable['version']}.")


if __name__ == "__main__":
    try:
        check()
    except (OSError, ValueError, SyntaxError, KeyError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
