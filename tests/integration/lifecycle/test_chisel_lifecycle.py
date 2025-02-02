# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright 2022 Canonical Ltd.
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
import os
import textwrap
from pathlib import Path

import pytest
import yaml

import craft_parts
from craft_parts import Step

IS_CI: bool = os.getenv("CI") == "true"


@pytest.mark.skipif(not IS_CI, reason="This test needs 'chisel' and only runs on CI.")
def test_chisel_lifecycle(new_dir):
    """Integrated test for Chisel support.

    Note that since this test needs the "chisel" binary, it currently only runs on CI.
    """
    _parts_yaml = textwrap.dedent(
        """\
        parts:
          foo:
            plugin: nil
            stage-packages: [ca-certificates_data]
        """
    )

    parts = yaml.safe_load(_parts_yaml)

    lf = craft_parts.LifecycleManager(
        parts, application_name="test_slice", cache_dir=new_dir, work_dir=new_dir
    )

    actions = lf.plan(Step.PRIME)
    with lf.action_executor() as ctx:
        ctx.execute(actions)

    root = Path(new_dir)
    assert (root / "prime/etc/ssl/certs/ca-certificates.crt").is_file()
    assert (root / "prime/usr/share/ca-certificates").is_dir()
