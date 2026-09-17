"""Acceptance-stage contract: artifact revision identity, outcome model, staleness.

This module is **stdlib-only** and has no supervisor inter-module imports, so
it stays usable from the ledger, the orchestrator, and tests without an import
cycle. It owns three things and nothing else:

- the acceptance outcome vocabulary (``accept`` / ``fix`` / ``escalate``) and
  the strict parser for the ``acceptance_reviewer`` worker's single JSON line;
- the *artifact review set* — a canonical, order-independent description of
  the real artifacts an acceptance verdict judges (the protected manifest
  snapshot hash and its dependency edges, the change's authored artifacts, its
  spec deltas with their delta identity, the referenced canonical specs, and
  the tracked change diff);
- the *artifact revision*: a content hash over that canonical review set, plus
  the pure staleness decision that rejects a verdict recorded against a
  different revision.

The revision is deliberately **not** the broker's ``material_hash``: an
acceptance revision fingerprints a change's authored artifacts, while the
broker's material gate hash invalidates approvals on gate-field changes. Two
authorities, two hashes.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "ABSENT",
    "ACCEPT",
    "ARTIFACT_REVISION_PREFIX",
    "ESCALATE",
    "FIX",
    "OUTCOMES",
    "AcceptanceContractError",
    "artifact_revision",
    "authoritative_artifact_identities",
    "build_review_set",
    "canonical_spec_rel_path",
    "collect_review_set",
    "content_digest",
    "file_digest",
    "normalize_rel_path",
    "normalize_verdict",
    "parse_spec_delta_identity",
    "revision_is_stale",
    "verdict_satisfies_stage",
]

ACCEPT = "accept"
FIX = "fix"
ESCALATE = "escalate"

#: The complete, closed acceptance outcome vocabulary. Any other value is a
#: contract violation, never a silent default.
OUTCOMES: tuple[str, ...] = (ACCEPT, FIX, ESCALATE)

#: Prefix distinguishes an acceptance artifact revision from any other digest
#: (notably the broker's material gate hash) in state and journal records.
ARTIFACT_REVISION_PREFIX = "acceptance-v1:"

#: Marker for an artifact that is part of the review set but absent on disk.
ABSENT = "absent"

_DELTA_OPERATIONS = ("ADDED", "MODIFIED", "REMOVED", "RENAMED")
_OPERATION_RE = re.compile(r"^##\s+(" + "|".join(_DELTA_OPERATIONS) + r")\b")
_REQUIREMENT_RE = re.compile(r"^###\s+Requirement:\s*(.+?)\s*$")


class AcceptanceContractError(ValueError):
    """A verdict or artifact-review-set input violated the acceptance contract."""


# ---------------------------------------------------------------------------
# Canonicalization helpers
# ---------------------------------------------------------------------------


def normalize_rel_path(path: Any) -> str:
    """Normalize a repository-relative path to canonical POSIX form.

    Backslashes become forward slashes and redundant ``./`` segments are
    dropped, so the same artifact reported with cosmetic differences hashes
    identically. Ordering differences are handled by the caller sorting the
    canonical structures.
    """
    if path is None:
        return ""
    text = str(path).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _normalize_text(text: str) -> str:
    """Canonicalize text content for hashing.

    Line endings are normalized to ``\\n`` so a CRLF checkout and an LF
    checkout of the same artifact produce the same revision.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def content_digest(text: Any) -> str:
    """Return the content digest of *text*, or :data:`ABSENT` when missing."""
    if text is None:
        return ABSENT
    normalized = _normalize_text(str(text))
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def file_digest(path: Path | str | None) -> str:
    """Return the byte digest of *path*, or :data:`ABSENT` when it is missing."""
    if path is None:
        return ABSENT
    candidate = Path(path)
    try:
        data = candidate.read_bytes()
    except OSError:
        return ABSENT
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Spec delta identity
# ---------------------------------------------------------------------------


def parse_spec_delta_identity(text: str) -> list[dict[str, str]]:
    """Extract ``(operation, requirement)`` identity pairs from a spec delta.

    A spec delta's identity is the delta operation (``ADDED``/``MODIFIED``/
    ``REMOVED``/``RENAMED``) together with the requirement name it applies to.
    Renaming a requirement is therefore a distinct identity from modifying the
    one it replaced, which is exactly the artifact-level defect acceptance must
    catch. Unparseable text contributes no identity pairs rather than raising.
    """
    identities: list[dict[str, str]] = []
    operation = ""
    for raw_line in _normalize_text(text or "").splitlines():
        operation_match = _OPERATION_RE.match(raw_line)
        if operation_match:
            operation = operation_match.group(1)
            continue
        requirement_match = _REQUIREMENT_RE.match(raw_line)
        if requirement_match and operation:
            identities.append(
                {
                    "operation": operation,
                    "requirement": requirement_match.group(1).strip(),
                }
            )
    return identities


def canonical_spec_rel_path(delta_rel_path: Any) -> str:
    """Map a change spec-delta path to the canonical spec it references.

    ``openspec/changes/<change>/specs/<capability>/spec.md`` references
    ``openspec/specs/<capability>/spec.md``. A path already rooted at
    ``openspec/specs/`` is returned unchanged; anything else yields ``""``.
    """
    rel = normalize_rel_path(delta_rel_path)
    marker = "/specs/"
    if rel.startswith("openspec/specs/"):
        return rel
    index = rel.find(marker)
    if index < 0:
        return ""
    capability = rel[index + len(marker):]
    if not capability.endswith("spec.md"):
        return ""
    return "openspec/specs/" + capability


# ---------------------------------------------------------------------------
# Review set and revision
# ---------------------------------------------------------------------------


def collect_review_set(
    *,
    authored_artifacts: Mapping[str, str | None] | None = None,
    spec_deltas: Mapping[str, str | None] | None = None,
    canonical_specs: Mapping[str, str | None] | None = None,
    tracked_change_files: Mapping[str, str | None] | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Build the file-derived half of a review set from path -> content maps.

    Kept separate from :func:`build_review_set` so callers that collect content
    from disk (the orchestrator) and callers that construct a set by hand
    (tests) share one canonicalization path.
    """

    def _entries(mapping: Mapping[str, str | None] | None) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []
        for raw_path, text in (mapping or {}).items():
            rel = normalize_rel_path(raw_path)
            if not rel:
                continue
            entries.append({"path": rel, "digest": content_digest(text)})
        entries.sort(key=lambda entry: entry["path"])
        return entries

    deltas: list[dict[str, str]] = []
    for raw_path, text in (spec_deltas or {}).items():
        rel = normalize_rel_path(raw_path)
        if not rel:
            continue
        digest = content_digest(text)
        for identity in parse_spec_delta_identity(text or ""):
            deltas.append(
                {
                    "path": rel,
                    "operation": identity["operation"],
                    "requirement": identity["requirement"],
                    "digest": digest,
                }
            )
    deltas.sort(key=lambda entry: (entry["path"], entry["operation"], entry["requirement"]))

    return {
        "authored_artifacts": _entries(authored_artifacts),
        "spec_deltas": deltas,
        "canonical_specs": _entries(canonical_specs),
        "tracked_change_files": _entries(tracked_change_files),
    }


def build_review_set(
    *,
    manifest_snapshot_hash: str = "",
    depends_on: Iterable[Any] = (),
    authored_artifacts: Mapping[str, str | None] | None = None,
    spec_deltas: Mapping[str, str | None] | None = None,
    canonical_specs: Mapping[str, str | None] | None = None,
    tracked_change_files: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    """Build the canonical acceptance artifact review set.

    The returned mapping is fully ordered and normalized: it is safe to hash
    with :func:`artifact_revision`, and two review sets over the same real
    artifacts are equal regardless of input ordering or path spelling.
    """
    review_set: dict[str, Any] = {
        "manifest_snapshot_hash": str(manifest_snapshot_hash or ""),
        "depends_on": sorted(
            {str(dep).strip() for dep in depends_on if str(dep).strip()}
        ),
    }
    review_set.update(
        collect_review_set(
            authored_artifacts=authored_artifacts,
            spec_deltas=spec_deltas,
            canonical_specs=canonical_specs,
            tracked_change_files=tracked_change_files,
        )
    )
    return review_set


def artifact_revision(review_set: Mapping[str, Any]) -> str:
    """Return the canonical content-hash revision for *review_set*.

    The hash is over a canonical JSON encoding (sorted keys, compact
    separators, ASCII-only), so an unrelated ordering or formatting difference
    in the inputs never changes the revision.
    """
    canonical = json.dumps(
        review_set, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return ARTIFACT_REVISION_PREFIX + digest


def authoritative_artifact_identities(review_set: Mapping[str, Any]) -> list[str]:
    """Return the authoritative artifact-identity list for *review_set*.

    This is the complete canonical set an ``accept`` verdict must acknowledge:
    the protected manifest ground truth (its snapshot hash and every dependency
    edge) plus the path of every file-derived artifact in the review set —
    authored artifacts, spec delta files, referenced canonical specs, and
    tracked change files. The list is sorted and duplicate-free, so a reviewer
    cannot satisfy it with a partial, reordered, or differently spelled claim.
    """
    identities: set[str] = set()
    snapshot = str(review_set.get("manifest_snapshot_hash", "") or "").strip()
    identities.add(f"manifest:snapshot:{snapshot or ABSENT}")
    for dep in review_set.get("depends_on", []) or []:
        edge = str(dep).strip()
        if edge:
            identities.add(f"manifest:depends_on:{edge}")
    for group in ("authored_artifacts", "canonical_specs", "tracked_change_files"):
        for entry in review_set.get(group, []) or []:
            path = str(entry.get("path", "") or "")
            if path:
                identities.add(path)
    for entry in review_set.get("spec_deltas", []) or []:
        path = str(entry.get("path", "") or "")
        if path:
            identities.add(path)
    return sorted(identities)


def revision_is_stale(recorded_revision: Any, current_revision: Any) -> bool:
    """True when a recorded revision does not match the artifacts under review.

    A missing or empty recorded revision is stale by definition: it cannot
    satisfy the stage, and neither can a revision recorded over different
    artifacts.
    """
    recorded = str(recorded_revision or "").strip()
    current = str(current_revision or "").strip()
    if not recorded or not current:
        return True
    return recorded != current


# ---------------------------------------------------------------------------
# Verdict parsing and stage satisfaction
# ---------------------------------------------------------------------------


def normalize_verdict(
    payload: Mapping[str, Any], *, required_artifacts: Iterable[Any] | None = None
) -> dict[str, Any]:
    """Validate and normalize one acceptance reviewer JSON payload.

    Raises :class:`AcceptanceContractError` for a missing/unknown outcome, an
    ``accept`` that names no reviewed artifacts (an accept for artifacts the
    reviewer did not inspect never satisfies the stage), or a ``fix`` with no
    defect text for the fixer.

    When *required_artifacts* is given, an ``accept`` is additionally bound to
    that authoritative set: its normalized ``artifacts_reviewed`` must cover
    exactly those identities — no missing entries, no unexpected ones — so a
    partial, arbitrary, or manifest/dependency-omitting accept is a contract
    violation rather than a satisfying verdict.
    """
    if not isinstance(payload, Mapping):
        raise AcceptanceContractError("acceptance verdict must be a JSON object")

    outcome = str(payload.get("outcome") or "").strip().lower()
    if outcome not in OUTCOMES:
        raise AcceptanceContractError(
            f"acceptance outcome {outcome or '(missing)'!r} is not one of "
            f"{', '.join(OUTCOMES)}"
        )

    raw_reviewed = payload.get("artifacts_reviewed")
    reviewed: list[str] = []
    if isinstance(raw_reviewed, Sequence) and not isinstance(raw_reviewed, (str, bytes)):
        for entry in raw_reviewed:
            if isinstance(entry, str) and entry.strip():
                reviewed.append(normalize_rel_path(entry))

    reason = str(payload.get("reason") or "").strip()
    fix_prompt = str(payload.get("fix_prompt") or "").strip()

    if outcome == ACCEPT and not reviewed:
        raise AcceptanceContractError(
            "an accept verdict must name the reviewed artifacts"
        )
    if outcome == ACCEPT and required_artifacts is not None:
        required = {
            normalized
            for entry in required_artifacts
            if (normalized := normalize_rel_path(entry))
        }
        claimed = set(reviewed)
        missing = sorted(required - claimed)
        unexpected = sorted(claimed - required)
        if missing or unexpected:
            raise AcceptanceContractError(
                "an accept verdict must acknowledge exactly the authoritative "
                f"artifact set; missing: {missing or '[]'}; "
                f"unexpected: {unexpected or '[]'}"
            )
    if outcome == FIX and not fix_prompt:
        fix_prompt = reason
    if outcome == FIX and not fix_prompt:
        raise AcceptanceContractError(
            "a fix verdict must name the mechanical defect for the fixer"
        )

    return {
        "outcome": outcome,
        "artifacts_reviewed": reviewed,
        "reason": reason,
        "fix_prompt": fix_prompt,
    }


def verdict_satisfies_stage(
    outcome: Any, recorded_revision: Any, current_revision: Any
) -> bool:
    """True only for a non-stale ``accept`` verdict.

    ``fix`` and ``escalate`` are transitions, never satisfaction; a stale
    ``accept`` is refused and requires a fresh acceptance over the new revision.
    """
    if str(outcome or "").strip().lower() != ACCEPT:
        return False
    return not revision_is_stale(recorded_revision, current_revision)
