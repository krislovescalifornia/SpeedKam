<?php
// SPDX-FileCopyrightText: 2026 Kris Kling
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * SpeedKam off-site dashboard -- the human web UI.
 *
 * A password-gated page that reads the mirrored records (events.csv + media/ +
 * status.json) your camera backs up here, so you can see everything from
 * anywhere -- and it still works if the camera itself is stolen or damaged.
 *
 * It mirrors the on-Pi dashboard's analytics (colour breakdown, average speeds,
 * day/hour charts, speed distribution) and its report builder, and it excludes
 * auto-rejected false positives (cyclists, pedestrians, phantom tracks) from
 * every stat while still listing them in a review bin you can restore from.
 *
 * It also offers BASIC REMOTE CONTROL: change the SpeedKapture threshold /
 * speed limit here and it's written to desired.json; the camera pulls that on
 * its next check-in and applies it. Control reaches a camera behind home NAT
 * because the camera reaches out -- nothing reaches in.
 *
 * SETUP: set $DASHBOARD_PASSWORD in speedkam_config.php, upload all three PHP
 * files together, then open this file's URL in a browser.
 */

require __DIR__ . '/speedkam_config.php';   // $SECRET, $DASHBOARD_PASSWORD, $DATA_DIR, $ALLOWED_EXT

session_start();

$configured = ($DASHBOARD_PASSWORD !== 'CHANGE-ME-dashboard-password'
    && $DASHBOARD_PASSWORD !== '');

// --- fleet mode: which camera are we viewing? -------------------------------
$BASE_DIR   = rtrim($DATA_DIR, '/');
$nodes_root = "$BASE_DIR/nodes";
$sel_node   = substr(preg_replace('/[^A-Za-z0-9_-]/', '', $_GET['node'] ?? ''), 0, 32);
if ($sel_node !== '' && is_dir("$nodes_root/$sel_node")) {
    $DATA_DIR = "$nodes_root/$sel_node";
}

function is_authed() { return !empty($_SESSION['speedkam_auth']); }
function self_path() { return strtok($_SERVER['REQUEST_URI'], '?'); }

// --- login rate limiting ----------------------------------------------------
const LOGIN_MAX_FAILS = 5;
const LOGIN_WINDOW    = 900;
const LOGIN_LOCKOUT   = 900;

function login_client_ip() { return $_SERVER['REMOTE_ADDR'] ?? 'unknown'; }

function login_locked_seconds($dir, $ip) {
    $file = "$dir/.login_throttle.json";
    if (!is_file($file)) return 0;
    $map = json_decode(@file_get_contents($file), true);
    $until = is_array($map) ? (int)($map[$ip]['locked_until'] ?? 0) : 0;
    $left = $until - time();
    return $left > 0 ? $left : 0;
}

function login_record($dir, $ip, $success) {
    if (!is_dir($dir)) { @mkdir($dir, 0750, true); }
    $fh = @fopen("$dir/.login_throttle.json", 'c+');
    if ($fh === false) return;
    if (flock($fh, LOCK_EX)) {
        $map = json_decode(stream_get_contents($fh), true);
        if (!is_array($map)) { $map = []; }
        $now = time();
        foreach ($map as $k => $v) {
            if ((int)($v['locked_until'] ?? 0) < $now
                && ($now - (int)($v['first'] ?? 0)) > LOGIN_WINDOW) {
                unset($map[$k]);
            }
        }
        if ($success) {
            unset($map[$ip]);
        } else {
            $e = $map[$ip] ?? ['fails' => 0, 'first' => $now, 'locked_until' => 0];
            if (($now - (int)$e['first']) > LOGIN_WINDOW) {
                $e['fails'] = 0; $e['first'] = $now;
            }
            $e['fails'] = (int)$e['fails'] + 1;
            if ($e['fails'] >= LOGIN_MAX_FAILS) { $e['locked_until'] = $now + LOGIN_LOCKOUT; }
            $map[$ip] = $e;
        }
        ftruncate($fh, 0);
        rewind($fh);
        fwrite($fh, json_encode($map));
        fflush($fh);
        flock($fh, LOCK_UN);
    }
    fclose($fh);
}

// --- logout -----------------------------------------------------------------
if (isset($_GET['logout'])) {
    $_SESSION = [];
    session_destroy();
    header('Location: ' . self_path());
    exit;
}

// --- login ------------------------------------------------------------------
$login_error = '';
if ($_SERVER['REQUEST_METHOD'] === 'POST' && isset($_POST['password'])) {
    $ip = login_client_ip();
    $locked = login_locked_seconds($BASE_DIR, $ip);
    if ($locked > 0) {
        $login_error = 'Too many attempts. Try again in ' . ceil($locked / 60) . ' min.';
    } elseif ($configured
        && hash_equals($DASHBOARD_PASSWORD, (string)$_POST['password'])) {
        login_record($BASE_DIR, $ip, true);
        session_regenerate_id(true);
        $_SESSION['speedkam_auth'] = true;
        header('Location: ' . self_path());
        exit;
    } else {
        login_record($BASE_DIR, $ip, false);
        $login_error = 'Incorrect password.';
    }
}

// --- authenticated media proxy ----------------------------------------------
if (isset($_GET['media'])) {
    if (!is_authed()) { http_response_code(403); exit('forbidden'); }
    $name = basename((string)$_GET['media']);
    $ext  = strtolower(pathinfo($name, PATHINFO_EXTENSION));
    if (!preg_match('/^[A-Za-z0-9._-]+$/', $name) || !isset($ALLOWED_EXT[$ext])) {
        http_response_code(400); exit('bad request');
    }
    $matches = glob("$DATA_DIR/media/*/" . $name);
    if (!$matches) { http_response_code(404); exit('not found'); }
    serve_file($matches[0], $ALLOWED_EXT[$ext]);
}

function serve_file($file, $mime) {
    $size = filesize($file);
    $fp = fopen($file, 'rb');
    if ($fp === false) { http_response_code(500); exit; }
    header("Content-Type: $mime");
    header('Accept-Ranges: bytes');
    header('Cache-Control: private, max-age=3600');
    if (isset($_SERVER['HTTP_RANGE'])
        && preg_match('/bytes=(\d*)-(\d*)/', $_SERVER['HTTP_RANGE'], $m)) {
        $start = ($m[1] === '') ? 0 : (int)$m[1];
        $end   = ($m[2] === '') ? $size - 1 : (int)$m[2];
        if ($start > $end || $end >= $size) { $end = $size - 1; }
        header('HTTP/1.1 206 Partial Content');
        header("Content-Range: bytes $start-$end/$size");
        header('Content-Length: ' . ($end - $start + 1));
        fseek($fp, $start);
        $remaining = $end - $start + 1;
        while ($remaining > 0 && !feof($fp)) {
            $chunk = fread($fp, (int)min(8192, $remaining));
            if ($chunk === false) break;
            echo $chunk;
            $remaining -= strlen($chunk);
        }
    } else {
        header("Content-Length: $size");
        fpassthru($fp);
    }
    fclose($fp);
    exit;
}

// --- not logged in -> login page --------------------------------------------
if (!is_authed()) {
    render_login($configured, $login_error);
    exit;
}

// --- fleet overview ---------------------------------------------------------
if ($sel_node === '' && is_dir($nodes_root)) {
    $nodes = [];
    foreach (scandir($nodes_root) as $e) {
        if ($e === '.' || $e === '..' || !is_dir("$nodes_root/$e")) continue;
        $nodes[] = $e;
    }
    if ($nodes) { render_fleet($nodes_root, $nodes); exit; }
}

// --- remote control write (authed) ------------------------------------------
function queue_desired($DATA_DIR, array $changes) {
    $dfile = "$DATA_DIR/desired.json";
    $cur = is_file($dfile) ? json_decode(@file_get_contents($dfile), true) : [];
    if (!is_array($cur)) { $cur = []; }
    $cur = array_merge($cur, $changes);
    $cur['rev']        = (int)($cur['rev'] ?? 0) + 1;
    $cur['updated_at'] = gmdate('c');
    @file_put_contents($dfile, json_encode($cur));
}

function redirect_self($sel_node) {
    header('Location: ' . self_path()
        . ($sel_node !== '' ? '?node=' . rawurlencode($sel_node) : ''));
    exit;
}

if ($_SERVER['REQUEST_METHOD'] === 'POST' && isset($_POST['set_threshold'])) {
    $val = $_POST['threshold'] ?? '';
    if (is_numeric($val) && (float)$val >= 0) {
        queue_desired($DATA_DIR, ['speedkapture_threshold' => (float)$val]);
    }
    redirect_self($sel_node);
}

if ($_SERVER['REQUEST_METHOD'] === 'POST' && isset($_POST['set_limit'])) {
    $val = $_POST['limit'] ?? '';
    $lu  = ($_POST['limit_units'] ?? 'mph') === 'kmh' ? 'kmh' : 'mph';
    if (is_numeric($val) && (float)$val > 0) {
        $kmh = ($lu === 'mph') ? (float)$val * 1.609344 : (float)$val;
        queue_desired($DATA_DIR, ['speed_limit_kmh' => round($kmh, 3)]);
    }
    redirect_self($sel_node);
}

// NOTE: the "only count cars" car-filter is fixed policy in the node's code
// (seeded from config on boot) and is deliberately NOT remotely tunable -- so
// there is no set_reject handler here. Manual per-reading reject/restore below
// is separate: it re-tags an individual event, it does not change the filter.

// --- manual reject / restore (rewrites the mirrored events.csv status) -------
if ($_SERVER['REQUEST_METHOD'] === 'POST' && isset($_POST['set_status'])) {
    $key    = (string)($_POST['event_key'] ?? '');
    $status = ($_POST['set_status'] === 'rejected') ? 'rejected' : 'ok';
    $reason = $status === 'rejected'
        ? ((string)($_POST['reason'] ?? 'manually marked not a vehicle')) : '';
    if ($key !== '') {
        set_event_status("$DATA_DIR/events.csv", $key, $status, $reason);
    }
    redirect_self($sel_node);
}

// ============================ load data =====================================
$status  = json_decode(@file_get_contents("$DATA_DIR/status.json"), true) ?: [];
$desired = json_decode(@file_get_contents("$DATA_DIR/desired.json"), true) ?: [];

$last_seen = isset($status['received_at']) ? strtotime($status['received_at']) : null;
$online    = ($last_seen !== null) && (time() - $last_seen < 120);
$units     = $status['units'] ?? 'mph';
$limit_kmh = isset($status['speed_limit_kmh']) ? (float)$status['speed_limit_kmh'] : null;

list($header, $all_rows) = load_events("$DATA_DIR/events.csv");

function load_events($csv) {
    if (!is_file($csv)) return [[], []];
    $fh = fopen($csv, 'r');
    if ($fh === false) return [[], []];
    $header = null; $rows = [];
    while (($r = fgetcsv($fh)) !== false) {
        if ($r === [null] || (count($r) === 1 && trim((string)$r[0]) === '')) continue;
        if ($header === null) { $header = $r; continue; }
        $r = array_slice(array_pad($r, count($header), ''), 0, count($header));
        $rows[] = array_combine($header, $r);
    }
    fclose($fh);
    return [$header, $rows];
}

// --- false-positive helpers -------------------------------------------------
function is_rejected($r) {
    return strtolower(trim($r['status'] ?? 'ok')) === 'rejected';
}
function ev_row_key($r) {
    $clip = trim($r['clip'] ?? '');
    $snap = trim($r['snapshot'] ?? '');
    if ($clip !== '') return pathinfo($clip, PATHINFO_FILENAME);
    if ($snap !== '') return pathinfo($snap, PATHINFO_FILENAME);
    return ($r['wall_time'] ?? '') . '|' . ($r['track_id'] ?? '');
}
// Rewrite one row's status/review_reason in the mirrored CSV (manual bin edits).
function set_event_status($csv, $key, $status, $reason) {
    if (!is_file($csv)) return 0;
    $fh = fopen($csv, 'c+');
    if ($fh === false) return 0;
    $updated = 0;
    if (flock($fh, LOCK_EX)) {
        $header = fgetcsv($fh);
        if ($header !== false && $header !== null) {
            $idx = array_flip($header);
            $rows = [$header];
            while (($r = fgetcsv($fh)) !== false) {
                if ($r === [null]) continue;
                $r = array_slice(array_pad($r, count($header), ''), 0, count($header));
                $assoc = array_combine($header, $r);
                if (ev_row_key($assoc) === $key) {
                    if (isset($idx['status']))        $r[$idx['status']] = $status;
                    if (isset($idx['review_reason'])) $r[$idx['review_reason']] = $reason;
                    $updated++;
                }
                $rows[] = $r;
            }
            if ($updated > 0) {
                rewind($fh); ftruncate($fh, 0);
                foreach ($rows as $row) { fputcsv($fh, $row); }
                fflush($fh);
            }
        }
        flock($fh, LOCK_UN);
    }
    fclose($fh);
    return $updated;
}

function is_over($r, $limit_kmh) {
    // Fleet rows carry their own node's limit in _limit_kmh (each street can
    // have a different limit); single-node rows fall back to the passed limit.
    $lim = (isset($r['_limit_kmh']) && is_numeric($r['_limit_kmh']))
        ? (float)$r['_limit_kmh'] : $limit_kmh;
    return $lim !== null && is_numeric($r['speed_kmh'] ?? '')
        && (float)$r['speed_kmh'] > $lim;
}
function disp_speed_val($r, $units) {
    $v = ($units === 'mph') ? ($r['speed_mph'] ?? '') : ($r['speed_kmh'] ?? '');
    return is_numeric($v) ? (float)$v : null;
}
function disp_speed($r, $units) {
    $v = disp_speed_val($r, $units);
    return $v === null ? '?' : rtrim(rtrim(number_format($v, 1), '0'), '.');
}

// Real (non-rejected) passes drive every stat; the rejects feed the bin only.
$rows     = array_values(array_filter($all_rows, fn($r) => !is_rejected($r)));
$rejected = array_values(array_filter($all_rows, fn($r) => is_rejected($r)));

// --- aggregate counts + direction breakdown + averages ----------------------
// Shared by the single-node view and the fleet overview (which merges every
// node's rows). Returns period counts/over-limit, direction + colour tallies,
// per-period speed lists, and the day-of-week / hour / speed-distribution
// histograms -- everything the analytics panel needs.
$agg     = compute_stats($rows, $limit_kmh);
$c       = $agg['c'];       $sp      = $agg['sp'];
$dir     = $agg['dir'];     $col     = $agg['col'];
$spd     = $agg['spd'];     $spd_dir = $agg['spd_dir'];
$dow     = $agg['dow'];     $hod     = $agg['hod'];
$hist    = $agg['hist'];    $edges   = $agg['edges'];

function avg_of($a) { return $a ? array_sum($a) / count($a) : null; }

function compute_stats($rows, $limit_kmh) {
    $today        = date('Y-m-d');
    $week_start   = date('Y-m-d', strtotime('monday this week'));
    $month_prefix = date('Y-m');
    $c   = ['today' => 0, 'week' => 0, 'month' => 0, 'total' => count($rows)];
    $sp  = ['today' => 0, 'week' => 0, 'month' => 0, 'total' => 0];
    $dir = ['today' => [], 'week' => [], 'month' => [], 'all' => []];
    $col = ['today' => [], 'week' => [], 'month' => [], 'all' => []];
    $spd = ['today' => [], 'week' => [], 'month' => [], 'all' => []];  // km/h lists
    $spd_dir = ['today' => [], 'week' => [], 'month' => [], 'all' => []];
    foreach ($rows as $r) {
        $d    = substr($r['wall_time'] ?? '', 0, 10);
        $over = is_over($r, $limit_kmh);
        $kmh  = is_numeric($r['speed_kmh'] ?? '') ? (float)$r['speed_kmh'] : null;
        $dr   = trim($r['direction'] ?? '');
        $co   = trim($r['color'] ?? '');
        if ($over) $sp['total']++;
        $tally_periods = ['all'];
        if (preg_match('/^\d{4}-\d{2}-\d{2}$/', $d)) {
            if ($d === $today) $tally_periods[] = 'today';
            if ($d >= $week_start) $tally_periods[] = 'week';
            if (substr($d, 0, 7) === $month_prefix) $tally_periods[] = 'month';
        }
        foreach ($tally_periods as $p) {
            if ($p !== 'all') { $c[$p]++; if ($over) $sp[$p]++; }
            if ($dr !== '') $dir[$p][$dr] = ($dir[$p][$dr] ?? 0) + 1;
            if ($co !== '') $col[$p][$co] = ($col[$p][$co] ?? 0) + 1;
            if ($kmh !== null) {
                $spd[$p][] = $kmh;
                if ($dr !== '') $spd_dir[$p][$dr][] = $kmh;
            }
        }
    }
    // day-of-week + hour histograms + speed distribution (over ALL real passes)
    $dow = array_fill(0, 7, ['count' => 0, 'sum' => 0.0]);
    $hod = array_fill(0, 24, ['count' => 0, 'sum' => 0.0]);
    $edges = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 60, 999];
    $hist = array_fill(0, count($edges) - 1, 0);
    foreach ($rows as $r) {
        $kmh = is_numeric($r['speed_kmh'] ?? '') ? (float)$r['speed_kmh'] : null;
        $ts  = strtotime($r['wall_time'] ?? '');
        if ($kmh !== null && $ts !== false) {
            $w = (int)date('N', $ts) - 1;         // 0=Mon..6=Sun
            $h = (int)date('G', $ts);
            $dow[$w]['count']++; $dow[$w]['sum'] += $kmh;
            $hod[$h]['count']++; $hod[$h]['sum'] += $kmh;
            $mph = $kmh / 1.609344;
            for ($i = 0; $i < count($edges) - 1; $i++) {
                if ($mph >= $edges[$i] && $mph < $edges[$i + 1]) { $hist[$i]++; break; }
            }
        }
    }
    return compact('c', 'sp', 'dir', 'col', 'spd', 'spd_dir', 'dow', 'hod', 'hist', 'edges');
}

// --- report builder (GET filters) -------------------------------------------
$rep = null;
if (isset($_GET['report'])) {
    $rep = run_report($all_rows, $_GET, $limit_kmh);
}
function run_report($all_rows, $g, $limit_kmh) {
    $status = strtolower($g['status'] ?? 'ok');
    $color  = strtolower(trim($g['color'] ?? ''));
    $rdir   = strtolower(trim($g['direction'] ?? ''));
    $minmph = is_numeric($g['min_mph'] ?? '') ? (float)$g['min_mph'] : null;
    $maxmph = is_numeric($g['max_mph'] ?? '') ? (float)$g['max_mph'] : null;
    $from   = $g['from'] ?? ''; $to = $g['to'] ?? '';
    $hf     = ($g['hour_from'] ?? '') !== '' ? (int)$g['hour_from'] : null;
    $ht     = ($g['hour_to'] ?? '') !== '' ? (int)$g['hour_to'] : null;
    $dows   = isset($g['dow']) && is_array($g['dow'])
        ? array_map('intval', $g['dow']) : [];
    $out = [];
    foreach ($all_rows as $r) {
        if ($status !== 'all') {
            if ($status === 'rejected' && !is_rejected($r)) continue;
            if ($status === 'ok' && is_rejected($r)) continue;
        }
        $d = substr($r['wall_time'] ?? '', 0, 10);
        if ($from !== '' && $d < $from) continue;
        if ($to !== '' && $d > $to) continue;
        if ($color !== '' && strtolower(trim($r['color'] ?? '')) !== $color) continue;
        if ($rdir !== '' && strtolower(trim($r['direction'] ?? '')) !== $rdir) continue;
        $mph = is_numeric($r['speed_mph'] ?? '') ? (float)$r['speed_mph'] : null;
        if ($minmph !== null && ($mph === null || $mph < $minmph)) continue;
        if ($maxmph !== null && ($mph === null || $mph > $maxmph)) continue;
        if ($dows || $hf !== null || $ht !== null) {
            $ts = strtotime($r['wall_time'] ?? '');
            if ($ts === false) continue;
            if ($dows && !in_array((int)date('N', $ts) - 1, $dows, true)) continue;
            if ($hf !== null && (int)date('G', $ts) < $hf) continue;
            if ($ht !== null && (int)date('G', $ts) > $ht) continue;
        }
        $out[] = $r;
    }
    $speeds = [];
    $colors = [];
    foreach ($out as $r) {
        if (is_numeric($r['speed_kmh'] ?? '')) $speeds[] = (float)$r['speed_kmh'];
        $co = trim($r['color'] ?? '');
        if ($co !== '') $colors[$co] = ($colors[$co] ?? 0) + 1;
    }
    return [
        'rows' => array_reverse($out),
        'count' => count($out),
        'avg_kmh' => avg_of($speeds),
        'max_kmh' => $speeds ? max($speeds) : null,
        'over' => $limit_kmh !== null
            ? count(array_filter($speeds, fn($s) => $s > $limit_kmh)) : 0,
        'colors' => $colors,
        'filters' => compact('status', 'color', 'rdir', 'minmph', 'maxmph',
            'from', 'to', 'hf', 'ht', 'dows'),
    ];
}

// --- top 10 fastest (real passes only) --------------------------------------
$top = $rows;
usort($top, fn($a, $b) => (float)($b['speed_kmh'] ?? -1) <=> (float)($a['speed_kmh'] ?? -1));
$top = array_slice($top, 0, 10);

// --- table view -------------------------------------------------------------
$filter = ($_GET['filter'] ?? 'all') === 'speeders' ? 'speeders' : 'all';
$view = array_reverse($rows);
if ($filter === 'speeders') {
    $view = array_values(array_filter($view, fn($r) => is_over($r, $limit_kmh)));
}
$view = array_slice($view, 0, 100);

$desired_thr = $desired['speedkapture_threshold'] ?? null;
$camera_thr  = $status['speedkapture_threshold'] ?? null;
$desired_limit_kmh = $desired['speed_limit_kmh'] ?? null;

// recent clips (most-recent first, real passes that recorded a clip) for the grid
$recent_clips = array_slice(array_values(array_filter(
    array_reverse($rows), fn($r) => trim($r['clip'] ?? '') !== '')), 0, 12);
// newest snapshot for the "latest snapshot" tile: the camera's last_event wins
// (freshest, even sub-threshold), else the newest mirrored row with a snapshot.
$latest_snap = trim($status['last_event']['snapshot'] ?? '');
$latest_snap_time = $status['last_event']['time'] ?? '';
if ($latest_snap === '') {
    foreach (array_reverse($rows) as $r) {
        $s = trim($r['snapshot'] ?? '');
        if ($s !== '') { $latest_snap = $s; $latest_snap_time = $r['wall_time'] ?? ''; break; }
    }
}

// list of colours seen (for the report dropdown)
$all_colors = [];
foreach ($rows as $r) { $co = trim($r['color'] ?? ''); if ($co !== '') $all_colors[$co] = true; }
$all_colors = array_keys($all_colors); sort($all_colors);
$all_dirs = array_keys($dir['all']);

render_dashboard(compact(
    'online', 'last_seen', 'units', 'limit_kmh', 'status', 'c', 'sp', 'dir',
    'col', 'spd', 'spd_dir', 'dow', 'hod', 'hist', 'edges', 'top', 'view', 'rows',
    'filter', 'desired', 'desired_thr', 'camera_thr', 'desired_limit_kmh',
    'sel_node', 'rejected', 'rep', 'all_colors', 'all_dirs',
    'recent_clips', 'latest_snap', 'latest_snap_time'
));

// ============================ rendering =====================================
function h($s) { return htmlspecialchars((string)$s, ENT_QUOTES, 'UTF-8'); }

function ago($ts) {
    if (!$ts) return 'never';
    $s = time() - $ts;
    if ($s < 60)   return "{$s}s ago";
    if ($s < 3600) return floor($s / 60) . 'm ago';
    if ($s < 86400) return floor($s / 3600) . 'h ago';
    return floor($s / 86400) . 'd ago';
}
function to_disp($kmh, $units) {
    if ($kmh === null) return null;
    return $units === 'mph' ? $kmh / 1.609344 : $kmh;
}
function ulbl($units) { return $units === 'mph' ? 'mph' : 'km/h'; }

// Colour name -> swatch, matching the on-Pi dashboard.
function swatch($n) {
    static $m = ['red'=>'#e5484d','orange'=>'#f76b15','yellow'=>'#f5d90a',
        'green'=>'#30a46c','teal'=>'#12a4b4','blue'=>'#3b82f6','purple'=>'#8b5cf6',
        'pink'=>'#e93d82','white'=>'#e7edf3','gray'=>'#8a97a6','grey'=>'#8a97a6',
        'silver'=>'#c0c7cf','black'=>'#3a4250','brown'=>'#a06a42'];
    return $m[strtolower($n)] ?? '#5b6675';
}

function page_head($title) {
    echo '<!doctype html><html lang="en"><head><meta charset="utf-8">'
       . '<meta name="viewport" content="width=device-width,initial-scale=1">'
       . '<title>' . h($title) . '</title><style>'
       . ':root{color-scheme:dark;--bg:#0d1017;--card:#151b23;--card2:#1c232d;--line:#28313d;'
       . '--fg:#e8eef5;--muted:#8a97a6;--ok:#37c871;--bad:#ff5470;--warn:#ffb020;'
       . '--accent:#3aa0ff;--accent2:#7c5cff}'
       . '*{box-sizing:border-box}body{font:15px/1.5 system-ui,-apple-system,Segoe UI,'
       . 'Roboto,sans-serif;margin:0;padding:1.25rem;color:var(--fg);'
       . 'background:radial-gradient(1200px 600px at 80% -10%,rgba(124,92,255,.10),transparent 60%),'
       . 'radial-gradient(1000px 500px at -10% 0%,rgba(58,160,255,.10),transparent 55%),var(--bg)}'
       . '.wrap{max-width:1080px;margin:0 auto}'
       . 'h1{font-size:1.25rem;margin:0 0 1rem;display:flex;align-items:center;gap:.6rem;flex-wrap:wrap}'
       . '.dot{width:.7rem;height:.7rem;border-radius:50%;display:inline-block}'
       . '.ok{color:var(--ok)}.bad{color:var(--bad)}.dot.ok{background:var(--ok)}.dot.bad{background:var(--bad)}'
       . '.muted{color:var(--muted)}.grow{flex:1}'
       . 'a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}'
       . '.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.75rem;margin:1rem 0}'
       . '.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:1rem;'
       . 'box-shadow:0 6px 24px rgba(0,0,0,.35)}'
       . '.card .n{font-size:1.7rem;font-weight:800;font-variant-numeric:tabular-nums}'
       . '.card .l{color:var(--muted);font-size:.8rem;text-transform:uppercase;letter-spacing:.04em}'
       . '.card .s{color:var(--muted);font-size:.85rem;margin-top:.15rem}'
       . 'table{width:100%;border-collapse:collapse;background:var(--card);'
       . 'border:1px solid var(--line);border-radius:12px;overflow:hidden}'
       . 'th,td{padding:.55rem .65rem;text-align:left;border-top:1px solid var(--line);vertical-align:middle}'
       . 'th{font-size:.72rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);border-top:0}'
       . 'td.num{font-variant-numeric:tabular-nums}'
       . 'img.thumb{height:44px;border-radius:6px;display:block}'
       . '.pill{padding:.15rem .5rem;border-radius:999px;font-size:.75rem;border:1px solid var(--line);font-variant-numeric:tabular-nums}'
       . '.pill.over{color:var(--bad);border-color:var(--bad)}'
       . 'form.ctl{background:var(--card);border:1px solid var(--line);border-radius:12px;'
       . 'padding:1rem;margin:1rem 0;display:flex;gap:.6rem;align-items:end;flex-wrap:wrap}'
       . 'label{display:block;font-size:.8rem;color:var(--muted);margin-bottom:.25rem}'
       . 'input[type=number],input[type=password],input[type=date],select{background:var(--bg);color:var(--fg);'
       . 'border:1px solid var(--line);border-radius:8px;padding:.5rem .6rem;font-size:.95rem}'
       . 'input[type=number]{width:7rem}'
       . 'button{background:linear-gradient(90deg,var(--accent),var(--accent2));color:#fff;border:0;border-radius:8px;'
       . 'padding:.55rem 1rem;font-size:.95rem;cursor:pointer}button:hover{filter:brightness(1.1)}'
       . 'button.ghost{background:var(--card2);border:1px solid var(--line);color:var(--fg)}'
       . '.tabs{display:flex;gap:.5rem;margin:.5rem 0;flex-wrap:wrap}'
       . '.tabs a{padding:.3rem .7rem;border:1px solid var(--line);border-radius:999px;color:var(--fg)}'
       . '.tabs a.on{background:linear-gradient(90deg,var(--accent),var(--accent2));border-color:transparent;color:#fff}'
       . '.center{min-height:80vh;display:flex;align-items:center;justify-content:center}'
       . '.latest{display:flex;align-items:center;gap:1rem;background:var(--card);'
       . 'border:1px solid var(--line);border-radius:12px;padding:1rem 1.25rem;margin:1rem 0}'
       . '.latest .big{font-size:2.4rem;font-weight:800;line-height:1;font-variant-numeric:tabular-nums;display:flex;align-items:baseline;gap:.35rem}'
       . '.latest .big.over{color:var(--bad)}.latest .big .u{font-size:.9rem;font-weight:500;color:var(--muted)}'
       . '.latest .meta{color:var(--fg);font-size:.95rem}'
       . '.badge{display:inline-block;background:var(--bad);color:#fff;border-radius:6px;padding:.1rem .45rem;font-size:.7rem;font-weight:700;letter-spacing:.04em}'
       . '.chips{display:flex;gap:.3rem;flex-wrap:wrap;margin-top:.4rem}'
       . '.chip{border:1px solid var(--line);border-radius:999px;padding:.05rem .5rem;font-size:.72rem;color:var(--muted);text-transform:capitalize}'
       . 'h2.sec{font-size:.8rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin:1.5rem 0 .5rem}'
       . 'ol.toplist{list-style:none;counter-reset:t;margin:0;padding:0;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}'
       . 'ol.toplist li{counter-increment:t;display:flex;align-items:center;gap:.75rem;padding:.55rem .8rem;border-top:1px solid var(--line)}'
       . 'ol.toplist li:first-child{border-top:0}'
       . 'ol.toplist li::before{content:counter(t);color:var(--muted);font-weight:700;width:1.4rem;text-align:right;font-variant-numeric:tabular-nums}'
       . 'ol.toplist .sp{font-weight:700;min-width:5.5rem;font-variant-numeric:tabular-nums}ol.toplist .sp.over{color:var(--bad)}'
       . 'ol.toplist .dt{margin-left:auto;font-size:.82rem}'
       // last 10 cars (verification)
       . 'ol.lastcars{list-style:none;margin:0;padding:0;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}'
       . 'ol.lastcars li{display:flex;align-items:center;gap:.7rem;padding:.5rem .8rem;border-top:1px solid var(--line);font-size:.92rem}'
       . 'ol.lastcars li:first-child{border-top:0}'
       . 'ol.lastcars .lc-t{color:var(--muted);font-variant-numeric:tabular-nums;min-width:9.5rem}'
       . 'ol.lastcars .lc-sp{font-weight:700;min-width:4.2rem;font-variant-numeric:tabular-nums}ol.lastcars .lc-sp.over{color:var(--bad)}'
       . 'ol.lastcars .lc-col{display:flex;align-items:center;gap:.4rem;flex:1;text-transform:capitalize}'
       . 'ol.lastcars .lc-col i{width:.7rem;height:.7rem;border-radius:50%;border:1px solid var(--line);flex:none}'
       . 'ol.lastcars .lc-col .none{color:var(--muted);opacity:.6;text-transform:none}'
       . 'ol.lastcars .lc-dir{margin-left:auto;color:var(--muted);font-size:.82rem;text-align:right}'
       . '.lc-note{color:var(--muted);font-size:.8rem;margin:.4rem .2rem 0}'
       // analytics
       . '.panel{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:1rem 1.1rem;margin:1rem 0;box-shadow:0 6px 24px rgba(0,0,0,.35)}'
       . '.panel h3{margin:0 0 .8rem;font-size:.8rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}'
       . '.angrid{display:grid;grid-template-columns:repeat(2,1fr);gap:1.2rem}'
       . '@media(max-width:720px){.angrid{grid-template-columns:1fr}}'
       . '.ct{font-size:.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:.5rem}'
       . '.donutwrap{display:flex;gap:1rem;align-items:center;flex-wrap:wrap}'
       . '.legend{display:flex;flex-direction:column;gap:.35rem;font-size:.85rem;flex:1;min-width:140px}'
       . '.legend .li{display:flex;align-items:center;gap:.5rem}.legend .sw{width:12px;height:12px;border-radius:3px;border:1px solid rgba(255,255,255,.15)}'
       . '.legend .nm{text-transform:capitalize}.legend .pc{margin-left:auto;color:var(--muted);font-variant-numeric:tabular-nums}'
       . '.bars{display:flex;flex-direction:column;gap:.45rem}'
       . '.bar{display:grid;grid-template-columns:64px 1fr auto;align-items:center;gap:.6rem;font-size:.8rem}'
       . '.bar .lbl{color:var(--muted);text-transform:capitalize;text-align:right;white-space:nowrap}'
       . '.bar .track{background:var(--card2);border-radius:6px;height:16px;overflow:hidden}'
       . '.bar .fill{height:100%;border-radius:6px;background:linear-gradient(90deg,var(--accent),var(--accent2));min-width:2px}'
       . '.bar .val{font-variant-numeric:tabular-nums;min-width:52px;text-align:right}'
       . '.vbars{display:flex;align-items:flex-end;gap:3px;height:120px}'
       . '.vb{flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;gap:2px;height:100%}'
       . '.vb .col{width:100%;border-radius:4px 4px 0 0;background:linear-gradient(180deg,var(--accent),var(--accent2));min-height:2px}'
       . '.vb .cap{font-size:9px;color:var(--muted)}'
       . '.axis{display:flex;justify-content:space-between;font-size:9.5px;color:var(--muted);margin-top:3px}'
       . '.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:1px;background:var(--line);border-radius:10px;overflow:hidden;margin-bottom:1rem}'
       . '.metrics .m{background:var(--card);padding:.7rem .8rem}.metrics .m .k{font-size:.68rem;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}'
       . '.metrics .m .v{font-size:1.4rem;font-weight:800;font-variant-numeric:tabular-nums}.metrics .m .s{font-size:.72rem;color:var(--muted)}'
       . '.rb{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:.7rem;align-items:end}'
       . '.rb .fld label{font-size:.68rem}.rb .fld input,.rb .fld select{width:100%}'
       . '.dowsel{display:flex;gap:.3rem;flex-wrap:wrap}'
       . '.dowsel label{display:inline-flex;align-items:center;gap:.25rem;background:var(--card2);border:1px solid var(--line);'
       . 'border-radius:7px;padding:.35rem .5rem;font-size:.75rem;color:var(--fg);margin:0;cursor:pointer}'
       . '.rejgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:.7rem}'
       . '.rej{background:var(--card2);border:1px solid var(--line);border-radius:10px;overflow:hidden}'
       . '.rej img{width:100%;aspect-ratio:16/9;object-fit:cover;background:#000;display:block}'
       . '.rej .b{padding:.5rem .6rem;display:flex;flex-direction:column;gap:.4rem}'
       . '.rej .rsn{color:var(--warn);font-size:.72rem;line-height:1.35}'
       . 'details>summary{cursor:pointer;color:var(--muted);font-size:.8rem;text-transform:uppercase;letter-spacing:.05em;list-style:none}'
       . 'details>summary::-webkit-details-marker{display:none}'
       // latest-snapshot "live" tile
       . '.snap{margin:1rem 0;background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden;box-shadow:0 6px 24px rgba(0,0,0,.35)}'
       . '.snap img{width:100%;display:block;aspect-ratio:16/9;object-fit:cover;background:#000;cursor:pointer}'
       . '.snap .cap{padding:.5rem .8rem;font-size:.78rem;color:var(--muted);display:flex;align-items:center;gap:.5rem;flex-wrap:wrap}'
       // recent-clips grid + inline player
       . '.clipgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:.7rem}'
       . '.clip{background:var(--card2);border:1px solid var(--line);border-radius:10px;overflow:hidden}'
       . '.clip .thumb{width:100%;aspect-ratio:16/9;object-fit:cover;background:#000;display:block;cursor:pointer}'
       . '.clip .meta{padding:.5rem .6rem;display:flex;align-items:center;gap:.5rem;font-size:.85rem}'
       . '.clip .meta .sp{font-weight:700}.clip .meta .sp.over{color:var(--bad)}'
       . '.clip .meta .t{margin-left:auto;font-size:.72rem;color:var(--muted)}'
       . '#vidModal{position:fixed;inset:0;background:rgba(0,0,0,.85);display:none;align-items:center;justify-content:center;padding:1rem;z-index:50}'
       . '#vidModal.show{display:flex}'
       . '#vidModal video{max-width:100%;max-height:85vh;border-radius:10px;background:#000}'
       . '#vidModal .x{position:absolute;top:1rem;right:1.2rem;font-size:2rem;line-height:1;color:#fff;cursor:pointer;background:none;border:0}'
       . '</style></head><body><div class="wrap">';
}
function page_foot() { echo '</div></body></html>'; }

function render_login($configured, $error) {
    page_head('SpeedKam');
    echo '<div class="center"><form method="post" class="card" style="min-width:280px">'
       . '<h1>SpeedKam</h1>';
    if (!$configured) {
        echo '<p class="bad">Dashboard password not set. Edit '
           . '<code>speedkam_config.php</code> ($DASHBOARD_PASSWORD).</p>';
    } else {
        if ($error) echo '<p class="bad">' . h($error) . '</p>';
        echo '<label for="pw">Password</label>'
           . '<input id="pw" type="password" name="password" autofocus style="width:100%">'
           . '<div style="margin-top:.8rem"><button type="submit">Sign in</button></div>';
    }
    echo '</form></div>';
    page_foot();
}

// donut (inline SVG), returns html
function render_donut($colors) {
    $colors = is_array($colors) ? $colors : [];  // never let a null/scalar fatal the whole page (PHP 8 array_sum)
    $total = array_sum($colors);
    if (!$total) return '<div class="muted">No data</div>';
    arsort($colors);
    $R = 54; $C = 2 * M_PI * $R; $cx = 64; $cy = 64; $off = 0; $segs = '';
    foreach ($colors as $name => $v) {
        $len = ($v / $total) * $C;
        $segs .= '<circle cx="' . $cx . '" cy="' . $cy . '" r="' . $R . '" fill="none" stroke="'
            . swatch($name) . '" stroke-width="18" stroke-dasharray="' . $len . ' ' . ($C - $len)
            . '" stroke-dashoffset="' . (-$off) . '" transform="rotate(-90 ' . $cx . ' ' . $cy . ')"></circle>';
        $off += $len;
    }
    $leg = '';
    foreach ($colors as $name => $v) {
        $leg .= '<div class="li"><span class="sw" style="background:' . swatch($name) . '"></span>'
            . '<span class="nm">' . h($name) . '</span><span class="pc">'
            . round($v / $total * 100) . '% · ' . $v . '</span></div>';
    }
    return '<div class="donutwrap"><svg width="128" height="128" viewBox="0 0 128 128">' . $segs
        . '<text x="64" y="60" text-anchor="middle" fill="#e8eef5" font-size="22" font-weight="800">' . $total . '</text>'
        . '<text x="64" y="78" text-anchor="middle" fill="#8a97a6" font-size="10">vehicles</text></svg>'
        . '<div class="legend">' . $leg . '</div></div>';
}
function render_hbars($items) {  // items: [[label, frac(0..1), valtext]]
    if (!$items) return '<div class="muted">No data</div>';
    $out = '<div class="bars">';
    foreach ($items as $it) {
        [$lbl, $frac, $val] = $it;
        $out .= '<div class="bar"><span class="lbl">' . h($lbl) . '</span>'
            . '<span class="track"><span class="fill" style="width:' . max(0, min(100, $frac * 100)) . '%"></span></span>'
            . '<span class="val">' . h($val) . '</span></div>';
    }
    return $out . '</div>';
}
function render_vbars($items, $axis) {  // items: [[count, max, title]]
    $max = 1; foreach ($items as $it) { $max = max($max, $it[0]); }
    $out = '<div class="vbars">';
    foreach ($items as $it) {
        $hpc = $it[0] / $max * 100;
        $out .= '<div class="vb" title="' . h($it[2]) . '"><div class="col" style="height:' . $hpc . '%"></div>'
            . '<div class="cap">' . ($it[0] ?: '') . '</div></div>';
    }
    $out .= '</div><div class="axis">';
    foreach ($axis as $a) { $out .= '<span>' . h($a) . '</span>'; }
    return $out . '</div>';
}

function render_dashboard($d) {
    extract($d);
    page_head('SpeedKam dashboard');

    if (!empty($sel_node)) {
        echo '<div style="margin-bottom:.5rem"><a href="?" class="muted" style="font-size:.85rem">&lsaquo; all cameras</a>'
           . ' <span class="muted" style="font-size:.85rem">&middot; node <code>' . h($sel_node) . '</code></span></div>';
    }

    $sw = $online ? 'ok' : 'bad';
    echo '<h1><span class="dot ' . $sw . '"></span>SpeedKam '
       . '<span class="' . $sw . '">' . ($online ? 'online' : 'offline') . '</span>'
       . '<span class="grow"></span>'
       . '<span class="muted" style="font-size:.85rem">camera check-in ' . h(ago($last_seen)) . '</span> '
       . '<a href="?logout" class="muted" style="font-size:.85rem">sign out</a></h1>';

    $limit_disp = ($limit_kmh !== null)
        ? (($units === 'mph') ? round($limit_kmh / 1.609) . ' mph' : round($limit_kmh) . ' km/h')
        : 'not set';
    echo '<div class="muted" style="font-size:.9rem;margin-top:-.5rem">'
       . 'Speed limit ' . h($limit_disp) . ' &middot; live FPS ' . h($status['fps'] ?? '?')
       . ' &middot; capturing above ' . h($camera_thr ?? '?') . ' ' . h($units) . '</div>';

    $mnq = (!empty($sel_node)) ? '&node=' . rawurlencode($sel_node) : '';

    // latest snapshot -- the off-site stand-in for the on-Pi live MJPEG view,
    // which can't cross home NAT. Shows the newest capture; refreshes on reload.
    if (!empty($latest_snap)) {
        $ssrc = '?media=' . rawurlencode($latest_snap) . $mnq;
        echo '<div class="snap"><a href="' . h($ssrc) . '" target="_blank">'
           . '<img src="' . h($ssrc) . '" alt="latest snapshot" loading="lazy"></a>'
           . '<div class="cap">Latest snapshot'
           . ($latest_snap_time ? ' &middot; ' . h(str_replace('T', ' ', substr($latest_snap_time, 0, 16))) : '')
           . ' &middot; <span>live video isn\'t available off-site &mdash; newest capture shown</span></div></div>';
    }

    // latest reading
    $ev = $status['last_event'] ?? null;
    if (is_array($ev)) {
        $ev_sp = ($units === 'mph') ? ($ev['speed_mph'] ?? null) : ($ev['speed_kmh'] ?? null);
        $ev_over = !empty($ev['over_limit']);
        $chips = trim(implode(' ', array_filter([
            $ev['color'] ?? '', $ev['vehicle_type'] ?? '', $ev['make'] ?? '',
            $ev['model'] ?? '', $ev['year'] ?? ''])));
        echo '<div class="latest"><div class="big' . ($ev_over ? ' over' : '') . '">'
           . h($ev_sp !== null ? round((float)$ev_sp) : '—') . '<span class="u">' . h($units) . '</span></div>'
           . '<div class="meta">' . ($ev_over ? '<span class="badge">SPEEDING</span> ' : '')
           . (isset($ev['captured']) && !$ev['captured'] ? '<span class="chip">not captured</span> ' : '')
           . h($ev['direction'] ?? '')
           . '<div class="muted" style="font-size:.8rem">' . h(str_replace('T', ' ', substr($ev['time'] ?? '', 0, 16))) . '</div>'
           . ($chips ? '<div class="chips">' . h($chips) . '</div>' : '') . '</div></div>';
    }

    // stat cards
    echo '<div class="cards">';
    stat_card('Today', $c['today'], $sp['today'] . ' over limit', dir_chips($dir['today']));
    stat_card('This week', $c['week'], $sp['week'] . ' over limit', dir_chips($dir['week']));
    stat_card('This month', $c['month'], $sp['month'] . ' over limit', dir_chips($dir['month']));
    stat_card('All time', $c['total'], $sp['total'] . ' over limit');
    echo '</div>';

    // -------- LAST 10 CARS (verification) --------
    render_last_cars($rows, $units, $limit_kmh);

    // -------- ANALYTICS --------
    render_analytics($d);

    // -------- REPORT BUILDER --------
    render_report_builder($d);

    $nq = (!empty($sel_node)) ? '&node=' . rawurlencode($sel_node) : '';

    // control panels
    render_controls($d);

    // recent clips grid (inline player)
    if (!empty($recent_clips)) {
        echo '<h2 class="sec">Recent clips</h2><div class="clipgrid">';
        foreach ($recent_clips as $r) {
            $over  = is_over($r, $limit_kmh);
            $snap  = trim($r['snapshot'] ?? '');
            $clip  = trim($r['clip'] ?? '');
            $thumb = $snap ? '?media=' . rawurlencode($snap) . $nq : '';
            $vsrc  = '?media=' . rawurlencode($clip) . $nq;
            echo '<div class="clip">'
               . ($thumb
                    ? '<img class="thumb" src="' . h($thumb) . '" loading="lazy" onclick="skPlay(\'' . h($vsrc) . '\')">'
                    : '<div class="thumb" onclick="skPlay(\'' . h($vsrc) . '\')"></div>')
               . '<div class="meta"><span class="sp' . ($over ? ' over' : '') . '">'
               . h(disp_speed($r, $units)) . ' ' . h($units) . '</span>'
               . '<span class="t">' . h(substr(str_replace('T', ' ', $r['wall_time'] ?? ''), 11, 5)) . '</span>'
               . '</div></div>';
        }
        echo '</div>';
    }

    // top 10
    if ($top) {
        echo '<h2 class="sec">Top 10 speeders</h2><ol class="toplist">';
        foreach ($top as $r) {
            $over = is_over($r, $limit_kmh);
            $clip = trim($r['clip'] ?? '');
            echo '<li><span class="sp' . ($over ? ' over' : '') . '">' . h(disp_speed($r, $units)) . ' ' . h($units) . '</span>'
               . '<span class="muted">' . h($r['direction'] ?? '') . '</span>'
               . '<span class="dt muted">' . h(str_replace('T', ' ', substr($r['wall_time'] ?? '', 0, 16))) . '</span>'
               . ($clip ? '<a href="?media=' . h(rawurlencode($clip)) . $nq . '" target="_blank">video</a>' : '') . '</li>';
        }
        echo '</ol>';
    }

    // filter tabs + table
    echo '<div class="tabs">'
       . '<a href="?filter=all' . $nq . '" class="' . ($filter === 'all' ? 'on' : '') . '">All passes</a>'
       . '<a href="?filter=speeders' . $nq . '" class="' . ($filter === 'speeders' ? 'on' : '') . '">Over limit</a></div>';
    render_table($view, $units, $limit_kmh, $nq, true);

    // -------- REJECTED BIN --------
    render_reject_bin($rejected, $units, $nq);

    // inline video player (clips grid) + close handlers
    echo '<div id="vidModal"><button class="x" onclick="skClose()">&times;</button>'
       . '<video id="vidPlayer" controls playsinline></video></div>';
    echo '<script>'
       . 'function skPlay(s){var m=document.getElementById("vidModal"),v=document.getElementById("vidPlayer");'
       . 'v.src=s;m.classList.add("show");var p=v.play();if(p&&p.catch)p.catch(function(){});}'
       . 'function skClose(){var m=document.getElementById("vidModal"),v=document.getElementById("vidPlayer");'
       . 'v.pause();v.removeAttribute("src");v.load();m.classList.remove("show");}'
       . 'document.getElementById("vidModal").addEventListener("click",function(e){if(e.target.id==="vidModal")skClose();});'
       . 'document.addEventListener("keydown",function(e){if(e.key==="Escape")skClose();});'
       . '</script>';

    echo auto_refresh_js();
    page_foot();
}

// Gentle self-refresh: reload every 30s so the page stays current (like the
// on-Pi live dashboard), but never while the operator is typing in a control,
// watching a clip, or has the tab in the background. Scroll position is
// restored only across an auto-reload, so it feels seamless.
function auto_refresh_js() {
    return '<script>(function(){'
      . 'try{if(sessionStorage.getItem("skAR")==="1"){var y=sessionStorage.getItem("skY");'
      . 'if(y!==null)window.scrollTo(0,parseInt(y));sessionStorage.removeItem("skAR");}}catch(e){}'
      . 'function busy(){var a=document.activeElement;'
      . 'if(a&&/^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName))return true;'
      . 'if(document.hidden)return true;'
      . 'var m=document.getElementById("vidModal");if(m&&m.classList.contains("show"))return true;'
      . 'return false;}'
      . 'setInterval(function(){if(busy())return;'
      . 'try{sessionStorage.setItem("skY",String(window.scrollY));sessionStorage.setItem("skAR","1");}catch(e){}'
      . 'location.reload();},30000);})();</script>';
}

function render_analytics($d) {
    extract($d);
    // period selector via ?p=
    $p = $_GET['p'] ?? 'today';
    if (!in_array($p, ['today', 'week', 'month', 'all'], true)) { $p = 'today'; }
    $nq = (!empty($sel_node)) ? '&node=' . rawurlencode($sel_node) : '';
    echo '<div class="panel"><h3>Analytics</h3>';
    echo '<div class="tabs">';
    foreach (['today'=>'Today','week'=>'Week','month'=>'Month','all'=>'All time'] as $k=>$lbl) {
        echo '<a href="?p=' . $k . $nq . '#an" class="' . ($p===$k?'on':'') . '">' . $lbl . '</a>';
    }
    echo '</div><a name="an"></a>';

    $count = ($p==='all') ? $c['total'] : $c[$p];
    $over  = ($p==='all') ? $sp['total'] : $sp[$p];
    $avg   = avg_of($spd[$p]);
    $avg_disp = $avg !== null ? round(to_disp($avg, $units)) : '—';
    echo '<div class="metrics">';
    echo '<div class="m"><div class="k">Vehicles</div><div class="v">' . $count . '</div><div class="s">' . h($p==='all'?'all time':$p) . '</div></div>';
    echo '<div class="m"><div class="k">Over limit</div><div class="v" style="color:var(--bad)">' . $over . '</div><div class="s">' . ($count?round($over/$count*100):0) . '% of traffic</div></div>';
    echo '<div class="m"><div class="k">Avg speed</div><div class="v">' . $avg_disp . '</div><div class="s">' . h(ulbl($units)) . '</div></div>';
    foreach (($spd_dir[$p] ?? []) as $dname => $list) {
        $a = avg_of($list);
        echo '<div class="m"><div class="k">Avg ' . h($dname) . '</div><div class="v">' . ($a!==null?round(to_disp($a,$units)):'—') . '</div><div class="s">' . h(ulbl($units)) . ' · ' . count($list) . ' veh</div></div>';
    }
    echo '</div>';

    echo '<div class="angrid">';
    // colour donut
    echo '<div><div class="ct">Colour breakdown — ' . h($p==='all'?'all time':$p) . '</div>' . render_donut($col[$p]) . '</div>';
    // avg speed by direction
    $dirItems = [];
    $maxdir = 1; foreach (($spd_dir[$p] ?? []) as $l) { $maxdir = max($maxdir, avg_of($l) ?? 0); }
    foreach (($spd_dir[$p] ?? []) as $dn => $l) {
        $a = avg_of($l); $dirItems[] = [$dn, $maxdir? ($a/$maxdir):0, round(to_disp($a,$units)) . ' ' . ulbl($units)];
    }
    echo '<div><div class="ct">Average speed by direction</div>' . render_hbars($dirItems) . '</div>';
    // day of week (all real passes)
    $dowNames = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
    $dowItems = [];
    $maxdow = 1; foreach ($dow as $x) { $maxdow = max($maxdow, $x['count']); }
    foreach ($dow as $i => $x) {
        $a = $x['count'] ? $x['sum']/$x['count'] : null;
        $dowItems[] = [$dowNames[$i], $maxdow? $x['count']/$maxdow:0,
            $x['count'] . ' · ' . ($a!==null?round(to_disp($a,$units)) . ulbl($units):'—')];
    }
    echo '<div><div class="ct">Traffic &amp; avg speed by day of week</div>' . render_hbars($dowItems) . '</div>';
    // speed distribution
    $histItems = []; $ax = [];
    for ($i=0;$i<count($hist);$i++){
        $histItems[] = [$hist[$i], 0, (($edges[$i+1]>=999)?($edges[$i].'+'):($edges[$i].'–'.$edges[$i+1])) . ' mph: ' . $hist[$i]];
        $ax[] = ($edges[$i]%10===0 || $edges[$i+1]>=999) ? ($edges[$i+1]>=999?$edges[$i].'+':$edges[$i]) : '';
    }
    echo '<div><div class="ct">Speed distribution (mph)</div>' . render_vbars($histItems, $ax) . '</div>';
    echo '</div>';

    // hour of day (full width)
    $hodItems = []; $hax = [];
    for ($i=0;$i<24;$i++){
        $a = $hod[$i]['count']?$hod[$i]['sum']/$hod[$i]['count']:null;
        $hodItems[] = [$hod[$i]['count'], 0, sprintf('%02d:00 — %d veh%s', $i, $hod[$i]['count'], $a!==null?(' · '.round(to_disp($a,$units)).ulbl($units)):'')];
        $hax[] = ($i%6===0)? $i : '';
    }
    echo '<div style="margin-top:1rem"><div class="ct">Traffic by hour of day</div>' . render_vbars($hodItems, $hax) . '</div>';
    echo '</div>';
}

function render_report_builder($d) {
    extract($d);
    $nq = (!empty($sel_node)) ? '<input type="hidden" name="node" value="' . h($sel_node) . '">' : '';
    $f = $rep['filters'] ?? [];
    echo '<div class="panel"><h3>Report builder</h3>';
    echo '<form method="get">' . $nq . '<input type="hidden" name="report" value="1">';
    echo '<div class="rb">';
    echo '<div class="fld"><label>From</label><input type="date" name="from" value="' . h($f['from'] ?? '') . '"></div>';
    echo '<div class="fld"><label>To</label><input type="date" name="to" value="' . h($f['to'] ?? '') . '"></div>';
    echo '<div class="fld"><label>Colour</label><select name="color"><option value="">any</option>';
    foreach ($all_colors as $co) {
        $secl = (($f['color'] ?? '') === strtolower($co)) ? ' selected' : '';
        echo '<option value="' . h($co) . '"' . $secl . '>' . h($co) . '</option>';
    }
    echo '</select></div>';
    echo '<div class="fld"><label>Direction</label><select name="direction"><option value="">any</option>';
    foreach ($all_dirs as $dn) {
        $secl = (($f['rdir'] ?? '') === strtolower($dn)) ? ' selected' : '';
        echo '<option value="' . h($dn) . '"' . $secl . '>' . h($dn) . '</option>';
    }
    echo '</select></div>';
    echo '<div class="fld"><label>Min ' . h(ulbl($units)) . '</label><input type="number" name="min_mph" value="' . h($f['minmph'] ?? '') . '"></div>';
    echo '<div class="fld"><label>Max ' . h(ulbl($units)) . '</label><input type="number" name="max_mph" value="' . h($f['maxmph'] ?? '') . '"></div>';
    echo '<div class="fld"><label>From hour</label><input type="number" min="0" max="23" name="hour_from" value="' . h($f['hf'] ?? '') . '"></div>';
    echo '<div class="fld"><label>To hour</label><input type="number" min="0" max="23" name="hour_to" value="' . h($f['ht'] ?? '') . '"></div>';
    echo '</div>';
    echo '<div style="margin:.7rem 0"><label>Days of week</label><div class="dowsel">';
    $dowNames = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
    $selDows = $f['dows'] ?? [];
    foreach ($dowNames as $i => $nm) {
        $ck = in_array($i, $selDows, true) ? ' checked' : '';
        echo '<label><input type="checkbox" name="dow[]" value="' . $i . '"' . $ck . '>' . $nm . '</label>';
    }
    echo '</div></div>';
    echo '<div style="display:flex;gap:.6rem;align-items:center;flex-wrap:wrap">'
       . '<button type="submit">Run report</button>'
       . '<a class="muted" href="?' . (!empty($sel_node)?'node='.rawurlencode($sel_node):'') . '" style="font-size:.85rem">reset</a></div>';
    echo '</form>';

    if ($rep !== null) {
        $avg = $rep['avg_kmh'] !== null ? round(to_disp($rep['avg_kmh'], $units)) : '—';
        $mx  = $rep['max_kmh'] !== null ? round(to_disp($rep['max_kmh'], $units)) : '—';
        echo '<div class="metrics" style="margin-top:1rem">'
           . '<div class="m"><div class="k">Matches</div><div class="v">' . $rep['count'] . '</div></div>'
           . '<div class="m"><div class="k">Avg speed</div><div class="v">' . $avg . '</div><div class="s">' . h(ulbl($units)) . '</div></div>'
           . '<div class="m"><div class="k">Fastest</div><div class="v">' . $mx . '</div><div class="s">' . h(ulbl($units)) . '</div></div>'
           . '<div class="m"><div class="k">Over limit</div><div class="v" style="color:var(--bad)">' . $rep['over'] . '</div></div>'
           . '</div>';
        if ($rep['colors']) {
            arsort($rep['colors']);
            echo '<div class="chips" style="margin:.6rem 0">';
            foreach ($rep['colors'] as $cn => $cv) {
                echo '<span class="chip"><span style="display:inline-block;width:9px;height:9px;border-radius:2px;background:' . swatch($cn) . ';margin-right:4px"></span>' . h($cn) . ' ' . $cv . '</span>';
            }
            echo '</div>';
        }
        $rows = array_slice($rep['rows'], 0, 200);
        render_table($rows, $units, $limit_kmh, $nq_link ?? ((!empty($sel_node))?'&node='.rawurlencode($sel_node):''), false);
        if (count($rep['rows']) > 200) {
            echo '<p class="muted" style="font-size:.82rem">Showing first 200 of ' . $rep['count'] . ' matches.</p>';
        }
    }
    echo '</div>';
}

function render_controls($d) {
    extract($d);
    $pending = ($desired_thr !== null) && ($camera_thr !== null)
        && ((float)$desired_thr != (float)$camera_thr);
    echo '<form method="post" class="ctl">'
       . (!empty($sel_node) ? '<input type="hidden" name="node" value="' . h($sel_node) . '">' : '')
       . '<div><label for="thr">SpeedKapture threshold (' . h($units) . ')</label>'
       . '<input id="thr" type="number" step="0.1" min="0" name="threshold" value="' . h($desired_thr ?? $camera_thr ?? 0) . '"></div>'
       . '<button type="submit" name="set_threshold" value="1">Set on camera</button>'
       . '<div class="muted" style="font-size:.85rem">';
    if ($pending) echo 'Queued ' . h($desired_thr) . ' &middot; camera still at ' . h($camera_thr) . ' &middot; applies on next check-in.';
    else echo 'Camera capturing everything above ' . h($camera_thr ?? '?') . ' ' . h($units) . '.';
    echo '</div></form>';

    $to_disp = fn($kmh) => ($kmh === null) ? null
        : (($units === 'mph') ? round((float)$kmh / 1.609344) : round((float)$kmh));
    $cam_limit_disp = $to_disp($limit_kmh);
    $des_limit_disp = $to_disp($desired_limit_kmh);
    $limit_pending = ($desired_limit_kmh !== null) && ($limit_kmh !== null)
        && (round((float)$desired_limit_kmh, 1) != round((float)$limit_kmh, 1));
    echo '<form method="post" class="ctl">'
       . (!empty($sel_node) ? '<input type="hidden" name="node" value="' . h($sel_node) . '">' : '')
       . '<input type="hidden" name="limit_units" value="' . ($units === 'mph' ? 'mph' : 'kmh') . '">'
       . '<div><label for="lim">My Road Speed Limit (' . h($units) . ')</label>'
       . '<input id="lim" type="number" step="1" min="1" name="limit" value="' . h($des_limit_disp ?? $cam_limit_disp ?? '') . '"></div>'
       . '<button type="submit" name="set_limit" value="1">Set on camera</button>'
       . '<div class="muted" style="font-size:.85rem">';
    if ($limit_pending) echo 'Queued ' . h($des_limit_disp) . ' ' . h($units) . ' &middot; camera still at ' . h($cam_limit_disp) . ' ' . h($units) . '.';
    else echo 'Vehicles above ' . h($cam_limit_disp ?? '?') . ' ' . h($units) . ' are flagged as speeding.';
    echo '</div></form>';

    // The "only count cars" car-filter is fixed policy in the node's code, not
    // a setting -- intentionally no control here. Its results still show up in
    // the auto-rejected bin below (transparency), which stays read-only.
}

// Shared event table. $with_reject adds a "reject" action column.
function render_table($view, $units, $limit_kmh, $nq, $with_reject) {
    if (!$view) { echo '<p class="muted">No records.</p>'; return; }
    echo '<table><thead><tr><th></th><th>Time</th><th>Speed</th><th>Dir</th><th>Vehicle</th><th>Clip</th>'
       . ($with_reject ? '<th></th>' : '') . '</tr></thead><tbody>';
    foreach ($view as $r) {
        $over = is_over($r, $limit_kmh);
        $snap = trim($r['snapshot'] ?? '');
        $clip = trim($r['clip'] ?? '');
        $veh = trim(implode(' ', array_filter([
            $r['color'] ?? '', $r['vehicle_type'] ?? '', $r['make'] ?? '',
            $r['model'] ?? '', $r['year'] ?? ''])));
        echo '<tr>';
        echo '<td>' . ($snap ? '<a href="?media=' . h(rawurlencode($snap)) . $nq . '" target="_blank">'
            . '<img class="thumb" src="?media=' . h(rawurlencode($snap)) . $nq . '" alt=""></a>' : '') . '</td>';
        echo '<td class="num muted">' . h(str_replace('T', ' ', substr($r['wall_time'] ?? '', 0, 16))) . '</td>';
        echo '<td class="num"><span class="pill' . ($over ? ' over' : '') . '">' . h(disp_speed($r, $units)) . ' ' . h($units) . '</span></td>';
        echo '<td class="muted">' . h($r['direction'] ?? '') . '</td>';
        echo '<td>' . h($veh ?: '—') . '</td>';
        echo '<td>' . ($clip ? '<a href="?media=' . h(rawurlencode($clip)) . $nq . '" target="_blank">video</a>' : '<span class="muted">—</span>') . '</td>';
        if ($with_reject) {
            echo '<td><form method="post" style="margin:0" onsubmit="return confirm(\'Reject this reading as not a vehicle?\')">'
               . '<input type="hidden" name="event_key" value="' . h(ev_row_key($r)) . '">'
               . '<button type="submit" name="set_status" value="rejected" class="ghost" style="padding:.25rem .55rem;font-size:.75rem;color:#ff9ab0;border-color:#5c2130">reject</button></form></td>';
        }
        echo '</tr>';
    }
    echo '</tbody></table>';
}

// A swatch colour for a named vehicle colour (mirrors the node dashboard).
function sk_color_hex($name) {
    static $map = [
        'black' => '#111418', 'white' => '#f4f4f5', 'gray' => '#9ca3af',
        'grey' => '#9ca3af', 'silver' => '#c8ccd2', 'red' => '#ef4444',
        'blue' => '#3b82f6', 'navy' => '#1e3a8a', 'green' => '#22c55e',
        'yellow' => '#eab308', 'orange' => '#f97316', 'brown' => '#8b5a2b',
        'beige' => '#d9c9a3', 'gold' => '#d4af37', 'tan' => '#d2b48c',
        'maroon' => '#7f1d1d', 'purple' => '#8b5cf6', 'pink' => '#ec4899',
    ];
    return $map[strtolower(trim($name))] ?? null;
}

// Last 10 recorded cars, any speed, newest first -- the off-site twin of the
// node's live-verification card. $rows is already non-rejected (real vehicles).
function render_last_cars($rows, $units, $limit_kmh) {
    echo '<h2 class="sec">Last 10 cars</h2>';
    $last = array_reverse(array_slice($rows, -10));   // newest first
    if (!$last) { echo '<p class="muted" style="font-size:.85rem">No cars recorded yet.</p>'; return; }
    echo '<ol class="lastcars">';
    foreach ($last as $r) {
        $over = is_over($r, $limit_kmh);
        $time = str_replace('T', ' ', substr($r['wall_time'] ?? '', 0, 19));  // to the second
        $col  = trim($r['color'] ?? '');
        $hex  = $col ? sk_color_hex($col) : null;
        $colHtml = $col
            ? '<i style="background:' . h($hex ?: 'var(--line)') . '"></i>' . h($col)
            : '<span class="none">no colour</span>';
        echo '<li>'
           . '<span class="lc-t">' . h($time) . '</span>'
           . '<span class="lc-sp' . ($over ? ' over' : '') . '">' . h(disp_speed($r, $units)) . ' ' . h($units) . '</span>'
           . '<span class="lc-col">' . $colHtml . '</span>'
           . '<span class="lc-dir">' . h($r['direction'] ?? '') . '</span>'
           . '</li>';
    }
    echo '</ol>';
    echo '<div class="lc-note">Every recorded car, any speed &mdash; newest first. Rejected non-cars are excluded. Confirm a car you saw drive by landed here.</div>';
}

function render_reject_bin($rejected, $units, $nq) {
    echo '<h2 class="sec">🚫 Auto-rejected readings (' . count($rejected) . ')</h2>';
    if (!$rejected) { echo '<p class="muted" style="font-size:.85rem">Nothing rejected. Cyclists, pedestrians and phantom tracks land here and stay out of every stat.</p>'; return; }
    echo '<details><summary>Show ' . count($rejected) . ' rejected — click Restore if one is really a vehicle</summary>';
    echo '<div class="rejgrid" style="margin-top:.7rem">';
    foreach (array_slice(array_reverse($rejected), 0, 60) as $r) {
        $snap = trim($r['snapshot'] ?? '');
        echo '<div class="rej">';
        if ($snap) echo '<a href="?media=' . h(rawurlencode($snap)) . $nq . '" target="_blank"><img src="?media=' . h(rawurlencode($snap)) . $nq . '" alt=""></a>';
        echo '<div class="b"><span style="font-weight:700">' . h(disp_speed($r, $units)) . ' ' . h($units) . ' · ' . h($r['direction'] ?? '') . '</span>'
           . '<span class="rsn">' . h($r['review_reason'] ?: 'auto-rejected') . '</span>'
           . '<span class="muted" style="font-size:.72rem">' . h(str_replace('T', ' ', substr($r['wall_time'] ?? '', 0, 16))) . '</span>'
           . '<form method="post" style="margin:0"><input type="hidden" name="event_key" value="' . h(ev_row_key($r)) . '">'
           . '<button type="submit" name="set_status" value="ok" class="ghost" style="padding:.3rem .6rem;font-size:.78rem">Restore as vehicle</button></form>'
           . '</div></div>';
    }
    echo '</div></details>';
}

function stat_card($label, $n, $sub, $extra = '') {
    echo '<div class="card"><div class="n">' . h($n) . '</div><div class="l">' . h($label) . '</div>'
       . '<div class="s">' . h($sub) . '</div>' . ($extra ?: '') . '</div>';
}

function dir_chips($dir) {
    if (!$dir) return '';
    arsort($dir);
    $out = '<div class="chips">';
    foreach ($dir as $k => $v) { $out .= '<span class="chip">' . h($k) . ' ' . h($v) . '</span>'; }
    return $out . '</div>';
}

// Fleet overview: every node's data merged into one broad picture, plus a
// per-node comparison. Shown at the bare dashboard URL (no ?node=). Drilling
// into a camera (?node=<id>) gives its full single-node dashboard.
function render_fleet($nodes_root, $nodes) {
    // Merge every node's real (non-rejected) passes into one dataset, tagging
    // each row with its node's speed limit so fleet-wide "over limit" respects
    // each street's own limit. Collect a per-node summary alongside.
    $fleet_rows = [];
    $summ = [];
    $unit_votes = [];
    $today = date('Y-m-d');
    foreach ($nodes as $n) {
        $ndir   = "$nodes_root/$n";
        $st     = json_decode(@file_get_contents("$ndir/status.json"), true) ?: [];
        $seen   = isset($st['received_at']) ? strtotime($st['received_at']) : null;
        $online = ($seen !== null) && (time() - $seen < 120);
        $nlimit = isset($st['speed_limit_kmh']) ? (float)$st['speed_limit_kmh'] : null;
        $nunits = (($st['units'] ?? 'mph') === 'kmh') ? 'kmh' : 'mph';
        $unit_votes[$nunits] = ($unit_votes[$nunits] ?? 0) + 1;

        [$hdr, $all] = load_events("$ndir/events.csv");
        $real = array_filter($all, fn($r) => !is_rejected($r));
        $n_today = 0; $n_over = 0; $speeds = []; $maxk = null;
        foreach ($real as $r) {
            $r['_limit_kmh'] = $nlimit;      // so fleet-wide is_over uses this street's limit
            $fleet_rows[] = $r;
            if (substr($r['wall_time'] ?? '', 0, 10) === $today) $n_today++;
            if (is_over($r, $nlimit)) $n_over++;
            if (is_numeric($r['speed_kmh'] ?? '')) {
                $k = (float)$r['speed_kmh']; $speeds[] = $k;
                if ($maxk === null || $k > $maxk) $maxk = $k;
            }
        }
        $summ[] = [
            'id' => $n, 'online' => $online, 'seen' => $seen,
            'total' => count($real), 'today' => $n_today, 'over' => $n_over,
            'avg_kmh' => avg_of($speeds), 'max_kmh' => $maxk,
            'limit_kmh' => $nlimit, 'units' => $nunits,
        ];
    }
    arsort($unit_votes);
    $units = array_key_first($unit_votes) ?: 'mph';

    // per-row _limit_kmh drives over-limit, so pass null as the fallback limit
    $agg = compute_stats($fleet_rows, null);

    usort($summ, function ($a, $b) {
        if ($a['online'] !== $b['online']) return $a['online'] ? -1 : 1;
        if ($a['today'] !== $b['today'])   return $b['today'] <=> $a['today'];
        return ($b['seen'] ?? 0) <=> ($a['seen'] ?? 0);
    });
    $online_n    = count(array_filter($summ, fn($s) => $s['online']));
    $fleet_total = array_sum(array_map(fn($s) => $s['total'], $summ));

    page_head('SpeedKam fleet');
    echo '<h1><span class="dot ' . ($online_n ? 'ok' : 'bad') . '"></span>SpeedKam fleet'
       . '<span class="grow"></span>'
       . '<span class="muted" style="font-size:.85rem">' . count($summ) . ' camera'
       . (count($summ) === 1 ? '' : 's') . ' &middot; ' . $online_n . ' online</span> '
       . '<a href="?logout" class="muted" style="font-size:.85rem">sign out</a></h1>';

    // fleet health
    echo '<div class="cards">';
    stat_card('Cameras', count($summ), $online_n . ' online');
    stat_card('Offline', count($summ) - $online_n, 'no check-in >2m');
    stat_card('Vehicles (all)', $fleet_total, 'across the fleet');
    stat_card('Over limit (all)', $agg['sp']['total'], 'across the fleet');
    echo '</div>';

    // combined traffic (all cameras summed)
    echo '<h2 class="sec">Combined traffic &mdash; all cameras</h2><div class="cards">';
    stat_card('Today', $agg['c']['today'], $agg['sp']['today'] . ' over limit', dir_chips($agg['dir']['today']));
    stat_card('This week', $agg['c']['week'], $agg['sp']['week'] . ' over limit', dir_chips($agg['dir']['week']));
    stat_card('This month', $agg['c']['month'], $agg['sp']['month'] . ' over limit', dir_chips($agg['dir']['month']));
    stat_card('All time', $agg['c']['total'], $agg['sp']['total'] . ' over limit');
    echo '</div>';

    // combined analytics panel (reuses the single-node renderer)
    render_analytics($agg + ['units' => $units, 'sel_node' => '']);

    // per-node comparison -- spot your worst streets at a glance
    echo '<h2 class="sec">Cameras</h2>';
    echo '<table><thead><tr><th></th><th>Camera</th><th>Last check-in</th>'
       . '<th class="num">Today</th><th class="num">All time</th>'
       . '<th class="num">Avg</th><th class="num">% over</th><th class="num">Fastest</th>'
       . '<th></th></tr></thead><tbody>';
    foreach ($summ as $s) {
        $sw   = $s['online'] ? 'ok' : 'bad';
        $href = '?node=' . rawurlencode($s['id']);
        $avg  = $s['avg_kmh'] !== null ? round(to_disp($s['avg_kmh'], $units)) . ' ' . ulbl($units) : '—';
        $mx   = $s['max_kmh'] !== null ? round(to_disp($s['max_kmh'], $units)) . ' ' . ulbl($units) : '—';
        $pct  = $s['total'] ? round($s['over'] / $s['total'] * 100) . '%' : '—';
        echo '<tr><td><span class="dot ' . $sw . '"></span></td>'
           . '<td><a href="' . h($href) . '"><code>' . h($s['id']) . '</code></a> '
           . '<span class="muted">' . ($s['online'] ? 'online' : 'offline') . '</span></td>'
           . '<td class="muted">' . h(ago($s['seen'])) . '</td>'
           . '<td class="num">' . h($s['today']) . '</td>'
           . '<td class="num">' . h($s['total']) . '</td>'
           . '<td class="num">' . h($avg) . '</td>'
           . '<td class="num">' . h($pct) . '</td>'
           . '<td class="num">' . h($mx) . '</td>'
           . '<td><a href="' . h($href) . '">open &rsaquo;</a></td></tr>';
    }
    echo '</tbody></table>';
    echo auto_refresh_js();
    page_foot();
}
