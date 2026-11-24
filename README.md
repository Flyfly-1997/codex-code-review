# Code Review for Codex

Review committed Git changes from Codex. The plugin prepares a pinned diff, splits it into reading packs, and uses independent verification when the host has worker capacity. The final report includes evidence, severity, coverage, and a merge verdict.

## Install

Use a Codex version with the `codex plugin` commands:

```sh
codex plugin marketplace add https://github.com/lisanlai/codex-code-review.git --ref main
codex plugin add code-review@codex-code-review
```

To update an existing installation:

```sh
codex plugin marketplace upgrade codex-code-review
codex plugin add code-review@codex-code-review
```

The review runs locally. It requires Python 3.9+ and Git. GitHub pull request review also uses the [GitHub CLI](https://cli.github.com/) and the repository permissions of your login. The bundled scripts use Python's standard library. Use macOS, Linux, or a Linux environment such as WSL.

## Use

Open your repository in Codex and select **Code Review** or invoke `$code-review:code-review` in chat. Example requests:

```text
$code-review:code-review Review the current branch. Write the report in English.
$code-review:code-review PR #123
$code-review:code-review https://github.com/OWNER/REPOSITORY/pull/123
$code-review:code-review release..feature/search
$code-review:code-review --path src/auth
```

Branches, commit ranges, commits and paths work in any local Git repository. The direct PR-number and PR-URL forms use GitHub. Reports default to Chinese and follow an explicitly requested report language.

| Option | Use |
| --- | --- |
| `low`, `medium`, `high`, `max` | Select review depth. Default is `medium`. |
| `--base <ref>` | Use a specific comparison base for a local branch or path review. |
| `--existing-checkout` | Reuse a clean checkout at the target commit without creating a separate Git snapshot. |
| `--expected-head <full-sha>` | Require a GitHub PR to match a specific commit. |
| `--rereview` | Review again when the same revision has already been reviewed. |
| `--max-findings <n>` | Limit issues displayed in the report. |
| `--comment` | Explicitly send one summary comment to the reviewed GitHub PR. By default, the report stays in chat. |

The workflow reads committed changes and surfaces any limits in coverage or independent verification. It does not edit the target repository or run its project code. Reading packs and the evidence ledger are generated locally for each run. For a standalone local install from a checkout of this repository, pass its path to `codex plugin marketplace add` in place of the GitHub URL.

## Validation

```sh
python3 scripts/check_release.py
python3 -m unittest discover -s tests -v
```

The release check examines the tracked file list, manifests and text. The tests use synthetic changes and exercise partitioning, independent assignment, and report generation.
