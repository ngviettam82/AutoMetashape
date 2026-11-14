# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""AutoMetashape Deliverable Verification CLI Facade.

CLI wrapper for running deliverable quality and integrity gates.
"""

from __future__ import annotations

import os
import sys

_SOURCE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "source"))
if _SOURCE_DIR not in sys.path:
    sys.path.insert(0, _SOURCE_DIR)

from autometashape.core.verification import (
    build_arg_parser,
    main,
    verify_geotiff,
    verify_glb_model,
    verify_obj_model,
    verify_pdf_report,
    verify_point_cloud,
    verify_project_deliverables,
)

if __name__ == "__main__":
    sys.exit(main())
