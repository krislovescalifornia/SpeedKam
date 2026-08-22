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
 * It also offers BASIC REMOTE CONTROL: change the SpeedKapture threshold here
 * and it's written to desired.json; the camera pulls that on its next check-in
 * (see speedkam_receiver.php `action=sync`) and applies it. Control reaches a
 * camera behind home NAT because the camera reaches out -- nothing reaches in.
 *
 * SETUP: set $DASHBOARD_PASSWORD in speedkam_config.php, upload all three PHP
 * files together, then open this file's URL in a browser.
 */

require __DIR__ . '/speedkam_config.php';   // $SECRET, $DASHBOARD_PASSWORD, $DATA_DIR, $ALLOWED_EXT

session_start();

$configured = ($DASHBOARD_PASSWORD !== 'CHANGE-ME-dashboard-password'
    && $DASHBOARD_PASSWORD !== '');

// --- fleet mode: which camera are we viewing? -------------------------------
// The receiver buckets each node under $DATA_DIR/nodes/<id>/. $BASE_DIR is the
// root (used for the global login throttle); when a node is selected, $DATA_DIR
// points at that node's bucket so all the existing read/write logic below just
// works. No nodes/ dir at all -> legacy single-node layout, read $DATA_DIR as-is.
$BASE_DIR   = rtrim($DATA_DIR, '/');
$nodes_root = "$BASE_DIR/nodes";
$sel_node   = substr(preg_replace('/[^A-Za-z0-9_-]/', '', $_GET['node'] ?? ''), 0, 32);
if ($sel_node !== '' && is_dir("$nodes_root/$sel_node")) {
    $DATA_DIR = "$nodes_root/$sel_node";
}

function is_authed() { return !empty($_SESSION['speedkam_auth']); }
function self_path() { return strtok($_SERVER['REQUEST_URI'], '?'); }

// --- login rate limiting ----------------------------------------------------
// Slow down password guessing: after LOGIN_MAX_FAILS bad attempts from one IP
// within LOGIN_WINDOW seconds, lock that IP out for LOGIN_LOCKOUT seconds. State
// is a small JSON file in the (non-public) data dir. Keyed on REMOTE_ADDR only
// -- X-Forwarded-For is client-spoofable and must not gate security.
const LOGIN_MAX_FAILS = 5;
const LOGIN_WINDOW    = 900;   // count bad attempts over a 15-minute window
const LOGIN_LOCKOUT   = 900;   // lock out for 15 minutes once tripped

function login_client_ip() { return $_SERVER['REMOTE_ADDR'] ?? 'unknown'; }

// Seconds remaining on a lockout for this IP, or 0 if not locked.
function login_locked_seconds($dir, $ip) {
    $file = "$dir/.login_throttle.json";
    if (!is_file($file)) return 0;
    $map = json_decode(@file_get_contents($file), true);
    $until = is_array($map) ? (int)($map[$ip]['locked_until'] ?? 0) : 0;
    $left = $until - time();
    return $left > 0 ? $left : 0;
}

// Record the outcome of a login attempt: success clears the IP's counter, a
// failure increments it (and may start a lockout).
function login_record($dir, $ip, $success) {
    if (!is_dir($dir)) { @mkdir($dir, 0750, true); }
    $fh = @fopen("$dir/.login_throttle.json", 'c+');
    if ($fh === false) return;   // best-effort; never block login on IO failure
    if (flock($fh, LOCK_EX)) {
        $map = json_decode(stream_get_contents($fh), true);
        if (!is_array($map)) { $map = []; }
        $now = time();
        // Drop stale, unlocked entries so the file can't grow unbounded.
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
                $e['fails'] = 0; $e['first'] = $now;   // window elapsed -> reset
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
        $login_error = 'Too many attempts. Try again in '
            . ceil($locked / 60) . ' min.';
    } elseif ($configured
        && hash_equals($DASHBOARD_PASSWORD, (string)$_POST['password'])) {
        login_record($BASE_DIR, $ip, true);      // clear the counter
        session_regenerate_id(true);
        $_SESSION['speedkam_auth'] = true;
        header('Location: ' . self_path());
        exit;
    } else {
        login_record($BASE_DIR, $ip, false);     // count the failure
        $login_error = 'Incorrect password.';
    }
}

// --- authenticated media proxy (serves files from the .htaccess-denied dir) --
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
    // Basic HTTP range support so video clips can seek.
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

// --- fleet overview: no node chosen but per-node buckets exist -> list them --
if ($sel_node === '' && is_dir($nodes_root)) {
    $nodes = [];
    foreach (scandir($nodes_root) as $e) {
        if ($e === '.' || $e === '..' || !is_dir("$nodes_root/$e")) continue;
        $nodes[] = $e;
    }
    if ($nodes) { render_node_list($nodes_root, $nodes); exit; }
}

// --- remote control write (authed): queue desired settings for the node ------
// Each write MERGES into desired.json (preserving other queued settings) and
// bumps `rev`; the node adopts the whole set on its next check-in (RemoteControl).
function queue_desired($DATA_DIR, array $changes) {
    $dfile = "$DATA_DIR/desired.json";
    $cur = is_file($dfile) ? json_decode(@file_get_contents($dfile), true) : [];
    if (!is_array($cur)) { $cur = []; }
    $cur = array_merge($cur, $changes);
    $cur['rev']        = (int)($cur['rev'] ?? 0) + 1;
    $cur['updated_at'] = gmdate('c');
    @file_put_contents($dfile, json_encode($cur));
}

if ($_SERVER['REQUEST_METHOD'] === 'POST' && isset($_POST['set_threshold'])) {
    $val = $_POST['threshold'] ?? '';
    if (is_numeric($val) && (float)$val >= 0) {
        queue_desired($DATA_DIR, ['speedkapture_threshold' => (float)$val]);
    }
    header('Location: ' . self_path()
        . ($sel_node !== '' ? '?node=' . rawurlencode($sel_node) : ''));
    exit;
}

// --- My Road Speed Limit: entered in display units, queued to the node in km/h
if ($_SERVER['REQUEST_METHOD'] === 'POST' && isset($_POST['set_limit'])) {
    $val = $_POST['limit'] ?? '';
    $lu  = ($_POST['limit_units'] ?? 'mph') === 'kmh' ? 'kmh' : 'mph';
    if (is_numeric($val) && (float)$val > 0) {
        $kmh = ($lu === 'mph') ? (float)$val * 1.609344 : (float)$val;
        queue_desired($DATA_DIR, ['speed_limit_kmh' => round($kmh, 3)]);
    }
    header('Location: ' . self_path()
        . ($sel_node !== '' ? '?node=' . rawurlencode($sel_node) : ''));
    exit;
}

// ============================ load data =====================================
$status  = json_decode(@file_get_contents("$DATA_DIR/status.json"), true) ?: [];
$desired = json_decode(@file_get_contents("$DATA_DIR/desired.json"), true) ?: [];

$last_seen = isset($status['received_at']) ? strtotime($status['received_at']) : null;
$online    = ($last_seen !== null) && (time() - $last_seen < 120);
$units     = $status['units'] ?? 'mph';
$limit_kmh = isset($status['speed_limit_kmh']) ? (float)$status['speed_limit_kmh'] : null;

list($header, $rows) = load_events("$DATA_DIR/events.csv");

function load_events($csv) {
    if (!is_file($csv)) return [[], []];
    $fh = fopen($csv, 'r');
    if ($fh === false) return [[], []];
    $header = null; $rows = [];
    while (($r = fgetcsv($fh)) !== false) {
        if ($r === [null] || (count($r) === 1 && trim((string)$r[0]) === '')) {
            continue;  // skip blank lines
        }
        if ($header === null) { $header = $r; continue; }
        $r = array_slice(array_pad($r, count($header), ''), 0, count($header));
        $rows[] = array_combine($header, $r);
    }
    fclose($fh);
    return [$header, $rows];
}

function is_over($r, $limit_kmh) {
    return $limit_kmh !== null && is_numeric($r['speed_kmh'] ?? '')
        && (float)$r['speed_kmh'] > $limit_kmh;
}
function disp_speed($r, $units) {
    $v = ($units === 'mph') ? ($r['speed_mph'] ?? '') : ($r['speed_kmh'] ?? '');
    return is_numeric($v) ? rtrim(rtrim(number_format((float)$v, 1), '0'), '.') : '?';
}

// --- aggregate counts + direction breakdown ---------------------------------
// Period windows mirror the node's /api/summary EXACTLY so the off-site numbers
// match the on-Pi dashboard: "today" is the calendar day, "week" is since Monday
// of the current ISO week, "month" is the current calendar month. We also tally
// travel direction per period, the same breakdown the node shows under "Traffic
// summary".
$today        = date('Y-m-d');
$week_start   = date('Y-m-d', strtotime('monday this week'));
$month_prefix = date('Y-m');
$c   = ['today' => 0, 'week' => 0, 'month' => 0, 'total' => count($rows)];
$sp  = ['today' => 0, 'week' => 0, 'month' => 0, 'total' => 0];
$dir = ['today' => [], 'week' => [], 'month' => []];   // direction => count
foreach ($rows as $r) {
    $d    = substr($r['wall_time'] ?? '', 0, 10);
    $over = is_over($r, $limit_kmh);
    if ($over) $sp['total']++;
    if (!preg_match('/^\d{4}-\d{2}-\d{2}$/', $d)) continue;
    $dr = trim($r['direction'] ?? '');
    foreach (['today', 'week', 'month'] as $p) {
        $in = ($p === 'today') ? ($d === $today)
            : (($p === 'week')  ? ($d >= $week_start)
                                : (substr($d, 0, 7) === $month_prefix));
        if (!$in) continue;
        $c[$p]++;
        if ($over) $sp[$p]++;
        if ($dr !== '') $dir[$p][$dr] = ($dir[$p][$dr] ?? 0) + 1;
    }
}

// --- top 10 fastest passes (mirrors the node's "Top 10 speeders" panel) ------
$top = $rows;
usort($top, fn($a, $b) =>
    (float)($b['speed_kmh'] ?? -1) <=> (float)($a['speed_kmh'] ?? -1));
$top = array_slice($top, 0, 10);

// --- table view (optionally filtered to speeders) ---------------------------
$filter = ($_GET['filter'] ?? 'all') === 'speeders' ? 'speeders' : 'all';
$view = array_reverse($rows);
if ($filter === 'speeders') {
    $view = array_values(array_filter($view, fn($r) => is_over($r, $limit_kmh)));
}
$view = array_slice($view, 0, 100);

$desired_thr = $desired['speedkapture_threshold'] ?? null;
$camera_thr  = $status['speedkapture_threshold'] ?? null;
$desired_limit_kmh = $desired['speed_limit_kmh'] ?? null;  // queued, not yet applied

render_dashboard(compact(
    'online', 'last_seen', 'units', 'limit_kmh', 'status', 'c', 'sp', 'dir',
    'top', 'view', 'filter', 'desired', 'desired_thr', 'camera_thr',
    'desired_limit_kmh', 'sel_node'
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

function page_head($title) {
    echo '<!doctype html><html lang="en"><head><meta charset="utf-8">'
       . '<meta name="viewport" content="width=device-width,initial-scale=1">'
       . '<title>' . h($title) . '</title><style>'
       . ':root{color-scheme:light dark;--bg:#0b0f14;--card:#111823;--line:#223;'
       . '--fg:#e6edf3;--muted:#8b949e;--ok:#3fb950;--bad:#f85149;--accent:#388bfd}'
       . '*{box-sizing:border-box}body{font:15px/1.5 system-ui,-apple-system,Segoe UI,'
       . 'Roboto,sans-serif;margin:0;padding:1.25rem;background:var(--bg);color:var(--fg)}'
       . '.wrap{max-width:1000px;margin:0 auto}'
       . 'h1{font-size:1.25rem;margin:0 0 1rem;display:flex;align-items:center;gap:.6rem;flex-wrap:wrap}'
       . '.dot{width:.7rem;height:.7rem;border-radius:50%;display:inline-block}'
       . '.ok{color:var(--ok)}.bad{color:var(--bad)}.dot.ok{background:var(--ok)}.dot.bad{background:var(--bad)}'
       . '.muted{color:var(--muted)}.grow{flex:1}'
       . 'a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}'
       . '.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.75rem;margin:1rem 0}'
       . '.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1rem}'
       . '.card .n{font-size:1.7rem;font-weight:700;font-variant-numeric:tabular-nums}'
       . '.card .l{color:var(--muted);font-size:.8rem;text-transform:uppercase;letter-spacing:.04em}'
       . '.card .s{color:var(--muted);font-size:.85rem;margin-top:.15rem}'
       . 'table{width:100%;border-collapse:collapse;background:var(--card);'
       . 'border:1px solid var(--line);border-radius:12px;overflow:hidden}'
       . 'th,td{padding:.55rem .65rem;text-align:left;border-top:1px solid var(--line);vertical-align:middle}'
       . 'th{font-size:.75rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);border-top:0}'
       . 'td.num{font-variant-numeric:tabular-nums}'
       . 'img.thumb{height:44px;border-radius:6px;display:block}'
       . '.pill{padding:.15rem .5rem;border-radius:999px;font-size:.75rem;border:1px solid var(--line)}'
       . '.pill.over{color:var(--bad);border-color:var(--bad)}'
       . 'form.ctl{background:var(--card);border:1px solid var(--line);border-radius:12px;'
       . 'padding:1rem;margin:1rem 0;display:flex;gap:.6rem;align-items:end;flex-wrap:wrap}'
       . 'label{display:block;font-size:.8rem;color:var(--muted);margin-bottom:.25rem}'
       . 'input[type=number],input[type=password]{background:var(--bg);color:var(--fg);'
       . 'border:1px solid var(--line);border-radius:8px;padding:.5rem .6rem;font-size:1rem;width:8rem}'
       . 'button{background:var(--accent);color:#fff;border:0;border-radius:8px;'
       . 'padding:.55rem 1rem;font-size:.95rem;cursor:pointer}button:hover{filter:brightness(1.1)}'
       . '.tabs{display:flex;gap:.5rem;margin:.5rem 0}'
       . '.tabs a{padding:.3rem .7rem;border:1px solid var(--line);border-radius:999px;color:var(--fg)}'
       . '.tabs a.on{background:var(--accent);border-color:var(--accent);color:#fff}'
       . '.center{min-height:80vh;display:flex;align-items:center;justify-content:center}'
       // latest reading
       . '.latest{display:flex;align-items:center;gap:1rem;background:var(--card);'
       . 'border:1px solid var(--line);border-radius:12px;padding:1rem 1.25rem;margin:1rem 0}'
       . '.latest .big{font-size:2.4rem;font-weight:800;line-height:1;'
       . 'font-variant-numeric:tabular-nums;display:flex;align-items:baseline;gap:.35rem}'
       . '.latest .big.over{color:var(--bad)}'
       . '.latest .big .u{font-size:.9rem;font-weight:500;color:var(--muted)}'
       . '.latest .meta{color:var(--fg);font-size:.95rem}'
       // badge + chips (direction/attribute tags), shared by cards + latest
       . '.badge{display:inline-block;background:var(--bad);color:#fff;border-radius:6px;'
       . 'padding:.1rem .45rem;font-size:.7rem;font-weight:700;letter-spacing:.04em}'
       . '.chips{display:flex;gap:.3rem;flex-wrap:wrap;margin-top:.4rem}'
       . '.chip{border:1px solid var(--line);border-radius:999px;padding:.05rem .5rem;'
       . 'font-size:.72rem;color:var(--muted);font-variant-numeric:tabular-nums}'
       // section heading + top-speeders list
       . 'h2.sec{font-size:.8rem;text-transform:uppercase;letter-spacing:.05em;'
       . 'color:var(--muted);margin:1.5rem 0 .5rem}'
       . 'ol.toplist{list-style:none;counter-reset:t;margin:0;padding:0;'
       . 'background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}'
       . 'ol.toplist li{counter-increment:t;display:flex;align-items:center;gap:.75rem;'
       . 'padding:.55rem .8rem;border-top:1px solid var(--line)}'
       . 'ol.toplist li:first-child{border-top:0}'
       . 'ol.toplist li::before{content:counter(t);color:var(--muted);font-weight:700;'
       . 'width:1.4rem;text-align:right;font-variant-numeric:tabular-nums}'
       . 'ol.toplist .sp{font-weight:700;min-width:5.5rem;font-variant-numeric:tabular-nums}'
       . 'ol.toplist .sp.over{color:var(--bad)}'
       . 'ol.toplist .dt{margin-left:auto;font-size:.82rem}'
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
           . '<input id="pw" type="password" name="password" autofocus '
           . 'style="width:100%">'
           . '<div style="margin-top:.8rem"><button type="submit">Sign in</button></div>';
    }
    echo '</form></div>';
    page_foot();
}

function render_dashboard($d) {
    extract($d);
    page_head('SpeedKam dashboard');

    // fleet breadcrumb (only when viewing a specific node)
    if (!empty($sel_node)) {
        echo '<div style="margin-bottom:.5rem">'
           . '<a href="?" class="muted" style="font-size:.85rem">&lsaquo; all cameras</a>'
           . ' <span class="muted" style="font-size:.85rem">&middot; node '
           . '<code>' . h($sel_node) . '</code></span></div>';
    }

    // header
    $sw = $online ? 'ok' : 'bad';
    echo '<h1><span class="dot ' . $sw . '"></span>SpeedKam '
       . '<span class="' . $sw . '">' . ($online ? 'online' : 'offline') . '</span>'
       . '<span class="grow"></span>'
       . '<span class="muted" style="font-size:.85rem">camera check-in '
       . h(ago($last_seen)) . '</span> '
       . '<a href="?logout" class="muted" style="font-size:.85rem">sign out</a></h1>';

    // live camera line -- mirrors the on-Pi status pills (limit, fps, capture
    // threshold, mount orientation, calibration) so both dashboards read the same.
    $limit_disp = ($limit_kmh !== null)
        ? (($units === 'mph') ? round($limit_kmh / 1.609) . ' mph'
                              : round($limit_kmh) . ' km/h')
        : 'not set';
    $orient = (($status['orientation'] ?? '') === 'head_on') ? 'head-on' : 'parallel';
    $calib_txt = !empty($status['calibrated'])
        ? ('calibrated ' . h($status['calibration_points'] ?? '?') . 'pts'
            . (isset($status['reprojection_error_m']) && $status['reprojection_error_m'] !== null
                ? ' ±' . h($status['reprojection_error_m']) . 'm' : ''))
        : 'not calibrated';
    echo '<div class="muted" style="font-size:.9rem;margin-top:-.5rem">'
       . 'Speed limit ' . h($limit_disp)
       . ' &middot; live FPS ' . h($status['fps'] ?? '?')
       . ' &middot; capturing above ' . h($camera_thr ?? '?') . ' ' . h($units)
       . ' &middot; mount ' . h($orient)
       . ' &middot; ' . $calib_txt
       . '</div>';

    // latest reading -- the node's most recent pass, straight from the heartbeat.
    $ev = $status['last_event'] ?? null;
    if (is_array($ev)) {
        $ev_sp = ($units === 'mph') ? ($ev['speed_mph'] ?? null)
                                    : ($ev['speed_kmh'] ?? null);
        $ev_over = !empty($ev['over_limit']);
        $chips = trim(implode(' ', array_filter([
            $ev['color'] ?? '', $ev['vehicle_type'] ?? '',
            $ev['make'] ?? '', $ev['model'] ?? '', $ev['year'] ?? '',
        ])));
        echo '<div class="latest">'
           . '<div class="big' . ($ev_over ? ' over' : '') . '">'
           . h($ev_sp !== null ? round((float)$ev_sp) : '—')
           . '<span class="u">' . h($units) . '</span></div>'
           . '<div class="meta">'
           . ($ev_over ? '<span class="badge">SPEEDING</span> ' : '')
           . (isset($ev['captured']) && !$ev['captured']
                ? '<span class="chip">not captured</span> ' : '')
           . h($ev['direction'] ?? '')
           . '<div class="muted" style="font-size:.8rem">'
           . h(str_replace('T', ' ', substr($ev['time'] ?? '', 0, 16))) . '</div>'
           . ($chips ? '<div class="chips">' . h($chips) . '</div>' : '')
           . '</div></div>';
    }

    // stat cards -- counts + over-limit + travel-direction breakdown per period,
    // matching the node's "Traffic summary". "All time" spans the whole mirror.
    echo '<div class="cards">';
    stat_card('Today', $c['today'], $sp['today'] . ' over limit', dir_chips($dir['today']));
    stat_card('This week', $c['week'], $sp['week'] . ' over limit', dir_chips($dir['week']));
    stat_card('This month', $c['month'], $sp['month'] . ' over limit', dir_chips($dir['month']));
    stat_card('All time', $c['total'], $sp['total'] . ' over limit');
    echo '</div>';

    // control panel
    $pending = ($desired_thr !== null) && ($camera_thr !== null)
        && ((float)$desired_thr != (float)$camera_thr);
    echo '<form method="post" class="ctl">'
       . '<div><label for="thr">SpeedKapture threshold (' . h($units) . ')</label>'
       . '<input id="thr" type="number" step="0.1" min="0" name="threshold" '
       . 'value="' . h($desired_thr ?? $camera_thr ?? 0) . '"></div>'
       . '<button type="submit" name="set_threshold" value="1">Set on camera</button>'
       . '<div class="muted" style="font-size:.85rem">';
    if ($pending) {
        echo 'Queued ' . h($desired_thr) . ' &middot; camera still at '
           . h($camera_thr) . ' &middot; applies on next check-in.';
    } else {
        echo 'Camera capturing everything above ' . h($camera_thr ?? '?') . ' '
           . h($units) . '. Below it, passes are still counted &amp; logged.';
    }
    echo '</div></form>';

    // My Road Speed Limit -- entered in the node's display units, pushed as km/h.
    $to_disp = fn($kmh) => ($kmh === null) ? null
        : (($units === 'mph') ? round((float)$kmh / 1.609344) : round((float)$kmh));
    $cam_limit_disp = $to_disp($limit_kmh);
    $des_limit_disp = $to_disp($desired_limit_kmh);
    $limit_pending = ($desired_limit_kmh !== null) && ($limit_kmh !== null)
        && (round((float)$desired_limit_kmh, 1) != round((float)$limit_kmh, 1));
    echo '<form method="post" class="ctl">'
       . '<input type="hidden" name="limit_units" value="'
       . ($units === 'mph' ? 'mph' : 'kmh') . '">'
       . '<div><label for="lim">My Road Speed Limit (' . h($units) . ')</label>'
       . '<input id="lim" type="number" step="1" min="1" name="limit" '
       . 'value="' . h($des_limit_disp ?? $cam_limit_disp ?? '') . '"></div>'
       . '<button type="submit" name="set_limit" value="1">Set on camera</button>'
       . '<div class="muted" style="font-size:.85rem">';
    if ($limit_pending) {
        echo 'Queued ' . h($des_limit_disp) . ' ' . h($units)
           . ' &middot; camera still at ' . h($cam_limit_disp) . ' ' . h($units)
           . ' &middot; applies on next check-in.';
    } else {
        echo 'Vehicles above ' . h($cam_limit_disp ?? '?') . ' ' . h($units)
           . ' are flagged as speeding. Slower passes are still counted.';
    }
    echo '</div></form>';

    // Node query fragment: keep ?node=<id> on every in-view link/redirect so the
    // per-node context survives filter clicks, media requests and form posts.
    $nq = (!empty($sel_node)) ? '&node=' . rawurlencode($sel_node) : '';

    // top 10 speeders -- the fastest mirrored passes, matching the node's panel.
    if ($top) {
        echo '<h2 class="sec">Top 10 speeders</h2><ol class="toplist">';
        foreach ($top as $r) {
            $over = is_over($r, $limit_kmh);
            $clip = trim($r['clip'] ?? '');
            echo '<li>'
               . '<span class="sp' . ($over ? ' over' : '') . '">'
               . h(disp_speed($r, $units)) . ' ' . h($units) . '</span>'
               . '<span class="muted">' . h($r['direction'] ?? '') . '</span>'
               . '<span class="dt muted">'
               . h(str_replace('T', ' ', substr($r['wall_time'] ?? '', 0, 16))) . '</span>'
               . ($clip
                    ? '<a href="?media=' . h(rawurlencode($clip)) . $nq . '" target="_blank">video</a>'
                    : '') . '</li>';
        }
        echo '</ol>';
    }

    // filter tabs
    echo '<div class="tabs">'
       . '<a href="?filter=all' . $nq . '" class="' . ($filter === 'all' ? 'on' : '') . '">All passes</a>'
       . '<a href="?filter=speeders' . $nq . '" class="' . ($filter === 'speeders' ? 'on' : '') . '">Over limit</a>'
       . '</div>';

    // event table
    if (!$view) {
        echo '<p class="muted">No records yet. Once the camera backs events up '
           . 'here, they\'ll appear.</p>';
    } else {
        echo '<table><thead><tr><th></th><th>Time</th><th>Speed</th><th>Dir</th>'
           . '<th>Vehicle</th><th>Clip</th></tr></thead><tbody>';
        foreach ($view as $r) {
            $over = is_over($r, $limit_kmh);
            $snap = trim($r['snapshot'] ?? '');
            $clip = trim($r['clip'] ?? '');
            $veh = trim(implode(' ', array_filter([
                $r['color'] ?? '', $r['vehicle_type'] ?? '',
                $r['make'] ?? '', $r['model'] ?? '', $r['year'] ?? '',
            ])));
            echo '<tr>';
            echo '<td>' . ($snap
                ? '<a href="?media=' . h(rawurlencode($snap)) . $nq . '" target="_blank">'
                  . '<img class="thumb" src="?media=' . h(rawurlencode($snap)) . $nq . '" alt=""></a>'
                : '') . '</td>';
            echo '<td class="num muted">'
               . h(str_replace('T', ' ', substr($r['wall_time'] ?? '', 0, 16))) . '</td>';
            echo '<td class="num"><span class="pill' . ($over ? ' over' : '') . '">'
               . h(disp_speed($r, $units)) . ' ' . h($units) . '</span></td>';
            echo '<td class="muted">' . h($r['direction'] ?? '') . '</td>';
            echo '<td>' . h($veh ?: '—') . '</td>';
            echo '<td>' . ($clip
                ? '<a href="?media=' . h(rawurlencode($clip)) . $nq . '" target="_blank">video</a>'
                : '<span class="muted">—</span>') . '</td>';
            echo '</tr>';
        }
        echo '</tbody></table>';
        echo '<p class="muted" style="font-size:.82rem;margin-top:.6rem">'
           . 'Showing the ' . count($view) . ' most recent'
           . ($filter === 'speeders' ? ' over-limit' : '') . ' passes. '
           . 'Every pass is kept in the log even after old video is rotated away.</p>';
    }

    page_foot();
}

function stat_card($label, $n, $sub, $extra = '') {
    echo '<div class="card"><div class="n">' . h($n) . '</div>'
       . '<div class="l">' . h($label) . '</div>'
       . '<div class="s">' . h($sub) . '</div>'
       . ($extra ?: '') . '</div>';
}

// Render a period's travel-direction tally as chips ("→ 12", "← 9"), mirroring
// the direction breakdown the node shows under "Traffic summary". $dir is a
// map of direction-label => count.
function dir_chips($dir) {
    if (!$dir) return '';
    arsort($dir);
    $out = '<div class="chips">';
    foreach ($dir as $k => $v) {
        $out .= '<span class="chip">' . h($k) . ' ' . h($v) . '</span>';
    }
    return $out . '</div>';
}

// Fleet overview: one row per camera (node), linking into its own dashboard.
function render_node_list($nodes_root, $nodes) {
    page_head('SpeedKam fleet');

    $summ = [];
    foreach ($nodes as $n) {
        $dir  = "$nodes_root/$n";
        $st   = json_decode(@file_get_contents("$dir/status.json"), true) ?: [];
        $seen = isset($st['received_at']) ? strtotime($st['received_at']) : null;
        $online = ($seen !== null) && (time() - $seen < 120);
        // Cheap event count: data lines in events.csv minus the header row.
        $total = 0;
        $csv = "$dir/events.csv";
        if (is_file($csv)) {
            $lines = @file($csv, FILE_SKIP_EMPTY_LINES | FILE_IGNORE_NEW_LINES);
            if (is_array($lines)) { $total = max(0, count($lines) - 1); }
        }
        $summ[] = ['id' => $n, 'online' => $online, 'seen' => $seen, 'total' => $total];
    }
    // Online first, then most-recently-seen.
    usort($summ, function ($a, $b) {
        if ($a['online'] !== $b['online']) return $a['online'] ? -1 : 1;
        return ($b['seen'] ?? 0) <=> ($a['seen'] ?? 0);
    });
    $online_n = count(array_filter($summ, fn($s) => $s['online']));
    $fleet_total = array_sum(array_map(fn($s) => $s['total'], $summ));

    echo '<h1>SpeedKam fleet<span class="grow"></span>'
       . '<a href="?logout" class="muted" style="font-size:.85rem">sign out</a></h1>';
    echo '<div class="cards">';
    stat_card('Cameras', count($summ), $online_n . ' online');
    stat_card('Offline', count($summ) - $online_n, 'no check-in >2m');
    stat_card('Events (all)', $fleet_total, 'across the fleet');
    echo '</div>';

    echo '<table><thead><tr><th></th><th>Camera</th><th>Last check-in</th>'
       . '<th class="num">Events</th><th></th></tr></thead><tbody>';
    foreach ($summ as $s) {
        $sw  = $s['online'] ? 'ok' : 'bad';
        $href = '?node=' . rawurlencode($s['id']);
        echo '<tr>'
           . '<td><span class="dot ' . $sw . '"></span></td>'
           . '<td><a href="' . h($href) . '"><code>' . h($s['id']) . '</code></a> '
           . '<span class="muted">' . ($s['online'] ? 'online' : 'offline') . '</span></td>'
           . '<td class="muted">' . h(ago($s['seen'])) . '</td>'
           . '<td class="num">' . h($s['total']) . '</td>'
           . '<td><a href="' . h($href) . '">open &rsaquo;</a></td>'
           . '</tr>';
    }
    echo '</tbody></table>';
    page_foot();
}
