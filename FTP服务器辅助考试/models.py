from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, BigInteger, ForeignKey, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship, synonym
from datetime import datetime
import os

# 从 config.py 导入数据库配置（仅允许本地 MySQL）
try:
    from config import SQLALCHEMY_DATABASE_URI, DB_TYPE
    DATABASE_URL = SQLALCHEMY_DATABASE_URI
except (ImportError, AttributeError):
    raise RuntimeError("数据库配置加载失败：系统仅支持本地 MySQL。")

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False)
    password = Column(String(255), nullable=False)  # 存 bcrypt 哈希
    role = Column(String(20), default='user')       # admin / user
    perms = Column(String(50), default='elr')       # FTP 权限：e=列表,l=读,r=下载,w=写,d=删除
    home_dir = Column(String(255), default='')
    quota_bytes = Column(BigInteger, default=0)     # 字节配额，0=不限
    used_bytes = Column(BigInteger, default=0)
    enabled = Column(Boolean, default=True)
    last_login = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<User {self.username}>"


class ExamPaper(Base):
    """试卷池表 - 存储每场考试的多份试卷"""
    __tablename__ = 'exam_papers'

    id = Column(Integer, primary_key=True, autoincrement=True)
    exam_id = Column(String(100), nullable=False, index=True)  # 考试 ID
    filename = Column(String(255), nullable=False)  # 文件名
    filepath = Column(String(500), nullable=False)  # 服务器路径
    # 兼容历史代码：旧逻辑使用 path 字段读写试卷相对路径
    path = synonym('filepath')
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<ExamPaper {self.exam_id}/{self.filename}>"


class Exam(Base):
    """考试信息表 - 持久化存储考试基本信息"""
    __tablename__ = 'exams'

    id = Column(Integer, primary_key=True, autoincrement=True)
    exam_id = Column(String(100), unique=True, nullable=False, index=True)  # 考试 ID（唯一标识）
    exam_name = Column(String(255), default='')  # 考试名称
    description = Column(Text, default='')  # 考试描述
    quota_bytes = Column(BigInteger, default=0)  # 每个考生的存储配额
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Exam {self.exam_id}>"


class ExamStudent(Base):
    """考生分卷记录表 - 记录每位考生分到的具体试卷"""
    __tablename__ = 'exam_students'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)  # 关联 users 表
    exam_id = Column(String(100), nullable=False, index=True)  # 考试 ID
    username = Column(String(50), nullable=False)  # 考生用户名
    password_plain = Column(String(100), default='')  # 明文密码（用于导出）
    seat_number = Column(String(20), default='')  # 工位号
    distributed_paper_id = Column(Integer, ForeignKey('exam_papers.id'), nullable=True)  # 分到的试卷 ID
    distributed_paper_path = Column(String(500), default='')  # 分到的试卷路径（相对路径）
    distributed_paper_name = Column(String(255), default='')  # 分到的试卷文件名
    distributed_at = Column(DateTime, nullable=True)  # 分卷时间
    student_dir = Column(String(500), default='')  # 学生独立目录路径
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<ExamStudent {self.username} - {self.exam_id}>"


class FtpConfig(Base):
    __tablename__ = 'ftp_config'

    id = Column(Integer, primary_key=True)
    max_download_speed = Column(Integer, default=0)  # KB/s，0=不限
    max_upload_speed = Column(Integer, default=0)
    max_connections = Column(Integer, default=100)
    passive_ports_start = Column(Integer, default=60000)
    passive_ports_end = Column(Integer, default=60099)
    masquerade_address = Column(String(64), default='')

    # 单例行，id 固定为 1
    @staticmethod
    def get_singleton(session):
        cfg = session.query(FtpConfig).first()
        if not cfg:
            cfg = FtpConfig()
            session.add(cfg)
            session.commit()
        return cfg

class FtpLog(Base):
    __tablename__ = 'ftp_logs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), nullable=False)
    action = Column(String(50), nullable=False)   # login/upload/download/delete
    filename = Column(String(255), default='')
    remote_ip = Column(String(64), default='')
    bytes_transferred = Column(BigInteger, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<FtpLog {self.username} {self.action}>"

# 创建引擎（与 db_helper.py 保持一致）
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=3600,
    echo=False
)

# 全局 Session 工厂
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

def init_db():
    """创建所有表（幂等）"""
    Base.metadata.create_all(bind=engine)

def get_db():
    """FastAPI 风格依赖注入，也可手动调用"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()