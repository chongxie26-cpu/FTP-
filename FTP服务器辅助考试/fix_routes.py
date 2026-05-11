import re

fpath = r'c:\Users\19216\Desktop\W1edO9KqCz3AjsYmgfRd-master-84dffc9132366064742ca4793f6c30b5ef7eb560\web_server.py'

with open(fpath, 'r', encoding='utf-8') as f:
    content = f.read()

# Replace admin_panel function
old = '''@app.route('/admin')
def admin_panel():
    """管理员控制台 - 通过 auth_session Cookie 认证"""
    auth_session = request.cookies.get('auth_session')
    if auth_session:
        username, role = _verify_isolated_session(auth_session)
        if username and role == 'admin':
            g.current_user = username
            g.current_role = role
            return render_template('admin_panel.html')

    # 无有效 admin 会话：直接渲染登录页
    return render_template('login.html', next='/admin')'''

new = '''@app.route('/admin')
def admin_panel():
    """管理员控制台 - 优先从 URL 参数读取 auth_session"""
    auth_session = (request.args.get('auth_session') or '').strip()
    if not auth_session:
        auth_session = request.cookies.get('auth_session') or ''
    if auth_session:
        username, role = _verify_isolated_session(auth_session)
        if username and role == 'admin':
            g.current_user = username
            g.current_role = role
            return render_template('admin_panel.html')
    return render_template('login.html', next='/admin')'''

if old in content:
    content = content.replace(old, new, 1)
    print('admin_panel replaced')
else:
    print('admin_panel pattern not found')

# Replace user_explorer - check URL param first
old2 = '''@app.route('/explorer')
def user_explorer():
    """用户文件浏览器 - 统一入口，未登录时在页面内弹出登录框"""
    # 优先检查 auth_session Cookie 独立会话
    auth_session = request.cookies.get('auth_session')
    if auth_session:
        username, role = _verify_isolated_session(auth_session)
        if username:
            g.current_user = username
            g.current_role = role or 'user'
            return render_template('user_explorer.html')

    # 回退：Flask session
    g.current_user = session.get('username')
    g.current_role = session.get('role')

    # 管理员和普通用户都可以访问文件浏览器
    return render_template('user_explorer.html')'''

new2 = '''@app.route('/explorer')
def user_explorer():
    """用户文件浏览器 - 统一入口，未登录时在页面内弹出登录框"""
    # 优先从 URL 参数读取 auth_session
    auth_session = (request.args.get('auth_session') or '').strip()
    if not auth_session:
        auth_session = request.cookies.get('auth_session') or ''
    if auth_session:
        username, role = _verify_isolated_session(auth_session)
        if username:
            g.current_user = username
            g.current_role = role or 'user'
            return render_template('user_explorer.html')
    # 回退：Flask session
    g.current_user = session.get('username')
    g.current_role = session.get('role')
    return render_template('user_explorer.html')'''

if old2 in content:
    content = content.replace(old2, new2, 1)
    print('user_explorer replaced')
else:
    print('user_explorer pattern not found')

# Replace login_page - check URL param first
old3 = '''@app.route('/login')
def login_page():
    """登录页面"""
    next_page = request.args.get('next', '')

    # 优先检查 auth_session Cookie（admin / explorer 独立会话，浏览器自动携带）
    auth_session = request.cookies.get('auth_session')
    if auth_session:
        username, role = _verify_isolated_session(auth_session)
        if username:
            if next_page:
                return redirect(next_page)
            return redirect(url_for('admin_panel') if role == 'admin' else url_for('user_explorer'))'''

new3 = '''@app.route('/login')
def login_page():
    """登录页面"""
    next_page = request.args.get('next', '')
    # 优先从 URL 参数读取 auth_session
    auth_session = (request.args.get('auth_session') or '').strip()
    if not auth_session:
        auth_session = request.cookies.get('auth_session') or ''
    if auth_session:
        username, role = _verify_isolated_session(auth_session)
        if username:
            if next_page:
                return redirect(next_page)
            return redirect(url_for('admin_panel') if role == 'admin' else url_for('user_explorer'))'''

if old3 in content:
    content = content.replace(old3, new3, 1)
    print('login_page replaced')
else:
    print('login_page pattern not found')

# Replace explorer_login_page
old4 = '''@app.route('/explorer/login.html')
def explorer_login_page():
    """用户文件浏览器专用登录页"""
    next_page = request.args.get('next', '')

    # 优先检查 auth_session Cookie
    auth_session = request.cookies.get('auth_session')
    if auth_session:
        username, role = _verify_isolated_session(auth_session)
        if username:
            return redirect(url_for('user_explorer'))

    # 回退：Flask session
    if 'username' in session:
        return redirect(url_for('user_explorer'))

    # 未登录，渲染专用的文件浏览器登录页
    return render_template('explorer_login.html', next=next_page)'''

new4 = '''@app.route('/explorer/login.html')
def explorer_login_page():
    """用户文件浏览器专用登录页"""
    next_page = request.args.get('next', '')
    # 优先从 URL 参数读取 auth_session
    auth_session = (request.args.get('auth_session') or '').strip()
    if not auth_session:
        auth_session = request.cookies.get('auth_session') or ''
    if auth_session:
        username, role = _verify_isolated_session(auth_session)
        if username:
            return redirect(url_for('user_explorer'))
    if 'username' in session:
        return redirect(url_for('user_explorer'))
    return render_template('explorer_login.html', next=next_page)'''

if old4 in content:
    content = content.replace(old4, new4, 1)
    print('explorer_login_page replaced')
else:
    print('explorer_login_page pattern not found')

# Replace require_auth
old5 = '''        # 1. auth_session Cookie（explorer / admin 页面专用，浏览器自动携带）
        auth_session = request.cookies.get('auth_session')
        if auth_session:
            username, role = _verify_isolated_session(auth_session)
            if username:
                g.current_user = username
                g.current_role = role or 'user'
                return f(*args, **kwargs)
            # 独立会话无效，清除无效 cookie 继续往下
            # （让 Flask session 或 Basic Auth 作为 fallback）

        # 2. Flask cookie session'''

new5 = '''        # 1. 优先从 URL 参数读取 auth_session
        auth_session = (request.args.get('auth_session') or '').strip()
        if not auth_session:
            auth_session = request.cookies.get('auth_session') or ''
        if auth_session:
            username, role = _verify_isolated_session(auth_session)
            if username:
                g.current_user = username
                g.current_role = role or 'user'
                return f(*args, **kwargs)

        # 2. Flask cookie session'''

if old5 in content:
    content = content.replace(old5, new5, 1)
    print('require_auth replaced')
else:
    print('require_auth pattern not found')

# Replace login_required
old6 = '''        # 优先检查 X-Auth-Session（explorer / admin 独立会话）
        auth_session = (request.headers.get('X-Auth-Session') or '').strip()
        if auth_session:
            username, role = _verify_isolated_session(auth_session)
            if username:
                g.current_user = username
                g.current_role = role or 'user'
                return f(*args, **kwargs)

        # 回退：Flask cookie session'''

new6 = '''        # 优先从 URL 参数读取 auth_session
        auth_session = (request.args.get('auth_session') or '').strip()
        if not auth_session:
            auth_session = request.cookies.get('auth_session') or ''
        if auth_session:
            username, role = _verify_isolated_session(auth_session)
            if username:
                g.current_user = username
                g.current_role = role or 'user'
                return f(*args, **kwargs)

        # 回退：Flask cookie session'''

if old6 in content:
    content = content.replace(old6, new6, 1)
    print('login_required replaced')
else:
    print('login_required pattern not found')

# Replace api_logout cookie read
old7 = '    auth_session = request.cookies.get(\'auth_session\')'
if old7 in content:
    content = content.replace(old7, "    auth_session = (request.args.get('auth_session') or request.cookies.get('auth_session') or '').strip()", 1)
    print('api_logout replaced')
else:
    print('api_logout pattern not found')

with open(fpath, 'w', encoding='utf-8') as f:
    f.write(content)

print('Done')
