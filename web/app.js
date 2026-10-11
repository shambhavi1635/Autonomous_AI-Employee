/* Your AI Colleague -- the page.
 *
 * No framework. The whole interface is: send a task, render a plan, take
 * a decision, stream the run. That is a few hundred lines of plain DOM
 * work, and a build step would cost more than it saved.
 *
 * One rule carried over from the Streamlit version: this file holds no
 * agent logic. It renders what the server reports and collects answers
 * at the gates. Everything that decides anything lives in core/.
 */

const $ = (sel) => document.querySelector(sel);
const workspace = $("#workspace");
const composer = $("#composer-wrap");

let currentRun = null;
let busy = false;

const EXAMPLES = [
    "File the Globex invoice from the invoices folder into the ledger",
    "In sales.xlsx, mark every region that beat its target as approved",
    "Read sales.xlsx and write a summary as a Word document",
    "List every invoice in the ledger",
];

/* --------------------------------------------------------- utilities */
const esc = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, options) {
    const response = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options,
    });
    if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
    return response.json();
}

/* ----------------------------------------------------------- sidebar */
$("#sidebar-toggle").onclick = () => {
    $("#sidebar").classList.toggle("open");
    $("#sidebar-toggle").classList.toggle("shifted");
};

$("#new-run").onclick = () => {
    currentRun = null;
    workspace.innerHTML = "";
    composer.style.display = "";
    $("#task").value = "";
    $("#task").focus();
    renderRuns();
};

async function renderRuns() {
    const { runs } = await api("/api/runs");
    const list = $("#run-list");

    if (!runs.length) {
        list.innerHTML = `<div class="empty">Nothing yet.</div>`;
        return;
    }
    list.innerHTML = runs
        .map(
            (run) => `
        <div class="run-item ${run.run_id === currentRun ? "active" : ""}"
             data-id="${run.run_id}">
          <div class="task">${esc(run.task)}</div>
          <div class="meta">
            <span class="dot ${esc(run.status)}"></span>
            <span>${esc(run.status)}</span>
            <span>&middot;</span>
            <span>${esc(run.when.replace("T", " ").slice(5, 16))}</span>
          </div>
        </div>`
        )
        .join("");

    list.querySelectorAll(".run-item").forEach((item) => {
        item.onclick = () => openRun(item.dataset.id);
    });
}

async function renderFooter() {
    try {
        const memory = await api("/api/memory");
        const ledger = await api("/api/ledger");
        $("#sidebar-foot").innerHTML =
            `<div>memory &middot; <code>${memory.stats.lessons}</code> lessons,
             <code>${memory.stats.episodes}</code> runs</div>
             <div>ledger &middot; <code>${ledger.invoices.length}</code> invoices</div>`;
    } catch {
        /* the footer is decoration; never let it break the page */
    }
}

/* ------------------------------------------------------------ render */
function planTable(plan, editable) {
    const rows = plan.steps
        .map(
            (step, index) => `
      <tr data-index="${index}">
        <td class="num">${step.id}</td>
        <td><span class="risk ${esc(step.risk)}">${esc(step.risk)}</span></td>
        <td>
          <div ${editable ? 'contenteditable="true" data-field="intent"' : ""}>${esc(step.intent)}</div>
          <div class="call">${esc(step.tool)}(<span
             ${editable ? 'contenteditable="true" data-field="arguments_json"' : ""}
             >${esc(step.arguments_json)}</span>)</div>
          <div class="check">check: <span
             ${editable ? 'contenteditable="true" data-field="postcondition"' : ""}
             >${esc(step.postcondition)}</span></div>
        </td>
      </tr>`
        )
        .join("");

    return `<table><thead><tr><th></th><th>Risk</th><th>Step</th></tr></thead>
            <tbody>${rows}</tbody></table>`;
}

/* Read the table back, so an edit the user made is what gets approved. */
function collectPlan(original) {
    const plan = JSON.parse(JSON.stringify(original));
    document.querySelectorAll("#plan-panel tbody tr").forEach((row) => {
        const step = plan.steps[Number(row.dataset.index)];
        row.querySelectorAll("[data-field]").forEach((cell) => {
            step[cell.dataset.field] = cell.innerText.trim();
        });
    });
    return plan;
}

function renderPlanGate(state) {
    const plan = state.gate.plan;
    const risky = plan.steps.some((s) => s.risk === "irreversible");

    workspace.innerHTML = `
      <div class="panel" id="plan-panel">
        <div class="panel-head"><h3>Proposed plan</h3>
          <span class="hint">${plan.steps.length} steps &middot; editable</span></div>
        <div class="panel-body">
          <div class="reasoning">${esc(plan.reasoning)}</div>
          ${risky ? `<div class="verdict rejected" style="margin-bottom:16px">
             This plan contains an irreversible step. You will be asked again,
             with the exact details, immediately before it runs.</div>` : ""}
          ${planTable(plan, true)}
          <div id="plan-problems"></div>
        </div>
        <div class="actions">
          <button class="btn-primary" id="approve">Approve &amp; run</button>
          <button class="btn-ghost" id="reject">Reject</button>
        </div>
      </div>`;

    $("#approve").onclick = async () => {
        const edited = collectPlan(plan);
        const unchanged = JSON.stringify(edited.steps) === JSON.stringify(plan.steps);

        if (!unchanged) {
            const check = await api("/api/plan/validate", {
                method: "POST",
                body: JSON.stringify({ plan: edited }),
            });
            if (!check.ok) {
                $("#plan-problems").innerHTML =
                    `<div class="verdict failed" style="margin-top:16px">
                       That edit does not make a runnable plan:<br>
                       ${check.problems.map((p) => "&bull; " + esc(p)).join("<br>")}
                     </div>`;
                return;
            }
            return run({ resume: { action: "edit", plan: check.plan } });
        }
        run({ resume: { action: "approve" } });
    };

    $("#reject").onclick = () =>
        run({ resume: { action: "reject", reason: "Rejected in the interface." } });
}

function renderActionGate(state) {
    const gate = state.gate;
    const args = Object.entries(gate.arguments || {})
        .map(([k, v]) => `<div><span class="k">${esc(k)}</span>: <span class="v">${esc(v)}</span></div>`)
        .join("");

    workspace.innerHTML = `
      <div class="panel gate">
        <div class="panel-head"><h3>Step ${gate.step_id} is irreversible</h3></div>
        <div class="panel-body">
          <div style="margin-bottom:16px">${esc(gate.intent)}</div>
          <div class="gate-args">${args}</div>
          <div class="check" style="margin-top:14px">
            These are the real values, resolved from earlier steps &mdash; not
            placeholders. This is exactly what will happen.</div>
        </div>
        <div class="actions">
          <button class="btn-primary" id="go">Run it</button>
          <button class="btn-ghost" id="skip">Skip this step</button>
          <button class="btn-danger" id="abort">Abort</button>
        </div>
      </div>`;

    $("#go").onclick = () => run({ resume: { action: "approve" } });
    $("#skip").onclick = () => run({ resume: { action: "skip" } });
    $("#abort").onclick = () => run({ resume: { action: "abort" } });
}

function renderOutcome(state) {
    const parts = [];

    if (state.answer) {
        parts.push(`<div class="panel"><div class="panel-head"><h3>Answer</h3></div>
          <div class="panel-body"><div class="answer">${esc(state.answer)}</div></div></div>`);
    }

    if (state.status !== "unknown") {
        parts.push(`<div class="panel"><div class="panel-head"><h3>Outcome</h3>
            <span class="hint">${esc(state.status)}</span></div>
          <div class="panel-body">
            <div class="verdict ${esc(state.status)}">${esc(state.message)}</div>
            ${state.replans ? `<div class="check" style="margin-top:10px">
               The plan was reconsidered ${state.replans} time(s).</div>` : ""}
          </div></div>`);
    }

    if (state.results.length) {
        const lines = state.results
            .map(
                (r) => `<div class="feed-line">
                   <span class="feed-mark ${r.verified ? "pass" : "fail"}">${r.verified ? "PASS" : "FAIL"}</span>
                   <div class="feed-body">Step ${r.step_id}
                     <div class="detail">${esc(r.evidence || r.error || "")}</div></div></div>`
            )
            .join("");
        parts.push(`<div class="panel"><div class="panel-head"><h3>Steps</h3></div>
          <div class="panel-body"><div class="feed">${lines}</div></div></div>`);
    }

    const outputs = Object.entries(state.outputs || {});
    if (outputs.length) {
        const blocks = outputs
            .sort((a, b) => Number(a[0]) - Number(b[0]))
            .map(([id, value]) => {
                const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
                return `<details ${id === outputs[outputs.length - 1][0] ? "open" : ""}>
                  <summary>Step ${esc(id)} returned</summary>
                  <pre>${esc(text)}</pre></details>`;
            })
            .join("");
        parts.push(`<div class="panel"><div class="panel-head"><h3>What the steps returned</h3></div>
          <div class="panel-body">${blocks}</div></div>`);
    }

    if (state.resumable) {
        parts.push(`<div class="panel"><div class="panel-body">
            <div class="verdict rejected">This run stopped part-way through. Nothing
            was lost &mdash; its state is saved.</div>
            <div class="actions" style="border:none;padding:16px 0 0">
              <button class="btn-primary" id="continue">Continue this run</button>
            </div></div></div>`);
    }

    workspace.innerHTML = parts.join("");
    const cont = $("#continue");
    if (cont) cont.onclick = () => run({ resume: "continue" });
}

function render(state) {
    currentRun = state.run_id;

    if (state.gate?.kind === "plan_approval") {
        composer.style.display = "none";
        renderPlanGate(state);
    } else if (state.gate?.kind === "action_approval") {
        composer.style.display = "none";
        renderActionGate(state);
    } else {
        // The run is over, so hand the box back rather than leaving the
        // page as a dead end. Cleared, because the next task is a new
        // one -- but the finished run stays on screen below it.
        renderOutcome(state);
        composer.style.display = "";
        $("#task").value = "";
    }

    renderRuns();
    renderFooter();
}

/* ------------------------------------------------------------ stream */
function feedPanel() {
    const panel = document.createElement("div");
    panel.className = "panel";
    panel.innerHTML = `<div class="panel-head"><h3>Running</h3><span class="spinner"></span></div>
                       <div class="panel-body"><div class="feed" id="feed"></div></div>`;
    workspace.appendChild(panel);
    return panel;
}

function feedLine(mark, markClass, text, detail) {
    const line = document.createElement("div");
    line.className = "feed-line";
    line.innerHTML = `<span class="feed-mark ${markClass}">${mark}</span>
      <div class="feed-body">${esc(text)}
        ${detail ? `<div class="detail">${esc(detail)}</div>` : ""}</div>`;
    $("#feed").appendChild(line);
    line.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/* Read a server-sent-event stream from a POST. EventSource cannot send a
 * body, and the decision has to travel with the request. */
async function run(body) {
    if (busy) return;
    busy = true;
    composer.style.display = "none";

    const panel = feedPanel();
    let finalState = null;

    try {
        const response = await fetch("/api/stream", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ run_id: currentRun, ...body }),
        });

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });

            const chunks = buffer.split("\n\n");
            buffer = chunks.pop();

            for (const chunk of chunks) {
                if (!chunk.startsWith("data: ")) continue;
                const event = JSON.parse(chunk.slice(6));

                if (event.type === "step") {
                    feedLine(event.passed ? "PASS" : "FAIL",
                             event.passed ? "pass" : "fail",
                             `Step ${event.step_id}`, event.detail);
                } else if (event.type === "note" && event.text) {
                    feedLine("", "note", event.text);
                } else if (event.type === "error") {
                    feedLine("ERROR", "fail", event.text);
                } else if (event.type === "state") {
                    finalState = event;
                }
            }
        }
    } catch (error) {
        feedLine("ERROR", "fail", String(error));
    } finally {
        busy = false;
        panel.querySelector(".spinner")?.remove();
    }

    if (finalState) render(finalState);
}

async function openRun(runId) {
    currentRun = runId;
    render(await api(`/api/run/${runId}`));
}

async function start() {
    const task = $("#task").value.trim();
    if (!task || busy) return;
    const { run_id } = await api("/api/runs", {
        method: "POST",
        body: JSON.stringify({ task }),
    });
    currentRun = run_id;
    workspace.innerHTML = "";
    run({ task });
}

/* --------------------------------------------------------------- init */
$("#execute").onclick = start;
$("#task").addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") start();
});

$("#examples").innerHTML = EXAMPLES
    .map((text) => `<span class="example">${esc(text)}</span>`)
    .join("");
document.querySelectorAll(".example").forEach((chip) => {
    chip.onclick = () => {
        $("#task").value = chip.textContent;
        $("#task").focus();
    };
});

renderRuns();
renderFooter();
$("#task").focus();
