#!/usr/bin/env node
/**
 * Fixture-driven tests for the opsx-usage-emitter OpenCode plugin.
 *
 * Each test case spawns an isolated Node process with specific environment
 * variables so the plugin's module-scoped gate is exercised independently.
 *
 * Usage: node tests/opencode/test-opsx-usage-emitter.js
 * Exit 0 = all tests passed, non-zero = at least one failure.
 */

const fs = require("fs");
const path = require("path");
const cp = require("child_process");
const os = require("os");

// ---------------------------------------------------------------------------
// Test harness
// ---------------------------------------------------------------------------

const REPO = path.resolve(__dirname, "../..");
const PLUGIN = path.join(REPO, "adapters/opencode/plugins/opsx-usage-emitter.js");
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "opsx-plugin-test-"));

let passed = 0;
let failed = 0;

function ok(msg) {
  passed++;
  console.log("  \x1b[32m✓\x1b[0m %s", msg);
}

function fail(msg, detail) {
  failed++;
  console.error("  \x1b[31m✗\x1b[0m %s", msg);
  if (detail) console.error("    %s", detail);
}

function assert(cond, msg, detail) {
  if (cond) ok(msg);
  else fail(msg, detail);
}

/**
 * Spawn `node <script>` with the given env vars and return stdout lines.
 */
function spawn(scriptPath, envVars) {
  try {
    const result = cp.execSync(`node "${scriptPath}"`, {
      env: { ...process.env, ...envVars },
      timeout: 5000,
      encoding: "utf8",
    });
    return { ok: true, lines: result.trim().split("\n").filter(Boolean) };
  } catch (e) {
    return {
      ok: false,
      code: e.status,
      stderr: (e.stderr || "").toString().trim(),
      stdout: (e.stdout || "").toString().trim(),
    };
  }
}

function cleanup() {
  try { fs.rmSync(TMP, { recursive: true, force: true }); } catch (_) {}
}

process.on("exit", cleanup);

// ---------------------------------------------------------------------------
// Shared fixture helpers (written as temp scripts for isolation)
// ---------------------------------------------------------------------------

/**
 * Script: register plugin, feed events, then print the sidecar file contents.
 */
function makeRunScript(usagePath, eventsJSON) {
  const code = `
const fs = require("fs");
const path = require("path");
const plugin = require(${JSON.stringify(PLUGIN)});

async function main() {
  const hooks = await plugin.OpsxUsageEmitter({
    project: {},
    client: {},
    $: {},
    directory: "/tmp",
    worktree: "/tmp",
  });
  const eventHook = hooks.event;
  if (!eventHook) {
    // Plugin returned no hooks → inert
    console.log("INERT");
    process.exit(0);
  }

  const events = ${eventsJSON};
  for (const evt of events) {
    await eventHook({ event: evt });
  }

  // Read back sidecar output
  try {
    const data = fs.readFileSync(${JSON.stringify(usagePath)}, "utf8").trim();
    if (data) console.log(data);
    else console.log("EMPTY_FILE");
  } catch (_e) {
    console.log("NO_FILE");
  }
}
main().catch(e => { console.error("PANIC:" + e.message); process.exit(0); });
`;
  const scriptPath = path.join(TMP, "run-" + Math.random().toString(36).slice(2) + ".js");
  fs.writeFileSync(scriptPath, code);
  return scriptPath;
}

/**
 * Build a minimal message.updated event with an assistant message carrying tokens.
 */
function makeUpdateEvent(tokens, providerID, modelID) {
  return {
    type: "message.updated",
    properties: {
      info: {
        role: "assistant",
        tokens,
        providerID: providerID || null,
        modelID: modelID || null,
      },
    },
  };
}

/**
 * Build a session.idle event.  Optionally attach info/tokens for future-proof tests.
 */
function makeIdleEvent(info) {
  const props = { sessionID: "sess-1" };
  if (info) props.info = info;
  return { type: "session.idle", properties: props };
}

// ---------------------------------------------------------------------------
// Test cases
// ---------------------------------------------------------------------------

console.log("\n🧪 Testing opsx-usage-emitter plugin\n");

// -- 3.1: Incremental token-bearing event --------------------------------
{
  const usagePath = path.join(TMP, "inc-3.1.jsonl");
  const env = {
    OPSX_USAGE_PATH: usagePath,
    OPSX_PLAN_NAME: "plan-3.1",
    OPSX_RUN_ID: "run-3.1",
    OPSX_CHANGE_ID: "change-3.1",
    OPSX_STAGE: "implement",
    OPSX_ROUND: "5",
  };

  const events = JSON.stringify([
    makeUpdateEvent(
      { input: 400, output: 200, reasoning: 3, cache: { read: 10, write: 5 }, total: 613 },
      "test-provider",
      "test-model",
    ),
  ]);

  const script = makeRunScript(usagePath, events);
  const { lines } = spawn(script, env);

  const record = lines && lines.length === 1 ? (() => { try { return JSON.parse(lines[0]); } catch(_){ return null; } })() : null;
  assert(record !== null, "3.1 emits incremental record for token-bearing message.updated", "Got: " + JSON.stringify(lines));
  if (record) {
    assert(record.event_type === "incremental", "3.1 record event_type is incremental");
    assert(record.schema_version === 1, "3.1 schema_version is 1");
    assert(record.input_tokens === 400, "3.1 input_tokens is 400");
    assert(record.output_tokens === 200, "3.1 output_tokens is 200");
    assert(record.reasoning_tokens === 3, "3.1 reasoning_tokens is 3");
    assert(record.cached_input_tokens === 10, "3.1 cached_input_tokens is 10");
    assert(record.total_tokens === 613, "3.1 total_tokens is 613");
    assert(record.provider === "test-provider", "3.1 provider is test-provider");
    assert(record.model_id === "test-model", "3.1 model_id is test-model");
    assert(record.plan_name === "plan-3.1", "3.1 plan_name present");
    assert(record.run_id === "run-3.1", "3.1 run_id present");
    assert(record.change_id === "change-3.1", "3.1 change_id present");
    assert(record.stage === "implement", "3.1 stage is implement");
    assert(record.round === 5, "3.1 round is 5");
    assert(typeof record.emitted_at === "string" && record.emitted_at.endsWith("Z"), "3.1 emitted_at is ISO-8601");
  }
}

// -- 3.2: Final usage event ----------------------------------------------
{
  // 3.2a – session.idle without tokens → NO final record
  {
    const usagePath = path.join(TMP, "fin-3.2a.jsonl");
    const env = {
      OPSX_USAGE_PATH: usagePath,
      OPSX_PLAN_NAME: "plan-3.2a",
      OPSX_RUN_ID: "run-3.2a",
      OPSX_CHANGE_ID: "change-3.2a",
      OPSX_STAGE: "review",
      OPSX_ROUND: "2",
    };

    const events = JSON.stringify([makeIdleEvent(null)]);
    const script = makeRunScript(usagePath, events);
    const { lines } = spawn(script, env);

    const noRecords = !lines || lines.length === 0 || lines[0] === "EMPTY_FILE" || lines[0] === "NO_FILE";
    assert(noRecords, "3.2a session.idle without tokens emits no final record", "Lines: " + JSON.stringify(lines));
  }

  // 3.2b – session.idle WITH info.tokens → final record (future-proof)
  {
    const usagePath = path.join(TMP, "fin-3.2b.jsonl");
    const env = {
      OPSX_USAGE_PATH: usagePath,
      OPSX_PLAN_NAME: "plan-3.2b",
      OPSX_RUN_ID: "run-3.2b",
      OPSX_CHANGE_ID: "change-3.2b",
      OPSX_STAGE: "archive",
      OPSX_ROUND: "3",
    };

    const events = JSON.stringify([
      makeIdleEvent({
        role: "assistant",
        tokens: { input: 50, output: 30, reasoning: 0, cache: { read: 0, write: 0 }, total: 80 },
        providerID: "prov-b",
        modelID: "model-b",
      }),
    ]);
    const script = makeRunScript(usagePath, events);
    const { lines } = spawn(script, env);

    const record = lines && lines.length === 1 ? (() => { try { return JSON.parse(lines[0]); } catch(_){ return null; } })() : null;
    assert(record !== null, "3.2b session.idle with info.tokens emits final record", "Got: " + JSON.stringify(lines));
    if (record) {
      assert(record.event_type === "final", "3.2b event_type is final");
      assert(record.total_tokens === 80, "3.2b total_tokens is 80");
      assert(record.provider === "prov-b", "3.2b provider extracted");
      assert(record.model_id === "model-b", "3.2b model_id extracted");
    }
  }

  // 3.2c – session.idle with model metadata but NO tokens → final record
  {
    const usagePath = path.join(TMP, "fin-3.2c.jsonl");
    const env = {
      OPSX_USAGE_PATH: usagePath,
      OPSX_PLAN_NAME: "plan-3.2c",
      OPSX_RUN_ID: "run-3.2c",
      OPSX_CHANGE_ID: "change-3.2c",
      OPSX_STAGE: "implement",
      OPSX_ROUND: "1",
    };

    const events = JSON.stringify([
      makeIdleEvent({
        role: "assistant",
        // no tokens – just model metadata
        providerID: "prov-c",
        modelID: "model-c",
      }),
    ]);
    const script = makeRunScript(usagePath, events);
    const { lines } = spawn(script, env);

    const record = lines && lines.length === 1 ? (() => { try { return JSON.parse(lines[0]); } catch(_){ return null; } })() : null;
    assert(record !== null, "3.2c session.idle with metadata but no tokens emits final record", "Got: " + JSON.stringify(lines));
    if (record) {
      assert(record.event_type === "final", "3.2c event_type is final");
      assert(record.provider === "prov-c", "3.2c provider extracted from metadata-only info");
      assert(record.model_id === "model-c", "3.2c model_id extracted from metadata-only info");
      assert(record.input_tokens === null, "3.2c input_tokens is null (no tokens provided)");
      assert(record.total_tokens === null, "3.2c total_tokens is null (no tokens provided)");
    }
  }

  // 3.2d – session.idle with info but NO tokens AND NO metadata → skip
  {
    const usagePath = path.join(TMP, "fin-3.2d.jsonl");
    const env = {
      OPSX_USAGE_PATH: usagePath,
      OPSX_PLAN_NAME: "plan-3.2d",
      OPSX_RUN_ID: "run-3.2d",
      OPSX_CHANGE_ID: "change-3.2d",
      OPSX_STAGE: "implement",
      OPSX_ROUND: "1",
    };

    // info object present but contains nothing usable
    const events = JSON.stringify([
      makeIdleEvent({
        role: "assistant",
        // no tokens, no providerID, no modelID
      }),
    ]);
    const script = makeRunScript(usagePath, events);
    const { lines } = spawn(script, env);

    const noRecords = !lines || lines.length === 0 || lines[0] === "EMPTY_FILE" || lines[0] === "NO_FILE";
    assert(noRecords, "3.2d session.idle with all-null info emits no final record", "Lines: " + JSON.stringify(lines));
  }

  // 3.2e – session.idle with malformed tokens-only payload (no metadata) → skip
  {
    const usagePath = path.join(TMP, "fin-3.2e.jsonl");
    const env = {
      OPSX_USAGE_PATH: usagePath,
      OPSX_PLAN_NAME: "plan-3.2e",
      OPSX_RUN_ID: "run-3.2e",
      OPSX_CHANGE_ID: "change-3.2e",
      OPSX_STAGE: "implement",
      OPSX_ROUND: "1",
    };

    // tokens object present but all values are unusable (negative, float,
    // NaN, Infinity) AND no provider/model metadata — must not emit.
    const badTokens = {
      input: -5,
      output: 3.7,
      reasoning: NaN,
      cache: { read: "twelve", write: null },
      total: Infinity,
    };

    const events = JSON.stringify([
      makeIdleEvent({
        role: "assistant",
        tokens: badTokens,
        // no providerID, no modelID
      }),
    ]);
    const script = makeRunScript(usagePath, events);
    const { lines } = spawn(script, env);

    const noRecords = !lines || lines.length === 0 || lines[0] === "EMPTY_FILE" || lines[0] === "NO_FILE";
    assert(noRecords, "3.2e session.idle with malformed tokens-only emits no final record", "Lines: " + JSON.stringify(lines));
  }
}

// -- 3.3: Plugin inert when OPSX_USAGE_PATH is absent ---------------------
{
  const env = {
    // no OPSX_USAGE_PATH
    OPSX_PLAN_NAME: "plan-3.3",
    OPSX_RUN_ID: "run-3.3",
    OPSX_CHANGE_ID: "change-3.3",
    OPSX_STAGE: "implement",
    OPSX_ROUND: "1",
  };

  const events = JSON.stringify([
    makeUpdateEvent({ input: 1, output: 1, reasoning: 0, cache: { read: 0, write: 0 }, total: 2 }, "p", "m"),
  ]);

  const script = makeRunScript("/tmp/should-not-exist-3.3.jsonl", events);
  const { lines } = spawn(script, env);

  assert(lines && lines[0] === "INERT", "3.3 plugin returns inert (empty hooks) when OPSX_USAGE_PATH absent", "Got: " + JSON.stringify(lines));
}

// -- 3.4: Malformed numeric values not coerced ---------------------------
{
  const usagePath = path.join(TMP, "mal-3.4.jsonl");
  const env = {
    OPSX_USAGE_PATH: usagePath,
    OPSX_PLAN_NAME: "plan-3.4",
    OPSX_RUN_ID: "run-3.4",
    OPSX_CHANGE_ID: "change-3.4",
    OPSX_STAGE: "implement",
    OPSX_ROUND: "1",
  };

  // tokens with negative, float, non-numeric values
  const badTokens = {
    input: -5,
    output: 3.7,
    reasoning: NaN,
    cache: { read: "twelve", write: null },
    total: Infinity,
  };

  const events = JSON.stringify([makeUpdateEvent(badTokens, null, null)]);
  const script = makeRunScript(usagePath, events);
  const { lines } = spawn(script, env);

  const record = lines && lines.length >= 1 ? (() => { try { return JSON.parse(lines[0]); } catch(_){ return null; } })() : null;
  assert(record !== null, "3.4 emits record even with malformed token values", "Got: " + JSON.stringify(lines));
  if (record) {
    assert(record.input_tokens === null, "3.4 negative input → null");
    assert(record.output_tokens === null, "3.4 float output → null");
    assert(record.reasoning_tokens === null, "3.4 NaN reasoning → null");
    assert(record.cached_input_tokens === null, "3.4 non-numeric cache.read → null");
    assert(record.total_tokens === null, "3.4 Infinity total → null");
    assert(record.event_type === "incremental", "3.4 record is still incremental");
  }
}

// -- 3.5: Invalid stage identity suppresses writes ------------------------
{
  // 3.5a – OPSX_ROUND = "0" (not positive)
  {
    const usagePath = path.join(TMP, "inv-3.5a.jsonl");
    const env = {
      OPSX_USAGE_PATH: usagePath,
      OPSX_PLAN_NAME: "plan-3.5a",
      OPSX_RUN_ID: "run-3.5a",
      OPSX_CHANGE_ID: "change-3.5a",
      OPSX_STAGE: "implement",
      OPSX_ROUND: "0",
    };
    const events = JSON.stringify([makeUpdateEvent({ input: 1, output: 1, reasoning: 0, cache: { read: 0, write: 0 }, total: 2 }, "p", "m")]);
    const script = makeRunScript(usagePath, events);
    const { lines } = spawn(script, env);
    assert(lines && lines[0] === "INERT", "3.5a OPSX_ROUND=0 suppressed (inert)", "Got: " + JSON.stringify(lines));
  }

  // 3.5b – OPSX_ROUND = "3.5" (not integer)
  {
    const usagePath = path.join(TMP, "inv-3.5b.jsonl");
    const env = {
      OPSX_USAGE_PATH: usagePath,
      OPSX_PLAN_NAME: "plan-3.5b",
      OPSX_RUN_ID: "run-3.5b",
      OPSX_CHANGE_ID: "change-3.5b",
      OPSX_STAGE: "implement",
      OPSX_ROUND: "3.5",
    };
    const events = JSON.stringify([makeUpdateEvent({ input: 1, output: 1, reasoning: 0, cache: { read: 0, write: 0 }, total: 2 }, "p", "m")]);
    const script = makeRunScript(usagePath, events);
    const { lines } = spawn(script, env);
    assert(lines && lines[0] === "INERT", "3.5b OPSX_ROUND=3.5 suppressed (float)", "Got: " + JSON.stringify(lines));
  }

  // 3.5c – OPSX_ROUND = "3abc" (trailing garbage)
  {
    const usagePath = path.join(TMP, "inv-3.5c.jsonl");
    const env = {
      OPSX_USAGE_PATH: usagePath,
      OPSX_PLAN_NAME: "plan-3.5c",
      OPSX_RUN_ID: "run-3.5c",
      OPSX_CHANGE_ID: "change-3.5c",
      OPSX_STAGE: "implement",
      OPSX_ROUND: "3abc",
    };
    const events = JSON.stringify([makeUpdateEvent({ input: 1, output: 1, reasoning: 0, cache: { read: 0, write: 0 }, total: 2 }, "p", "m")]);
    const script = makeRunScript(usagePath, events);
    const { lines } = spawn(script, env);
    assert(lines && lines[0] === "INERT", "3.5c OPSX_ROUND=3abc suppressed (trailing garbage)", "Got: " + JSON.stringify(lines));
  }

  // 3.5d – OPSX_ROUND missing entirely
  {
    const usagePath = path.join(TMP, "inv-3.5d.jsonl");
    const env = {
      OPSX_USAGE_PATH: usagePath,
      OPSX_PLAN_NAME: "plan-3.5d",
      OPSX_RUN_ID: "run-3.5d",
      OPSX_CHANGE_ID: "change-3.5d",
      OPSX_STAGE: "implement",
      // OPSX_ROUND intentionally omitted
    };
    const events = JSON.stringify([makeUpdateEvent({ input: 1, output: 1, reasoning: 0, cache: { read: 0, write: 0 }, total: 2 }, "p", "m")]);
    const script = makeRunScript(usagePath, events);
    const { lines } = spawn(script, env);
    assert(lines && lines[0] === "INERT", "3.5d missing OPSX_ROUND suppressed (inert)", "Got: " + JSON.stringify(lines));
  }

  // 3.5e – OPSX_STAGE = "bogus"
  {
    const usagePath = path.join(TMP, "inv-3.5e.jsonl");
    const env = {
      OPSX_USAGE_PATH: usagePath,
      OPSX_PLAN_NAME: "plan-3.5e",
      OPSX_RUN_ID: "run-3.5e",
      OPSX_CHANGE_ID: "change-3.5e",
      OPSX_STAGE: "bogus",
      OPSX_ROUND: "1",
    };
    const events = JSON.stringify([makeUpdateEvent({ input: 1, output: 1, reasoning: 0, cache: { read: 0, write: 0 }, total: 2 }, "p", "m")]);
    const script = makeRunScript(usagePath, events);
    const { lines } = spawn(script, env);
    assert(lines && lines[0] === "INERT", "3.5e unsupported OPSX_STAGE suppressed (inert)", "Got: " + JSON.stringify(lines));
  }

  // 3.5f – OPSX_ROUND = "-1" (negative)
  {
    const usagePath = path.join(TMP, "inv-3.5f.jsonl");
    const env = {
      OPSX_USAGE_PATH: usagePath,
      OPSX_PLAN_NAME: "plan-3.5f",
      OPSX_RUN_ID: "run-3.5f",
      OPSX_CHANGE_ID: "change-3.5f",
      OPSX_STAGE: "implement",
      OPSX_ROUND: "-1",
    };
    const events = JSON.stringify([makeUpdateEvent({ input: 1, output: 1, reasoning: 0, cache: { read: 0, write: 0 }, total: 2 }, "p", "m")]);
    const script = makeRunScript(usagePath, events);
    const { lines } = spawn(script, env);
    assert(lines && lines[0] === "INERT", "3.5f OPSX_ROUND=-1 suppressed (negative)", "Got: " + JSON.stringify(lines));
  }
}

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------

console.log("\n──────────────────────────────");
console.log("  passed: %d  failed: %d", passed, failed);
console.log("──────────────────────────────\n");

process.exit(failed > 0 ? 1 : 0);
