# Copyright 2026 Canonical Ltd.
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

"""Service for creating and modifying the image."""

import pathlib
import shutil
from collections.abc import Mapping
from typing import cast

from craft_application import AppMetadata, AppService, ServiceFactory
from craft_cli import emit

from imagecraft.models import Project
from imagecraft.models.volume import (
    GPTVolume,
    HybridVolume,
    MBRVolume,
    PartitionSchema,
)
from imagecraft.pack import gptutil, mbrutil


class ImageService(AppService):
    """Service for accessing the final image file."""

    def __init__(
        self,
        app: AppMetadata,
        services: ServiceFactory,
        *,
        project_dir: pathlib.Path,
    ) -> None:
        super().__init__(app, services)
        self._project_dir = project_dir
        self._sector_size = gptutil.SECTOR_SIZE_512
        self._images: dict[str, pathlib.Path] | None = None

    def get_images(self) -> Mapping[str, pathlib.Path]:
        """Return the current mapping of volume names to image paths.

        :raises ValueError: If images have not been created yet.
        """
        if self._images is None:
            raise ValueError("Images must be created before they can be retrieved.")
        return self._images

    def create_images(self) -> Mapping[str, pathlib.Path]:
        """Create the image files on disk.

        This method creates the image files described by the volumes key in
        imagecraft.yaml. The images are partitioned, but the partitions are not
        formatted. This is the state of the images that will be available during the
        parts lifecycle.
        """
        if self._images is not None:
            return self._images

        project = cast(Project, self._services.get("project").get())
        self._images = {}

        for name, volume in project.volumes.items():
            # Use predictable hidden names for temporary images.
            image_path = self._project_dir / f".{name}.img.tmp"
            match volume.volume_schema:
                case PartitionSchema.GPT:
                    gptutil.create_empty_gpt_image(
                        imagepath=image_path,
                        sector_size=self._sector_size,
                        layout=cast(GPTVolume, volume),
                    )
                case PartitionSchema.MBR:
                    mbrutil.create_empty_mbr_image(
                        imagepath=image_path,
                        sector_size=self._sector_size,
                        layout=cast(MBRVolume, volume),
                    )
                case _:
                    # Reaching this case is a bug.
                    raise NotImplementedError(
                        f"Creating images with partition schema {volume.volume_schema} unimplemented."
                    )
            self._images[name] = image_path

        return self._images

    def _get_partition_numbers(
        self, volume: GPTVolume | MBRVolume | HybridVolume
    ) -> dict[str, int]:
        """Return a mapping of partition name to disk partition number for a volume.

        For GPT and plain MBR (≤4 partitions), numbers are 1-based positions,
        respecting any explicit partition_number on the structure item.
        For MBR with extended partitions (>4), the first 3 are primaries (1-3),
        slot 4 is the synthesised extended container, and logical partitions
        start at 5.
        """
        structure = volume.structure
        needs_extended = (
            volume.volume_schema == PartitionSchema.MBR
            and len(structure) > mbrutil.MAX_PRIMARY_SLOTS
        )
        result: dict[str, int] = {}
        for i, item in enumerate(structure, start=1):
            if needs_extended and i > mbrutil.PRIMARY_SLOTS_WITH_EXTENDED:
                # Skip slot 4 (extended container) — logicals start at 5
                part_num = i + 1
            else:
                part_num = getattr(item, "partition_number", None) or i
            result[item.name] = part_num
        return result

    def verify_images(self) -> None:
        """Verify the integrity of all created images."""
        if self._images is None:
            return

        project = cast(Project, self._services.get("project").get())
        for name, image_path in self._images.items():
            schema = project.volumes[name].volume_schema
            match schema:
                case PartitionSchema.GPT:
                    gptutil.verify_partition_tables(image_path)
                case PartitionSchema.MBR:
                    mbrutil.verify_partition_tables(image_path)

    def finalize_images(self, dest: pathlib.Path) -> Mapping[str, pathlib.Path]:
        """Move hidden image files to their final destination.

        Moves each .{name}.img.tmp to dest/{name}.img.

        :param dest: Directory to move the final images into.
        :returns: a Mapping of the image names to their paths.
        """
        images = dict(self.get_images())
        dest.mkdir(parents=True, exist_ok=True)
        for name, hidden_path in list(images.items()):
            final_path = dest / f"{name}.img"
            shutil.move(str(hidden_path), final_path)
            emit.debug(f"Finalized image {name!r} -> {final_path}")
            images[name] = final_path
        self._images = None
        return images
