# Lens blocks (fill {{LENSES}} / {{LENS_FOCUS}})

Paste the relevant block(s) into the reviewer prompt. A single reviewer takes blocks 1, 2, 3, 4; a
second reader takes the two blocks matching its framing.

## 1. Guidelines compliance

Check the diff against the quoted rules that apply to each changed file. Report only an unambiguous
violation you can quote, with the rule's path. Do not generalize one directory's rule to another.
Schema changes get their own check: which policy the repo states for adding columns (model sync vs
`OnceMigration`), and whether this change duplicates a definition the model already carries.

## 2. Bug scan

Look for clear logic errors, broken error paths, wrong conditions, unresolved references.
Three patterns earn special attention because they hide in plausible code:

- **Stale gate** — recheck the state that can change between a decision and its effect: an expired
  session, changed permission, consumed token, or updated record version.
- **Fallback values** — when a guard or query uses another value for its timestamp, limit, or owner,
  check empty results, the first attempt, and data saved before the change.
- **Flag scope** — a setting documented as "off records nothing" must gate every writer of that data,
  including retries, scheduled work, and cleanup jobs.
- **Text matching and editing** — compare ordinary input with a repeated prefix followed by a whole
  match, a language without spaces, and a localized edit. If input is assembled from multiple sources,
  check which sequences the real caller emits. Trace match selection, cursor advancement and final validation.
- **Asynchronous failure and expiry** — compare success with rejection, an unavailable dependency,
  timeout and retry. Check that each path emits the terminal result, and that claims, markers and their
  parent objects preserve their shared expiration and one-use rules.

## 3. Comments and contracts

Read the comments, docstrings, types and assertions around the changed code. Flag a change that
contradicts a documented contract or makes an existing comment false.

- When the false contract is about **behaviour** — lock order, transaction scope, ownership, what a
  caller receives — file it as `bug` with the comment as evidence, not `comment-contract`: the
  category decides whether a verifier ever sees it.
- **Report the behavioural delta, not the wording**: a field no longer returned, a mode that hands
  downstream nothing, a conversation that no longer reaches a consumer — that is the finding. A report
  that lists only the docstring while the behaviour changed is a miss.

## 4. Efficiency and scale

What now runs per request, per item, or per turn that did not before, and at what volume does it hurt?
N+1 queries where one batch would do; an `EXISTS` or scan added to a query that runs on every page
load; a per-turn fetch of a payload that never changes (files up to megabytes, blobs, remote objects)
with no cache; an unbounded loop over submitted content. Name the volume ("every generated file, up to
2 MB, on every turn"). Do not flag micro-optimisations, and do not flag volume the change did not
introduce.

## 5. Maintainability (only at high/max effort)

Duplicated logic that already has a home, needless wrappers or branches, clear wasted work — only when
the fix is obvious and behaviour-preserving. Not a style audit.

---

# Second-reader framings

## A — behaviour (runtime)

Will this break at runtime? Bug paths, state machines, concurrency and lock order, data written versus
data shown. Assume the first reader has the diff's obvious surface; your value is the failure path it
did not trace.

## B — contracts and cost

Does the change contradict what the repository, the docs or a comment says — and what does it now cost?
Guidelines, comments/contracts, efficiency and scale, test assertions. Assume the first reader checked
the happy path; your value is the contradiction and the bill.
