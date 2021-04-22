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

from typing import Dict, List, Set

import pytest

from craft_parts import common, plugins
from craft_parts.infos import PartInfo, ProjectInfo
from craft_parts.parts import Part


@pytest.fixture
def fake_check_output(mocker):
    return mocker.patch("subprocess.check_output")


class CustomPlugin(plugins.Plugin):
    """A test plugin."""

    properties_class = plugins.PluginProperties

    def get_build_snaps(self) -> Set[str]:
        return set()

    def get_build_packages(self) -> Set[str]:
        return {"pkg1", "pkg2"}

    def get_build_environment(self) -> Dict[str, str]:
        return {}

    def get_build_commands(self) -> List[str]:
        return []


def test_get_build_packages():
    p1 = Part(
        "p1",
        {
            "plugin": "custom",
            "source": "foo",
            "source-type": "tar",
            "build-packages": ["pkg3"],
        },
    )
    props = plugins.PluginProperties.unmarshal({})
    info = ProjectInfo()
    part_info = PartInfo(project_info=info, part=p1)
    plugin = CustomPlugin(properties=props, part_info=part_info)

    pkgs = common.get_build_packages(part=p1, plugin=plugin)
    assert pkgs == {"pkg1", "pkg2", "pkg3", "tar"}


def test_get_machine_manifest(
    mocker, fake_check_output
):  # pylint: disable=redefined-outer-name
    fake_check_output.return_value = (
        b"Linux 5.4.0-70-generic #78-Ubuntu SMP Fri Mar 19 13:29:52 UTC "
        b"2021 x86_64 x86_64 x86_64 GNU/Linux"
    )

    mocker.patch(
        "craft_parts.packages.Repository.get_installed_packages",
        return_value=["fake-pkg-2", "fake-pkg-1"],
    )
    mocker.patch(
        "craft_parts.packages.snaps.get_installed_snaps",
        return_value=["fake-snap-2", "fake-snap-1"],
    )

    assert common.get_machine_manifest() == {
        "uname": (
            "Linux 5.4.0-70-generic #78-Ubuntu SMP Fri Mar 19 13:29:52 UTC "
            "2021 x86_64 x86_64 x86_64 GNU/Linux"
        ),
        "installed-packages": ["fake-pkg-1", "fake-pkg-2"],
        "installed-snaps": ["fake-snap-1", "fake-snap-2"],
    }
