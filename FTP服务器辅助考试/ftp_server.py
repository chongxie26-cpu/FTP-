#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTP Server Core - 基于 pyftpdlib 的高性能 FTP 服务器
支持 MySQL 用户认证、限速、配额、断点续传、日志记录
"""

import os
import sys
import time
import logging
import threading
from datetime import datetime
from functools import wraps

from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler, ThrottledDTPHandler
from pyftpdlib.servers import FTPServer
from pyftpdlib.filesystems import FilesystemError

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('ftp_server.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class MySQLAuthorizer:
    """MySQL 用户认证器 - 从数据库读取用户信息，支持虚拟目录映射"""
    
    def __init__(self):
        self._user_cache = {}
        self._cache_time = {}
        self._cache_ttl = 60  # 缓存 TTL（秒）
        # 虚拟目录映射：{username: {'/virtual/path': '/physical/path'}}
        self._virtual_dirs = {}
    
    def validate_authentication(self, username, password, handler=None):
        """验证用户登录 (pyftpdlib API: username, password, handler)"""
        # 明确拒绝匿名用户
        if not username or username.lower() in ('anonymous', 'anon', 'ftp', 'guest', ''):
            logger.warning(f'FTP 匿名登录被拒绝')
            return False

        try:
            from db_helper import authenticate_user

            logger.info(f"FTP 认证尝试：{username}")
            success, result = authenticate_user(username, password)
            
            if not success:
                logger.warning(f"认证失败：{username} - {result}")
                return False
            
            # 缓存用户信息
            self._user_cache[username] = result
            self._cache_time[username] = time.time()
            
            home = result.get('home_dir', './ftp_root')
            logger.info(f"用户认证成功：{username}, home_dir: {home}")
            return True
            
        except Exception as e:
            logger.error(f"认证异常：{username} - {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False
    
    def _resolve_home_dir(self, home_dir):
        """
        统一解析 home_dir：无论是 Unix 风格（/ftp_root/...）还是 Windows 风格，
        都转换为当前操作系统的物理绝对路径，基准目录为项目根目录。
        
        Unix 风格路径（/ftp_root/...）在所有平台上解析到 ./ftp_root/...
        -> 对应 Windows 上的 C:\\...\\project\\ftp_root\\...
        -> 对应 Linux/Mac 上的 /.../project/ftp_root/...
        """
        if not home_dir:
            return os.path.abspath('./ftp_root')

        # 统一分隔符：先全部转为当前 OS 的分隔符
        home_dir = home_dir.replace('/', os.sep)

        # Windows 绝对路径（带盘符）：C:\Users\...，保持不变
        if len(home_dir) >= 2 and home_dir[1] == ':':
            return home_dir

        # Unix 风格绝对路径（以 / 或 \ 开头）
        if home_dir.startswith(os.sep) or home_dir.startswith('/'):
            if sys.platform == 'win32':
                # 陷阱：Windows 上 os.path.abspath('/ftp_root/...') → C:\ftp_root\...
                # 这是 drive-relative 路径，不是项目目录！
                # 正确做法：去掉开头的 /ftp_root/ -> 'students/001'
                # 然后拼接 ./ftp_root/ -> ./ftp_root/students/001
                # 最后 os.path.abspath() 基于项目根目录解析
                stripped = home_dir.lstrip('/\\')
                if stripped.startswith('ftp_root' + os.sep) or stripped == 'ftp_root':
                    suffix = stripped[len('ftp_root'):].lstrip(os.sep)
                    home_dir = ('./ftp_root/' + suffix) if suffix else './ftp_root'
                else:
                    home_dir = './' + stripped
            else:
                # Unix 系统：直接添加 '.' 前缀
                home_dir = '.' + home_dir

        # 统一分隔符
        home_dir = home_dir.replace('/', os.sep)

        # 如果是相对路径，转换为绝对路径
        if not os.path.isabs(home_dir):
            home_dir = os.path.abspath(home_dir)

        return home_dir
    
    def get_home_dir(self, username):
        """获取用户主目录（物理路径）"""
        user = self._get_cached_user(username)
        if not user:
            return self._resolve_home_dir('./ftp_root')

        return self._resolve_home_dir(user.get('home_dir', './ftp_root'))

    def has_user(self, username):
        """
        检查用户是否存在于数据库（pyftpdlib 用来判断是否允许匿名等）
        返回 True 才允许该用户登录；返回 False 则视为匿名或不存在
        关键：禁止匿名访问，只允许数据库中已注册的用户登录
        """
        # 明确拒绝空用户名（匿名访问）
        if not username or username.lower() in ('anonymous', 'anon', 'ftp', 'guest'):
            logger.warning(f'拒绝匿名访问尝试：{username}')
            return False
        # 查询数据库是否有此用户
        try:
            from db_helper import get_user_by_username
            success, _ = get_user_by_username(username)
            if success:
                return True
            logger.warning(f'用户不存在：{username}')
            return False
        except Exception as e:
            logger.error(f'查询用户失败：{e}')
            return False

    def get_mapping(self, username, path):
        """
        获取路径映射（支持虚拟目录）
        pyftpdlib 会调用此方法来解析虚拟路径
        """
        user = self._get_cached_user(username)
        if not user:
            return None
        
        # 检查是否有虚拟目录配置
        virtual_dirs = user.get('virtual_dirs', {})
        
        # 检查请求路径是否匹配虚拟目录
        for virt_path, config in virtual_dirs.items():
            if path.startswith(virt_path + '/') or path == virt_path:
                # 替换虚拟路径为物理路径
                rel_path = path[len(virt_path):].lstrip('/')
                physical_base = config['path']
                
                # 统一解析路径
                physical_base = self._resolve_home_dir(physical_base)
                
                if rel_path:
                    return os.path.join(physical_base, rel_path)
                else:
                    return physical_base
        
        # 无匹配则返回 None，使用默认路径解析
        return None

    def get_perms(self, username):
        """获取用户权限"""
        user = self._get_cached_user(username)
        if not user:
            return 'elr'
        base_perms = user.get('perms', 'elr')
        if username in self._virtual_dirs and self._virtual_dirs[username]:
            if 'w' not in base_perms:
                base_perms += 'w'
            if 'a' not in base_perms:
                base_perms += 'a'
        return base_perms

    def has_perm(self, username, perm, path=None):
        """检查用户是否有指定权限"""
        perms = self.get_perms(username)
        return perm in perms

    def get_max_connections(self, username):
        """获取用户最大连接数"""
        user = self._get_cached_user(username)
        return user.get('max_connections', 5) if user else 5

    def get_msg_login(self, username):
        """获取登录消息"""
        return "Login successful. Welcome to Inscode FTP Server!"

    def impersonate_user(self, username, password):
        """模拟用户操作（pyftpdlib 要求）"""
        pass

    def terminate_impersonation(self, username):
        """终止模拟用户（pyftpdlib 要求）"""
        pass

    def close_connection(self, username):
        """关闭连接（pyftpdlib 要求）"""
        pass

    def remove_user(self, username):
        """移除用户（pyftpdlib 要求）"""
        pass

    def get_msg_quota(self, username):
        """获取配额消息"""
        quota = self.get_quota(username)
        if quota:
            used, max_bytes = quota
            if max_bytes > 0:
                return f"Storage: {used}/{max_bytes} bytes"
        return ""

    def get_bandwidth_limit(self, username):
        """获取用户带宽限制（字节/秒）"""
        user = self._get_cached_user(username)
        limit = user.get('speed_limit_kbps', 0) if user else 0
        return limit * 1024 if limit > 0 else None

    def get_quota(self, username):
        """获取用户配额"""
        try:
            from db_helper import get_user_by_username
            success, result = get_user_by_username(username)
            if not success:
                return None
            return (result.get('used_bytes', 0), result.get('quota_bytes', 0))
        except Exception as e:
            logger.error(f"获取配额失败：{e}")
            return None

    def update_quota(self, username, bytes_delta):
        """更新用户已用空间"""
        try:
            from db_helper import update_user_quota
            update_user_quota(username, bytes_delta)
        except Exception as e:
            logger.error(f"更新配额失败：{e}")

    def _get_cached_user(self, username):
        """获取缓存的用户信息"""
        try:
            from db_helper import get_user_by_username
            success, result = get_user_by_username(username)
            if success:
                self._user_cache[username] = result
                self._cache_time[username] = time.time()
            return result if success else None
        except Exception as e:
            logger.error(f"获取用户失败：{e}")
            return None

    def add_virtual_dir(self, username, virtual_path, physical_path, perms='elr'):
        """为指定用户添加虚拟目录映射"""
        try:
            os.makedirs(physical_path, exist_ok=True)
            user = self._get_cached_user(username)
            if user:
                if 'virtual_dirs' not in user:
                    user['virtual_dirs'] = {}
                user['virtual_dirs'][virtual_path] = {
                    'path': physical_path,
                    'perms': perms
                }
                logger.info(f"已为用户 {username} 添加虚拟目录：{virtual_path} -> {physical_path}")
                return True
            else:
                logger.warning(f"用户不存在，无法添加虚拟目录：{username}")
                return False
        except Exception as e:
            logger.error(f"添加虚拟目录失败：{e}")
            return False

    def remove_virtual_dir(self, username, virtual_path):
        """移除用户的虚拟目录映射"""
        try:
            user = self._get_cached_user(username)
            if user and 'virtual_dirs' in user and virtual_path in user['virtual_dirs']:
                del user['virtual_dirs'][virtual_path]
                logger.info(f"已移除用户 {username} 的虚拟目录：{virtual_path}")
                return True
            return False
        except Exception as e:
            logger.error(f"移除虚拟目录失败：{e}")
            return False

    def get_virtual_dirs(self, username):
        """获取用户的所有虚拟目录映射"""
        return self._virtual_dirs.get(username, {})


class CustomFTPHandler(FTPHandler):
    """自定义 FTP 处理器，支持限速、配额、日志、虚拟目录"""

    # 类变量用于统计
    active_connections = {}
    total_bytes_transferred = 0
    connection_history = []
    _lock = threading.Lock()
    
    def ftp_OPENDIR(self, path):
        """重写目录列表操作，支持虚拟目录"""
        try:
            username = getattr(self, 'username', None)
            if username:
                authorizer = getattr(self, 'authorizer', None)
                if authorizer and hasattr(authorizer, 'get_mapping'):
                    mapped_path = authorizer.get_mapping(username, path)
                    if mapped_path:
                        logger.debug(f"虚拟目录映射：{path} -> {mapped_path}")
                        return super().ftp_OPENDIR(mapped_path)
        except Exception as e:
            logger.debug(f"ftp_OPENDIR 处理异常：{e}")
        
        return super().ftp_OPENDIR(path)
    
    def ftp_RETR(self, path):
        """重写下载操作，支持虚拟目录"""
        try:
            username = getattr(self, 'username', None)
            if username:
                authorizer = getattr(self, 'authorizer', None)
                if authorizer and hasattr(authorizer, 'get_mapping'):
                    mapped_path = authorizer.get_mapping(username, path)
                    if mapped_path:
                        logger.debug(f"下载虚拟目录映射：{path} -> {mapped_path}")
                        return super().ftp_RETR(mapped_path)
        except Exception as e:
            logger.debug(f"ftp_RETR 处理异常：{e}")
        
        return super().ftp_RETR(path)
    
    def ftp_STOR(self, path):
        """重写上传操作，支持虚拟目录"""
        try:
            username = getattr(self, 'username', None)
            if username:
                authorizer = getattr(self, 'authorizer', None)
                if authorizer and hasattr(authorizer, 'get_mapping'):
                    mapped_path = authorizer.get_mapping(username, path)
                    if mapped_path:
                        logger.debug(f"上传虚拟目录映射：{path} -> {mapped_path}")
                        return super().ftp_STOR(mapped_path)
        except Exception as e:
            logger.debug(f"ftp_STOR 处理异常：{e}")
        
        return super().ftp_STOR(path)
    
    def on_connect(self):
        """连接建立时调用"""
        try:
            ip = self.remote_addr[0] if self.remote_addr else 'unknown'
            with CustomFTPHandler._lock:
                CustomFTPHandler.active_connections[ip] = {
                    'username': 'anonymous',
                    'connected_at': datetime.now().isoformat(),
                    'bytes_sent': 0,
                    'bytes_received': 0,
                    'ip': ip
                }
                CustomFTPHandler.connection_history.append({
                    'event': 'connect',
                    'ip': ip,
                    'time': datetime.now().isoformat()
                })
            logger.info(f"新连接：{ip}")
        except Exception as e:
            logger.debug(f"on_connect 处理异常：{e}")
    
    def on_disconnect(self):
        """连接断开时调用"""
        try:
            ip = self.remote_addr[0] if self.remote_addr else 'unknown'
            with CustomFTPHandler._lock:
                if ip in CustomFTPHandler.active_connections:
                    del CustomFTPHandler.active_connections[ip]
                CustomFTPHandler.connection_history.append({
                    'event': 'disconnect',
                    'ip': ip,
                    'time': datetime.now().isoformat()
                })
            logger.info(f"连接断开：{ip}")
        except Exception as e:
            logger.debug(f"on_disconnect 处理异常：{e}")
    
    def on_login(self, username):
        """登录时调用"""
        try:
            self.username = username
            ip = self.remote_addr[0] if self.remote_addr else 'unknown'
            
            with CustomFTPHandler._lock:
                if ip in CustomFTPHandler.active_connections:
                    CustomFTPHandler.active_connections[ip]['username'] = username
                CustomFTPHandler.connection_history.append({
                    'event': 'login',
                    'username': username,
                    'ip': ip,
                    'time': datetime.now().isoformat()
                })
            
            # 应用带宽限制
            bandwidth = self.authorizer.get_bandwidth_limit(username)
            if bandwidth:
                self.dtp_handler = ThrottledDTPHandler
                self.dtp_handler.read_limit = bandwidth
                self.dtp_handler.write_limit = bandwidth
            
            logger.info(f"用户登录：{username} from {ip}")
            
            # 记录到数据库
            from db_helper import log_ftp_action
            log_ftp_action(username, 'login', remote_ip=ip)
            
        except Exception as e:
            logger.debug(f"on_login 处理异常：{e}")
    
    def on_logout(self, username):
        """登出时调用"""
        try:
            ip = self.remote_addr[0] if self.remote_addr else 'unknown'
            with CustomFTPHandler._lock:
                CustomFTPHandler.connection_history.append({
                    'event': 'logout',
                    'username': username,
                    'ip': ip,
                    'time': datetime.now().isoformat()
                })
            logger.info(f"用户登出：{username}")
            
            from db_helper import log_ftp_action
            log_ftp_action(username, 'logout', remote_ip=ip)
            
        except Exception as e:
            logger.debug(f"on_logout 处理异常：{e}")
    
    def on_file_sent(self, file_path, bytes_count):
        """文件发送完成时调用"""
        try:
            with CustomFTPHandler._lock:
                CustomFTPHandler.total_bytes_transferred += bytes_count
                self._bytes_sent = getattr(self, '_bytes_sent', 0) + bytes_count
                
                ip = self.remote_addr[0] if self.remote_addr else 'unknown'
                if ip in CustomFTPHandler.active_connections:
                    CustomFTPHandler.active_connections[ip]['bytes_sent'] = \
                        CustomFTPHandler.active_connections[ip].get('bytes_sent', 0) + bytes_count
            
            # 更新配额
            username = getattr(self, 'username', None)
            if username:
                # 下载不增加 used_bytes，只记录日志
                from db_helper import log_ftp_action
                log_ftp_action(username, 'download', filename=file_path, 
                              bytes_transferred=bytes_count, remote_ip=ip)
            
            logger.info(f"文件发送：{file_path} ({bytes_count} bytes)")
        except Exception as e:
            logger.debug(f"on_file_sent 处理异常：{e}")
    
    def on_file_received(self, file_path, bytes_count):
        """文件接收完成时调用"""
        try:
            with CustomFTPHandler._lock:
                CustomFTPHandler.total_bytes_transferred += bytes_count
                self._bytes_received = getattr(self, '_bytes_received', 0) + bytes_count
                
                ip = self.remote_addr[0] if self.remote_addr else 'unknown'
                if ip in CustomFTPHandler.active_connections:
                    CustomFTPHandler.active_connections[ip]['bytes_received'] = \
                        CustomFTPHandler.active_connections[ip].get('bytes_received', 0) + bytes_count
            
            # 更新配额
            username = getattr(self, 'username', None)
            if username:
                from db_helper import update_user_quota, log_ftp_action
                update_user_quota(username, bytes_count)  # 上传增加已用空间
                log_ftp_action(username, 'upload', filename=file_path, 
                              bytes_transferred=bytes_count, remote_ip=ip)
            
            logger.info(f"文件接收：{file_path} ({bytes_count} bytes)")
        except Exception as e:
            logger.debug(f"on_file_received 处理异常：{e}")
    
    def on_upload_started(self, file_path):
        """上传开始时调用 - 检查配额"""
        try:
            username = getattr(self, 'username', None)
            if username:
                quota_info = self.authorizer.get_quota(username)
                if quota_info:
                    used, max_quota = quota_info
                    logger.debug(f"用户上传检查：{username}, 已用：{used}, 配额：{max_quota}")
        except Exception as e:
            logger.debug(f"on_upload_started 处理异常：{e}")
    
    @classmethod
    def get_stats(cls):
        """获取统计信息"""
        with cls._lock:
            return {
                'active_connections': len(cls.active_connections),
                'connections_detail': dict(cls.active_connections),
                'total_bytes_transferred': cls.total_bytes_transferred,
                'connection_history': cls.connection_history[-100:]
            }
    
    @classmethod
    def reset_stats(cls):
        """重置统计信息"""
        with cls._lock:
            cls.active_connections = {}
            cls.total_bytes_transferred = 0
            cls.connection_history = []


class FTPServerController:
    """FTP 服务器控制器"""
    
    def __init__(self):
        self.authorizer = None
        self.server = None
        self.server_thread = None
        self.is_running = False
        self.start_time = None
        
        # 默认配置
        self.config = {
            "address": "0.0.0.0",
            "port": 2121,
            "max_connections": 256,
            "passive_ports": list(range(60000, 60100)),
            "timeout": 300,
            "banner": "Welcome to Inscode FTP Server",
            "use_tls": False
        }
    
    def set_config(self, key, value):
        """设置配置项"""
        self.config[key] = value
    
    def start(self):
        """启动 FTP 服务器"""
        if self.is_running:
            return False, "服务器已在运行中"
        
        try:
            # 创建 MySQL 认证器
            self.authorizer = MySQLAuthorizer()
            
            # 配置 Handler
            handler = CustomFTPHandler
            handler.authorizer = self.authorizer
            handler.timeout = self.config['timeout']
            handler.banner = self.config['banner']
            handler.passive_ports = self.config['passive_ports']
            
            # 创建服务器
            address = (self.config['address'], self.config['port'])
            self.server = FTPServer(address, handler)
            self.server.max_cons = self.config['max_connections']
            self.server.max_cons_per_ip = 5
            
            # 启动服务器线程
            self.is_running = True
            self.start_time = datetime.now()
            self.server_thread = threading.Thread(target=self.server.serve_forever)
            self.server_thread.daemon = True
            self.server_thread.start()
            
            logger.info(f"FTP 服务器启动于 {self.config['address']}:{self.config['port']}")
            return True, f"服务器启动成功，监听 {self.config['address']}:{self.config['port']}"
            
        except Exception as e:
            logger.error(f"启动服务器失败：{e}")
            self.is_running = False
            return False, str(e)
    
    def stop(self):
        """停止 FTP 服务器"""
        if not self.is_running:
            return False, "服务器未运行"
        
        try:
            if self.server:
                self.server.close_all()
            self.is_running = False
            self.start_time = None
            logger.info("FTP 服务器已停止")
            return True, "服务器已停止"
        except Exception as e:
            logger.error(f"停止服务器失败：{e}")
            return False, str(e)
    
    def restart(self):
        """重启 FTP 服务器"""
        logger.info("重启 FTP 服务器...")
        self.stop()
        time.sleep(1)
        return self.start()
    
    def get_status(self):
        """获取服务器状态"""
        stats = CustomFTPHandler.get_stats() if CustomFTPHandler else {}
        return {
            'is_running': self.is_running,
            'config': self.config,
            'stats': stats,
            'uptime': str(datetime.now() - self.start_time) if self.start_time else '0:00:00'
        }


# 全局控制器实例（用于兼容旧代码）
controller = None


def create_controller():
    """创建控制器实例"""
    global controller
    controller = FTPServerController()
    return controller


def main():
    """主函数 - 独立运行模式（使用 SQLite）"""
    print("=" * 50)
    print("Inscode FTP Server")
    print("=" * 50)
    
    # 确保根目录存在
    os.makedirs('./ftp_root', exist_ok=True)
    
    # 创建控制器（无数据库模式）
    ctrl = create_controller()
    
    # 启动服务器
    success, message = ctrl.start()
    print(message)
    
    if success:
        print("\n服务器运行中... 按 Ctrl+C 停止")
        print(f"\n提示：完整功能请使用 main.py 启动并配置 MySQL 数据库")
        
        try:
            while ctrl.is_running:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n正在停止服务器...")
            ctrl.stop()


if __name__ == '__main__':
    main()