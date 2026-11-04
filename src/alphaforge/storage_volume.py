"""One calibration backend: already provisioned APFS volume with kernel quota.

Never provisions, attaches or detaches a volume. Geometry inspection is not the
physical ENOSPC/sandbox proof required by verify(). All outputs stay on volume.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import struct
import subprocess
import sys
import time

from alphaforge.burnin import canonical_hash
from alphaforge.storage_calibration import file_hash, filesystem_identity, fsync_directory, redact

BACKEND = "apfs-capacity-quota.v1"
SLOT = 1024 * 1024
SLOTS = 4
MAX_PROOF_AGE = 86400


def plist(command):
    result = subprocess.run(command, capture_output=True, timeout=15, check=True)
    return plistlib.loads(result.stdout)


def host_identity() -> str:
    """SHA-256 of the exact IOPlatformUUID string, as in the legacy plist path.

    ioreg -a opens /dev/stdout via CFWriteStream, incompatible with our write
    sandbox. Text mode writes to the inherited pipe directly. Only one complete
    IORegistry object with one UUID string is accepted; there is no fallback or
    case/UUID normalization. Both parent and sandboxed worker use this authority.
    """
    result = subprocess.run(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice", "-w0"],
                            capture_output=True, check=True, timeout=15)
    try:
        text = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("HOST_HARDWARE_ID_UNAVAILABLE") from None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if (len(lines) < 4 or not lines[0].startswith("+-o ") or "<class " not in lines[0]
            or not lines[0].endswith(">") or lines[1] != "{" or lines[-1] != "}"
            or sum(line.startswith("+-o ") for line in lines) != 1 or "\x00" in text):
        raise ValueError("HOST_HARDWARE_ID_UNAVAILABLE")
    properties = [line for line in lines[2:-1] if line.startswith('"IOPlatformUUID"')]
    match = (re.fullmatch(r'"IOPlatformUUID"[ \t]*=[ \t]*"([0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12})"',
                          properties[0]) if len(properties) == 1 else None)
    if match is None:
        raise ValueError("HOST_HARDWARE_ID_UNAVAILABLE")
    return hashlib.sha256(match[1].encode("utf-8")).hexdigest()


def inspect_volume(mount: Path, cap: int) -> dict:
    mount = mount.resolve(strict=True)
    if platform.system() != "Darwin" or not mount.is_mount():
        raise ValueError("PREPROVISIONED_MAC_QUOTA_VOLUME_REQUIRED")
    volume = plist(["/usr/sbin/diskutil", "info", "-plist", str(mount)])
    containers = plist(["/usr/sbin/diskutil", "apfs", "list", "-plist"])
    matches = [(c, v) for c in containers.get("Containers", []) for v in c.get("Volumes", [])
               if v.get("APFSVolumeUUID") == volume.get("VolumeUUID")]
    if len(matches) != 1:
        raise ValueError("APFS_VOLUME_BINDING_UNVERIFIED")
    container, entry = matches[0]
    if (volume.get("FilesystemType") != "apfs" or volume.get("MountPoint") != str(mount)
            or volume.get("WritableVolume") is not True or volume.get("WritableMedia") is not True or not volume.get("VolumeUUID")
            or entry.get("DeviceIdentifier") != volume.get("DeviceIdentifier")
            or type(cap) is not int or cap <= 0 or entry.get("CapacityQuota") != cap
            or not container.get("APFSContainerUUID") or not entry.get("Roles") == []
            or not Path("/usr/bin/sandbox-exec").is_file()):
        raise ValueError("DEDICATED_WRITABLE_APFS_QUOTA_UNVERIFIED")
    if any(p.is_mount() for p in mount.iterdir() if p.is_dir()):
        raise ValueError("NESTED_MOUNT_FORBIDDEN")
    return {"backend": BACKEND, "mount": str(mount), "cap_bytes": cap,
            "volume_uuid": volume["VolumeUUID"], "device": volume["DeviceIdentifier"],
            "container_uuid": container["APFSContainerUUID"],
            "filesystem": filesystem_identity(mount),
            "host": host_identity(),
            "sandbox_binary": file_hash(Path("/usr/bin/sandbox-exec")), "platform":platform.platform()}


def usage(identity: dict) -> dict:
    data = plist(["/usr/sbin/diskutil", "apfs", "list", "-plist"])
    matches = [(c,v) for c in data.get("Containers", []) for v in c.get("Volumes", [])
               if c.get("APFSContainerUUID")==identity["container_uuid"] and v.get("APFSVolumeUUID")==identity["volume_uuid"]]
    if (len(matches)!=1 or type(matches[0][0].get("CapacityFree")) is not int
            or type(matches[0][1].get("CapacityInUse")) is not int
            or matches[0][1].get("CapacityQuota") != identity["cap_bytes"]
            or filesystem_identity(Path(identity["mount"])) != identity["filesystem"]):
        raise ValueError("HOST_OR_QUOTA_USAGE_UNKNOWN")
    return {"host_container_free_bytes":matches[0][0]["CapacityFree"],"quota_used_bytes":matches[0][1]["CapacityInUse"]}


def container_free(identity: dict) -> int:
    return usage(identity)["host_container_free_bytes"]


def sandbox_profile(mount: Path) -> str:
    # deny outside volume, including unexpected absolute log/DB/temp destinations.
    # Quoting uses SBPL string syntax; reject escapes instead of interpolating.
    value = str(mount.resolve())
    if any(c in value for c in ('"', '\\', '\n', '\r')):
        raise ValueError("SANDBOX_PATH_INVALID")
    return ('(version 1)(allow default)'
            f'(deny file-write* (require-all (require-not (subpath "{value}"))'
            '(require-not (literal "/dev/null"))))')


SANDBOX_SCHEMA = "canonical-sandbox-verification.v1"
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# Only this child writes the challenge. EPERM/EACCES must be observed by Python,
# not inferred from sandbox-exec failing to start or apply its profile.
_SANDBOX_CHECK = '''import errno,json,sys
nonce,inside,outside=sys.argv[1:]
with open(inside,'x') as f:f.write(nonce)
denied=None
try:
    with open(outside,'x') as f:f.write(nonce)
except OSError as exc:
    denied=exc.errno
print(json.dumps({'nonce':nonce,'outside_errno':denied}),flush=True)
sys.exit(0 if denied in (errno.EPERM,errno.EACCES) else 2)
'''


class SandboxPreflightError(ValueError):
    def __init__(self, evidence):
        self.evidence = evidence
        super().__init__("CANONICAL_SANDBOX_PREFLIGHT_FAILED:" + evidence["reason"])


def sandbox_verified(evidence: dict, mount: Path, source_sha: str) -> bool:
    """One validator for live preflight and persisted physical acceptance."""
    if not isinstance(evidence, dict):
        return False
    try:
        stdout = evidence.get("stdout", "")
        payload = json.loads(stdout)
        if (not isinstance(stdout, str) or len(stdout) > 65536 or not isinstance(payload, dict)
                or set(payload) != {"nonce", "outside_errno"}
                or payload["nonce"] != evidence.get("nonce")
                or type(payload["outside_errno"]) is not int
                or payload["outside_errno"] != evidence.get("outside_errno")
                or evidence.get("stderr") != ""):
            return False
    except (ValueError, TypeError):
        return False
    return (isinstance(evidence, dict)
        and evidence.get("schema") == SANDBOX_SCHEMA and evidence.get("status") == "SANDBOX_VERIFIED"
        and evidence.get("source_sha") == source_sha
        and evidence.get("profile_sha256") == hashlib.sha256(sandbox_profile(mount).encode()).hexdigest()
        and evidence.get("script_sha256") == hashlib.sha256(_SANDBOX_CHECK.encode()).hexdigest()
        and evidence.get("executable") == SANDBOX_EXEC
        and evidence.get("returncode") == 0 and type(evidence.get("returncode")) is int
        and evidence.get("inside_write") is True and evidence.get("outside_absent") is True
        and type(evidence.get("outside_errno")) is int and evidence["outside_errno"] in (errno.EPERM, errno.EACCES)
        and isinstance(evidence.get("nonce"), str) and re.fullmatch(r"[0-9a-f]{32}", evidence["nonce"]) is not None)


def sandbox_preflight(mount: Path, source_sha: str) -> dict:
    """Bounded challenge in the actual caller context; no claim, quota fill or admission.

    Call before an operator attempt latch as well as at the canonical probe
    boundary. An outer write denial cannot substitute for this inner execution.
    """
    if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", source_sha):
        raise ValueError("SANDBOX_SOURCE_SHA_INVALID")
    mount = mount.resolve(strict=True)
    profile = sandbox_profile(mount)
    nonce = os.urandom(16).hex()
    inside = mount / (".af-sandbox-positive-" + nonce)
    outside = mount.parent / (".af-sandbox-denied-" + nonce)
    result = dict(schema=SANDBOX_SCHEMA, status="FAILED", reason="UNVERIFIED",
        source_sha=source_sha, executable=SANDBOX_EXEC, nonce=nonce,
        profile_sha256=hashlib.sha256(profile.encode()).hexdigest(),
        script_sha256=hashlib.sha256(_SANDBOX_CHECK.encode()).hexdigest(),
        interpreter=sys.executable, returncode=None, inside_write=False,
        outside_absent=False, outside_errno=None, stdout="", stderr="")
    def diagnostic(value):
        raw = value or b""
        return redact(raw[:65536].decode(errors="replace") if isinstance(raw, bytes) else str(raw)[:65536])
    def owned(path):
        return (not path.is_symlink() and path.is_file() and path.stat().st_size == len(nonce)
                and path.read_text() == nonce)
    if any(path.exists() or path.is_symlink() for path in (inside, outside)):
        result["reason"] = "SANDBOX_CHALLENGE_COLLISION"
        return result
    try:
        completed = subprocess.run([SANDBOX_EXEC, "-p", profile, sys.executable, "-c",
            _SANDBOX_CHECK, nonce, str(inside), str(outside)], capture_output=True, timeout=10)
        result.update(returncode=completed.returncode, stdout=diagnostic(completed.stdout),
                      stderr=diagnostic(completed.stderr), inside_write=owned(inside),
                      outside_absent=not (outside.exists() or outside.is_symlink()))
        if completed.returncode != 0:
            result["reason"] = "CANONICAL_SANDBOX_EXECUTION_FAILED"
        else:
            payload = json.loads(completed.stdout)
            if (not isinstance(payload, dict) or set(payload) != {"nonce", "outside_errno"}
                    or payload.get("nonce") != nonce or len(completed.stdout) > 65536):
                raise ValueError("SANDBOX_OUTPUT_INVALID")
            result.update(outside_errno=payload["outside_errno"], status="SANDBOX_VERIFIED")
            if sandbox_verified(result, mount, source_sha):
                result["reason"] = "VERIFIED_INNER_EXECUTION_AND_DENIAL"
            else:
                result.update(status="FAILED", reason="SANDBOX_CHALLENGE_UNVERIFIED")
    except subprocess.TimeoutExpired as exc:
        result.update(reason="SANDBOX_TIMEOUT", stdout=diagnostic(exc.stdout), stderr=diagnostic(exc.stderr))
    except (OSError, ValueError, TypeError, KeyboardInterrupt) as exc:
        result.update(status="FAILED", reason="SANDBOX_" + type(exc).__name__,
                      spawn_errno=getattr(exc, "errno", None))
    finally:
        # Never delete a pre-existing or unexpected object, even on failure.
        for path in (inside, outside):
            try:
                if owned(path):
                    path.unlink()
                elif path.exists() or path.is_symlink():
                    result.update(status="FAILED", reason="SANDBOX_SENTINEL_IDENTITY_MISMATCH")
            except OSError:
                result.update(status="FAILED", reason="SANDBOX_SENTINEL_CLEANUP_FAILED")
    return result


class ControlFile:
    """One O_EXCL attempt; preallocated fixed slots, no unbounded host logs.

    Claim is fsynced before allocation/preparation. Partial slots fail their hash;
    disk-full/power-loss cannot inherit an old favorable completion record.
    """
    def __init__(self, path: Path, claim: dict | None = None):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT | os.O_EXCL if claim is not None else 0), 0o600)
        try:
            if claim is not None:
                self.put(0, claim, preparing=True)
                fsync_directory(path.parent)
                for offset in range(SLOT, SLOT * SLOTS, SLOT):
                    self._pwrite( bytes(SLOT), offset)
                os.fsync(self.fd)
                if os.fstat(self.fd).st_size != SLOT * SLOTS:
                    raise ValueError("CONTROL_PREALLOCATION_INCOMPLETE")
            elif os.fstat(self.fd).st_size != SLOT * SLOTS:
                raise ValueError("CONTROL_FILE_INCOMPLETE")
        except BaseException:
            os.close(self.fd)
            raise

    def _pwrite(self, data, offset):
        if os.pwrite(self.fd, data, offset) != len(data):
            raise OSError(errno.EIO, "CONTROL_SHORT_WRITE")

    def put(self, index: int, value: dict, *, preparing=False):
        raw = json.dumps(value, sort_keys=True, allow_nan=False).encode()
        if not 0 <= index < SLOTS or len(raw) > SLOT - 40:
            raise ValueError("CONTROL_SLOT_BOUND")
        if not preparing and os.fstat(self.fd).st_size != SLOT * SLOTS:
            raise ValueError("CONTROL_FILE_SIZE_CHANGED")
        offset = index * SLOT
        try:
            self._pwrite(bytes(40), offset)
            os.fsync(self.fd)
            if preparing:
                self._pwrite(bytes(SLOT), offset)
            self._pwrite(raw, offset + 40)
            self._pwrite(struct.pack('>Q', len(raw)) + hashlib.sha256(raw).digest(), offset)
            os.fsync(self.fd)
        except BaseException:
            # Never leave a readable favorable slot after a reported write/fsync
            # failure. If the device cannot invalidate either, evidence is unsafe
            # and requires external storage recovery, never automatic admission.
            try:
                os.pwrite(self.fd, bytes(40), offset)
                os.fsync(self.fd)
            except OSError:
                pass
            raise

    def get(self, index: int) -> dict:
        header = os.pread(self.fd, 40, index * SLOT)
        if len(header) != 40:
            raise ValueError("CONTROL_EVIDENCE_MISSING")
        length = struct.unpack('>Q', header[:8])[0]
        if not 0 < length <= SLOT - 40:
            raise ValueError("CONTROL_EVIDENCE_MISSING")
        raw = os.pread(self.fd, length, index * SLOT + 40)
        if hashlib.sha256(raw).digest() != header[8:]:
            raise ValueError("CONTROL_EVIDENCE_TORN")
        return json.loads(raw)

    def close(self):
        os.close(self.fd)


def _proof_paths(mount: Path):
    legacy = mount / ".af-capacity-proof.control"
    paths = ([legacy] if os.path.lexists(legacy) else []) + sorted(mount.glob(".af-capacity-proof.*.control"))
    for path in paths:
        if (path.is_symlink() or not path.is_file() or (path != legacy and
                not re.fullmatch(r"\.af-capacity-proof\.[A-Za-z0-9_-]{1,64}\.control", path.name))):
            raise ValueError("PHYSICAL_PROOF_PATH_INVALID")
    return paths


def _read_proof(path: Path):
    control = ControlFile(path)
    try:
        return control.get(0), control.get(1)
    finally:
        control.close()


def verify(mount: Path, cap: int, proof_hash: str, source_sha: str) -> dict:
    identity = inspect_volume(mount, cap)
    paths = _proof_paths(mount)
    if not paths:
        raise FileNotFoundError("PHYSICAL_BACKEND_PROOF_MISSING")
    matches = [(path, claim, result) for path in paths for claim, result in [_read_proof(path)]
               if canonical_hash(result) == proof_hash]
    if len(matches) != 1:
        raise ValueError("PHYSICAL_BACKEND_PROOF_MISSING_STALE_OR_CROSS_SCOPED")
    path, claim, result = matches[0]
    expected_path = (mount / f".af-capacity-proof.{claim['attempt_id']}.control"
                     if claim.get('attempt_id') else mount / '.af-capacity-proof.control')
    if (path != expected_path or claim.get('attempt_id') != result.get('attempt_id')
            or claim.get('prior_proofs', {}) != result.get('prior_proofs', {})
            or any(name not in {p.name for p in paths} or file_hash(mount / name) != digest
                   for name, digest in claim.get('prior_proofs', {}).items())):
        raise ValueError("PHYSICAL_BACKEND_PROOF_HISTORY_MISMATCH")
    if (canonical_hash(result) != proof_hash or claim.get("identity") != identity
            or claim.get("source_sha") != source_sha or result.get("identity") != identity
            or result.get("status") != "PHYSICALLY_VERIFIED"
            or result.get("aggregate_enospc") is not True or result.get("sandbox_denial") is not True
            or not sandbox_verified(result.get("sandbox_verification"), mount, source_sha)
            or type(result.get("sandbox_returncode")) is not int or result["sandbox_returncode"] != 0
            or result.get("files_written", 0) < 2 or result.get("observed_capacity_bytes", 0) <= cap // 2
            or result.get("capacity_errno") not in (errno.ENOSPC, errno.EDQUOT)
            or type(claim.get("host_reserve_bytes")) is not int or claim["host_reserve_bytes"] <= 0
            or result.get("container_free_at_error", -1) < claim["host_reserve_bytes"]
            or not 0 <= time.time() - result.get("completed_at", 0) <= MAX_PROOF_AGE):
        raise ValueError("PHYSICAL_BACKEND_PROOF_MISSING_STALE_OR_CROSS_SCOPED")
    return {**identity, "proof_expires_at": result["completed_at"] + MAX_PROOF_AGE}


def probe(mount: Path, cap: int, source_sha: str, host_reserve: int, *, attempt_id: str | None = None) -> dict:
    if attempt_id is None:
        return _probe(mount, cap, source_sha, host_reserve)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", attempt_id):
        raise ValueError("PHYSICAL_ATTEMPT_ID_INVALID")
    # All explicitly authorized successor attempts serialize on the unchanged
    # original evidence inode. flock changes no bytes and dies with the owner.
    prior = ControlFile(mount / ".af-capacity-proof.control")
    try:
        fcntl.flock(prior.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _probe(mount, cap, source_sha, host_reserve, attempt_id=attempt_id)
    finally:
        prior.close()


def _probe(mount: Path, cap: int, source_sha: str, host_reserve: int, *, attempt_id: str | None = None) -> dict:
    """Explicit future physical test, never called by inspect/run or on CI.

    Writes ONLY fresh synthetic fill files, verifies aggregate ENOSPC and sandbox
    denial, removes ONLY those own fill files, preserves claim/results forever.
    Does not create a volume, change quota or touch any image.
    """
    identity = inspect_volume(mount, cap)
    system_entries = {".fseventsd", ".Spotlight-V100", ".Trashes", ".TemporaryItems"}
    prior_proofs = {}
    allowed = set(system_entries)
    if attempt_id is not None:
        for path in _proof_paths(mount):
            claim, result = _read_proof(path)
            if (claim.get("identity") != identity or result.get("identity") != identity
                    or result.get("status") not in ("FAILED", "PHYSICALLY_VERIFIED")):
                raise ValueError("PRIOR_PHYSICAL_ATTEMPT_INCOMPLETE_OR_CROSS_SCOPED")
            allowed.add(path.name)
            prior_proofs[path.name] = file_hash(path)
    if any(p.name not in allowed for p in mount.iterdir()):
        raise ValueError("EMPTY_DEDICATED_VOLUME_REQUIRED")
    if container_free(identity) < cap + host_reserve:
        raise ValueError("BACKING_HOST_RESERVE_INSUFFICIENT")
    path = mount / (f".af-capacity-proof.{attempt_id}.control" if attempt_id else ".af-capacity-proof.control")
    extra = {"attempt_id": attempt_id, "prior_proofs": prior_proofs} if attempt_id else {}
    if path.exists() or path.is_symlink():
        raise FileExistsError("PHYSICAL_ATTEMPT_ID_ALREADY_CONSUMED")
    verification = sandbox_preflight(mount, source_sha)
    if not sandbox_verified(verification, mount, source_sha):
        raise SandboxPreflightError(verification)
    control = ControlFile(path, {"identity": identity,
                          "source_sha": source_sha, "host_reserve_bytes": host_reserve, "started_at": time.time(), **extra})
    files, total, enospc = [], 0, False
    started = time.monotonic()
    result = {"status": "FAILED", "identity": identity, "aggregate_enospc": False,
              "sandbox_denial": True, "sandbox_returncode": verification["returncode"],
              "sandbox_verification": verification,
              "files_written": 0, "observed_capacity_bytes": 0, **extra}
    try:
        control.put(2, {"stdout": verification["stdout"]})
        control.put(3, {"stderr": verification["stderr"]})
        # At least two files: no single-file RLIMIT can impersonate aggregate cap.
        # Native APFS inventory is a subprocess per write. A 1 MiB ceiling
        # can spend the 600s window polling before a multi-GiB quota fills.
        # Keep writes bounded, and reserve headroom for the whole next write.
        chunk = os.urandom(min(8 * 1024 * 1024, max(4096, identity["cap_bytes"] // 64)))
        file_limit = max(len(chunk), identity["cap_bytes"] // 4)
        while total <= cap:
            path = mount / f".af-fill-{len(files):06d}"
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            files.append(path)
            try:
                written = 0
                while written < file_limit:
                    if time.monotonic() - started > 600:
                        raise TimeoutError("PHYSICAL_PROBE_DEADLINE")
                    if container_free(identity) < host_reserve + len(chunk):
                        raise ValueError("HOST_RESERVE_VIOLATION")
                    n = os.write(fd, chunk)
                    if n <= 0: raise OSError(errno.EIO,"PHYSICAL_PROBE_SHORT_WRITE")
                    written += n
                    total += n
                os.fsync(fd)
            except OSError as exc:
                if exc.errno not in (errno.ENOSPC, errno.EDQUOT):
                    raise
                enospc = True
                result["capacity_errno"] = exc.errno
                result["container_free_at_error"] = container_free(identity)
                break
            finally:
                os.close(fd)
        result.update(aggregate_enospc=enospc, files_written=len(files), observed_capacity_bytes=total)
        if enospc and total > cap // 2 and len(files) >= 2 and result["sandbox_denial"] and result["container_free_at_error"] >= host_reserve:
            result["status"] = "PHYSICALLY_VERIFIED"
    except Exception as exc:
        result["reason"] = type(exc).__name__  # no raw command/secret output
    finally:
        result.update(files_written=len(files), observed_capacity_bytes=total)
        for path in files:
            path.unlink()  # own disposable fill bytes only; never image/evidence
        fsync_directory(mount)
        result["completed_at"] = time.time()
        result["wall_seconds"] = time.monotonic() - started
        control.put(1, result)
        control.close()
    return result
