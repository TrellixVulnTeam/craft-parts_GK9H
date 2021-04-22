# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright 2021 Canonical Ltd.
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

"""Common helpers to be used the sequencer and the executor."""

import logging
import subprocess
import sys
from typing import Any, Dict, Set

from craft_parts import packages, sources
from craft_parts.parts import Part
from craft_parts.plugins import Plugin

logger = logging.getLogger(__name__)


def get_build_packages(*, part: Part, plugin: Plugin) -> Set[str]:
    """Obtain the list of build packages from part, source, and plugin.

    :param part: The part being processed.
    :param repository: The package repository to get packages from.
    :param plugin: The plugin handler for this part.

    :return: A set of all build packages needed to build this part.
    """
    all_packages: Set[str] = set()

    build_packages = part.spec.build_packages
    if build_packages:
        logger.debug("part build packages: %s", build_packages)
        all_packages |= set(build_packages)

    source = part.spec.source
    if source:
        source_type = part.spec.source_type
        if not source_type:
            source_type = sources.get_source_type_from_uri(source)

        source_build_packages = packages.Repository.get_packages_for_source_type(
            source_type
        )
        if source_build_packages:
            logger.debug("source build packages: %s", source_build_packages)
            all_packages |= set(source_build_packages)

    plugin_build_packages = plugin.get_build_packages()
    if plugin_build_packages:
        logger.debug("plugin build packages: %s", plugin_build_packages)
        all_packages |= set(plugin_build_packages)

    return all_packages


def get_build_snaps(*, part: Part, plugin: Plugin) -> Set[str]:
    """Obtain the list of build snaps from part and plugin.

    :param part: The part being processed.
    :param plugin: The plugin handler for this part.

    :return: A set of all snaps needed to build this part.
    """
    all_snaps: Set[str] = set()

    build_snaps = part.spec.build_snaps
    if build_snaps:
        logger.debug("part build snaps: %s", build_snaps)
        all_snaps |= set(build_snaps)

    plugin_build_snaps = plugin.get_build_snaps()
    if plugin_build_snaps:
        logger.debug("plugin build snaps: %s", plugin_build_snaps)
        all_snaps |= set(plugin_build_snaps)

    return all_snaps


def get_machine_manifest() -> Dict[str, Any]:
    """Obtain information about the system OS and runtime environment.

    :return: The machine manifest.
    """
    return {
        "uname": _get_system_info(),
        "installed-packages": sorted(packages.Repository.get_installed_packages()),
        "installed-snaps": sorted(packages.snaps.get_installed_snaps()),
    }


def _get_system_info() -> str:
    """Obtain running system information."""
    # Use subprocess directly here. common.run_output will use binaries out
    # of the snap, and we want to use the one on the host.
    try:
        output = subprocess.check_output(
            [
                "uname",
                "--kernel-name",
                "--kernel-release",
                "--kernel-version",
                "--machine",
                "--processor",
                "--hardware-platform",
                "--operating-system",
            ]
        )
    except subprocess.CalledProcessError as err:
        logger.warning(
            "'uname' exited with code %d: unable to record machine manifest",
            err.returncode,
        )
        return ""

    try:
        uname = output.decode(sys.getfilesystemencoding()).strip()
    except UnicodeEncodeError:
        logger.warning("Could not decode output for 'uname' correctly")
        uname = output.decode("latin-1", "surrogateescape").strip()

    return uname
