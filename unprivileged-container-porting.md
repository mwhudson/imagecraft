# Running imagecraft in an unprivileged LXD container

This document describes the obstacles encountered when trying to run
`imagecraft pack` inside an unprivileged LXD container and the approach
taken to overcome each one. The obstacles are presented in the order they
would cause a run to fail — i.e., the order you would encounter them if
you started from the original code and ran it unmodified in the container.

## Background

An unprivileged LXD container runs entirely within a user namespace. The
container's UID 0 maps to an unprivileged UID on the host. The kernel
gates several privileged operations — notably anything that touches
`/dev/loop-control` and `mount(2)` with certain filesystem types — on
`CAP_SYS_ADMIN` in the _initial_ user namespace, not in the container's
user namespace. imagecraft's original implementation relied on three such
operations: loop device allocation (`losetup`), filesystem mounting
(`mount -t devtmpfs`, `mount --bind`), and raw device access for GRUB
installation.

The target environment is any unprivileged LXD container. Regardless of
the host's storage backend, the host's block devices are not exposed
inside the container.

---

## Obstacle 1: `mmdebstrap --mode=root` fails with mknod errors

_Phase: parts lifecycle — build step (first operation in a run)._

### Root cause

The mmdebstrap plugin originally invoked mmdebstrap with `--mode=root`.
In this mode, mmdebstrap tests whether `mknod` works (via its internal
`havemknod` function, which attempts `mknod test-dev-null c 1 3`). In an
unprivileged LXD container, `mknod` for device nodes is blocked by the
kernel — it requires `CAP_MKNOD` in the _initial_ user namespace, not in
the container's user namespace. Even as root inside the container, the
call returns `EPERM`.

When `havemknod` returns false in root mode, mmdebstrap's fallback path
attempts to bind-mount host device nodes into the chroot. However, the
timing of this fallback relative to package installation scripts varies,
and certain operations fail before the bind-mounts are in place.

### Approach

Switch from `--mode=root` to `--mode=unshare`.

When mmdebstrap is already running as root (as it is inside the container),
`--mode=unshare` skips `CLONE_NEWUSER` entirely and only unshares mount,
PID, UTS, and IPC namespaces. This requires `unshare(CLONE_NEWNS)` and
`mount --bind` to work — both are available in unprivileged LXD containers
(which grant `CAP_SYS_ADMIN` within the container's user namespace).

Inside the unshared mount namespace:

1. `havemknod` is tested and returns false (same as root mode).
2. mmdebstrap bind-mounts `/dev/null`, `/dev/zero`, `/dev/random`, etc.
   from the host into the chroot _before_ any package scripts run.
3. Package postinst scripts that write to `/dev/null` or read
   `/dev/urandom` work without issue.
4. On exit, the mount namespace is discarded, cleaning up all bind-mounts
   automatically.

The change in the plugin is a single token: `--mode=root` → `--mode=unshare`.

**Trade-offs.** The `--mode=unshare` path creates a new mount namespace
per invocation. This is transparent in practice but means that any
filesystem mounts created inside mmdebstrap are not visible to the parent
process (by design — this is a feature, not a limitation, since the mounts
are temporary scaffolding for package installation).

If the container's kernel does not support `unshare(CLONE_NEWNS)` (e.g., a
severely restricted Docker container without `--cap-add SYS_ADMIN`), this
mode would also fail. In standard unprivileged LXD containers this is not
an issue — LXD grants `CAP_SYS_ADMIN` within the user namespace by
default.

---

## Obstacle 2: Loop devices are unavailable for partition formatting

_Phase: packing — partition format loop (after parts lifecycle completes)._

### Root cause

The original packing flow worked like this:

1. Create a raw disk image file and partition it with `sfdisk`.
2. Call `losetup --find --show --partscan` to attach the image as a loop
   device, which causes the kernel to expose `/dev/loopXpY` devices for
   each partition.
3. Format each partition by calling `mkfs.*` against `/dev/loopXpY`.
4. Copy content into the formatted filesystem by mounting the loop device
   and writing files, or by calling `mcopy` against the loop path.
5. Write GRUB's `boot.img` / `core.img` by seeking into `/dev/loopX`.
6. Detach the loop device.

Step 2 requires `CAP_SYS_ADMIN` in the initial user namespace to open
`/dev/loop-control`. Inside an unprivileged LXD container that capability
is absent, so `losetup` fails immediately.

In parallel, the environment variables `CRAFT_VOLUME_<NAME>` that the
lifecycle layer exposed to craft-parts plugins pointed at `/dev/loopXpY`
paths. With no loop devices those paths do not exist.

### Approach

The loop device was eliminated at every level of the stack:

**Partition formatting.** Most modern filesystem tools support writing
into an _offset_ inside a regular file rather than requiring a block
device:

- `mke2fs` accepts `-E offset=<bytes>` to position the filesystem at an
  arbitrary byte offset within a file.
- `mkfs.fat` accepts `--offset <sectors>`.
- `mcopy` accepts an `@@<offset>` suffix on an image path.
- `dd` with `oflag=seek_bytes` and `seek=<bytes>` can inject raw bytes at
  a known offset.

`diskutil.format_populate_partition` was extended with an optional
`PartitionGeometry` parameter carrying the byte offset and size of the
target partition. When present, the offset is threaded through to every
tool invocation, so the entire mkfs/copy cycle operates directly on the
disk image file. An earlier iteration used a temp-file-and-dd-inject
approach but this was replaced by the cleaner in-place offset approach
once all the tool invocations were confirmed to support it.

**Lifecycle env vars.** `CRAFT_VOLUME_<NAME>` was replaced by a triple
of variables per volume region:

```
CRAFT_VOLUME_<NAME>_FILE    absolute path to the disk image
CRAFT_VOLUME_<NAME>_OFFSET  byte offset of the partition within that file
CRAFT_VOLUME_<NAME>_SIZE    byte size of the partition
```

Plugins that need to write to a specific region use `dd oflag=seek_bytes`
against the disk image file directly.

**Trade-offs.** Writing at an offset inside a large image file means the
kernel has to zero out the sparse region on first write if the image was
created with `fallocate` or if the underlying filesystem does not support
sparse files. On ZFS this is transparent. The offset arithmetic
(sector × 512) must be kept consistent between `sfdisk --json` output and
the values passed to the tools; a `get_partition_geometry` helper in
`diskutil.py` centralises that conversion.

The in-place approach also constrains which filesystem types imagecraft
can support. The implementation relies on the mkfs tool being able both
to write at an offset inside a regular file _and_ to pre-populate the
filesystem with content from a directory in the same step — the equivalent
of `mke2fs -d <dir>` for ext4 and `mkfs.fat --offset` plus `mcopy` for
FAT. A filesystem type whose tooling does not support one or both of
those capabilities (for example, `btrfs-progs` has no equivalent of `-d`,
and some tools require a real block device) cannot be handled by this
path without an intermediate temp-file approach: create a correctly-sized
file, format and populate it with the tool's native mechanism, then `dd`
it into the disk image at the right offset. This is workable but
reintroduces a copy step. Any future addition of a new partition
filesystem type should check for this limitation early.

---

## Obstacle 3: GRUB installation required loop devices and image mounts

_Phase: packing — GRUB setup (same phase as obstacle 2, different code
path)._

### Root cause

The original `setup_grub` function:

1. Called `losetup --partscan` to get per-partition devices.
2. `mount`-ed the rootfs partition at a temp directory.
3. Bind-mounted `/dev` as a devtmpfs.
4. `chroot`-ed into the mounted rootfs and ran `update-grub` and
   `grub-install`.
5. Copied the resulting EFI files and `grub.cfg` back out.
6. Used the loop device path for the raw writes of `boot.img` / `core.img`.

Steps 1–3 all require privileges unavailable in the container. In
addition, `grub-install` itself probes real block devices, making it
doubly unsuitable for a container environment.

### Approach

The `grub-install` path was replaced with a two-phase, loop-free flow:

**Phase 1 — `prepare_grub_assets` (runs before the format loop).**
Instead of mounting a formatted partition, the function chroots directly
into the _prime directory_ that `mmdebstrap` populated — the directory
that _will become_ the rootfs filesystem but has not yet been formatted
into an image. At this point the directory tree is an ordinary directory
accessible without any mount.

Inside the chroot:

- `/dev` is bind-mounted from the _host's_ `/dev` (not a fresh devtmpfs),
  so device nodes that the chrooted tools expect are present without
  requiring `mount -t devtmpfs`.
- `update-grub` is run to produce `/boot/grub/grub.cfg` inside the prime
  directory. The resulting file is picked up naturally by `mke2fs -d`
  when the ext4 partition is formatted later.
- `grub-mkimage` is run to produce `core.img` for the BIOS boot path.

For the EFI partition, the shim and signed GRUB binaries are already
present in the rootfs prime directory (installed by `mmdebstrap`). The
function surveys their filenames at runtime (they vary across Ubuntu
releases) and copies them into the ESP prime directory so that `mcopy`
will pick them up at format time.

**Phase 2 — raw content application.**
After all partitions are formatted, `boot.img` and `core.img` are written
directly to the disk image file at their known byte offsets using `dd
conv=notrunc`. For GPT images `core.img` goes into the BIOS-boot
(`ef02`) partition; for MBR images it goes into the post-MBR gap. A
`rawcontent.py` module provides a bootloader-agnostic `apply_raw_content`
function so the disk-writing mechanism is not entangled with GRUB policy.

**Trade-offs.** Using `grub-mkimage` instead of `grub-install` means
imagecraft generates the BIOS boot path itself rather than delegating to
the GRUB packaging. If Canonical changes the recommended module set for
`grub-mkimage` between Ubuntu releases, imagecraft's module list (currently
hard-coded for `x86_64-efi` and `i386-pc`) would need to be updated. This
is an acceptable trade-off: `grub-install` in a container is fundamentally
untenable, whereas the module list changes infrequently.

Non-amd64 architectures (`arm64`, `armhf`, `riscv64`) emit a `TODO`
progress message and skip the GRUB setup step. They are not regressed
relative to the original code — the original flow was also x86-centric —
but they do not yet benefit from the container-safe path.

---

## Obstacle 4: `grub-probe` fails because the host's block devices are absent

_Phase: packing — inside the `prepare_grub_assets` chroot (only reachable
once obstacles 2 and 3 are addressed)._

### Root cause

`update-grub` (which wraps `grub-mkconfig`) unconditionally invokes
`grub-probe` to discover the block device underlying `/` and `/boot` and
to read their UUIDs. On the host, `grub-probe` walks `/proc/self/mountinfo`
to find the device, then inspects the device via `blkid`. Inside the
container the rootfs is backed by the host's storage pool whose vdevs are
not exposed in the container's `/dev` — so `grub-probe --target=device /`
fails with _"cannot find a device for / (is /dev mounted?)"_.

There is no environment variable or configuration knob to bypass this:
`grub-mkconfig.in` line 44 unconditionally overwrites `GRUB_DEVICE` by
calling `grub_probe`. Patching `/etc/default/grub` is ineffective.

A secondary consequence: even if `grub-probe` returned a UUID, the UUID
would belong to the _host's_ rootfs — not to the image being built. The
resulting `grub.cfg` would reference the wrong UUID, causing a boot failure.

### Approach

**grub-probe diversion.** Before running `update-grub`
inside the chroot, `grub-probe` is replaced with a stub using
`dpkg-divert`:

```sh
dpkg-divert --local --rename --divert /usr/sbin/grub-probe.real \
            /usr/sbin/grub-probe
```

A small shell script is written to `/usr/sbin/grub-probe`. It parses the
`--target`/`-t` argument and returns canned values:

| target         | value returned                     |
| -------------- | ---------------------------------- |
| `device`       | `/dev/sda`                         |
| `disk`         | `/dev/sda`                         |
| `fs`           | `ext2`                             |
| `fs_uuid`      | the pre-allocated UUID (see below) |
| `fs_label`     | _(empty)_                          |
| `abstraction`  | _(empty)_                          |
| `drive`        | `(hd0)`                            |
| `hints_string` | _(empty)_                          |
| `partmap`      | `gpt`                              |

`/dev/sda` and `(hd0)` are harmless placeholders; they appear in GRUB's
device map but are overridden at boot time by the `search --fs-uuid`
directive that `10_linux` generates. Critically, the `fs_uuid` value is
the real UUID that will be stamped on the image's rootfs partition (see
below).

The divert and stub are created inside a `try/finally` block. The
`finally` clause removes the stub, deletes the by-uuid symlink, and
un-diverts the real binary, so the chroot is left clean even if
`update-grub` fails.

**Pre-allocated UUID.** Rather than letting `mke2fs`
generate a random UUID at format time and then trying to extract it and
patch `grub.cfg`, the UUID is generated by imagecraft _before_ any disk
operation:

```python
rootfs_uuid = str(uuid.uuid4())
```

This UUID is stored in `GrubAssets` and flows along two independent paths:

1. Into the `grub-probe` stub as the `fs_uuid` return value, so
   `grub-mkconfig` embeds it in `grub.cfg` via `search --no-floppy
--fs-uuid --set=root <uuid>` and `root=UUID=<uuid>`.
2. Into `mke2fs` as `-U <uuid>`, so the formatted ext4 partition carries
   exactly that UUID on disk.

Because the same value is used in both places, `grub.cfg` and the
filesystem always agree.

**`/dev/disk/by-uuid` symlink.** The script `10_linux.in` in
`grub-mkconfig` guards the `root=UUID=...` directive with `test -e
/dev/disk/by-uuid/<uuid>`. If the symlink is absent the script falls back
to the raw device name (`root=/dev/sda`), which would be wrong at boot.
The stub creation code creates a temporary symlink:

```sh
/dev/disk/by-uuid/<uuid> -> /dev/null
```

This satisfies the `test -e` check without requiring a real device node.
The symlink is removed in the `finally` block.

**Trade-offs.** Diverting a system binary inside the chroot is invasive
and relies on `dpkg-divert` being present (it always is on Debian/Ubuntu).
If `grub-mkconfig` is restructured in a future GRUB release to call
`grub-probe` via a different path or to check the divert database, the
approach would need revisiting. The alternative — patching `grub-mkconfig`
itself — is more fragile and would require re-patching on every GRUB
upgrade.

The pre-allocated UUID approach means the UUID is determined by imagecraft
rather than by the filesystem tool. This is fine for a purpose-built image
(UUID uniqueness across builds is not required beyond being a valid UUID4)
but differs from the usual model where `mke2fs` generates a UUID and
everything else reads it back.

---

## Obstacle 5: `mkfs.fat` cluster-count error for EFI partitions

_Phase: packing — FAT partition formatting (a consequence of fixing
obstacle 2; the original loop-based code did not have this problem because
`mkfs.fat` saw a correctly-sized loop device rather than the full image
file)._

### Root cause

When `mkfs.fat` is called against a regular file with an explicit block
count (the partition size), it also reads the _total size of the file_
(the full disk image) to select a cluster size. For a 256 MiB EFI
partition inside a 5+ GiB image, `mkfs.fat` selected a cluster size
appropriate for a 5 GiB device. That cluster size divided into 256 MiB
of partition space produces a cluster count below FAT32's minimum (65,527
clusters), causing `mkfs.fat` to abort with _"not enough clusters"_.

### Approach

When `fatsize` is not explicitly specified in the structure definition and
both the partition offset and size are known, `diskutil` now auto-selects
`-F 32` (force FAT32) and `-s <sectors-per-cluster>` derived from the
actual partition size (targeting approximately 131,072 clusters, comfortably
above the FAT32 minimum of 65,527):

```python
total_sectors = size_bytes // 512
spc = max(1, total_sectors // 131072)
# round up to next power of two
spc = 1 << (spc - 1).bit_length()
```

This ensures the cluster count is valid regardless of the size of the
surrounding disk image file.

**Trade-offs.** Auto-selecting the cluster size overrides the kernel
default heuristic. For partitions smaller than ~256 MiB the formula
produces `spc = 1` (512-byte clusters), which is legal but may be
suboptimal for very large files. EFI partitions typically hold only a
handful of small GRUB/shim binaries, so this is not a practical concern.
If a user explicitly sets `fatsize` in the structure definition, the
auto-selection is skipped entirely and the original behaviour is preserved.

---

## Obstacle 6: mcopy fails with "plain langstrstrncasecmp" on resolute hosts

_Phase: pack — copying files into FAT partition._

**Root cause.** The imagecraft snap is built with `base: core24` (glibc
2.39). Its bundled `mcopy` is patchelf'd to use that glibc at runtime.
However, glibc loads character-set conversion modules (gconv) via
`dlopen` using a compiled-in search path that still resolves to the
**host** system's gconv directory (`/usr/lib/<triplet>/gconv`). On a
resolute host (glibc 2.41) the host's `IBM850.so` is ABI-incompatible
with core24's glibc 2.39, causing `iconv_open("WCHAR_T", "CP850")` to
return -1 with `errno=EINVAL`. mcopy then falls back to `langstrstrncasecmp`
which produces garbled behaviour or failures.

**Diagnosis.**
- `LD_DEBUG=all` trace showed `dlopen` of IBM850.so from the host path.
- Python on the same system (using host glibc 2.41) could successfully
  `iconv_open` all codepages — confirming the issue is ABI mismatch, not
  a missing module.
- Setting `GCONV_PATH=/snap/core24/current/usr/lib/x86_64-linux-gnu/gconv`
  before invoking mcopy resolved the issue immediately.

**Fix.** Added `_gconv_env_prefix()` helper in `diskutil.py`. When
`$SNAP` is set (i.e., running as a snap), it computes the architecture-
appropriate gconv directory inside the core24 base snap and returns a
`GCONV_PATH=<dir> ` shell prefix. This is prepended to the mcopy
command string (alongside the existing `LC_ALL=C` prefix).

The helper:
1. Checks `os.environ.get("SNAP")` — returns `""` if not running as a snap.
2. Maps `platform.machine()` to the multiarch triplet.
3. Verifies the gconv directory exists (`.is_dir()`); returns `""` if not.

**Trade-offs.** The fix is specific to core24-based snaps. If the base
snap changes (e.g., core26), the gconv path changes too. The function
uses `/snap/core24/current/` which tracks the latest revision of core24.
A future migration to a different base would require updating the path.
An alternative approach (compiling mtools without `HAVE_ICONV_H` to use
built-in codepage tables) avoids the issue entirely but loses proper
Unicode support in FAT filenames.

---

## Future work: supporting filesystems without offset/populate tooling

The current implementation avoids loop devices entirely because ext4 and
FAT both have tools that support direct-to-image operations:

| Filesystem | Create at offset | Populate without mounting |
| ---------- | ---------------- | ------------------------ |
| ext4       | `mke2fs -E offset=` | `mke2fs -d <dir>` |
| FAT/vfat   | `mkfs.fat --offset` | `mcopy -i <file>@@<offset>` |

If a new filesystem type is needed (e.g. XFS, btrfs, f2fs) that lacks
these capabilities, the container would need access to a block device in
order to run `mkfs` + `mount` + copy + `umount`. Since block device
access (loop devices) requires host privilege, a **host-side daemon**
would be needed to provide that plumbing.

### Design constraint

The daemon must **not** run filesystem tools itself. The container holds
the correct mkfs/mount implementations for the target release (e.g.
noble's `mkfs.xfs` may produce different on-disk features than
resolute's). Delegating mkfs to the host would silently use the wrong
tool version. The daemon's responsibility is limited to privileged
plumbing — it is a "loop device vending machine."

### Interaction sketch

```
Container (correct tools)               Host daemon (has privilege)
─────────────────────────               ──────────────────────────────

1. Request:
   "attach <image-path>
    offset=<bytes>
    sizelimit=<bytes>"          ──────►

                                        2. losetup --offset <off> \
                                             --sizelimit <size> \
                                             --find --show <image-path>
                                           → /dev/loop7

                                        3. lxc config device add <ctr> loop7 \
                                             unix-block source=/dev/loop7 \
                                             path=/dev/loop7
                                           (also adds cgroup device allow)

                                ◄────── 4. Response: "/dev/loop7"

5. mkfs.xfs /dev/loop7          ← container's own mkfs (target release)
6. mount /dev/loop7 /mnt         ← allowed via security.syscalls.intercept.mount
7. cp -a <content>/* /mnt/
8. umount /mnt

9. Request: "detach /dev/loop7"  ──────►

                                       10. lxc config device remove <ctr> loop7
                                       11. losetup -d /dev/loop7

                                ◄────── 12. Response: "ok"
```

### LXD host configuration required

```yaml
# Allow the container to mount the injected block device.
config:
  security.syscalls.intercept.mount: "true"
  security.syscalls.intercept.mount.allowed: xfs
```

The container itself never touches `/dev/loop-control` or calls
`losetup`. It only sees a pre-attached block device that appears and
disappears on demand.

### Communication channel

The daemon could listen on a Unix socket bind-mounted into the container
(e.g. `/run/imagecraft-loopd.sock`). The protocol is trivial:

| Request | Parameters | Response |
| ------- | ---------- | -------- |
| `attach` | `image=<path>`, `offset=<bytes>`, `sizelimit=<bytes>` | `device=<path>` |
| `detach` | `device=<path>` | `ok` |

The image file must be accessible to both the host daemon and the
container (shared bind mount or host-path device). The daemon validates
that the requested file belongs to the calling container's rootfs or an
explicitly allowed path, preventing escape.

### Separation of concerns

| Layer | Responsibility |
| ----- | -------------- |
| Container (imagecraft) | Filesystem policy: which mkfs, which options, which content, which offset/size |
| Host daemon | Privilege mechanism: loop device lifecycle, device injection, cgroup rules |
| LXD config | Mount syscall interception for the specific filesystem type |

This keeps the "zero privilege in the container" property for all
operations except the block device itself, and ensures the filesystem is
always created by the target release's tools.

### When this is NOT needed

If a filesystem's tools gain offset and populate support (as ext4 and
FAT already have), the daemon is unnecessary for that type. Upstreaming
`-d` / `--offset` / equivalent into additional mkfs implementations is
the long-term preferred path.

---

## Summary of changed files

| File                                           | What changed                                                                                                                                                         |
| ---------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `imagecraft/plugins/mmdebstrap_plugin.py`      | Changed `--mode=root` to `--mode=unshare`                                                                                                                            |
| `imagecraft/pack/diskutil.py`                  | Added offset/size support to `format_populate_partition` and helpers; added `uuid` parameter to `_format_populate_ext_partition`; fixed FAT32 cluster-size selection; added `_gconv_env_prefix()` for mcopy snap compatibility |
| `imagecraft/pack/grubutil.py`                  | Replaced `grub-install` + loop-mount flow with `grub-mkimage` + prime-dir chroot; added `grub-probe` divert + stub; added UUID pre-allocation                        |
| `imagecraft/pack/rawcontent.py`                | New module: bootloader-agnostic raw-content applier (`RawContent`, `apply_raw_content`)                                                                              |
| `imagecraft/services/pack.py`                  | Replaced `attach_images`/loop-path flow with offset-based format loop; calls `prepare_grub_assets` + `apply_raw_content`                                             |
| `imagecraft/services/lifecycle.py`             | Replaced `CRAFT_VOLUME_<NAME>` loop-path vars with `_FILE`/`_OFFSET`/`_SIZE` triples                                                                                 |
| `imagecraft/pack/image.py`                     | Removed unreferenced loop-device machinery                                                                                                                           |
| `tests/unit/plugins/test_mmdebstrap_plugin.py` | Updated assertion for `--mode=unshare`                                                                                                                               |
| `tests/unit/pack/test_grubutil.py`             | Fully rewritten for the new flow                                                                                                                                     |
| `tests/unit/pack/test_diskutil.py`             | Added tests for offset/uuid/FAT32 paths; added tests for `_gconv_env_prefix()`                                                                                       |
| `tests/unit/pack/test_rawcontent.py`           | New: tests for the generic applier                                                                                                                                   |
| `tests/unit/services/test_pack.py`             | Updated for new pack-service call sequence                                                                                                                           |
| `tests/unit/services/test_lifecycle.py`        | Updated for new env-var shape                                                                                                                                        |
