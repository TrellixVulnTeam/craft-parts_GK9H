# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright 2017-2021 Canonical Ltd.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License version 3 as published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Definitions and helpers for part handlers."""

import logging
import os
import os.path
import shutil
from glob import iglob
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

import yaml

from craft_parts import callbacks, errors, overlays, packages, plugins, sources
from craft_parts.actions import Action, ActionType
from craft_parts.infos import PartInfo, StepInfo
from craft_parts.overlays import LayerMounter, OverlayManager, PackageCacheMounter
from craft_parts.packages import errors as packages_errors
from craft_parts.parts import Part, has_overlay_visibility, parts_with_overlay
from craft_parts.plugins import Plugin
from craft_parts.state_manager import State, StepState, states
from craft_parts.steps import Step
from craft_parts.utils import file_utils, os_utils

from . import filesets
from .organize import organize_files
from .step_handler import StepContents, StepHandler, migrate_files

logger = logging.getLogger(__name__)


class PartHandler:
    """Handle lifecycle steps for a part.

    :param part: The part being processed.
    :param part_info: Information about the part being processed.
    :param part_list: A list containing all parts.
    """

    def __init__(
        self,
        part: Part,
        *,
        part_info: PartInfo,
        part_list: List[Part],
        overlay_manager: OverlayManager,
        ignore_patterns: Optional[List[str]] = None,
        base_layer_hash: bytes = b"",
    ):
        self._part = part
        self._part_info = part_info
        self._part_list = part_list
        self._overlay_manager = overlay_manager
        self._base_layer_hash = base_layer_hash

        self._plugin = plugins.get_plugin(
            part=part,
            properties=part.plugin_properties,
            part_info=part_info,
        )

        self._part_properties = part.spec.marshal()
        self._source_handler = sources.get_source_handler(
            cache_dir=part_info.cache_dir,
            part=part,
            project_dirs=part_info.dirs,
            ignore_patterns=ignore_patterns,
        )

        self.build_packages = _get_build_packages(part=self._part, plugin=self._plugin)
        self.build_snaps = _get_build_snaps(part=self._part, plugin=self._plugin)

    def run_action(self, action: Action) -> None:
        """Execute the given action for this part using a plugin.

        :param action: The action to execute.
        """
        step_info = StepInfo(self._part_info, action.step)

        if action.action_type == ActionType.UPDATE:
            self._update_action(action, step_info=step_info)
            return

        if action.action_type == ActionType.RERUN:
            for step in [action.step] + action.step.next_steps():
                self.clean_step(step=step)

        handler: Callable[[StepInfo], StepState]

        if action.step == Step.PULL:
            handler = self._run_pull
        elif action.step == Step.OVERLAY:
            handler = self._run_overlay
        elif action.step == Step.BUILD:
            handler = self._run_build
        elif action.step == Step.STAGE:
            handler = self._run_stage
        elif action.step == Step.PRIME:
            handler = self._run_prime
        else:
            raise RuntimeError(f"cannot run action for invalid step {action.step!r}")

        callbacks.run_pre_step(step_info)
        state = handler(step_info)
        state_file = states.state_file_path(self._part, action.step)
        state.write(state_file)
        callbacks.run_post_step(step_info)

    def _run_pull(self, step_info: StepInfo) -> StepState:
        _remove(self._part.part_src_dir)
        self._make_dirs()

        fetched_packages = self._fetch_stage_packages(step_info=step_info)
        fetched_snaps = self._fetch_stage_snaps()
        self._fetch_overlay_packages()

        self._run_step(
            step_info=step_info,
            scriptlet_name="override-pull",
            work_dir=self._part.part_src_dir,
        )

        state = states.PullState(
            part_properties=self._part_properties,
            project_options=step_info.project_options,
            assets={
                "stage-packages": fetched_packages,
                "stage-snaps": fetched_snaps,
                "source-details": getattr(self._source_handler, "source_details", None),
            },
        )

        return state

    def _run_overlay(self, step_info: StepInfo) -> StepState:
        self._make_dirs()

        # must be in a separate context manager to work around pychroot process leak
        with LayerMounter(self._overlay_manager, top_part=self._part) as ctx:
            self._install_overlay_packages(ctx)

        with LayerMounter(self._overlay_manager, top_part=self._part) as ctx:
            contents = self._run_step(
                step_info=step_info,
                scriptlet_name="overlay-script",
                work_dir=self._part.part_layer_dir,
            )

        # apply overlay filter
        overlay_fileset = filesets.Fileset(
            self._part.spec.overlay_files, name="overlay"
        )
        destdir = self._part.part_layer_dir
        files, dirs = filesets.migratable_filesets(overlay_fileset, str(destdir))
        _apply_file_filter(filter_files=files, filter_dirs=dirs, destdir=destdir)

        layer_hash = self._compute_layer_hash()
        overlays.save_layer_hash(self._part, hash_bytes=layer_hash)

        return states.OverlayState(
            part_properties=self._part_properties,
            project_options=step_info.project_options,
            files=contents.files,
            directories=contents.dirs,
        )

    def _compute_layer_hash(self, *, all_parts: bool = False) -> bytes:
        part_hash = self._base_layer_hash

        for part in self._part_list:
            part_hash = overlays.compute_layer_hash(part, part_hash)
            if not all_parts and part.name == self._part.name:
                break

        return part_hash

    def _run_build(self, step_info: StepInfo, *, update=False) -> StepState:
        self._make_dirs()
        _remove(self._part.part_build_dir)
        self._unpack_stage_packages()
        self._unpack_stage_snaps()

        # Copy source from the part source dir to the part build dir
        shutil.copytree(
            self._part.part_src_dir, self._part.part_build_dir, symlinks=True
        )

        # Perform the build step
        if has_overlay_visibility(self._part, part_list=self._part_list):
            with LayerMounter(self._overlay_manager, top_part=self._part):
                self._run_step(
                    step_info=step_info,
                    scriptlet_name="override-build",
                    work_dir=self._part.part_build_dir,
                )
        else:
            self._run_step(
                step_info=step_info,
                scriptlet_name="override-build",
                work_dir=self._part.part_build_dir,
            )

        # Organize the installed files as requested. We do this in the build step for
        # two reasons:
        #
        #   1. So cleaning and re-running the stage step works even if `organize` is
        #      used
        #   2. So collision detection takes organization into account, i.e. we can use
        #      organization to get around file collisions between parts when staging.
        #
        # If `update` is true, we give the snapcraft CLI permission to overwrite files
        # that already exist. Typically we do NOT want this, so that parts don't
        # accidentally clobber e.g. files brought in from stage-packages, but in the
        # case of updating build, we want the part to have the ability to organize over
        # the files it organized last time around. We can be confident that this won't
        # overwrite anything else, because to do so would require changing the
        # `organize` keyword, which will make the build step dirty and require a clean
        # instead of an update.
        self._organize(overwrite=update)

        assets = {
            "build-packages": self.build_packages,
            "build-snaps": self.build_snaps,
        }
        assets.update(_get_machine_manifest())

        overlay_hash = self._compute_layer_hash(all_parts=True)

        state = states.BuildState(
            part_properties=self._part_properties,
            project_options=step_info.project_options,
            assets=assets,
            overlay_hash=overlay_hash.hex(),
        )
        return state

    def _run_stage(self, step_info: StepInfo) -> StepState:
        self._make_dirs()

        contents = self._run_step(
            step_info=step_info,
            scriptlet_name="override-stage",
            work_dir=self._part.stage_dir,
        )

        self._stage_overlay()

        overlay_hash = self._compute_layer_hash(all_parts=True)

        state = states.StageState(
            part_properties=self._part_properties,
            project_options=step_info.project_options,
            files=contents.files,
            directories=contents.dirs,
            overlay_hash=overlay_hash.hex(),
        )
        return state

    def _run_prime(self, step_info: StepInfo) -> StepState:
        self._make_dirs()

        contents = self._run_step(
            step_info=step_info,
            scriptlet_name="override-prime",
            work_dir=self._part.prime_dir,
        )

        self._prime_overlay()

        state = states.PrimeState(
            part_properties=self._part_properties,
            project_options=step_info.project_options,
            files=contents.files,
            directories=contents.dirs,
        )
        return state

    def _run_step(
        self, *, step_info: StepInfo, scriptlet_name: str, work_dir: Path
    ) -> StepContents:
        """Run the scriptlet if overriding, otherwise run the built-in handler.

        :param step_info: Information about the step to execute.
        :param scriptlet_name: The name of this step's scriptlet.
        :param work_dir: The path to run the scriptlet on.

        :return: If step is Stage or Prime, return a tuple of sets containing
            the step's file and directory artifacts.
        """
        step_handler = StepHandler(
            self._part,
            step_info=step_info,
            plugin=self._plugin,
            source_handler=self._source_handler,
        )
        scriptlet = self._part.spec.get_scriptlet(step_info.step)
        if scriptlet:
            step_handler.run_scriptlet(
                scriptlet, scriptlet_name=scriptlet_name, work_dir=work_dir
            )
            return StepContents()

        return step_handler.run_builtin()

    def _update_action(self, action: Action, *, step_info: StepInfo) -> None:
        handler: Callable[[StepInfo], None]

        if action.step == Step.PULL:
            handler = self._update_pull
        elif action.step == Step.OVERLAY:
            handler = self._update_overlay
        elif action.step == Step.BUILD:
            handler = self._update_build
        else:
            step_name = action.step.name.lower()
            raise errors.InvalidAction(
                f"cannot update step {step_name!r} of {self._part.name!r}"
            )

        callbacks.run_pre_step(step_info)
        handler(step_info)
        state_file = states.state_file_path(self._part, action.step)
        state_file.touch()
        callbacks.run_post_step(step_info)

    def _update_pull(self, step_info: StepInfo) -> None:
        """Handle update action for the pull step.

        This handler is called if the pull step is outdated. In this case,
        invoke the source update method.

        :param step_info: The step information.
        """
        self._make_dirs()

        # if there's an override-pull scriptlet, execute it instead
        if self._part.spec.override_pull:
            self._run_step(
                step_info=step_info,
                scriptlet_name="override-pull",
                work_dir=self._part.part_src_dir,
            )
            return

        # the sequencer won't generate update actions for parts without
        # source, but they can be created manually
        if not self._source_handler:
            logger.warning(
                "Update requested on part %r without a source handler.",
                self._part.name,
            )
            return

        # the update action is sequenced only if an update is required and the
        # source knows how to update
        state_file = states.state_file_path(self._part, step_info.step)
        self._source_handler.check_if_outdated(str(state_file))
        self._source_handler.update()

    def _update_overlay(self, step_info: StepInfo) -> None:
        """Handle update action for the overlay step.

        The overlay update handler is empty (out of date overlay must not rerun,
        otherwise its state will be cleaned and build will run again instead of
        just updating).

        :param step_info: The step information.
        """

    def _update_build(self, step_info: StepInfo) -> None:
        """Handle update action for the build step.

        This handler is called if the build step is outdated. In this case,
        rebuild without cleaning the current build tree contents.

        :param step_info: The step information.
        """
        self._make_dirs()
        self._unpack_stage_packages()
        self._unpack_stage_snaps()

        if not self._plugin.out_of_source_build:
            # Use the local source to update. It's important to use
            # file_utils.copy instead of link_or_copy, as the build process
            # may modify these files
            source = sources.LocalSource(
                self._part.part_src_dir,
                self._part.part_build_dir,
                copy_function=file_utils.copy,
                cache_dir=step_info.cache_dir,
            )
            state_file = states.state_file_path(self._part, step_info.step)
            source.check_if_outdated(str(state_file))  # required by source.update()
            source.update()

        _remove(self._part.part_install_dir)

        self._run_step(
            step_info=step_info,
            scriptlet_name="override-build",
            work_dir=self._part.part_build_dir,
        )

        self._organize(overwrite=True)

    def _stage_overlay(self) -> None:
        """Migrate overlay files to stage and create state."""
        stage_overlay_file = self._overlay_state_file(Step.STAGE)
        if stage_overlay_file.exists():
            logger.debug("stage_overlay state exists")
            return

        if self._part not in parts_with_overlay(part_list=self._part_list):
            return

        logger.debug("staging overlay files")
        state = self._migrate_overlay(destdir=self._part.stage_dir)
        logger.debug("stage_overlay state: %s", state)
        state.write(stage_overlay_file)

    def _prime_overlay(self) -> None:
        """Migrate overlay files to prime and create state."""
        prime_overlay_file = self._overlay_state_file(Step.PRIME)
        if prime_overlay_file.exists():
            logger.debug("prime_overlay state exists")
            return

        if self._part not in parts_with_overlay(part_list=self._part_list):
            return

        logger.debug("priming overlay files")
        state = self._migrate_overlay(destdir=self._part.prime_dir)
        logger.debug("prime_overlay state: %s", state)
        state.write(prime_overlay_file)

    def _migrate_overlay(self, destdir: Path) -> State:
        """Migrate overlay files to prime and create state."""
        migratable_files: Set[str] = set()
        migratable_dirs: Set[str] = set()

        for part in reversed(self._part_list):
            if not part.has_overlay:
                continue

            logger.debug("migrate part %r overlay", part.name)
            srcdir = str(part.part_layer_dir)
            files, dirs = overlays.visible_in_layer(srcdir, destdir)
            migrate_files(
                files=files, dirs=dirs, srcdir=srcdir, destdir=str(destdir)
            )
            migratable_files |= files
            migratable_dirs |= dirs

        return State(files=migratable_files, directories=migratable_dirs)

    def clean_step(self, step: Step) -> None:
        """Remove the work files and the state of the given step.

        :param step: The step to clean.
        """
        logger.debug("clean %s:%s", self._part.name, step)

        handler: Callable[[], None]

        if step == Step.PULL:
            handler = self._clean_pull
        elif step == Step.OVERLAY:
            handler = self._clean_overlay
        elif step == Step.BUILD:
            handler = self._clean_build
        elif step == Step.STAGE:
            handler = self._clean_stage
        elif step == Step.PRIME:
            handler = self._clean_prime
        else:
            raise RuntimeError(
                f"Attempt to clean invalid step {step!r} in part {self._part!r}."
            )

        handler()
        states.remove(self._part, step)

    def _clean_pull(self) -> None:
        """Remove the current part's pull step files and state."""
        # remove dirs where stage packages and snaps are fetched
        _remove(self._part.part_packages_dir)
        _remove(self._part.part_snaps_dir)

        # remove the source tree
        _remove(self._part.part_src_dir)

    def _clean_overlay(self) -> None:
        # TODO: implement clean_overlay
        _remove(self._part.part_state_dir / "layer_hash")

    def _clean_build(self) -> None:
        """Remove the current part's build step files and state."""
        _remove(self._part.part_build_dir)
        _remove(self._part.part_install_dir)

    def _clean_stage(self) -> None:
        """Remove the current part's stage step files and state."""
        part_states = _load_part_states(Step.STAGE, self._part_list)
        _clean_shared_area(
            part_name=self._part.name,
            shared_dir=self._part.stage_dir,
            part_states=part_states,
        )

        # remove overlay data if this is the last staged part with overlay
        if len(_staged_parts_with_overlay(part_list=self._part_list)) == 1:
            _clean_shared_overlay(
                state_file=self._overlay_state_file(Step.STAGE),
                shared_dir=self._part.stage_dir,
                part_states=part_states,
            )

    def _clean_prime(self) -> None:
        """Remove the current part's prime step files and state."""
        part_states = _load_part_states(Step.PRIME, self._part_list)
        _clean_shared_area(
            part_name=self._part.name,
            shared_dir=self._part.prime_dir,
            part_states=part_states,
        )

        # remove overlay data if this is the last primed part with overlay
        if len(_staged_parts_with_overlay(part_list=self._part_list)) == 1:
            _clean_shared_overlay(
                state_file=self._overlay_state_file(Step.PRIME),
                shared_dir=self._part.prime_dir,
                part_states=part_states,
            )

    def _make_dirs(self):
        dirs = [
            self._part.part_src_dir,
            self._part.part_build_dir,
            self._part.part_install_dir,
            self._part.part_layer_dir,
            self._part.part_state_dir,
            self._part.part_run_dir,
            self._part.stage_dir,
            self._part.prime_dir,
        ]
        for dir_name in dirs:
            os.makedirs(dir_name, exist_ok=True)

    def _organize(self, *, overwrite=False):
        mapping = self._part.spec.organize_files
        organize_files(
            part_name=self._part.name,
            mapping=mapping,
            base_dir=self._part.part_install_dir,
            overwrite=overwrite,
        )

    def _fetch_stage_packages(self, *, step_info: StepInfo) -> Optional[List[str]]:
        stage_packages = self._part.spec.stage_packages
        if not stage_packages:
            return None

        try:
            fetched_packages = packages.Repository.fetch_stage_packages(
                cache_dir=step_info.cache_dir,
                package_names=stage_packages,
                target_arch=step_info.target_arch,
                base=step_info.base,
                stage_packages_path=self._part.part_packages_dir,
            )
        except packages_errors.PackageNotFound as err:
            raise errors.StagePackageNotFound(
                part_name=self._part.name, package_name=err.package_name
            )

        return fetched_packages

    def _fetch_stage_snaps(self):
        stage_snaps = self._part.spec.stage_snaps
        if not stage_snaps:
            return None

        packages.snaps.download_snaps(
            snaps_list=stage_snaps, directory=str(self._part.part_snaps_dir)
        )

        return stage_snaps

    def _fetch_overlay_packages(self) -> None:
        overlay_packages = self._part.spec.overlay_packages
        if not overlay_packages or not self._overlay_manager:
            return

        try:
            with PackageCacheMounter(self._overlay_manager) as ctx:
                ctx.fetch_packages(overlay_packages)
        except packages_errors.PackageNotFound as err:
            raise errors.OverlayPackageNotFound(
                part_name=self._part.name, package_name=err.package_name
            )

    def _install_overlay_packages(self, ctx: LayerMounter) -> None:
        overlay_packages = self._part.spec.overlay_packages
        if not overlay_packages:
            return

        ctx.install_packages(overlay_packages)

    def _unpack_stage_packages(self):
        packages.Repository.unpack_stage_packages(
            stage_packages_path=self._part.part_packages_dir,
            install_path=Path(self._part.part_install_dir),
        )

    def _unpack_stage_snaps(self):
        stage_snaps = self._part.spec.stage_snaps
        if not stage_snaps:
            return

        snaps_dir = self._part.part_snaps_dir
        install_dir = self._part.part_install_dir

        logger.debug("Unpacking stage-snaps to %s", install_dir)

        snap_files = iglob(os.path.join(snaps_dir, "*.snap"))
        snap_sources = (
            sources.SnapSource(
                source=s, part_src_dir=snaps_dir, cache_dir=self._part_info.cache_dir
            )
            for s in snap_files
        )

        for snap_source in snap_sources:
            snap_source.provision(str(install_dir), clean_target=False, keep=True)

    def _overlay_state_file(self, step: Step) -> Path:
        if step == Step.STAGE:
            return Path(self._part.overlay_dir / "stage_overlay")

        if step == Step.PRIME:
            return Path(self._part.overlay_dir / "prime_overlay")

        raise RuntimeError(f"no overlay migration state in step {step!r}")


def _remove(filename: Path) -> None:
    if filename.is_symlink() or filename.is_file():
        logger.debug("remove file %s", filename)
        os.unlink(filename)
    elif filename.is_dir():
        logger.debug("remove directory %s", filename)
        shutil.rmtree(filename)


def _clean_shared_area(
    *, part_name: str, shared_dir: Path, part_states: Dict[str, StepState]
) -> None:
    """Clean files added by a part to a shared directory.

    :param part_name: The name of the part that added the files.
    :param shared_dir: The shared directory to remove files from.
    :param part_states: A dictionary containing the each part's state for the
        step being processed.
    """
    # no state defined for this part, we won't remove files
    if part_name not in part_states:
        return

    state = part_states[part_name]
    files = state.files
    directories = state.directories

    # We want to make sure we don't remove a file or directory that's
    # being used by another part. So we'll examine the state for all parts
    # in the project and leave any files or directories found to be in
    # common.
    for other_name, other_state in part_states.items():
        if other_state and other_name != part_name:
            files -= other_state.files
            directories -= other_state.directories

    # Finally, clean the files and directories that are specific to this
    # part.
    _clean_migrated_files(files, directories, shared_dir)


def _clean_shared_overlay(
    *, state_file: Path, shared_dir: Path, part_states: Dict[str, StepState]
) -> None:
    # no state defined for staging overlay, we won't remove files
    if not state_file.is_file():
        return

    with open(state_file) as file:
        state_data = yaml.safe_load(file)
    state = State.unmarshal(state_data)
    files = state.files
    directories = state.directories

    # Don't remove entries that also belong to a part.
    for other_state in part_states.values():
        if other_state:
            files -= other_state.files
            directories -= other_state.directories

    _clean_migrated_files(files, directories, shared_dir)


def _clean_migrated_files(files: Set[str], dirs: Set[str], directory: Path) -> None:
    """Remove files and directories migrated from part install to a common directory.

    :param files: A set of files to remove.
    :param dirs: A set of directories to remove.
    :param directory: The path to remove files and directories from.
    """
    for each_file in files:
        try:
            Path(directory, each_file).unlink()
        except FileNotFoundError:
            logger.warning(
                "Attempted to remove file %r, but it didn't exist. Skipping...",
                each_file,
            )

    # Directories may not be ordered so that subdirectories come before
    # parents, and we want to be able to remove directories if possible, so
    # we'll sort them in reverse here to get subdirectories before parents.

    for each_dir in sorted(dirs, reverse=True):
        migrated_directory = os.path.join(directory, each_dir)
        try:
            if not os.listdir(migrated_directory):
                os.rmdir(migrated_directory)
        except FileNotFoundError:
            logger.warning(
                "Attempted to remove directory '%s', but it didn't exist. "
                "Skipping...",
                each_dir,
            )


def _apply_file_filter(
    *, filter_files: Set[str], filter_dirs: Set[str], destdir: Path
) -> None:
    for (root, directories, files) in os.walk(destdir, topdown=True):
        for file_name in files:
            path = Path(root, file_name)
            relpath = path.relative_to(destdir)
            if str(relpath) not in filter_files and not overlays.is_whiteout_file(path):
                logger.debug("delete file: %s", relpath)
                path.unlink()

        for directory in directories:
            path = Path(root, directory)
            relpath = path.relative_to(destdir)
            if path.is_symlink():
                if str(relpath) not in filter_files:
                    logger.debug("delete symlink: %s", relpath)
                    relpath.unlink()
            elif str(relpath) not in filter_dirs:
                logger.debug("delete dir: %s", relpath)
                # Don't descend into this directory-- we'll just delete it
                # entirely.
                directories.remove(directory)
                if path.is_dir():
                    shutil.rmtree(str(path))
                else:
                    path.unlink()


def _get_build_packages(*, part: Part, plugin: Plugin) -> List[str]:
    """Obtain the consolidated list of required build packages.

    The list of build packages include packages defined directly in
    the parts specification, packages required by the source handler,
    and packages required by the plugin.

    :param part: The part being processed.
    :param plugin: The plugin used in this part.

    :return: The list of build packages.
    """
    all_packages: List[str] = []

    build_packages = part.spec.build_packages
    if build_packages:
        logger.debug("part build packages: %s", build_packages)
        all_packages.extend(build_packages)

    source = part.spec.source
    if source:
        repo = packages.Repository
        source_type = sources.get_source_type_from_uri(source)
        source_build_packages = repo.get_packages_for_source_type(source_type)
        if source_build_packages:
            logger.debug("source build packages: %s", source_build_packages)
            all_packages.extend(source_build_packages)

    plugin_build_packages = plugin.get_build_packages()
    if plugin_build_packages:
        logger.debug("plugin build packages: %s", plugin_build_packages)
        all_packages.extend(plugin_build_packages)

    return all_packages


def _get_build_snaps(*, part: Part, plugin: Plugin) -> List[str]:
    """Obtain the consolidated list of required build snaps.

    The list of build snaps include snaps defined directly in the parts
    specification and snaps required by the plugin.

    :param part: The part being processed.
    :param plugin: The plugin used in this part.

    :return: The list of build snaps.
    """
    all_snaps: List[str] = []

    build_snaps = part.spec.build_snaps
    if build_snaps:
        logger.debug("part build snaps: %s", build_snaps)
        all_snaps.extend(build_snaps)

    plugin_build_snaps = plugin.get_build_snaps()
    if plugin_build_snaps:
        logger.debug("plugin build snaps: %s", plugin_build_snaps)
        all_snaps.extend(plugin_build_snaps)

    return all_snaps


def _get_machine_manifest() -> Dict[str, Any]:
    """Obtain information about the system OS and runtime environment."""
    return {
        "uname": os_utils.get_system_info(),
        "installed-packages": sorted(packages.Repository.get_installed_packages()),
        "installed-snaps": sorted(packages.snaps.get_installed_snaps()),
    }


def _load_part_states(step: Step, part_list: List[Part]) -> Dict[str, StepState]:
    """Return a dictionary of the state of the given step for all given parts.

    :param step: The step whose states should be loaded.
    :part_list: The list of parts whose states should be loaded.

    :return: A dictionary mapping part names to its state for the given step.
    """
    part_states: Dict[str, StepState] = {}
    for part in part_list:
        state = states.load_state(part, step)
        if state:
            part_states[part.name] = state
    return part_states


def _staged_parts_with_overlay(part_list: List[Part]) -> List[Part]:
    """Obtain a list of parts with overlay that have been staged."""
    oparts = parts_with_overlay(part_list=part_list)
    return [p for p in oparts if states.state_file_path(p, Step.STAGE).exists()]


def _migrate_whiteouts(
    omgr: OverlayManager, *, srcdir: Path, destdir: Path
) -> Set[str]:
    migratable_whiteouts: Set[str] = set()
    whiteouts: Set[str] = set()

    for (root, _, files) in os.walk(srcdir, topdown=True):
        for file_name in files:
            path = Path(root, file_name)
            relpath = path.relative_to(srcdir)
            if overlays.is_whiteout_file(path):
                logger.debug("whiteout file: %s", relpath)
                # migrate the whiteout file if it's visible from the top layer
                if omgr.is_path_visible(srcdir, relpath):
                    oci_relpath = overlays.oci_whiteout(relpath)
                    migratable_whiteouts.add(str(relpath))
                    whiteouts.add(str(oci_relpath))

    migrate_files(
        files=migratable_whiteouts, dirs=set(), srcdir=str(srcdir), destdir=str(destdir)
    )

    return whiteouts
