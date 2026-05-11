#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Web Server - 提供管理面板 HTTP 服务和 API 接口
支持管理员控制台和用户文件浏览器，所有页面强制登录认证
"""

import os
import json
import time
import shutil
import logging
import threading
import socket
from datetime import datetime, timedelta
from functools import wraps
from base64 import b64decode

from flask import Flask, jsonify, request, send_file, redirect, url_for, session, g, render_template, make_response
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__, template_folder='templates', static_folder='.')
# 从 config.py 读取 SECRET_KEY，保持全项目一致
try:
    from config import SECRET_KEY
    app.secret_key = SECRET_KEY
except (ImportError, AttributeError):
    import secrets
    app.secret_key = secrets.token_hex(32)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=31)
# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('web_server.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 全局控制器（由 main.py 注入）
controller = None
# 独立会话存储（支持 explorer 页面跨标签页持久登录）
# key: session_id (UUID), value: {'username': ..., 'role': ..., 'expires_at': timestamp}
import threading
import hmac
import hashlib
import time as _time

_sessions_lock = threading.Lock()
SESSION_LIFETIME_SECONDS = 8 * 3600  # 8小时有效期
AUTH_SCOPE_ADMIN = 'admin'
AUTH_SCOPE_EXPLORER = 'explorer'
AUTH_COOKIE_ADMIN = 'auth_session_admin'
AUTH_COOKIE_EXPLORER = 'auth_session_explorer'
AUTH_COOKIE_LEGACY = 'auth_session'

def _make_signed_token(username, role):
    payload = f"{username}|{role}|{int(_time.time())}"
    sig = hmac.new(app.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
    import base64
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()

def _verify_signed_token(token, max_age=SESSION_LIFETIME_SECONDS):
    try:
        import base64
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        parts = raw.rsplit("|", 1)
        if len(parts) != 2:
            return None, None
        payload, sig = parts
        expected_sig = hmac.new(app.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(sig, expected_sig):
            return None, None
        username, role, ts = payload.rsplit("|", 2)
        if _time.time() - int(ts) > max_age:
            return None, None
        return username, role
    except Exception:
        return None, None

def _verify_isolated_session(session_id):
    with _sessions_lock:
        if session_id in _isolated_sessions:
            sess = _isolated_sessions[session_id]
            if sess["expires_at"] > _time.time():
                return sess["username"], sess["role"]
            del _isolated_sessions[session_id]
    return _verify_signed_token(session_id)


def _normalize_auth_scope(scope_value):
    scope = (scope_value or '').strip().lower()
    if scope == AUTH_SCOPE_ADMIN:
        return AUTH_SCOPE_ADMIN
    return AUTH_SCOPE_EXPLORER


def _scope_cookie_name(scope):
    return AUTH_COOKIE_ADMIN if scope == AUTH_SCOPE_ADMIN else AUTH_COOKIE_EXPLORER


def _infer_scope_from_request_path():
    admin_prefixes = (
        '/admin',
        '/exam_admin',
        '/api/admin',
        '/api/server',
    )
    admin_exact_paths = {
        '/api/system/db',
        '/api/stream',
    }
    req_path = request.path or ''
    if req_path in admin_exact_paths:
        return AUTH_SCOPE_ADMIN
    for prefix in admin_prefixes:
        if req_path.startswith(prefix):
            return AUTH_SCOPE_ADMIN
    return AUTH_SCOPE_EXPLORER


def _resolve_auth_scope_from_request(default_scope=AUTH_SCOPE_EXPLORER):
    scope = (request.headers.get('X-Auth-Scope') or '').strip().lower()
    if not scope:
        scope = (request.args.get('auth_scope') or '').strip().lower()
    if not scope and request.is_json:
        body = request.get_json(silent=True) or {}
        scope = (body.get('scope') or '').strip().lower()
    if scope in (AUTH_SCOPE_ADMIN, AUTH_SCOPE_EXPLORER):
        return scope
    return default_scope


def _get_scoped_auth_session(default_scope=AUTH_SCOPE_EXPLORER):
    effective_default_scope = default_scope or _infer_scope_from_request_path()
    scope = _resolve_auth_scope_from_request(default_scope=effective_default_scope)
    auth_session = (request.args.get('auth_session') or '').strip()
    if auth_session:
        return auth_session

    scoped_cookie = request.cookies.get(_scope_cookie_name(scope)) or ''
    if scoped_cookie:
        return scoped_cookie
    return ''

_isolated_sessions = {}
_overview_cache = {

    'users': {'ts': 0, 'value': [], 'success': False},
    'logs': {'ts': 0, 'value': [], 'success': False},
    'disk': {'ts': 0, 'value': {'totalGB': 0, 'usedGB': 0, 'freeGB': 0, 'percent': 0}}
}

# ==================== 导入数据访问层 ====================

from db_helper import (
    authenticate_user,
    add_user,
    update_user,
    delete_user,
    list_users,
    get_user_by_username,
    update_user_quota,
    get_ftp_config,
    update_ftp_config,
    log_ftp_action,
    get_ftp_logs,
    hash_password,
    verify_password
)
from config import DB_TYPE, SQLALCHEMY_DATABASE_URI

# 导入考试系统蓝图
from web_server_exam import exam_bp

# 注册考试系统蓝图
app.register_blueprint(exam_bp)

def _verify_isolated_session(session_id):
    """验证独立会话，优先内存会话，失败时回退到签名 token"""
    if not session_id:
        return None, None
    with _sessions_lock:
        info = _isolated_sessions.get(session_id)
    if info:
        if info.get('expires_at', 0) >= datetime.utcnow().timestamp():
            return info.get('username'), info.get('role')
        with _sessions_lock:
            _isolated_sessions.pop(session_id, None)
    return _verify_signed_token(session_id)


def require_auth(f):
    """要求用户登录认证（API 使用，支持 auth_session URL 参数 / header / Cookie / Flask session / Basic Auth）"""
    @wraps(f)
    def decorated(*args, **kwargs):
        # 1. 优先从 X-Auth-Scope header 读取作用域，明确告诉后端本次请求属于哪个域
        header_scope = request.headers.get('X-Auth-Scope')
        resolved_scope = header_scope if header_scope in (AUTH_SCOPE_ADMIN, AUTH_SCOPE_EXPLORER) else None
        # 2. URL 参数 auth_session（优先级最高，支持分享链接场景）
        auth_session = (request.args.get('auth_session') or '').strip()
        if not auth_session:
            # fallback：按解析到的 scope 或默认 scope 读取对应 Cookie
            auth_session = _get_scoped_auth_session(default_scope=resolved_scope or AUTH_SCOPE_EXPLORER)
        if auth_session:
            username, role = _verify_isolated_session(auth_session)
            if username:
                g.current_user = username
                g.current_role = role or 'user'
                return f(*args, **kwargs)

        # 3. Flask cookie session（完全兼容历史版本，无 cookie 时兜底）
        if 'username' in session:
            g.current_user = session.get('username')
            g.current_role = session.get('role', 'user')
            return f(*args, **kwargs)

        # 4. Basic Auth（兼容旧客户端）
        auth = request.headers.get('Authorization')
        if not auth:
            return jsonify({'error': '需要认证', 'redirect': '/login'}), 401

        try:
            auth_parts = auth.split(' ')
            if len(auth_parts) != 2 or auth_parts[0] != 'Basic':
                return jsonify({'error': '认证格式错误'}), 401

            decoded = b64decode(auth_parts[1]).decode('utf-8')
            username, password = decoded.split(':', 1)

            # 验证用户并写入 session
            success, result = authenticate_user(username, password)
            if not success:
                return jsonify({'error': result}), 403

            session['username'] = username
            session['role'] = result.get('role', 'user')
            g.current_user = username
            g.current_role = result.get('role', 'user')
            return f(*args, **kwargs)

        except Exception as e:
            logger.error(f"认证失败：{e}")
            return jsonify({'error': '认证失败'}), 401

    return decorated


def require_admin(f):
    """要求管理员权限"""
    @wraps(f)
    @require_auth
    def decorated(*args, **kwargs):
        if g.current_role != 'admin':
            return jsonify({'error': '权限不足'}), 403
        return f(*args, **kwargs)
    return decorated


def login_required(f):
    """页面访问要求登录（支持 auth_session URL 参数 / header / Cookie / Flask session）"""
    @wraps(f)
    def decorated(*args, **kwargs):
        # 优先从 X-Auth-Scope header 读取作用域
        header_scope = request.headers.get('X-Auth-Scope')
        resolved_scope = header_scope if header_scope in (AUTH_SCOPE_ADMIN, AUTH_SCOPE_EXPLORER) else None
        auth_session = (request.args.get('auth_session') or '').strip()
        if not auth_session:
            auth_session = _get_scoped_auth_session(default_scope=resolved_scope or AUTH_SCOPE_EXPLORER)
        if auth_session:
            username, role = _verify_isolated_session(auth_session)
            if username:
                g.current_user = username
                g.current_role = role or 'user'
                return f(*args, **kwargs)

        # 回退：Flask cookie session
        if 'username' not in session:
            return redirect(url_for('login_page', next=request.path))

        # 额外安全检查：确保 session 数据完整
        if 'role' not in session:
            logger.warning(f"Session 不完整，用户 {session.get('username')} 缺少 role 字段")
            session.clear()
            return redirect(url_for('login_page', next=request.path))

        g.current_user = session.get('username')
        g.current_role = session.get('role')
        return f(*args, **kwargs)
    return decorated


def get_overview_cached_data():
    """缓存管理端概览中的高频昂贵查询，降低实时接口开销"""
    now = time.time()

    users_cache = _overview_cache['users']
    if now - users_cache['ts'] >= 1:  # 用户数据每秒刷新
        users_success, users_value = list_users()
        users_cache['ts'] = now
        users_cache['success'] = users_success
        users_cache['value'] = users_value if users_success else []
    users_success = users_cache['success']
    users = users_cache['value'] or []

    logs_cache = _overview_cache['logs']
    if now - logs_cache['ts'] >= 3:  # 日志每 3 秒刷新
        logs_success, logs_value = get_ftp_logs(limit=5)
        logs_cache['ts'] = now
        logs_cache['success'] = logs_success
        logs_cache['value'] = logs_value if logs_success else []
    logs = logs_cache['value'] or []

    disk_cache = _overview_cache['disk']
    if now - disk_cache['ts'] >= 5:  # 磁盘每 5 秒刷新
        disk_cache['ts'] = now
        disk_cache['value'] = get_disk_usage()
    disk = disk_cache['value']

    return users_success, users, logs, disk


@app.route('/api/system/db')
@require_admin
def get_db_status():
    """返回当前数据库类型和连接信息（用于前端显示与自检）"""
    db_uri = SQLALCHEMY_DATABASE_URI or ''
    safe_uri = db_uri
    if '@' in db_uri and '://' in db_uri:
        scheme, rest = db_uri.split('://', 1)
        if ':' in rest and '@' in rest and rest.index(':') < rest.index('@'):
            user = rest.split(':', 1)[0]
            after_at = rest.split('@', 1)[1]
            safe_uri = f"{scheme}://{user}:***@{after_at}"
    return jsonify({
        'db_type': DB_TYPE,
        'is_mysql': DB_TYPE == 'mysql',
        'database_uri': safe_uri
    })


def get_user_home(username):
    """获取用户主目录"""
    try:
        success, result = get_user_by_username(username)
        if success:
            home_dir = result.get('home_dir', '')
            
            # 处理各种路径格式，统一转换为绝对路径
            if not home_dir:
                # 空路径，使用默认值
                home_dir = os.path.abspath('./ftp_root/' + username)
            elif home_dir.startswith('/ftp_root'):
                # 数据库路径格式：/ftp_root/xxx -> 转换为 ./ftp_root/xxx 的绝对路径
                home_dir = os.path.abspath('.' + home_dir)
            elif home_dir.startswith('./') or home_dir.startswith('../'):
                # 相对路径
                home_dir = os.path.abspath(home_dir)
            elif not os.path.isabs(home_dir):
                # 其他相对路径
                home_dir = os.path.abspath(home_dir)
            # else: 已经是绝对路径，直接使用
            
            # 确保目录存在
            os.makedirs(home_dir, exist_ok=True)
            logger.info(f'get_user_home({username}): home_dir={home_dir}, original={result.get("home_dir", "")}')
            return home_dir
        return None
    except Exception as e:
        logger.error(f"获取用户主目录失败：{e}")
        return None


# ==================== 页面路由 ====================

@app.route('/login')
def login_page():
    """登录页面"""
    next_page = request.args.get('next', '')
    # 优先从 URL 参数读取 auth_session
    default_scope = AUTH_SCOPE_ADMIN if next_page == '/admin' else AUTH_SCOPE_EXPLORER
    auth_session = _get_scoped_auth_session(default_scope=default_scope)
    if auth_session:
        username, role = _verify_isolated_session(auth_session)
        if username:
            # 安全检查：非管理员携带 /admin 目标时不能直跳管理端
            if next_page == '/admin' and role != 'admin':
                return redirect(url_for('user_explorer'))
            if next_page:
                return redirect(next_page)
            return redirect(url_for('admin_panel') if role == 'admin' else url_for('user_explorer'))

    # 回退：检查 Flask session
    if 'username' in session:
        # 已登录，根据角色决定跳转（忽略 next 参数，防止非管理员访问/admin）
        user_role = session.get('role', 'user')

        # 安全检查：如果 next 参数是/admin 但用户不是管理员，跳转到用户页面
        if next_page == '/admin' and user_role != 'admin':
            logger.warning(f"非管理员用户 {session.get('username')} 尝试通过 next 参数访问 /admin")
            return redirect(url_for('user_explorer'))

        # 根据 next 参数或角色决定跳转
        if next_page:
            # 额外检查：如果 next 是/admin 但用户不是管理员，重定向到用户页面
            if next_page == '/admin' and user_role != 'admin':
                return redirect(url_for('user_explorer'))
            return redirect(next_page)
        if user_role == 'admin':
            return redirect(url_for('admin_panel'))
        else:
            return redirect(url_for('user_explorer'))

    # 未登录，渲染登录页并传递 next 参数
    return render_template('login.html', next=next_page)


@app.route('/')
def index():
    """统一门户首页"""
    return render_template('index.html')


@app.route('/admin')
def admin_panel():
    """管理员控制台 - 优先从 URL 参数读取 auth_session"""
    auth_session = _get_scoped_auth_session(default_scope=AUTH_SCOPE_ADMIN)
    if auth_session:
        username, role = _verify_isolated_session(auth_session)
        if username and role == 'admin':
            g.current_user = username
            g.current_role = role
            return render_template('admin_panel.html')
    # 统一跳转到 /login?next=/admin，避免前端无法感知 next 导致 scope 判定错误
    return redirect(url_for('login_page', next='/admin'))


@app.route('/explorer')
def user_explorer():
    """用户文件浏览器 - 统一入口，未登录时在页面内弹出登录框"""
    # 优先从 URL 参数读取 auth_session
    auth_session = _get_scoped_auth_session(default_scope=AUTH_SCOPE_EXPLORER)
    if auth_session:
        username, role = _verify_isolated_session(auth_session)
        if username:
            g.current_user = username
            g.current_role = role or 'user'
            return render_template('user_explorer.html')
    # 回退：Flask session
    g.current_user = session.get('username')
    g.current_role = session.get('role')
    return render_template('user_explorer.html')


@app.route('/explorer/login.html')
def explorer_login_page():
    """用户文件浏览器专用登录页"""
    next_page = request.args.get('next', '')
    # 优先从 URL 参数读取 auth_session
    auth_session = _get_scoped_auth_session(default_scope=AUTH_SCOPE_EXPLORER)
    if auth_session:
        username, role = _verify_isolated_session(auth_session)
        if username:
            return redirect(url_for('user_explorer'))
    if 'username' in session:
        return redirect(url_for('user_explorer'))
    return render_template('explorer_login.html', next=next_page)


@app.route('/exam_admin')
@login_required
def exam_admin_page():
    """考试管理页面"""
    if getattr(g, 'current_role', session.get('role')) != 'admin':
        return redirect(url_for('user_explorer'))
    return render_template('exam_admin.html')


# ==================== 静态文件路由 ====================

@app.route('/static/<path:filename>')
def serve_static(filename):
    """
    提供静态文件服务
    /static/js/a.js -> ./static/js/a.js
    """
    from flask import send_from_directory
    return send_from_directory('static', filename)


# ==================== 认证 API ====================

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    """用户登录 API"""
    data = request.json
    username = data.get('username', '')
    password = data.get('password', '')
    remember = bool(data.get('remember', False))
    next_page = data.get('next', '')
    isolated = bool(data.get('isolated', False))

    if not username or not password:
        return jsonify({'error': '用户名和密码不能为空'}), 400

    success, result = authenticate_user(username, password)

    if success:
        user_role = result.get('role', 'user')

        if isolated:
            # 独立会话：生成签名 token，通过 HttpOnly Cookie 传递
            token = _make_signed_token(username, user_role)
            redirect_target = next_page or ('/admin' if user_role == 'admin' else '/explorer')
            # 优先使用请求体中明确指定的 scope（仅管理员可用 admin cookie）
            explicit_scope = (data.get('scope') or '').strip().lower()
            if explicit_scope == AUTH_SCOPE_ADMIN and user_role == 'admin':
                scope = AUTH_SCOPE_ADMIN
            elif explicit_scope == AUTH_SCOPE_EXPLORER:
                scope = AUTH_SCOPE_EXPLORER
            elif redirect_target in ('/admin', '/exam_admin') and user_role == 'admin':
                # 管理员访问管理后台 → admin cookie
                scope = AUTH_SCOPE_ADMIN
            else:
                # 普通用户或访问 /explorer → explorer cookie
                scope = AUTH_SCOPE_EXPLORER
            cookie_name = _scope_cookie_name(scope)
            resp = redirect(redirect_target)
            resp.set_cookie(
                cookie_name,
                token,
                max_age=SESSION_LIFETIME_SECONDS if remember else None,
                httponly=True,
                samesite='Lax',
                path='/'
            )
            return resp

        # 普通会话（写 Flask session）
        session.permanent = bool(remember)
        session['username'] = username
        session['role'] = user_role
        session['login_time'] = datetime.now().isoformat()
        logger.info(f"用户登录：{username} (role={user_role}, remember={remember})")
        log_ftp_action(username, 'login', remote_ip=request.remote_addr)

        if next_page and next_page.startswith('/'):
            redirect_url = next_page
        elif user_role == 'admin':
            redirect_url = '/admin'
        else:
            redirect_url = '/explorer'

        return jsonify({'success': True, 'redirect': redirect_url, 'role': user_role})
    else:
        logger.warning(f"登录失败：{username} - {result}")
        return jsonify({'error': result}), 401


@app.route('/api/auth/logout', methods=['POST'])
def api_logout():
    """用户登出 API"""
    username = session.get('username')
    if username:
        log_ftp_action(username, 'logout', remote_ip=request.remote_addr)

    # 仅清理 Flask session（如果存在）
    if 'username' in session:
        session.clear()

    # 清理独立会话（按 scope 精确删除，避免互相影响）
    scope = _resolve_auth_scope_from_request(default_scope='')
    candidate_cookies = []
    if scope == AUTH_SCOPE_ADMIN:
        candidate_cookies = [AUTH_COOKIE_ADMIN, AUTH_COOKIE_LEGACY]
    elif scope == AUTH_SCOPE_EXPLORER:
        candidate_cookies = [AUTH_COOKIE_EXPLORER, AUTH_COOKIE_LEGACY]
    else:
        candidate_cookies = [AUTH_COOKIE_ADMIN, AUTH_COOKIE_EXPLORER, AUTH_COOKIE_LEGACY]

    for cookie_name in candidate_cookies:
        auth_session = request.cookies.get(cookie_name)
        if auth_session:
            with _sessions_lock:
                _isolated_sessions.pop(auth_session, None)

    resp = jsonify({'success': True, 'redirect': '/login'})
    for cookie_name in candidate_cookies:
        resp.delete_cookie(cookie_name, path='/')
    return resp


@app.route('/api/auth/me')
@require_auth
def api_get_current_user():
    """获取当前用户信息"""
    return jsonify({
        'username': g.current_user,
        'role': g.current_role
    })


@app.route('/api/admin/logs/stream')
@require_admin
def stream_admin_logs():
    """日志实时流（终端风格）"""
    def generate():
        import json
        while True:
            try:
                success, logs = get_ftp_logs(limit=200)
                payload = {'logs': logs if success else []}
                yield f"data:{json.dumps(payload, ensure_ascii=False)}\n\n"
            except Exception as e:
                yield f"data:{json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
            time.sleep(1)

    return app.response_class(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no'
        }
    )


# ==================== 仪表盘数据 API（普通 HTTP，替代 SSE）====================

@app.route('/api/stream')
@require_admin
def stream_dashboard():
    """
    仪表盘实时数据（普通 HTTP API，无需 EventSource）
    每秒轮询调用此接口，Cookie 自动携带，无重连风暴风险
    """
    try:
        if not controller:
            return jsonify({'error': '服务器未初始化'}), 500

        status_data = controller.get_status()

        try:
            success, users, logs, disk = get_overview_cached_data()
            total_users = len(users) if success else 0
            enabled_users = len([u for u in (users or []) if u.get('enabled', True) != False])

            online_users = []
            try:
                from ftp_server import CustomFTPHandler
                stats = CustomFTPHandler.get_stats() if CustomFTPHandler else {}
                for ip, info in stats.get('connections_detail', {}).items():
                    online_users.append({
                        'ip': ip,
                        'username': info.get('username', 'unknown'),
                        'connected_at': info.get('connected_at', ''),
                        'bytes_sent': info.get('bytes_sent', 0),
                        'bytes_received': info.get('bytes_received', 0)
                    })
            except Exception:
                pass

            recent_logins = []
            if logs:
                for log_item in logs:
                    recent_logins.append({
                        'username': log_item.get('username', ''),
                        'action': log_item.get('action', ''),
                        'created_at': log_item.get('created_at', ''),
                        'ip_address': log_item.get('ip_address', '')
                    })

            top_users_by_usage = []
            if success and users:
                sorted_users = sorted(
                    [u for u in users if u.get('quota_bytes', 0) > 0],
                    key=lambda x: x.get('used_bytes', 0) / max(x.get('quota_bytes', 1), 1),
                    reverse=True
                )[:5]
                for u in sorted_users:
                    top_users_by_usage.append({
                        'username': u.get('username', ''),
                        'used_bytes': u.get('used_bytes', 0),
                        'quota_bytes': u.get('quota_bytes', 0),
                        'usage_percent': round(u.get('used_bytes', 0) / max(u.get('quota_bytes', 1), 1) * 100, 1)
                    })

            return jsonify({
                'connections': status_data.get('connections', 0),
                'total_users': total_users,
                'enabled_users': enabled_users,
                'online_count': len(online_users),
                'online_users': online_users,
                'disk': disk,
                'recent_logins': recent_logins,
                'top_users_by_usage': top_users_by_usage,
                'uptime': str(datetime.now() - controller.start_time) if controller and controller.start_time else '0:00:00',
                'transferRateKBps': status_data.get('transferRateKBps', 0),
                'totalBytesTransferred': status_data.get('totalBytesTransferred', 0),
                'is_running': status_data.get('is_running', False),
                'timestamp': datetime.now().isoformat()
            })
        except Exception as e:
            logger.error(f'仪表盘数据聚合失败：{e}')
            return jsonify({'error': str(e)}), 500
    except Exception as e:
        logger.error(f'仪表盘 API 异常：{e}')
        return jsonify({'error': str(e)}), 500


# ==================== 服务器状态 API ====================

@app.route('/api/status')
@require_admin
def get_status():
    """获取服务器状态（仅管理员）"""
    if not controller:
        return jsonify({'error': '服务器未初始化'}), 500
    
    return jsonify(controller.get_status())


@app.route('/api/admin/overview')
@require_admin
def get_admin_overview():
    """
    管理控制台仪表盘概览数据（实时聚合接口）
    返回：用户统计、磁盘用量、在线人数、最近登录等
    """
    try:
        # 获取用户统计（复用缓存，避免与 SSE 并发时重复打 DB）
        success, users, logs, disk = get_overview_cached_data()
        total_users = len(users) if success else 0
        enabled_users = len([u for u in (users or []) if u.get('enabled', True) != False])
        
        # 获取在线用户（从健康检查接口复用逻辑）
        online_users = []
        ftp_port = controller.config.get('port', 2121) if controller else 2121
        
        try:
            from ftp_server import CustomFTPHandler
            stats = CustomFTPHandler.get_stats() if CustomFTPHandler else {}
            for ip, info in stats.get('connections_detail', {}).items():
                online_users.append({
                    'ip': ip,
                    'username': info.get('username', 'unknown'),
                    'connected_at': info.get('connected_at', ''),
                    'bytes_sent': info.get('bytes_sent', 0),
                    'bytes_received': info.get('bytes_received', 0)
                })
        except Exception:
            pass
        
        # 获取最近登录记录（前 5 条）
        recent_logins = []
        try:
            if logs:
                recent_logins = [
                    {
                        'username': log.get('username', ''),
                        'action': log.get('action', ''),
                        'created_at': log.get('created_at', ''),
                        'ip_address': log.get('ip_address', '')
                    }
                    for log in logs
                ]
        except Exception:
            pass
        
        # 计算使用率 TOP 5 用户
        top_users_by_usage = []
        if success and users:
            sorted_users = sorted(
                [u for u in users if u.get('quota_bytes', 0) > 0],
                key=lambda x: x.get('used_bytes', 0) / max(x.get('quota_bytes', 1), 1),
                reverse=True
            )[:5]
            top_users_by_usage = [
                {
                    'username': u.get('username', ''),
                    'used_bytes': u.get('used_bytes', 0),
                    'quota_bytes': u.get('quota_bytes', 0),
                    'usage_percent': round(u.get('used_bytes', 0) / max(u.get('quota_bytes', 1), 1) * 100, 1)
                }
                for u in sorted_users
            ]
        
        return jsonify({
            'total_users': total_users,
            'enabled_users': enabled_users,
            'online_count': len(online_users),
            'online_users': online_users,
            'disk': disk,
            'recent_logins': recent_logins,
            'top_users_by_usage': top_users_by_usage,
            'timestamp': datetime.now().isoformat()
        })
    
    except Exception as e:
        logger.error(f'获取概览数据失败：{e}')
        return jsonify({'error': f'获取数据失败：{str(e)}'}), 500


@app.route('/api/health')
def health_check():
    """健康检查 - 实时检测 FTP 服务器运行状况"""
    is_ftp_alive = False
    ftp_port = 2121
    
    if controller and controller.config:
        ftp_port = controller.config.get('port', 2121)
    
    # 检查端口是否存活
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex(('127.0.0.1', ftp_port))
        sock.close()
        is_ftp_alive = (result == 0)
    except Exception:
        pass
    
    # 获取统计信息
    stats = {}
    if controller and hasattr(controller, 'authorizer'):
        from ftp_server import CustomFTPHandler
        stats = CustomFTPHandler.get_stats() if CustomFTPHandler else {}
    
    disk = get_disk_usage()
    
    # 计算传输速率
    current_bytes = stats.get('total_bytes_transferred', 0)
    current_time = time.time()
    
    transfer_rate = 0
    if hasattr(health_check, 'last_bytes'):
        bytes_diff = current_bytes - health_check.last_bytes
        time_diff = current_time - health_check.last_time
        if time_diff > 0:
            transfer_rate = (bytes_diff / 1024) / time_diff
    health_check.last_bytes = current_bytes
    health_check.last_time = current_time
    
    online_users = []
    for ip, info in stats.get('connections_detail', {}).items():
        online_users.append({
            'ip': ip,
            'username': info.get('username', 'unknown'),
            'connected_at': info.get('connected_at', ''),
            'bytes_sent': info.get('bytes_sent', 0),
            'bytes_received': info.get('bytes_received', 0)
        })
    
    is_running = controller.is_running if controller else False
    
    return jsonify({
        'status': 'up' if (is_ftp_alive and is_running) else 'down',
        'ftp_port': ftp_port,
        'is_running': is_running,
        'uptime': str(datetime.now() - controller.start_time) if controller and controller.start_time else '0:00:00',
        'connections': len(online_users),
        'online_users': online_users,
        'diskFreeGB': disk['freeGB'],
        'diskPercent': disk['percent'],
        'transferRateKBps': round(transfer_rate, 2),
        'totalBytesTransferred': current_bytes,
        'timestamp': datetime.now().isoformat()
    })


def get_disk_usage():
    """获取磁盘使用情况"""
    try:
        total, used, free = shutil.disk_usage('.')
        return {
            'totalGB': round(total / (1024**3), 2),
            'usedGB': round(used / (1024**3), 2),
            'freeGB': round(free / (1024**3), 2),
            'percent': round(used / total * 100, 1)
        }
    except Exception:
        return {'totalGB': 0, 'usedGB': 0, 'freeGB': 0, 'percent': 0}


# ==================== 文件管理 API ====================

@app.route('/api/files', methods=['GET'])
@require_auth
def list_files():
    """列出文件（用户文件浏览器 API）"""
    path = request.args.get('path', '/')
    username = g.current_user
    base_dir = get_user_home(username)
    
    if not base_dir:
        return jsonify({'error': '用户主目录不存在'}), 500
    
    # 规范化 base_dir 路径（去除末尾斜杠，统一使用绝对路径）
    base_dir = os.path.abspath(os.path.normpath(base_dir))
    
    # 处理路径：去掉开头的/，然后与 base_dir 拼接
    clean_path = path.lstrip('/')
    
    # 计算完整路径
    if clean_path == '' or clean_path == '/':
        full_path = base_dir
    else:
        full_path = os.path.abspath(os.path.normpath(os.path.join(base_dir, clean_path)))
    
    # 安全检查：确保在 base_dir 内（使用 abspath 后比较）
    if not (full_path == base_dir or full_path.startswith(base_dir + os.sep)):
        logger.warning(f"非法路径访问：{username} 尝试访问 {full_path} (base: {base_dir})")
        return jsonify({'error': '非法路径'}), 403
    
    if not os.path.exists(full_path):
        return jsonify({'error': '路径不存在'}), 404
    
    if not os.path.isdir(full_path):
        return jsonify({'error': '不是目录'}), 400
    
    files = []
    try:
        logger.info(f'列出文件：{username} -> {full_path}, base_dir={base_dir}')
        for item in os.listdir(full_path):
            item_path = os.path.join(full_path, item)
            stat = os.stat(item_path)
            # 构建相对于 base_dir 的路径
            rel_path = os.path.relpath(item_path, base_dir)
            files.append({
                'name': item,
                'path': '/' + rel_path.replace('\\', '/'),
                'is_directory': os.path.isdir(item_path),
                'size': stat.st_size if os.path.isfile(item_path) else 0,
                'modified': datetime.fromtimestamp(stat.st_mtime).isoformat()
            })
    except PermissionError as e:
        logger.error(f"权限拒绝：{full_path} - {e}")
        return jsonify({'error': '权限拒绝，请检查目录权限'}), 403
    except Exception as e:
        logger.error(f"列出文件失败：{e}")
        return jsonify({'error': str(e)}), 500
    
    # current_path 始终返回相对于根目录的路径
    current_path = '/' + clean_path if clean_path else '/'
    
    return jsonify({
        'current_path': current_path,
        'base_dir': base_dir,
        'files': sorted(files, key=lambda x: (not x['is_directory'], x['name']))
    })


@app.route('/api/download', methods=['GET'])
@require_auth
def download_file():
    """下载文件"""
    file_path = request.args.get('path', '')
    username = g.current_user
    base_dir = get_user_home(username)
    
    if not base_dir:
        return jsonify({'error': '用户主目录不存在'}), 500
    
    if not file_path:
        return jsonify({'error': '未指定文件路径'}), 400
    
    # 规范化 base_dir 路径（使用绝对路径）
    base_dir = os.path.abspath(os.path.normpath(base_dir))
    
    # 去掉路径开头的/
    clean_path = file_path.lstrip('/')
    
    # 计算完整路径
    full_path = os.path.abspath(os.path.normpath(os.path.join(base_dir, clean_path)))
    
    # 安全检查
    if not (full_path == base_dir or full_path.startswith(base_dir + os.sep)):
        logger.warning(f"非法下载路径：{username} 尝试下载 {full_path}")
        return jsonify({'error': '非法路径'}), 403
    
    if not os.path.exists(full_path):
        return jsonify({'error': '文件不存在'}), 404
    
    if not os.path.isfile(full_path):
        return jsonify({'error': '不是文件'}), 400
    
    try:
        logger.info(f"用户下载文件：{username} -> {full_path}")
        return send_file(full_path, as_attachment=True)
    except PermissionError as e:
        logger.error(f"下载权限拒绝：{full_path} - {e}")
        return jsonify({'error': '权限拒绝'}), 403
    except Exception as e:
        logger.error(f"发送文件失败：{e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/upload', methods=['POST'])
@require_auth
def upload_file():
    """上传文件 - 支持文件夹上传（带 relativePath 参数）"""
    if 'file' not in request.files:
        return jsonify({'error': '未找到文件'}), 400
    
    file = request.files['file']
    upload_path = request.args.get('path', '/')
    relative_path = request.form.get('relativePath', '')  # 文件夹上传时的相对路径
    username = g.current_user
    base_dir = get_user_home(username)
    
    if not base_dir:
        return jsonify({'error': '用户主目录不存在'}), 500
    
    if file.filename == '':
        return jsonify({'error': '文件名为空'}), 400
    
    # 规范化 base_dir 路径（使用绝对路径）
    base_dir = os.path.abspath(os.path.normpath(base_dir))
    
    # 处理上传路径
    clean_path = upload_path.lstrip('/')
    
    # 计算目标目录
    if clean_path == '' or clean_path == '/':
        target_dir = base_dir
    else:
        target_dir = os.path.abspath(os.path.normpath(os.path.join(base_dir, clean_path)))
    
    # 安全检查
    if not (target_dir == base_dir or target_dir.startswith(base_dir + os.sep)):
        logger.warning(f"非法上传路径：{username} 尝试上传到 {target_dir}")
        return jsonify({'error': '非法路径'}), 403
    
    if not os.path.exists(target_dir):
        return jsonify({'error': '路径不存在'}), 404
    
    if not os.path.isdir(target_dir):
        return jsonify({'error': '不是目录'}), 400
    
    try:
        # 如果有 relativePath，说明是文件夹上传，需要创建子目录结构
        if relative_path:
            # 从 relativePath 中提取目录部分（去掉文件名）
            import posixpath
            rel_dir = posixpath.dirname(relative_path)
            if rel_dir:
                # 创建嵌套目录
                nested_dir = os.path.abspath(os.path.normpath(os.path.join(target_dir, rel_dir)))
                # 确保创建的目录仍在 base_dir 内
                if not nested_dir.startswith(base_dir + os.sep):
                    logger.warning(f"非法嵌套目录：{nested_dir}")
                    return jsonify({'error': '非法路径'}), 403
                os.makedirs(nested_dir, exist_ok=True)
                file_path = os.path.join(nested_dir, file.filename)
            else:
                file_path = os.path.join(target_dir, file.filename)
        else:
            file_path = os.path.join(target_dir, file.filename)
        
        file.save(file_path)
        logger.info(f"用户上传文件：{username} -> {file_path}")
        
        # 记录日志
        log_ftp_action(username, 'upload', filename=file.filename, 
                      remote_ip=request.remote_addr, bytes_transferred=os.path.getsize(file_path))
        
        return jsonify({'success': True, 'message': f'文件 {file.filename} 上传成功'})
    except PermissionError as e:
        logger.error(f"上传权限拒绝：{target_dir} - {e}")
        return jsonify({'error': '权限拒绝，请检查目录写入权限'}), 403
    except Exception as e:
        logger.error(f"上传文件失败：{e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/delete', methods=['POST'])
@require_auth
def delete_file():
    """删除文件/文件夹"""
    data = request.get_json()
    file_path = data.get('path', '')
    username = g.current_user
    base_dir = get_user_home(username)
    
    if not base_dir:
        return jsonify({'error': '用户主目录不存在'}), 500
    
    if not file_path:
        return jsonify({'error': '未指定路径'}), 400
    
    # 规范化 base_dir 路径（使用绝对路径）
    base_dir = os.path.abspath(os.path.normpath(base_dir))
    
    # 处理路径
    clean_path = file_path.lstrip('/')
    
    full_path = os.path.abspath(os.path.normpath(os.path.join(base_dir, clean_path)))
    
    # 安全检查
    if not (full_path == base_dir or full_path.startswith(base_dir + os.sep)):
        logger.warning(f"非法删除路径：{username} 尝试删除 {full_path}")
        return jsonify({'error': '非法路径'}), 403
    
    if not os.path.exists(full_path):
        return jsonify({'error': '路径不存在'}), 404
    
    try:
        if os.path.isdir(full_path):
            shutil.rmtree(full_path)
        else:
            os.remove(full_path)
        logger.info(f"用户删除：{username} -> {full_path}")
        
        # 记录日志
        log_ftp_action(username, 'delete', filename=file_path, remote_ip=request.remote_addr)
        
        return jsonify({'success': True, 'message': '删除成功'})
    except PermissionError as e:
        logger.error(f"删除权限拒绝：{full_path} - {e}")
        return jsonify({'error': '权限拒绝'}), 403
    except Exception as e:
        logger.error(f"删除失败：{e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/mkdir', methods=['POST'])
@require_auth
def create_directory():
    """创建目录"""
    data = request.get_json()
    dir_name = data.get('name', '')
    parent_path = data.get('path', '/')
    username = g.current_user
    base_dir = get_user_home(username)
    
    if not base_dir:
        return jsonify({'error': '用户主目录不存在'}), 500
    
    if not dir_name:
        return jsonify({'error': '未指定目录名'}), 400
    
    # 规范化 base_dir 路径（使用绝对路径）
    base_dir = os.path.abspath(os.path.normpath(base_dir))
    
    # 处理父路径
    clean_path = parent_path.lstrip('/')
    
    # 计算完整路径
    if clean_path == '' or clean_path == '/':
        target_path = os.path.abspath(os.path.join(base_dir, dir_name))
    else:
        target_path = os.path.abspath(os.path.normpath(os.path.join(base_dir, clean_path, dir_name)))
    
    # 安全检查
    if not target_path.startswith(base_dir + os.sep):
        logger.warning(f"非法创建目录路径：{username} 尝试创建 {target_path}")
        return jsonify({'error': '非法路径'}), 403
    
    try:
        os.makedirs(target_path, exist_ok=True)
        logger.info(f"用户创建目录：{username} -> {target_path}")
        return jsonify({'success': True, 'message': f'目录 {dir_name} 创建成功'})
    except PermissionError as e:
        logger.error(f"创建目录权限拒绝：{target_path} - {e}")
        return jsonify({'error': '权限拒绝'}), 403
    except Exception as e:
        logger.error(f"创建目录失败：{e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/rename', methods=['POST'])
@require_auth
def rename_file():
    """重命名文件/文件夹"""
    data = request.get_json()
    old_path = data.get('old_path', '')
    new_name = data.get('new_name', '')
    username = g.current_user
    base_dir = get_user_home(username)
    
    if not base_dir:
        return jsonify({'error': '用户主目录不存在'}), 500
    
    if not old_path or not new_name:
        return jsonify({'error': '未指定路径或新名称'}), 400
    
    # 规范化 base_dir 路径（使用绝对路径）
    base_dir = os.path.abspath(os.path.normpath(base_dir))
    
    # 处理旧路径
    clean_path = old_path.lstrip('/')
    
    old_full = os.path.abspath(os.path.normpath(os.path.join(base_dir, clean_path)))
    parent_dir = os.path.dirname(old_full)
    new_full = os.path.abspath(os.path.join(parent_dir, new_name))
    
    # 安全检查
    if not old_full.startswith(base_dir + os.sep) or not new_full.startswith(base_dir + os.sep):
        logger.warning(f"非法重命名路径：{username}")
        return jsonify({'error': '非法路径'}), 403
    
    if not os.path.exists(old_full):
        return jsonify({'error': '原路径不存在'}), 404
    
    try:
        os.rename(old_full, new_full)
        logger.info(f"用户重命名：{username} -> {old_full} -> {new_full}")
        return jsonify({'success': True, 'message': '重命名成功'})
    except PermissionError as e:
        logger.error(f"重命名权限拒绝：{old_full} - {e}")
        return jsonify({'error': '权限拒绝'}), 403
    except Exception as e:
        logger.error(f"重命名失败：{e}")
        return jsonify({'error': str(e)}), 500


# ==================== 管理员 API - 用户管理 ====================

@app.route('/api/admin/users', methods=['GET'])
@require_admin
def admin_list_users():
    """管理员 - 获取用户列表"""
    success, result = list_users()
    if success:
        return jsonify({'users': result})
    else:
        return jsonify({'error': result}), 500


@app.route('/api/admin/users', methods=['POST'])
@require_admin
def admin_add_user():
    """管理员 - 添加新用户"""
    data = request.json
    username = data.get('username')
    password = data.get('password')
    role = data.get('role', 'user')
    quota_bytes = data.get('quota_bytes', 0)
    home_dir = data.get('home_dir', '')
    
    if not username or not password:
        return jsonify({'error': '用户名和密码不能为空'}), 400
    
    success, result = add_user(
        username=username,
        password=password,
        role=role,
        quota_bytes=int(quota_bytes),
        home_dir=home_dir
    )
    
    if success:
        # 创建用户主目录
        user_home = result.get('home_dir', f'/ftp_root/{username}')
        os.makedirs(user_home, exist_ok=True)
        
        logger.info(f"管理员添加用户：{username}")
        return jsonify({'message': '用户添加成功', 'user': result})
    else:
        return jsonify({'error': result}), 400


@app.route('/api/admin/users/<int:user_id>', methods=['DELETE'])
@require_admin
def admin_delete_user(user_id):
    """管理员 - 删除用户"""
    success, message = delete_user(user_id)
    
    if success:
        logger.info(f"管理员删除用户 ID: {user_id}")
        return jsonify({'message': message})
    else:
        return jsonify({'error': message}), 400


@app.route('/api/admin/users/<int:user_id>', methods=['PUT'])
@require_admin
def admin_update_user(user_id):
    """管理员 - 更新用户信息"""
    data = request.json
    
    update_data = {}
    
    if 'password' in data and data['password']:
        update_data['password'] = data['password']
    if 'role' in data:
        update_data['role'] = data['role']
    if 'quota_bytes' in data:
        update_data['quota_bytes'] = int(data['quota_bytes'])
    if 'enabled' in data:
        update_data['enabled'] = bool(data['enabled'])
    if 'home_dir' in data:
        update_data['home_dir'] = data['home_dir']
    
    success, message = update_user(user_id, **update_data)
    
    if success:
        logger.info(f"管理员更新用户 ID: {user_id}")
        return jsonify({'message': message})
    else:
        return jsonify({'error': message}), 400


# ==================== 管理员 API - FTP 配置 ====================

@app.route('/api/admin/config', methods=['GET'])
@require_admin
def admin_get_config():
    """管理员 - 获取 FTP 配置"""
    success, result = get_ftp_config()
    if success:
        return jsonify({'config': result})
    else:
        return jsonify({'error': result}), 500


@app.route('/api/server/config', methods=['GET'])
@require_admin
def server_get_runtime_config():
    """管理端运行时服务器配置（供 /admin 页面使用）"""
    cfg = controller.config if controller and getattr(controller, 'config', None) else {}
    return jsonify({
        'address': cfg.get('host', '0.0.0.0'),
        'port': cfg.get('port', 2121),
        'max_connections': cfg.get('max_connections', 256),
        'timeout': cfg.get('timeout', 300),
        'banner': cfg.get('banner', 'Welcome to Inscode FTP Server')
    })


@app.route('/api/admin/config', methods=['POST'])
@require_admin
def admin_update_config():
    """管理员 - 更新 FTP 配置"""
    data = request.json
    
    update_data = {}
    if 'max_download_speed' in data:
        update_data['max_download_speed'] = int(data['max_download_speed'])
    if 'max_upload_speed' in data:
        update_data['max_upload_speed'] = int(data['max_upload_speed'])
    if 'max_connections' in data:
        update_data['max_connections'] = int(data['max_connections'])
    if 'passive_ports_start' in data:
        update_data['passive_ports_start'] = int(data['passive_ports_start'])
    if 'passive_ports_end' in data:
        update_data['passive_ports_end'] = int(data['passive_ports_end'])
    if 'masquerade_address' in data:
        update_data['masquerade_address'] = data['masquerade_address']
    
    success, message = update_ftp_config(**update_data)
    
    if success:
        logger.info("管理员更新 FTP 配置")
        return jsonify({'message': message})
    else:
        return jsonify({'error': message}), 400


@app.route('/api/server/config', methods=['POST'])
@require_admin
def server_update_runtime_config():
    """更新管理端运行时服务器配置"""
    data = request.json or {}
    if controller and getattr(controller, 'config', None) is not None:
        controller.config['host'] = data.get('address', controller.config.get('host', '0.0.0.0'))
        controller.config['port'] = int(data.get('port', controller.config.get('port', 2121)))
        controller.config['max_connections'] = int(data.get('max_connections', controller.config.get('max_connections', 256)))
        controller.config['timeout'] = int(data.get('timeout', controller.config.get('timeout', 300)))
        controller.config['banner'] = data.get('banner', controller.config.get('banner', 'Welcome to Inscode FTP Server'))

    update_ftp_config(
        max_connections=int(data.get('max_connections', 256)),
        masquerade_address=data.get('address', '')
    )

    return jsonify({'message': '配置已更新'})


# ==================== 管理员 API - 日志查询 ====================

@app.route('/api/admin/logs', methods=['GET'])
@require_admin
def admin_get_logs():
    """管理员 - 获取 FTP 日志"""
    limit = request.args.get('limit', 100, type=int)
    success, result = get_ftp_logs(limit=limit)
    if success:
        return jsonify({'logs': result})
    else:
        return jsonify({'error': result}), 500


# ==================== 服务器控制 API ====================

@app.route('/api/server/start', methods=['POST'])
@require_admin
def start_server():
    """启动 FTP 服务器"""
    if not controller:
        return jsonify({'error': '控制器未初始化'}), 500
    
    success, message = controller.start()
    if success:
        logger.info("管理员启动 FTP 服务器")
        return jsonify({'message': message, 'status': 'running'})
    else:
        return jsonify({'error': message}), 400


@app.route('/api/server/stop', methods=['POST'])
@require_admin
def stop_server():
    """停止 FTP 服务器"""
    if not controller:
        return jsonify({'error': '控制器未初始化'}), 500
    
    success, message = controller.stop()
    if success:
        logger.info("管理员停止 FTP 服务器")
        return jsonify({'message': message, 'status': 'stopped'})
    else:
        return jsonify({'error': message}), 400


@app.route('/api/server/restart', methods=['POST'])
@require_admin
def restart_server():
    """重启 FTP 服务器"""
    if not controller:
        return jsonify({'error': '控制器未初始化'}), 500
    
    success, message = controller.restart()
    if success:
        logger.info("管理员重启 FTP 服务器")
        return jsonify({'message': message, 'status': 'running'})
    else:
        return jsonify({'error': message}), 400


if __name__ == '__main__':
    print("=" * 50)
    print("FTP Web Management Server")
    print("=" * 50)
    print("\n启动 Web 服务器...")
    print("⚠️  注意：请使用 main.py 启动完整系统")
    print("\n按 Ctrl+C 停止服务")
    
    os.makedirs('./ftp_root', exist_ok=True)
    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)