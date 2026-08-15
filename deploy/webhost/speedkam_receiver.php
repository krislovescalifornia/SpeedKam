<?php
// SPDX-FileCopyrightText: 2026 Kris Kling
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * SpeedKam off-site backup receiver.
 *
 * Drop this on your own web domain (e.g. https://yourdomain/speedkam/). The
 * camera POSTs each event here over HTTPS; this script mirrors the record and
 * its snapshot/clip into a data folder + a CSV, so you have an off-site copy
 * if the camera is stolen or damaged.
 *
 * SETUP
 *  1. Copy speedkam_config.example.php to speedkam_config.php and set $SECRET
 *     (must match backup.secret in the camera's config.local.yaml) and
 *     $DASHBOARD_PASSWORD.
 *  2. Upload speedkam_config.php, this file, and speedkam_dashboard.php into the
 *     same folder. Make sure your host runs PHP and allows file uploads.
 *  3. For video clips, raise these in php.ini (or .htaccess/.user.ini):
 *        upload_max_filesize = 64M
 *        post_max_size       = 80M
 *  4. Point config.yaml backup.url at this file's URL and set enabled: true.
 *
 * The data folder (default ./speedkam_data) holds events.csv + media/. Keep it
 * private (the included .htaccess denies direct web browsing of it).
 */

require __DIR__ . '/speedkam_config.php';   // $SECRET, $DATA_DIR, $ALLOWED_EXT

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

// --- health check (GET) -----------------------------------------------------
// A human- and machine-readable "is this receiver alive and set up right?" page.
// Public on purpose (no secret needed) but leaks NOTHING sensitive: no secret,
// no event data, no counts. Visit the URL in a browser for a status page, or
// add ?format=json (or send Accept: application/json) for a monitoring probe.
if ($_SERVER['REQUEST_METHOD'] === 'GET') {
    $exists   = is_dir($DATA_DIR);
    $writable = $exists ? is_writable($DATA_DIR) : is_writable(dirname($DATA_DIR));
    $configured = ($SECRET !== 'CHANGE-ME-to-a-long-random-string' && $SECRET !== '');
    $ok = ($writable && $configured);
    $health = [
        'ok'         => $ok,
        'service'    => 'speedkam-receiver',
        'time'       => gmdate('c'),
        'php'        => PHP_VERSION,
        'writable'   => $writable,
        'configured' => $configured,
        'limits'     => [
            'upload_max_filesize' => ini_get('upload_max_filesize'),
            'post_max_size'       => ini_get('post_max_size'),
        ],
    ];
    $wants_json = (($_GET['format'] ?? '') === 'json')
        || strpos($_SERVER['HTTP_ACCEPT'] ?? '', 'application/json') !== false;
    if ($wants_json) {
        // header() is already application/json from above.
        http_response_code($ok ? 200 : 503);
        echo json_encode($health);
        exit;
    }
    header('Content-Type: text/html; charset=utf-8');
    http_response_code($ok ? 200 : 503);
    $yn = function ($b) {
        return $b
            ? '<span class="ok">&#10003; yes</span>'
            : '<span class="bad">&#10007; no</span>';
    };
    $status_word  = $ok ? 'Online' : 'Needs attention';
    $status_class = $ok ? 'ok' : 'bad';
    echo '<!doctype html><html lang="en"><head><meta charset="utf-8">'
       . '<meta name="viewport" content="width=device-width,initial-scale=1">'
       . '<title>SpeedKam backup &mdash; ' . htmlspecialchars($status_word) . '</title>'
       . '<style>'
       . ':root{color-scheme:light dark}'
       . 'body{font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;'
       . 'margin:0;padding:2rem;display:flex;justify-content:center;'
       . 'background:#0b0f14;color:#e6edf3}'
       . '.card{max-width:560px;width:100%;background:#111823;border:1px solid #223;'
       . 'border-radius:14px;padding:1.5rem 1.75rem;box-shadow:0 8px 30px rgba(0,0,0,.3)}'
       . 'h1{margin:.2rem 0 1rem;font-size:1.3rem;display:flex;align-items:center;gap:.6rem}'
       . '.dot{width:.7rem;height:.7rem;border-radius:50%;display:inline-block}'
       . '.ok{color:#3fb950}.bad{color:#f85149}'
       . '.dot.ok{background:#3fb950}.dot.bad{background:#f85149}'
       . 'table{width:100%;border-collapse:collapse;margin:.5rem 0}'
       . 'td{padding:.5rem .25rem;border-top:1px solid #223}'
       . 'td:last-child{text-align:right;font-variant-numeric:tabular-nums}'
       . '.muted{color:#8b949e;font-size:.85rem;margin-top:1rem}'
       . 'code{background:#0b0f14;padding:.1rem .35rem;border-radius:6px}'
       . '</style></head><body><div class="card">'
       . '<h1><span class="dot ' . $status_class . '"></span>SpeedKam backup receiver &mdash; '
       . '<span class="' . $status_class . '">' . htmlspecialchars($status_word) . '</span></h1>'
       . '<table>'
       . '<tr><td>Storage writable</td><td>' . $yn($writable) . '</td></tr>'
       . '<tr><td>Secret configured</td><td>' . $yn($configured) . '</td></tr>'
       . '<tr><td>Max upload size</td><td><code>'
       . htmlspecialchars(ini_get('upload_max_filesize')) . '</code></td></tr>'
       . '<tr><td>Max POST size</td><td><code>'
       . htmlspecialchars(ini_get('post_max_size')) . '</code></td></tr>'
       . '<tr><td>PHP</td><td><code>' . htmlspecialchars(PHP_VERSION) . '</code></td></tr>'
       . '<tr><td>Server time (UTC)</td><td>' . htmlspecialchars(gmdate('c')) . '</td></tr>'
       . '</table>'
       . '<p class="muted">This endpoint receives camera uploads over HTTPS (POST). '
       . 'A camera authenticates with a shared key; this page never exposes it or '
       . 'any recorded data. Machine-readable status: <code>?format=json</code>.</p>'
       . '</div></body></html>';
    exit;
}

if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    fail(405, 'POST only');
}

// --- authenticate (constant-time) ---
$key = $_SERVER['HTTP_X_SPEEDKAM_KEY'] ?? '';
if ($SECRET === 'CHANGE-ME-to-a-long-random-string' || !hash_equals($SECRET, $key)) {
    fail(403, 'bad or missing key');
}

// --- fleet multi-tenancy: bucket every node under its own data dir -----------
// One shared receiver URL + secret serves the whole fleet. Each camera sends a
// stable node id (src/speedkam/identity.py, derived from its CPU serial), and we
// route its storage into $DATA_DIR/nodes/<id>/ -- auto-created, no per-node setup
// on the card. The id is stripped to [A-Za-z0-9_-] (no dots) so it can never be a
// path-traversal token. No node id -> legacy single-node mode ($DATA_DIR as-is).
$node = substr(preg_replace('/[^A-Za-z0-9_-]/', '', $_POST['node'] ?? ''), 0, 32);
if ($node !== '') {
    $DATA_DIR = rtrim($DATA_DIR, '/') . '/nodes/' . $node;
}

// --- camera heartbeat + settings pull (pull-based remote control) ---
// The camera POSTs its status here every control.poll_seconds. We store the
// status so the dashboard can show liveness + counts even when you're off the
// camera's LAN, and we return the DESIRED settings the operator set on the
// dashboard. The camera applies them on its next poll -- this is how control
// reaches a camera behind home NAT without any inbound ports.
if (($_POST['action'] ?? '') === 'sync') {
    if (!is_dir($DATA_DIR) && !mkdir($DATA_DIR, 0750, true) && !is_dir($DATA_DIR)) {
        fail(500, 'cannot create data dir');
    }
    $status = json_decode($_POST['status'] ?? '', true);
    if (!is_array($status)) { $status = []; }
    $status['received_at'] = gmdate('c');   // server-stamped, for liveness
    @file_put_contents("$DATA_DIR/status.json", json_encode($status));
    // Hand back whatever the dashboard has queued (empty until first change).
    $desired = [];
    $dfile = "$DATA_DIR/desired.json";
    if (is_file($dfile)) {
        $d = json_decode(@file_get_contents($dfile), true);
        if (is_array($d)) { $desired = $d; }
    }
    echo json_encode(['ok' => true, 'settings' => $desired]);
    exit;
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

// --- deferred recognition write-back: fill attributes on an existing row ---
// Called by tools/recognize_worker.py after it runs YOLO off-box. Matches the
// event by the stem of its clip (or snapshot) filename and rewrites only the
// attribute columns (vehicle_type/make/model/year/color) in events.csv.
if (($_POST['action'] ?? '') === 'enrich') {
    $event_id = preg_replace('/[^A-Za-z0-9._-]/', '_', $_POST['event_id'] ?? '');
    $attrs = json_decode($_POST['attrs'] ?? '', true);
    if ($event_id === '' || !is_array($attrs)) {
        fail(400, 'missing event_id or attrs');
    }
    $csv = "$DATA_DIR/events.csv";
    if (!is_file($csv)) { echo json_encode(['ok' => true, 'updated' => 0]); exit; }
    $ATTR_COLS = ['vehicle_type', 'make', 'model', 'year', 'color'];
    $updated = 0;
    $fh = fopen($csv, 'c+');
    if ($fh === false) { fail(500, 'cannot open events.csv'); }
    if (flock($fh, LOCK_EX)) {
        $rows = [];
        $header = null;
        while (($r = fgetcsv($fh)) !== false) {
            if ($header === null) { $header = $r; $rows[] = $r; continue; }
            $rows[] = $r;
        }
        if ($header !== null) {
            $idx = array_flip($header);
            $clip_i = $idx['clip'] ?? null;
            $snap_i = $idx['snapshot'] ?? null;
            foreach ($rows as $ri => &$row) {
                if ($ri === 0) continue;  // header
                $stem = null;
                if ($clip_i !== null && !empty($row[$clip_i])) {
                    $stem = pathinfo($row[$clip_i], PATHINFO_FILENAME);
                } elseif ($snap_i !== null && !empty($row[$snap_i])) {
                    $stem = pathinfo($row[$snap_i], PATHINFO_FILENAME);
                }
                if ($stem !== $event_id) continue;
                foreach ($ATTR_COLS as $col) {
                    if (isset($attrs[$col]) && $attrs[$col] !== ''
                        && isset($idx[$col])) {
                        $row[$idx[$col]] = $attrs[$col];
                    }
                }
                $updated++;
            }
            unset($row);
            if ($updated > 0) {
                ftruncate($fh, 0);
                rewind($fh);
                foreach ($rows as $row) { fputcsv($fh, $row); }
                fflush($fh);
            }
        }
        flock($fh, LOCK_UN);
    }
    fclose($fh);
    echo json_encode(['ok' => true, 'updated' => $updated]);
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
