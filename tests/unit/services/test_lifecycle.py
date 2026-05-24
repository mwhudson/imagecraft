# This file is part of imagecraft.
#
# Copyright 2022-2025 Canonical Ltd.
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

from pathlib import Path
from unittest.mock import ANY, MagicMock

from craft_parts import LifecycleManager, ProjectVar, ProjectVarInfo, callbacks
from craft_parts.infos import ProjectInfo
from craft_platforms import DebianArchitecture
from imagecraft.services.lifecycle import ImagecraftLifecycleService


def test_lifecycle_args(
    lifecycle_service: ImagecraftLifecycleService,
    mocker,
    monkeypatch,
):
    mock_lifecycle = mocker.patch.object(
        LifecycleManager,
        "__init__",
        return_value=None,
    )

    lifecycle_service.setup()

    mock_lifecycle.assert_called_once_with(
        {
            "parts": {
                "my-part": {
                    "plugin": "nil",
                }
            }
        },
        application_name="imagecraft",
        arch=str(DebianArchitecture.from_host()),
        cache_dir=Path("cache"),
        work_dir=Path("work"),
        ignore_local_sources=[".craft"],
        ignore_outdated=[".craft"],
        parallel_build_count=ANY,  # Value will vary when tests run locally or in CI
        project_vars=ProjectVarInfo.unmarshal(
            {
                "version": ProjectVar(value="1.0"),
                "summary": ProjectVar(
                    value="default project", updated=False, part_name=None
                ),
                "description": ProjectVar(
                    value="default project", updated=False, part_name=None
                ),
            }
        ),
        track_stage_packages=True,
        partitions=["volume/pc/rootfs", "volume/pc/efi"],
        build_for="amd64",
        platform="amd64",
        project_name="default",
        base_layer_dir=Path("work/bare_base_layer"),
        base_layer_hash=b"i\x8e\x9c\xa4\x12\x1e\xe8\x97\xe4g\x08\xbc\x88\xddjb\x07\x8cp\xec",
        filesystem_mounts={
            "default": [
                {"device": "(volume/pc/rootfs)", "mount": "/"},
                {"device": "(volume/pc/efi)", "mount": "/boot/efi"},
            ]
        },
    )


def test_lifecycle_setup_registers_prologue(
    lifecycle_service: ImagecraftLifecycleService,
    mocker,
):
    mocker.patch.object(LifecycleManager, "__init__", return_value=None)
    mock_register = mocker.patch.object(callbacks, "register_prologue")

    lifecycle_service.setup()

    mock_register.assert_called_once_with(lifecycle_service._prologue_hook)


def test_lifecycle_prologue_hook(
    lifecycle_service: ImagecraftLifecycleService,
    mocker,
    tmp_path,
):
    from imagecraft.pack import diskutil

    image_path = tmp_path / "pc.img"
    image_path.write_bytes(b"\0" * 8192)  # 8 KiB stub disk

    mock_image_service = MagicMock()
    mock_image_service.get_images.return_value = {"pc": image_path}
    mock_image_service._get_partition_numbers.return_value = {"efi": 1, "rootfs": 2}

    mock_project = MagicMock()
    efi_item = MagicMock()
    efi_item.name = "efi"
    rootfs_item = MagicMock()
    rootfs_item.name = "rootfs"
    mock_project.volumes = {"pc": MagicMock(structure=[efi_item, rootfs_item])}

    def fake_service(name):
        return {"image": mock_image_service, "project": MagicMock(get=lambda: mock_project)}[name]

    mocker.patch.object(lifecycle_service._services, "get", side_effect=fake_service)

    geometries = {
        1: diskutil.PartitionGeometry(
            sector_offset=2048, sector_count=1024, sector_size=512
        ),
        2: diskutil.PartitionGeometry(
            sector_offset=3072, sector_count=2048, sector_size=512
        ),
    }
    mocker.patch(
        "imagecraft.services.lifecycle.diskutil.get_partition_geometry",
        side_effect=lambda *, imagepath, partition_number: geometries[partition_number],
    )

    project_info = MagicMock(spec=ProjectInfo)
    project_info.global_environment = {}

    lifecycle_service._prologue_hook(project_info)

    assert project_info.global_environment == {
        "CRAFT_VOLUME_PC_FILE": str(image_path),
        "CRAFT_VOLUME_PC_OFFSET": "0",
        "CRAFT_VOLUME_PC_SIZE": "8192",
        "CRAFT_VOLUME_PC_EFI_FILE": str(image_path),
        "CRAFT_VOLUME_PC_EFI_OFFSET": str(2048 * 512),
        "CRAFT_VOLUME_PC_EFI_SIZE": str(1024 * 512),
        "CRAFT_VOLUME_PC_ROOTFS_FILE": str(image_path),
        "CRAFT_VOLUME_PC_ROOTFS_OFFSET": str(3072 * 512),
        "CRAFT_VOLUME_PC_ROOTFS_SIZE": str(2048 * 512),
    }
    mock_image_service.create_images.assert_called_once()
