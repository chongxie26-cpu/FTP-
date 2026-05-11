#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
考试系统 API 扩展
提供批量生成考生账号、分发试卷、一键清除等功能
"""

import os
import csv
import io
import random
import string
import shutil
import zipfile
import ftplib
import hmac
import hashlib
import base64
import time
from datetime import datetime
from functools import wraps
from sqlalchemy import func

from flask import Blueprint, jsonify, request, session, g
from werkzeug.utils import secure_filename

from db_helper import (
    add_user,
    delete_user,
    get_user_by_username,
    list_users,
    log_ftp_action
)
from models import User, ExamPaper, ExamStudent, Exam
from db_helper import get_session, hash_password
from config import FTP_HOST, FTP_PORT, FTP_USER, FTP_PASSWORD
from config import SECRET_KEY

# 配置日志
import logging
logger = logging.getLogger(__name__)

exam_bp = Blueprint('exam', __name__, url_prefix='/api/exam')
SESSION_LIFETIME_SECONDS = 8 * 3600

# 认证作用域常量（与 web_server.py 保持一致）
AUTH_SCOPE_ADMIN = 'admin'
AUTH_SCOPE_EXPLORER = 'explorer'
AUTH_COOKIE_ADMIN = 'auth_session_admin'
AUTH_COOKIE_EXPLORER = 'auth_session_explorer'
AUTH_COOKIE_LEGACY = 'auth_session'


def _exam_get_scoped_auth_session(default_scope=AUTH_SCOPE_ADMIN):
    """
    获取当前请求对应的 auth_session 值。
    与 web_server.py 的 _get_scoped_auth_session 逻辑一致：
    1. 优先读取 X-Auth-Scope header 确定作用域
    2. 按作用域读取对应的 Cookie
    """
    header_scope = request.headers.get('X-Auth-Scope')
    resolved_scope = header_scope if header_scope in (AUTH_SCOPE_ADMIN, AUTH_SCOPE_EXPLORER) else None

    auth_session = (request.args.get('auth_session') or '').strip()
    if auth_session:
        return auth_session

    scope = resolved_scope or default_scope
    if scope == AUTH_SCOPE_ADMIN:
        cookie_name = AUTH_COOKIE_ADMIN
    else:
        cookie_name = AUTH_COOKIE_EXPLORER

    cookie_val = request.cookies.get(cookie_name)
    if cookie_val:
        return cookie_val
    # 回退：尝试 legacy cookie
    legacy = request.cookies.get(AUTH_COOKIE_LEGACY)
    if legacy:
        return legacy
    return ''


def _exam_verify_auth_session_token(token, max_age=SESSION_LIFETIME_SECONDS):
    """校验签名 token，与 web_server.py 的 _verify_signed_token 逻辑一致"""
    if not token:
        return None, None
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        parts = raw.rsplit("|", 1)
        if len(parts) != 2:
            return None, None
        payload, sig = parts
        expected_sig = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(sig, expected_sig):
            return None, None
        username, role, ts = payload.rsplit("|", 2)
        if time.time() - int(ts) > max_age:
            return None, None
        return username, role
    except Exception:
        return None, None


def unix_to_physical(unix_path):
    """
    将 Unix 风格路径 /ftp_root/... 转换为当前 OS 的物理绝对路径。
    关键：Windows 上 os.path.abspath('/ftp_root/...') -> C:\\ftp_root\\...
    （盘符根目录），这是错的。必须加 './' 前缀转为项目相对路径。
    """
    if not unix_path:
        return os.path.abspath('./ftp_root')
    # 去掉开头的 /ftp_root/ -> 'students/...'
    stripped = unix_path.lstrip('/')
    if stripped.startswith('ftp_root' + os.sep) or stripped == 'ftp_root':
        suffix = stripped[len('ftp_root'):].lstrip(os.sep)
        return os.path.abspath('./ftp_root/' + suffix) if suffix else os.path.abspath('./ftp_root')
    elif stripped.startswith('ftp_root'):
        suffix = stripped[len('ftp_root'):].lstrip(os.sep)
        return os.path.abspath('./ftp_root/' + suffix) if suffix else os.path.abspath('./ftp_root')
    else:
        return os.path.abspath('./' + stripped)


def _ftp_upload_file(local_path, remote_filename, exam_username, exam_password):
    """
    通过 FTP 协议上传文件到考生 FTP 目录
    考生登录后已在其 home_dir 中，所以远程路径只需要文件名
    
    local_path: 本地试卷文件路径
    remote_filename: FTP 远程文件名（不含路径，因为登录后已在 home_dir）
    exam_username: 考生 FTP 用户名
    exam_password: 考生 FTP 密码
    返回: (success, error_message)
    """
    ftp = None
    try:
        ftp = ftplib.FTP()
        ftp.connect(FTP_HOST, FTP_PORT, timeout=30)
        ftp.login(exam_username, exam_password)
        
        # 上传文件
        with open(local_path, 'rb') as f:
            ftp.storbinary(f'STOR {remote_filename}', f)
        
        logger.info(f'FTP 上传成功：{local_path} -> {exam_username}/{remote_filename}')
        return True, None
        
    except ftplib.error_perm as e:
        logger.error(f'FTP 权限错误：{exam_username} - {e}')
        return False, f'FTP权限错误: {e}'
    except ftplib.error_temp as e:
        logger.error(f'FTP 临时错误：{exam_username} - {e}')
        return False, f'FTP临时错误: {e}'
    except Exception as e:
        # 检查是否是传输完成后的正常断开
        error_str = str(e)
        if 'EOF' in error_str or error_str == '':
            # 传输成功但连接异常关闭，视为成功
            logger.info(f'FTP 上传成功（连接正常关闭）：{exam_username}/{remote_filename}')
            return True, None
        logger.error(f'FTP 上传失败：{exam_username} - {type(e).__name__}: {e}')
        return False, f'{type(e).__name__}: {e}'
    finally:
        # 安全关闭连接
        if ftp:
            try:
                ftp.quit()
            except:
                try:
                    ftp.close()
                except:
                    pass


def _direct_copy_file(src_path, dst_path):
    """
    直接通过文件系统复制文件（推荐方式，绕过 FTP）
    比 FTP 更稳定，不依赖 FTP 服务状态

    src_path: 源文件路径（绝对路径）
    dst_path: 目标文件路径（完整路径，包含文件名）
    返回: (success, error_message)
    """
    try:
        if not os.path.exists(src_path):
            return False, f'源文件不存在：{src_path}'

        # 确保目标目录存在（支持 dst_path 可能是目录路径的情况）
        if os.path.isdir(dst_path):
            # dst_path 本身是目录，直接在其下创建与源文件同名文件
            dst_dir = dst_path
            dst_file = os.path.join(dst_dir, os.path.basename(src_path))
        else:
            # dst_path 是完整文件路径，取其目录
            dst_dir = os.path.dirname(dst_path)
            dst_file = dst_path

        if dst_dir:
            os.makedirs(dst_dir, exist_ok=True)

        # 直接复制文件
        shutil.copy2(src_path, dst_file)

        # 验证文件已成功复制
        if os.path.exists(dst_file):
            logger.info(f'文件复制成功：{src_path} -> {dst_file}')
            return True, None
        else:
            return False, f'复制后文件不存在：{dst_file}'

    except PermissionError as e:
        return False, f'权限不足：{e}'
    except OSError as e:
        return False, f'文件系统错误：{e}'
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


def _ftp_mkdir_recursive(ftp, path):
    """递归创建 FTP 远程目录"""
    dirs = path.strip('/').split('/')
    for d in dirs:
        try:
            ftp.cwd(d)
        except ftplib.error_perm:
            ftp.mkd(d)
            ftp.cwd(d)


def _sync_exam_papers_from_fs(db_session, exam_id):
    """
    从文件系统同步试卷池文件到 ExamPaper 表（幂等）
    返回新增记录数量
    """
    # 使用跨平台路径
    paper_pool_dir = os.path.join('ftp_root', 'exam_paper_pool', exam_id)
    if not os.path.isdir(paper_pool_dir):
        return 0

    existing = db_session.query(ExamPaper).filter_by(exam_id=exam_id).all()
    existing_keys = {(p.filename, p.path) for p in existing}
    added = 0

    for root, _, filenames in os.walk(paper_pool_dir):
        for fname in filenames:
            if fname.startswith('.'):
                continue
            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, paper_pool_dir).replace('\\', '/')
            key = (fname, rel_path)
            if key in existing_keys:
                continue
            db_session.add(ExamPaper(
                exam_id=exam_id,
                filename=fname,
                path=rel_path,
                created_at=datetime.utcnow()
            ))
            existing_keys.add(key)
            added += 1
    return added


def _collect_exam_ids_from_fs():
    """从历史目录扫描考试 ID（学生目录 + 试卷池目录）"""
    exam_ids = set()

    # 使用跨平台路径
    students_root = os.path.join('ftp_root', 'students')
    if os.path.isdir(students_root):
        for item in os.listdir(students_root):
            p = os.path.join(students_root, item)
            if os.path.isdir(p) and item:
                exam_ids.add(item)

    paper_root = os.path.join('ftp_root', 'exam_paper_pool')
    if os.path.isdir(paper_root):
        for item in os.listdir(paper_root):
            p = os.path.join(paper_root, item)
            if os.path.isdir(p) and item:
                exam_ids.add(item)

    return exam_ids


def _sync_exam_students_from_users(db_session, exam_id):
    """
    从 users.home_dir 与学生目录反向补全 ExamStudent 记录（幂等）
    返回新增记录数
    """
    existing = db_session.query(ExamStudent).filter_by(exam_id=exam_id).all()
    existing_user_ids = {e.user_id for e in existing}
    existing_usernames = {e.username for e in existing}

    max_seat_row = db_session.query(func.max(ExamStudent.seat_number)).filter_by(exam_id=exam_id).first()
    max_seat = 0
    if max_seat_row and max_seat_row[0] and str(max_seat_row[0]).isdigit():
        max_seat = int(max_seat_row[0])

    added = 0
    # 使用跨平台路径标记
    path_mark = os.path.join('ftp_root', 'students', exam_id, '').replace('\\', '/')

    # 1) 从 users.home_dir 反推
    users = db_session.query(User).all()
    for user in users:
        norm_home = (user.home_dir or '').replace('\\', '/')
        if path_mark not in norm_home:
            continue
        if user.id in existing_user_ids or user.username in existing_usernames:
            continue

        suffix = norm_home.split(path_mark, 1)[-1].strip('/')
        guessed_username = suffix.split('/', 1)[0] if suffix else user.username
        if not guessed_username:
            guessed_username = user.username

        max_seat += 1
        db_session.add(ExamStudent(
            user_id=user.id,
            exam_id=exam_id,
            username=guessed_username,
            password_plain='',
            seat_number=f'{max_seat:03d}',
            student_dir=user.home_dir or os.path.join('ftp_root', 'students', exam_id, guessed_username),
            created_at=user.created_at or datetime.utcnow()
        ))
        existing_user_ids.add(user.id)
        existing_usernames.add(guessed_username)
        added += 1

    # 2) 从学生目录兜底（目录在但 home_dir 非标准格式时）
    students_root = os.path.join('ftp_root', 'students', exam_id)
    if os.path.isdir(students_root):
        for dirname in os.listdir(students_root):
            full_dir = os.path.join(students_root, dirname)
            if not os.path.isdir(full_dir):
                continue
            username = dirname.strip()
            if not username:
                continue
            if username in existing_usernames:
                continue

            user = db_session.query(User).filter_by(username=username).first()
            if not user or user.id in existing_user_ids:
                continue

            max_seat += 1
            db_session.add(ExamStudent(
                user_id=user.id,
                exam_id=exam_id,
                username=username,
                password_plain='',
                seat_number=f'{max_seat:03d}',
                student_dir=full_dir.replace('\\', '/'),
                created_at=user.created_at or datetime.utcnow()
            ))
            existing_user_ids.add(user.id)
            existing_usernames.add(username)
            added += 1

    return added


def generate_password(length=8):
    """生成随机密码（包含大小写字母和数字）"""
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(length))


def generate_6digit_account():
    """生成 6 位纯数字考生账号"""
    return ''.join(random.choice(string.digits) for _ in range(6))


def require_admin(f):
    """要求管理员权限（与 web_server.py 的 require_auth 逻辑一致，支持 X-Auth-Scope）"""
    @wraps(f)
    def decorated(*args, **kwargs):
        # 优先从 X-Auth-Scope header 读取作用域
        header_scope = request.headers.get('X-Auth-Scope')
        resolved_scope = header_scope if header_scope in (AUTH_SCOPE_ADMIN, AUTH_SCOPE_EXPLORER) else None
        auth_session = (request.args.get('auth_session') or '').strip()
        if not auth_session:
            auth_session = _exam_get_scoped_auth_session(default_scope=resolved_scope or AUTH_SCOPE_ADMIN)
        if auth_session:
            username, role = _exam_verify_auth_session_token(auth_session)
            if username:
                if role != 'admin':
                    return jsonify({'error': '权限不足'}), 403
                g.current_user = username
                g.current_role = role
                return f(*args, **kwargs)

        # Flask cookie session 兜底
        if 'username' in session:
            if session.get('role') != 'admin':
                return jsonify({'error': '权限不足'}), 403
            g.current_user = session.get('username')
            g.current_role = session.get('role')
            return f(*args, **kwargs)

        return jsonify({'error': '需要认证', 'redirect': '/login'}), 401
    return decorated


def require_user_auth(f):
    """要求用户登录认证（学生/普通用户，与 web_server.py 逻辑一致）"""
    @wraps(f)
    def decorated(*args, **kwargs):
        # 优先从 X-Auth-Scope header 读取作用域
        header_scope = request.headers.get('X-Auth-Scope')
        resolved_scope = header_scope if header_scope in (AUTH_SCOPE_ADMIN, AUTH_SCOPE_EXPLORER) else None
        auth_session = (request.args.get('auth_session') or '').strip()
        if not auth_session:
            auth_session = _exam_get_scoped_auth_session(default_scope=resolved_scope or AUTH_SCOPE_EXPLORER)
        if auth_session:
            username, role = _exam_verify_auth_session_token(auth_session)
            if username:
                g.current_user = username
                g.current_role = role or 'user'
                return f(*args, **kwargs)

        # Flask cookie session 兜底
        if 'username' in session:
            g.current_user = session.get('username')
            g.current_role = session.get('role', 'user')
            return f(*args, **kwargs)

        return jsonify({'error': '需要认证', 'redirect': '/login'}), 401
    return decorated


@exam_bp.route('/create-users', methods=['POST'])
@require_admin
def create_exam_users():
    """
    创建考试并批量生成 6 位纯数字考生账号
    参数：
        exam_id: 考试 ID（唯一标识）
        exam_name: 考试名称（可选）
        count: 考生数量
        quota_bytes: 每个考生的存储配额（字节）
    """
    try:
        data = request.get_json() or {}
    except Exception:
        data = {}
    
    exam_id = data.get('exam_id', '').strip()
    exam_name = data.get('exam_name', exam_id)
    count = data.get('count', 0)
    quota_bytes = data.get('quota_bytes', 0)
    
    if not exam_id:
        return jsonify({'error': '考试 ID 不能为空'}), 400
    
    if not count or count < 1 or count > 500:
        return jsonify({'error': '考生数量必须在 1-500 之间'}), 400
    
    # 验证 exam_id 格式
    if not all(c.isalnum() or c in '_-' for c in exam_id):
        return jsonify({'error': '考试 ID 只能包含字母、数字、下划线和连字符'}), 400
    
    students = []
    base_dir = 'ftp_root/students'
    exam_base_dir = os.path.join(base_dir, exam_id)
    os.makedirs(exam_base_dir, exist_ok=True)
    
    try:
        with get_session() as db_session:
            # 强约束：考试生成账号必须落到 MySQL 的 users 表
            dialect = db_session.bind.dialect.name if db_session.bind else ''
            if dialect != 'mysql':
                return jsonify({
                    'error': '当前数据库不是 MySQL，已阻止创建考生账号。请先配置 MYSQL_URL 并重启服务。'
                }), 400

            # 检查考试 ID 是否已存在
            existing_exam = db_session.query(Exam).filter_by(exam_id=exam_id).first()
            if existing_exam:
                return jsonify({'error': f'考试 ID "{exam_id}" 已存在，请使用不同的考试 ID'}), 400
            
            # 创建考试记录
            exam_record = Exam(
                exam_id=exam_id,
                exam_name=exam_name,
                quota_bytes=quota_bytes,
                created_at=datetime.utcnow()
            )
            db_session.add(exam_record)
            
            for i in range(1, count + 1):
                # 生成唯一的 6 位纯数字考生账号
                while True:
                    username = generate_6digit_account()
                    # 检查是否已存在
                    existing_user = db_session.query(User).filter_by(username=username).first()
                    if not existing_user:
                        break
                
                password = generate_password(8)
                seat_number = f'{i:03d}'
                
                # Unix 风格路径（与 admin 用户一致）：/ftp_root/students/{exam_id}/{username}
                # 存储在数据库中，FTP 服务器启动时解析为当前 OS 的物理路径
                unix_home_dir = f'/ftp_root/students/{exam_id}/{username}'
                unix_student_dir = unix_home_dir  # 与 home_dir 相同

                # 创建物理目录（使用 unix_to_physical 保证跨平台）
                physical_home_dir = unix_to_physical(unix_home_dir)
                os.makedirs(physical_home_dir, exist_ok=True)
                
                # 创建新用户
                user = User(
                    username=username,
                    password=hash_password(password),
                    role='student',
                    home_dir=unix_home_dir,
                    quota_bytes=quota_bytes,
                    used_bytes=0,
                    enabled=True,
                    created_at=datetime.utcnow()
                )
                db_session.add(user)
                db_session.flush()
                
                # 创建分卷记录（存储明文密码用于导出）
                exam_student = ExamStudent(
                    user_id=user.id,
                    exam_id=exam_id,
                    username=username,
                    password_plain=password,
                    seat_number=seat_number,
                    student_dir=unix_student_dir
                )
                db_session.add(exam_student)
                
                students.append({
                    'id': user.id,
                    'exam_student_id': exam_student.id,
                    'username': username,
                    'password': password,
                    'home_dir': unix_home_dir,
                    'seat_number': seat_number,
                    'student_dir': unix_student_dir,
                    'physical_dir': physical_home_dir,
                    'status': 'created'
                })
            
            db_session.commit()

            # 再次校验：确认本次生成账号确实写入 users 表
            created_usernames = [s['username'] for s in students]
            inserted_count = db_session.query(User).filter(User.username.in_(created_usernames)).count()
            if inserted_count != len(created_usernames):
                db_session.rollback()
                return jsonify({'error': '账号写入 users 表校验失败，请重试'}), 500
            
            # 记录日志
            admin_username = session.get('username', 'admin')
            log_ftp_action(admin_username, 'exam_create', 
                          filename=f'{exam_id} ({count} students)',
                          remote_ip=request.remote_addr)
        
        return jsonify({
            'success': True,
            'exam_id': exam_id,
            'exam_name': exam_name,
            'students': students,
            'count': len(students),
            'storage': 'mysql.users'
        })
    
    except Exception as e:
        logger.error(f'创建考试失败：{e}')
        return jsonify({'error': f'创建失败：{str(e)}'}), 500


@exam_bp.route('/upload_papers', methods=['POST'])
@require_admin
def upload_exam_papers():
    """
    上传试卷池（支持多份试卷 A/B/C...卷）
    参数：
        exam_id: 考试 ID
        files: 多个文件（支持 zip 自动解压）
    """
    exam_id = request.form.get('exam_id', '').strip()
    
    if not exam_id:
        return jsonify({'error': '考试 ID 不能为空'}), 400
    
    if 'files' not in request.files:
        return jsonify({'error': '未找到上传文件'}), 400
    
    files = request.files.getlist('files')
    if not files or all(f.filename == '' for f in files):
        return jsonify({'error': '文件名为空'}), 400
    
    # 试卷池目录：ftp_root/exam_paper_pool/{exam_id}/（跨平台路径）
    paper_pool_dir = os.path.join('ftp_root', 'exam_paper_pool', exam_id)
    os.makedirs(paper_pool_dir, exist_ok=True)
    
    uploaded_papers = []
    
    try:
        with get_session() as db_session:
            for file in files:
                if file.filename == '':
                    continue
                
                # 保留原始文件名（支持中文），只做基本安全检查
                # 移除路径分隔符和目录遍历攻击字符
                original_filename = file.filename
                safe_filename = original_filename.replace('/', '').replace('\\', '').replace('..', '')
                filename = safe_filename.strip()
                
                if filename.lower().endswith('.zip'):
                    # ZIP 文件：解压到试卷池目录
                    import tempfile
                    with tempfile.TemporaryDirectory() as temp_dir:
                        zip_path = os.path.join(temp_dir, filename)
                        file.save(zip_path)
                        
                        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                            zip_ref.extractall(paper_pool_dir)
                        
                        # 记录解压后的文件
                        for root, dirs, filenames in os.walk(paper_pool_dir):
                            for f in filenames:
                                if not f.startswith('.'):
                                    full_path = os.path.join(root, f)
                                    rel_path = os.path.relpath(full_path, paper_pool_dir)
                                    
                                    # 检查是否已记录
                                    existing = db_session.query(ExamPaper).filter_by(
                                        exam_id=exam_id,
                                        filename=f
                                    ).first()
                                    
                                    if not existing:
                                        paper = ExamPaper(
                                            exam_id=exam_id,
                                            filename=f,
                                            path=rel_path,
                                            created_at=datetime.utcnow()
                                        )
                                        db_session.add(paper)
                                        uploaded_papers.append({'filename': f, 'path': rel_path})
                else:
                    # 单个文件
                    file_path = os.path.join(paper_pool_dir, filename)
                    file.save(file_path)
                    
                    # 检查是否已记录
                    existing = db_session.query(ExamPaper).filter_by(
                        exam_id=exam_id,
                        filename=filename
                    ).first()
                    
                    if not existing:
                        paper = ExamPaper(
                            exam_id=exam_id,
                            filename=filename,
                            path=filename,
                            created_at=datetime.utcnow()
                        )
                        db_session.add(paper)
                        uploaded_papers.append({'filename': filename, 'path': filename})
            
            db_session.commit()
        
        # 记录日志
        admin_username = session.get('username', 'admin')
        log_ftp_action(admin_username, 'exam_upload_papers',
                      filename=f'{exam_id} ({len(uploaded_papers)} papers)',
                      remote_ip=request.remote_addr)
        
        return jsonify({
            'success': True,
            'exam_id': exam_id,
            'papers': uploaded_papers,
            'count': len(uploaded_papers),
            'paper_pool_dir': paper_pool_dir
        })
    
    except Exception as e:
        logger.error(f'上传试卷池失败：{e}')
        return jsonify({'error': f'上传失败：{str(e)}'}), 500


@exam_bp.route('/distribute', methods=['POST'])
@require_admin
def distribute_exam_files():
    """
    随机分卷：每人独立目录，随机抽取一份试卷
    参数：
        exam_id: 考试 ID
    """
    data = request.get_json(silent=True) or {}
    exam_id = (request.form.get('exam_id') or data.get('exam_id') or '').strip()

    if not exam_id:
        return jsonify({'error': '考试 ID 不能为空'}), 400

    logger.info(f'='*40)
    logger.info(f'开始分发试卷：exam_id={exam_id}')
    logger.info(f'='*40)

    try:
        with get_session() as db_session:
            # 每次分发前先同步试卷池（确保文件系统和数据库同步）
            _sync_exam_papers_from_fs(db_session, exam_id)
            db_session.commit()

            # 获取该考试的试卷池（过滤空文件名和无效记录）
            papers = db_session.query(ExamPaper).filter_by(exam_id=exam_id).all()

            # 过滤出有效的试卷文件（文件名非空且不含路径分隔符）
            valid_papers = []
            for p in papers:
                if p.filename and p.filename.strip() and not p.filename.startswith('.'):
                    valid_papers.append({'id': p.id, 'filename': p.filename, 'path': p.path})

            if not valid_papers:
                logger.error(f'考试 {exam_id} 没有有效试卷池')
                return jsonify({'error': '该考试没有试卷池，请先上传试卷'}), 400

            logger.info(f'考试 {exam_id} 试卷池文件：{[(p["filename"], p["path"]) for p in valid_papers]}')

            # ========== 预检查：验证试卷文件物理存在 ==========
            paper_pool_dir = os.path.abspath(os.path.join('ftp_root', 'exam_paper_pool', exam_id))
            logger.info(f'试卷池目录：{paper_pool_dir}')

            missing_papers = []
            for p in valid_papers:
                src_path = os.path.abspath(os.path.join(paper_pool_dir, p['path']))
                logger.info(f'  试卷 {p["filename"]} -> 物理路径：{src_path}, 存在：{os.path.exists(src_path)}')
                if not os.path.exists(src_path):
                    missing_papers.append(p)

            if missing_papers:
                logger.error(f'试卷池中有 {len(missing_papers)} 个文件物理不存在：{[p["filename"] for p in missing_papers]}')
                return jsonify({
                    'error': f'试卷池中有 {len(missing_papers)} 个文件物理不存在，请重新上传',
                    'missing': [p['filename'] for p in missing_papers]
                }), 400

            # ========== 预检查：验证考生目录物理存在 ==========
            students_exam_dir = os.path.abspath(os.path.join('ftp_root', 'students', exam_id))
            logger.info(f'考生目录：{students_exam_dir}, 存在：{os.path.exists(students_exam_dir)}')

            if not os.path.exists(students_exam_dir):
                logger.error(f'考生目录不存在：{students_exam_dir}')
                return jsonify({'error': f'考生目录不存在，请先创建考生账号'}), 400

            # 获取该考试的所有考生记录
            exam_students = db_session.query(ExamStudent).filter_by(exam_id=exam_id).all()

            if not exam_students:
                return jsonify({'error': '未找到考生记录，请先创建考生账号'}), 400

            logger.info(f'考试 {exam_id} 考生数量：{len(exam_students)}')

            distributed_count = 0
            failed_count = 0
            distribution_results = []

            for es in exam_students:
                try:
                    # 跳过已分发过的考生
                    if es.distributed_paper_path:
                        logger.info(f'考生 {es.username} 已分发过试卷，跳过')
                        continue

                    # 随机选择一份试卷
                    selected_paper = random.choice(valid_papers)

                    # 构造源试卷文件的物理路径
                    src_path = os.path.abspath(os.path.join(paper_pool_dir, selected_paper['path']))
                    logger.info(f'准备复制：{src_path}')

                    # Unix 风格目标路径（与 home_dir 一致）
                    unix_student_dir = f'/ftp_root/students/{exam_id}/{es.username}'

                    # 创建物理目录（使用 unix_to_physical 保证跨平台）
                    physical_student_dir = unix_to_physical(unix_student_dir)
                    logger.info(f'目标目录：{physical_student_dir}')
                    os.makedirs(physical_student_dir, exist_ok=True)

                    dst_filename = selected_paper['filename']
                    dst_path = os.path.join(physical_student_dir, dst_filename)

                    logger.info(f'复制目标路径：{dst_path}')

                    # 使用直接文件复制（比 FTP 更稳定）
                    success, error_msg = _direct_copy_file(src_path, dst_path)

                    if not success:
                        logger.error(f'文件复制失败：{es.username} - {error_msg}')
                        failed_count += 1
                        distribution_results.append({
                            'username': es.username,
                            'status': 'failed',
                            'reason': f'文件复制失败: {error_msg}'
                        })
                        continue

                    # 更新分卷记录（存储 Unix 风格路径，与 home_dir 风格一致）
                    es.distributed_paper_path = f'{unix_student_dir}/{dst_filename}'
                    es.distributed_paper_name = selected_paper['filename']
                    es.distributed_at = datetime.utcnow()

                    distributed_count += 1
                    distribution_results.append({
                        'username': es.username,
                        'seat_number': es.seat_number,
                        'paper': selected_paper['filename'],
                        'remote_path': es.distributed_paper_path,
                        'status': 'success'
                    })

                    logger.info(f'分卷成功：{es.username} -> {selected_paper["filename"]} -> {dst_path}')

                except Exception as e:
                    logger.error(f'分卷失败 {es.username}: {e}')
                    import traceback
                    logger.error(traceback.format_exc())
                    failed_count += 1
                    distribution_results.append({
                        'username': es.username,
                        'status': 'failed',
                        'reason': str(e)
                    })

            db_session.commit()

            # 记录日志
            admin_username = session.get('username', 'admin')
            log_ftp_action(admin_username, 'exam_distribute_random',
                          filename=f'{exam_id} ({distributed_count} success, {failed_count} failed)',
                          remote_ip=request.remote_addr)

            logger.info(f'分发完成：{exam_id}, 成功：{distributed_count}, 失败：{failed_count}')

            return jsonify({
                'success': True,
                'exam_id': exam_id,
                'distributed_count': distributed_count,
                'failed_count': failed_count,
                'total_students': len(exam_students),
                'results': distribution_results
            })

    except Exception as e:
        logger.error(f'随机分卷失败：{e}')
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({'error': f'分发失败：{str(e)}'}), 500


@exam_bp.route('/cleanup', methods=['DELETE'])
@require_admin
def cleanup_exam():
    """
    清除考试数据（删除考生账号、独立目录、试卷池）
    参数：
        exam_id: 考试 ID
        keep_papers: 是否保留试卷池（默认 false）
    """
    data = request.json or {}
    exam_id = data.get('exam_id', '').strip()
    keep_papers = data.get('keep_papers', False)
    
    if not exam_id:
        return jsonify({'error': '考试 ID 不能为空'}), 400
    
    try:
        with get_session() as db_session:
            # 查找该考试下的所有考生记录
            exam_students = db_session.query(ExamStudent).filter_by(exam_id=exam_id).all()
            
            deleted_dirs = []
            
            # 确保学生目录存在（跨平台路径）
            students_exam_dir = os.path.join('ftp_root', 'students', exam_id)
            
            for es in exam_students:
                # 先删子表记录（exam_students），再删父表 users，避免外键约束失败
                user = db_session.query(User).filter_by(id=es.user_id).first()

                # 删除独立目录（优先使用记录中的路径，需转换为物理路径）
                physical_student_dir = unix_to_physical(es.student_dir) if es.student_dir else ''
                if physical_student_dir and os.path.exists(physical_student_dir):
                    shutil.rmtree(physical_student_dir)
                    deleted_dirs.append(physical_student_dir)
                
                # 删除分卷记录
                db_session.delete(es)
                db_session.flush()

                # 删除关联的用户记录
                if user:
                    db_session.delete(user)
            
            # 额外清理：删除整个考试的学生目录（确保彻底删除）
            if os.path.exists(students_exam_dir):
                shutil.rmtree(students_exam_dir)
                deleted_dirs.append(students_exam_dir)
            
            # 删除试卷池（如果不需要保留）
            deleted_papers = 0
            if not keep_papers:
                papers = db_session.query(ExamPaper).filter_by(exam_id=exam_id).all()
                # 使用跨平台路径
                paper_pool_dir = os.path.abspath(os.path.join('ftp_root', 'exam_paper_pool', exam_id))
                
                for paper in papers:
                    db_session.delete(paper)
                    deleted_papers += 1
                
                if os.path.exists(paper_pool_dir):
                    shutil.rmtree(paper_pool_dir)
            
            # 删除 Exam 记录（彻底删除考试ID）
            exam_record = db_session.query(Exam).filter_by(exam_id=exam_id).first()
            if exam_record:
                db_session.delete(exam_record)
            
            db_session.commit()
            
            # 记录日志
            admin_username = session.get('username', 'admin')
            log_ftp_action(admin_username, 'exam_cleanup',
                          filename=f'{exam_id} ({len(deleted_dirs)} dirs, {deleted_papers} papers)',
                          remote_ip=request.remote_addr)
        
        return jsonify({
            'success': True,
            'exam_id': exam_id,
            'deleted_dirs': len(deleted_dirs),
            'deleted_papers': deleted_papers,
            'dir_list': deleted_dirs,
            'message': f'已清除 {len(deleted_dirs)} 个考生目录及 {deleted_papers} 份试卷'
        })
    
    except Exception as e:
        logger.error(f'清除考试数据失败：{e}')
        return jsonify({'error': f'清除失败：{str(e)}'}), 500


@exam_bp.route('/list', methods=['GET'])
@require_admin
def list_exams():
    """
    获取所有考试列表（从 exam_students 表查询，支持 6 位数字账号）
    """
    try:
        with get_session() as db_session:
            # 1) 从 Exam 表获取所有考试（核心数据源）
            exam_records = db_session.query(Exam).all()
            db_exam_ids = {e.exam_id for e in exam_records}
            
            # 2) 补充：收集 exam_papers 和 exam_students 表中的考试 ID
            paper_exam_ids = {row[0] for row in db_session.query(ExamPaper.exam_id).distinct().all() if row[0]}
            student_exam_ids = {row[0] for row in db_session.query(ExamStudent.exam_id).distinct().all() if row[0]}
            db_exam_ids = db_exam_ids.union(paper_exam_ids).union(student_exam_ids)

            # 3) 再收集文件系统中的考试（重启后依然可恢复显示）
            fs_exam_ids = _collect_exam_ids_from_fs()
            all_exam_ids = db_exam_ids.union(fs_exam_ids)

            exams = []
            for exam_id in all_exam_ids:
                # 同步该考试的试卷池文件（防止 DB 丢失但文件还在）
                _sync_exam_papers_from_fs(db_session, exam_id)
                # 同步考生明细（防止 exam_students 记录缺失）
                _sync_exam_students_from_users(db_session, exam_id)

                student_count = db_session.query(ExamStudent).filter_by(exam_id=exam_id).count()
                if student_count == 0:
                    # 兜底：从 users.home_dir 推断历史账号（支持跨平台路径）
                    # 检查 Windows 和 Unix 两种路径格式
                    unix_pattern = f'%/ftp_root/students/{exam_id}/%'
                    win_pattern = f'%\\ftp_root\\students\\{exam_id}\\%'
                    student_count = db_session.query(User).filter(
                        (User.home_dir.like(unix_pattern)) | (User.home_dir.like(win_pattern))
                    ).count()

                # 从 Exam 表获取考试信息
                exam_record = db_session.query(Exam).filter_by(exam_id=exam_id).first()
                created_at = exam_record.created_at.isoformat() if exam_record and exam_record.created_at else None
                exam_name = exam_record.exam_name if exam_record else ''

                exams.append({
                    'exam_id': exam_id,
                    'exam_name': exam_name,
                    'student_count': student_count,
                    'created_at': created_at
                })

            db_session.commit()
            exams.sort(key=lambda x: x['created_at'] or '', reverse=True)
            
            return jsonify({'exams': exams, 'count': len(exams)})
    
    except Exception as e:
        logger.error(f'获取考试列表失败：{e}')
        return jsonify({'error': f'获取考试列表失败：{str(e)}'}), 500


@exam_bp.route('/history', methods=['GET'])
@require_admin
def get_exam_history():
    """
    获取考试历史记录
    通过扫描 ftp_root/exam 目录获取已创建的考试
    """
    try:
        exam_base_dir = './ftp_root/exam'
        exams = []
        
        if os.path.exists(exam_base_dir):
            for item in os.listdir(exam_base_dir):
                item_path = os.path.join(exam_base_dir, item)
                if os.path.isdir(item_path) and item.startswith('stu_'):
                    # 从目录名提取 exam_id: stu_{exam_id}_{001} -> exam_id
                    parts = item.split('_', 2)
                    if len(parts) >= 3:
                        exam_id = parts[1]
                        
                        # 检查是否已记录
                        if not any(e['exam_id'] == exam_id for e in exams):
                            # 统计该考试的考生数量
                            student_count = len([d for d in os.listdir(exam_base_dir) 
                                               if d.startswith(f'stu_{exam_id}_')])
                            
                            # 查找考试文件
                            exam_file = None
                            first_student_dir = os.path.join(exam_base_dir, f'stu_{exam_id}_001')
                            if os.path.exists(first_student_dir):
                                for f in os.listdir(first_student_dir):
                                    if not f.startswith('.'):
                                        exam_file = f
                                        break
                            
                            # 获取创建时间（使用最早的学生目录时间）
                            created_at = None
                            for d in os.listdir(exam_base_dir):
                                if d.startswith(f'stu_{exam_id}_'):
                                    dir_path = os.path.join(exam_base_dir, d)
                                    stat = os.stat(dir_path)
                                    dir_created = datetime.fromtimestamp(stat.st_ctime)
                                    if created_at is None or dir_created < created_at:
                                        created_at = dir_created
                            
                            exams.append({
                                'exam_id': exam_id,
                                'student_count': student_count,
                                'exam_file': exam_file,
                                'created_at': created_at.isoformat() if created_at else None,
                                'status': 'active'
                            })
        
        # 按创建时间倒序排列
        exams.sort(key=lambda x: x['created_at'] or '', reverse=True)
        
        return jsonify({'exams': exams})
    
    except Exception as e:
        return jsonify({'error': f'获取历史记录失败：{str(e)}'}), 500


@exam_bp.route('/papers/<exam_id>', methods=['GET'])
@require_admin
def get_exam_papers(exam_id):
    """
    获取指定考试的试卷池列表
    """
    try:
        with get_session() as db_session:
            # 每次查询前先从目录同步，保证重启后也可恢复历史试卷池
            _sync_exam_papers_from_fs(db_session, exam_id)
            db_session.commit()

            papers = db_session.query(ExamPaper).filter_by(exam_id=exam_id).all()
            
            result = []
            for p in papers:
                result.append({
                    'id': p.id,
                    'filename': p.filename,
                    'path': p.path,
                    'created_at': p.created_at.isoformat() if p.created_at else None
                })
            
            logger.info(f'获取考试 {exam_id} 的试卷池，共 {len(result)} 份')
            return jsonify({'papers': result, 'count': len(result), 'exam_id': exam_id})
    
    except Exception as e:
        logger.error(f'获取试卷池失败：{e}')
        return jsonify({'error': f'获取试卷池失败：{str(e)}'}), 500


@exam_bp.route('/students/<exam_id>', methods=['GET'])
@require_admin
def get_exam_students(exam_id):
    """
    获取指定考试的所有考生信息（含完整账号密码）
    """
    try:
        with get_session() as db_session:
            # 每次查询前进行历史数据同步，确保重启后可恢复
            _sync_exam_students_from_users(db_session, exam_id)
            db_session.commit()

            # 通过 exam_students 表查询
            exam_students = db_session.query(ExamStudent).filter_by(exam_id=exam_id).all()
            
            result = []
            for es in exam_students:
                user = db_session.query(User).filter_by(id=es.user_id).first()
                
                if user:
                    student_info = {
                        'id': user.id,
                        'exam_student_id': es.id,
                        'username': user.username,
                        'password': es.password_plain or '[已加密]',  # 返回明文密码
                        'home_dir': user.home_dir,
                        'quota_bytes': user.quota_bytes,
                        'used_bytes': user.used_bytes,
                        'enabled': user.enabled,
                        'seat_number': es.seat_number,
                        'student_dir': es.student_dir,
                        'distributed_paper': es.distributed_paper_name,
                        'distributed_at': es.distributed_at.isoformat() if es.distributed_at else None,
                        'created_at': user.created_at.isoformat() if user.created_at else None
                    }
                    result.append(student_info)
            
            logger.info(f'获取考试 {exam_id} 的考生列表，共 {len(result)} 人')
            return jsonify({'students': result, 'count': len(result), 'exam_id': exam_id})
    
    except Exception as e:
        logger.error(f'获取考生列表失败：{e}')
        return jsonify({'error': f'获取考生列表失败：{str(e)}'}), 500


@exam_bp.route('/students/<exam_id>/export', methods=['GET'])
@require_admin
def export_exam_students(exam_id):
    """
    一键导出考生账户密码（CSV 格式，含明文密码）
    """
    try:
        with get_session() as db_session:
            _sync_exam_students_from_users(db_session, exam_id)
            db_session.commit()
            exam_students = db_session.query(ExamStudent).filter_by(exam_id=exam_id).all()
            
            output = io.StringIO()
            writer = csv.writer(output)
            
            # 写入 BOM 防止 Excel 乱码
            output.write('\ufeff')
            
            # 表头
            writer.writerow(['序号', '考生账号 (6 位数字)', '密码', '工位号', 'FTP 访问路径', '创建时间'])
            
            # 数据行 - 包含明文密码
            for idx, es in enumerate(exam_students, 1):
                user = db_session.query(User).filter_by(id=es.user_id).first()
                if user:
                    # 从 exam_students 表获取密码（创建时存储的明文）
                    password = getattr(es, 'password_plain', '[已加密]')
                    writer.writerow([
                        idx,
                        user.username,
                        password,
                        es.seat_number,
                        f"ftp://<server>:2121{user.home_dir}",
                        user.created_at.strftime('%Y-%m-%d %H:%M:%S') if user.created_at else ''
                    ])
            
            csv_content = output.getvalue()
            
            from flask import make_response
            response = make_response(csv_content)
            # 使用 ASCII 安全文件名，避免 latin-1 编码错误
            safe_exam_id = exam_id.encode('ascii', 'ignore').decode('ascii') or 'exam'
            filename = f"{safe_exam_id}_students_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
            response.headers['Content-Type'] = 'text/csv; charset=utf-8'
            return response
    
    except Exception as e:
        logger.error(f'导出考生信息失败：{e}')
        return jsonify({'error': f'导出失败：{str(e)}'}), 500


@exam_bp.route('/student/<int:student_id>', methods=['DELETE'])
@require_admin
def delete_exam_student(student_id):
    """
    删除单个考生（同步删除 FTP 目录）
    """
    try:
        with get_session() as db_session:
            # 查找分卷记录
            es = db_session.query(ExamStudent).filter_by(id=student_id).first()
            if not es:
                return jsonify({'error': '考生记录不存在'}), 404
            
            exam_id = es.exam_id
            username = es.username
            student_dir = es.student_dir
            
            # 删除物理目录（使用物理路径，兼容 Windows）
            physical_student_dir = unix_to_physical(student_dir) if student_dir else ''
            if physical_student_dir and os.path.exists(physical_student_dir):
                shutil.rmtree(physical_student_dir)
                logger.info(f'已删除考生目录：{physical_student_dir}')
            
            # 删除分卷记录
            db_session.delete(es)
            db_session.flush()

            # 删除关联用户（先删 exam_students 后删 users）
            user = db_session.query(User).filter_by(id=es.user_id).first()
            if user:
                db_session.delete(user)
            db_session.commit()
            
            # 记录日志
            admin_username = session.get('username', 'admin')
            log_ftp_action(admin_username, 'exam_delete_student',
                          filename=f'{exam_id}/{username}',
                          remote_ip=request.remote_addr)
            
            logger.info(f'删除考生成功：{exam_id}/{username}')
            return jsonify({
                'success': True,
                'message': f'考生 {username} 已删除',
                'student_id': student_id
            })
    
    except Exception as e:
        logger.error(f'删除考生失败：{e}')
        return jsonify({'error': f'删除失败：{str(e)}'}), 500


@exam_bp.route('/students/<exam_id>', methods=['DELETE'])
@require_admin
def delete_all_exam_students(exam_id):
    """
    一键清空某考试下所有考生
    """
    if not exam_id:
        return jsonify({'error': '考试 ID 不能为空'}), 400
    
    try:
        with get_session() as db_session:
            exam_students = db_session.query(ExamStudent).filter_by(exam_id=exam_id).all()
            
            deleted_count = 0
            for es in exam_students:
                # 先删子表记录（exam_students），再删父表 users）
                user = db_session.query(User).filter_by(id=es.user_id).first()

                # 删除物理目录（使用物理路径，兼容 Windows）
                physical_student_dir = unix_to_physical(es.student_dir) if es.student_dir else ''
                if physical_student_dir and os.path.exists(physical_student_dir):
                    shutil.rmtree(physical_student_dir)
                
                # 删除分卷记录
                db_session.delete(es)
                db_session.flush()

                # 删除关联用户
                if user:
                    db_session.delete(user)
                deleted_count += 1
            
            db_session.commit()
            
            # 记录日志
            admin_username = session.get('username', 'admin')
            log_ftp_action(admin_username, 'exam_delete_all_students',
                          filename=f'{exam_id} ({deleted_count} students)',
                          remote_ip=request.remote_addr)
            
            logger.info(f'清空考生成功：{exam_id}, 删除 {deleted_count} 名考生')
            return jsonify({
                'success': True,
                'message': f'已删除 {deleted_count} 名考生',
                'exam_id': exam_id,
                'deleted_count': deleted_count
            })
    
    except Exception as e:
        logger.error(f'清空考生失败：{e}')
        return jsonify({'error': f'清空失败：{str(e)}'}), 500


@exam_bp.route('/paper/<int:paper_id>', methods=['DELETE'])
@require_admin
def delete_exam_paper(paper_id):
    """
    删除单份试卷（同时删除数据库记录和磁盘文件）
    """
    try:
        with get_session() as db_session:
            # 查找试卷记录
            paper = db_session.query(ExamPaper).filter_by(id=paper_id).first()
            
            if not paper:
                return jsonify({'error': '试卷记录不存在'}), 404
            
            exam_id = paper.exam_id
            filename = paper.filename
            # 使用跨平台路径（path 字段存储文件相对路径）
            paper_pool_dir = os.path.abspath(os.path.join('ftp_root', 'exam_paper_pool', exam_id))
            file_path = os.path.join(paper_pool_dir, paper.path)
            
            # 检查是否已分发给考生
            distributed = db_session.query(ExamStudent).filter_by(
                exam_id=exam_id,
                distributed_paper_name=filename
            ).first()
            
            if distributed:
                return jsonify({
                    'error': f'该试卷已分发给考生 {distributed.username}，请先清理考生数据后再删除'
                }), 400
            
            # 删除物理文件
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f'已删除试卷文件：{file_path}')
            
            # 删除数据库记录
            db_session.delete(paper)
            db_session.commit()
            
            # 记录日志
            admin_username = session.get('username', 'admin')
            log_ftp_action(admin_username, 'exam_delete_paper',
                          filename=f'{exam_id}/{filename}',
                          remote_ip=request.remote_addr)
            
            logger.info(f'删除试卷成功：{exam_id}/{filename}')
            return jsonify({
                'success': True,
                'message': f'试卷 {filename} 已删除',
                'paper_id': paper_id
            })
    
    except Exception as e:
        logger.error(f'删除试卷失败：{e}')
        return jsonify({'error': f'删除失败：{str(e)}'}), 500


@exam_bp.route('/papers/<exam_id>', methods=['DELETE'])
@require_admin
def clear_exam_papers(exam_id):
    """
    一键清空某场考试的全部试卷
    """
    if not exam_id:
        return jsonify({'error': '考试 ID 不能为空'}), 400
    
    try:
        with get_session() as db_session:
            # 查找该考试的所有试卷
            papers = db_session.query(ExamPaper).filter_by(exam_id=exam_id).all()
            
            if not papers:
                return jsonify({'error': '该考试没有试卷记录'}), 404
            
            # 检查是否有试卷已分发
            for paper in papers:
                distributed = db_session.query(ExamStudent).filter_by(
                    exam_id=exam_id,
                    distributed_paper_name=paper.filename
                ).first()
                
                if distributed:
                    return jsonify({
                        'error': f'试卷 {paper.filename} 已分发给考生 {distributed.username}，请先清理考生数据'
                    }), 400
            
            deleted_count = 0
            # 使用跨平台路径
            paper_pool_dir = os.path.abspath(os.path.join('ftp_root', 'exam_paper_pool', exam_id))
            
            # 删除所有试卷文件和记录
            for paper in papers:
                file_path = os.path.join(paper_pool_dir, paper.path)
                file_path = os.path.abspath(file_path)
                
                if os.path.exists(file_path):
                    os.remove(file_path)
                
                db_session.delete(paper)
                deleted_count += 1
            
            # 删除试卷池目录（如果存在且为空）
            if os.path.exists(paper_pool_dir):
                try:
                    os.rmdir(paper_pool_dir)  # 仅当目录为空时删除
                except OSError:
                    pass  # 目录非空，保留
            
            db_session.commit()
            
            # 记录日志
            admin_username = session.get('username', 'admin')
            log_ftp_action(admin_username, 'exam_clear_papers',
                          filename=f'{exam_id} ({deleted_count} papers)',
                          remote_ip=request.remote_addr)
            
            logger.info(f'清空试卷池成功：{exam_id}, 删除 {deleted_count} 份试卷')
            return jsonify({
                'success': True,
                'message': f'已删除 {deleted_count} 份试卷',
                'exam_id': exam_id,
                'deleted_count': deleted_count
            })
    
    except Exception as e:
        logger.error(f'清空试卷池失败：{e}')
        return jsonify({'error': f'清空失败：{str(e)}'}), 500


# ==================== 学生端 API（供 user_explorer.html 调用）====================

@exam_bp.route('/my-exams', methods=['GET'])
@require_user_auth
def get_my_exams():
    """
    获取当前学生用户的考试信息（供 /explorer 页面调用）
    返回该学生账号关联的考试及试卷分发状态
    """
    try:
        username = g.current_user
        
        with get_session() as db_session:
            # 查找该用户的所有考试记录
            exam_students = db_session.query(ExamStudent).filter_by(username=username).all()
            
            if not exam_students:
                return jsonify({
                    'has_exams': False,
                    'exams': [],
                    'message': '暂无考试安排'
                })
            
            exams = []
            for es in exam_students:
                exam_info = {
                    'exam_id': es.exam_id,
                    'seat_number': es.seat_number,
                    'has_paper': bool(es.distributed_paper_name),
                    'paper_name': es.distributed_paper_name,
                    'distributed_paper_path': es.distributed_paper_path,
                    'distributed_at': es.distributed_at.isoformat() if es.distributed_at else None,
                    'student_dir': es.student_dir
                }
                exams.append(exam_info)
            
            return jsonify({
                'has_exams': True,
                'exams': exams,
                'username': username
            })
    
    except Exception as e:
        logger.error(f'获取学生考试信息失败：{e}')
        return jsonify({'error': f'获取失败：{str(e)}'}), 500


