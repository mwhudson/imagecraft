# Copyright 2025 Canonical Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Unit tests for the Phase-B (no-loop, no-image-mount) grub setup."""

import subprocess
import uuid as uuid_mod
from pathlib import Path

import pytest
from craft_platforms import DebianArchitecture
from imagecraft.errors import GRUBInstallError
from imagecraft.models.volume import (
    GPTVolume,
    MBRVolume,
)
from imagecraft.pack import grubutil
from imagecraft.pack.chroot import Mount
from imagecraft.pack.grubutil import (
    _GRUB_MKRELPATH_STUB,
    _GRUB_PROBE_STUB,
    GrubAssets,
    _ensure_chroot_mountpoints,
    _phase_b_chroot_mounts,
    grub_raw_content,
    install_grub_to_image,
    prepare_grub_assets,
    setup_grub,
)
from imagecraft.pack.image import Image
from imagecraft.pack.rawcontent import (
    MbrBootCode,
    PartitionStart,
    SectorOffset,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def gpt_volume_efi_rootfs():
    return GPTVolume.unmarshal(
        {
            "schema": "gpt",
            "structure": [
                {
                    "name": "efi",
                    "role": "system-boot",
                    "type": "C12A7328-F81F-11D2-BA4B-00A0C93EC93B",
                    "filesystem": "vfat",
                    "size": "256M",
                    "filesystem-label": "EFI System",
                },
                {
                    "name": "rootfs",
                    "role": "system-data",
                    "type": "0FC63DAF-8483-4772-8E79-3D69D8477DE4",
                    "filesystem": "ext4",
                    "size": "5G",
                    "filesystem-label": "writable",
                },
            ],
        }
    )


@pytest.fixture
def gpt_volume_with_bios_boot():
    return GPTVolume.unmarshal(
        {
            "schema": "gpt",
            "structure": [
                {
                    "name": "bios-boot",
                    "role": "system-boot",
                    "type": "21686148-6449-6E6F-744E-656564454649",
                    "filesystem": "vfat",
                    "size": "1M",
                    "filesystem-label": "bios",
                },
                {
                    "name": "efi",
                    "role": "system-boot",
                    "type": "C12A7328-F81F-11D2-BA4B-00A0C93EC93B",
                    "filesystem": "vfat",
                    "size": "256M",
                    "filesystem-label": "EFI System",
                },
                {
                    "name": "rootfs",
                    "role": "system-data",
                    "type": "0FC63DAF-8483-4772-8E79-3D69D8477DE4",
                    "filesystem": "ext4",
                    "size": "5G",
                    "filesystem-label": "writable",
                },
            ],
        }
    )


@pytest.fixture
def mbr_volume_boot_rootfs():
    return MBRVolume.unmarshal(
        {
            "schema": "mbr",
            "structure": [
                {
                    "name": "boot",
                    "role": "system-boot",
                    "type": "83",
                    "filesystem": "ext4",
                    "size": "512M",
                },
                {
                    "name": "rootfs",
                    "role": "system-data",
                    "type": "83",
                    "filesystem": "ext4",
                    "size": "5G",
                },
            ],
        }
    )


@pytest.fixture
def gpt_volume_rootfs_only():
    return GPTVolume.unmarshal(
        {
            "schema": "gpt",
            "structure": [
                {
                    "name": "rootfs",
                    "role": "system-data",
                    "type": "0FC63DAF-8483-4772-8E79-3D69D8477DE4",
                    "filesystem": "ext4",
                    "size": "5G",
                    "filesystem-label": "writable",
                },
            ],
        }
    )


def _make_prime_dirs(tmp_path: Path, volume_name: str, structures) -> dict[str, Path]:
    """Build a prime-dirs mapping under tmp_path, one subdir per structure."""
    out: dict[str, Path] = {}
    for s in structures:
        key = f"volume/{volume_name}/{s.name}"
        d = tmp_path / "primes" / s.name
        d.mkdir(parents=True, exist_ok=True)
        out[key] = d
    return out


def _populate_rootfs_with_grub_files(rootfs_prime: Path) -> None:
    """Drop dummy shim + signed grub + boot.img into the rootfs prime dir."""
    shim_dir = rootfs_prime / "usr/lib/shim"
    shim_dir.mkdir(parents=True, exist_ok=True)
    (shim_dir / "shimx64.efi.signed.latest").write_bytes(b"SHIMX64")

    grub_dir = rootfs_prime / "usr/lib/grub/x86_64-efi-signed"
    grub_dir.mkdir(parents=True, exist_ok=True)
    (grub_dir / "grubx64.efi.signed").write_bytes(b"GRUBX64")

    bios_dir = rootfs_prime / "usr/lib/grub/i386-pc"
    bios_dir.mkdir(parents=True, exist_ok=True)
    (bios_dir / "boot.img").write_bytes(b"X" * 512)


# ── _phase_b_chroot_mounts ────────────────────────────────────────────────────


def test_phase_b_chroot_mounts_uses_bind_for_dev():
    """The /dev mount must be a --bind, NOT devtmpfs (blocked in user ns)."""
    mounts = _phase_b_chroot_mounts()

    dev_mounts = [m for m in mounts if m._relative_mountpoint == "/dev"]
    assert len(dev_mounts) == 1
    dev = dev_mounts[0]
    assert dev._fstype is None, "devtmpfs is blocked in non-init user ns"
    assert dev._src == "/dev"
    assert dev._options == ["--bind"]


def test_phase_b_chroot_mounts_has_no_devtmpfs():
    """No mount in the phase-B set should request fstype devtmpfs."""
    mounts = _phase_b_chroot_mounts()
    fstypes = {m._fstype for m in mounts}
    assert "devtmpfs" not in fstypes


def test_phase_b_chroot_mounts_no_boot_bind_by_default():
    """Without a separate /boot, no /boot bind mount is staged."""
    mounts = _phase_b_chroot_mounts()
    assert not [m for m in mounts if m._relative_mountpoint == "/boot"]


def test_phase_b_chroot_mounts_binds_separate_boot(tmp_path):
    """A separate boot prime dir is bind-mounted at /boot, before /dev."""
    boot_prime = tmp_path / "boot-prime"
    mounts = _phase_b_chroot_mounts(boot_prime=boot_prime)

    boot_mounts = [m for m in mounts if m._relative_mountpoint == "/boot"]
    assert len(boot_mounts) == 1
    boot = boot_mounts[0]
    assert boot._fstype is None
    assert boot._src == str(boot_prime)
    assert boot._options == ["--bind"]
    # /boot must be mounted first so it is unmounted last (after /dev et al.).
    assert mounts[0] is boot


def test_ensure_chroot_mountpoints_creates_boot_when_requested(tmp_path):
    """``mount_boot`` makes /boot exist so the bind mount has a target."""
    _ensure_chroot_mountpoints(tmp_path, mount_boot=True)
    assert (tmp_path / "boot").is_dir()


def test_ensure_chroot_mountpoints_no_boot_by_default(tmp_path):
    """Without ``mount_boot`` we do not create /boot."""
    _ensure_chroot_mountpoints(tmp_path)
    assert not (tmp_path / "boot").exists()


# ── prepare_grub_assets: skip cases ───────────────────────────────────────────


@pytest.mark.parametrize(
    "arch",
    [
        DebianArchitecture.ARM64,
        DebianArchitecture.ARMHF,
        DebianArchitecture.RISCV64,
        DebianArchitecture.S390X,
    ],
)
def test_prepare_grub_assets_non_amd64_emits_todo(
    tmp_path, gpt_volume_efi_rootfs, emitter, arch
):
    prime_dirs = _make_prime_dirs(tmp_path, "pc", gpt_volume_efi_rootfs.structure)

    result = prepare_grub_assets(
        arch=arch,
        volume_name="pc",
        volume=gpt_volume_efi_rootfs,
        prime_dirs=prime_dirs,
        workdir=tmp_path / "wd",
    )

    assert result is None
    emitter.assert_progress(
        f"TODO: Phase B grub install not yet implemented for {arch}",
        permanent=True,
    )


def test_prepare_grub_assets_skips_when_no_data_partition(tmp_path, emitter):
    volume = GPTVolume.unmarshal(
        {
            "schema": "gpt",
            "structure": [
                {
                    "name": "efi",
                    "role": "system-boot",
                    "type": "C12A7328-F81F-11D2-BA4B-00A0C93EC93B",
                    "filesystem": "vfat",
                    "size": "256M",
                },
            ],
        }
    )
    prime_dirs = _make_prime_dirs(tmp_path, "pc", volume.structure)

    result = prepare_grub_assets(
        arch=DebianArchitecture.AMD64,
        volume_name="pc",
        volume=volume,
        prime_dirs=prime_dirs,
        workdir=tmp_path / "wd",
    )

    assert result is None
    emitter.assert_progress(
        "Skipping GRUB installation because no data partition was found",
        permanent=True,
    )


def test_prepare_grub_assets_skips_gpt_when_no_boot_partition(
    tmp_path, gpt_volume_rootfs_only, emitter
):
    prime_dirs = _make_prime_dirs(tmp_path, "pc", gpt_volume_rootfs_only.structure)

    result = prepare_grub_assets(
        arch=DebianArchitecture.AMD64,
        volume_name="pc",
        volume=gpt_volume_rootfs_only,
        prime_dirs=prime_dirs,
        workdir=tmp_path / "wd",
    )

    assert result is None
    emitter.assert_progress(
        "Skipping GRUB installation because no boot partition was found",
        permanent=True,
    )


# ── prepare_grub_assets: success cases ────────────────────────────────────────


def test_prepare_grub_assets_amd64_gpt_efi(mocker, tmp_path, gpt_volume_efi_rootfs):
    prime_dirs = _make_prime_dirs(tmp_path, "pc", gpt_volume_efi_rootfs.structure)
    rootfs_prime = prime_dirs["volume/pc/rootfs"]
    esp_prime = prime_dirs["volume/pc/efi"]
    _populate_rootfs_with_grub_files(rootfs_prime)
    # Simulate the chroot side-effect: a core.img gets created at
    # /tmp/imagecraft-core.img inside the chroot (which IS rootfs_prime).
    (rootfs_prime / "tmp").mkdir(parents=True, exist_ok=True)

    def fake_execute(
        *, target, core_prefix, rootfs_uuid, search_uuid, grub_prefix, boot_uuid
    ):
        # rootfs_uuid must be a valid UUID string.
        uuid_mod.UUID(rootfs_uuid)
        # Reproduce what _build_grub_in_chroot would create.
        (rootfs_prime / "tmp" / "imagecraft-core.img").write_bytes(b"CORE")
        # Also produce the kernel-listing grub.cfg that update-grub
        # would normally write.
        (rootfs_prime / "boot" / "grub").mkdir(parents=True, exist_ok=True)
        (rootfs_prime / "boot" / "grub" / "grub.cfg").write_text("menuentry...")
        # Sanity: core_prefix should point at the rootfs partition.
        assert "gpt2" in core_prefix
        # No separate /boot: search_uuid == rootfs_uuid, no boot UUID.
        assert search_uuid == rootfs_uuid
        assert boot_uuid is None
        assert grub_prefix == "($root)/boot/grub"

    mock_chroot_cls = mocker.patch("imagecraft.pack.grubutil.Chroot")
    mock_chroot_cls.return_value.execute.side_effect = fake_execute

    result = prepare_grub_assets(
        arch=DebianArchitecture.AMD64,
        volume_name="pc",
        volume=gpt_volume_efi_rootfs,
        prime_dirs=prime_dirs,
        workdir=tmp_path / "wd",
    )

    assert result is not None
    assert result.boot_img is not None
    assert result.boot_img.read_bytes() == b"X" * 512
    assert result.core_img is not None
    assert result.core_img.read_bytes() == b"CORE"
    # No ef02 in this layout — core.img goes elsewhere on disk.
    assert result.bios_boot_partition_name is None
    # A rootfs UUID must be pre-allocated and stored.
    assert result.rootfs_uuid is not None
    uuid_mod.UUID(result.rootfs_uuid)  # must be a valid UUID
    assert result.rootfs_partition_name == "rootfs"

    # ESP prime dir should now contain shim + signed grub + grub.cfg stub.
    assert (esp_prime / "EFI/BOOT/BOOTX64.EFI").read_bytes() == b"SHIMX64"
    assert (esp_prime / "EFI/ubuntu/shimx64.efi").read_bytes() == b"SHIMX64"
    assert (esp_prime / "EFI/ubuntu/grubx64.efi").read_bytes() == b"GRUBX64"
    assert (esp_prime / "EFI/ubuntu/grub.cfg").exists()
    esp_grub_cfg = (esp_prime / "EFI/ubuntu/grub.cfg").read_text()
    assert "configfile" in esp_grub_cfg
    # The ESP stub must use the pre-allocated UUID (not a label search).
    assert f"search.fs_uuid {result.rootfs_uuid} root" in esp_grub_cfg

    # Chroot must have been constructed with the rootfs prime dir as
    # its path (Phase-B: no image partition mount, no loop device).
    chroot_kwargs = mock_chroot_cls.call_args.kwargs
    assert chroot_kwargs["path"] == rootfs_prime
    # And with a /dev --bind mount, NOT a devtmpfs.
    dev_mount = next(
        m for m in chroot_kwargs["mounts"] if m._relative_mountpoint == "/dev"
    )
    assert dev_mount._fstype is None
    assert dev_mount._options == ["--bind"]


def test_prepare_grub_assets_amd64_gpt_with_bios_boot(
    mocker, tmp_path, gpt_volume_with_bios_boot
):
    prime_dirs = _make_prime_dirs(tmp_path, "pc", gpt_volume_with_bios_boot.structure)
    rootfs_prime = prime_dirs["volume/pc/rootfs"]
    _populate_rootfs_with_grub_files(rootfs_prime)

    def fake_execute(
        *, target, core_prefix, rootfs_uuid, search_uuid, grub_prefix, boot_uuid
    ):
        (rootfs_prime / "tmp").mkdir(parents=True, exist_ok=True)
        (rootfs_prime / "tmp" / "imagecraft-core.img").write_bytes(b"CORE")
        # rootfs is the third partition in this layout (bios-boot, efi,
        # rootfs) → core_prefix should reference gpt3.
        assert "gpt3" in core_prefix

    mock_chroot_cls = mocker.patch("imagecraft.pack.grubutil.Chroot")
    mock_chroot_cls.return_value.execute.side_effect = fake_execute

    result = prepare_grub_assets(
        arch=DebianArchitecture.AMD64,
        volume_name="pc",
        volume=gpt_volume_with_bios_boot,
        prime_dirs=prime_dirs,
        workdir=tmp_path / "wd",
    )

    assert result is not None
    # bios-boot partition is the ef02 target for core.img.
    assert result.bios_boot_partition_name == "bios-boot"


def test_prepare_grub_assets_amd64_mbr(mocker, tmp_path, mbr_volume_boot_rootfs):
    prime_dirs = _make_prime_dirs(tmp_path, "pc", mbr_volume_boot_rootfs.structure)
    rootfs_prime = prime_dirs["volume/pc/rootfs"]
    _populate_rootfs_with_grub_files(rootfs_prime)

    def fake_execute(
        *, target, core_prefix, rootfs_uuid, search_uuid, grub_prefix, boot_uuid
    ):
        (rootfs_prime / "tmp").mkdir(parents=True, exist_ok=True)
        (rootfs_prime / "tmp" / "imagecraft-core.img").write_bytes(b"CORE")
        # MBR prefix uses msdos<N>, not gpt<N>.
        assert "msdos2" in core_prefix

    mock_chroot_cls = mocker.patch("imagecraft.pack.grubutil.Chroot")
    mock_chroot_cls.return_value.execute.side_effect = fake_execute

    result = prepare_grub_assets(
        arch=DebianArchitecture.AMD64,
        volume_name="pc",
        volume=mbr_volume_boot_rootfs,
        prime_dirs=prime_dirs,
        workdir=tmp_path / "wd",
    )

    assert result is not None
    assert result.boot_img is not None
    assert result.core_img is not None
    assert result.bios_boot_partition_name is None  # MBR: gap, not ef02.


def test_prepare_grub_assets_missing_shim_raises(
    mocker, tmp_path, gpt_volume_efi_rootfs
):
    prime_dirs = _make_prime_dirs(tmp_path, "pc", gpt_volume_efi_rootfs.structure)
    rootfs_prime = prime_dirs["volume/pc/rootfs"]
    # Provide grub-signed and boot.img but NOT shim.
    grub_dir = rootfs_prime / "usr/lib/grub/x86_64-efi-signed"
    grub_dir.mkdir(parents=True, exist_ok=True)
    (grub_dir / "grubx64.efi.signed").write_bytes(b"GRUBX64")
    bios_dir = rootfs_prime / "usr/lib/grub/i386-pc"
    bios_dir.mkdir(parents=True, exist_ok=True)
    (bios_dir / "boot.img").write_bytes(b"X" * 512)

    def fake_execute(
        *, target, core_prefix, rootfs_uuid, search_uuid, grub_prefix, boot_uuid
    ):
        (rootfs_prime / "tmp").mkdir(parents=True, exist_ok=True)
        (rootfs_prime / "tmp" / "imagecraft-core.img").write_bytes(b"CORE")

    mock_chroot_cls = mocker.patch("imagecraft.pack.grubutil.Chroot")
    mock_chroot_cls.return_value.execute.side_effect = fake_execute

    with pytest.raises(GRUBInstallError, match="No signed shim binary"):
        prepare_grub_assets(
            arch=DebianArchitecture.AMD64,
            volume_name="pc",
            volume=gpt_volume_efi_rootfs,
            prime_dirs=prime_dirs,
            workdir=tmp_path / "wd",
        )


# ── grub_raw_content (policy) ─────────────────────────────────────────────────


def test_grub_raw_content_gpt_emits_mbr_and_ef02(tmp_path):
    """GPT with a BIOS-boot partition: boot.img → MBR, core.img → ef02."""
    boot_img = tmp_path / "boot.img"
    core_img = tmp_path / "core.img"
    assets = GrubAssets(
        boot_img=boot_img,
        core_img=core_img,
        bios_boot_partition_name="bios-boot",
    )

    items = grub_raw_content(assets)

    assert len(items) == 2
    assert items[0].source == boot_img
    assert items[0].target == MbrBootCode(max_bytes=440)
    assert items[1].source == core_img
    assert items[1].target == PartitionStart(
        partition_name="bios-boot", sector_size=512
    )


def test_grub_raw_content_mbr_emits_postmbr_gap(tmp_path):
    """No ef02 partition: core.img is placed in the post-MBR gap (sector 1)."""
    boot_img = tmp_path / "boot.img"
    core_img = tmp_path / "core.img"
    assets = GrubAssets(
        boot_img=boot_img,
        core_img=core_img,
        bios_boot_partition_name=None,
    )

    items = grub_raw_content(assets)

    assert len(items) == 2
    assert items[1].source == core_img
    assert items[1].target == SectorOffset(sector=1, sector_size=512)


def test_grub_raw_content_no_assets_is_empty():
    items = grub_raw_content(
        GrubAssets(boot_img=None, core_img=None, bios_boot_partition_name=None)
    )
    assert items == []


# ── install_grub_to_image (thin shim) ─────────────────────────────────────────


def test_install_grub_to_image_delegates_to_applier(
    mocker, tmp_path, gpt_volume_with_bios_boot
):
    """The compat shim feeds grub policy to the generic raw-content applier."""
    disk_path = tmp_path / "pc.img"
    disk_path.write_bytes(b"\x00" * 2048)
    image = Image(volume=gpt_volume_with_bios_boot, disk_path=disk_path)

    boot_img = tmp_path / "boot.img"
    boot_img.write_bytes(b"X" * 512)
    assets = GrubAssets(
        boot_img=boot_img,
        core_img=None,
        bios_boot_partition_name=None,
    )

    mock_apply = mocker.patch("imagecraft.pack.grubutil.rawcontent.apply_raw_content")

    install_grub_to_image(image=image, assets=assets)

    mock_apply.assert_called_once()
    kwargs = mock_apply.call_args.kwargs
    assert kwargs["disk_path"] == disk_path
    assert kwargs["contents"] == grub_raw_content(assets)


# ── setup_grub orchestration ──────────────────────────────────────────────────


def test_setup_grub_runs_prepare_then_install(mocker, tmp_path, gpt_volume_efi_rootfs):
    disk_path = tmp_path / "pc.img"
    disk_path.write_bytes(b"\x00" * 2048)
    image = Image(volume=gpt_volume_efi_rootfs, disk_path=disk_path)

    prime_dirs = _make_prime_dirs(tmp_path, "pc", gpt_volume_efi_rootfs.structure)

    fake_assets = GrubAssets(
        boot_img=tmp_path / "boot.img",
        core_img=tmp_path / "core.img",
        bios_boot_partition_name=None,
    )
    mock_prepare = mocker.patch(
        "imagecraft.pack.grubutil.prepare_grub_assets",
        return_value=fake_assets,
    )
    mock_install = mocker.patch("imagecraft.pack.grubutil.install_grub_to_image")

    setup_grub(
        image=image,
        workdir=tmp_path / "wd",
        arch=DebianArchitecture.AMD64,
        prime_dirs=prime_dirs,
        volume_name="pc",
    )

    mock_prepare.assert_called_once()
    mock_install.assert_called_once_with(image=image, assets=fake_assets)


def test_setup_grub_skips_install_when_prepare_returns_none(
    mocker, tmp_path, gpt_volume_efi_rootfs
):
    disk_path = tmp_path / "pc.img"
    disk_path.write_bytes(b"\x00" * 2048)
    image = Image(volume=gpt_volume_efi_rootfs, disk_path=disk_path)

    mocker.patch("imagecraft.pack.grubutil.prepare_grub_assets", return_value=None)
    mock_install = mocker.patch("imagecraft.pack.grubutil.install_grub_to_image")

    setup_grub(
        image=image,
        workdir=tmp_path / "wd",
        arch=DebianArchitecture.ARM64,  # would skip anyway
        prime_dirs={},
        volume_name="pc",
    )

    mock_install.assert_not_called()


# ── Backwards-compat exports ──────────────────────────────────────────────────


def test_module_exports_grub_assets_and_entry_points():
    """Public surface the rest of imagecraft depends on."""
    assert hasattr(grubutil, "GrubAssets")
    assert callable(grubutil.prepare_grub_assets)
    assert callable(grubutil.grub_raw_content)
    assert callable(grubutil.install_grub_to_image)
    assert callable(grubutil.setup_grub)


def test_mount_supports_bind():
    """The Mount class — unchanged in Phase B — supports --bind."""
    m = Mount(fstype=None, src="/dev", relative_mountpoint="/dev", options=["--bind"])
    assert m._options == ["--bind"]
    assert m._fstype is None


# ── Separate /boot partition ──────────────────────────────────────────────────


@pytest.fixture
def gpt_volume_efi_boot_rootfs():
    return GPTVolume.unmarshal(
        {
            "schema": "gpt",
            "structure": [
                {
                    "name": "efi",
                    "role": "system-boot",
                    "type": "C12A7328-F81F-11D2-BA4B-00A0C93EC93B",
                    "filesystem": "vfat",
                    "size": "256M",
                    "filesystem-label": "EFI System",
                },
                {
                    "name": "boot",
                    "role": "system-boot",
                    "type": "0FC63DAF-8483-4772-8E79-3D69D8477DE4",
                    "filesystem": "ext4",
                    "size": "512M",
                    "filesystem-label": "boot",
                },
                {
                    "name": "rootfs",
                    "role": "system-data",
                    "type": "0FC63DAF-8483-4772-8E79-3D69D8477DE4",
                    "filesystem": "ext4",
                    "size": "5G",
                    "filesystem-label": "writable",
                },
            ],
        }
    )


def test_prepare_grub_assets_separate_boot_partition(
    mocker, tmp_path, gpt_volume_efi_boot_rootfs
):
    """When /boot is a separate partition, ESP stub uses boot UUID and /grub prefix."""
    prime_dirs = _make_prime_dirs(tmp_path, "pc", gpt_volume_efi_boot_rootfs.structure)
    rootfs_prime = prime_dirs["volume/pc/rootfs"]
    esp_prime = prime_dirs["volume/pc/efi"]
    _populate_rootfs_with_grub_files(rootfs_prime)
    (rootfs_prime / "tmp").mkdir(parents=True, exist_ok=True)

    def fake_execute(
        *, target, core_prefix, rootfs_uuid, search_uuid, grub_prefix, boot_uuid
    ):
        uuid_mod.UUID(rootfs_uuid)
        uuid_mod.UUID(search_uuid)
        # search_uuid must differ from rootfs_uuid (it's the boot UUID)
        assert search_uuid != rootfs_uuid
        # the boot UUID is passed through for the grub-probe stub and equals
        # the search UUID for a separate /boot.
        assert boot_uuid == search_uuid
        # prefix points at boot partition (gpt2) with /grub path
        assert core_prefix == "(,gpt2)/grub"
        assert grub_prefix == "($root)/grub"
        (rootfs_prime / "tmp" / "imagecraft-core.img").write_bytes(b"CORE")
        (rootfs_prime / "boot" / "grub").mkdir(parents=True, exist_ok=True)
        (rootfs_prime / "boot" / "grub" / "grub.cfg").write_text("menuentry...")

    mock_chroot_cls = mocker.patch("imagecraft.pack.grubutil.Chroot")
    mock_chroot_cls.return_value.execute.side_effect = fake_execute

    result = prepare_grub_assets(
        arch=DebianArchitecture.AMD64,
        volume_name="pc",
        volume=gpt_volume_efi_boot_rootfs,
        prime_dirs=prime_dirs,
        workdir=tmp_path / "wd",
        boot_partition_name="boot",
    )

    assert result is not None
    assert result.rootfs_uuid is not None
    assert result.boot_uuid is not None
    assert result.boot_uuid != result.rootfs_uuid
    assert result.boot_partition_name == "boot"
    assert result.rootfs_partition_name == "rootfs"

    # ESP grub.cfg stub should use the boot UUID, not rootfs UUID.
    esp_grub_cfg = (esp_prime / "EFI/ubuntu/grub.cfg").read_text()
    assert f"search.fs_uuid {result.boot_uuid} root" in esp_grub_cfg
    assert "($root)/grub" in esp_grub_cfg
    # Should NOT reference rootfs UUID or /boot/grub in the stub.
    assert result.rootfs_uuid not in esp_grub_cfg
    assert "/boot/grub" not in esp_grub_cfg


# ── grub-probe / grub-mkrelpath stubs (executed as real shell scripts) ────────


ROOTFS_UUID = "11111111-1111-1111-1111-111111111111"
BOOT_UUID = "22222222-2222-2222-2222-222222222222"


def _run_probe_stub(tmp_path, boot_uuid, *args):
    """Materialise and run the grub-probe stub, returning its stdout."""
    script = tmp_path / "grub-probe"
    script.write_text(
        _GRUB_PROBE_STUB.replace("@@ROOTFS_UUID@@", ROOTFS_UUID).replace(
            "@@BOOT_UUID@@", boot_uuid
        )
    )
    script.chmod(0o755)
    return subprocess.run(
        ["sh", str(script), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_probe_stub_single_partition_collapses_to_rootfs(tmp_path):
    """With no boot UUID, / and /boot both resolve to the rootfs."""
    run = lambda *a: _run_probe_stub(tmp_path, "", *a)  # noqa: E731
    assert run("--target=device", "/") == "/dev/sda2"
    # /boot collapses to the rootfs device when /boot is not separate.
    assert run("--target=device", "/boot") == "/dev/sda2"
    assert run("--device", "/dev/sda2", "--target=fs_uuid") == ROOTFS_UUID
    # Even a /boot device query yields the rootfs UUID in this mode.
    assert run("--device", "/dev/sda1", "--target=fs_uuid") == ROOTFS_UUID


def test_probe_stub_separate_boot_resolves_distinct_devices(tmp_path):
    """With a boot UUID, / and /boot resolve to distinct devices/UUIDs."""
    run = lambda *a: _run_probe_stub(tmp_path, BOOT_UUID, *a)  # noqa: E731
    assert run("--target=device", "/") == "/dev/sda2"
    assert run("--target=device", "/boot") == "/dev/sda1"
    assert run("--target=device", "/boot/grub") == "/dev/sda1"
    # fs_uuid keyed on the device passed back by 10_linux.
    assert run("--device", "/dev/sda2", "--target=fs_uuid") == ROOTFS_UUID
    assert run("--device", "/dev/sda1", "--target=fs_uuid") == BOOT_UUID


def test_probe_stub_short_flags(tmp_path):
    """Short ``-t``/``-d`` forms behave like their long equivalents."""
    run = lambda *a: _run_probe_stub(tmp_path, BOOT_UUID, *a)  # noqa: E731
    assert run("-t", "device", "/boot") == "/dev/sda1"
    assert run("-d", "-t", "fs_uuid", "/dev/sda1") == BOOT_UUID
    assert run("-d", "-t", "fs_uuid", "/dev/sda2") == ROOTFS_UUID


def _run_mkrelpath_stub(tmp_path, *args):
    """Materialise and run the grub-mkrelpath stub, returning its stdout."""
    script = tmp_path / "grub-mkrelpath"
    script.write_text(_GRUB_MKRELPATH_STUB)
    script.chmod(0o755)
    return subprocess.run(
        ["sh", str(script), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_mkrelpath_stub_strips_boot_prefix(tmp_path):
    """Paths under /boot become boot-partition-relative; others unchanged."""
    assert _run_mkrelpath_stub(tmp_path, "/boot") == ""
    assert _run_mkrelpath_stub(tmp_path, "/boot/vmlinuz-6.8.0-1") == "/vmlinuz-6.8.0-1"
    assert _run_mkrelpath_stub(tmp_path, "/boot/grub") == "/grub"
    # Not under /boot: identity (matches the real tool in a single-fs chroot).
    assert _run_mkrelpath_stub(tmp_path, "/usr/share/grub/x.pf2") == (
        "/usr/share/grub/x.pf2"
    )
    # Flags are ignored; the path is still resolved.
    assert _run_mkrelpath_stub(tmp_path, "-r", "/boot/initrd.img") == "/initrd.img"
