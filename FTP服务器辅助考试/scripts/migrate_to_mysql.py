#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 SQLite 迁移数据到 MySQL
确保密码哈希保持一致
"""

import sqlite3
import pymysql
import bcrypt
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def migrate():
    # 连接 SQLite
    sqlite_conn = sqlite3.connect('data/ftp_server.db')
    sqlite_cursor = sqlite_conn.cursor()
    
    # 连接 MySQL
    mysql_conn = pymysql.connect(
        host='localhost',
        user='root',
        password='123456',
        database='ftpdb',
        charset='utf8mb4'
    )
    mysql_cursor = mysql_conn.cursor()
    
    try:
        # 创建表
        mysql_cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(50) UNIQUE NOT NULL,
                password VARCHAR(255) NOT NULL,
                role VARCHAR(20) DEFAULT 'user',
                home_dir VARCHAR(255) DEFAULT '',
                quota_bytes BIGINT DEFAULT 0,
                used_bytes BIGINT DEFAULT 0,
                enabled BOOLEAN DEFAULT TRUE,
                last_login DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        mysql_cursor.execute('''
            CREATE TABLE IF NOT EXISTS ftp_config (
                id INT AUTO_INCREMENT PRIMARY KEY,
                max_download_speed INT DEFAULT 0,
                max_upload_speed INT DEFAULT 0,
                max_connections INT DEFAULT 100,
                passive_ports_start INT DEFAULT 60000,
                passive_ports_end INT DEFAULT 60099,
                masquerade_address VARCHAR(64) DEFAULT ''
            )
        ''')
        
        mysql_cursor.execute('''
            CREATE TABLE IF NOT EXISTS ftp_logs (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(50) NOT NULL,
                action VARCHAR(50) NOT NULL,
                filename VARCHAR(255) DEFAULT '',
                remote_ip VARCHAR(64) DEFAULT '',
                bytes_transferred BIGINT DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        mysql_conn.commit()
        print("✓ MySQL 表结构创建成功")
        
        # 迁移用户数据
        sqlite_cursor.execute('SELECT id, username, password, role, home_dir, quota_bytes, used_bytes, enabled, last_login, created_at FROM users')
        users = sqlite_cursor.fetchall()
        
        if users:
            for user in users:
                # 检查用户是否已存在
                mysql_cursor.execute('SELECT id FROM users WHERE username = %s', (user[1],))
                if mysql_cursor.fetchone():
                    print(f"  跳过已存在用户：{user[1]}")
                    continue
                
                # 插入用户（保留原有密码哈希）
                mysql_cursor.execute('''
                    INSERT INTO users (username, password, role, home_dir, quota_bytes, used_bytes, enabled, last_login, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ''', (user[1], user[2], user[3], user[4], user[5], user[6], user[7], user[8], user[9]))
                print(f"✓ 迁移用户：{user[1]} -> {user[4]}")
            
            mysql_conn.commit()
            print(f"✓ 迁移了 {len(users)} 个用户")
        else:
            print("  SQLite 中没有用户数据，创建默认用户...")
            # 创建默认用户
            admin_hash = bcrypt.hashpw('admin123'.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
            guest_hash = bcrypt.hashpw('guest123'.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
            
            mysql_cursor.execute('''
                INSERT INTO users (username, password, role, home_dir, quota_bytes, enabled)
                VALUES (%s, %s, %s, %s, %s, %s)
            ''', ('admin', admin_hash, 'admin', '/ftp_root/admin', 0, True))
            
            mysql_cursor.execute('''
                INSERT INTO users (username, password, role, home_dir, quota_bytes, enabled)
                VALUES (%s, %s, %s, %s, %s, %s)
            ''', ('guest', guest_hash, 'user', '/ftp_root/guest', 0, True))
            
            mysql_conn.commit()
            print("✓ 创建默认用户：admin/admin123, guest/guest123")
        
        # 验证
        mysql_cursor.execute('SELECT username, role, home_dir FROM users')
        mysql_users = mysql_cursor.fetchall()
        print(f"\nMySQL 中的用户:")
        for u in mysql_users:
            print(f"  - {u[0]} ({u[1]}) -> {u[2]}")
        
        print("\n" + "=" * 60)
        print("迁移完成！")
        print("=" * 60)
        
    except Exception as e:
        print(f"✗ 错误：{e}")
        import traceback
        traceback.print_exc()
        mysql_conn.rollback()
    finally:
        sqlite_conn.close()
        mysql_conn.close()

if __name__ == '__main__':
    migrate()