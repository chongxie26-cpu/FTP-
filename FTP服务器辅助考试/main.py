#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTP 服务器管理系统 - 主启动脚本
一键启动 FTP 服务、Web 管理面板和数据库初始化
"""

import os
import sys
import io

# 设置控制台输出编码为 UTF-8
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
import time
import threading
import signal
import argparse

# 全局状态
class SystemState:
    def __init__(self):
        self.ftp_controller = None
        self.is_running = False
        self.start_time = None

state = SystemState()


def ensure_directories():
    """确保必要的目录存在"""
    directories = [
        './ftp_root',
        './ftp_root/admin',
        './ftp_root/guest',
        './ftp_root/students',
        './ftp_root/exam_paper_pool'
    ]
    for directory in directories:
        os.makedirs(directory, exist_ok=True)


def init_database():
    """初始化数据库表结构"""
    print("\n" + "=" * 50)
    print("正在初始化数据库...")
    print("=" * 50)
    
    try:
        from config import DB_TYPE, SQLALCHEMY_DATABASE_URI
        db_type_text = "MySQL" if DB_TYPE == 'mysql' else "SQLite"
        print(f"✓ 当前数据库类型：{db_type_text}")
        if DB_TYPE != 'mysql':
            print("⚠ 当前不是 MySQL，考试批量生成账号将被阻止（仅允许写入 MySQL users 表）")
        if SQLALCHEMY_DATABASE_URI:
            masked_uri = SQLALCHEMY_DATABASE_URI
            if '@' in SQLALCHEMY_DATABASE_URI and '://' in SQLALCHEMY_DATABASE_URI:
                scheme, rest = SQLALCHEMY_DATABASE_URI.split('://', 1)
                if ':' in rest and '@' in rest and rest.index(':') < rest.index('@'):
                    user = rest.split(':', 1)[0]
                    after_at = rest.split('@', 1)[1]
                    masked_uri = f"{scheme}://{user}:***@{after_at}"
            print(f"✓ 数据库连接：{masked_uri}")

        from db_helper import init_db_tables
        success, message = init_db_tables()
        if success:
            print(f"✓ {message}")
        else:
            print(f"⚠ 数据库初始化警告：{message}")
            print("  将尝试使用现有数据库")
    except Exception as e:
        print(f"⚠ 数据库初始化异常：{e}")
        print("  将尝试使用现有数据库")


def start_ftp_server(ftp_port=2121):
    """启动 FTP 服务器"""
    print("\n" + "=" * 50)
    print("正在启动 FTP 服务器...")
    print("=" * 50)
    
    from ftp_server import FTPServerController
    
    controller = FTPServerController()
    controller.config['port'] = ftp_port
    state.ftp_controller = controller
    
    success, message = controller.start()
    print(message)
    
    if success:
        print(f"✓ FTP 服务监听端口：{ftp_port}")
        return controller
    else:
        print(f"✗ FTP 服务启动失败：{message}")
        return None


def start_web_server(web_port=8080, ftp_controller=None):
    """启动 Web 管理服务器（支持 SSE 实时通信）"""
    time.sleep(2)  # 等待 FTP 服务器初始化
    
    print("\n" + "=" * 50)
    print("正在启动 Web 管理面板...")
    print("=" * 50)
    
    import web_server
    
    # 注入 FTP 控制器
    web_server.controller = ftp_controller
    
    print(f"✓ Web 服务监听端口：{web_port}")
    print(f"✓ SSE 实时推送已启用")
    print(f"✓ 统一门户地址：http://localhost:{web_port}")
    print(f"✓ 管理员控制台：http://localhost:{web_port}/admin")
    print(f"✓ 用户文件浏览器：http://localhost:{web_port}/explorer")
    
    # 使用 Flask 内置服务器启动
    web_server.app.run(host='0.0.0.0', port=web_port, debug=False, threaded=True)


def print_banner():
    """打印启动横幅"""
    banner = """
    ╔══════════════════════════════════════════════════════════╗
    ║                                                          ║
    ║        🚀 企业级 FTP 文件管理系统 v2.0 🚀                 ║
    ║                                                          ║
    ║   FTP Server + Web Portal + MySQL Database               ║
    ║                                                          ║
    ║   功能特性：                                              ║
    ║   ✓ MySQL 用户认证体系                                    ║
    ║   ✓ Web 管理后台（仪表盘/用户/日志）                      ║
    ║   ✓ 用户文件浏览器（上传/下载/分享）                      ║
    ║   ✓ 限速/配额/断点续传                                   ║
    ║   ✓ 操作日志记录                                         ║
    ║   ✓ 文件分享链接                                         ║
    ║                                                          ║
    ╚══════════════════════════════════════════════════════════╝
    """
    print(banner)


def shutdown_handler(signum, frame):
    """优雅退出处理"""
    print("\n\n🛑 收到退出信号，正在停止服务...")
    
    if state.ftp_controller and state.ftp_controller.is_running:
        print("停止 FTP 服务器...")
        state.ftp_controller.stop()
    
    print("再见！👋")
    sys.exit(0)


def open_browser(web_port=8080):
    """自动打开浏览器"""
    time.sleep(4)
    url = f"http://localhost:{web_port}"
    print(f"\n🌐 正在打开统一门户：{url}")
    
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        print(f"⚠ 请手动访问：{url}")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='FTP 服务器管理系统')
    parser.add_argument('--ftp-port', type=int, default=2121, help='FTP 端口（默认：2121）')
    parser.add_argument('--web-port', type=int, default=8080, help='Web 端口（默认：8080）')
    parser.add_argument('--no-browser', action='store_true', help='不自动打开浏览器')
    args = parser.parse_args()
    
    print_banner()
    
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
    
    print("\n📋 系统初始化...")
    ensure_directories()
    init_database()
    
    print("\n✅ 初始化完成！")
    
    print("\n" + "=" * 50)
    print("📝 默认账户信息")
    print("=" * 50)
    print("管理员账户:")
    print("  用户名：admin")
    print("  密码：admin123")
    print("  权限：完全访问 + 管理面板")
    print("\n普通用户:")
    print("  用户名：guest")
    print("  密码：guest123")
    print("  权限：只读访问")
    print("=" * 50)
    
    print("\n🔄 启动服务...")
    
    # 启动 FTP 服务器
    ftp_controller = start_ftp_server(args.ftp_port)
    
    # 启动 Web 服务器（在新线程中）
    web_thread = threading.Thread(
        target=start_web_server, 
        args=(args.web_port, ftp_controller), 
        daemon=True
    )
    web_thread.start()
    
    if not args.no_browser:
        browser_thread = threading.Thread(target=open_browser, args=(args.web_port,), daemon=True)
        browser_thread.start()
    
    print("\n✨ 所有服务已启动！")
    print("\n💡 访问地址：")
    print(f"   - 统一门户：http://localhost:{args.web_port}")
    print(f"   - FTP 服务：ftp://localhost:{args.ftp_port}")
    print(f"   - 管理员控制台：http://localhost:{args.web_port}/admin")
    print(f"   - 用户文件浏览器：http://localhost:{args.web_port}/explorer")
    print("\n   按 Ctrl+C 停止所有服务")
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown_handler(None, None)


if __name__ == '__main__':
    main()