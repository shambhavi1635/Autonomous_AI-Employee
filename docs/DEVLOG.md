# Development Log

One entry per development step: what was built, why, files touched, problems
hit, and how they were solved. Decisions that were later reversed are kept
visible rather than rewritten -- the reasoning is the part that cannot be
reconstructed from code afterwards.

---

## Phase 0 -- Foundations

**Date:** 2026-10-11
**Goal:** A project skeleton, a dependency and secrets contract, and a
working LLM transport layer proven by a smoke test.
**Status:** Complete. All three smoke-test checks pass.

### Why this first

Everything later depends on it. Proving the model connection now means that
when the planner misbehaves in Phase 1, we already know the transport layer
is sound and can look for the bug in our own logic.

### Decisions

| Decision | Choice | Reasoning |
|---|---|---|
| LLM provider | Google Gemini (`google-genai`) | User's available API key. Isolated behind `llm/client.py` so a provider change is a one-file edit. |
| Planner model | `gemini-3.8-flash` | Gemini 3.x **Pro is unavailable on the free tier** (`limit: 0`). Flash 3.8 handles structured planning well and costs a fraction. Upgrading is one line in `.env`. |
| Fast model | `gemini-3.5-flash-lite` | Verification and small classifications do not need the planner model. |
| Model IDs | Config in `.env`, not constants | Google's IDs churn; 2.5 deprecates 2026-10-16. `list_models()` discovers what the key can actually use. Pinned exact IDs rather than `-latest` aliases so behaviour cannot shift without a deliberate change. |
| Orchestration | ~~None -- hand-rolled loop~~ **LangGraph** | Reversed; see below. |
| Structured output | Pydantic schema passed to the API | Constrains decoding to valid JSON. No regex parsing of model prose. |
| Retry policy | Transient errors only, **and only when the server's `retryDelay` is under 30s** | See "Retry classifier" below. |
| Python version | 3.12 (conda env at `venv/`) | Env defaulted to 3.14.8. Nothing here needs >3.12, and PyTorch wheels lag new releases, so newer was pure risk for no benefit. |
| Dependencies | All installed up front, minus three | User preference. `torch`, `sentence-transformers`, `chromadb` deferred to Phase 4 after download failures. |
| First use case | Invoice -> ledger | Pure library calls, fully verifiable by reading state back, richest demo. |
| Email approach | Gmail IMAP/SMTP, not Outlook GUI | Reliable, and reading the Sent folder makes sends genuinely verifiable. |
| Build order | One file at a time, commit each | User feedback after an up-front folder skeleton was created. See "Scaffolding removed". |

### Files

| File | Purpose |
|---|---|
| `.gitignore` | Excludes `.env`, `venv/`, generated `data/`. |
| `requirements.txt` | Full dependency set, one per line. |
| `core/config.py` | Single place resolving the API key, model IDs and retry limit. |
| `llm/client.py` | Gemini adapter: retries, structured output, token accounting. |
| `scripts/smoke_test_llm.py` | Proves model access, text generation and structured extraction. Asserts on extracted values. |
| `docs/DEVLOG.md` | This file. |

### Architectural boundaries

- `llm/client.py` is the only file that knows Google exists.
- `core/config.py` is the only file that reads the environment.
- Dependencies point one way: `llm/` imports `core/config`, never the reverse.
- `llm/client.py` holds no prompts and no concept of a Plan.

### Decision reversed: orchestration framework

**Original:** no framework, hand-rolled executor loop.
**Revised:** LangGraph from Phase 1. **Raised by:** user.

The original argument -- that a framework hides the planner, executor,
approval gate and retry logic -- holds for LangChain and CrewAI, which
supply agents. It does not hold for LangGraph, which supplies a state
machine: every node is a plain function we write. No reasoning is hidden
because none of it belongs to LangGraph.

Two concrete wins over the original design:

1. **Durable human-in-the-loop.** `interrupt()` persists graph state to
   SQLite and halts; `Command(resume=...)` continues from that point. The
   original used a worker thread blocking on an in-memory event, so a
   pending approval died with the process. (`interrupt()` requires a
   checkpointer -- it errors without one.)
2. **Streamlit rerun immunity.** Streamlit re-executes the script on every
   interaction; the original workaround was a thread publishing to a queue.
   With a checkpointer, agent state lives outside the script.

Also removes the need for a hand-built event bus: LangGraph streams node
events, which will feed the live-execution view.

Scope held deliberately: added `langgraph` and `langgraph-checkpoint-sqlite`
only -- no `langchain`, no LangChain model wrappers. `llm/client.py` is
unchanged; nodes are plain functions that call it directly.

Accepted costs: another abstraction to learn and explain; LangGraph's API
has churned, so versions are pinned; debugging through a framework is harder
than a `while` loop, mitigated by keeping nodes as pure testable functions.

### Scaffolding removed (user feedback)

Phase 0 originally created the full folder skeleton up front
(`core/ llm/ tools/ memory/ ui/ docs/ data/ sandbox/ tests/ scripts/`).
User objected: empty folders for unstarted phases are the "unnecessary
abstraction" the project rules forbid, and they imply structure no code uses.

**Rule adopted:** create a folder or file only when the code being written
at that moment needs it, then commit it.

`core/config.py` was trimmed on the same grounds -- `CHECKPOINT_DB`,
`MAX_STEPS`, `RUN_TIMEOUT_SECONDS`, `DATA_DIR`, `SCREENSHOT_DIR`,
`ALLOWED_WRITE_PATHS` and `ensure_dirs()` were removed. Each returns in the
phase that reads it.

### Problems encountered

1. **`venv/` is not a venv.** It is a conda *prefix* environment, so the
   interpreter is at `venv/python.exe`, not `venv/Scripts/python.exe`.
   Created on Python 3.14.8 because `conda create -p venv python` pins no
   version. **Resolved:** recreated with `python=3.12`; now 3.12.15.

2. **Packages installed into the wrong environment, twice.**
   First into conda base, then into a *different* environment also named
   `venv` at `miniconda3/envs/venv`. Root cause: `conda activate venv`
   matches by **name**, and a prefix environment has no name -- so it
   silently activated an unrelated global env, and bare `pip` followed it.
   **Resolved:** never activate; always `venv\python.exe -m pip ...`, which
   is a file path and cannot be ambiguous.

3. **`torch` download failed.** Repeated
   `Failed to resolve 'files.pythonhosted.org'` (DNS), plus a `[WinError 32]`
   file lock caused by two pip processes running concurrently.
   **Resolved:** deferred `torch`, `sentence-transformers` and `chromadb` to
   Phase 4. If torch still fails then, the memory layer falls back to SQLite
   + numpy cosine similarity behind the same interface.

4. **A pipeline hid a failed install.** `pip ... | tail -25` reported exit 0
   because the exit code of a pipeline is the *last* command's. The install
   had actually failed. **Lesson:** do not pipe a command whose exit status
   matters.

5. **`venv/python.exe` not recognised in cmd.** The VS Code terminal runs
   `cmd.exe`, where `/` introduces a switch, so `venv/python.exe` parsed as
   the command `venv` with option `/python.exe`. Use backslashes in cmd.

6. **Gemini 3.x Pro is unavailable on the free tier.** `429
   RESOURCE_EXHAUSTED ... limit: 0, model: gemini-3.1-pro` -- not an
   exhausted allowance but a zero allowance. **Resolved:** planner switched
   to `gemini-3.8-flash`, which passes all checks.

7. **Retry classifier too naive** (found via problem 6). A 429 was always
   treated as transient, so a quota error with `retryDelay: 57464s` (16
   hours) was retried three times before failing -- burying Google's actual
   message under "failed after 3 attempts".
   **Fix:** `_is_transient()` now also parses `retryDelay` from the error
   body and refuses to retry anything longer than 30s. Covered by 8 cases
   including the real error text; all pass.
   **Generalisable lesson:** the same status code can mean opposite things,
   and the server usually says which in the body.

### Verification

`scripts/smoke_test_llm.py` -- all three checks pass:

- 45 models visible; both configured IDs resolve
- text generation returns `READY`
- invoice extraction returns `total_amount=2540.5`, `line_item_count=3`,
  `currency=USD`, all asserted
- cost: 2 API calls, 157 in / 47 out tokens

### Known gaps

- The 8 retry-classifier test cases were run ad hoc, not saved. They belong
  in `tests/` once there is a second thing worth testing.
- `requirements.txt` still lists `torch`, `sentence-transformers` and
  `chromadb`, which are not installed -- a fresh `pip install -r` will fail
  the same way. Either comment them out or accept it knowingly.
- A benign SDK warning about automatic function calling appears on every
  `generate_content` call. Cosmetic.

### Next

**Phase 1 -- skeleton execution loop.** In order:
`core/schemas.py` (typed `Plan` / `Step` / `AgentState`), `tools/registry.py`
(the `@tool` decorator), two or three trivial tools, `core/planner.py`, then
the first LangGraph graph with a working `interrupt()` approval gate.

**Success criterion:** "read a file and tell me the third line" is planned,
approved in the terminal, executed and verified -- *and* the run survives a
process restart between approval and execution, proving the checkpointer.

---

## Phase 1 -- Skeleton execution loop

**Date:** 2026-10-11
**Goal:** A task goes in, a plan comes out, a human approves it, the steps
run, each one is verified, and the run survives the process dying.
**Status:** Complete. Success criterion met, including restart survival.

### Files, in build order

| File | Purpose |
|---|---|
| `core/schemas.py` | `Plan`, `Step`, `StepResult`, `Risk`. The contract every component agrees on. |
| `tools/registry.py` | The `@tool` decorator: catalog, argument schema, validation and risk tier from one source. |
| `tools/files.py` | `list_files`, `read_file`, `write_file`, confined to `sandbox/`. |
| `core/planner.py` + `llm/prompts/planner.md` | Task to validated plan, checked against the real registry. |
| `core/executor.py` | Resolve placeholder arguments, invoke, verify the postcondition. |
| `core/graph.py` | The LangGraph state machine and both approval gates. |
| `app_cli.py` | Terminal front end: approve / edit / reject, list runs, resume. |

### Design decisions

| Decision | Choice | Reasoning |
|---|---|---|
| Planned vs observed | `Plan`/`Step` come from the model; `StepResult` only from our code | If the model could write its own results, "did it work?" would be judged by the thing being checked. |
| `ok` vs `verified` | Two separate fields; `succeeded` requires both | A tool can return cleanly and still achieve nothing. |
| Risk source of truth | The tool registry, never the plan | A plan is untrusted input and can be influenced by documents the agent reads. `effective_risk()` lets a plan raise a tier, never lower it. |
| Tool arguments in a plan | `arguments_json: str`, parsed by a property | A `dict[str, Any]` compiles to `additionalProperties`, which the Gemini Developer API rejects outright. Nothing is lost: arguments are validated against the tool's own schema, which is stricter. |
| Unknown tool arguments | `extra="forbid"` on every derived model | Pydantic ignores unknown fields by default, so a plan passing a misspelled argument would otherwise run as if nothing were wrong. |
| Placeholder resolution | A model call, not string substitution | `list_files` returns ten names when the next step needs one; `read_file` returns a document when the intent wants one line. Lets the approved plan stay readable. |
| Verification | Two cheap calls: choose a READ probe, then judge its output | Asking "did it work?" from the tool's own return value lets the model agree with itself. A fresh read makes the verdict rest on independent evidence. |
| Probe safety | Only `READ` tools may probe; enforced in code | A check must never change the thing it is checking. |
| Plan validation | Against the live registry, before the human sees it | A schema-valid plan can still name a tool that does not exist. |
| Repair attempts | One | If the model cannot fix a plan after being shown the exact errors, another attempt will not help. |
| Gate ordering | Resolve arguments *before* gate 2 | Gate 1 approves an intention; gate 2 approves the actual payload. |
| CLI edit | Opens the plan as JSON in an editor | Supports every edit with no bespoke UI, and the result is validated like any other plan. |

### Problems encountered

8. **`str.format()` silently broke the placeholder syntax.** The planner
   prompt teaches the model to write `{{step_N.output}}`, but `.format()`
   converts `{{` into `{`, so the model was being taught single braces
   while the parser looked for double. The mechanism would never have
   matched, and the symptom would have been baffling: plans that look
   correct and quietly fail to pass data. **Fix:** plain `.replace()`.
   **Lesson:** never run a brace-heavy prompt through a brace-based
   templating function.

9. **The verifier confabulated.** Step 2 wrote the correct third line, and
   the verifier failed it, claiming the text was "the first line of
   notes.txt". The judge had been shown `out/third.txt` but never
   `notes.txt`, so it could not know which line it was -- and instead of
   saying so, it invented a confident wrong answer. **Fix:** pass earlier
   steps' outputs to the judge, and state in the prompt that "cannot tell"
   is an acceptable verdict and better than a guess.
   **Lesson:** a verifier is just another model call, and needs the same
   scepticism as the thing it verifies.

10. **Gate 2 showed arguments the human could not read.** The approval
    panel displayed `body: '{{step_1.output}}'`. Approving a payload you
    cannot see is not approval. **Fix:** resolve arguments before the gate.
    Resolution now runs twice per irreversible step, because a node is
    re-entered from the top on resume -- acceptable, since resolving only
    reads. Had the tool call sat before the gate instead, approving would
    have sent two emails.

11. **A real step failure, caught by verification.** A resumed run
    extracted line 2 instead of line 3. `write_file` reported success, and
    the run still failed because `verified` was false. Recorded as evidence
    that the verification layer earns its cost on real mistakes, not just
    in principle.

12. **Model choice reversed by measurement.** Problem 11 looked like the
    lite model being weak at counting, so resolution was switched to
    `PLANNER_MODEL`. Re-running the same resolution five times gave 5/5
    correct on the lite model. The failure was nondeterminism, not
    capability, so the change was reverted; the real answer is the retry
    ladder. **Lesson:** one LLM failure is an anecdote. Sample before
    believing a pattern.

13. **Free-tier quota is 20 requests per day on the newest models.**
    `gemini-3.8-flash`, and `gemini-flash-latest` which points at it, were
    exhausted by one day of testing; both return a 429 with a ~13 hour
    `retryDelay`. Still available with room: `gemini-3.5-flash`,
    `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`, `gemini-3.6-flash`.
    **Fix:** `PLANNER_MODEL=gemini-3.5-flash`; `FAST_MODEL` unchanged.
    The Phase 0 retry fix proved itself here -- the error surfaced
    immediately instead of after three pointless retries.
    **Planning note:** a two-step run costs about 5 calls, only one of
    which is planning. Later phases will make more; enabling billing on
    the Google project removes the ceiling if it becomes limiting.

### Verification

End to end, through the CLI:

- **Approve** -- correct file written, both steps verified.
- **Reject** -- run stopped, `notes.txt` untouched.
- **Gate 2** -- execution paused mid-run before `send_email`; `abort` gave
  zero emails sent; `approve` showed the real resolved body first.
- **Restart survival** -- process 1 planned and paused at the gate, then
  exited without approving, leaving the sandbox untouched. A separate
  process read the run from a 20KB SQLite checkpoint, presented the same
  gate, and completed the task correctly.
- **Plan validation** -- rejected a nonexistent tool, a misspelled
  argument, a forward reference and a self-reference.
- **Sandbox** -- four traversal attempts blocked, including
  `notes/../../../../secrets.txt`.

### Known gaps

- **No retry or repair.** `after_step()` stops on the first unverified
  step. The ladder (retry, reflect, re-plan, escalate) belongs in that one
  function -- which is the main argument for a graph over a `while` loop.
- **Still no `tests/`.** Everything above was verified by hand at the time
  it was written. Those checks belong in pytest files.
- **Email postconditions are not independently verifiable** until there is
  a Sent folder to read. The verifier can only confirm the tool returned.
- **`MAX_STEPS` is checked, but there is no wall-clock timeout** and no
  loop detection (same tool, same arguments, twice in a row).
- Carried over from Phase 0: `requirements.txt` lists three packages that
  are not installed, and the SDK's automatic-function-calling warning
  still prints on every call.

### Next

**Phase 2 -- the real use case.** `tools/documents.py` (PDF extraction),
`tools/ledger.py` (a stand-in for the internal system), and the invoice to
ledger task working end to end. Plus the retry ladder, which problem 11
showed is the missing piece rather than a nicety.

---

## Phase 2 -- Invoice to ledger, and failing well

**Date:** 2026-10-11
**Goal:** The first real use case end to end -- find an invoice PDF, read
it, file it in an internal system, and confirm it is there. Plus the
failure handling Phase 1 showed was missing.
**Status:** Working. Invoice to ledger verified on two different invoices,
including one designed to be hard.

### Files

| File | Purpose |
|---|---|
| `tools/ledger.py` | SQLite invoice store: `add_invoice`, `find_invoice`, `list_invoices`. |
| `tools/documents.py` | `read_pdf` -- text extraction only, no interpretation. |
| `scripts/make_sample_invoices.py` | Four invoice PDFs, deliberately different layouts. |
| `core/executor.py` | `repair_step()` and `verify_goal()`. |
| `core/graph.py` | `repair` and `replan` nodes; goal check in `finalise`. |
| `tests/` | 131 offline tests. |

### Design decisions

| Decision | Choice | Reasoning |
|---|---|---|
| Invoice extraction | **No extraction code at all** | The obvious `extract_invoice_fields()` would need a model call inside `tools/` (breaking the rule that tools stay testable without an API key) and would be an invoice-shaped workflow -- the hardcoding this project exists to avoid. `read_pdf` plus placeholder resolution already does it, and the same two steps work for a receipt or a contract. |
| Sample invoices | Four vendors, four layouts | One shared template would let memorisation pass as extraction. They differ in what the number is called, date format, what the total is called, currency, and structure. Globex carries a subtotal and a GST line directly above the real total, as a trap. |
| Ledger realism | Real SQLite with constraints | The point of the use case is that the agent's work can be checked by something other than the agent. |
| Unique invoice numbers | Enforced by the schema | Agents retry. Without it, a retry after a timeout silently creates a second payment record. |
| `add_invoice` risk | `WRITE`, not `IRREVERSIBLE` | A row can be deleted. In a real system that triggers payment it would be `IRREVERSIBLE` -- a one-word change, which is the point of declarative risk. |
| Date handling | **Refuse** non-ISO dates | `02/10/2026` is 2 October or 10 February depending on the country. A tool that guessed would file wrong records that look fine. Refusing pushes the decision back to the step that can still see the document. |
| Where constraints live | In the tool, enforced | Not in the prompt, hoped for. A prompt is a request; a tool is a gate. |
| Repair vs retry | Repair, not blind retry | Blind retries only help with transient faults. Most agent failures are the agent having been wrong, and repeating the same call repeats the mistake. Tools that explain themselves ("not UTF-8 text -- use read_pdf") make repair possible. |
| Re-plan scope | Whole plan, from scratch | Splicing a new plan onto half-finished state would leave placeholders pointing at step numbers from a plan that no longer exists. Costs re-doing earlier reads; buys internal consistency. |
| Re-plan approval | Always back to the human | The agent may change its mind; it may not change what you approved without asking. |
| Test suite | Entirely offline | A suite needing credentials gets skipped, and one costing money per run gets run less often than it should. Model judgement is checked by hand against the sample invoices; everything that must hold regardless of what the model says is tested. |

### The failure ladder

    step fails -> repair   (fix this step, max 2 attempts)
               -> replan   (the plan itself is wrong, max 1)
               -> stop and tell the human

Three safeguards on repair: risk is re-derived from the replacement tool,
so a repair cannot swap in something dangerous and skip the gate; a repair
proposing the identical call is rejected; and the repaired step replaces
the original in the plan, with `message` recording the swap, because a
plan that silently rewrote itself would undermine approving one.

### Problems encountered

14. **Typed placeholders.** `_stub_placeholders` substituted the string
    `"placeholder"` when type-checking, which failed `total_amount: float`.
    So a *correct* plan was rejected, and the repair round then pushed the
    model to replace the placeholder with a literal it invented -- a
    hardcoded `0.0` instead of the real total. **Lesson:** a validator that
    rejects correct input is worse than no validator, because the system
    works around it in a way that looks like success.

15. **The catalog shows only a docstring's first paragraph -- hit twice.**
    The "not for PDFs" warning on `read_file` and the ISO-date requirement
    on `add_invoice` were both in paragraph two, invisible to the planner.
    **Lesson:** the first line of a tool's docstring *is* the prompt.
    Anything needed when *choosing* the tool belongs there.

16. **The verification probe hallucinated a filename.** Asked how to check
    a step without being told what the step had touched, it invented
    `acme_invoice.pdf`. Same root cause as problem 9: a model asked to
    decide without the information needed to decide. **Fix:** the probe now
    sees the real call with its resolved arguments.

17. **The planner copied values out of the task.** Asked to file "the Acme
    invoice", it hardcoded `vendor="Acme"` instead of reading the vendor
    from the document, filing `Acme` where the invoice said
    `ACME CORPORATION`. **Fix:** prompt rule 3b -- words in the task
    identify *which* thing to find; they are not the data.

18. **Right steps, wrong thing -- the most serious failure so far.**
    Asked to file the **Wayne Enterprises** invoice, which does not exist,
    the agent listed the folder, read `acme.pdf`, filed ACME CORPORATION,
    and reported *"All 3 steps completed and verified."* Every step was
    honest. Every postcondition held. The postconditions asked whether *an*
    invoice had been filed, not whether *that* one had.

    No per-step check can catch this, because no step is wrong. The hole
    was structural: the original design called for three levels of
    verification and only two had been built.

    **Fix:** `verify_goal()` -- a final check comparing the original
    request against the whole trace, run in `finalise` after every step has
    already passed. It is told explicitly that "could not find what was
    asked for" is a legitimate outcome and substituting the nearest
    available thing is not. Plus prompt rule 2b: when a request names a
    vendor, file or record, the postcondition must name it too.

    **Lesson:** this is the failure that matters in a business setting --
    not crashing, but confidently doing the wrong thing. Verification has
    to reach all the way up to the request, or it verifies the agent's
    interpretation rather than the user's intent.

### Verification

- **Acme invoice** -- filed correctly: number, date, amount, currency.
- **Globex invoice** -- the hard one. Correct vendor, number and currency,
  and it picked 98,400.00 as the total rather than the 83,389.83 subtotal
  or the 15,010.17 GST line sitting above it.
- **Repair ladder, in a real run** -- `attempts: {'3': 2}`. Step 3 was
  rejected for a non-ISO date, the agent read its own error, converted
  `02/10/2026` to `2026-10-02`, retried, and passed. No human involved.
- **Repair, isolated** -- `read_file` on a PDF -> error -> repaired to
  `read_pdf` with the same path -> retry succeeded.
- **Tests** -- 131 passing in under 5 seconds, no API key required.

### Known gaps

- **`verify_goal` has not yet been confirmed against the Wayne case in a
  live run.** Two attempts were lost to Google-side 503s. The fix is
  written and the unit tests pass, but the end-to-end proof is owed.
- **The re-plan rung is unproven live** for the same reason. Its routing
  is covered by offline tests.
- **No LLM response cache.** Most quota today went on re-running the same
  task while fixing bugs, which also made failures hard to reproduce --
  the line-2-vs-line-3 bug disappeared on re-run. A record/replay cache in
  `_call()` would make development cheaper and debugging deterministic.
- **No wall-clock timeout, no loop detection.**
- Three packages in `requirements.txt` are still not installed.

### Next

**Phase 3 -- the UI.** Streamlit: the plan as an editable table, a live
execution feed, a stop button, run history. The graph needs no changes --
`app_cli.py` was kept free of agent logic precisely so a second front end
could drive the same machine.
