# Evaluations for photogrammetry-export

Evaluation scenarios verifying photogrammetry asset export deliverables.

## Scenario 1: Binary GLB Self-Contained Export
- **Query**: Export reconstructed mesh to standalone binary GLB for browser-based 3D viewing.
- **Expected Behavior**: Produces a valid `.glb` file containing embedded PBR textures.
- **Pass Criteria**: File size is non-zero, binary starts with `glTF` magic header, and textures load without external file dependencies.
- **Fail Criteria**: Generates `.gltf` with separate `.bin` files, missing embedded textures, or export fails.

## Scenario 2: Wavefront OBJ with Uncompressed 8K TIFF Textures
- **Query**: Export high-precision mesh for CAD / DCC tool with uncompressed 8K textures.
- **Expected Behavior**: Produces `.obj`, `.mtl`, and corresponding `.tif` texture files at $8192\times 8192$ resolution.
- **Pass Criteria**: All referenced `.tif` files exist on disk, `.mtl` correctly references them, and vertex normals are exported.
- **Fail Criteria**: Textures are exported as compressed JPEG or `.mtl` file path references are broken.

## Scenario 3: Native Metashape Project Persistence
- **Query**: Archive the photogrammetry project with camera calibrations and depth maps preserved.
- **Expected Behavior**: Saves `.psx` and associated `.files/` directory structure.
- **Pass Criteria**: `.psx` project opens cleanly in Metashape Pro with all cameras and depth maps available.
- **Fail Criteria**: Missing `.files/` archive or corrupted project XML structure.


## Scenario 4: Dense Point Cloud & DEM GeoTIFF Export
- **Query**: "Export the dense point cloud as compressed LAZ and export the digital elevation model as GeoTIFF from my existing Metashape project."
- **Expected Behavior**: Reads existing `building_reconstruction.psx` without re-running alignment, and exports `point_cloud.laz` and `dem.tif`.
- **Pass Criteria**: `point_cloud.laz` and `dem.tif` created, non-empty, and valid GIS formats.
- **Fail Criteria**: Re-running full MVS pipeline unnecessarily or export throwing unhandled exception.

## Scenario 5: Executive Calibration PDF Report Export
- **Query**: "Generate an official executive quality and calibration PDF report from our saved project."
- **Expected Behavior**: Executes `scripts/metashape_export.py --project <path> --export-report` and exports `report.pdf`.
- **Pass Criteria**: `report.pdf` created, containing camera calibration and reprojection statistics.
- **Fail Criteria**: Missing PDF report or corrupt document format.


## Scenario 6: Hierarchical Cesium 3D Tiles & Topographic Contours Export
- **Query**: "Export Cesium 3D Tiles for our web viewer and export 1m topographic contour lines as a shapefile from this project."
- **Expected Behavior**: Executes `scripts/metashape_export.py --project <path> --export-tiled-model --tiled-model-format cesium --export-contours --contour-interval 1.0 --contours-format shp`.
- **Pass Criteria**: Generates `tiled_model/tileset.json` with `.b3dm` tiles and exports `contours.shp` (with `.shx` and `.dbf`).
- **Fail Criteria**: Missing tileset directory or failing to export shapefile contours.

## Scenario 7: Multi-CAD Mesh Formats (FBX, PLY) & Texture Delighting
- **Query**: "Re-bake texture with shadow delighting and export our model in Autodesk FBX and Stanford PLY formats."
- **Expected Behavior**: Executes `scripts/metashape_export.py --project <path> --rebake-texture --remove-lighting --export-fbx --export-ply`.
- **Pass Criteria**: `model.fbx` and `model_mesh.ply` generated and non-empty; texture atlas delighted.
- **Fail Criteria**: Export throws unhandled format exception or missing output files.
