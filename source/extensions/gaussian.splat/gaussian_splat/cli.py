# Copyright (c) 2026, GaussianSplat Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Command-line interface for Gaussian Splatting & 3D Reconstruction."""

import argparse
import sys
from gaussian_splat.pipeline import reconstruct_3d_model


def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description="Reconstruct a textured 3D model from an image set.")
    parser.add_argument("--images", "-i", required=True, help="Directory containing input images.")
    parser.add_argument("--output", "-o", required=True, help="Output directory for 3D model and textures.")
    parser.add_argument("--colmap", default=None, help="Path to colmap.exe executable.")
    parser.add_argument("--threads", type=int, default=20, help="Number of worker threads.")
    parser.add_argument("--res", type=int, default=2048, help="Texture resolution.")

    args = parser.parse_args()
    success = reconstruct_3d_model(
        images_dir=args.images,
        output_dir=args.output,
        colmap_exe=args.colmap,
        num_threads=args.threads,
        texture_res=args.res,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
