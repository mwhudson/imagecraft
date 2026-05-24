# This file is part of imagecraft.
#
# Copyright 2023-2025 Canonical Ltd.
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 3, as published
# by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranties of MERCHANTABILITY,
# SATISFACTORY QUALITY, or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Imagecraft Lifecycle service."""

import hashlib
from pathlib import Path
from typing import cast

import craft_platforms
from craft_application import LifecycleService
from craft_cli import CraftError
from craft_parts import Action, callbacks
from craft_parts.executor.errors import EnvironmentChangedError
from craft_parts.infos import ProjectInfo
from craft_parts.plugins import Plugin
from craft_parts.plugins.plugins import PluginGroup
from typing_extensions import override

from imagecraft import models, plugins
from imagecraft.pack import diskutil
from imagecraft.services.image import ImageService


class ImagecraftLifecycleService(LifecycleService):
    """Imagecraft-specific lifecycle service."""

    @staticmethod
    @override
    def get_plugin_group(
        build_info: craft_platforms.BuildInfo,
    ) -> dict[str, type[Plugin]] | None:
        return {**PluginGroup.MINIMAL.value, **plugins.get_app_plugins()}  # pyright: ignore[reportUnknownMemberType]

    @override
    def setup(self) -> None:
        """Initialize the LifecycleManager with previously-set arguments."""
        # Configure extra args to the LifecycleManager
        project = cast(models.Project, self._services.get("project").get())

        base_layer_name = "bare_base_layer"
        base_layer_dir = self._work_dir / Path(base_layer_name)
        base_layer_dir.mkdir(parents=True, exist_ok=True)
        hasher = hashlib.sha1()  # noqa: S324

        hasher.update(base_layer_name.encode())

        self._manager_kwargs.update(
            project_name=project.name,
            base_layer_dir=base_layer_dir,
            base_layer_hash=hasher.digest(),
            filesystem_mounts=project.filesystems,
        )

        super().setup()
        callbacks.register_prologue(self._prologue_hook)

    def _prologue_hook(self, project_info: ProjectInfo) -> None:
        """Create images and export file/offset/size triples as environment variables.

        For each volume and each partition within a volume, three variables are
        published so that parts can write to specific byte ranges of the disk
        image file directly (no loop device required):

        - ``CRAFT_VOLUME_<NAME>_FILE``: absolute path to the disk image
        - ``CRAFT_VOLUME_<NAME>_OFFSET``: byte offset of the region
        - ``CRAFT_VOLUME_<NAME>_SIZE``: byte size of the region

        ``<NAME>`` is the volume name for whole-disk entries, or
        ``<VOLUME>_<PARTITION>`` for per-partition entries.

        Use ``dd oflag=seek_bytes`` to seek by these byte offsets directly.
        """
        image_service = cast(ImageService, self._services.get("image"))
        image_service.create_images()

        project = cast(models.Project, self._services.get("project").get())
        env = project_info.global_environment

        for vol_name, image_path in image_service.get_images().items():
            volume = project.volumes[vol_name]

            # Whole-volume triple.
            vol_key = _env_key(vol_name)
            env[f"CRAFT_VOLUME_{vol_key}_FILE"] = str(image_path)
            env[f"CRAFT_VOLUME_{vol_key}_OFFSET"] = "0"
            env[f"CRAFT_VOLUME_{vol_key}_SIZE"] = str(image_path.stat().st_size)

            # Per-partition triples.
            partition_numbers = image_service._get_partition_numbers(volume)  # noqa: SLF001
            for structure_item in volume.structure:
                part_num = partition_numbers[structure_item.name]
                geometry = diskutil.get_partition_geometry(
                    imagepath=image_path,
                    partition_number=part_num,
                )
                part_key = f"{vol_key}_{_env_key(structure_item.name)}"
                env[f"CRAFT_VOLUME_{part_key}_FILE"] = str(image_path)
                env[f"CRAFT_VOLUME_{part_key}_OFFSET"] = str(
                    geometry.sector_offset * geometry.sector_size
                )
                env[f"CRAFT_VOLUME_{part_key}_SIZE"] = str(
                    geometry.sector_count * geometry.sector_size
                )

    @override
    def _exec(self, actions: list[Action]) -> None:
        """Execute actions of the lifecycle."""
        try:
            super()._exec(actions)
        except EnvironmentChangedError as err:
            raise CraftError(
                message="Partitions changed.",
                details=str(err),
                resolution="Run imagecraft clean",
            )


def _env_key(name: str) -> str:
    return name.upper().replace("/", "_").replace("-", "_")
