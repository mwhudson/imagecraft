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

from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from craft_application import ServiceFactory
from imagecraft.models import Project, Volume
from imagecraft.models.volume import GPTStructureItem, MBRVolume, PartitionSchema
from imagecraft.services.image import ImageService


@pytest.fixture
def image_service(default_factory: ServiceFactory):
    return cast(ImageService, default_factory.get("image"))


@pytest.fixture
def project_dir(image_service: ImageService):
    return image_service._project_dir


@pytest.fixture
def mock_project():
    vol = MagicMock(spec=Volume)
    vol.volume_schema = PartitionSchema.GPT
    vol.structure = [
        MagicMock(spec=GPTStructureItem, name="efi", partition_number=None),
        MagicMock(spec=GPTStructureItem, name="rootfs", partition_number=2),
    ]
    vol.structure[0].name = "efi"
    vol.structure[1].name = "rootfs"

    project = MagicMock(spec=Project)
    project.volumes = {"pc": vol}
    return project


def test_get_images_uninitialized(image_service):
    with pytest.raises(
        ValueError, match="Images must be created before they can be retrieved"
    ):
        image_service.get_images()


def test_create_images_success(
    image_service, default_factory, mock_project, project_dir, mocker
):
    mocker.patch.object(
        default_factory.get("project"), "get", return_value=mock_project
    )

    with patch("imagecraft.pack.gptutil.create_empty_gpt_image") as mock_create:
        images = image_service.create_images()

        expected_path = project_dir / ".pc.img.tmp"
        assert images == {"pc": expected_path}
        assert image_service.get_images() == {"pc": expected_path}
        mock_create.assert_called_once()


def test_create_images_mbr(image_service, default_factory, project_dir, mocker):
    mbr_vol = MBRVolume.unmarshal(
        {
            "schema": "mbr",
            "structure": [
                {
                    "name": "boot",
                    "role": "system-boot",
                    "type": "83",
                    "filesystem": "ext4",
                    "size": "256M",
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
    mock_project = MagicMock(spec=Project)
    mock_project.volumes = {"pi": mbr_vol}
    mocker.patch.object(
        default_factory.get("project"), "get", return_value=mock_project
    )

    with patch("imagecraft.pack.mbrutil.create_empty_mbr_image") as mock_create:
        images = image_service.create_images()

        expected_path = project_dir / ".pi.img.tmp"
        assert images == {"pi": expected_path}
        mock_create.assert_called_once()


def test_create_images_idempotent(image_service, default_factory, mock_project, mocker):
    project_service = default_factory.get("project")
    mock_get = mocker.patch.object(project_service, "get", return_value=mock_project)

    with patch("imagecraft.pack.gptutil.create_empty_gpt_image"):
        first_call = image_service.create_images()
        second_call = image_service.create_images()

        assert first_call is second_call
        mock_get.assert_called_once()  # Only called once


def test_get_partition_numbers_gpt(image_service, mock_project):
    """GPT: numbers are 1-based positions, honouring explicit partition_number."""
    numbers = image_service._get_partition_numbers(mock_project.volumes["pc"])

    # efi has no explicit number -> position 1; rootfs has partition_number=2.
    assert numbers == {"efi": 1, "rootfs": 2}


def test_get_partition_numbers_mbr_plain(image_service):
    """MBR with ≤4 partitions: numbers are plain 1-based positions."""
    vol = MBRVolume.unmarshal(
        {
            "schema": "mbr",
            "structure": [
                {
                    "name": "boot",
                    "role": "system-boot",
                    "type": "83",
                    "filesystem": "ext4",
                    "size": "256M",
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

    numbers = image_service._get_partition_numbers(vol)

    assert numbers == {"boot": 1, "rootfs": 2}


def test_get_partition_numbers_mbr_extended(image_service):
    """MBR with >4 partitions: logical partitions start at 5, skipping slot 4."""
    vol = MBRVolume.unmarshal(
        {
            "schema": "mbr",
            "structure": [
                {
                    "name": "boot",
                    "role": "system-boot",
                    "type": "83",
                    "filesystem": "ext4",
                    "size": "256M",
                },
                {
                    "name": "p2",
                    "role": "system-boot",
                    "type": "83",
                    "filesystem": "ext4",
                    "size": "256M",
                },
                {
                    "name": "p3",
                    "role": "system-boot",
                    "type": "83",
                    "filesystem": "ext4",
                    "size": "256M",
                },
                {
                    "name": "logical1",
                    "role": "system-boot",
                    "type": "83",
                    "filesystem": "ext4",
                    "size": "256M",
                },
                {
                    "name": "logical2",
                    "role": "system-data",
                    "type": "83",
                    "filesystem": "ext4",
                    "size": "1G",
                },
            ],
        }
    )

    numbers = image_service._get_partition_numbers(vol)

    assert numbers == {
        "boot": 1,
        "p2": 2,
        "p3": 3,
        "logical1": 5,
        "logical2": 6,
    }


def test_verify_images_gpt(
    image_service, default_factory, mock_project, project_dir, mocker
):
    mocker.patch.object(
        default_factory.get("project"), "get", return_value=mock_project
    )
    image_service._images = {"pc": project_dir / ".pc.img.tmp"}

    with patch("imagecraft.pack.gptutil.verify_partition_tables") as mock_verify:
        image_service.verify_images()
        mock_verify.assert_called_once_with(project_dir / ".pc.img.tmp")


def test_verify_images_mbr(image_service, default_factory, project_dir, mocker):
    mbr_vol = MBRVolume.unmarshal(
        {
            "schema": "mbr",
            "structure": [
                {
                    "name": "boot",
                    "role": "system-boot",
                    "type": "83",
                    "filesystem": "ext4",
                    "size": "256M",
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
    mock_project = MagicMock(spec=Project)
    mock_project.volumes = {"pi": mbr_vol}
    mocker.patch.object(
        default_factory.get("project"), "get", return_value=mock_project
    )
    image_service._images = {"pi": project_dir / ".pi.img.tmp"}

    with patch("imagecraft.pack.mbrutil.verify_partition_tables") as mock_verify:
        image_service.verify_images()
        mock_verify.assert_called_once_with(project_dir / ".pi.img.tmp")


def test_finalize_images(image_service, project_dir, mocker):
    hidden = project_dir / ".pc.img.tmp"
    hidden.touch()
    image_service._images = {"pc": hidden}

    dest = project_dir / "dest"
    mock_move = mocker.patch("imagecraft.services.image.shutil.move")

    result = image_service.finalize_images(dest)

    final_path = dest / "pc.img"
    mock_move.assert_called_once_with(str(hidden), final_path)
    assert result == {"pc": final_path}
    assert dest.exists()


def test_finalize_images_multiple_volumes(image_service, project_dir, mocker):
    hidden_pc = project_dir / ".pc.img.tmp"
    hidden_rpi = project_dir / ".rpi.img.tmp"
    hidden_pc.touch()
    hidden_rpi.touch()
    image_service._images = {"pc": hidden_pc, "rpi": hidden_rpi}

    dest = project_dir / "dest"
    mock_move = mocker.patch("imagecraft.services.image.shutil.move")

    result = image_service.finalize_images(dest)

    mock_move.assert_any_call(str(hidden_pc), dest / "pc.img")
    mock_move.assert_any_call(str(hidden_rpi), dest / "rpi.img")
    assert mock_move.call_count == 2
    assert result == {"pc": dest / "pc.img", "rpi": dest / "rpi.img"}


def test_finalize_images_creates_dest(image_service, project_dir, mocker):
    hidden = project_dir / ".pc.img.tmp"
    image_service._images = {"pc": hidden}

    dest = project_dir / "nonexistent" / "nested" / "dest"
    mocker.patch("imagecraft.services.image.shutil.move")

    image_service.finalize_images(dest)

    assert dest.exists()
