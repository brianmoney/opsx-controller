import fs from "node:fs";
import path from "node:path";

/**
 * opsx-usage-emitter – OpenCode plugin that appends normalized JSONL usage
 * sidecar records during opsx-plan stage invocations.
 *
 * The plugin is inert unless OPSX_USAGE_PATH and all required stage identity
 * environment variables are present and valid.
 */

// ---------------------------------------------------------------------------
// Environment gate
// ---------------------------------------------------------------------------

const USAGE_PATH = process.env.OPSX_USAGE_PATH;
const PLAN_NAME = process.env.OPSX_PLAN_NAME;
const RUN_ID = process.env.OPSX_RUN_ID;
const CHANGE_ID = process.env.OPSX_CHANGE_ID;
const STAGE = process.env.OPSX_STAGE;
const ROUND = process.env.OPSX_ROUND;

const VALID_STAGES = new Set(["implement", "review", "archive"]);

/**
 * Returns true only when every required stage identity variable is present
 * and valid.  The plugin is a no-op when this returns false.
 */
function stageIdentityValid() {
  if (!USAGE_PATH || USAGE_PATH.trim() === "") return false;
  if (!PLAN_NAME || !RUN_ID || !CHANGE_ID || !STAGE || !ROUND) return false;
  if (!VALID_STAGES.has(STAGE)) return false;

  const trimmed = ROUND.trim();
  const roundNum = Number(trimmed);
  // Must be a positive integer with no trailing garbage — "3.5", "3abc",
  // "-0", "03", etc. are all rejected.
  if (
    !Number.isInteger(roundNum) ||
    roundNum <= 0 ||
    String(roundNum) !== trimmed
  ) {
    return false;
  }
  return true;
}

// ---------------------------------------------------------------------------
// Numeric normalization
// ---------------------------------------------------------------------------

/**
 * Return `value` as-is when it is a non-negative integer; otherwise `null`.
 * Floating-point, negative, NaN, non-numeric, and ambiguous values are never
 * coerced – missing/unavailable is always `null`.
 */
function safeNonNegInt(value) {
  if (typeof value !== "number") return null;
  if (!Number.isInteger(value)) return null;
  if (value < 0) return null;
  return value;
}

// ---------------------------------------------------------------------------
// Record building
// ---------------------------------------------------------------------------

function isoNow() {
  return new Date().toISOString();
}

/**
 * Build a sidecar record object conforming to the OpenCode plugin usage
 * sidecar contract.  Every field that cannot be populated is `null`.
 */
function buildRecord(eventType, tokens, providerID, modelID) {
  const input = tokens ? safeNonNegInt(tokens.input) : null;
  const output = tokens ? safeNonNegInt(tokens.output) : null;
  const cachedInput =
    tokens && tokens.cache ? safeNonNegInt(tokens.cache.read) : null;
  const reasoning = tokens ? safeNonNegInt(tokens.reasoning) : null;
  const total = tokens ? safeNonNegInt(tokens.total) : null;

  return {
    schema_version: 1,
    plan_name: PLAN_NAME,
    run_id: RUN_ID,
    change_id: CHANGE_ID,
    stage: STAGE,
    round: parseInt(ROUND, 10),
    event_type: eventType,
    provider: typeof providerID === "string" && providerID ? providerID : null,
    model_id: typeof modelID === "string" && modelID ? modelID : null,
    model_alias: null,
    input_tokens: input,
    output_tokens: output,
    cached_input_tokens: cachedInput,
    reasoning_tokens: reasoning,
    total_tokens: total,
    request_count: null,
    latency_ms: null,
    emitted_at: isoNow(),
  };
}

function hasTokenField(record) {
  return (
    record.input_tokens !== null ||
    record.output_tokens !== null ||
    record.cached_input_tokens !== null ||
    record.reasoning_tokens !== null ||
    record.total_tokens !== null
  );
}

function hasUsableField(record) {
  return (
    hasTokenField(record) ||
    record.provider !== null ||
    record.model_id !== null
  );
}

function mergeRecord(primary, fallback) {
  const merged = { ...fallback, ...primary };
  for (const field of [
    "provider",
    "model_id",
    "model_alias",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
    "total_tokens",
  ]) {
    if (merged[field] === null && fallback) merged[field] = fallback[field];
  }
  return merged;
}

// ---------------------------------------------------------------------------
// Sidecar append (best-effort, never throws)
// ---------------------------------------------------------------------------

function appendRecord(record) {
  try {
    const dir = path.dirname(USAGE_PATH);
    fs.mkdirSync(dir, { recursive: true });
    fs.appendFileSync(USAGE_PATH, JSON.stringify(record) + "\n");
  } catch (_e) {
    // File write failures are intentionally swallowed.
    // Usage emission is observability – it must never alter OpenCode behavior.
  }
}

// ---------------------------------------------------------------------------
// Plugin entry-point
// ---------------------------------------------------------------------------

export default async function OpsxUsageEmitter({ project, client, $, directory, worktree }) {
  if (!stageIdentityValid()) {
    // Plugin is inert: do not register any hooks (return empty object).
    return {};
  }

  // OpenCode's session.idle event does not reliably include the final message
  // usage. Keep the latest normalized snapshot so idle can close the stage.
  let latestSnapshot = null;
  let finalEmitted = false;

  return {
    /**
     * event – the main event hook.
     *
     * OpenCode dispatches events in a flat shape:
     *   { type: "<type>", properties: { ... } }
     *
     * Some versions wrap them in a GlobalEvent shape:
     *   { payload: { type: "...", properties: { ... } } }
     *
     * We handle both formats defensively.
     */
    event: async function ({ event }) {
      try {
        // Normalize to { type, properties }
        const evt = event.payload || event;
        const props = evt.properties || {};

        switch (evt.type) {
          // -- Token-bearing update event → incremental ------------------
          case "message.updated": {
            const info = props.info;
            // Only assistant messages carry token usage.
            if (!info || info.role !== "assistant" || !info.tokens) return;

            const record = buildRecord(
              "incremental", info.tokens, info.providerID, info.modelID
            );
            if (hasUsableField(record)) latestSnapshot = record;
            appendRecord(record);
            break;
          }

          // -- Final session event → final ------------------------------
          case "session.idle": {
            if (finalEmitted) return;

            // Prefer usage attached to idle when available, then supplement
            // it with the latest message snapshot. Real idle events commonly
            // have no info at all, so the snapshot is the normal fallback.
            const info = props.info;
            const idleRecord = info
              ? buildRecord("final", info.tokens, info.providerID, info.modelID)
              : null;
            const baseRecord = idleRecord && hasUsableField(idleRecord)
              ? idleRecord
              : latestSnapshot;
            if (!baseRecord || !hasUsableField(baseRecord)) return;

            const record = mergeRecord(
              idleRecord && hasTokenField(idleRecord) ? idleRecord : baseRecord,
              idleRecord && !hasTokenField(idleRecord) ? latestSnapshot : null,
            );
            record.event_type = "final";
            record.emitted_at = isoNow();
            appendRecord(record);
            finalEmitted = true;
            break;
          }

          default:
            // Unsupported event shape – silently ignored.
            break;
        }
      } catch (_e) {
        // Any unexpected error inside the event handler is silently
        // swallowed.  Usage capture must never affect OpenCode behavior.
      }
    },
  };
};
