import os
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()   # 加载 .env 文件（如果有）

# -------------------- 数据库 --------------------
# 仅允许本地 MySQL（localhost / 127.0.0.1 / ::1）
MYSQL_URL = os.getenv("MYSQL_URL", "").strip()
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "123456")
MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
MYSQL_PORT = os.getenv("MYSQL_PORT", "3306")
MYSQL_DB = os.getenv("MYSQL_DB", "ftpdb")

if MYSQL_URL:
    SQLALCHEMY_DATABASE_URI = MYSQL_URL
else:
    SQLALCHEMY_DATABASE_URI = (
        f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}?charset=utf8mb4"
    )

parsed = urlparse(SQLALCHEMY_DATABASE_URI)
if not parsed.scheme.startswith("mysql"):
    raise RuntimeError("仅支持本地 MySQL，当前配置不是 MySQL。")
if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
    raise RuntimeError(f"仅支持本地 MySQL，当前主机为 {parsed.hostname}。")

DB_TYPE = 'mysql'

SQLALCHEMY_TRACK_MODIFICATIONS = False

# -------------------- Flask --------------------
SECRET_KEY = os.getenv("SECRET_KEY", "ChangeMePlease")
JWT_EXPIRES_HOURS = 24

# -------------------- FTP --------------------
FTP_PORT = int(os.getenv("FTP_PORT", 2121))
# FTP_HOST 用于客户端连接，始终使用 127.0.0.1
FTP_HOST = "127.0.0.1"
FTP_ROOT = os.getenv("FTP_ROOT", "ftp_root")
FTP_USER = os.getenv("FTP_USER", "admin")
FTP_PASSWORD = os.getenv("FTP_PASSWORD", "admin123")

# 被动端口范围
PASSIVE_PORT_START = 60000
PASSIVE_PORT_END   = 60099

# 限速（KB/s，0=不限）
DEFAULT_MAX_DOWN = 0
DEFAULT_MAX_UP   = 0