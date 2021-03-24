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

from craft_parts.dirs import ProjectDirs


def test_dirs(new_dir):
    dirs = ProjectDirs()
    assert dirs.work_dir == new_dir
    assert dirs.parts_dir == new_dir / "parts"
    assert dirs.stage_dir == new_dir / "stage"
    assert dirs.prime_dir == new_dir / "prime"


def test_dirs_work_dir(new_dir):
    dirs = ProjectDirs(work_dir="foobar")
    assert dirs.work_dir == new_dir / "foobar"
    assert dirs.parts_dir == new_dir / "foobar/parts"
    assert dirs.stage_dir == new_dir / "foobar/stage"
    assert dirs.prime_dir == new_dir / "foobar/prime"
