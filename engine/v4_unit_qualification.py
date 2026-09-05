"""Root-operated staging and in-unit canaries for the actual protected V4 units.

Runbook steps 3-7 of ``runbooks/v4-authenticated-runtime-integration.md``.
The canary runs inside the EXISTING ``hramatka-api.service`` and
``learn-ukrainian-sources.service`` through a temporary ``ExecStartPre=-``
drop-in, so every probe observes the real unit identity, credential
namespace, sandbox and scoped PostgreSQL login. A same-UID proxy or test
subprocess never satisfies it: the unit is read from the process cgroup and
``$CREDENTIALS_DIRECTORY`` and refused when absent.

Output is fixed codes, counts and digests only. No DSN, address, key, token
or protected text is ever printed. Execution/admission switches stay OFF;
the produced qualification credential is read by public readiness only when
an operator later enables execution with a separate restart.

Every qualification flag is recomputed by the root composer from the complete
probe evidence against a fixed schema. Summaries never override evidence; an
unknown field, wrong type, missing probe or incomplete child proof refuses.
The child closure is proven by starting the production executable inside the
uninstrumented production plan before any instrumented diagnostics; unknown
outcomes (unreachable endpoint, foreign protocol, pending restart job) are
never promoted to a pass.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mmap
import os
import pwd
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = "hramatka-v4-actual-unit-qualification.v1"
CANARY_SCHEMA = "hramatka-v4-unit-canary.v1"
API_UNIT = "hramatka-api.service"
SOURCES_UNIT = "learn-ukrainian-sources.service"
UNITS = {"api": API_UNIT, "sources": SOURCES_UNIT}
CANARIES = ("bwrap", "unit_hardening", "credential_separation", "scoped_database_roles")
CANARY_MARK = "HRAMATKA_V4_CANARY "
CANARY_DROPIN = "zz-v4-qualification-canary.conf"
STAGED_DROPINS = {"api": "v4-api.conf", "sources": "v4-sources.conf"}
SWITCHES = ("HRAMATKA_V4_EXECUTION_ENABLED", "HRAMATKA_V4_ADMISSION_ENABLED")
CONTROL_ROLE = "hramatka_v4_control_writer"
SOURCES_ROLE = "hramatka_v4_sources_writer"
V4_TABLES = (
    "v4_operation_authorizations",
    "v4_operation_jtis",
    "v4_execution_dispatch_bindings",
    "v4_execution_attempts",
    "v4_execution_observations",
    "v4_authorship_receipts",
    "v4_sources_invocations",
)
SOURCES_FUNCTION = "public.hramatka_v4_record_sources_invocation_v1(text,text,text,text)"
CANARY_DOMAIN = b"hramatka-v4-unit-qualification-canary.v1"
CANARY_PAYLOAD = {
    "schema": "hramatka-v4-unit-qualification-canary.v1",
    "fixed": "signing-challenge",
}
FORBIDDEN_TOOLS = (
    "sh",
    "bash",
    "dash",
    "psql",
    "sudo",
    "env",
    "bwrap",
    "curl",
    "ssh",
    "git",
    "python3",
)
HARNESSES = ("codex", "claude")
# Exact production signing-key roles (the verified runtime's KEYRING_ROLES).
SIGNING_ROLES = ("sources", "a3", "fleet_execution")
PROBE_MOUNT_ROOT = "/canary"
PROBE_HOST_ROOT = PROBE_MOUNT_ROOT + "/host"
PROBE_BASE = PROBE_MOUNT_ROOT + "/base"
# Fixed, provider-free native startup of every supported adapter executable.
NATIVE_STARTUP_ARGV = ("--version",)
COMMAND_TIMEOUT = 120
RESTART_MARGIN = 180
HEX32 = re.compile(r"[0-9a-f]{32}")
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Executed by the relocated probe interpreter inside the COMPLETE production
# bwrap plan (all pinned adapter mounts, production environment shape and the
# subscription auth transport when configured). It reports only booleans; it
# never reads or prints protected paths' content, addresses or credentials.
BWRAP_PROBE = r"""
import hashlib, json, os, socket, sys
expect = json.loads(os.environ["HRAMATKA_V4_CANARY_EXPECT"])
# PostgreSQL AuthenticationRequest codes that demand a secret the child lacks.
PG_AUTH_CHALLENGES = {2, 3, 5, 6, 7, 8, 9, 10, 11, 12}
def absent(p):
    try:
        os.stat(p)
        return False
    except FileNotFoundError:
        return True
    except OSError:
        return True
def listing(p):
    try:
        return os.listdir(p)
    except OSError:
        return None
def digest(p):
    try:
        with open(p, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None
def read_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data
def pg_error_fields(payload):
    # ErrorResponse body: (type byte, NUL-terminated value)* then one NUL.
    fields = {}
    i = 0
    while i < len(payload):
        if payload[i] == 0:
            return fields if i == len(payload) - 1 else None
        end = payload.find(b"\0", i + 1)
        if end < 0:
            return None
        fields[payload[i:i + 1]] = payload[i + 1:end]
        i = end + 1
    return None
def pg_denied(host, port, dbname, user):
    # Direct child authentication as a restricted principal without any
    # secret against the actual configured endpoint. Only a validated
    # PostgreSQL authentication challenge or a class-28 authentication
    # rejection is denial. Connection failure, EOF, malformed or foreign
    # protocol is UNKNOWN (False); AuthenticationOk is NOT denial (False).
    try:
        sock = socket.create_connection((host, port), timeout=5)
    except OSError:
        return False
    try:
        sock.settimeout(5)
        params = b"user\0" + user.encode() + b"\0database\0" + dbname.encode() + b"\0\0"
        body = (196608).to_bytes(4, "big") + params
        sock.sendall((len(body) + 4).to_bytes(4, "big") + body)
        head = read_exact(sock, 5)
        if head is None:
            return False
        length = int.from_bytes(head[1:5], "big")
        if not 4 <= length <= 65536:
            return False
        payload = read_exact(sock, length - 4)
        if payload is None:
            return False
        if head[0:1] == b"R":
            return len(payload) >= 4 and int.from_bytes(payload[:4], "big") in PG_AUTH_CHALLENGES
        if head[0:1] == b"E":
            fields = pg_error_fields(payload)
            if not fields or fields.get(b"S") not in (b"FATAL", b"ERROR"):
                return False
            code = fields.get(b"C", b"")
            return len(code) == 5 and code[:2] == b"28"
        return False
    except OSError:
        return False
    finally:
        try:
            sock.sendall(b"X" + (4).to_bytes(4, "big"))
        except OSError:
            pass
        sock.close()
r = {}
procs = listing("/proc")
r["pid_namespace"] = os.getpid() in (1, 2) and procs is not None and "1" in procs
r["parent_proc_invisible"] = all(not e.isdigit() or int(e) <= 2 for e in listing("/proc") or ["x"])
r["credentials_absent"] = absent("/run/credentials") and absent(
    "/run/credentials/hramatka-api.service")
r["signing_absent"] = absent("/run/credentials/hramatka-api.service/v4-signing-keys")
r["home_isolated"] = listing("/home") == ["v4"] and listing("/home/v4") in ([], [".codex"])
r["protected_state_absent"] = all(
    absent(p) for p in ("/etc/hramatka", "/var/lib/hramatka", "/opt/hramatka", "/root"))
r["pg_socket_absent"] = absent("/var/run/postgresql") and absent("/run/postgresql")
r["general_tools_absent"] = all(
    absent(d) for d in ("/bin", "/sbin", "/usr/bin", "/usr/sbin", "/usr/local/bin")
) and all(absent("/runtime/" + t) for t in expect["tools"])
r["uid_unprivileged"] = os.getuid() != 0
r["network_shared"] = True  # the provider needs egress; never claimed as PG denial
r["closure_files_verified"] = bool(expect["files"]) and all(
    os.path.isfile(d) and not os.path.islink(d) and digest(d) == s for d, s in expect["files"])
r["executable_runnable"] = os.path.isfile(expect["executable"]) and os.access(
    expect["executable"], os.X_OK)
r["runtime_closed"] = sorted(listing("/runtime") or []) == sorted(expect["runtime_entries"])
r["env_shape"] = (
    set(expect["env_keys"]) <= set(os.environ) and os.environ.get("PATH") == "/runtime"
    and os.environ.get("HOME") == "/home/v4" and os.environ.get("TMPDIR") == "/tmp")
r["pg_child_auth_denied"] = bool(expect["pg"]["addresses"]) and all(
    pg_denied(address, expect["pg"]["port"], expect["pg"]["dbname"], role)
    for address in expect["pg"]["addresses"] for role in expect["pg"]["roles"])
if expect["auth_destination"]:
    try:
        st = os.stat(expect["auth_destination"])
        r["codex_auth_mounted"] = (
            not os.path.islink(expect["auth_destination"]) and (st.st_mode & 0o777) == 0o400)
    except OSError:
        r["codex_auth_mounted"] = False
print(json.dumps(r, sort_keys=True))
"""
BWRAP_PROBE_KEYS = frozenset(
    {
        "pid_namespace",
        "parent_proc_invisible",
        "credentials_absent",
        "signing_absent",
        "home_isolated",
        "protected_state_absent",
        "pg_socket_absent",
        "general_tools_absent",
        "uid_unprivileged",
        "network_shared",
        "closure_files_verified",
        "executable_runnable",
        "runtime_closed",
        "env_shape",
        "pg_child_auth_denied",
    }
)
# Observed by the root parent from the UNINSTRUMENTED production closure.
NATIVE_STARTUP_FLAG = "native_startup"
BWRAP_ADAPTER_FLAGS = BWRAP_PROBE_KEYS | {NATIVE_STARTUP_FLAG}
INFORMATIONAL = frozenset({"network_shared"})


class QualificationRefused(RuntimeError):
    """Fixed code; no message text beyond the code."""


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _unit_from_cgroup(cgroup_text: str) -> str | None:
    for line in cgroup_text.splitlines():
        tail = line.split(":", 2)[-1].rstrip("/").rsplit("/", 1)[-1]
        if tail.endswith(".service"):
            return tail
    return None


def in_unit_identity(expected_unit: str, *, cgroup: str | None = None, environ=None) -> dict:
    """Bind the actual systemd unit: cgroup leaf, credential namespace, invocation."""
    environ = os.environ if environ is None else environ
    if cgroup is None:
        cgroup = Path("/proc/self/cgroup").read_text()
    unit = _unit_from_cgroup(cgroup)
    invocation = environ.get("INVOCATION_ID", "")
    credentials = environ.get("CREDENTIALS_DIRECTORY", "")
    if unit != expected_unit:
        raise QualificationRefused("not_in_expected_unit")
    if not HEX32.fullmatch(invocation):
        raise QualificationRefused("invocation_id_required")
    if credentials != f"/run/credentials/{expected_unit}":
        raise QualificationRefused("credential_namespace_mismatch")
    return {
        "unit": unit,
        "invocation_id": invocation,
        "uid_name": pwd.getpwuid(os.getuid()).pw_name,
        "credentials_directory": credentials,
    }


def _mode_private(path: Path) -> bool:
    return path.exists() and not path.is_symlink() and not path.stat().st_mode & 0o077


def probe_unit_hardening() -> dict:
    """Observable consequences of the staged drop-in inside the running unit."""
    status = Path("/proc/self/status").read_text()
    result = {
        "no_new_privs": "NoNewPrivs:\t1" in status,
        "protect_proc_invisible": not Path("/proc/1/status").exists(),
        "proc_subset_pid": not Path("/proc/cpuinfo").exists(),
        "protect_home": not any(
            Path(p).is_dir() and os.listdir(p) for p in ("/home", "/root") if os.access(p, os.R_OK)
        ),
        "protect_system_strict": not os.access("/etc", os.W_OK) and not os.access("/usr", os.W_OK),
        "unprivileged": os.getuid() != 0 and os.getgid() != 0,
    }
    try:
        mmap.mmap(-1, 4096, prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC).close()
        result["memory_deny_write_execute"] = False
    except (PermissionError, OSError):
        result["memory_deny_write_execute"] = True
    return result


def _release_binding() -> dict:
    """Verified installed release, profile and policy digests from inside the unit."""
    from hramatka.engine.v4_runtime_vendor import verify_installed

    manifest = verify_installed()
    from learn_ukrainian_v4_runtime import v4_trust_authority as trust
    from learn_ukrainian_v4_runtime.child_runtime import _verified_file, load_profile, profile_path
    from learn_ukrainian_v4_runtime.provenance import verify_current_identity

    identity = verify_current_identity()
    profile = load_profile()
    policy, policy_digest = trust.load_production_trust_policy()
    active = all(
        any(not key.get("revoked", False) for key in ring.values())
        for ring in policy["keyrings"].values()
    )
    adapters = sorted(profile["adapters"])
    bwrap_ok = False
    try:
        _verified_file(profile["bwrap"], profile["bwrap_sha256"]) if profile[
            "bwrap_sha256"
        ] else None
        bwrap_ok = bool(profile["bwrap_sha256"])
    except Exception:
        bwrap_ok = False
    return {
        "public_commit": identity["public_commit"],
        "wheel_sha256": next(iter(manifest["files"].values()))["sha256"],
        "package_manifest_sha256": identity["release_manifest_sha256"],
        "trust_policy_sha256": policy_digest,
        "trust_policy_active": active,
        "child_profile_sha256": _digest(profile_path().read_bytes()),
        "child_profile_adapters": adapters,
        "bwrap_binary_verified": bwrap_ok,
    }


def probe_api_credentials(credentials: Path) -> dict:
    """Parent-only signing succeeds; Sources namespace and modes are closed."""
    from learn_ukrainian_v4_runtime import v4_trust_authority as trust

    names = sorted(p.name for p in credentials.iterdir())
    signing = credentials / "v4-signing-keys"
    result = {
        "control_dsn_private": _mode_private(credentials / "v4-control-dsn"),
        "qualification_private": _mode_private(credentials / "v4-unit-qualification.json"),
        "signing_root_private": signing.is_dir() and not signing.is_symlink(),
        "provider_credential_count": sum(n.startswith("v4-provider-") for n in names),
        "sources_namespace_denied": not os.access(f"/run/credentials/{SOURCES_UNIT}", os.R_OK),
        "credential_names_sha256": _digest(_canonical(names)),
    }
    challenge = {}
    policy, _ = trust.load_production_trust_policy()
    # The release's role set must be exactly the fixed production roles.
    roles = tuple(trust.KEYRING_ROLES) if set(trust.KEYRING_ROLES) == set(SIGNING_ROLES) else ()
    for role in roles:
        try:
            private_hex, key_id = trust.load_production_signing_key(role)
            public_hex = trust.resolve_public_key(policy, role, key_id)
            signature = trust.sign(private_hex, CANARY_DOMAIN, CANARY_PAYLOAD)
            trust.verify(public_hex, CANARY_DOMAIN, CANARY_PAYLOAD, signature)
            challenge[role] = {"ok": True, "key_id_sha256": _digest(key_id.encode())}
        except Exception:
            challenge[role] = {"ok": False}
        finally:
            private_hex = None
    result["signing_challenge"] = challenge
    result["signing_challenge_ok"] = set(challenge) == set(SIGNING_ROLES) and all(
        v["ok"] for v in challenge.values()
    )
    return result


def _acl_grantees(conn, table: str) -> list[str]:
    rows = conn.execute(
        "SELECT COALESCE(pg_get_userbyid(a.grantee), 'PUBLIC') AS grantee FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(c.relacl) a "
        "WHERE n.nspname = 'public' AND c.relname = %s AND a.grantee <> c.relowner",
        (table,),
    ).fetchall()
    return sorted({row["grantee"] for row in rows})


def _memberships(conn, role: str) -> list[str]:
    """Every role the principal is a member of (inheriting or SET-only)."""
    rows = conn.execute(
        "SELECT m.rolname FROM pg_auth_members am JOIN pg_roles m ON m.oid = am.roleid "
        "JOIN pg_roles r ON r.oid = am.member WHERE r.rolname = %s",
        (role,),
    ).fetchall()
    return sorted(row["rolname"] for row in rows)


def probe_control_database(conn) -> dict:
    """Run as the control login: schema drift, role attributes, ownership and ACL closure."""
    from learn_ukrainian_v4_runtime.pg_schema import verify_pg_schema

    role = conn.execute(
        "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolinherit FROM pg_roles "
        "WHERE rolname = current_user"
    ).fetchone()
    control_members = _memberships(conn, CONTROL_ROLE)
    sources_members = _memberships(conn, SOURCES_ROLE)
    sources = conn.execute(
        "SELECT rolsuper, rolbypassrls, rolcanlogin, rolcreaterole, rolcreatedb FROM pg_roles "
        "WHERE rolname = %s",
        (SOURCES_ROLE,),
    ).fetchone()
    owners = {
        t: conn.execute(
            "SELECT tableowner FROM pg_tables WHERE schemaname='public' AND tablename=%s", (t,)
        ).fetchone()
        for t in V4_TABLES
    }
    owner_names = sorted({o["tableowner"] for o in owners.values() if o})
    function_owner = conn.execute(
        "SELECT pg_get_userbyid(proowner) AS owner FROM pg_proc WHERE oid = %s::regprocedure",
        (SOURCES_FUNCTION,),
    ).fetchone()
    # Privileged membership/ownership paths: neither restricted principal may
    # reach a table owner through USAGE (inherit) or MEMBER (SET ROLE).
    owner_paths = [
        conn.execute(
            "SELECT pg_has_role(%s, %s, 'USAGE') OR pg_has_role(%s, %s, 'MEMBER') AS ok",
            (p, o, p, o),
        ).fetchone()["ok"]
        for p in (CONTROL_ROLE, SOURCES_ROLE)
        for o in owner_names
    ]
    grantees = {t: _acl_grantees(conn, t) for t in V4_TABLES}
    sources_dml = any(
        conn.execute(
            "SELECT has_table_privilege(%s, %s, 'INSERT,UPDATE,DELETE')",
            (SOURCES_ROLE, "public." + t),
        ).fetchone()["has_table_privilege"]
        for t in V4_TABLES
    )
    function_exec = conn.execute(
        "SELECT has_function_privilege(%s, %s, 'EXECUTE') AS ok", (SOURCES_ROLE, SOURCES_FUNCTION)
    ).fetchone()["ok"]
    control_in_sources = conn.execute(
        "SELECT pg_has_role(current_user, %s, 'MEMBER') AS ok", (SOURCES_ROLE,)
    ).fetchone()["ok"]
    sources_in_control = conn.execute(
        "SELECT pg_has_role(%s, current_user, 'MEMBER') AS ok", (SOURCES_ROLE,)
    ).fetchone()["ok"]
    return {
        "schema_version_6": verify_pg_schema(conn) == 6,
        "control_not_superuser": not (
            role["rolsuper"] or role["rolbypassrls"] or role["rolcreaterole"] or role["rolcreatedb"]
        ),
        "control_memberships_absent": not control_members,
        "sources_role_scoped": sources is not None
        and not (
            sources["rolsuper"]
            or sources["rolbypassrls"]
            or sources["rolcreaterole"]
            or sources["rolcreatedb"]
        ),
        "sources_memberships_absent": not sources_members,
        "owner_not_v4_role": bool(owner_names)
        and all(o and o["tableowner"] not in (CONTROL_ROLE, SOURCES_ROLE) for o in owners.values()),
        "owner_membership_denied": bool(owner_paths) and not any(owner_paths),
        "function_owner_not_v4_role": function_owner is not None
        and function_owner["owner"] not in (CONTROL_ROLE, SOURCES_ROLE),
        "grantees_closed": all(g == [CONTROL_ROLE] for g in grantees.values()),
        "public_grant_absent": all("PUBLIC" not in g for g in grantees.values()),
        "sources_table_dml_denied": not sources_dml,
        "sources_function_execute": bool(function_exec),
        "roles_disjoint": not control_in_sources and not sources_in_control,
    }


def _set_role_denied(conn, psycopg, role: str) -> bool:
    try:
        conn.execute(f'SET ROLE "{role}"')
        denied = False
    except psycopg.errors.InsufficientPrivilege:
        denied = True
    except psycopg.Error:
        denied = False
    conn.rollback()
    return denied


def probe_sources_database(dsn: str) -> dict:
    """Run as the Sources login: only the stored function; writes, control/owner roles denied."""
    import psycopg
    from psycopg.rows import dict_row

    result = {}
    with psycopg.connect(dsn, autocommit=False, row_factory=dict_row) as conn:
        result["sources_login"] = (
            conn.execute("SELECT current_user AS u").fetchone()["u"] == SOURCES_ROLE
        )
        attributes = conn.execute(
            "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb FROM pg_roles "
            "WHERE rolname = current_user"
        ).fetchone()
        result["role_attributes_scoped"] = attributes is not None and not any(attributes.values())
        result["memberships_absent"] = not _memberships(conn, SOURCES_ROLE)
        result["function_execute"] = conn.execute(
            "SELECT has_function_privilege(%s, 'EXECUTE') AS ok", (SOURCES_FUNCTION,)
        ).fetchone()["ok"]
        result["table_privileges_absent"] = not any(
            conn.execute(
                "SELECT has_table_privilege(%s, 'SELECT,INSERT,UPDATE,DELETE') AS ok",
                ("public." + t,),
            ).fetchone()["ok"]
            for t in V4_TABLES
        )
        owners = sorted(
            {
                row["tableowner"]
                for t in V4_TABLES
                for row in conn.execute(
                    "SELECT tableowner FROM pg_tables WHERE schemaname='public' AND tablename=%s",
                    (t,),
                ).fetchall()
            }
        )
        try:
            conn.execute(
                "INSERT INTO public.v4_sources_invocations "
                "SELECT * FROM public.v4_sources_invocations LIMIT 0"
            )
            result["direct_insert_denied"] = False
        except psycopg.errors.InsufficientPrivilege:
            result["direct_insert_denied"] = True
        except psycopg.Error:
            result["direct_insert_denied"] = False
        conn.rollback()
        result["control_role_denied"] = _set_role_denied(conn, psycopg, CONTROL_ROLE)
        # SET ROLE to any table owner (non-inheriting membership) is the ownership escape.
        result["owner_role_denied"] = bool(owners) and all(
            _set_role_denied(conn, psycopg, owner) for owner in owners
        )
    return result


def probe_sources_credentials(credentials: Path) -> dict:
    from learn_ukrainian_v4_runtime import v4_trust_authority as trust

    names = sorted(p.name for p in credentials.iterdir())
    return {
        "sources_dsn_private": _mode_private(credentials / "v4-sources-dsn"),
        "api_credentials_absent": not any(
            n in names or n.startswith("v4-provider-")
            for n in ("v4-control-dsn", "v4-signing-keys", "v4-unit-qualification.json")
        )
        and not any(n.startswith("v4-provider-") for n in names),
        "api_namespace_denied": not os.access(f"/run/credentials/{API_UNIT}", os.R_OK),
        "signing_root_denied": not os.access(str(trust.HRAMATKA_SIGNING_KEY_ROOT), os.R_OK),
        "credential_names_sha256": _digest(_canonical(names)),
    }


# ----- bwrap: complete production plan plus a disjoint probe interpreter ----


def _elf_interpreter(binary: Path) -> Path:
    """PT_INTERP (dynamic loader) of a 64-bit little-endian ELF executable."""
    try:
        with binary.open("rb") as handle:
            header = handle.read(64)
            if len(header) != 64 or header[:4] != b"\x7fELF" or header[4:6] != b"\x02\x01":
                raise QualificationRefused("probe_interpreter_unrelocatable")
            phoff = int.from_bytes(header[32:40], "little")
            phentsize = int.from_bytes(header[54:56], "little")
            phnum = int.from_bytes(header[56:58], "little")
            for index in range(phnum):
                handle.seek(phoff + index * phentsize)
                entry = handle.read(56)
                if len(entry) != 56:
                    break
                if int.from_bytes(entry[0:4], "little") == 3:  # PT_INTERP
                    handle.seek(int.from_bytes(entry[8:16], "little"))
                    raw = handle.read(int.from_bytes(entry[32:40], "little"))
                    loader = Path(raw.split(b"\0", 1)[0].decode())
                    if loader.is_absolute() and loader.is_file():
                        return loader
                    break
    except OSError:
        pass
    raise QualificationRefused("probe_interpreter_unrelocatable")


def _probe_interpreter(
    python: Path | None = None, base: Path | None = None
) -> tuple[list[str], list[str]]:
    """Relocate the probe interpreter, its loader and host libraries under /canary ONLY.

    Nothing is mounted at a production path: the production closure decides
    alone whether the child executable can start (see ``_native_startup``).
    The interpreter is started through its own dynamic loader with an explicit
    library path, so an interpreter whose libpython lives at an absolute
    original RUNPATH (the CI tool interpreter) resolves it under
    ``/canary/base/lib`` instead of failing with a missing loader library.
    Returns the bwrap mount arguments and the interpreter argv prefix.
    """
    python = Path(sys.executable if python is None else python).resolve()
    base = Path(sys.base_prefix if base is None else base).resolve()
    if not python.is_relative_to(base) or str(base) == "/" or not python.is_file():
        raise QualificationRefused("probe_interpreter_unrelocatable")
    loader = _elf_interpreter(python).resolve()
    multiarch = loader.parent.name if loader.parent.name not in ("lib", "lib64") else ""
    candidates = [loader.parent]
    for lib in ("/lib", "/lib64", "/usr/lib", "/usr/lib64"):
        candidates.append(Path(lib))
        if multiarch:
            candidates.append(Path(lib) / multiarch)
    library_dirs: list[Path] = []
    for directory in candidates:
        if directory.is_dir():
            resolved = directory.resolve()
            if resolved != Path("/") and resolved not in library_dirs:
                library_dirs.append(resolved)
    roots: list[Path] = []
    for directory in sorted(library_dirs, key=lambda p: len(p.parts)):
        if not any(directory == root or directory.is_relative_to(root) for root in roots):
            roots.append(directory)
    if not any(loader.is_relative_to(root) for root in roots):
        raise QualificationRefused("probe_interpreter_unrelocatable")
    mounts = ["--dir", PROBE_MOUNT_ROOT, "--ro-bind", str(base), PROBE_BASE]
    for root in roots:
        mounts += ["--ro-bind", str(root), PROBE_HOST_ROOT + str(root)]
    library_path = [PROBE_BASE + "/lib", *(PROBE_HOST_ROOT + str(d) for d in library_dirs)]
    interpreter = [
        PROBE_HOST_ROOT + str(loader),
        "--library-path",
        ":".join(library_path),
        PROBE_BASE + "/" + str(python.relative_to(base)),
    ]
    return mounts, interpreter


def _canary_credential(harness: str, mode: str):
    """Fixed, non-secret credential of the exact production shape for planning only."""
    from learn_ukrainian_v4_runtime.child_runtime import ProviderCredential

    if mode == "api_key":
        return "canary-fixed-provider-value"
    expires = int(time.time()) + 3600
    if harness == "claude":
        return ProviderCredential("claude", "subscription", "canary-fixed-provider-value", expires)

    def b64(value) -> str:
        return base64.urlsafe_b64encode(_canonical(value)).rstrip(b"=").decode()

    token = ".".join(
        (b64({"alg": "RS256", "typ": "JWT"}), b64({"exp": expires}), b64({"canary": "fixed"}))
    )
    auth = {
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": token,
            "access_token": token,
            "refresh_token": "canary-fixed",
            "account_id": "canary-fixed",
        },
    }
    return ProviderCredential("codex", "subscription", json.dumps(auth), expires)


def bwrap_plans(profile: dict) -> dict[str, dict]:
    """Complete production bwrap prefix and environment per profile adapter.

    The prefix is everything the public plan passes to bwrap before the
    adapter argv: every fixed namespace flag, every pinned closure mount and
    the codex home. Nothing is truncated or substituted. Refuses when any
    adapter of the profile cannot be planned.
    """
    from learn_ukrainian_v4_runtime.child_runtime import (
        CODEX_AUTH_DESTINATION,
        _plan,
        credential_mode,
    )
    from learn_ukrainian_v4_runtime.operation_auth import OperationRefused

    plans = {}
    for harness in sorted(profile["adapters"]):
        adapter = profile["adapters"][harness]
        if harness not in HARNESSES or not isinstance(adapter, dict) or not adapter.get("models"):
            raise QualificationRefused("child_plan_unavailable")
        claim = {
            "binding": {
                "expected_harness": harness,
                "expected_seat_or_model": adapter["models"][0],
                "role": "author",
            },
            "capability_token": "canary-fixed-capability",
        }
        try:
            mode = credential_mode(profile, harness)
            credential = _canary_credential(harness, mode)
            cmd, env = _plan(profile, claim, credential)
        except OperationRefused:
            raise QualificationRefused("child_plan_unavailable") from None
        prefix = cmd[: cmd.index("--chdir")]
        destinations = [(entry["destination"], entry["sha256"]) for entry in adapter["files"]]
        auth_bytes = None
        if (harness, mode) == ("codex", "subscription"):
            auth_bytes = credential.value.encode()
        plans[harness] = {
            "prefix": prefix,
            "env": env,
            "files": destinations,
            "executable": adapter["executable"],
            "runtime_entries": sorted(
                {Path(d).parts[2] for d, _ in destinations if Path(d).parts[1] == "runtime"}
            ),
            "auth_destination": CODEX_AUTH_DESTINATION if auth_bytes is not None else None,
            "auth_bytes": auth_bytes,
        }
    if not plans:
        raise QualificationRefused("child_plan_unavailable")
    return plans


def bwrap_prefix(profile: dict) -> list[str]:
    """Complete production prefix of the first planned adapter; refuses an unqualified profile."""
    return next(iter(bwrap_plans(profile).values()))["prefix"]


def _single(value) -> str:
    """One libpq list entry; a comma list (multiple endpoints) is ambiguous."""
    text = str(value or "")
    if "," in text:
        raise QualificationRefused("pg_target_ambiguous")
    return text


def pg_target(dsn: str) -> dict:
    """Numeric TCP addresses of the actual configured server; never the password or DSN.

    libpq semantics: ``hostaddr`` (numeric) is the network address when set;
    otherwise the single ``host`` name is resolved here, in the unit, to every
    address libpq would try. A socket directory, missing host, service file,
    or comma lists are unsupported/ambiguous and refuse rather than guess.
    """
    import ipaddress
    import socket

    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(dsn)
    if info.get("service") or info.get("servicefile"):
        raise QualificationRefused("pg_target_unsupported")
    hostaddr = _single(info.get("hostaddr"))
    host = _single(info.get("host"))
    port_text = _single(info.get("port")) or "5432"
    if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        raise QualificationRefused("pg_target_unsupported")
    port = int(port_text)
    if hostaddr:
        try:
            addresses = [str(ipaddress.ip_address(hostaddr))]
        except ValueError:
            raise QualificationRefused("pg_target_unsupported") from None
    elif not host or host.startswith("/") or host.startswith("@"):
        raise QualificationRefused("pg_target_unsupported")
    else:
        try:
            addresses = [str(ipaddress.ip_address(host))]
        except ValueError:
            try:
                resolved = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            except OSError:
                resolved = []
            addresses = sorted({str(entry[4][0]) for entry in resolved})
            if not addresses:
                raise QualificationRefused("pg_target_unresolved") from None
    dbname = str(info.get("dbname") or info.get("user") or CONTROL_ROLE)
    return {
        "addresses": addresses,
        "port": port,
        "dbname": dbname,
        "roles": [CONTROL_ROLE, SOURCES_ROLE],
    }


def _auth_transport(plan: dict, cmd: list[str]) -> tuple[int | None, tuple[int, ...]]:
    """Exact Codex auth transport of the production plan (sealed memfd, 0400 bind).

    The verified runtime's own sealed-memfd constructor is used, so the shape is
    the production one and not a re-implementation. The caller closes the fd.
    """
    if plan["auth_bytes"] is None:
        return None, ()
    from learn_ukrainian_v4_runtime.child_runtime import _sealed_auth_fd
    from learn_ukrainian_v4_runtime.operation_auth import OperationRefused

    try:
        auth_fd = _sealed_auth_fd(plan["auth_bytes"])
    except OperationRefused:
        raise QualificationRefused("auth_transport_unavailable") from None
    cmd += ["--perms", "0400", "--ro-bind-data", str(auth_fd), plan["auth_destination"]]
    return auth_fd, (auth_fd,)


def _native_startup(plan: dict, *, timeout: float) -> bool:
    """Start the adapter executable inside the UNINSTRUMENTED production closure.

    Exactly the production prefix, auth transport, chdir and environment; no
    probe mount, interpreter or variable is added. A fixed, provider-free
    ``--version`` must exit 0. A missing loader or library therefore refuses
    here, before any instrumented diagnostics run. Output is never printed.
    """
    cmd = [*plan["prefix"]]
    auth_fd = None
    try:
        auth_fd, pass_fds = _auth_transport(plan, cmd)
        cmd += ["--chdir", "/work", "--", plan["executable"], *NATIVE_STARTUP_ARGV]
        try:
            completed = subprocess.run(
                cmd,
                env=dict(plan["env"]),
                capture_output=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                pass_fds=pass_fds,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
    finally:
        if auth_fd is not None:
            os.close(auth_fd)
    return completed.returncode == 0


def _run_probe(plan: dict, target: dict, *, timeout: float) -> dict | None:
    mounts, interpreter = _probe_interpreter()
    probe = BWRAP_PROBE
    expect = {
        "tools": list(FORBIDDEN_TOOLS),
        "files": plan["files"],
        "executable": plan["executable"],
        "runtime_entries": plan["runtime_entries"],
        "env_keys": sorted(plan["env"]),
        "pg": target,
        "auth_destination": plan["auth_destination"],
    }
    cmd = [*plan["prefix"], *mounts]
    env = {
        **plan["env"],
        "PYTHONHOME": PROBE_BASE,
        "PYTHONDONTWRITEBYTECODE": "1",
        "HRAMATKA_V4_CANARY_EXPECT": json.dumps(expect, sort_keys=True),
    }
    auth_fd = None
    try:
        auth_fd, pass_fds = _auth_transport(plan, cmd)
        cmd += ["--chdir", "/work", "--", *interpreter, "-s", "-B", "-c", probe]
        try:
            completed = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                pass_fds=pass_fds,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
    finally:
        if auth_fd is not None:
            os.close(auth_fd)
    if completed.returncode != 0:
        return None
    try:
        observed = json.loads(completed.stdout.decode().strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    expected_keys = BWRAP_PROBE_KEYS | (
        {"codex_auth_mounted"} if plan["auth_destination"] else set()
    )
    if (
        not isinstance(observed, dict)
        or set(observed) != expected_keys
        or any(type(v) is not bool for v in observed.values())
    ):
        return None
    return observed


def probe_bwrap(profile: dict, dsn: str, *, timeout: float = 60.0) -> dict:
    """Exercise every adapter's complete production plan; incomplete evidence never passes.

    Per adapter, first the uninstrumented native startup of the production
    closure must succeed; only then the relocated probe runs. Either failure
    stops with ``probe_completed`` False and a fixed code.
    """
    plans = bwrap_plans(profile)
    target = pg_target(dsn)
    mounts, interpreter = _probe_interpreter()
    result = {
        "probe_completed": False,
        "probe_sha256": _digest(BWRAP_PROBE.encode()),
        "probe_mounts_sha256": _digest(_canonical({"mounts": mounts, "interpreter": interpreter})),
        "sandbox_argv_prefix_sha256": _digest(
            _canonical({h: p["prefix"] for h, p in plans.items()})
        ),
        "adapters_probed_complete": False,
        "adapters": {},
    }
    for harness, plan in plans.items():
        if not _native_startup(plan, timeout=timeout):
            result["code"] = f"native_startup_failed:{harness}"
            return result
        observed = _run_probe(plan, target, timeout=timeout)
        if observed is None:
            result["code"] = f"probe_incomplete:{harness}"
            return result
        observed[NATIVE_STARTUP_FLAG] = True
        result["adapters"][harness] = observed
    result["probe_completed"] = True
    result["adapters_probed_complete"] = set(result["adapters"]) == set(profile["adapters"])
    return result


# ----- strict evidence schemas --------------------------------------------


def _hex(pattern: re.Pattern):
    return lambda v: isinstance(v, str) and pattern.fullmatch(v) is not None


def _count(v) -> bool:
    return type(v) is int and v >= 0


def _signing_challenge(v) -> bool:
    """Exactly the fixed production role set; a missing or extra role refuses."""
    return (
        isinstance(v, dict)
        and set(v) == set(SIGNING_ROLES)
        and all(
            isinstance(r, dict)
            and type(r.get("ok")) is bool
            and set(r) == ({"ok", "key_id_sha256"} if r["ok"] else {"ok"})
            and (not r["ok"] or _hex(HEX64)(r["key_id_sha256"]))
            for r in v.values()
        )
    )


UNIT_HARDENING_FLAGS = frozenset(
    {
        "no_new_privs",
        "protect_proc_invisible",
        "proc_subset_pid",
        "protect_home",
        "protect_system_strict",
        "unprivileged",
        "memory_deny_write_execute",
    }
)
PROBE_SCHEMAS = {
    ("api", "unit_hardening"): (UNIT_HARDENING_FLAGS, {}),
    ("sources", "unit_hardening"): (UNIT_HARDENING_FLAGS, {}),
    ("api", "credential_separation"): (
        frozenset(
            {
                "control_dsn_private",
                "qualification_private",
                "signing_root_private",
                "sources_namespace_denied",
                "signing_challenge_ok",
            }
        ),
        {
            "provider_credential_count": _count,
            "credential_names_sha256": _hex(HEX64),
            "signing_challenge": _signing_challenge,
        },
    ),
    ("sources", "credential_separation"): (
        frozenset(
            {
                "sources_dsn_private",
                "api_credentials_absent",
                "api_namespace_denied",
                "signing_root_denied",
            }
        ),
        {"credential_names_sha256": _hex(HEX64)},
    ),
    ("api", "scoped_database_roles"): (
        frozenset(
            {
                "schema_version_6",
                "control_not_superuser",
                "control_memberships_absent",
                "sources_role_scoped",
                "sources_memberships_absent",
                "owner_not_v4_role",
                "owner_membership_denied",
                "function_owner_not_v4_role",
                "grantees_closed",
                "public_grant_absent",
                "sources_table_dml_denied",
                "sources_function_execute",
                "roles_disjoint",
            }
        ),
        {},
    ),
    ("sources", "scoped_database_roles"): (
        frozenset(
            {
                "sources_login",
                "role_attributes_scoped",
                "memberships_absent",
                "function_execute",
                "table_privileges_absent",
                "direct_insert_denied",
                "control_role_denied",
                "owner_role_denied",
            }
        ),
        {},
    ),
}


def _flags_pass(probe: dict, flags: frozenset, fields: dict) -> bool:
    if not isinstance(probe, dict) or set(probe) != flags | set(fields):
        return False
    if any(type(probe[k]) is not bool for k in flags):
        return False
    if any(not check(probe[k]) for k, check in fields.items()):
        return False
    return all(probe[k] for k in flags if k not in INFORMATIONAL)


def _bwrap_pass(probe: dict) -> bool:
    fields = {
        "probe_completed": lambda v: v is True,
        "probe_sha256": _hex(HEX64),
        "probe_mounts_sha256": _hex(HEX64),
        "sandbox_argv_prefix_sha256": _hex(HEX64),
        "adapters_probed_complete": lambda v: v is True,
        "adapters": lambda v: isinstance(v, dict) and bool(v),
    }
    if not _flags_pass(probe, frozenset(), fields):
        return False
    for harness, observed in probe["adapters"].items():
        if harness not in HARNESSES or not isinstance(observed, dict):
            return False
        flags = BWRAP_ADAPTER_FLAGS | (
            {"codex_auth_mounted"} if "codex_auth_mounted" in observed else set()
        )
        if not _flags_pass(observed, flags, {}):
            return False
    return True


def canary_pass(name: str, probe: dict, *, kind: str = "api") -> bool:
    """Exact schema: every required flag present, boolean and True; no unknown fields."""
    if name == "bwrap":
        return kind == "api" and _bwrap_pass(probe)
    schema = PROBE_SCHEMAS.get((kind, name))
    if schema is None or not _flags_pass(probe, *schema):
        return False
    if (kind, name) == ("api", "credential_separation"):
        # The summary must equal the complete challenge evidence, never override it.
        return probe["signing_challenge_ok"] is all(
            r["ok"] for r in probe["signing_challenge"].values()
        )
    return True


def run_canary(kind: str) -> dict:
    """In-unit entrypoint; prints one machine line, never raises key material."""
    unit = UNITS[kind]
    report = {"schema": CANARY_SCHEMA, "kind": kind, "at": datetime.now(UTC).isoformat()}
    try:
        report["identity"] = in_unit_identity(unit)
        # The switches the unit actually started with (Environment, every
        # EnvironmentFile and the manager environment applied): both must be OFF.
        report["switches"] = {switch: os.environ.get(switch) for switch in SWITCHES}
        if not switches_off(report["switches"]):
            raise QualificationRefused("unit_switch_not_off")
        credentials = Path(report["identity"]["credentials_directory"])
        report["release"] = _release_binding()
        if kind == "api":
            hardening = probe_unit_hardening()
            separation = probe_api_credentials(credentials)
            from learn_ukrainian_v4_runtime.child_runtime import load_profile
            from learn_ukrainian_v4_runtime.scoped_store import ScopedAuthorityStore

            store = ScopedAuthorityStore()
            try:
                database = probe_control_database(store.connection)
            finally:
                store.close()
            dsn_path = credentials / "v4-control-dsn"
            if not _mode_private(dsn_path):
                raise QualificationRefused("control_credential_required")
            try:
                bwrap = probe_bwrap(load_profile(), dsn_path.read_text().strip())
            except QualificationRefused as exc:
                bwrap = {"probe_completed": False, "code": str(exc)}
            report["probes"] = {
                "unit_hardening": hardening,
                "credential_separation": separation,
                "scoped_database_roles": database,
                "bwrap": bwrap,
            }
        else:
            dsn_path = credentials / "v4-sources-dsn"
            if not _mode_private(dsn_path):
                raise QualificationRefused("sources_credential_required")
            report["probes"] = {
                "unit_hardening": probe_unit_hardening(),
                "credential_separation": probe_sources_credentials(credentials),
                "scoped_database_roles": probe_sources_database(dsn_path.read_text().strip()),
            }
        report["passed"] = {
            name: canary_pass(name, probe, kind=kind) for name, probe in report["probes"].items()
        }
        report["refused"] = None
    except QualificationRefused as exc:
        report["refused"] = str(exc)
    except Exception as exc:  # fixed class name only; exception text may carry a DSN
        report["refused"] = "canary_error:" + type(exc).__name__
    return report


RELEASE_SCHEMA = {
    "public_commit": _hex(HEX40),
    "wheel_sha256": _hex(HEX64),
    "package_manifest_sha256": _hex(HEX64),
    "trust_policy_sha256": _hex(HEX64),
    "child_profile_sha256": _hex(HEX64),
    "trust_policy_active": lambda v: type(v) is bool,
    "bwrap_binary_verified": lambda v: type(v) is bool,
    "child_profile_adapters": lambda v: (
        isinstance(v, list)
        and all(isinstance(a, str) and a in HARNESSES for a in v)
        and len(set(v)) == len(v)
    ),
}


def _validate_report(kind: str, report: dict, unit: str) -> None:
    if (
        not isinstance(report, dict)
        or report.get("schema") != CANARY_SCHEMA
        or report.get("kind") != kind
    ):
        raise QualificationRefused(f"{kind}_canary_schema")
    identity = report.get("identity")
    if (
        report.get("refused") is not None
        or not isinstance(identity, dict)
        or identity.get("unit") != unit
    ):
        raise QualificationRefused(f"{kind}_canary_refused")
    invocation = identity.get("invocation_id")
    if not isinstance(invocation, str) or not HEX32.fullmatch(invocation):
        raise QualificationRefused(f"{kind}_canary_invocation_invalid")
    switches = report.get("switches")
    if (
        not isinstance(switches, dict)
        or set(switches) != set(SWITCHES)
        or not switches_off(switches)
    ):
        raise QualificationRefused(f"{kind}_unit_switch_not_off")
    release = report.get("release")
    if (
        not isinstance(release, dict)
        or set(release) != set(RELEASE_SCHEMA)
        or not all(check(release[k]) for k, check in RELEASE_SCHEMA.items())
    ):
        raise QualificationRefused(f"{kind}_release_schema")
    probes = report.get("probes")
    expected = set(CANARIES) if kind == "api" else set(CANARIES) - {"bwrap"}
    if not isinstance(probes, dict) or set(probes) != expected:
        raise QualificationRefused(f"{kind}_probes_incomplete")


def compose_qualification(api: dict, sources: dict, *, unit_properties: dict) -> dict:
    """Combine two in-unit canaries into the readiness credential; refuse gaps.

    Every flag is recomputed from the complete probe evidence under the fixed
    schemas. The canaries' own ``passed`` summaries are never consulted.
    """
    for kind, report, unit in (("api", api, API_UNIT), ("sources", sources, SOURCES_UNIT)):
        _validate_report(kind, report, unit)
    if api["release"] != sources["release"]:
        raise QualificationRefused("release_mismatch_between_units")
    release = api["release"]
    if not release["trust_policy_active"]:
        raise QualificationRefused("trust_policy_empty")
    if not release["child_profile_adapters"] or not release["bwrap_binary_verified"]:
        raise QualificationRefused("child_profile_unqualified")
    canaries = {}
    for name in CANARIES:
        if not canary_pass(name, api["probes"][name], kind="api"):
            raise QualificationRefused(f"canary_failed:{name}")
        if name != "bwrap" and not canary_pass(name, sources["probes"][name], kind="sources"):
            raise QualificationRefused(f"canary_failed:{name}")
        canaries[name] = True
    if set(api["probes"]["bwrap"]["adapters"]) != set(release["child_profile_adapters"]):
        raise QualificationRefused("canary_failed:bwrap")
    return {
        "schema": SCHEMA,
        "unit": API_UNIT,
        "sources_unit": SOURCES_UNIT,
        "package_manifest_sha256": release["package_manifest_sha256"],
        "trust_policy_sha256": release["trust_policy_sha256"],
        "child_profile_sha256": release["child_profile_sha256"],
        "public_commit": release["public_commit"],
        "wheel_sha256": release["wheel_sha256"],
        "canaries": canaries,
        "api_invocation_id": api["identity"]["invocation_id"],
        "sources_invocation_id": sources["identity"]["invocation_id"],
        "unit_configuration_sha256": _digest(_canonical(unit_properties)),
        "probe_digests": {
            "api": _digest(_canonical(api["probes"])),
            "sources": _digest(_canonical(sources["probes"])),
        },
        "qualified_at": datetime.now(UTC).isoformat(),
    }


def pending_qualification() -> dict:
    """Root-owned placeholder so the credential exists before any canary passes."""
    return {
        "schema": SCHEMA,
        "unit": API_UNIT,
        "sources_unit": SOURCES_UNIT,
        "canaries": {n: False for n in CANARIES},
        "status": "pending",
    }


# ----- root staging ---------------------------------------------------------

UNIT_PROPERTIES = (
    "Id",
    "FragmentPath",
    "DropInPaths",
    "User",
    "Group",
    "ExecStart",
    "LoadCredential",
    "PrivateMounts",
    "ProtectProc",
    "ProcSubset",
    "NoNewPrivileges",
    "ProtectHome",
    "ProtectSystem",
    "SystemCallFilter",
    "InaccessiblePaths",
    "MemoryDenyWriteExecute",
    "TimeoutStopUSec",
    "Environment",
    "EnvironmentFiles",
)
_TIMESPAN_UNITS = {
    "y": 31557600.0,
    "year": 31557600.0,
    "years": 31557600.0,
    "month": 2629800.0,
    "months": 2629800.0,
    "w": 604800.0,
    "week": 604800.0,
    "weeks": 604800.0,
    "d": 86400.0,
    "day": 86400.0,
    "days": 86400.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "m": 60.0,
    "min": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "s": 1.0,
    "sec": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "ms": 0.001,
    "msec": 0.001,
    "us": 0.000001,
    "usec": 0.000001,
    "µs": 0.000001,
}


def _systemctl(*args: str, systemctl: str = "systemctl", timeout: float = COMMAND_TIMEOUT) -> str:
    return subprocess.run(
        [systemctl, *args], check=True, capture_output=True, text=True, timeout=timeout
    ).stdout


def _environment_values(assignment: str) -> dict[str, str]:
    values = {}
    for item in shlex.split(assignment):
        if "=" in item:
            key, value = item.split("=", 1)
            values[key] = value
    return values


def dropin_environment(text: str) -> dict[str, str]:
    """Effective environment values a unit file contributes.

    ``Environment=``: comments ignored, the last assignment wins, an empty
    assignment resets. ``EnvironmentFile=`` lines (optional ``-`` prefix, empty
    resets the list) are then applied in order with systemd precedence; a
    specifier or relative path is ambiguous here and refuses.
    """
    values: dict[str, str] = {}
    files: list[tuple[Path, bool]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("Environment="):
            assignment = line[len("Environment=") :].strip()
            if not assignment:
                values.clear()
                continue
            values.update(_environment_values(assignment))
        elif line.startswith("EnvironmentFile="):
            spec = line[len("EnvironmentFile=") :].strip()
            if not spec:
                files.clear()
                continue
            optional = spec.startswith("-")
            path = spec.lstrip("-")
            if "%" in path or not path.startswith("/"):
                raise QualificationRefused("envfile_property_unparseable")
            files.append((Path(path), optional))
    _apply_environment_files(values, files)
    return values


def _apply_environment_files(
    values: dict[str, str], entries: list[tuple[Path, bool]]
) -> list[Path]:
    """Read (never execute or source) each file in order; later files win."""
    applied = []
    for path, optional in entries:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            if optional:
                continue
            raise QualificationRefused("envfile_missing") from None
        except OSError:
            raise QualificationRefused("envfile_unreadable") from None
        values.update(environment_file_values(raw))
        applied.append(path)
    return applied


def switches_off(values: dict[str, str]) -> bool:
    """Both switches effectively present and exactly ``0``."""
    return all(values.get(switch) == "0" for switch in SWITCHES)


def environment_file_values(raw: bytes) -> dict[str, str]:
    """Assignments of a systemd ``EnvironmentFile=`` under a strict, refusing grammar.

    Supported exactly as systemd.exec(5) reads them: UTF-8, blank and ``#``/``;``
    comment lines ignored, lines without ``=`` ignored, ``KEY=VALUE`` with the
    last assignment winning and ``KEY=`` resetting to empty, single- or
    double-quoted single-line values. Anything the grammar does not cover
    unambiguously (escapes, multi-line quotes, text after a closing quote,
    quote or comment characters inside unquoted values, invalid names, NUL,
    BOM) refuses instead of guessing. Values are never logged.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise QualificationRefused("envfile_unparseable") from None
    if "\0" in text or text.startswith("\ufeff"):
        raise QualificationRefused("envfile_unparseable")
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not ENV_KEY.fullmatch(key):
            raise QualificationRefused("envfile_unparseable")
        if value[:1] in ("'", '"'):
            quote = value[0]
            inner = value[1:-1]
            if len(value) < 2 or value[-1] != quote or quote in inner or "\\" in inner:
                raise QualificationRefused("envfile_unparseable")
            value = inner
        elif any(ch in value for ch in "\\\"'#;"):
            raise QualificationRefused("envfile_unparseable")
        values[key] = value
    return values


def _environment_file_entries(text: str) -> list[tuple[Path, bool]]:
    """``EnvironmentFiles=`` property lines: ``<path> (ignore_errors=yes|no)``, in order."""
    entries = []
    for line in text.splitlines():
        if not line.startswith("EnvironmentFiles="):
            continue
        value = line[len("EnvironmentFiles=") :]
        if not value:
            continue
        match = re.fullmatch(r"(/.+) \(ignore_errors=(yes|no)\)", value)
        if match is None:
            raise QualificationRefused("envfile_property_unparseable")
        entries.append((Path(match.group(1)), match.group(2) == "yes"))
    return entries


def _effective_environment(show_output: str) -> tuple[dict[str, str], list[Path]]:
    """systemd precedence: ``Environment=`` first, then each EnvironmentFile in order.

    Later files override earlier ones and every file overrides ``Environment=``.
    Files are read, never executed or sourced; only parsed assignments are kept.
    """
    values: dict[str, str] = {}
    for line in show_output.splitlines():
        if line.startswith("Environment="):
            values.update(_environment_values(line[len("Environment=") :]))
    files = _apply_environment_files(values, _environment_file_entries(show_output))
    return values, files


def effective_switches(unit: str, *, systemctl: str = "systemctl") -> dict[str, str | None]:
    """Effective unit switches as systemd will start the unit: Environment + EnvironmentFile."""
    out = _systemctl(
        "show", unit, "--property=Environment", "--property=EnvironmentFiles", systemctl=systemctl
    )
    values, _ = _effective_environment(out)
    return {switch: values.get(switch) for switch in SWITCHES}


def timespan_seconds(text: str) -> float | None:
    """systemd human-readable time span; ``infinity``/unparseable is None."""
    text = text.strip()
    if (
        not text
        or text == "infinity"
        or not re.fullmatch(r"(?:\s*[0-9]+(?:\.[0-9]+)?\s*[a-zµ]*)+\s*", text)
    ):
        return None
    total = 0.0
    for number, unit in re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*([a-zµ]*)", text):
        if unit and unit not in _TIMESPAN_UNITS:
            return None
        total += float(number) * _TIMESPAN_UNITS[unit or "s"]
    return total


def unit_properties(unit: str, *, systemctl: str = "systemctl") -> dict:
    out = _systemctl(
        "show", unit, *(f"--property={p}" for p in UNIT_PROPERTIES), systemctl=systemctl
    )
    props = dict(
        line.split("=", 1)
        for line in out.splitlines()
        if "=" in line and not line.startswith("EnvironmentFiles=")
    )
    # Environment/LoadCredential may reference root-owned source paths; keep names only,
    # plus the effective values of the two switches (Environment= and every
    # EnvironmentFile= applied in systemd order) which must read OFF.
    values, files = _effective_environment(out)
    props["LoadCredential"] = sorted(
        item.split(":", 1)[0] for item in props.get("LoadCredential", "").split() if item
    )
    props["Environment"] = sorted(values)
    props["EnvironmentFiles"] = [str(path) for path in files]
    props["EffectiveSwitches"] = {switch: values.get(switch) for switch in SWITCHES}
    return props


def preflight(
    *,
    dropin_dirs: dict[str, Path],
    qualification_file: Path,
    python: Path,
    release_root: Path,
    systemctl: str = "systemctl",
    require_root: bool = True,
) -> dict:
    """Explicit prerequisite report; each failure is a fixed code, never a guess."""
    problems = []
    if require_root and os.geteuid() != 0:
        problems.append("root_required")
    for kind, directory in dropin_dirs.items():
        staged = directory / STAGED_DROPINS[kind]
        if not staged.is_file():
            problems.append(f"{kind}_dropin_missing")
        else:
            text = staged.read_text()
            if "<" in text or ">" in text:
                problems.append(f"{kind}_dropin_placeholder")
            try:
                staged_values = dropin_environment(text)
            except QualificationRefused as exc:
                staged_values = {}
                problems.append(f"{kind}_{exc}")
            if not switches_off(staged_values):
                problems.append(f"{kind}_switch_not_off")
            if (directory / CANARY_DROPIN).exists():
                problems.append(f"{kind}_canary_dropin_present")
    if not python.is_file() or python.is_symlink() and not python.resolve().is_file():
        problems.append("unit_python_missing")
    if not (release_root / "hramatka" / "engine" / "v4_runtime_vendor.py").is_file():
        problems.append("release_root_missing_private_engine")
    parent = qualification_file.parent
    if not parent.is_dir() or parent.stat().st_mode & 0o077:
        problems.append("qualification_directory_not_private")
    if qualification_file.exists() and (
        qualification_file.is_symlink() or qualification_file.stat().st_mode & 0o077
    ):
        problems.append("qualification_file_not_private")
    for kind, unit in UNITS.items():
        try:
            state = _systemctl("is-active", unit, systemctl=systemctl).strip()
        except (subprocess.CalledProcessError, OSError):
            state = "unknown"
        if state != "active":
            problems.append(f"{unit}:not_active:{state}")
        try:
            effective = effective_switches(unit, systemctl=systemctl)
        except (subprocess.CalledProcessError, OSError):
            effective = {}
        except QualificationRefused as exc:
            effective = {}
            problems.append(f"{kind}_{exc}")
        if not switches_off(effective):
            problems.append(f"{kind}_effective_switch_not_off")
    return {"ok": not problems, "problems": problems}


def canary_dropin(kind: str, *, python: Path, release_root: Path) -> str:
    return (
        "# Temporary in-unit canary installed by hramatka.engine.v4_unit_qualification stage.\n"
        "# Removed after collection. Failure never blocks the service start.\n"
        "[Service]\n"
        f"ExecStartPre=-{python} -B -m hramatka.engine.v4_unit_qualification canary --unit {kind}\n"
        f"WorkingDirectory={release_root}\n"
    )


def _write_private(path: Path, payload: dict) -> None:
    """Exclusively created, owned 0600 temporary; never truncates or renames a foreign file."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            fd = None
            json.dump(payload, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        if fd is not None:
            os.close(fd)
        tmp.unlink(missing_ok=True)
        raise


def _strict_json(text: str):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    return json.loads(text, object_pairs_hook=unique)


def collect_canary(
    unit: str, kind: str, *, systemctl: str = "systemctl", journalctl: str = "journalctl"
) -> dict:
    invocation = _systemctl(
        "show", unit, "--property=InvocationID", "--value", systemctl=systemctl
    ).strip()
    if not HEX32.fullmatch(invocation):
        raise QualificationRefused(f"{kind}_canary_invocation_invalid")
    out = subprocess.run(
        [journalctl, "-u", unit, f"_SYSTEMD_INVOCATION_ID={invocation}", "-o", "cat", "--no-pager"],
        check=True,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT,
    ).stdout
    lines = [line[len(CANARY_MARK) :] for line in out.splitlines() if line.startswith(CANARY_MARK)]
    if len(lines) != 1:
        raise QualificationRefused(f"{kind}_canary_line_count:{len(lines)}")
    try:
        report = _strict_json(lines[0])
    except ValueError:
        raise QualificationRefused(f"{kind}_canary_schema") from None
    if (
        not isinstance(report, dict)
        or report.get("identity", {}).get("invocation_id") != invocation
    ):
        raise QualificationRefused(f"{kind}_canary_invocation_mismatch")
    return report


def restart_deadline(unit: str, *, systemctl: str = "systemctl") -> float:
    """Blocking allowance derived from the unit's own stop timeout (drain) plus a fixed margin."""
    text = _systemctl(
        "show", unit, "--property=TimeoutStopUSec", "--value", systemctl=systemctl
    ).strip()
    drain = timespan_seconds(text)
    if drain is None:
        raise QualificationRefused(f"{unit}:drain_unbounded")
    return drain + RESTART_MARGIN


def _unit_state(unit: str, *, systemctl: str) -> tuple[str, str]:
    out = _systemctl("show", unit, "--property=ActiveState", "--property=Job", systemctl=systemctl)
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return props.get("ActiveState", "unknown"), props.get("Job", "")


def wait_restarted(unit: str, allowance: float, *, systemctl: str = "systemctl") -> None:
    """Wait until the restart job has settled and the unit is active, within the drain allowance.

    A job that settles into a non-active state refuses at once; a job still
    pending after the allowance refuses without any further service operation.
    """
    deadline = time.monotonic() + allowance
    while True:
        state, job = _unit_state(unit, systemctl=systemctl)
        if not job:
            if state == "active":
                return
            raise QualificationRefused(f"{unit}:restart_settled:{state}")
        if time.monotonic() > deadline:
            raise QualificationRefused(f"{unit}:restart_not_active")
        time.sleep(1)


def _job_settlement(unit: str, deadline: float, *, systemctl: str) -> str | None:
    """Rollback safety: verified settlement (``None``) or the specific reason it is not.

    Waits until the deadline, then queries once more. Elapsed time is never
    equated with completion and a failed state query is never treated as
    settled: ``restart_job_pending`` or ``unit_state_unknown`` is returned so
    the caller retains staging instead of removing it blindly.
    """
    while True:
        try:
            _, job = _unit_state(unit, systemctl=systemctl)
        except (subprocess.CalledProcessError, OSError):
            return "unit_state_unknown"
        if not job:
            return None
        if time.monotonic() > deadline:
            return "restart_job_pending"
        time.sleep(1)


def _rollback(installed: list[Path], pending: dict[str, float], *, systemctl: str) -> dict:
    """Remove the owned canary drop-ins only after every restart job verifiably settled.

    Running units are never stopped. When a job is still pending or its state
    is unknown, the staging is retained and reported for operator recovery.
    """
    retained = {
        unit: reason
        for unit, deadline in pending.items()
        if (reason := _job_settlement(unit, deadline, systemctl=systemctl)) is not None
    }
    if retained:
        return {"retained": retained, "retained_dropins": sorted(str(p) for p in installed)}
    for path in installed:
        path.unlink(missing_ok=True)
    if installed:
        _systemctl("daemon-reload", systemctl=systemctl)
    return {"retained": {}, "retained_dropins": []}


def stage(args) -> int:
    dropin_dirs = {"api": args.api_dropin_dir, "sources": args.sources_dropin_dir}
    report = preflight(
        dropin_dirs=dropin_dirs,
        qualification_file=args.qualification_file,
        python=args.unit_python,
        release_root=args.release_root,
        systemctl=args.systemctl,
        require_root=not args.dry_run,
    )
    plan = {
        "preflight": report,
        "canary_dropins": {k: str(d / CANARY_DROPIN) for k, d in dropin_dirs.items()},
        "restart_order": [API_UNIT, SOURCES_UNIT],
        "qualification_file": str(args.qualification_file),
        "switches": "execution and admission remain OFF; "
        "credential is read at the next operator restart",
    }
    if args.dry_run or not report["ok"]:
        print(json.dumps(plan, sort_keys=True))
        return 0 if report["ok"] else 2
    installed: list[Path] = []
    pending: dict[str, float] = {}
    try:
        if not args.qualification_file.exists():
            _write_private(args.qualification_file, pending_qualification())
        for kind, directory in dropin_dirs.items():
            path = directory / CANARY_DROPIN
            path.write_text(
                canary_dropin(kind, python=args.unit_python, release_root=args.release_root)
            )
            os.chmod(path, 0o644)
            installed.append(path)
        _systemctl("daemon-reload", systemctl=args.systemctl)
        reports = {}
        for kind, unit in (("api", API_UNIT), ("sources", SOURCES_UNIT)):
            # Effective OFF (Environment= and every EnvironmentFile=) must hold
            # for the configuration about to be started.
            if not switches_off(effective_switches(unit, systemctl=args.systemctl)):
                raise QualificationRefused(f"{kind}_effective_switch_not_off")
            allowance = restart_deadline(unit, systemctl=args.systemctl)
            pending[unit] = time.monotonic() + allowance
            _systemctl(
                "restart", "--no-block", unit, systemctl=args.systemctl
            )  # honors TimeoutStopSec drain
            wait_restarted(unit, allowance, systemctl=args.systemctl)
            del pending[unit]
            reports[kind] = collect_canary(
                unit, kind, systemctl=args.systemctl, journalctl=args.journalctl
            )
        properties = {
            unit: unit_properties(unit, systemctl=args.systemctl) for unit in UNITS.values()
        }
        if not all(switches_off(p["EffectiveSwitches"]) for p in properties.values()):
            raise QualificationRefused("effective_switch_not_off")
        qualification = compose_qualification(
            reports["api"], reports["sources"], unit_properties=properties
        )
        _write_private(args.qualification_file, qualification)
        outcome = {
            "outcome": "qualified",
            "qualification_sha256": _digest(_canonical(qualification)),
            "canaries": qualification["canaries"],
            "unit_configuration_sha256": qualification["unit_configuration_sha256"],
        }
        code = 0
    except QualificationRefused as exc:
        outcome = {"outcome": "refused", "code": str(exc)}
        code = 3
    finally:
        # Rollback: the temporary canary drop-ins are removed only after every
        # pending restart job has verifiably settled; running units stay up.
        rollback = _rollback(installed, pending, systemctl=args.systemctl)
    if rollback["retained"]:
        # Recoverable: the operator settles/inspects the job, then removes the
        # listed drop-ins and daemon-reloads. Nothing else was changed.
        outcome["rollback"] = rollback
        code = 4
    print(json.dumps(outcome, sort_keys=True))
    return code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="hramatka.engine.v4_unit_qualification")
    sub = parser.add_subparsers(dest="command", required=True)
    canary = sub.add_parser("canary", help="run inside the actual unit (ExecStartPre)")
    canary.add_argument("--unit", choices=sorted(UNITS), required=True)
    staging = sub.add_parser("stage", help="root-operated staging, collection and rollback")
    staging.add_argument(
        "--api-dropin-dir", type=Path, default=Path(f"/etc/systemd/system/{API_UNIT}.d")
    )
    staging.add_argument(
        "--sources-dropin-dir", type=Path, default=Path(f"/etc/systemd/system/{SOURCES_UNIT}.d")
    )
    staging.add_argument(
        "--qualification-file",
        type=Path,
        required=True,
        help="root-owned 0600 LoadCredential source",
    )
    staging.add_argument(
        "--unit-python", type=Path, default=Path("/opt/hramatka/current/.venv/bin/python")
    )
    staging.add_argument("--release-root", type=Path, default=Path("/opt/hramatka/current"))
    staging.add_argument("--systemctl", default=shutil.which("systemctl") or "systemctl")
    staging.add_argument("--journalctl", default=shutil.which("journalctl") or "journalctl")
    staging.add_argument(
        "--dry-run", action="store_true", help="preflight and plan only; changes nothing"
    )
    args = parser.parse_args(argv)
    if args.command == "canary":
        report = run_canary(args.unit)
        sys.stdout.write(
            CANARY_MARK + json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n"
        )
        return 0 if report["refused"] is None and all(report.get("passed", {}).values()) else 1
    return stage(args)


if __name__ == "__main__":
    sys.exit(main())
