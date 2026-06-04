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

## Obstacle 7: a separate `/boot` partition breaks the prime-dir chroot

_Phase: packing — inside `prepare_grub_assets` (extends obstacles 3 and
4)._

### Root cause

The loop-based flow handled a dedicated `/boot` partition for free.
`losetup --partscan` plus the per-partition `mount` calls (driven by the
`filesystems:` mounts) reassembled the _real_ directory hierarchy —
rootfs at `/`, boot at `/boot`, ESP at `/boot/efi` — before `update-grub`
ran. The loop-free flow gave that up: it chroots into the rootfs
_prime dir_, but craft's partitions feature has already routed the
`/boot` subtree (kernels, initrds) into the **boot partition's** prime
dir, _away_ from the rootfs prime dir. So inside the chroot:

1. `/boot` is empty — `update-grub` (which globs `/boot/vmlinuz-*`, see
   `10_linux.in`) finds no kernels and emits a menu with no entries.
2. The kernel-listing `grub.cfg` is written to the rootfs prime dir's
   `/boot/grub`, i.e. onto the **rootfs** partition — but the ESP stub
   and `core.img` were told (correctly) to load it from `($root)/grub`
   on the **boot** partition. Nothing reads it.
3. Even with the kernels visible, `grub-mkconfig` would emit
   `linux /boot/vmlinuz-…`. At boot `$root` is the boot partition, where
   the kernel lives at `/vmlinuz-…` (the partition root) — so the path
   prefix is wrong.

The earlier "separate `/boot`" commit aimed the boot chain at the boot
partition (a pre-allocated boot UUID, `($root)/grub` prefix) but did not
address any of the three placement/visibility problems above.

### Approach

Reproduce what `losetup --partscan` + mount did, but unprivileged and
pre-format. Three coordinated pieces, only active when a `filesystems:`
entry mounts a partition at exactly `/boot`:

**1. Bind-mount the boot prime dir at `/boot` (extends obstacle 3).**
`_phase_b_chroot_mounts` prepends a `--bind` of the boot partition's
prime dir onto `/boot` in the chroot. `update-grub` then sees the kernels
and writes `grub.cfg` straight onto the boot partition's prime dir, where
`mke2fs -d` picks it up for the boot partition. This fixes problems 1 and
2.

**2. Distinguish `/` from `/boot` in the `grub-probe` stub (extends
obstacle 4).** A separate boot UUID is pre-allocated (same pattern as the
rootfs UUID) and stamped onto the boot partition via `mke2fs -U`. The
stub now parses the positional path (for `--target=device`) and the
`--device` value (for `--target=fs_uuid`) so that:

| query                                  | returns        |
| -------------------------------------- | -------------- |
| `--target=device /`                    | rootfs device  |
| `--target=device /boot`                | boot device    |
| `--device <rootfs> --target=fs_uuid`   | rootfs UUID    |
| `--device <boot> --target=fs_uuid`     | boot UUID      |

So `grub-mkconfig` sets `GRUB_DEVICE_BOOT_UUID` to the boot UUID and
`10_linux` emits `search --fs-uuid <boot UUID>` per menu entry, while
`root=UUID=<rootfs UUID>` stays on the kernel command line. When `/boot`
is not separate the boot UUID is empty and every query collapses back to
the rootfs values — identical to the single-partition behaviour. (Only
the rootfs UUID still needs the `/dev/disk/by-uuid` symlink; the boot
partition is reached via `search --fs-uuid`, which `10_linux` does not
gate on a `by-uuid` `test -e`.)

**3. Divert `grub-mkrelpath` for the path prefix (new).** The
`/boot` vs `/vmlinuz` prefix is _not_ decided by `grub-probe`. `10_linux`
computes it via `make_system_path_relative_to_its_root`, which
`grub-mkconfig_lib.in` defines as a call to `grub-mkrelpath`. The real
tool finds filesystem boundaries by comparing `st_dev` while walking up
the tree — but our bind mount keeps `/boot` on the _same_ underlying
filesystem as `/`, so `st_dev` never changes and the prefix would not be
stripped. So `grub-mkrelpath` is diverted too (same `dpkg-divert`
mechanism as `grub-probe`) with a stub that treats `/boot` as the
filesystem root: paths under `/boot` come out boot-partition-relative
(`/vmlinuz-…`), everything else is the identity. This is exactly what the
real tool would emit on a genuine separate-`/boot` system, so the
font/theme/initrd callers stay correct too.

The argument forms and the kernel-path/`search` logic were checked
against the grub2 source: `grub-mkconfig.in` (lines 135–141, the
`grub-probe` calls), `grub-mkconfig_lib.in` (lines 33–52,
`grub-mkrelpath` resolution and `bindir`), and `grub.d/10_linux.in`
(lines 54–65 root-UUID gating, 132–146 the `search`/`linux` lines,
169–212 kernel discovery and `rel_dirname`).

**Trade-offs.** The `grub-mkrelpath` stub shares the fragility of the
`grub-probe` divert (obstacle 4): it assumes `grub-mkconfig` keeps
calling these binaries by their packaged paths. It also hard-codes the
`/usr/bin/grub-mkrelpath` location and raises a clear error if it is
absent, so a wrong assumption fails the build loudly rather than
producing a silently-unbootable image. Bootability of a separate-`/boot`
image is **not yet exercised by CI** — the spread test only greps
`update-grub` log lines, which appear even with zero kernel entries — so
this path still needs a real boot test.

---

## Obstacle 8: a hand-written ESP is incomplete vs. `grub-install`

_Phase: packing — ESP population in `prepare_grub_assets`, plus the
in-chroot core-image build (extends obstacle 3)._

### Root cause

Obstacle 3 replaced `grub-install` with a hand-rolled flow that wrote
only the bare minimum to boot in the common case: the signed shim at
`EFI/BOOT/BOOTX64.EFI`, the signed shim + signed grub + a chainload
`grub.cfg` stub under `EFI/ubuntu/`, and the raw `boot.img`/`core.img`
bytes. Compared with what real `grub-install --uefi-secure-boot
--no-nvram` leaves on disk — which is exactly what cloud images run
(`livecd-rootfs`, `ubuntu-cpc/hooks.d/base/disk-image-uefi.binary`,
against a loop device we cannot use here) — two things were missing, and
both bite outside the happy path.

**1. The EFI removable-media boot path had no second stage.** imagecraft
produces an offline disk image and cannot call `efibootmgr` (there are no
EFI variables to write, and the image is not the running system), so a
freshly-written image has **no NVRAM boot entry**. On first boot the
firmware therefore falls back to the removable media path
`\EFI\BOOT\BOOTX64.EFI`, which is shim. Shim then either loads its second
stage (`grubx64.efi`) from its **own** directory or, if a fallback binary
`fbx64.efi` is present there, launches that instead. imagecraft's
`EFI/BOOT/` contained only shim — no `grubx64.efi` next to it and no
`fbx64.efi` — so shim had nothing to hand off to and the image did not
boot via the removable path. `grub-install` handles this in
`also_install_removable()` (Ubuntu patch
`ubuntu-grub-install-extra-removable.patch`) together with the signed
install (`ubuntu-install-signed.patch`): it populates `EFI/BOOT/` with
shim + `fbx64.efi` + `mmx64.efi`, and drops `BOOTX64.CSV` in `EFI/ubuntu/`
so the fallback can recreate the NVRAM entry. `--no-nvram` (which cloud
images pass) skips only the `efibootmgr` call; it still installs the
removable fallback, which is precisely the part an offline image relies
on.

**2. The grub package postinsts refuse to update a hand-written
bootloader.** When grub is upgraded inside the running image, the
`grub-pc` and `grub-efi-amd64` postinsts only reinstall the bootloader if
a platform *core* image already exists on disk
(`debian/postinst.in`): `test -e /boot/grub/i386-pc/core.img` for BIOS
(line ~402) and `-e /boot/grub/$target/core.efi` for EFI (line ~578).
Real `grub-install` leaves both behind (`util/grub-install.c` writes
`platdir/core.img` and `platdir/core.efi`); imagecraft's flow wrote
neither, because it `grub-mkimage`s a BIOS `core.img` straight to a temp
path for the `dd` step and never touches the EFI platform dir. The result
is a silent failure mode: the new modules land in `/usr/lib/grub`, but
the on-disk bootloader is never refreshed, and nothing logs that it was
skipped.

### Approach

Reproduce the on-disk state `grub-install --uefi-secure-boot --no-nvram`
produces, without running `grub-install`:

**Removable-path fallback.** `_populate_esp_prime_dir` now also copies, on
a best-effort basis (surveying the shim-signed filenames at runtime, as it
already does for shim/grub), MokManager (`mmx64.efi`) to both `EFI/BOOT/`
and `EFI/ubuntu/`, the fallback `fbx64.efi` to `EFI/BOOT/`, and
`BOOTX64.CSV` to `EFI/ubuntu/`. If the fallback pair is absent a warning
is emitted rather than failing the build — matching `grub-install`, which
treats these as non-critical.

**Postinst gate files.** The in-chroot build (`_build_grub_in_chroot`)
now produces an `x86_64-efi` `core.efi` via `grub-mkimage` straight into
`/boot/grub/x86_64-efi/`, and a new `_stage_postinst_gate_files` helper
copies the BIOS `core.img` into `/boot/grub/i386-pc/`, creates the
`grubenv` env block (`grub-editenv … create`) that `grub-install` would,
and seeds the `grub-pc/cloud_style_installation` debconf flag so the BIOS
postinst reinstalls non-interactively against the boot disk (the same
flag the cloud hook sets). All of these run inside the chroot, so the
`/boot` bind-mount from obstacle 7 routes the platform dirs and `grubenv`
onto the boot partition's prime dir when `/boot` is separate.

The `core.efi` module set mirrors the BIOS `core.img` list but drops
`biosdisk` (which does not exist for `x86_64-efi`) and adds the EFI video
stack. That image is not what the signed-shim chain actually executes —
the signed `grubx64.efi` is — but it is a legitimate Secure-Boot-off
fallback and, more importantly, the file the EFI postinst gate checks for.

**Trade-offs.** The shim-signed companion filenames (`mmx64.efi` /
`fbx64.efi` vs. `.signed[.latest]` variants) drift across releases, so
they are surveyed at runtime; a release that renames them would silently
fall back to "no removable self-registration" (with the warning) rather
than failing loudly. The `cloud_style_installation` debconf seed assumes
the `grub-pc` package is the BIOS bootloader owner, matching cloud images;
it is harmless if that package is absent. As with obstacles 3, 4 and 7,
none of this is yet exercised by a real boot in CI — the removable
fallback chain in particular (shim → `fbx64.efi` → CSV → NVRAM entry →
`EFI/ubuntu`) is only verified by reasoning against the shim/grub sources
and unit tests over the staged file layout.

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
| `imagecraft/pack/grubutil.py`                  | Replaced `grub-install` + loop-mount flow with `grub-mkimage` + prime-dir chroot; added `grub-probe` divert + stub; added UUID pre-allocation; separate-`/boot` support: boot bind-mount, `/`-vs-`/boot` stub resolution, `grub-mkrelpath` divert (obstacle 7); EFI removable-path fallback (shim/`fbx64.efi`/`mmx64.efi`/`BOOTX64.CSV`), `x86_64-efi` `core.efi` + BIOS `core.img` postinst gate files, `grubenv`, cloud-style debconf (obstacle 8) |
| `imagecraft/pack/rawcontent.py`                | New module: bootloader-agnostic raw-content applier (`RawContent`, `apply_raw_content`)                                                                              |
| `imagecraft/services/pack.py`                  | Replaced `attach_images`/loop-path flow with offset-based format loop; calls `prepare_grub_assets` + `apply_raw_content`; detects a separate `/boot` and stamps its pre-allocated UUID at format time (obstacle 7) |
| `imagecraft/services/lifecycle.py`             | Replaced `CRAFT_VOLUME_<NAME>` loop-path vars with `_FILE`/`_OFFSET`/`_SIZE` triples                                                                                 |
| `imagecraft/pack/image.py`                     | Removed unreferenced loop-device machinery                                                                                                                           |
| `tests/unit/plugins/test_mmdebstrap_plugin.py` | Updated assertion for `--mode=unshare`                                                                                                                               |
| `tests/unit/pack/test_grubutil.py`             | Fully rewritten for the new flow; added coverage for the EFI removable-path fallback files, the missing-fallback warning, and `_stage_postinst_gate_files` (obstacle 8) |
| `tests/unit/pack/test_diskutil.py`             | Added tests for offset/uuid/FAT32 paths; added tests for `_gconv_env_prefix()`                                                                                       |
| `tests/unit/pack/test_rawcontent.py`           | New: tests for the generic applier                                                                                                                                   |
| `tests/unit/services/test_pack.py`             | Updated for new pack-service call sequence                                                                                                                           |
| `tests/unit/services/test_lifecycle.py`        | Updated for new env-var shape                                                                                                                                        |
