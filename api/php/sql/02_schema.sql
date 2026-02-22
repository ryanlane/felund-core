-- Felund relay — MySQL schema
-- The PHP application creates these tables automatically on first request,
-- so running this file is optional.  It is provided for review, pre-provisioning,
-- or deploying into environments where the app user lacks CREATE privilege.
--
-- Run as the felund_data user (or as root after running 01_create_user.sql):
--
--   mysql -u felund_data -p felund_data < api/php/sql/02_schema.sql

USE felund_data;

-- ── Presence / rendezvous ─────────────────────────────────────────────────────
-- Tracks which nodes are currently online in each circle.
-- circle_hint is SHA-256(circle_id)[0:16] — no circle secret is stored here.
-- Records expire after the TTL negotiated at registration time (default 120 s).

CREATE TABLE IF NOT EXISTS presence (
    circle_hint  VARCHAR(128)    NOT NULL  COMMENT 'SHA-256 prefix of circle_id',
    node_id      VARCHAR(128)    NOT NULL  COMMENT 'Opaque node identifier',
    endpoints    TEXT            NOT NULL  COMMENT 'JSON array of advertised endpoints',
    capabilities TEXT            NOT NULL  COMMENT 'JSON object of node capabilities',
    observed_at  BIGINT UNSIGNED NOT NULL  COMMENT 'Unix timestamp of last registration',
    expires_at   BIGINT UNSIGNED NOT NULL  COMMENT 'Unix timestamp after which row is stale',
    PRIMARY KEY (circle_hint, node_id),
    INDEX idx_circle  (circle_hint),
    INDEX idx_expires (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Peer presence records for the rendezvous service';

-- ── Relay messages ────────────────────────────────────────────────────────────
-- Store-and-forward message bus for clients that cannot open direct TCP
-- connections (e.g. browsers).  The server stores message payloads opaquely;
-- integrity is guaranteed end-to-end by HMAC-SHA256 and verified by clients.
--
-- stored_at is set server-side at INSERT time and used as the polling cursor
-- (GET /v1/messages?since=<stored_at>).  Using a server timestamp instead of
-- the client-supplied created_ts prevents clock-skew from causing messages to
-- be missed by pollers.
--
-- Messages are retained for up to 30 days (expires_at = stored_at + 2 592 000).

CREATE TABLE IF NOT EXISTS relay_messages (
    msg_id      VARCHAR(64)     NOT NULL  COMMENT 'Client-generated unique message ID',
    circle_hint VARCHAR(128)    NOT NULL  COMMENT 'SHA-256 prefix of circle_id',
    payload     MEDIUMTEXT      NOT NULL  COMMENT 'Full message JSON as received from client',
    created_ts  BIGINT UNSIGNED NOT NULL  COMMENT 'Client-supplied creation timestamp',
    stored_at   BIGINT UNSIGNED NOT NULL DEFAULT 0
                                          COMMENT 'Server-side insertion timestamp (polling cursor)',
    expires_at  BIGINT UNSIGNED NOT NULL  COMMENT 'Unix timestamp after which row may be pruned',
    PRIMARY KEY (msg_id),
    INDEX idx_rmsg_stored (circle_hint, stored_at),
    INDEX idx_rmsg_exp    (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Relay message store for browser and NAT-to-NAT clients';
