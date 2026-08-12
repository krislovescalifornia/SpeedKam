<?php
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

function is_authed() { return !empty($_SESSION['speedkam_auth']); }
function self_path() { return strtok($_SERVER['REQUEST_URI'], '?'); }

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
    if ($configured && hash_equals($DASHBOARD_PASSWORD, (string)$_POST['password'])) {
        session_regenerate_id(true);
        $_SESSION['speedkam_auth'] = true;
        header('Location: ' . self_path());
        exit;
    }
    $login_error = 'Incorrect password.';
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

// --- remote control write (authed): queue a desired SpeedKapture threshold ---
if ($_SERVER['REQUEST_METHOD'] === 'POST' && isset($_POST['set_threshold'])) {
    $val = $_POST['threshold'] ?? '';
    if (is_numeric($val) && (float)$val >= 0) {
        $dfile = "$DATA_DIR/desired.json";
        $cur = is_file($dfile) ? json_decode(@file_get_contents($dfile), true) : [];
        if (!is_array($cur)) { $cur = []; }
        $desired = [
            'speedkapture_threshold' => (float)$val,
            'rev'        => (int)($cur['rev'] ?? 0) + 1,
            'updated_at' => gmdate('c'),
        ];
        @file_put_contents($dfile, json_encode($desired));
    }
    header('Location: ' . self_path());
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

// --- aggregate counts -------------------------------------------------------
$today = date('Y-m-d');
$c  = ['today' => 0, 'week' => 0, 'month' => 0, 'total' => count($rows)];
$sp = ['today' => 0, 'week' => 0, 'month' => 0, 'total' => 0];
$wk = strtotime('-7 days'); $mo = strtotime('-30 days');
foreach ($rows as $r) {
    $d = substr($r['wall_time'] ?? '', 0, 10);
    $ts = $d ? strtotime($d) : false;
    $over = is_over($r, $limit_kmh);
    if ($over) $sp['total']++;
    if ($ts === false) continue;
    if ($d === $today) { $c['today']++; if ($over) $sp['today']++; }
    if ($ts >= $wk)    { $c['week']++;  if ($over) $sp['week']++; }
    if ($ts >= $mo)    { $c['month']++; if ($over) $sp['month']++; }
}

// --- table view (optionally filtered to speeders) ---------------------------
$filter = ($_GET['filter'] ?? 'all') === 'speeders' ? 'speeders' : 'all';
$view = array_reverse($rows);
if ($filter === 'speeders') {
    $view = array_values(array_filter($view, fn($r) => is_over($r, $limit_kmh)));
}
$view = array_slice($view, 0, 100);

$desired_thr = $desired['speedkapture_threshold'] ?? null;
$camera_thr  = $status['speedkapture_threshold'] ?? null;

render_dashboard(compact(
    'online', 'last_seen', 'units', 'limit_kmh', 'status', 'c', 'sp',
    'view', 'filter', 'desired', 'desired_thr', 'camera_thr'
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

    // header
    $sw = $online ? 'ok' : 'bad';
    echo '<h1><span class="dot ' . $sw . '"></span>SpeedKam '
       . '<span class="' . $sw . '">' . ($online ? 'online' : 'offline') . '</span>'
       . '<span class="grow"></span>'
       . '<span class="muted" style="font-size:.85rem">camera check-in '
       . h(ago($last_seen)) . '</span> '
       . '<a href="?logout" class="muted" style="font-size:.85rem">sign out</a></h1>';

    // live camera line
    $limit_disp = ($limit_kmh !== null)
        ? (($units === 'mph') ? round($limit_kmh / 1.609) . ' mph'
                              : round($limit_kmh) . ' km/h')
        : 'not set';
    echo '<div class="muted" style="font-size:.9rem;margin-top:-.5rem">'
       . 'Speed limit ' . h($limit_disp)
       . ' &middot; live FPS ' . h($status['fps'] ?? '?')
       . ' &middot; capturing above ' . h($camera_thr ?? '?') . ' ' . h($units)
       . '</div>';

    // stat cards
    echo '<div class="cards">';
    stat_card('Today', $c['today'], $sp['today'] . ' over limit');
    stat_card('This week', $c['week'], $sp['week'] . ' over limit');
    stat_card('This month', $c['month'], $sp['month'] . ' over limit');
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

    // filter tabs
    echo '<div class="tabs">'
       . '<a href="?filter=all" class="' . ($filter === 'all' ? 'on' : '') . '">All passes</a>'
       . '<a href="?filter=speeders" class="' . ($filter === 'speeders' ? 'on' : '') . '">Over limit</a>'
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
                ? '<a href="?media=' . h(rawurlencode($snap)) . '" target="_blank">'
                  . '<img class="thumb" src="?media=' . h(rawurlencode($snap)) . '" alt=""></a>'
                : '') . '</td>';
            echo '<td class="num muted">'
               . h(str_replace('T', ' ', substr($r['wall_time'] ?? '', 0, 16))) . '</td>';
            echo '<td class="num"><span class="pill' . ($over ? ' over' : '') . '">'
               . h(disp_speed($r, $units)) . ' ' . h($units) . '</span></td>';
            echo '<td class="muted">' . h($r['direction'] ?? '') . '</td>';
            echo '<td>' . h($veh ?: '—') . '</td>';
            echo '<td>' . ($clip
                ? '<a href="?media=' . h(rawurlencode($clip)) . '" target="_blank">video</a>'
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

function stat_card($label, $n, $sub) {
    echo '<div class="card"><div class="n">' . h($n) . '</div>'
       . '<div class="l">' . h($label) . '</div>'
       . '<div class="s">' . h($sub) . '</div></div>';
}
