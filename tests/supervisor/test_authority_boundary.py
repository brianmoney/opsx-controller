"""Tests for the operator authority boundary.

Covers backend detection fixtures, the store-file contract, the fail-closed
gate, the pure worker-domain write decision, the operator/worker endpoint split
over a real loopback socketpair, the activation-probe evidence checks, the CLI
surface, and a conditional real restricted-process smoke test.
"""

from __future__ import annotations

import argparse
import errno
import io
import json
import os
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from lib.supervisor import authority, endpoints

REPO_ROOT = Path(__file__).resolve().parents[2]


def _principal_facts(mapping: dict[str, int | None]):
    return lambda name: mapping.get(name)


def _available_principals() -> authority.PrincipalSet:
    return authority.PrincipalSet(
        operator=authority.Principal("operator", "alice", 1000),
        service=authority.Principal("service", "opsx-supervisor", 1001),
        worker=authority.Principal("worker", "opsx-worker", 1002),
    )


def _store_stat(*, uid: int = 1001, gid: int = 1001, mode: int = 0o600, is_dir: bool = False):
    """Build a synthetic stat result for a provisioned store file."""
    kind = stat.S_IFDIR if is_dir else stat.S_IFREG
    return os.stat_result((kind | mode, 0, 0, 1, uid, gid, 4096, 0, 0, 0))


def _dir_stat(*, uid: int = 0, gid: int = 0, mode: int = 0o755):
    """Build a synthetic stat result for a trusted parent directory."""
    return os.stat_result((stat.S_IFDIR | mode, 0, 0, 2, uid, gid, 4096, 0, 0, 0))


def _trusted_ancestor(path) -> os.stat_result:
    """Root-owned, non-worker-writable directory for ancestry checks."""
    return _dir_stat(uid=0, gid=0, mode=0o755)


def _worker_writable_ancestor(path) -> os.stat_result:
    """Root-owned but world-writable directory: the worker can replace the leaf."""
    return _dir_stat(uid=0, gid=0, mode=0o777)


def _trusted_launcher_stat(path) -> os.stat_result:
    """Root-owned, executable launcher stat for switch validation."""
    return os.stat_result((stat.S_IFREG | 0o755, 0, 0, 3, 0, 0, 4096, 0, 0, 0))


def _posix_acl(
    *,
    user_obj: int = 0o6,
    named_user: tuple[int, int] | None = None,
    group_obj: int = 0o4,
    named_group: tuple[int, int] | None = None,
    mask: int | None = 0o6,
    other: int = 0o4,
) -> bytes:
    """Build a kernel-layout ``system.posix_acl_access`` xattr value.

    Linux (``linux/posix_acl_xattr.h``) encodes the access ACL as a single
    little-endian ``u32`` version header followed by 8-byte
    ``posix_acl_xattr_entry`` records ``(tag, perm, id)``. There is no
    entry-count field: the count is implied by the remaining payload length.
    """
    undefined = 0xFFFFFFFF

    def entry(tag: int, perm: int, ident: int) -> bytes:
        return struct.pack("<HHI", tag, perm, ident)

    parts = [entry(0x01, user_obj, undefined)]
    if named_user is not None:
        parts.append(entry(0x02, named_user[1], named_user[0]))
    parts.append(entry(0x04, group_obj, undefined))
    if named_group is not None:
        parts.append(entry(0x08, named_group[1], named_group[0]))
    if mask is not None:
        parts.append(entry(0x10, mask, undefined))
    parts.append(entry(0x20, other, undefined))
    return struct.pack("<I", 0x0002) + b"".join(parts)


def _acl_payload(*entries: tuple[int, int, int]) -> bytes:
    """Build a kernel-layout ACL from explicit ``(tag, perm, id)`` entries.

    Unlike :func:`_posix_acl`, this performs no ordering or completeness
    repair, so callers can construct well-aligned but structurally malformed
    payloads to prove the parser rejects them.
    """
    body = b"".join(
        struct.pack("<HHI", tag, perm, ident) for tag, perm, ident in entries
    )
    return struct.pack("<I", 0x0002) + body


def _canonical_launcher_leaf(executable: str = "/usr/bin/setpriv") -> Path:
    """The canonical path the launcher checks actually stat and read."""
    return authority.ledger._canonical(executable)


def _ancestor_only_acl(payload: bytes, leaf: Path):
    """ACL reader yielding no ACL for *leaf* and *payload* for its parents.

    This keeps a launcher ancestry regression honest: the mode-safe launcher
    leaf must pass on its own, so the refusal can only come from the parent
    chain the test names.
    """
    leaf = Path(leaf)

    def reader(path):
        return None if Path(path) == leaf else payload

    return reader


def _denied_from_mode(stat_result: os.stat_result) -> bool:
    """Injected stand-in for the real worker-write denial decision."""
    return not (int(stat_result.st_mode) & 0o002)


def _probe_stdout(
    *,
    uid: int,
    expected_uid: int,
    outcome: str,
    errno_value=None,
    nonce: str | None = "test-nonce",
) -> str:
    payload = {"uid": uid, "expected_uid": expected_uid, "outcome": outcome}
    if nonce is not None:
        payload["nonce"] = nonce
    if errno_value is not None:
        payload["errno"] = errno_value
    return json.dumps(payload)


class DetectionFixtureTests(unittest.TestCase):
    """6.1: supported / unprovisioned / unsupported fixtures and distinctness."""

    def _detect(self, **kwargs) -> authority.CapabilityReport:
        kwargs.setdefault("system", "linux")
        kwargs.setdefault("peer_credential_supported", True)
        kwargs.setdefault("trusted_location_check", lambda path: None)
        kwargs.setdefault("store_stat", lambda path: _store_stat())
        kwargs.setdefault("ancestor_stat", _trusted_ancestor)
        return authority.detect_backend(**kwargs)

    def test_supported_host_is_available(self) -> None:
        report = self._detect(principals=_available_principals())
        self.assertEqual(report.status, authority.STATUS_AVAILABLE)
        self.assertEqual(report.reasons, ())
        self.assertTrue(report.available)

    def test_unprovisioned_host_reports_missing_principals(self) -> None:
        principals = authority.PrincipalSet(
            operator=authority.Principal("operator", "alice", 1000),
            service=authority.Principal("service", "opsx-supervisor", None),
            worker=authority.Principal("worker", "opsx-worker", None),
        )
        report = self._detect(principals=principals)
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(any("not provisioned" in reason for reason in report.reasons))
        self.assertIn("provision", report.provisioning_pointer)

    def test_collapsed_principals_are_rejected(self) -> None:
        collapsed = authority.PrincipalSet(
            operator=authority.Principal("operator", "alice", 1000),
            service=authority.Principal("service", "alice", 1000),
            worker=authority.Principal("worker", "alice", 1000),
        )
        self.assertFalse(collapsed.distinct)
        report = self._detect(principals=collapsed)
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(any("distinct" in reason for reason in report.reasons))

    def test_two_of_three_collapsed_is_rejected(self) -> None:
        collapsed = authority.PrincipalSet(
            operator=authority.Principal("operator", "alice", 1000),
            service=authority.Principal("service", "opsx-supervisor", 1001),
            worker=authority.Principal("worker", "opsx-supervisor", 1001),
        )
        report = self._detect(principals=collapsed)
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(any("distinct" in reason for reason in report.reasons))

    def test_non_linux_host_is_unsupported(self) -> None:
        report = self._detect(
            principals=_available_principals(), system="darwin"
        )
        self.assertEqual(report.status, authority.STATUS_UNSUPPORTED)
        self.assertTrue(any("not Linux" in reason for reason in report.reasons))

    def test_missing_peer_credentials_is_unsupported(self) -> None:
        report = self._detect(
            principals=_available_principals(), peer_credential_supported=False
        )
        self.assertEqual(report.status, authority.STATUS_UNSUPPORTED)
        self.assertTrue(any("SO_PEERCRED" in reason for reason in report.reasons))

    def test_untrusted_state_location_is_unprovisioned(self) -> None:
        def refuse(path: Path) -> None:
            raise authority.ledger.TrustedLocationError("inside a worktree")

        report = self._detect(
            principals=_available_principals(), trusted_location_check=refuse
        )
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(any("not trusted" in reason for reason in report.reasons))

    def test_detection_has_no_filesystem_or_account_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "supervisor.sqlite3"
            before = sorted(p.name for p in Path(tmp).iterdir())
            report = self._detect(
                principals=_available_principals(), state_path=state
            )
            after = sorted(p.name for p in Path(tmp).iterdir())
            self.assertEqual(before, after)
            self.assertFalse(state.exists())
            self.assertEqual(report.status, authority.STATUS_AVAILABLE)

    # -- store-file contract regressions ---------------------------------

    def test_missing_store_file_is_unprovisioned(self) -> None:
        def absent(path: Path) -> os.stat_result:
            raise FileNotFoundError(path)

        report = self._detect(
            principals=_available_principals(),
            state_path="/tmp/opsx-store.sqlite3",
            store_stat=absent,
        )
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(any("does not exist" in reason for reason in report.reasons))

    def test_directory_store_target_is_rejected(self) -> None:
        report = self._detect(
            principals=_available_principals(),
            state_path="/tmp/opsx-store-dir",
            store_stat=lambda path: _store_stat(is_dir=True),
        )
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(any("is a directory" in reason for reason in report.reasons))

    def test_wrong_owner_store_file_is_unprovisioned(self) -> None:
        report = self._detect(
            principals=_available_principals(),
            state_path="/tmp/opsx-store.sqlite3",
            store_stat=lambda path: _store_stat(uid=4242, gid=4242),
        )
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(any("not the service principal" in reason for reason in report.reasons))

    def test_worker_writable_store_file_is_unprovisioned(self) -> None:
        report = self._detect(
            principals=_available_principals(),
            state_path="/tmp/opsx-store.sqlite3",
            store_stat=lambda path: _store_stat(uid=1002, gid=1002, mode=0o600),
        )
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(
            any("does not deny the worker" in reason for reason in report.reasons)
        )

    def test_world_writable_store_file_is_unprovisioned(self) -> None:
        report = self._detect(
            principals=_available_principals(),
            state_path="/tmp/opsx-store.sqlite3",
            store_stat=lambda path: _store_stat(mode=0o666),
        )
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(
            any("does not deny the worker" in reason for reason in report.reasons)
        )

    def test_default_state_path_is_a_file_not_a_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = authority.default_state_path(env={}, service_home=tmp)
        self.assertEqual(path.name, "supervisor.sqlite3")
        self.assertFalse(path.is_dir())

    def test_default_store_target_validated_as_file(self) -> None:
        # A directory at the default location must be rejected, never opened.
        with tempfile.TemporaryDirectory() as tmp:
            default = authority.default_state_path(env={}, service_home=tmp)
        report = self._detect(
            principals=_available_principals(),
            state_path=default,
            store_stat=lambda path: _store_stat(is_dir=True),
        )
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(any("is a directory" in reason for reason in report.reasons))

    # -- path-hardening regressions --------------------------------------

    def test_default_ignores_caller_home(self) -> None:
        # The default must derive from the service principal, never the
        # invoking user's home (which the caller controls).
        with tempfile.TemporaryDirectory() as caller_home:
            with mock.patch.object(authority.Path, "home", return_value=Path(caller_home)):
                path = authority.default_state_path(env={}, service_home="/home/opsx-supervisor")
        self.assertNotIn(str(caller_home), str(path))
        self.assertTrue(str(path).startswith("/home/opsx-supervisor/"))

    def test_default_without_service_principal_uses_system_dir(self) -> None:
        path = authority.default_state_path(
            env={}, service_uid=None, service_home=None
        )
        self.assertTrue(str(path).startswith("/var/lib/"))

    def test_explicit_state_path_is_canonicalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real-store.sqlite3"
            real.write_text("[]", encoding="utf-8")
            link = Path(tmp) / "link-store.sqlite3"
            link.symlink_to(real)
            report = self._detect(
                principals=_available_principals(),
                state_path=link,
                store_stat=lambda path: _store_stat(),
            )
        self.assertEqual(report.state_path, str(real.resolve()))

    def test_symlink_to_worktree_target_is_rejected(self) -> None:
        # A symlink that resolves inside a worktree must fail the trusted
        # location rule because canonicalization happens first.
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp) / "repo"
            (worktree / ".git").mkdir(parents=True)
            inside = worktree / "store.sqlite3"
            inside.write_text("[]", encoding="utf-8")
            link = Path(tmp) / "authority.sqlite3"
            link.symlink_to(inside)

            def trusted(path: Path) -> None:
                authority.ledger._assert_trusted_location(path)

            report = self._detect(
                principals=_available_principals(),
                state_path=link,
                trusted_location_check=trusted,
                store_stat=lambda path: _store_stat(),
            )
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(any("not trusted" in reason for reason in report.reasons))
        self.assertEqual(report.state_path, str(inside.resolve()))

    def test_worker_writable_parent_is_rejected(self) -> None:
        report = self._detect(
            principals=_available_principals(),
            state_path="/tmp/opsx-store.sqlite3",
            store_stat=lambda path: _store_stat(),
            ancestor_stat=_worker_writable_ancestor,
        )
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(
            any("writable by the worker" in reason for reason in report.reasons)
        )

    def test_wrong_owner_parent_is_rejected(self) -> None:
        report = self._detect(
            principals=_available_principals(),
            state_path="/tmp/opsx-store.sqlite3",
            store_stat=lambda path: _store_stat(),
            ancestor_stat=lambda path: _dir_stat(uid=4242, gid=4242, mode=0o700),
        )
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(
            any("neither the root trust root" in reason for reason in report.reasons)
        )

    def test_service_owned_parent_is_accepted(self) -> None:
        report = self._detect(
            principals=_available_principals(),
            state_path="/tmp/opsx-store.sqlite3",
            store_stat=lambda path: _store_stat(),
            ancestor_stat=lambda path: _dir_stat(uid=1001, gid=1001, mode=0o700),
        )
        self.assertEqual(report.status, authority.STATUS_AVAILABLE)

    # -- POSIX ACL regressions -------------------------------------------

    def test_acl_writable_parent_is_rejected(self) -> None:
        # Mode bits alone say 0o700 (worker denied), but a named ACL user entry
        # grants the worker write on the parent: fail closed.
        report = self._detect(
            principals=_available_principals(),
            state_path="/tmp/opsx-store.sqlite3",
            store_stat=lambda path: _store_stat(),
            ancestor_stat=lambda path: _dir_stat(uid=0, gid=0, mode=0o700),
            acl_reader=lambda path: _posix_acl(
                user_obj=0o6, named_user=(1002, 0o6), group_obj=0o0, other=0o0
            ),
        )
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(
            any("writable by the worker" in reason for reason in report.reasons)
        )

    def test_acl_protected_parent_is_accepted(self) -> None:
        # A named ACL entry for the worker that denies write must not by itself
        # fail the boundary.
        report = self._detect(
            principals=_available_principals(),
            state_path="/tmp/opsx-store.sqlite3",
            store_stat=lambda path: _store_stat(),
            ancestor_stat=lambda path: _dir_stat(uid=0, gid=0, mode=0o755),
            acl_reader=lambda path: _posix_acl(
                user_obj=0o7, named_user=(1002, 0o5), group_obj=0o5, other=0o5
            ),
        )
        self.assertEqual(report.status, authority.STATUS_AVAILABLE)

    def test_acl_mask_widening_is_rejected(self) -> None:
        # The ACL group entry plus mask grants the worker (in the owning group)
        # write even though the mode's group bits alone would not be trusted.
        report = self._detect(
            principals=_available_principals(),
            state_path="/tmp/opsx-store.sqlite3",
            store_stat=lambda path: _store_stat(),
            ancestor_stat=lambda path: _dir_stat(uid=0, gid=0, mode=0o770),
            gid_resolver=lambda name: [0],
            acl_reader=lambda path: _posix_acl(
                user_obj=0o7, group_obj=0o7, mask=0o7, other=0o0
            ),
        )
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(
            any("writable by the worker" in reason for reason in report.reasons)
        )

    def test_unparseable_acl_fails_closed(self) -> None:
        report = self._detect(
            principals=_available_principals(),
            state_path="/tmp/opsx-store.sqlite3",
            store_stat=lambda path: _store_stat(),
            ancestor_stat=lambda path: _dir_stat(uid=0, gid=0, mode=0o700),
            acl_reader=lambda path: b"not-a-valid-acl",
        )
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(
            any("writable by the worker" in reason for reason in report.reasons)
        )

    def test_acl_present_but_unevaluable_fails_closed(self) -> None:
        # A well-formed kernel ACL with no evaluable entries must not fall back
        # to the (safe-looking) mode bits.
        header_only = struct.pack("<I", 0x0002)
        report = self._detect(
            principals=_available_principals(),
            state_path="/tmp/opsx-store.sqlite3",
            store_stat=lambda path: _store_stat(),
            ancestor_stat=lambda path: _dir_stat(uid=0, gid=0, mode=0o700),
            acl_reader=lambda path: header_only,
        )
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(
            any("writable by the worker" in reason for reason in report.reasons)
        )

    def test_acl_truncated_record_fails_closed(self) -> None:
        # A kernel ACL whose payload length is not 4 + 8*n is malformed.
        malformed = struct.pack("<I", 0x0002) + struct.pack("<HHI", 0x01, 0o6, 0xFFFFFFFF)[:6]
        report = self._detect(
            principals=_available_principals(),
            state_path="/tmp/opsx-store.sqlite3",
            store_stat=lambda path: _store_stat(),
            ancestor_stat=lambda path: _dir_stat(uid=0, gid=0, mode=0o700),
            acl_reader=lambda path: malformed,
        )
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(
            any("writable by the worker" in reason for reason in report.reasons)
        )

    def test_acl_wrong_version_fails_closed(self) -> None:
        wrong_version = _posix_acl(user_obj=0o6, named_user=(1002, 0o6), other=0o0)
        wrong_version = struct.pack("<I", 0x0003) + wrong_version[4:]
        report = self._detect(
            principals=_available_principals(),
            state_path="/tmp/opsx-store.sqlite3",
            store_stat=lambda path: _store_stat(),
            ancestor_stat=lambda path: _dir_stat(uid=0, gid=0, mode=0o700),
            acl_reader=lambda path: wrong_version,
        )
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(
            any("writable by the worker" in reason for reason in report.reasons)
        )

    def test_kernel_layout_protected_named_user_acl_is_accepted(self) -> None:
        # A real kernel-layout ACL whose named-user entry for the worker denies
        # write must parse and be accepted, not fail closed as unparsable.
        payload = _posix_acl(
            user_obj=0o7,
            named_user=(1002, 0o5),
            group_obj=0o5,
            mask=0o7,
            other=0o5,
        )
        entries = authority.parse_posix_acl_access(payload)
        self.assertIn((authority._ACL_USER, 1002, 0o5), entries)
        report = self._detect(
            principals=_available_principals(),
            state_path="/tmp/opsx-store.sqlite3",
            store_stat=lambda path: _store_stat(),
            ancestor_stat=lambda path: _dir_stat(uid=0, gid=0, mode=0o755),
            acl_reader=lambda path: payload,
        )
        self.assertEqual(report.status, authority.STATUS_AVAILABLE, report.reasons)

    def test_kernel_layout_writable_named_group_acl_is_rejected(self) -> None:
        # A kernel-layout ACL with a named-group entry (the worker is a member
        # of a group that is *not* the file's owning group) whose mask permits
        # write must be rejected even though the mode bits look safe.
        supplementary_gid = 2000
        payload = _posix_acl(
            user_obj=0o7,
            group_obj=0o0,
            named_group=(supplementary_gid, 0o6),
            mask=0o6,
            other=0o0,
        )
        report = self._detect(
            principals=_available_principals(),
            state_path="/tmp/opsx-store.sqlite3",
            store_stat=lambda path: _store_stat(),
            ancestor_stat=lambda path: _dir_stat(uid=0, gid=0, mode=0o700),
            gid_resolver=lambda name: [supplementary_gid],
            acl_reader=lambda path: payload,
        )
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(
            any("writable by the worker" in reason for reason in report.reasons)
        )

    def test_kernel_layout_owning_plus_named_group_write_is_rejected(self) -> None:
        # The worker is in the file's owning group *and* a named group. The
        # owning-group entry denies write but the named-group entry grants it;
        # the group class is their union, so the effective grant must be seen
        # and the mode-safe parent rejected.
        payload = _posix_acl(
            user_obj=0o7,
            group_obj=0o0,
            named_group=(2000, 0o6),
            mask=0o6,
            other=0o0,
        )
        report = self._detect(
            principals=_available_principals(),
            state_path="/tmp/opsx-store.sqlite3",
            store_stat=lambda path: _store_stat(),
            ancestor_stat=lambda path: _dir_stat(uid=0, gid=0, mode=0o700),
            gid_resolver=lambda name: [0, 2000],
            acl_reader=lambda path: payload,
        )
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(
            any("writable by the worker" in reason for reason in report.reasons)
        )

    def test_acl_group_class_union_includes_matching_named_group(self) -> None:
        # Kernel semantics: owning-group and matching named-group entries are a
        # single group class whose permissions union before the mask applies.
        payload = _posix_acl(
            user_obj=0o6,
            group_obj=0o0,
            named_group=(2000, 0o6),
            mask=0o6,
            other=0o0,
        )
        decision = authority.acl_worker_write_decision(
            payload,
            worker_uid=1002,
            worker_gids=[0, 2000],
            st_uid=1001,
            st_gid=0,
        )
        self.assertIsNotNone(decision)
        self.assertFalse(decision.denied)

    def test_acl_group_class_match_does_not_fall_back_to_other(self) -> None:
        # A matching owning-group entry (even a denying one) pre-empts the
        # other entry: the group class is authoritative for a group member.
        payload = _posix_acl(user_obj=0o6, group_obj=0o0, mask=0o6, other=0o6)
        decision = authority.acl_worker_write_decision(
            payload,
            worker_uid=1002,
            worker_gids=[0],
            st_uid=1001,
            st_gid=0,
        )
        self.assertTrue(decision.denied)

    def test_none_acl_means_no_extended_acl(self) -> None:
        self.assertIsNone(
            authority.acl_worker_write_decision(
                None, worker_uid=1002, worker_gids=[], st_uid=1001, st_gid=1001
            )
        )

    def test_empty_acl_is_fail_closed_not_no_acl(self) -> None:
        decision = authority.acl_worker_write_decision(
            b"", worker_uid=1002, worker_gids=[], st_uid=1001, st_gid=1001
        )
        self.assertIsNotNone(decision)
        self.assertFalse(decision.denied)

    def test_empty_acl_on_ancestry_is_not_a_mode_fallback(self) -> None:
        report = self._detect(
            principals=_available_principals(),
            state_path="/tmp/opsx-store.sqlite3",
            store_stat=lambda path: _store_stat(),
            ancestor_stat=lambda path: _dir_stat(uid=0, gid=0, mode=0o700),
            acl_reader=lambda path: b"",
        )
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(
            any("writable by the worker" in reason for reason in report.reasons)
        )

    def test_short_malformed_acl_on_ancestry_is_not_a_mode_fallback(self) -> None:
        short = struct.pack("<I", 0x0002)[:2]
        report = self._detect(
            principals=_available_principals(),
            state_path="/tmp/opsx-store.sqlite3",
            store_stat=lambda path: _store_stat(),
            ancestor_stat=lambda path: _dir_stat(uid=0, gid=0, mode=0o700),
            acl_reader=lambda path: short,
        )
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(
            any("writable by the worker" in reason for reason in report.reasons)
        )

    def test_empty_acl_on_store_leaf_is_not_a_mode_fallback(self) -> None:
        leaf = Path("/tmp/opsx-store.sqlite3")
        report = self._detect(
            principals=_available_principals(),
            state_path=leaf,
            store_stat=lambda path: _store_stat(mode=0o600),
            ancestor_stat=_trusted_ancestor,
            acl_reader=lambda path: b"" if path == leaf else None,
        )
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(
            any("does not deny the worker" in reason for reason in report.reasons)
        )

    def test_short_malformed_acl_on_store_leaf_is_not_a_mode_fallback(self) -> None:
        leaf = Path("/tmp/opsx-store.sqlite3")
        short = struct.pack("<HHI", 0x01, 0o6, 0xFFFFFFFF)[:6]
        report = self._detect(
            principals=_available_principals(),
            state_path=leaf,
            store_stat=lambda path: _store_stat(mode=0o600),
            ancestor_stat=_trusted_ancestor,
            acl_reader=lambda path: short if path == leaf else None,
        )
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(
            any("does not deny the worker" in reason for reason in report.reasons)
        )

    def test_kernel_layout_protected_store_leaf_acl_is_accepted(self) -> None:
        # Kernel-layout named-user ACL on the store leaf that denies write must
        # pass rather than be misread as unparsable.
        leaf = Path("/tmp/opsx-store.sqlite3")
        payload = _posix_acl(
            user_obj=0o6,
            named_user=(1002, 0o4),
            group_obj=0o0,
            mask=0o6,
            other=0o0,
        )
        report = self._detect(
            principals=_available_principals(),
            state_path=leaf,
            store_stat=lambda path: _store_stat(mode=0o600),
            ancestor_stat=_trusted_ancestor,
            acl_reader=lambda path: payload if path == leaf else None,
        )
        self.assertEqual(report.status, authority.STATUS_AVAILABLE, report.reasons)

    # -- structural ACL-layout regressions -------------------------------

    def _malformed_layouts(self) -> dict[str, bytes]:
        """Well-aligned, valid-tag ACLs whose entry layout is malformed.

        The entries *deny* the worker write under a naive partial parse (named
        user in class ``r--``), and the surrounding mode bits are safe. Only
        structural validation can reject these, which is exactly the fail-closed
        property under test: a mode/partial-entry fallback would call them safe.
        """
        undefined = 0xFFFFFFFF
        return {
            "missing-group-obj": _acl_payload(
                (authority._ACL_USER_OBJ, 0o6, undefined),
                (authority._ACL_USER, 0o4, 1002),
                (authority._ACL_OTHER, 0o4, undefined),
            ),
            "named-without-mask": _acl_payload(
                (authority._ACL_USER_OBJ, 0o6, undefined),
                (authority._ACL_USER, 0o4, 1002),
                (authority._ACL_GROUP_OBJ, 0o4, undefined),
                (authority._ACL_OTHER, 0o4, undefined),
            ),
        }

    def test_minimal_acl_layout_is_preserved(self) -> None:
        undefined = 0xFFFFFFFF
        payload = _acl_payload(
            (authority._ACL_USER_OBJ, 0o6, undefined),
            (authority._ACL_GROUP_OBJ, 0o4, undefined),
            (authority._ACL_OTHER, 0o4, undefined),
        )
        self.assertEqual(
            authority.parse_posix_acl_access(payload),
            [
                (authority._ACL_USER_OBJ, undefined, 0o6),
                (authority._ACL_GROUP_OBJ, undefined, 0o4),
                (authority._ACL_OTHER, undefined, 0o4),
            ],
        )

    def test_mask_only_extended_acl_layout_is_preserved(self) -> None:
        undefined = 0xFFFFFFFF
        payload = _acl_payload(
            (authority._ACL_USER_OBJ, 0o7, undefined),
            (authority._ACL_GROUP_OBJ, 0o5, undefined),
            (authority._ACL_MASK, 0o5, undefined),
            (authority._ACL_OTHER, 0o5, undefined),
        )
        self.assertEqual(len(authority.parse_posix_acl_access(payload)), 4)

    def test_structurally_malformed_acl_layouts_are_rejected(self) -> None:
        undefined = 0xFFFFFFFF
        cases = {
            "missing-group-obj": _acl_payload(
                (authority._ACL_USER_OBJ, 0o6, undefined),
                (authority._ACL_USER, 0o6, 1002),
                (authority._ACL_OTHER, 0o4, undefined),
            ),
            "named-without-mask": _acl_payload(
                (authority._ACL_USER_OBJ, 0o6, undefined),
                (authority._ACL_USER, 0o6, 1002),
                (authority._ACL_GROUP_OBJ, 0o4, undefined),
                (authority._ACL_OTHER, 0o4, undefined),
            ),
            "named-entry-out-of-order": _acl_payload(
                (authority._ACL_USER_OBJ, 0o6, undefined),
                (authority._ACL_GROUP_OBJ, 0o4, undefined),
                (authority._ACL_USER, 0o6, 1002),
                (authority._ACL_MASK, 0o6, undefined),
                (authority._ACL_OTHER, 0o4, undefined),
            ),
            "duplicate-named-user": _acl_payload(
                (authority._ACL_USER_OBJ, 0o6, undefined),
                (authority._ACL_USER, 0o4, 1002),
                (authority._ACL_USER, 0o4, 1003),
                (authority._ACL_USER, 0o4, 1002),
                (authority._ACL_GROUP_OBJ, 0o4, undefined),
                (authority._ACL_MASK, 0o4, undefined),
                (authority._ACL_OTHER, 0o4, undefined),
            ),
            "singleton-entry-with-id": _acl_payload(
                (authority._ACL_USER_OBJ, 0o6, 1002),
                (authority._ACL_GROUP_OBJ, 0o4, undefined),
                (authority._ACL_OTHER, 0o4, undefined),
            ),
            "named-entry-undefined-id": _acl_payload(
                (authority._ACL_USER_OBJ, 0o6, undefined),
                (authority._ACL_USER, 0o6, undefined),
                (authority._ACL_GROUP_OBJ, 0o4, undefined),
                (authority._ACL_MASK, 0o6, undefined),
                (authority._ACL_OTHER, 0o4, undefined),
            ),
            "out-of-range-permission-bits": _acl_payload(
                (authority._ACL_USER_OBJ, 0o10, undefined),
                (authority._ACL_GROUP_OBJ, 0o4, undefined),
                (authority._ACL_OTHER, 0o4, undefined),
            ),
            "trailing-entry-after-other": _acl_payload(
                (authority._ACL_USER_OBJ, 0o6, undefined),
                (authority._ACL_GROUP_OBJ, 0o4, undefined),
                (authority._ACL_OTHER, 0o4, undefined),
                (authority._ACL_OTHER, 0o4, undefined),
            ),
        }
        for name, payload in cases.items():
            with self.subTest(layout=name):
                self.assertEqual(authority.parse_posix_acl_access(payload), [])

    def test_structurally_malformed_acl_on_store_leaf_fails_closed(self) -> None:
        leaf = Path("/tmp/opsx-store.sqlite3")
        for name, payload in self._malformed_layouts().items():
            with self.subTest(layout=name):
                report = self._detect(
                    principals=_available_principals(),
                    state_path=leaf,
                    store_stat=lambda path: _store_stat(mode=0o600),
                    ancestor_stat=_trusted_ancestor,
                    acl_reader=lambda path, payload=payload: (
                        payload if path == leaf else None
                    ),
                )
                self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
                self.assertTrue(
                    any(
                        "does not deny the worker" in reason
                        for reason in report.reasons
                    ),
                    report.reasons,
                )

    def test_structurally_malformed_acl_on_ancestry_fails_closed(self) -> None:
        leaf = authority.ledger._canonical("/tmp/opsx-store.sqlite3")
        for name, payload in self._malformed_layouts().items():
            with self.subTest(layout=name):
                report = self._detect(
                    principals=_available_principals(),
                    state_path=leaf,
                    store_stat=lambda path: _store_stat(mode=0o600),
                    ancestor_stat=lambda path: _dir_stat(uid=0, gid=0, mode=0o700),
                    acl_reader=lambda path, payload=payload: (
                        None if path == leaf else payload
                    ),
                )
                self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
                self.assertTrue(
                    any(
                        "writable by the worker" in reason
                        for reason in report.reasons
                    ),
                    report.reasons,
                )

    def test_kernel_layout_launcher_ancestry_acl_is_evaluated(self) -> None:
        # The launcher parent chain uses the same kernel-layout ACL parser: a
        # named-user grant to the worker on a parent must refuse the launcher,
        # while the mode-safe launcher leaf yields no ACL of its own. This
        # cannot pass by refusing the leaf first.
        payload = _posix_acl(
            user_obj=0o6,
            named_user=(1002, 0o6),
            group_obj=0o0,
            other=0o0,
        )
        leaf = _canonical_launcher_leaf()
        reader = _ancestor_only_acl(payload, leaf)
        self.assertIn((authority._ACL_USER, 1002, 0o6), authority.parse_posix_acl_access(payload))
        self.assertIsNone(reader(leaf))
        self.assertEqual(reader(leaf.parent), payload)
        with self.assertRaises(authority.ActivationProbeError) as ctx:
            authority.validate_switch_mechanism(
                ["/usr/bin/setpriv", "--reuid", "opsx-worker"],
                service_uid=1001,
                worker_uid=1002,
                stat_fn=lambda path: _trusted_launcher_stat(path),
                ancestor_stat=_trusted_ancestor,
                worker_denied_check=None,
                acl_reader=reader,
            )
        self.assertIn("writable by the worker", str(ctx.exception))

    def test_acl_writable_store_leaf_is_rejected(self) -> None:
        leaf = Path("/tmp/opsx-store.sqlite3")
        report = self._detect(
            principals=_available_principals(),
            state_path=leaf,
            store_stat=lambda path: _store_stat(mode=0o600),
            ancestor_stat=_trusted_ancestor,
            acl_reader=lambda path: (
                _posix_acl(
                    user_obj=0o6, named_user=(1002, 0o6), group_obj=0o0, other=0o0
                )
                if path == leaf
                else None
            ),
        )
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(
            any("does not deny the worker" in reason for reason in report.reasons)
        )

    def test_uninspectable_acl_fails_closed(self) -> None:
        def broken(path):
            raise OSError(errno.EIO, "xattr read failed")

        report = self._detect(
            principals=_available_principals(),
            state_path="/tmp/opsx-store.sqlite3",
            store_stat=lambda path: _store_stat(),
            ancestor_stat=lambda path: _dir_stat(uid=0, gid=0, mode=0o700),
            acl_reader=broken,
        )
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(
            any("uninspectable extended ACL" in reason for reason in report.reasons)
        )


class FailClosedGateTests(unittest.TestCase):
    """6.2: the gate raises the named error and never downgrades or provisions."""

    def setUp(self) -> None:
        # The gate now resolves the worker's gids before probing. These fixture
        # reports use a synthetic worker name that has no real account, so
        # supply a resolution without an explicit per-call seam; the gid
        # plumbing tests below override this with their own resolvers.
        patcher = mock.patch.object(
            authority, "_default_gid_resolver", return_value=[1002]
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _report(self, status: str, reasons=("simulated",)) -> authority.CapabilityReport:
        return authority.CapabilityReport(status=status, reasons=tuple(reasons))

    def _available_report(self) -> authority.CapabilityReport:
        return authority.CapabilityReport(
            status=authority.STATUS_AVAILABLE,
            reasons=(),
            state_path="/tmp/opsx-store.sqlite3",
            principals=_available_principals(),
        )

    def test_unsupported_report_raises_named_error(self) -> None:
        with self.assertRaises(authority.UnsupportedHostError):
            authority.require_authority_backend(
                report=self._report(authority.STATUS_UNSUPPORTED)
            )

    def test_unprovisioned_report_raises_named_error(self) -> None:
        with self.assertRaises(authority.UnsupportedHostError):
            authority.require_authority_backend(
                report=self._report(authority.STATUS_UNPROVISIONED)
            )

    def test_available_report_always_runs_the_probe(self) -> None:
        with mock.patch.object(authority, "run_activation_probe") as probe:
            authority.require_authority_backend(report=self._available_report())
        probe.assert_called_once()
        kwargs = probe.call_args.kwargs
        self.assertEqual(kwargs["worker_name"], "opsx-worker")
        self.assertEqual(kwargs["worker_uid"], 1002)
        self.assertEqual(kwargs["store_path"], "/tmp/opsx-store.sqlite3")

    def test_no_probe_bypass_route_exists(self) -> None:
        # There is no run_probe/run_probe=False style escape hatch: a probe
        # failure is always surfaced even when the caller tries to skip it.
        with mock.patch.object(
            authority,
            "run_activation_probe",
            side_effect=authority.ActivationProbeError("simulated"),
        ):
            with self.assertRaises(authority.ActivationProbeError):
                authority.require_authority_backend(report=self._available_report())
        for bad_kwarg in ("run_probe", "probe"):
            with self.assertRaises(TypeError):
                authority.require_authority_backend(
                    report=self._available_report(), **{bad_kwarg: False}
                )

    def test_available_report_without_worker_is_refused(self) -> None:
        report = authority.CapabilityReport(
            status=authority.STATUS_AVAILABLE,
            reasons=(),
            state_path="/tmp/opsx-store.sqlite3",
            principals=authority.PrincipalSet(
                operator=authority.Principal("operator", "alice", 1000),
                service=authority.Principal("service", "opsx-supervisor", 1001),
                worker=authority.Principal("worker", "opsx-worker", None),
            ),
        )
        with mock.patch.object(authority, "run_activation_probe") as probe:
            with self.assertRaises(authority.ActivationProbeError):
                authority.require_authority_backend(report=report)
        probe.assert_not_called()

    def test_unavailable_host_never_runs_the_probe(self) -> None:
        with mock.patch.object(authority, "run_activation_probe") as probe:
            with self.assertRaises(authority.UnsupportedHostError):
                authority.require_authority_backend(
                    report=self._report(authority.STATUS_UNSUPPORTED)
                )
        probe.assert_not_called()

    def test_gate_resolves_default_gids_and_forwards_them(self) -> None:
        # No explicit gid seam: the gate resolves the worker's gids itself and
        # forwards them to the probe so its group-class checks are armed.
        with mock.patch.object(authority, "run_activation_probe") as probe:
            with mock.patch.object(
                authority, "_default_gid_resolver", return_value=[4242, 4243]
            ) as resolver:
                authority.require_authority_backend(report=self._available_report())
        resolver.assert_called_once_with("opsx-worker")
        probe.assert_called_once()
        self.assertEqual(probe.call_args.kwargs["worker_gids"], [4242, 4243])

    def test_gate_resolved_gids_refuse_group_writable_launcher(self) -> None:
        # The resolved gids reach the launcher check: a group-writable launcher
        # whose group is one of the worker's groups must be refused.
        def group_writable(path) -> os.stat_result:
            return os.stat_result(
                (stat.S_IFREG | 0o775, 0, 0, 3, 0, 4242, 4096, 0, 0, 0)
            )

        def unexpected_runner(argv, **kwargs):
            raise AssertionError("probe must not run when the launcher is refused")

        with mock.patch.object(
            authority, "_default_gid_resolver", return_value=[4242]
        ):
            with self.assertRaises(authority.ActivationProbeError):
                authority.require_authority_backend(
                    report=self._available_report(),
                    switch=["/usr/bin/setpriv", "--reuid", "opsx-worker"],
                    runner=unexpected_runner,
                    probe_kwargs={
                        "launcher_stat": group_writable,
                        "launcher_denied_check": None,
                        "launcher_acl_reader": lambda path: None,
                        "launcher_ancestor_stat": _trusted_ancestor,
                        "nonce": "test-nonce",
                    },
                )

    def test_gate_resolved_named_group_acl_refuses_launcher_parent(self) -> None:
        # A named-group ACL grant to the worker on a launcher *parent* must be
        # refused through the gate's own default gid resolution. No explicit
        # worker_gids are supplied: the gate's resolver (mocked here) is the
        # only source, and the mode-safe launcher leaf yields no ACL, so this
        # cannot pass by refusing the leaf first.
        leaf = _canonical_launcher_leaf()
        payload = _posix_acl(
            user_obj=0o6,
            group_obj=0o0,
            named_group=(4242, 0o6),
            mask=0o6,
            other=0o0,
        )

        def unexpected_runner(argv, **kwargs):
            raise AssertionError("probe must not run when the launcher is refused")

        with mock.patch.object(
            authority, "_default_gid_resolver", return_value=[1002, 4242]
        ):
            with self.assertRaises(authority.ActivationProbeError) as ctx:
                authority.require_authority_backend(
                    report=self._available_report(),
                    switch=["/usr/bin/setpriv", "--reuid", "opsx-worker"],
                    runner=unexpected_runner,
                    probe_kwargs={
                        "launcher_stat": _trusted_launcher_stat,
                        "launcher_denied_check": None,
                        "launcher_acl_reader": _ancestor_only_acl(payload, leaf),
                        "launcher_ancestor_stat": _trusted_ancestor,
                        "nonce": "test-nonce",
                    },
                )
        message = str(ctx.exception)
        self.assertIn("writable by the worker", message)
        self.assertNotIn(f"executable {leaf} is writable", message)

    def test_gate_gid_resolution_error_fails_closed(self) -> None:
        def boom(name: str):
            raise RuntimeError("directory service unavailable")

        with mock.patch.object(authority, "run_activation_probe") as probe:
            with self.assertRaises(authority.ActivationProbeError):
                authority.require_authority_backend(
                    report=self._available_report(), gid_resolver=boom
                )
        probe.assert_not_called()

    def test_gate_empty_gid_resolution_fails_closed(self) -> None:
        with mock.patch.object(authority, "run_activation_probe") as probe:
            with self.assertRaises(authority.ActivationProbeError):
                authority.require_authority_backend(
                    report=self._available_report(),
                    gid_resolver=lambda name: [],
                )
        probe.assert_not_called()

    def test_gate_provisions_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "authority"
            report = authority.CapabilityReport(
                status=authority.STATUS_UNSUPPORTED,
                reasons=("simulated",),
            )
            with self.assertRaises(authority.UnsupportedHostError):
                authority.require_authority_backend(report=report)
            self.assertEqual(list(Path(tmp).iterdir()), [])
            self.assertFalse(state.exists())


class WorkerWriteDecisionTests(unittest.TestCase):
    """6.3: the pure enforcement decision against real fixture stat data."""

    def test_worker_principal_is_denied_service_owned_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "authority-store.sqlite3"
            target.write_text("[]", encoding="utf-8")
            os.chmod(target, 0o600)
            stat_result = os.stat(target)
            worker_uid = stat_result.st_uid + 1

            decision = authority.decide_worker_write_for_stat(
                stat_result, worker_uid=worker_uid, worker_gids=[]
            )
            self.assertTrue(decision.denied)
            self.assertIn("denies write", decision.reason)

    def test_service_principal_is_permitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "authority-store.sqlite3"
            target.write_text("[]", encoding="utf-8")
            os.chmod(target, 0o600)
            stat_result = os.stat(target)

            decision = authority.decide_worker_write_for_stat(
                stat_result,
                worker_uid=stat_result.st_uid,
                worker_gids=[stat_result.st_gid],
            )
            self.assertFalse(decision.denied)

    def test_group_member_with_group_write_is_permitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "authority-store.sqlite3"
            target.write_text("[]", encoding="utf-8")
            os.chmod(target, 0o660)
            stat_result = os.stat(target)

            decision = authority.decide_worker_write_for_stat(
                stat_result,
                worker_uid=stat_result.st_uid + 1,
                worker_gids=[stat_result.st_gid],
            )
            self.assertFalse(decision.denied)

    def test_root_worker_is_never_denied(self) -> None:
        decision = authority.decide_worker_write(
            st_mode=0o100600, st_uid=1001, st_gid=1001, worker_uid=0, worker_gids=[]
        )
        self.assertFalse(decision.denied)


class EndpointSplitTests(unittest.TestCase):
    """6.4: peer-credential accept/deny and the disjoint dispatch tables."""

    def test_real_socketpair_accepts_matching_principal(self) -> None:
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        credentials = endpoints.peer_credentials(server)
        endpoint = endpoints.Endpoint(endpoints.ENDPOINT_OPERATOR, frozenset({credentials.uid}))

        read_calls: list[int] = []

        def read_request(conn):
            read_calls.append(1)
            return {"verb": "enable"}

        result = endpoint.handle(server, read_request=read_request)
        self.assertEqual(result["verb"], "enable")
        self.assertEqual(result["operator_uid"], credentials.uid)
        self.assertEqual(len(read_calls), 1)

    def test_mismatched_peer_is_closed_before_request_read(self) -> None:
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        real = endpoints.peer_credentials(server)
        mismatched = endpoints.PeerCredentials(pid=real.pid, uid=real.uid + 1, gid=real.gid)
        endpoint = endpoints.Endpoint(endpoints.ENDPOINT_OPERATOR, frozenset({real.uid}))

        read_calls: list[int] = []

        def read_request(conn):
            read_calls.append(1)
            return {"verb": "enable"}

        with mock.patch.object(endpoints, "peer_credentials", return_value=mismatched):
            with self.assertRaises(endpoints.PeerCredentialError):
                endpoint.handle(server, read_request=read_request)
        self.assertEqual(read_calls, [])
        self.assertEqual(server.fileno(), -1, "mismatched peer must be closed")

    def test_credential_lookup_error_closes_the_peer(self) -> None:
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        endpoint = endpoints.Endpoint(endpoints.ENDPOINT_OPERATOR, frozenset({1000}))

        read_calls: list[int] = []

        def read_request(conn):
            read_calls.append(1)
            return {"verb": "enable"}

        with mock.patch.object(
            endpoints,
            "peer_credentials",
            side_effect=endpoints.PeerCredentialError("lookup failed"),
        ):
            with self.assertRaises(endpoints.PeerCredentialError):
                endpoint.handle(server, read_request=read_request)
        self.assertEqual(read_calls, [])
        self.assertEqual(server.fileno(), -1, "unverifiable peer must be closed")

    def test_accept_verified_peer_closes_on_lookup_error(self) -> None:
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        with mock.patch.object(
            endpoints,
            "peer_credentials",
            side_effect=endpoints.PeerCredentialError("lookup failed"),
        ):
            with self.assertRaises(endpoints.PeerCredentialError):
                endpoints.accept_verified_peer(server, allowed_uids=[1000])
        self.assertEqual(server.fileno(), -1)

    def test_missing_peer_credentials_is_a_named_error(self) -> None:
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        with mock.patch.object(endpoints.socket, "SO_PEERCRED", None):
            with self.assertRaises(endpoints.PeerCredentialError):
                endpoints.peer_credentials(server)

    def test_handler_tables_are_disjoint(self) -> None:
        self.assertTrue(endpoints.handler_tables_are_disjoint())
        self.assertEqual(
            set(endpoints.OPERATOR_HANDLERS) & set(endpoints.WORKER_HANDLERS), set()
        )

    def test_operator_verb_unreachable_on_worker_endpoint(self) -> None:
        worker = endpoints.Endpoint(endpoints.ENDPOINT_WORKER, frozenset({1002}))
        for verb in endpoints.OPERATOR_HANDLERS:
            with self.assertRaises(endpoints.EndpointError):
                worker.resolve(verb)

    def test_worker_verb_unreachable_on_operator_endpoint(self) -> None:
        operator = endpoints.Endpoint(endpoints.ENDPOINT_OPERATOR, frozenset({1000}))
        for verb in endpoints.WORKER_HANDLERS:
            with self.assertRaises(endpoints.EndpointError):
                operator.resolve(verb)

    def test_no_operator_credential_material_exists(self) -> None:
        worker = endpoints.Endpoint(endpoints.ENDPOINT_WORKER, frozenset({1002}))
        self.assertEqual(worker.credential_material, {})
        self.assertFalse(endpoints.operator_credential_material_present(worker))
        self.assertFalse(endpoints.USES_TOKEN_MATERIAL)


class ActivationProbeTests(unittest.TestCase):
    """6.5: outcome mapping, real execution evidence, and the CLI surface."""

    def test_interpret_exit_codes(self) -> None:
        self.assertEqual(
            authority.interpret_probe_exit(authority.PROBE_EXIT_DENIED), "denied"
        )
        self.assertEqual(
            authority.interpret_probe_exit(authority.PROBE_EXIT_WROTE), "wrote"
        )
        self.assertEqual(authority.interpret_probe_exit(4), "indeterminate")
        self.assertEqual(authority.interpret_probe_exit(None), "indeterminate")

    def _runner(self, returncode=None, stdout="", exc=None):
        def run(argv, **kwargs):
            if exc is not None:
                raise exc
            return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

        return run

    def _probe(
        self,
        runner,
        *,
        worker_uid=1002,
        store_path="/tmp/opsx-store.sqlite3",
        nonce="test-nonce",
    ):
        return authority.run_activation_probe(
            store_path=store_path,
            worker_name="opsx-worker",
            worker_uid=worker_uid,
                switch=["/usr/bin/setpriv", "--reuid", "opsx-worker"],
            runner=runner,
            service_uid=1001,
            launcher_stat=_trusted_launcher_stat,
            launcher_denied_check=_denied_from_mode,
            nonce=nonce,
        )

    def test_eacces_with_execution_evidence_passes(self) -> None:
        result = self._probe(
            self._runner(
                returncode=authority.PROBE_EXIT_DENIED,
                stdout=_probe_stdout(
                    uid=1002,
                    expected_uid=1002,
                    outcome="denied",
                    errno_value=errno.EACCES,
                ),
            )
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.outcome, "denied")
        self.assertEqual(result.evidence["uid"], 1002)

    def test_successful_write_fails(self) -> None:
        with self.assertRaises(authority.ActivationProbeError):
            self._probe(
                self._runner(
                    returncode=authority.PROBE_EXIT_WROTE,
                    stdout=_probe_stdout(uid=1002, expected_uid=1002, outcome="wrote"),
                )
            )

    def test_forged_exit_three_without_evidence_fails(self) -> None:
        # A bare exit 3 with no execution evidence must never be accepted.
        with self.assertRaises(authority.ActivationProbeError):
            self._probe(
                self._runner(returncode=authority.PROBE_EXIT_DENIED, stdout="")
            )

    def test_forged_exit_three_with_wrong_uid_fails(self) -> None:
        with self.assertRaises(authority.ActivationProbeError):
            self._probe(
                self._runner(
                    returncode=authority.PROBE_EXIT_DENIED,
                    stdout=_probe_stdout(
                        uid=0,
                        expected_uid=1002,
                        outcome="denied",
                        errno_value=errno.EACCES,
                    ),
                )
            )

    def test_exit_three_with_non_eacces_errno_fails(self) -> None:
        with self.assertRaises(authority.ActivationProbeError):
            self._probe(
                self._runner(
                    returncode=authority.PROBE_EXIT_DENIED,
                    stdout=_probe_stdout(
                        uid=1002,
                        expected_uid=1002,
                        outcome="denied",
                        errno_value=errno.EPERM,
                    ),
                )
            )

    def test_indeterminate_result_fails(self) -> None:
        with self.assertRaises(authority.ActivationProbeError):
            self._probe(
                self._runner(
                    returncode=4,
                    stdout=_probe_stdout(
                        uid=1002, expected_uid=1002, outcome="error", errno_value=errno.ENOENT
                    ),
                )
            )

    def test_replayed_evidence_nonce_is_rejected(self) -> None:
        # Evidence from another invocation (or pre-baked) carries the wrong
        # nonce and must never be accepted, even with matching uid/errno/exit.
        with self.assertRaises(authority.ActivationProbeError):
            self._probe(
                self._runner(
                    returncode=authority.PROBE_EXIT_DENIED,
                    stdout=_probe_stdout(
                        uid=1002,
                        expected_uid=1002,
                        outcome="denied",
                        errno_value=errno.EACCES,
                        nonce="some-other-invocation",
                    ),
                )
            )

    def test_probe_child_environment_is_scrubbed(self) -> None:
        # PYTHONPATH and other startup-hook variables must not reach the child;
        # only the benign allowlist is passed through.
        captured: dict[str, dict[str, str]] = {}

        def runner(argv, **kwargs):
            captured["env"] = dict(kwargs.get("env") or {})
            return types.SimpleNamespace(
                returncode=authority.PROBE_EXIT_DENIED,
                stdout=_probe_stdout(
                    uid=1002,
                    expected_uid=1002,
                    outcome="denied",
                    errno_value=errno.EACCES,
                ),
                stderr="",
            )

        hostile = {
            "PATH": "/usr/bin",
            "PYTHONPATH": "/tmp/attacker",
            "PYTHONSTARTUP": "/tmp/attacker/startup.py",
            "PYTHONHOME": "/tmp/attacker",
            "LD_PRELOAD": "/tmp/attacker/lib.so",
            "BASH_ENV": "/tmp/attacker/env.sh",
        }
        result = authority.run_activation_probe(
            store_path="/tmp/opsx-store.sqlite3",
            worker_name="opsx-worker",
            worker_uid=1002,
            switch=["/usr/bin/setpriv", "--reuid", "opsx-worker"],
            runner=runner,
            env=hostile,
            service_uid=1001,
            launcher_stat=_trusted_launcher_stat,
            launcher_denied_check=_denied_from_mode,
            nonce="test-nonce",
        )
        self.assertTrue(result.passed)
        child_env = captured["env"]
        for hostile_key in (
            "PYTHONPATH",
            "PYTHONSTARTUP",
            "PYTHONHOME",
            "LD_PRELOAD",
            "BASH_ENV",
        ):
            self.assertNotIn(hostile_key, child_env)
        self.assertEqual(child_env.get("PATH"), "/usr/bin")

    def test_probe_uses_isolated_interpreter_flags(self) -> None:
        # The interpreter must run isolated (no site, no user site) so a
        # sitecustomize/usercustomize hook cannot intercept the probe.
        captured: dict[str, list[str]] = {}

        def runner(argv, **kwargs):
            captured["argv"] = list(argv)
            return types.SimpleNamespace(
                returncode=authority.PROBE_EXIT_DENIED,
                stdout=_probe_stdout(
                    uid=1002,
                    expected_uid=1002,
                    outcome="denied",
                    errno_value=errno.EACCES,
                ),
                stderr="",
            )

        self._probe(runner)
        argv = captured["argv"]
        self.assertIn("-I", argv)
        self.assertIn("-S", argv)

    def test_spawn_failure_fails(self) -> None:
        with self.assertRaises(authority.ActivationProbeError):
            self._probe(self._runner(exc=OSError("no such mechanism")))

    def test_missing_worker_uid_fails(self) -> None:
        with self.assertRaises(authority.ActivationProbeError):
            self._probe(
                self._runner(returncode=authority.PROBE_EXIT_DENIED),
                worker_uid=None,
            )

    def test_missing_store_path_fails(self) -> None:
        with self.assertRaises(authority.ActivationProbeError):
            self._probe(
                self._runner(returncode=authority.PROBE_EXIT_DENIED),
                store_path=None,
            )

    def test_missing_switch_mechanism_fails(self) -> None:
        with self.assertRaises(authority.ActivationProbeError):
            authority.run_activation_probe(
                store_path="/tmp/opsx-store.sqlite3",
                worker_name="opsx-worker",
                worker_uid=1002,
                switch=[],
                runner=self._runner(returncode=authority.PROBE_EXIT_DENIED),
            )

    def test_bare_untrusted_switch_is_refused_without_running(self) -> None:
        called: list[str] = []

        def runner(argv, **kwargs):
            called.append("ran")
            return types.SimpleNamespace(returncode=authority.PROBE_EXIT_DENIED, stdout="")

        with self.assertRaises(authority.ActivationProbeError):
            authority.run_activation_probe(
                store_path="/tmp/opsx-store.sqlite3",
                worker_name="opsx-worker",
                worker_uid=1002,
                switch=["my-helper", "--user", "opsx-worker"],
                runner=runner,
            )
        self.assertEqual(called, [])

    def test_worker_owned_absolute_helper_is_refused(self) -> None:
        # An absolute helper owned by the worker (or a caller uid that is
        # neither root nor service) is not part of the trusted base.
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / "probe-helper"
            helper.write_text("#!/bin/sh\n", encoding="utf-8")
            os.chmod(helper, 0o755)
            with self.assertRaises(authority.ActivationProbeError):
                authority.validate_switch_mechanism(
                    [str(helper), "{user}"], service_uid=1001, worker_uid=1002
                )

    def test_trusted_owned_absolute_helper_is_allowlisted(self) -> None:
        # A provisioned helper with root/service ownership and trusted
        # ancestry is accepted ("configured").
        with tempfile.TemporaryDirectory() as tmp:
            helper = Path(tmp) / "probe-helper"
            helper.write_text("#!/bin/sh\n", encoding="utf-8")
            os.chmod(helper, 0o755)
            kind = authority.validate_switch_mechanism(
                [str(helper), "{user}"],
                service_uid=1001,
                worker_uid=1002,
                stat_fn=_trusted_launcher_stat,
                ancestor_stat=_trusted_ancestor,
                worker_denied_check=_denied_from_mode,
            )
        self.assertEqual(kind, "configured")

    def test_launcher_path_is_canonicalized_before_validation(self) -> None:
        # A symlink in a trusted directory must not conceal a worker-owned real
        # target: validation canonicalizes the path before the ownership check.
        canonical_calls: list[object] = []
        real_canonical = authority.ledger._canonical

        def spy(path):
            canonical_calls.append(path)
            return real_canonical(path)

        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "tool"
            real.write_text("#!/bin/sh\n", encoding="utf-8")
            os.chmod(real, 0o755)
            link = Path(tmp) / "trusted-name"
            link.symlink_to(real)
            with mock.patch.object(authority.ledger, "_canonical", side_effect=spy):
                with self.assertRaises(authority.ActivationProbeError):
                    authority.validate_switch_mechanism(
                        [str(link), "{user}"],
                        service_uid=1001,
                        worker_uid=1002,
                        worker_denied_check=_denied_from_mode,
                    )
        self.assertTrue(canonical_calls, "launcher path must be canonicalized")

    def test_canonical_switch_replaces_launcher_with_real_target(self) -> None:
        # The execution argv must carry the resolved file, never the symlink
        # spelling a worker could repoint between validation and spawn.
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real-tool"
            real.write_text("#!/bin/sh\n", encoding="utf-8")
            os.chmod(real, 0o755)
            link = Path(tmp) / "trusted-name"
            link.symlink_to(real)
            mechanism = authority.canonical_switch_mechanism(
                [str(link), "-u", "opsx-worker", "--"],
                service_uid=1001,
                worker_uid=1002,
                stat_fn=lambda path: _trusted_launcher_stat(path),
                ancestor_stat=_trusted_ancestor,
                worker_denied_check=_denied_from_mode,
            )
        self.assertEqual(mechanism[0], str(real.resolve()))
        self.assertNotIn(str(link), mechanism)

    def test_probe_executes_canonical_launcher_not_symlink(self) -> None:
        # A symlink swapped to a worker-controlled wrapper after validation must
        # not be executed: the probe argv[0] is the canonical verified path.
        captured: dict[str, list[str]] = {}

        def runner(argv, **kwargs):
            captured["argv"] = list(argv)
            return types.SimpleNamespace(
                returncode=authority.PROBE_EXIT_DENIED,
                stdout=_probe_stdout(
                    uid=1002,
                    expected_uid=1002,
                    outcome="denied",
                    errno_value=errno.EACCES,
                ),
                stderr="",
            )

        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real-switcher"
            real.write_text("#!/bin/sh\n", encoding="utf-8")
            os.chmod(real, 0o755)
            link = Path(tmp) / "switcher-link"
            link.symlink_to(real)
            authority.run_activation_probe(
                store_path="/tmp/opsx-store.sqlite3",
                worker_name="opsx-worker",
                worker_uid=1002,
                switch=[str(link), "-u", "opsx-worker", "--"],
                runner=runner,
                service_uid=1001,
                launcher_stat=_trusted_launcher_stat,
                launcher_ancestor_stat=_trusted_ancestor,
                launcher_acl_reader=lambda path: None,
                launcher_denied_check=_denied_from_mode,
                nonce="test-nonce",
            )
        self.assertEqual(captured["argv"][0], str(real.resolve()))
        self.assertNotIn(str(link), captured["argv"])

    def test_launcher_acl_writable_parent_is_refused(self) -> None:
        # A launcher parent that is mode-safe but carries a named ACL grant to
        # the worker must be refused; the worker could otherwise swap the
        # launcher after validation. The mode-safe launcher leaf yields no ACL,
        # so the refusal can only come from the parent chain this test names.
        leaf = _canonical_launcher_leaf()
        payload = _posix_acl(
            user_obj=0o6, named_user=(1002, 0o6), group_obj=0o0, other=0o0
        )
        with self.assertRaises(authority.ActivationProbeError) as ctx:
            authority.validate_switch_mechanism(
                ["/usr/bin/setpriv", "--reuid", "opsx-worker"],
                service_uid=1001,
                worker_uid=1002,
                stat_fn=lambda path: _trusted_launcher_stat(path),
                ancestor_stat=_trusted_ancestor,
                worker_denied_check=None,
                acl_reader=_ancestor_only_acl(payload, leaf),
            )
        message = str(ctx.exception)
        self.assertIn("writable by the worker", message)
        self.assertNotIn(f"executable {leaf} is writable", message)

    def test_worker_writable_helper_directory_is_refused(self) -> None:
        kind_calls: list[str] = []
        with self.assertRaises(authority.ActivationProbeError):
            authority.validate_switch_mechanism(
                ["/opt/provisioned/probe-helper", "{user}"],
                service_uid=1001,
                worker_uid=1002,
                stat_fn=_trusted_launcher_stat,
                ancestor_stat=_worker_writable_ancestor,
                worker_denied_check=_denied_from_mode,
            )
        self.assertEqual(kind_calls, [])

    def test_configured_bare_command_resolves_via_trusted_dirs_only(self) -> None:
        # A bare configured helper is resolved through the trusted-dir lookup,
        # never ambient PATH. When the lookup finds nothing it is refused.
        mechanism = authority.discover_switch_mechanism(
            "opsx-worker",
            env={authority.SWITCH_CMD_ENV: "my-helper --user {user} --"},
            which=lambda name: None,
        )
        self.assertIsNone(mechanism)

    def test_configured_bare_command_resolves_to_trusted_absolute_path(self) -> None:
        mechanism = authority.discover_switch_mechanism(
            "opsx-worker",
            env={authority.SWITCH_CMD_ENV: "setpriv --reuid {user}"},
            which=lambda name: "/usr/bin/setpriv" if name == "setpriv" else None,
        )
        self.assertEqual(
            mechanism,
            ["/usr/bin/setpriv", "--reuid", "opsx-worker", "--regid", "opsx-worker",
             "--clear-groups"],
        )

    def test_configured_command_cannot_inject_arbitrary_helper(self) -> None:
        # Even an absolute, trusted-owned helper is not selectable through the
        # environment unless its basename is allowlisted; the argv is always
        # built from the allowlist, never taken from the environment.
        mechanism = authority.discover_switch_mechanism(
            "opsx-worker",
            env={authority.SWITCH_CMD_ENV: "/tmp/attacker/wrapper --user {user}"},
            which=lambda name: None,
        )
        self.assertIsNone(mechanism)

    def test_configured_command_cannot_inject_extra_argv(self) -> None:
        # Option injection through the environment is discarded: the argv is
        # constructed from the allowlisted launcher only.
        mechanism = authority.discover_switch_mechanism(
            "opsx-worker",
            env={authority.SWITCH_CMD_ENV: "setpriv --reuid {user} --no-new-privs"},
            which=lambda name: "/usr/bin/setpriv",
        )
        self.assertEqual(
            mechanism,
            ["/usr/bin/setpriv", "--reuid", "opsx-worker", "--regid", "opsx-worker",
             "--clear-groups"],
        )

    def test_discover_switch_returns_absolute_tool_paths(self) -> None:
        # Discovery must not return a bare name: PATH shadowing is impossible
        # because the resolved absolute path is what gets executed.
        mechanism = authority.discover_switch_mechanism(
            "opsx-worker",
            env={},
            which=lambda name: "/usr/sbin/runuser" if name == "runuser" else None,
        )
        self.assertEqual(
            mechanism, ["/usr/sbin/runuser", "-u", "opsx-worker", "--"]
        )

    def test_path_shadowed_switcher_is_not_discovered(self) -> None:
        # shutil.which is only consulted with the trusted-dir path; an
        # attacker-controlled PATH entry is never used.
        with mock.patch.dict(
            os.environ, {"PATH": "/tmp/attacker-bin"}, clear=False
        ):
            with mock.patch.object(
                authority.shutil, "which", wraps=authority.shutil.which
            ) as which:
                authority.discover_switch_mechanism("opsx-worker", env={})
        self.assertTrue(which.call_args_list)
        for call in which.call_args_list:
            path_arg = call.kwargs.get("path") or ""
            self.assertNotIn("/tmp/attacker-bin", path_arg)
            self.assertEqual(path_arg, os.pathsep.join(authority.TRUSTED_SWITCH_DIRS))

    def test_supervise_status_exits_zero_on_unsupported(self) -> None:
        from lib.orchestrator import cmd_supervise

        unsupported = authority.CapabilityReport(
            status=authority.STATUS_UNSUPPORTED, reasons=("not Linux",)
        )
        with mock.patch.object(
            cmd_supervise.authority, "detect_backend", return_value=unsupported
        ):
            out = io.StringIO()
            with redirect_stdout(out):
                code = cmd_supervise.cmd_supervise_status(
                    argparse.Namespace(json=False)
                )
        self.assertEqual(code, 0)
        self.assertIn("unsupported", out.getvalue())

    def test_supervise_probe_exits_nonzero_on_unsupported(self) -> None:
        from lib.orchestrator import cmd_supervise

        with mock.patch.object(
            cmd_supervise.authority,
            "require_authority_backend",
            side_effect=cmd_supervise.authority.UnsupportedHostError("simulated"),
        ):
            err = io.StringIO()
            with redirect_stderr(err):
                code = cmd_supervise.cmd_supervise_probe(argparse.Namespace())
        self.assertNotEqual(code, 0)
        self.assertIn("UnsupportedHostError", err.getvalue())

    def test_supervise_probe_names_probe_failure(self) -> None:
        from lib.orchestrator import cmd_supervise

        with mock.patch.object(
            cmd_supervise.authority,
            "require_authority_backend",
            side_effect=cmd_supervise.authority.ActivationProbeError("simulated"),
        ):
            err = io.StringIO()
            with redirect_stderr(err):
                code = cmd_supervise.cmd_supervise_probe(argparse.Namespace())
        self.assertNotEqual(code, 0)
        self.assertIn("ActivationProbeError", err.getvalue())

    def test_supervise_probe_never_provisions(self) -> None:
        from lib.orchestrator import cmd_supervise

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                cmd_supervise.authority,
                "require_authority_backend",
                side_effect=cmd_supervise.authority.UnsupportedHostError("simulated"),
            ):
                with redirect_stderr(io.StringIO()):
                    cmd_supervise.cmd_supervise_probe(argparse.Namespace())
            self.assertEqual(list(Path(tmp).iterdir()), [])


class DiagnosticsBoundaryFreeTests(unittest.TestCase):
    """6.6: the independent diagnostics never acquire a boundary dependency."""

    _DIAGNOSTICS = (
        "lib.orchestrator.cmd_doctor",
        "lib.orchestrator.cmd_status",
        "lib.orchestrator.cmd_logs",
        "lib.orchestrator.report",
    )

    def test_diagnostics_do_not_import_the_boundary(self) -> None:
        code = (
            "import importlib, sys\n"
            "mods = {0!r}\n"
            "for name in mods:\n"
            "    saved = dict(sys.modules)\n"
            "    importlib.import_module(name)\n"
            "    for boundary in ('lib.supervisor.authority', 'lib.supervisor.endpoints'):\n"
            "        assert boundary not in sys.modules, (name, boundary)\n"
            "print('BOUNDARY-FREE')\n"
        ).format(self._DIAGNOSTICS)
        import subprocess

        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("BOUNDARY-FREE", proc.stdout)

    def test_doctor_runs_on_unsupported_host_fixture(self) -> None:
        from lib.orchestrator import cmd_doctor

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "openspec").mkdir()
            args = argparse.Namespace(repo=str(repo), plan=None, adapter="opencode")
            with mock.patch.object(
                cmd_doctor, "_entry"
            ) as entry, mock.patch("sys.platform", "darwin"):
                entry.return_value.run_doctor_checks.return_value = 0
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cmd_doctor.cmd_doctor(args)
            self.assertEqual(code, 0)
            self.assertIn("All checks passed", out.getvalue())


class RealRestrictedProcessSmokeTest(unittest.TestCase):
    """6.7: conditional real worker-principal denial, skipped when unavailable."""

    @unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux")
    def test_real_worker_domain_write_is_denied(self) -> None:
        worker_name = os.environ.get(authority.WORKER_PRINCIPAL_ENV)
        if not worker_name:
            self.skipTest(
                f"{authority.WORKER_PRINCIPAL_ENV} not set; no distinct provisioned "
                "worker principal is available and this suite provisions nothing"
            )
        worker = authority.resolve_principal("worker", worker_name)
        if not worker.exists:
            self.skipTest(f"worker principal '{worker_name}' does not exist on this host")
        if worker.uid == os.getuid():
            self.skipTest("worker principal shares the current uid; no real boundary")

        mechanism = authority.discover_switch_mechanism(worker_name)
        if not mechanism:
            self.skipTest(
                "no usable restricted-spawn mechanism (setpriv/runuser/"
                f"{authority.SWITCH_CMD_ENV}) is available"
            )

        with tempfile.TemporaryDirectory() as tmp:
            store_dir = Path(tmp) / "authority-store"
            store_dir.mkdir()
            os.chmod(store_dir, 0o755)
            target = store_dir / "supervisor.sqlite3"
            target.write_text("[]", encoding="utf-8")
            os.chmod(target, 0o600)
            try:
                result = authority.run_activation_probe(
                    store_path=target,
                    worker_name=worker_name,
                    worker_uid=worker.uid,
                    switch=mechanism,
                )
            except authority.ActivationProbeError as exc:
                if "could not spawn" in str(exc):
                    self.skipTest(f"restricted spawn is not usable here: {exc}")
                raise
            self.assertTrue(result.passed)
            self.assertEqual(result.evidence["uid"], worker.uid)


class RepoExecutionDomainTests(unittest.TestCase):
    """The privileged service never executes repository-controlled code.

    The worker-domain execution rule has no running service to exercise in this
    change, so the check is structural: no dispatched verb or handler reaches a
    process-execution primitive. The injection seam proves the check itself is
    not vacuous.
    """

    def test_shipped_dispatcher_has_no_execution_surface(self) -> None:
        self.assertFalse(endpoints.dispatcher_executes_repo_code())
        self.assertFalse(
            endpoints.dispatcher_executes_repo_code(endpoints.DISPATCH_TABLES)
        )

    def test_check_detects_an_injected_execution_handler(self) -> None:
        def unsafe(request, credentials):
            return os.system("repo-hook.sh")  # never called; static check only

        self.assertIn("system", unsafe.__code__.co_names)
        tables = {endpoints.ENDPOINT_OPERATOR: {"approve": unsafe}}
        self.assertTrue(endpoints.dispatcher_executes_repo_code(tables))

    def test_check_detects_an_execution_verb_name(self) -> None:
        def harmless(request, credentials):
            return {}

        tables = {endpoints.ENDPOINT_WORKER: {"exec": harmless}}
        self.assertTrue(endpoints.dispatcher_executes_repo_code(tables))


class FrontierPrimaryConfinementTests(unittest.TestCase):
    """The frontier primary runs as the worker principal and holds no service
    identity privileges; a configuration that would make it the service is
    refused."""

    def _peer(self, uid: int) -> endpoints.PeerCredentials:
        return endpoints.PeerCredentials(pid=os.getpid(), uid=uid, gid=uid)

    def test_primary_cannot_invoke_operator_endpoint(self) -> None:
        principals = _available_principals()
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        operator = endpoints.Endpoint(
            endpoints.ENDPOINT_OPERATOR, frozenset({principals.operator.uid})
        )
        primary = self._peer(principals.worker.uid)
        read_calls: list[int] = []

        def read_request(conn):
            read_calls.append(1)
            return {"verb": "enable"}

        with mock.patch.object(endpoints, "peer_credentials", return_value=primary):
            with self.assertRaises(endpoints.PeerCredentialError):
                operator.handle(server, read_request=read_request)
        self.assertEqual(read_calls, [], "the primary must be rejected before any read")

    def test_primary_has_no_operator_verbs_on_the_worker_endpoint(self) -> None:
        principals = _available_principals()
        worker_endpoint = endpoints.Endpoint(
            endpoints.ENDPOINT_WORKER, frozenset({principals.worker.uid})
        )
        for verb in endpoints.OPERATOR_HANDLERS:
            with self.assertRaises(endpoints.EndpointError):
                worker_endpoint.resolve(verb)

    def test_primary_configured_as_service_is_refused(self) -> None:
        collapsed = authority.PrincipalSet(
            operator=authority.Principal("operator", "alice", 1000),
            service=authority.Principal("service", "opsx-worker", 1002),
            worker=authority.Principal("worker", "opsx-worker", 1002),
        )
        self.assertFalse(collapsed.distinct)
        report = authority.detect_backend(
            principals=collapsed,
            system="linux",
            peer_credential_supported=True,
            trusted_location_check=lambda path: None,
            store_stat=lambda path: _store_stat(uid=1002),
            ancestor_stat=_trusted_ancestor,
        )
        self.assertEqual(report.status, authority.STATUS_UNPROVISIONED)
        self.assertTrue(any("distinct" in reason for reason in report.reasons))

    def test_gate_refuses_collapsed_primary_configuration(self) -> None:
        report = authority.CapabilityReport(
            status=authority.STATUS_UNPROVISIONED,
            reasons=("operator, service, and worker are not distinct",),
        )
        with self.assertRaises(authority.UnsupportedHostError):
            authority.require_authority_backend(report=report)


class SuperviseCliTests(unittest.TestCase):
    """The `opsx-plan supervise` namespace is registered end to end, and the
    supported-host status branches are exercised."""

    SCRIPT = REPO_ROOT / "orchestrator" / "opsx-plan.py"

    def _run_supervise(self, *argv: str) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            [sys.executable, str(self.SCRIPT), "supervise", *argv],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )

    def test_supervise_help_lists_both_subcommands(self) -> None:
        proc = self._run_supervise("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status", proc.stdout)
        self.assertIn("probe", proc.stdout)

    def test_supervise_namespace_is_registered_and_status_reports(self) -> None:
        proc = self._run_supervise("status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("capability:", proc.stdout)

    def test_supervise_probe_is_registered_and_fails_closed(self) -> None:
        proc = self._run_supervise("probe")
        # 0 on a provisioned host; 1 with a named error otherwise. Never a
        # parser error (returncode 2) and never an unnamed failure.
        self.assertIn(proc.returncode, (0, 1), proc.stderr)
        if proc.returncode == 1:
            self.assertTrue(
                "UnsupportedHostError" in proc.stderr
                or "ActivationProbeError" in proc.stderr,
                proc.stderr,
            )

    def test_status_available_host_branch(self) -> None:
        from lib.orchestrator import cmd_supervise

        report = authority.CapabilityReport(
            status=authority.STATUS_AVAILABLE,
            reasons=(),
            principals=_available_principals(),
            state_path="/var/lib/opsx-controller/supervisor/supervisor.sqlite3",
        )
        with mock.patch.object(
            cmd_supervise.authority, "detect_backend", return_value=report
        ):
            out = io.StringIO()
            with redirect_stdout(out):
                code = cmd_supervise.cmd_supervise_status(
                    argparse.Namespace(json=False)
                )
        self.assertEqual(code, 0)
        self.assertIn("capability: available", out.getvalue())

    def test_status_json_branch_reports_capability(self) -> None:
        from lib.orchestrator import cmd_supervise

        report = authority.CapabilityReport(
            status=authority.STATUS_AVAILABLE,
            reasons=(),
            principals=_available_principals(),
            state_path="/var/lib/opsx-controller/supervisor/supervisor.sqlite3",
        )
        with mock.patch.object(
            cmd_supervise.authority, "detect_backend", return_value=report
        ):
            out = io.StringIO()
            with redirect_stdout(out):
                code = cmd_supervise.cmd_supervise_status(
                    argparse.Namespace(json=True)
                )
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], authority.STATUS_AVAILABLE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
