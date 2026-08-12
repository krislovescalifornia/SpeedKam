<?php
/**
 * Shared SpeedKam off-site configuration.
 *
 * Required by BOTH speedkam_receiver.php (the camera's upload endpoint) and
 * speedkam_dashboard.php (the human web UI). Edit the values below once, then
 * upload all three PHP files together into the same folder on your host.
 *
 * This file only DEFINES values -- it prints nothing, so visiting it directly
 * in a browser shows a blank page and leaks nothing.
 */

// The shared secret the CAMERA authenticates uploads/heartbeats with. Must match
// backup.secret in the camera's config.yaml. Make it long and random.
$SECRET = 'CHANGE-ME-to-a-long-random-string';

// The password YOU type to open the dashboard in a browser. Keep it DIFFERENT
// from $SECRET. While this is left at the placeholder the dashboard refuses to
// load (so you can't accidentally publish your driveway footage unprotected).
$DASHBOARD_PASSWORD = 'CHANGE-ME-dashboard-password';

// Where backups live: events.csv + media/ + status.json + desired.json.
$DATA_DIR = __DIR__ . '/speedkam_data';

// File types the receiver is allowed to store / the dashboard to serve.
$ALLOWED_EXT = ['jpg' => 'image/jpeg', 'mp4' => 'video/mp4'];
