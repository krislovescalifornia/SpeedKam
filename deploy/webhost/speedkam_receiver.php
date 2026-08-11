<?php
/**
 * SpeedKam off-site backup receiver.
 *
 * Drop this on your own web domain (e.g. https://yourdomain/speedkam/). The
 * camera POSTs each event here over HTTPS; this script mirrors the record and
 * its snapshot/clip into a data folder + a CSV, so you have an off-site copy
 * if the camera is stolen or damaged.
 *
 * SETUP
 *  1. Edit $SECRET below to a long random string, and set the SAME value in the
 *     camera's config.yaml (backup.secret).
 *  2. Upload this file. Make sure your host runs PHP and allows file uploads.
 *  3. For video clips, raise these in php.ini (or .htaccess/.user.ini):
 *        upload_max_filesize = 64M
 *        post_max_size       = 80M
 *  4. Point config.yaml backup.url at this file's URL and set enabled: true.
 *
 * The data folder (default ./speedkam_data) holds events.csv + media/. Keep it
 * private (the included .htaccess denies direct web browsing of it).
 */

// ===================== CONFIG =====================
$SECRET   = 'CHANGE-ME-to-a-long-random-string';     // must match config.yaml
$DATA_DIR = __DIR__ . '/speedkam_data';              // where backups are stored
$ALLOWED_EXT = ['jpg' => 'image/jpeg', 'mp4' => 'video/mp4'];
// ==================================================

header('Content-Type: application/json');

function fail($code, $msg) {
    http_response_code($code);
    echo json_encode(['ok' => false, 'error' => $msg]);
    exit;
}

// Recursively delete a directory; returns the number of files removed.
function rrmdir($dir) {
    $removed = 0;
    foreach (scandir($dir) as $entry) {
        if ($entry === '.' || $entry === '..') continue;
        $p = "$dir/$entry";
        if (is_dir($p)) {
            $removed += rrmdir($p);
        } else {
            if (@unlink($p)) $removed++;
        }
    }
    @rmdir($dir);
    return $removed;
}

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    fail(405, 'POST only');
}

// --- authenticate (constant-time) ---
$key = $_SERVER['HTTP_X_SPEEDKAM_KEY'] ?? '';
if ($SECRET === 'CHANGE-ME-to-a-long-random-string' || !hash_equals($SECRET, $key)) {
    fail(403, 'bad or missing key');
}

// --- remote rotation: delete off-site media older than N days ---
// Called by the camera's retention sweep (backup.remote_retention_days). Media
// is stored under media/<YYYY-MM-DD>/, so we drop whole day-folders past the
// cutoff. events.csv is tiny and kept, so remote counts survive.
if (($_POST['action'] ?? '') === 'prune') {
    $days = (int)($_POST['prune_older_than_days'] ?? 0);
    if ($days <= 0) { echo json_encode(['ok' => true, 'pruned' => 0]); exit; }
    $media_dir = "$DATA_DIR/media";
    $pruned = 0;
    $cutoff = time() - $days * 86400;
    if (is_dir($media_dir)) {
        foreach (scandir($media_dir) as $day) {
            if ($day === '.' || $day === '..') continue;
            $path = "$media_dir/$day";
            if (!is_dir($path)) continue;
            $ts = strtotime($day);              // folder name is YYYY-MM-DD
            if ($ts === false || $ts >= $cutoff) continue;
            $pruned += rrmdir($path);
        }
    }
    echo json_encode(['ok' => true, 'pruned' => $pruned]);
    exit;
}

// --- parse fields ---
$event_id = $_POST['event_id'] ?? '';
$meta_raw = $_POST['meta'] ?? '';
if ($event_id === '' || $meta_raw === '') {
    fail(400, 'missing event_id or meta');
}
$event_id = preg_replace('/[^A-Za-z0-9._-]/', '_', $event_id);
$meta = json_decode($meta_raw, true);
if (!is_array($meta)) {
    fail(400, 'meta is not valid JSON');
}

// --- ensure storage layout ---
$media_dir = "$DATA_DIR/media";
$index_dir = "$DATA_DIR/.index";
foreach ([$DATA_DIR, $media_dir, $index_dir] as $d) {
    if (!is_dir($d) && !mkdir($d, 0750, true) && !is_dir($d)) {
        fail(500, "cannot create $d");
    }
}
// Deny web browsing of the data dir (belt-and-braces alongside the .htaccess).
$guard = "$DATA_DIR/.htaccess";
if (!file_exists($guard)) {
    @file_put_contents($guard, "Require all denied\nOptions -Indexes\n");
}

// --- idempotency: if we already stored this event, succeed without dupes ---
$marker = "$index_dir/$event_id";
if (file_exists($marker)) {
    echo json_encode(['ok' => true, 'duplicate' => true]);
    exit;
}

// --- save uploaded files (snapshot, clip) ---
$saved = [];
$day = preg_replace('/[^0-9-]/', '', substr($meta['time'] ?? '', 0, 10)) ?: 'undated';
$dest_dir = "$media_dir/$day";
if (!is_dir($dest_dir) && !mkdir($dest_dir, 0750, true) && !is_dir($dest_dir)) {
    fail(500, 'cannot create media day dir');
}
foreach (['snapshot', 'clip'] as $field) {
    if (!isset($_FILES[$field]) || $_FILES[$field]['error'] !== UPLOAD_ERR_OK) {
        continue;
    }
    $name = basename($_FILES[$field]['name']);
    $name = preg_replace('/[^A-Za-z0-9._-]/', '_', $name);
    $ext  = strtolower(pathinfo($name, PATHINFO_EXTENSION));
    if (!isset($ALLOWED_EXT[$ext])) {
        fail(400, "disallowed file type: .$ext");
    }
    $target = "$dest_dir/$name";
    if (!move_uploaded_file($_FILES[$field]['tmp_name'], $target)) {
        fail(500, "could not store $field");
    }
    $saved[$field] = "media/$day/$name";
}

// --- append to CSV (write header once) ---
$csv = "$DATA_DIR/events.csv";
$cols = ['wall_time','track_id','speed_kmh','speed_mph','direction',
         'confidence','distance_m','vehicle_type','make','model','year',
         'color','captured','clip','snapshot','received_at'];
$fh = fopen($csv, 'a');
if ($fh === false) { fail(500, 'cannot open events.csv'); }
if (flock($fh, LOCK_EX)) {
    if (ftell($fh) === 0) { fputcsv($fh, $cols); }
    fputcsv($fh, [
        $meta['time'] ?? '',
        $meta['track_id'] ?? '',
        $meta['speed_kmh'] ?? '',
        $meta['speed_mph'] ?? '',
        $meta['direction'] ?? '',
        $meta['confidence'] ?? '',
        $meta['distance_m'] ?? '',
        $meta['vehicle_type'] ?? '',
        $meta['make'] ?? '',
        $meta['model'] ?? '',
        $meta['year'] ?? '',
        $meta['color'] ?? '',
        isset($meta['captured']) ? ($meta['captured'] ? 1 : 0) : '',
        $meta['clip'] ?? '',
        $meta['snapshot'] ?? '',
        gmdate('c'),
    ]);
    fflush($fh);
    flock($fh, LOCK_UN);
}
fclose($fh);

// mark processed
@file_put_contents($marker, gmdate('c'));

echo json_encode(['ok' => true, 'stored' => $saved]);
