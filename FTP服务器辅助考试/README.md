# FTP 文件管理系统

集 FTP 服务器、Web 管理后台、用户文件浏览器、在线考试系统于一体的完整解决方案。

## 快速启动

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 启动服务

```bash
python main.py
```

服务启动后访问 **http://localhost:8080**

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--ftp-port` | FTP 端口 | 2121 |
| `--web-port` | Web 端口 | 8080 |
| `--no-browser` | 不自动打开浏览器 | - |

### 默认账号

| 角色 | 用户名 | 密码 | 权限 |
|------|--------|------|------|
| 管理员 | admin | admin123 | 完全访问 + 管理后台 |
| 普通用户 | guest | guest123 | 只读（elr） |

## 项目结构

```
.
├── main.py              # 主启动脚本（一键启动所有服务）
├── ftp_server.py        # FTP 服务器核心
├── web_server.py        # Web 管理后台 API
├── web_server_exam.py   # 在线考试系统 API
├── models.py            # SQLAlchemy 数据模型
├── db_helper.py         # 数据访问层
├── config.py            # 统一配置（MySQL/SQLite 自动切换）
├── templates/
│   ├── index.html       # 统一门户
│   ├── login.html       # 登录页
│   ├── admin_panel.html # 管理员控制台
│   ├── exam_admin.html  # 考试管理后台
│   └── user_explorer.html # 用户文件浏览器
├── scripts/
│   ├── init_mysql.sql   # MySQL 数据库初始化
│   └── migrate_to_mysql.py # 数据迁移脚本
├── ftp_root/            # FTP 用户目录
│   ├── admin/          # 管理员目录
│   ├── guest/          # 访客目录
│   ├── students/       # 学生目录
│   └── exam_paper_pool/ # 考试试卷池
└── requirements.txt     # Python 依赖
```

## 功能模块

### 管理员控制台（/admin）

- **仪表盘**：实时监控 FTP 连接数、在线用户、磁盘使用、运行时间
- **用户管理**：增删改查 FTP 账号，配额/限速/权限配置
- **服务器控制**：启动/停止/重启 FTP 服务
- **操作日志**：实时查看所有 FTP 操作记录
- **考试管理**：创建考试、随机分发试卷、批量生成账号

### 用户文件浏览器（/explorer）

- 浏览、上传、下载、删除、重命名、新建文件夹
- 拖拽上传、进度显示、权限隔离（每个用户只能访问自己的目录）

### 在线考试系统（/exam）

- 上传题库 Excel，自动解析生成试卷
- 随机分发：同一考试不同学生抽取不同题目
- 批量创建学生账号、导出账号信息
- 考试数据统计

## 技术栈

| 组件 | 技术 |
|------|------|
| FTP 服务器 | pyftpdlib 2.0.1 |
| Web 框架 | Flask 2.3.3 |
| 数据库 ORM | SQLAlchemy 2.0 |
| 数据库 | MySQL 8.0 / SQLite |
| 前端框架 | Tailwind CSS (CDN) |
| 图表库 | Chart.js 4.4 |
| 图标 | Font Awesome 6.5 |

## 数据库配置

系统自动检测 MySQL 是否可用，不可用时自动回退到 SQLite。

### MySQL 模式

在项目根目录新建 `.env` 文件：

```
MYSQL_URL=mysql+pymysql://root:123456@localhost:3306/ftpdb?charset=utf8mb4
```

或设置环境变量：

```bash
# Linux/Mac
export MYSQL_URL="mysql+pymysql://root:123456@localhost:3306/ftp_server?charset=utf8mb4"

# Windows
set MYSQL_URL=mysql+pymysql://root:123456@localhost:3306/ftpdb?charset=utf8mb4
```

初始化 MySQL 数据库：

```bash
mysql -u root -p < scripts/init_mysql.sql
```

### SQLite 模式（无需配置）

不提供 `.env` 文件时，系统自动使用 SQLite，零配置即可运行。

## API 接口

### 认证 API

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/auth/login` | POST | 登录 |
| `/api/auth/logout` | POST | 登出 |
| `/api/auth/me` | GET | 获取当前用户信息 |

### 文件操作 API（需认证）

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/files` | GET | 列出文件 |
| `/api/download` | GET | 下载文件 |
| `/api/upload` | POST | 上传文件 |
| `/api/delete` | POST | 删除文件 |
| `/api/mkdir` | POST | 创建目录 |
| `/api/rename` | POST | 重命名 |

### 管理员 API（需 admin 权限）

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/admin/users` | GET | 用户列表 |
| `/api/admin/users` | POST | 添加用户 |
| `/api/admin/users/<id>` | PUT | 更新用户 |
| `/api/admin/users/<id>` | DELETE | 删除用户 |
| `/api/admin/overview` | GET | 仪表盘概览数据 |
| `/api/server/start` | POST | 启动 FTP |
| `/api/server/stop` | POST | 停止 FTP |
| `/api/server/config` | GET/POST | 获取/更新配置 |
| `/api/health` | GET | 健康检查 |

### 考试 API（需 admin 权限）

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/exam/papers` | GET | 试卷列表 |
| `/api/exam/papers` | POST | 上传题库 |
| `/api/exam/distribute` | POST | 分发试卷 |
| `/api/exam/students` | GET | 学生列表 |
| `/api/exam/students/batch` | POST | 批量创建学生 |

## 常见问题

### 端口被占用

```bash
# 查看端口占用
netstat -tlnp | grep 2121
netstat -tlnp | grep 8080

# 修改端口
python main.py --ftp-port 2122 --web-port 8081
```

### MySQL 连接失败

1. 确认 MySQL 服务已启动
2. 检查 `.env` 中的连接字符串
3. 确认用户有访问权限：`GRANT ALL ON ftp_server.* TO 'user'@'localhost'`

### 无法上传文件

- 检查用户权限是否包含 `w`
- 检查磁盘空间
- 查看 `web_server.log` 日志

## 部署说明

### Windows

1. 安装 Python 3.8+
2. 安装 MySQL 8.0+（可选）
3. `pip install -r requirements.txt`
4. 配置 MySQL（可选）或直接运行 SQLite 模式
5. `python main.py`

### Linux / macOS

```bash
# 安装 MySQL
sudo apt install mysql-server  # Ubuntu/Debian
brew install mysql@8.0         # macOS

# 启动 MySQL
sudo systemctl start mysql

# 初始化数据库
mysql -u root -p < scripts/init_mysql.sql

# 启动服务
python main.py
```

### 生产环境

- 修改默认管理员密码
- 配置防火墙，仅开放 2121 和 8080 端口
- 使用 Nginx 反向代理 + HTTPS
- 配置定期数据库备份
- 日志文件配置 logrotate 轮转
