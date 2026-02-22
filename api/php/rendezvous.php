<?php
/**
 * Felund Rendezvous API — PHP / SQLite single-file implementation.
 *
 * Identical API contract to api/rendezvous.py (Python/FastAPI).
 *
 * Requirements
 *   PHP 8.1+  (match expressions, readonly properties, first-class callables)
 *   pdo_sqlite extension (enabled by default on most hosts)
 *
 * Quick-start
 *   # Built-in server (dev / LAN)
 *   php -S 0.0.0.0:8000 rendezvous.php
 *
 *   # Apache  — copy api/php/ into your document root, .htaccess is included.
 *   # nginx   — see nginx.conf in this directory.
 *
 * The SQLite database is written to DB_PATH below.
 * Make sure that path is writable by the web-server process and is NOT
 * directly accessible via HTTP (see .htaccess / nginx.conf).
 */

declare(strict_types=1);

// ── Configuration ─────────────────────────────────────────────────────────────

const VERSION       = '0.1.0';
const DB_PATH       = __DIR__ . '/data/felund_rendezvous.sqlite';
const MAX_ENDPOINTS = 16;
const MAX_LIMIT     = 200;

// ── Database ──────────────────────────────────────────────────────────────────

function db(): PDO
{
    static $pdo = null;
    if ($pdo !== null) {
        return $pdo;
    }

    $dir = dirname(DB_PATH);
    if (!is_dir($dir) && !mkdir($dir, 0750, true)) {
        http_error('Cannot create data directory', 500);
    }

    $pdo = new PDO('sqlite:' . DB_PATH);
    $pdo->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
    $pdo->setAttribute(PDO::ATTR_DEFAULT_FETCH_MODE, PDO::FETCH_ASSOC);

    // WAL mode: concurrent readers don't block the writer.
    // busy_timeout: if a write lock is held, wait up to 8 s before giving up
    // instead of failing immediately with SQLITE_BUSY.  Essential when multiple
    // PHP workers (or concurrent push requests) write at the same time.
    $pdo->exec('PRAGMA journal_mode=WAL');
    $pdo->exec('PRAGMA synchronous=NORMAL');
    $pdo->exec('PRAGMA busy_timeout=8000');
    $pdo->exec('PRAGMA cache_size=-4096');  // 4 MB page cache

    $pdo->exec('CREATE TABLE IF NOT EXISTS presence (
        circle_hint  TEXT    NOT NULL,
        node_id      TEXT    NOT NULL,
        endpoints    TEXT    NOT NULL DEFAULT "[]",
        capabilities TEXT    NOT NULL DEFAULT "{}",
        observed_at  INTEGER NOT NULL,
        expires_at   INTEGER NOT NULL,
        PRIMARY KEY (circle_hint, node_id)
    )');
    $pdo->exec('CREATE INDEX IF NOT EXISTS idx_circle  ON presence (circle_hint)');
    $pdo->exec('CREATE INDEX IF NOT EXISTS idx_expires ON presence (expires_at)');

    // Relay: shared message store for clients that cannot open direct TCP
    // connections (e.g. browsers).  Messages are HMAC-verified by clients;
    // the server stores them opaquely and never inspects the content.
    //
    // stored_at = server-side Unix timestamp set at INSERT time.  The
    // since-cursor used by GET /v1/messages is compared against stored_at
    // (not created_ts) so that client clock-skew cannot cause messages to
    // be missed.
    $pdo->exec('CREATE TABLE IF NOT EXISTS relay_messages (
        msg_id      TEXT    NOT NULL PRIMARY KEY,
        circle_hint TEXT    NOT NULL,
        payload     TEXT    NOT NULL,
        created_ts  INTEGER NOT NULL,
        stored_at   INTEGER NOT NULL DEFAULT 0,
        expires_at  INTEGER NOT NULL
    )');
    // Migration: add stored_at to pre-existing databases.
    try {
        $pdo->exec('ALTER TABLE relay_messages ADD COLUMN stored_at INTEGER NOT NULL DEFAULT 0');
        $pdo->exec('UPDATE relay_messages SET stored_at = created_ts WHERE stored_at = 0');
    } catch (\PDOException) {
        // Column already exists — nothing to do.
    }
    $pdo->exec('CREATE INDEX IF NOT EXISTS idx_rmsg_stored ON relay_messages (circle_hint, stored_at)');
    $pdo->exec('CREATE INDEX IF NOT EXISTS idx_rmsg_exp    ON relay_messages (expires_at)');

    return $pdo;
}

function db_prune(PDO $db, int $now): void
{
    $db->exec("DELETE FROM presence       WHERE expires_at <= $now");
    $db->exec("DELETE FROM relay_messages WHERE expires_at <= $now");
}

// ── HTTP helpers ──────────────────────────────────────────────────────────────

function json_response(mixed $data, int $status = 200): never
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    // Allow cross-origin requests so web-based clients can reach the API
    header('Access-Control-Allow-Origin: *');
    header('Access-Control-Allow-Methods: GET, POST, DELETE, OPTIONS');
    header('Access-Control-Allow-Headers: Content-Type, X-Felund-Node');
    echo json_encode($data, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    exit;
}

function http_error(string $message, int $status = 400): never
{
    json_response(['ok' => false, 'detail' => $message], $status);
}

// ── Input validation helpers ──────────────────────────────────────────────────

function json_body(): array
{
    $raw = (string) file_get_contents('php://input');
    if ($raw === '') {
        return [];
    }
    $data = json_decode($raw, true);
    if (!is_array($data)) {
        http_error('Request body must be a JSON object');
    }
    return $data;
}

function require_str(array $data, string $key, int $min = 1, int $max = 255): string
{
    $val = $data[$key] ?? null;
    if (!is_string($val) || strlen($val) < $min || strlen($val) > $max) {
        http_error("Field '$key' must be a string ($min–$max chars)");
    }
    return $val;
}

function optional_int(array $data, string $key, int $min, int $max, int $default): int
{
    if (!array_key_exists($key, $data)) {
        return $default;
    }
    $val = $data[$key];
    if (!is_int($val) || $val < $min || $val > $max) {
        http_error("Field '$key' must be an integer between $min and $max");
    }
    return $val;
}

function validate_endpoints(array $data): array
{
    $eps = $data['endpoints'] ?? [];
    if (!is_array($eps)) {
        http_error("'endpoints' must be an array");
    }
    if (count($eps) > MAX_ENDPOINTS) {
        http_error('Too many endpoints (max ' . MAX_ENDPOINTS . ')');
    }

    $valid_transport = ['tcp', 'ws'];
    $valid_family    = ['ipv4', 'ipv6'];
    $valid_nat       = ['unknown', 'open', 'restricted', 'symmetric'];

    foreach ($eps as $i => $ep) {
        if (!is_array($ep)) {
            http_error("endpoints[$i] must be an object");
        }
        if (!in_array($ep['transport'] ?? '', $valid_transport, true)) {
            http_error("endpoints[$i].transport must be one of: " . implode(', ', $valid_transport));
        }
        $host = $ep['host'] ?? '';
        if (!is_string($host) || $host === '' || strlen($host) > 255) {
            http_error("endpoints[$i].host is invalid");
        }
        $port = $ep['port'] ?? 0;
        if (!is_int($port) || $port < 1 || $port > 65535) {
            http_error("endpoints[$i].port must be 1–65535");
        }
        if (!in_array($ep['family'] ?? 'ipv4', $valid_family, true)) {
            http_error("endpoints[$i].family must be ipv4 or ipv6");
        }
        if (!in_array($ep['nat'] ?? 'unknown', $valid_nat, true)) {
            http_error("endpoints[$i].nat must be one of: " . implode(', ', $valid_nat));
        }
    }
    return $eps;
}

function validate_capabilities(array $data): array
{
    $caps = $data['capabilities'] ?? ['relay' => false, 'transport' => ['tcp']];
    if (!is_array($caps)) {
        http_error("'capabilities' must be an object");
    }
    // Normalise; ignore unknown keys
    return [
        'relay'     => (bool) ($caps['relay'] ?? false),
        'transport' => is_array($caps['transport'] ?? null)
            ? array_values(array_filter($caps['transport'], fn($t) => in_array($t, ['tcp', 'ws'], true)))
            : ['tcp'],
    ];
}

// ── Route handlers ────────────────────────────────────────────────────────────

function route_health(): never
{
    json_response(['ok' => true, 'version' => VERSION, 'time' => time()]);
}

function route_register(): never
{
    $data = json_body();

    $node_id     = require_str($data, 'node_id',     8, 128);
    $circle_hint = require_str($data, 'circle_hint', 8, 128);
    $ttl_s       = optional_int($data, 'ttl_s', 30, 600, 120);
    $endpoints   = validate_endpoints($data);
    $caps        = validate_capabilities($data);

    $now        = time();
    $expires_at = $now + $ttl_s;

    $db = db();
    db_prune($db, $now);

    $stmt = $db->prepare(
        'INSERT OR REPLACE INTO presence
         (circle_hint, node_id, endpoints, capabilities, observed_at, expires_at)
         VALUES (:ch, :nid, :ep, :caps, :obs, :exp)'
    );
    $stmt->execute([
        ':ch'   => $circle_hint,
        ':nid'  => $node_id,
        ':ep'   => json_encode($endpoints),
        ':caps' => json_encode($caps),
        ':obs'  => $now,
        ':exp'  => $expires_at,
    ]);

    // Best-effort observed host (behind a proxy, X-Forwarded-For takes priority).
    // We return only the IP address — NOT the source port.  The source port of
    // an HTTP request is an ephemeral port chosen by the OS and has nothing to
    // do with the peer's listen port, so including it would produce misleading
    // endpoint hints.  Clients that want to advertise their listen port should
    // include it in the 'endpoints' array of the register request.
    $observed_host = null;
    $remote_ip = trim(explode(',', $_SERVER['HTTP_X_FORWARDED_FOR']
        ?? $_SERVER['HTTP_X_REAL_IP']
        ?? $_SERVER['REMOTE_ADDR']
        ?? '')[0]);

    if ($remote_ip !== '') {
        $observed_host = [
            'host'   => $remote_ip,
            'family' => str_contains($remote_ip, ':') ? 'ipv6' : 'ipv4',
        ];
    }

    json_response([
        'ok'            => true,
        'server_time'   => $now,
        'expires_at'    => $expires_at,
        'observed_host' => $observed_host,
    ]);
}

function route_peers(): never
{
    $circle_hint = $_GET['circle_hint'] ?? '';
    if (strlen($circle_hint) < 8 || strlen($circle_hint) > 128) {
        http_error("Query param 'circle_hint' is required (8–128 chars)");
    }

    $limit = (int) ($_GET['limit'] ?? 50);
    $limit = max(1, min($limit, MAX_LIMIT));

    $exclude_node = $_SERVER['HTTP_X_FELUND_NODE'] ?? null;

    $now = time();
    $db  = db();

    if ($exclude_node !== null) {
        $stmt = $db->prepare(
            'SELECT * FROM presence
             WHERE circle_hint = :ch AND expires_at > :now AND node_id != :excl
             ORDER BY observed_at DESC
             LIMIT :lim'
        );
        $stmt->execute([':ch' => $circle_hint, ':now' => $now, ':excl' => $exclude_node, ':lim' => $limit]);
    } else {
        $stmt = $db->prepare(
            'SELECT * FROM presence
             WHERE circle_hint = :ch AND expires_at > :now
             ORDER BY observed_at DESC
             LIMIT :lim'
        );
        $stmt->execute([':ch' => $circle_hint, ':now' => $now, ':lim' => $limit]);
    }

    $peers = array_map(
        fn(array $row): array => [
            'node_id'      => $row['node_id'],
            'circle_hint'  => $row['circle_hint'],
            'endpoints'    => json_decode($row['endpoints'], true),
            'capabilities' => json_decode($row['capabilities'], true),
            'observed_at'  => (int) $row['observed_at'],
            'expires_at'   => (int) $row['expires_at'],
        ],
        $stmt->fetchAll()
    );

    json_response(['ok' => true, 'peers' => $peers]);
}

function route_unregister(): never
{
    $data = json_body();

    $node_id     = require_str($data, 'node_id',     8, 128);
    $circle_hint = require_str($data, 'circle_hint', 8, 128);

    $stmt = db()->prepare(
        'DELETE FROM presence WHERE circle_hint = :ch AND node_id = :nid'
    );
    $stmt->execute([':ch' => $circle_hint, ':nid' => $node_id]);

    json_response(['ok' => true, 'removed' => $stmt->rowCount() > 0]);
}

function route_messages_post(): never
{
    $data        = json_body();
    $circle_hint = require_str($data, 'circle_hint', 8, 128);
    $msgs        = $data['messages'] ?? [];
    if (!is_array($msgs)) {
        http_error("'messages' must be an array");
    }
    if (count($msgs) > 50) {
        http_error('Too many messages (max 50 per batch)');
    }

    $now  = time();
    $db   = db();
    db_prune($db, $now);

    $stmt    = $db->prepare(
        'INSERT OR IGNORE INTO relay_messages (msg_id, circle_hint, payload, created_ts, stored_at, expires_at)
         VALUES (:mid, :ch, :payload, :ts, :stored, :exp)'
    );
    $stored = 0;

    foreach ($msgs as $i => $msg) {
        if (!is_array($msg)) {
            http_error("messages[$i] must be an object");
        }
        $msg_id = $msg['msg_id'] ?? '';
        if (!is_string($msg_id) || strlen($msg_id) < 8 || strlen($msg_id) > 64) {
            http_error("messages[$i].msg_id is invalid");
        }
        $created_ts = $msg['created_ts'] ?? 0;
        if (!is_int($created_ts) || $created_ts < 0) {
            http_error("messages[$i].created_ts must be a non-negative integer");
        }
        $text = $msg['text'] ?? '';
        if (!is_string($text) || strlen($text) > 4096) {
            http_error("messages[$i].text exceeds 4 096 chars");
        }

        $stmt->execute([
            ':mid'     => $msg_id,
            ':ch'      => $circle_hint,
            ':payload' => json_encode($msg, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE),
            ':ts'      => $created_ts,
            ':stored'  => $now,                     // server-side insertion time
            ':exp'     => $now + 30 * 24 * 3600,   // keep for 30 days
        ]);
        if ($stmt->rowCount() > 0) {
            $stored++;
        }
    }

    json_response(['ok' => true, 'stored' => $stored]);
}

function route_messages_get(): never
{
    $circle_hint = $_GET['circle_hint'] ?? '';
    if (strlen($circle_hint) < 8 || strlen($circle_hint) > 128) {
        http_error("Query param 'circle_hint' is required (8–128 chars)");
    }

    $since = (int) ($_GET['since'] ?? 0);
    $limit = (int) ($_GET['limit'] ?? 200);
    $limit = max(1, min($limit, 500));

    $now  = time();
    $db   = db();

    $stmt = $db->prepare(
        'SELECT payload FROM relay_messages
         WHERE  circle_hint = :ch
           AND  stored_at   > :since
           AND  expires_at  > :now
         ORDER BY stored_at ASC, msg_id ASC
         LIMIT :lim'
    );
    $stmt->execute([':ch' => $circle_hint, ':since' => $since, ':now' => $now, ':lim' => $limit]);

    $messages = [];
    while ($row = $stmt->fetch()) {
        $decoded = json_decode($row['payload'], true);
        if (is_array($decoded)) {
            $messages[] = $decoded;
        }
    }

    json_response(['ok' => true, 'messages' => $messages, 'server_time' => $now]);
}

// ── Router ────────────────────────────────────────────────────────────────────

// Preflight CORS
if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') {
    header('Access-Control-Allow-Origin: *');
    header('Access-Control-Allow-Methods: GET, POST, DELETE, OPTIONS');
    header('Access-Control-Allow-Headers: Content-Type, X-Felund-Node');
    http_response_code(204);
    exit;
}

$method = $_SERVER['REQUEST_METHOD'];
$path   = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH);
// Normalise: strip everything up to and including /v1, then trailing slash
// Works whether the file lives at the root or under a subdirectory (e.g. /api)
$path   = '/' . ltrim(preg_replace('#^.*/v1#', '', rtrim($path ?? '/', '/')), '/');

match (true) {
    $method === 'GET'    && $path === '/health'   => route_health(),
    $method === 'POST'   && $path === '/register' => route_register(),
    $method === 'GET'    && $path === '/peers'    => route_peers(),
    $method === 'DELETE' && $path === '/register' => route_unregister(),
    $method === 'POST'   && $path === '/messages' => route_messages_post(),
    $method === 'GET'    && $path === '/messages' => route_messages_get(),
    default                                       => http_error("Not found: $method $path", 404),
};
