-- MySQL 一键初始化脚本（Windows / Linux 通用）
-- 执行前请确保 MySQL 服务已启动，并以 root 身份登录
-- 用法：mysql -u root -p123456 < init_mysql.sql

CREATE DATABASE IF NOT EXISTS ftpdb CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE ftpdb;

CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    password VARCHAR(255) NOT NULL,
    role VARCHAR(20) DEFAULT 'user',
    home_dir VARCHAR(255) DEFAULT '',
    quota_bytes BIGINT DEFAULT 0,
    used_bytes BIGINT DEFAULT 0,
    enabled BOOLEAN DEFAULT TRUE,
    last_login DATETIME NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ftp_config (
    id INT PRIMARY KEY DEFAULT 1,
    max_download_speed INT DEFAULT 0,
    max_upload_speed INT DEFAULT 0,
    max_connections INT DEFAULT 100,
    passive_ports_start INT DEFAULT 60000,
    passive_ports_end INT DEFAULT 60099,
    masquerade_address VARCHAR(64) DEFAULT '',
    CONSTRAINT chk_id CHECK (id = 1)
);

CREATE TABLE IF NOT EXISTS ftp_logs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(50) NOT NULL,
    action VARCHAR(50) NOT NULL,
    filename VARCHAR(255) DEFAULT '',
    remote_ip VARCHAR(64) DEFAULT '',
    bytes_transferred BIGINT DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 初始管理员（密码 admin123）
INSERT IGNORE INTO users
(username, password, role, home_dir, quota_bytes, used_bytes, enabled)
VALUES
('admin', '$2b$12$ez4O0SY3cVZ.WVTCkOB0OeJfG5jX8jwup7KnShqQeQ3KXZdE6Y2Oq', 'admin', '/ftp_root/admin', 0, 0, 1);

-- 初始普通用户（密码 guest123）
INSERT IGNORE INTO users
(username, password, role, home_dir, quota_bytes, used_bytes, enabled)
VALUES
('guest', '$2b$12$8d0UgVPU1Nn9fKkX5Y2yLuH5nUQlEpwQGbRBDd6GvKqYJn1PZuQOG', 'user', '/ftp_root/guest', 0, 0, 1);

-- 默认 FTP 配置行
INSERT IGNORE INTO ftp_config
(id, max_download_speed, max_upload_speed, max_connections,
 passive_ports_start, passive_ports_end, masquerade_address)
VALUES
(1, 0, 0, 100, 60000, 60099, '');