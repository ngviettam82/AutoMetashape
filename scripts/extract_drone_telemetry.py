# Copyright (c) 2026, AETHER Project Developers.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

import os, glob, re
import numpy as np

images_dir = r"c:\Users\ADMIN\Documents\Code\GaussianSplat\data\toanha1\images"
image_files = sorted(glob.glob(os.path.join(images_dir, "*.JPG")))

lats, lons, alts, yaws, pitches = [], [], [], [], []

for p in image_files:
    with open(p, 'rb') as f:
        data = f.read(65536)
    m_lat = re.search(r'drone-dji:GpsLatitude="([^"]+)"', str(data))
    m_lon = re.search(r'drone-dji:GpsLongitude="([^"]+)"', str(data))
    m_alt = re.search(r'drone-dji:RelativeAltitude="([^"]+)"', str(data))
    m_yaw = re.search(r'drone-dji:GimbalYawDegree="([^"]+)"', str(data))
    m_pitch = re.search(r'drone-dji:GimbalPitchDegree="([^"]+)"', str(data))
    if m_lat and m_lon:
        lats.append(float(m_lat.group(1)))
        lons.append(float(m_lon.group(1)))
        alts.append(float(m_alt.group(1)) if m_alt else 0.0)
        yaws.append(float(m_yaw.group(1)) if m_yaw else 0.0)
        pitches.append(float(m_pitch.group(1)) if m_pitch else 0.0)

print(f"Extracted telemetry for {len(lats)} / {len(image_files)} images.")

# Approximate distance conversion: 1 deg lat ~ 111,320m, 1 deg lon ~ 111,320 * cos(lat)
lat_mean = np.mean(lats)
lon_mean = np.mean(lons)
x_meters = (np.array(lons) - lon_mean) * (111320.0 * np.cos(np.radians(lat_mean)))
y_meters = (np.array(lats) - lat_mean) * 111320.0
z_meters = np.array(alts)

print(f"Flight footprint: X span = {x_meters.max() - x_meters.min():.1f}m, Y span = {y_meters.max() - y_meters.min():.1f}m")
print(f"Altitude range: min = {z_meters.min():.1f}m, max = {z_meters.max():.1f}m, mean = {z_meters.mean():.1f}m")
print(f"Gimbal Pitch range: min = {min(pitches):.1f} deg, max = {max(pitches):.1f} deg")
