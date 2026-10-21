"""One calibration backend: already provisioned APFS volume with kernel quota.

Never provisions, attaches or detaches a volume. Geometry inspection is not the
physical ENOSPC/sandbox proof required by verify(). All outputs stay on volume.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import struct
import subprocess
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
    hardware = plist(["/usr/sbin/ioreg", "-a", "-rd1", "-c", "IOPlatformExpertDevice"])
    if not isinstance(hardware,list) or not hardware or not hardware[0].get("IOPlatformUUID"):
        raise ValueError("HOST_HARDWARE_ID_UNAVAILABLE")
    return {"backend": BACKEND, "mount": str(mount), "cap_bytes": cap,
            "volume_uuid": volume["VolumeUUID"], "device": volume["DeviceIdentifier"],
            "container_uuid": container["APFSContainerUUID"],
            "filesystem": filesystem_identity(mount),
            "host": hashlib.sha256(hardware[0]["IOPlatformUUID"].encode()).hexdigest(),
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


def verify(mount: Path, cap: int, proof_hash: str, source_sha: str) -> dict:
    identity = inspect_volume(mount, cap)
    control = ControlFile(mount / ".af-capacity-proof.control")
    try:
        claim, result = control.get(0), control.get(1)
    finally:
        control.close()
    if (canonical_hash(result) != proof_hash or claim.get("identity") != identity
            or claim.get("source_sha") != source_sha or result.get("identity") != identity
            or result.get("status") != "PHYSICALLY_VERIFIED"
            or result.get("aggregate_enospc") is not True or result.get("sandbox_denial") is not True
            or result.get("files_written", 0) < 2 or result.get("observed_capacity_bytes", 0) <= cap // 2
            or result.get("capacity_errno") not in (errno.ENOSPC, errno.EDQUOT)
            or type(claim.get("host_reserve_bytes")) is not int or claim["host_reserve_bytes"] <= 0
            or result.get("container_free_at_error", -1) < claim["host_reserve_bytes"]
            or not 0 <= time.time() - result.get("completed_at", 0) <= MAX_PROOF_AGE):
        raise ValueError("PHYSICAL_BACKEND_PROOF_MISSING_STALE_OR_CROSS_SCOPED")
    return {**identity, "proof_expires_at": result["completed_at"] + MAX_PROOF_AGE}


def probe(mount: Path, cap: int, source_sha: str, host_reserve: int) -> dict:
    """Explicit future physical test, never called by inspect/run or on CI.

    Writes ONLY fresh synthetic fill files, verifies aggregate ENOSPC and sandbox
    denial, removes ONLY those own fill files, preserves claim/results forever.
    Does not create a volume, change quota or touch any image.
    """
    identity = inspect_volume(mount, cap)
    system_entries = {".fseventsd", ".Spotlight-V100", ".Trashes", ".TemporaryItems"}
    if any(p.name not in system_entries for p in mount.iterdir()):
        raise ValueError("EMPTY_DEDICATED_VOLUME_REQUIRED")
    if container_free(identity) < cap + host_reserve:
        raise ValueError("BACKING_HOST_RESERVE_INSUFFICIENT")
    control = ControlFile(mount / ".af-capacity-proof.control", {"identity": identity,
                          "source_sha": source_sha, "host_reserve_bytes": host_reserve, "started_at": time.time()})
    files, total, enospc = [], 0, False
    started = time.monotonic()
    result = {"status": "FAILED", "identity": identity, "aggregate_enospc": False,
              "sandbox_denial": False, "files_written": 0, "observed_capacity_bytes": 0}
    try:
        profile = sandbox_profile(mount)
        # No outside file is created: attempt writing a random absent path in the
        # existing backing directory. Success makes verification FAIL.
        outside = mount.parent / (".af-denial-" + str(os.getpid()))
        if outside.exists():
            raise ValueError("PROBE_PATH_COLLISION")
        import sys
        inside = mount / ".af-sandbox-positive"
        denied = subprocess.run(["/usr/bin/sandbox-exec", "-p", profile, sys.executable, "-c",
                                "import sys;open(sys.argv[1],'x').close();open(sys.argv[2],'x').close()",
                                str(inside), str(outside)], capture_output=True, timeout=10)
        result["sandbox_denial"] = denied.returncode != 0 and inside.is_file() and not outside.exists()
        result["sandbox_returncode"] = denied.returncode
        control.put(2,{"stdout":redact(denied.stdout[:65536].decode(errors="replace"))})
        control.put(3,{"stderr":redact(denied.stderr[:65536].decode(errors="replace"))})
        if inside.exists():
            inside.unlink()
        if outside.exists():
            outside.unlink()  # only this probe's fresh zero-byte negative-test file
        # At least two files: no single-file RLIMIT can impersonate aggregate cap.
        chunk = os.urandom(min(1024 * 1024, max(4096, identity["cap_bytes"] // 64)))
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
                    if container_free(identity) < host_reserve:
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
        for path in files:
            path.unlink()  # own disposable fill bytes only; never image/evidence
        fsync_directory(mount)
        result["completed_at"] = time.time()
        result["wall_seconds"] = time.monotonic() - started
        control.put(1, result)
        control.close()
    return result
