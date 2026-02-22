-- Felund relay — MySQL admin setup
-- Run this file once as a MySQL root / admin user:
--
--   mysql -u root -p < api/php/sql/01_create_user.sql
--
-- Replace 'changeme' with a strong password before running.
-- The same password must be set in api/php/.env as MYSQL_PASSWORD.

-- Create the database (utf8mb4 for full Unicode + emoji support)
CREATE DATABASE IF NOT EXISTS felund_data
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

-- Create the application user (localhost only by default)
-- Change 'localhost' to '%' to allow connections from any host,
-- or to a specific IP/hostname to restrict access further.
CREATE USER IF NOT EXISTS 'felund_data'@'localhost'
    IDENTIFIED BY 'changeme';

-- Grant only the privileges the application needs at runtime.
-- Tables are pre-created by running 02_schema.sql, so no DDL privileges
-- (CREATE, ALTER, DROP, INDEX) are needed for the application user.
GRANT SELECT, INSERT, UPDATE, DELETE
    ON felund_data.*
    TO 'felund_data'@'localhost';

FLUSH PRIVILEGES;
