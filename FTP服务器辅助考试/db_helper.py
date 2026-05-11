"""
数据库访问层统一封装
所有 SQLAlchemy 操作集中于此模块，提供统一的 CRUD 接口和异常处理
"""

from sqlalchemy import create_engine, exc as sa_exc, inspect, text
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.pool import StaticPool
import bcrypt
import logging
from datetime import datetime
from contextlib import contextmanager
import os

from models import Base, User, FtpConfig, FtpLog, ExamPaper, ExamStudent

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 从 config.py 导入数据库配置（仅允许本地 MySQL）
try:
    from config import SQLALCHEMY_DATABASE_URI, DB_TYPE
    DATABASE_URL = SQLALCHEMY_DATABASE_URI
except (ImportError, AttributeError):
    raise RuntimeError("数据库配置加载失败：系统仅支持本地 MySQL。")

# 创建引擎
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=3600,
    echo=False
)

# 创建 Session 工厂
SessionLocal = scoped_session(sessionmaker(bind=engine))


class BizException(Exception):
    """业务异常基类"""
    def __init__(self, message, code=500):
        self.message = message
        self.code = code
        super().__init__(self.message)


@contextmanager
def get_session():
    """
    Session 上下文管理器
    自动 commit/rollback/close
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"数据库操作失败：{str(e)}")
        raise
    finally:
        session.close()


# ==================== 用户管理 ====================

def hash_password(password: str) -> str:
    """密码加密（bcrypt）"""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def verify_password(password: str, hashed: str) -> bool:
    """验证密码"""
    try:
        return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))
    except Exception:
        return False


def add_user(username: str, password: str, role: str = 'user', 
             quota_bytes: int = 0, home_dir: str = '') -> tuple:
    """
    添加用户
    返回：(success: bool, message_or_data: str|dict)
    """
    try:
        with get_session() as session:
            # 检查用户名是否存在
            existing = session.query(User).filter_by(username=username).first()
            if existing:
                return (False, f"用户 '{username}' 已存在")
            
            # 创建新用户
            user = User(
                username=username,
                password=hash_password(password),
                role=role,
                home_dir=home_dir or f"/ftp_root/{username}",
                quota_bytes=quota_bytes,
                used_bytes=0,
                enabled=True,
                created_at=datetime.utcnow()
            )
            session.add(user)
            session.flush()  # 获取自增 ID
            
            return (True, {
                "id": user.id,
                "username": user.username,
                "role": user.role,
                "home_dir": user.home_dir
            })
    except sa_exc.SQLAlchemyError as e:
        logger.error(f"添加用户失败：{str(e)}")
        return (False, f"数据库错误：{str(e)}")
    except Exception as e:
        logger.error(f"未知错误：{str(e)}")
        return (False, f"系统错误：{str(e)}")


def update_user(user_id: int, **kwargs) -> tuple:
    """
    更新用户信息
    kwargs 可包含：password, role, quota_bytes, enabled, home_dir
    返回：(success: bool, message: str)
    """
    try:
        with get_session() as session:
            user = session.query(User).filter_by(id=user_id).first()
            if not user:
                return (False, f"用户 ID {user_id} 不存在")
            
            # 更新字段
            if 'password' in kwargs and kwargs['password']:
                user.password = hash_password(kwargs['password'])
            if 'role' in kwargs:
                user.role = kwargs['role']
            if 'quota_bytes' in kwargs:
                user.quota_bytes = kwargs['quota_bytes']
            if 'enabled' in kwargs:
                user.enabled = kwargs['enabled']
            if 'home_dir' in kwargs:
                user.home_dir = kwargs['home_dir']
            
            session.add(user)
            return (True, f"用户 '{user.username}' 已更新")
    except sa_exc.SQLAlchemyError as e:
        logger.error(f"更新用户失败：{str(e)}")
        return (False, f"数据库错误：{str(e)}")
    except Exception as e:
        logger.error(f"未知错误：{str(e)}")
        return (False, f"系统错误：{str(e)}")


def delete_user(user_id: int) -> tuple:
    """
    删除用户
    返回：(success: bool, message: str)
    """
    try:
        with get_session() as session:
            user = session.query(User).filter_by(id=user_id).first()
            if not user:
                return (False, f"用户 ID {user_id} 不存在")
            
            username = user.username
            # 先删子表 exam_students，再删父表 users，避免外键约束失败
            exam_links = session.query(ExamStudent).filter_by(user_id=user_id).all()
            for link in exam_links:
                session.delete(link)
            session.flush()
            session.delete(user)
            return (True, f"用户 '{username}' 已删除")
    except sa_exc.SQLAlchemyError as e:
        logger.error(f"删除用户失败：{str(e)}")
        return (False, f"数据库错误：{str(e)}")
    except Exception as e:
        logger.error(f"未知错误：{str(e)}")
        return (False, f"系统错误：{str(e)}")


def list_users() -> tuple:
    """
    获取用户列表
    返回：(success: bool, data_or_message: list|str)
    """
    try:
        with get_session() as session:
            users = session.query(User).all()
            result = []
            for u in users:
                result.append({
                    "id": u.id,
                    "username": u.username,
                    "role": u.role,
                    "home_dir": u.home_dir,
                    "quota_bytes": u.quota_bytes,
                    "used_bytes": u.used_bytes,
                    "enabled": u.enabled,
                    "last_login": u.last_login.isoformat() if u.last_login else None,
                    "created_at": u.created_at.isoformat() if u.created_at else None
                })
            return (True, result)
    except sa_exc.SQLAlchemyError as e:
        logger.error(f"获取用户列表失败：{str(e)}")
        return (False, f"数据库错误：{str(e)}")
    except Exception as e:
        logger.error(f"未知错误：{str(e)}")
        return (False, f"系统错误：{str(e)}")


def get_user_by_username(username: str) -> tuple:
    """
    根据用户名获取用户
    返回：(success: bool, data_or_message: dict|str)
    """
    try:
        with get_session() as session:
            user = session.query(User).filter_by(username=username).first()
            if not user:
                return (False, f"用户 '{username}' 不存在")
            
            return (True, {
                "id": user.id,
                "username": user.username,
                "role": user.role,
                "perms": getattr(user, 'perms', 'elr'),
                "home_dir": user.home_dir,
                "quota_bytes": user.quota_bytes,
                "used_bytes": user.used_bytes,
                "enabled": user.enabled,
                "last_login": user.last_login.isoformat() if user.last_login else None
            })
    except sa_exc.SQLAlchemyError as e:
        logger.error(f"获取用户失败：{str(e)}")
        return (False, f"数据库错误：{str(e)}")
    except Exception as e:
        logger.error(f"未知错误：{str(e)}")
        return (False, f"系统错误：{str(e)}")


def authenticate_user(username: str, password: str) -> tuple:
    """
    用户认证（登录验证）
    返回：(success: bool, data_or_message: dict|str)
    """
    try:
        with get_session() as session:
            user = session.query(User).filter_by(username=username).first()
            if not user:
                return (False, "用户名或密码错误")
            
            if not user.enabled:
                return (False, "账户已被禁用")
            
            if not verify_password(password, user.password):
                return (False, "用户名或密码错误")
            
            # 更新最后登录时间
            user.last_login = datetime.utcnow()
            session.add(user)
            
            return (True, {
                "id": user.id,
                "username": user.username,
                "role": user.role,
                "home_dir": user.home_dir,
                "quota_bytes": user.quota_bytes,
                "used_bytes": user.used_bytes
            })
    except sa_exc.SQLAlchemyError as e:
        logger.error(f"认证失败：{str(e)}")
        return (False, "系统错误，请稍后重试")
    except Exception as e:
        logger.error(f"未知错误：{str(e)}")
        return (False, "系统错误，请稍后重试")


def update_user_quota(username: str, used_bytes: int) -> tuple:
    """
    更新用户已用配额
    返回：(success: bool, message: str)
    """
    try:
        with get_session() as session:
            user = session.query(User).filter_by(username=username).first()
            if not user:
                return (False, f"用户 '{username}' 不存在")
            
            user.used_bytes = used_bytes
            session.add(user)
            return (True, "配额已更新")
    except sa_exc.SQLAlchemyError as e:
        logger.error(f"更新配额失败：{str(e)}")
        return (False, f"数据库错误：{str(e)}")
    except Exception as e:
        logger.error(f"未知错误：{str(e)}")
        return (False, f"系统错误：{str(e)}")


# ==================== FTP 配置管理 ====================

def get_ftp_config() -> tuple:
    """
    获取 FTP 配置
    返回：(success: bool, data_or_message: dict|str)
    """
    try:
        with get_session() as session:
            cfg = FtpConfig.get_singleton(session)
            return (True, {
                "id": cfg.id,
                "max_download_speed": cfg.max_download_speed,
                "max_upload_speed": cfg.max_upload_speed,
                "max_connections": cfg.max_connections,
                "passive_ports_start": cfg.passive_ports_start,
                "passive_ports_end": cfg.passive_ports_end,
                "masquerade_address": cfg.masquerade_address
            })
    except sa_exc.SQLAlchemyError as e:
        logger.error(f"获取 FTP 配置失败：{str(e)}")
        return (False, f"数据库错误：{str(e)}")
    except Exception as e:
        logger.error(f"未知错误：{str(e)}")
        return (False, f"系统错误：{str(e)}")


def update_ftp_config(**kwargs) -> tuple:
    """
    更新 FTP 配置
    返回：(success: bool, message: str)
    """
    try:
        with get_session() as session:
            cfg = FtpConfig.get_singleton(session)
            
            if 'max_download_speed' in kwargs:
                cfg.max_download_speed = kwargs['max_download_speed']
            if 'max_upload_speed' in kwargs:
                cfg.max_upload_speed = kwargs['max_upload_speed']
            if 'max_connections' in kwargs:
                cfg.max_connections = kwargs['max_connections']
            if 'passive_ports_start' in kwargs:
                cfg.passive_ports_start = kwargs['passive_ports_start']
            if 'passive_ports_end' in kwargs:
                cfg.passive_ports_end = kwargs['passive_ports_end']
            if 'masquerade_address' in kwargs:
                cfg.masquerade_address = kwargs['masquerade_address']
            
            session.add(cfg)
            return (True, "FTP 配置已更新")
    except sa_exc.SQLAlchemyError as e:
        logger.error(f"更新 FTP 配置失败：{str(e)}")
        return (False, f"数据库错误：{str(e)}")
    except Exception as e:
        logger.error(f"未知错误：{str(e)}")
        return (False, f"系统错误：{str(e)}")


# ==================== 日志记录 ====================

def log_ftp_action(username: str, action: str, filename: str = '', 
                   remote_ip: str = '', bytes_transferred: int = 0) -> tuple:
    """
    记录 FTP 操作日志
    返回：(success: bool, message: str)
    """
    try:
        with get_session() as session:
            log = FtpLog(
                username=username,
                action=action,
                filename=filename,
                remote_ip=remote_ip,
                bytes_transferred=bytes_transferred,
                created_at=datetime.utcnow()
            )
            session.add(log)
            return (True, "日志已记录")
    except sa_exc.SQLAlchemyError as e:
        logger.error(f"记录日志失败：{str(e)}")
        return (False, f"数据库错误：{str(e)}")
    except Exception as e:
        logger.error(f"未知错误：{str(e)}")
        return (False, f"系统错误：{str(e)}")


def get_ftp_logs(limit: int = 100) -> tuple:
    """
    获取 FTP 日志（最近 N 条）
    返回：(success: bool, data_or_message: list|str)
    """
    try:
        with get_session() as session:
            logs = session.query(FtpLog).order_by(
                FtpLog.created_at.desc()
            ).limit(limit).all()
            
            result = []
            for log in logs:
                result.append({
                    "id": log.id,
                    "username": log.username,
                    "action": log.action,
                    "filename": log.filename,
                    "remote_ip": log.remote_ip,
                    "bytes_transferred": log.bytes_transferred,
                    "created_at": log.created_at.isoformat() if log.created_at else None
                })
            return (True, result)
    except sa_exc.SQLAlchemyError as e:
        logger.error(f"获取日志失败：{str(e)}")
        return (False, f"数据库错误：{str(e)}")
    except Exception as e:
        logger.error(f"未知错误：{str(e)}")
        return (False, f"系统错误：{str(e)}")


# ==================== 初始化数据库表 ====================

def init_db_tables():
    """
    初始化数据库表结构
    仅在首次部署时调用
    """
    try:
        Base.metadata.create_all(engine)
        # MySQL 下自动补齐历史版本缺失字段（幂等）
        if DB_TYPE == 'mysql':
            _ensure_mysql_schema_compatibility()
        logger.info("数据库表初始化成功")
        return (True, "表结构已创建")
    except sa_exc.SQLAlchemyError as e:
        logger.error(f"初始化表结构失败：{str(e)}")
        return (False, str(e))
    except Exception as e:
        logger.error(f"未知错误：{str(e)}")
        return (False, str(e))


def _ensure_mysql_schema_compatibility():
    """
    兼容旧版本表结构：自动补齐缺失列
    目标：避免 Unknown column 等运行时报错
    """
    inspector = inspect(engine)

    existing_tables = set(inspector.get_table_names())
    alter_sql_list = []

    if 'exam_students' in existing_tables:
        student_cols = {c['name'] for c in inspector.get_columns('exam_students')}
        if 'password_plain' not in student_cols:
            alter_sql_list.append(
                "ALTER TABLE exam_students ADD COLUMN password_plain VARCHAR(100) DEFAULT ''"
            )
        if 'seat_number' not in student_cols:
            alter_sql_list.append(
                "ALTER TABLE exam_students ADD COLUMN seat_number VARCHAR(20) DEFAULT ''"
            )
        if 'distributed_paper_id' not in student_cols:
            alter_sql_list.append(
                "ALTER TABLE exam_students ADD COLUMN distributed_paper_id INT NULL"
            )
        if 'distributed_paper_path' not in student_cols:
            alter_sql_list.append(
                "ALTER TABLE exam_students ADD COLUMN distributed_paper_path VARCHAR(500) DEFAULT ''"
            )
        if 'distributed_paper_name' not in student_cols:
            alter_sql_list.append(
                "ALTER TABLE exam_students ADD COLUMN distributed_paper_name VARCHAR(255) DEFAULT ''"
            )
        if 'distributed_at' not in student_cols:
            alter_sql_list.append(
                "ALTER TABLE exam_students ADD COLUMN distributed_at DATETIME NULL"
            )
        if 'student_dir' not in student_cols:
            alter_sql_list.append(
                "ALTER TABLE exam_students ADD COLUMN student_dir VARCHAR(500) DEFAULT ''"
            )

    if 'exam_papers' in existing_tables:
        paper_cols = {c['name'] for c in inspector.get_columns('exam_papers')}
        if 'filepath' not in paper_cols and 'path' in paper_cols:
            alter_sql_list.append(
                "ALTER TABLE exam_papers ADD COLUMN filepath VARCHAR(500) NULL"
            )
            alter_sql_list.append(
                "UPDATE exam_papers SET filepath = path WHERE filepath IS NULL"
            )
        elif 'filepath' not in paper_cols:
            alter_sql_list.append(
                "ALTER TABLE exam_papers ADD COLUMN filepath VARCHAR(500) NOT NULL DEFAULT ''"
            )

    with engine.begin() as conn:
        if alter_sql_list:
            for sql in alter_sql_list:
                conn.execute(text(sql))
            logger.info(f"MySQL 字段兼容修复完成，共执行 {len(alter_sql_list)} 条 SQL")

        # 确保外键为 ON DELETE CASCADE，支持直接在数据库删除父表记录
        _ensure_mysql_fk_cascade(conn)


def _ensure_mysql_fk_cascade(conn):
    """
    修复外键删除规则：
    - exam_students.user_id -> users.id 使用 ON DELETE CASCADE
    - exam_students.distributed_paper_id -> exam_papers.id 使用 ON DELETE SET NULL
    """
    fk_rows = conn.execute(text("""
        SELECT CONSTRAINT_NAME, DELETE_RULE
        FROM information_schema.REFERENTIAL_CONSTRAINTS
        WHERE CONSTRAINT_SCHEMA = DATABASE()
          AND TABLE_NAME = 'exam_students'
    """)).fetchall()

    fk_map = {row[0]: row[1] for row in fk_rows}

    # user_id 外键：需要 CASCADE
    user_fk_name = None
    user_fk_rule = None
    user_fk_cols = conn.execute(text("""
        SELECT CONSTRAINT_NAME
        FROM information_schema.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'exam_students'
          AND COLUMN_NAME = 'user_id'
          AND REFERENCED_TABLE_NAME = 'users'
          AND REFERENCED_COLUMN_NAME = 'id'
        LIMIT 1
    """)).fetchone()
    if user_fk_cols:
        user_fk_name = user_fk_cols[0]
        user_fk_rule = fk_map.get(user_fk_name)

    if user_fk_name and user_fk_rule != 'CASCADE':
        conn.execute(text(f"ALTER TABLE exam_students DROP FOREIGN KEY `{user_fk_name}`"))
        conn.execute(text("""
            ALTER TABLE exam_students
            ADD CONSTRAINT fk_exam_students_user_id
            FOREIGN KEY (user_id) REFERENCES users(id)
            ON DELETE CASCADE ON UPDATE CASCADE
        """))
        logger.info("已修复外键 exam_students.user_id 为 ON DELETE CASCADE")

    # distributed_paper_id 外键：建议 SET NULL（删除试卷不强制删学生记录）
    paper_fk_name = None
    paper_fk_rule = None
    paper_fk_cols = conn.execute(text("""
        SELECT CONSTRAINT_NAME
        FROM information_schema.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'exam_students'
          AND COLUMN_NAME = 'distributed_paper_id'
          AND REFERENCED_TABLE_NAME = 'exam_papers'
          AND REFERENCED_COLUMN_NAME = 'id'
        LIMIT 1
    """)).fetchone()
    if paper_fk_cols:
        paper_fk_name = paper_fk_cols[0]
        paper_fk_rule = fk_map.get(paper_fk_name)

    if paper_fk_name and paper_fk_rule != 'SET NULL':
        conn.execute(text(f"ALTER TABLE exam_students DROP FOREIGN KEY `{paper_fk_name}`"))
        conn.execute(text("""
            ALTER TABLE exam_students
            ADD CONSTRAINT fk_exam_students_paper_id
            FOREIGN KEY (distributed_paper_id) REFERENCES exam_papers(id)
            ON DELETE SET NULL ON UPDATE CASCADE
        """))
        logger.info("已修复外键 exam_students.distributed_paper_id 为 ON DELETE SET NULL")