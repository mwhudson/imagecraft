# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
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
import pathlib

import pytest
from craft_cli import CraftError
from craft_parts.executor.errors import EnvironmentChangedError
from craft_parts.executor.part_handler import (
    PartHandler,
)
from imagecraft.services.lifecycle import ImagecraftLifecycleService


@pytest.mark.requires_root
def test_lifecycle_args(
    lifecycle_service: ImagecraftLifecycleService,
    mocker,
):
    lifecycle_service.setup()

    mocker.patch.object(PartHandler, "run_action", side_effect=EnvironmentChangedError)

    with pytest.raises(CraftError, match="Partitions changed"):
        lifecycle_service.run("pull")


def test_lifecycle_prologue_hook(
    lifecycle_service: ImagecraftLifecycleService,
):
    lifecycle_service.setup()

    project_info = lifecycle_service._lcm._project_info

    lifecycle_service._prologue_hook(project_info)

    env = project_info.global_environment

    # default_project_yaml has volume 'pc' with structures 'efi' and 'rootfs'.
    # Each key gets a FILE / OFFSET / SIZE triple in bytes.
    for key in ("PC", "PC_EFI", "PC_ROOTFS"):
        assert f"CRAFT_VOLUME_{key}_FILE" in env
        assert f"CRAFT_VOLUME_{key}_OFFSET" in env
        assert f"CRAFT_VOLUME_{key}_SIZE" in env

    # All three triples reference the same disk image file.
    disk_file = pathlib.Path(env["CRAFT_VOLUME_PC_FILE"])
    assert disk_file.exists()
    assert env["CRAFT_VOLUME_PC_EFI_FILE"] == str(disk_file)
    assert env["CRAFT_VOLUME_PC_ROOTFS_FILE"] == str(disk_file)

    # Whole-volume offset is always 0; size is the full file size.
    assert env["CRAFT_VOLUME_PC_OFFSET"] == "0"
    assert int(env["CRAFT_VOLUME_PC_SIZE"]) == disk_file.stat().st_size

    # Partition offsets are positive byte offsets inside the disk;
    # sizes are positive and fit within the disk.
    efi_offset = int(env["CRAFT_VOLUME_PC_EFI_OFFSET"])
    efi_size = int(env["CRAFT_VOLUME_PC_EFI_SIZE"])
    rootfs_offset = int(env["CRAFT_VOLUME_PC_ROOTFS_OFFSET"])
    rootfs_size = int(env["CRAFT_VOLUME_PC_ROOTFS_SIZE"])
    assert 0 < efi_offset < rootfs_offset
    assert efi_offset + efi_size <= rootfs_offset
    assert rootfs_offset + rootfs_size <= disk_file.stat().st_size
