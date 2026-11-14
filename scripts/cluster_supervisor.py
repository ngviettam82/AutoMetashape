# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""AutoMetashape Cluster Supervisor CLI Facade.

CLI wrapper for running autonomous multi-dataset queue supervision and deliverable verification gates.
"""

from __future__ import annotations

import os
import sys

_SOURCE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "source"))
if _SOURCE_DIR not in sys.path:
    sys.path.insert(0, _SOURCE_DIR)

from autometashape.cluster.supervisor import (
    ClusterSupervisor,
    QueueJob,
    build_arg_parser,
    check_storage_headroom,
    main,
)

if __name__ == "__main__":
    sys.exit(main())
