from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
import sqlite3, hashlib, os, random, json
from datetime import datetime, timedelta
from functools import wraps

app = Flask(__name__)
app.secret_key = 'arcline-secret-key-2024'
DB_PATH = os.path.join(os.path.dirname(__file__), 'arcline.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('role') != 'admin':
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT DEFAULT 'client',
        business_name TEXT DEFAULT '',
        appointment_value REAL DEFAULT 170.0,
        subscription_price REAL DEFAULT 299.0,
        hours_open TEXT DEFAULT '08:00',
        hours_close TEXT DEFAULT '18:00',
        status TEXT DEFAULT 'active',
        receptionist_on INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        invoice_number TEXT,
        amount REAL,
        status TEXT DEFAULT 'pending',
        client_name TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        duration INTEGER,
        outcome TEXT,
        summary TEXT,
        out_of_hours INTEGER DEFAULT 0,
        called_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')

    if not c.execute("SELECT id FROM users WHERE email='admin@arcline.ai'").fetchone():
        c.execute("INSERT INTO users (name,email,password,role,business_name) VALUES (?,?,?,?,?)",
                  ('Admin','admin@arcline.ai',hash_pw('admin123'),'admin','Arcline HQ'))

    if not c.execute("SELECT id FROM users WHERE email='taylor@clinic.com'").fetchone():
        c.execute("""INSERT INTO users
            (name,email,password,role,business_name,appointment_value,subscription_price,hours_open,hours_close,status)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ('Taylor','taylor@clinic.com',hash_pw('demo123'),'client',
             'Metro Medical Clinic',170.0,299.0,'08:00','18:00','active'))
        conn.commit()
        uid = c.execute("SELECT id FROM users WHERE email='taylor@clinic.com'").fetchone()['id']
        for inv in [
            (uid,'INV-2024-0342',1299.00,'paid','Metro Medical Clinic'),
            (uid,'INV-2024-0341',599.00,'pending','Metro Medical Clinic'),
            (uid,'INV-2024-0340',1299.00,'paid','Metro Medical Clinic'),
            (uid,'INV-2024-0339',1299.00,'paid','Metro Medical Clinic'),
            (uid,'INV-2024-0338',899.00,'overdue','Metro Medical Clinic'),
        ]:
            c.execute("INSERT INTO invoices (user_id,invoice_number,amount,status,client_name) VALUES (?,?,?,?,?)", inv)
        outcomes = ['Appointment Booked','Appointment Booked','Appointment Booked',
                    'Question Asked','Transferred','Out-of-Hours','Cancelled']
        summaries = [
            'Patient called to book a follow-up appointment for next week.',
            'Caller asked about opening hours and available doctors.',
            'Call transferred to specialist department as requested.',
            'After-hours call — patient needed urgent medical advice.',
            'Patient cancelled their existing Friday appointment.',
            'New patient registration and first appointment confirmed.',
            'Prescription refill request forwarded to the GP on duty.',
            'General enquiry about Medicare bulk billing eligibility.',
            'Patient rescheduled to the following Monday successfully.',
            'Caller requested a referral letter for specialist visit.',
        ]
        random.seed(42)
        for i in range(60):
            days_ago = random.randint(0, 29)
            hour = random.randint(0, 23)
            called_at = datetime.now() - timedelta(days=days_ago, hours=hour, minutes=random.randint(0,59))
            duration = random.randint(45, 600)
            outcome = random.choice(outcomes)
            summary = random.choice(summaries)
            out_of_hours = 1 if (hour < 8 or hour >= 18) else 0
            c.execute("INSERT INTO calls (user_id,duration,outcome,summary,out_of_hours,called_at) VALUES (?,?,?,?,?,?)",
                      (uid, duration, outcome, summary, out_of_hours, called_at.strftime('%Y-%m-%d %H:%M:%S')))
    conn.commit()
    conn.close()

# ── ROUTES ────────────────────────────────────────────────────
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('admin_panel') if session.get('role') == 'admin' else url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET','POST'])
def login():
    error = None
    if request.method == 'POST':
        email = request.form.get('email','').strip().lower()
        pw    = request.form.get('password','')
        conn  = get_db()
        user  = conn.execute("SELECT * FROM users WHERE email=? AND password=?",
                             (email, hash_pw(pw))).fetchone()
        conn.close()
        if user:
            session.update({'user_id':user['id'],'name':user['name'],
                            'email':user['email'],'role':user['role'],
                            'business':user['business_name']})
            return redirect(url_for('admin_panel') if user['role']=='admin' else url_for('dashboard'))
        error = 'Invalid email or password.'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    if session.get('role') == 'admin':
        return redirect(url_for('admin_panel'))
    conn  = get_db()
    uid   = session['user_id']
    user  = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    calls = conn.execute("SELECT * FROM calls WHERE user_id=? ORDER BY called_at DESC LIMIT 10",(uid,)).fetchall()
    conn.close()
    return render_template('dashboard.html', user=user, calls=calls)

@app.route('/payments')
@login_required
def payments():
    if session.get('role') == 'admin':
        return redirect(url_for('admin_panel'))
    conn = get_db()
    uid  = session['user_id']
    user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    invoices = conn.execute("SELECT * FROM invoices WHERE user_id=? ORDER BY created_at DESC",(uid,)).fetchall()
    month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0)
    mins_row = conn.execute(
        "SELECT COALESCE(SUM(duration),0) as total FROM calls WHERE user_id=? AND called_at>=?",
        (uid, month_start.strftime('%Y-%m-%d %H:%M:%S'))).fetchone()
    conn.close()
    total_minutes = round(mins_row['total'] / 60, 1)
    rate = 0.35
    projected = round(total_minutes * rate, 2)
    return render_template('payments.html', user=user, invoices=invoices,
                           total_minutes=total_minutes, projected=projected, rate=rate)

@app.route('/api/stats')
@login_required
def api_stats():
    uid    = session['user_id']
    period = request.args.get('period','30d')
    conn   = get_db()
    user   = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    now    = datetime.now()
    if   period == '24h': since = now - timedelta(hours=24)
    elif period == 'mtd': since = now.replace(day=1,hour=0,minute=0,second=0)
    elif period == 'ytd': since = now.replace(month=1,day=1,hour=0,minute=0,second=0)
    else:                 since = now - timedelta(days=30)
    s = since.strftime('%Y-%m-%d %H:%M:%S')
    booked     = conn.execute("SELECT COUNT(*) as c FROM calls WHERE user_id=? AND outcome='Appointment Booked' AND called_at>=?",(uid,s)).fetchone()['c']
    ooh_calls  = conn.execute("SELECT COUNT(*) as c FROM calls WHERE user_id=? AND out_of_hours=1 AND called_at>=?",(uid,s)).fetchone()['c']
    ooh_booked = conn.execute("SELECT COUNT(*) as c FROM calls WHERE user_id=? AND out_of_hours=1 AND outcome='Appointment Booked' AND called_at>=?",(uid,s)).fetchone()['c']
    apt_val    = user['appointment_value'] or 170
    revenue    = booked * apt_val
    chart_since = now - timedelta(days=29)
    rows = conn.execute(
        "SELECT DATE(called_at) as day, SUM(duration) as total FROM calls WHERE user_id=? AND called_at>=? GROUP BY day ORDER BY day",
        (uid, chart_since.strftime('%Y-%m-%d'))).fetchall()
    conn.close()
    days_map = {}
    for i in range(30):
        d = (chart_since + timedelta(days=i)).strftime('%Y-%m-%d')
        days_map[d] = 0
    for r in rows:
        if r['day'] in days_map:
            days_map[r['day']] = round(r['total']/60, 1)
    return jsonify({'booked':booked,'revenue':revenue,'ooh_calls':ooh_calls,
                    'ooh_booked':ooh_booked,'chart_labels':list(days_map.keys()),
                    'chart_data':list(days_map.values()),
                    'receptionist_on':user['receptionist_on']})

@app.route('/api/toggle_receptionist', methods=['POST'])
@login_required
def toggle_receptionist():
    uid  = session['user_id']
    conn = get_db()
    cur  = conn.execute("SELECT receptionist_on FROM users WHERE id=?",(uid,)).fetchone()['receptionist_on']
    new  = 0 if cur else 1
    conn.execute("UPDATE users SET receptionist_on=? WHERE id=?",(new,uid))
    conn.commit(); conn.close()
    return jsonify({'receptionist_on': new})

# ── ADMIN ─────────────────────────────────────────────────────
@app.route('/admin')
@login_required
@admin_required
def admin_panel():
    conn    = get_db()
    clients = conn.execute("SELECT * FROM users WHERE role='client' ORDER BY created_at DESC").fetchall()
    all_inv = conn.execute("SELECT * FROM invoices").fetchall()
    outstanding = sum(i['amount'] for i in all_inv if i['status'] in ('pending','overdue'))
    conn.close()
    return render_template('admin.html', clients=clients,
                           total_clients=len(clients),
                           active_clients=sum(1 for c in clients if c['status']=='active'),
                           outstanding=outstanding)

@app.route('/admin/client/<int:cid>', methods=['GET','POST'])
@login_required
@admin_required
def admin_client(cid):
    conn = get_db()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'update':
            conn.execute("""UPDATE users SET name=?,email=?,business_name=?,
                appointment_value=?,subscription_price=?,hours_open=?,hours_close=?,status=?
                WHERE id=?""",
                (request.form['name'],request.form['email'],request.form['business_name'],
                 float(request.form.get('appointment_value',170)),
                 float(request.form.get('subscription_price',299)),
                 request.form.get('hours_open','08:00'),request.form.get('hours_close','18:00'),
                 request.form.get('status','active'),cid))
            conn.commit(); flash('Client updated.','success')
        elif action == 'reset_password':
            np = request.form.get('new_password','')
            if np:
                conn.execute("UPDATE users SET password=? WHERE id=?",(hash_pw(np),cid))
                conn.commit(); flash('Password updated.','success')
        elif action == 'delete':
            conn.execute("DELETE FROM users WHERE id=?",(cid,))
            conn.execute("DELETE FROM invoices WHERE user_id=?",(cid,))
            conn.execute("DELETE FROM calls WHERE user_id=?",(cid,))
            conn.commit(); conn.close()
            return redirect(url_for('admin_panel'))
    client   = conn.execute("SELECT * FROM users WHERE id=?",(cid,)).fetchone()
    invoices = conn.execute("SELECT * FROM invoices WHERE user_id=? ORDER BY created_at DESC",(cid,)).fetchall()
    conn.close()
    if not client: return redirect(url_for('admin_panel'))
    return render_template('admin_client.html', client=client, invoices=invoices)

@app.route('/admin/add', methods=['GET','POST'])
@login_required
@admin_required
def admin_add_client():
    error = None
    if request.method == 'POST':
        name=request.form.get('name','').strip()
        email=request.form.get('email','').strip().lower()
        pw=request.form.get('password','').strip()
        biz=request.form.get('business_name','').strip()
        try:
            conn=get_db()
            conn.execute("INSERT INTO users (name,email,password,role,business_name) VALUES (?,?,?,?,?)",
                         (name,email,hash_pw(pw),'client',biz))
            conn.commit(); conn.close()
            flash('Client added!','success')
            return redirect(url_for('admin_panel'))
        except sqlite3.IntegrityError:
            error='Email already exists.'
    return render_template('admin_add.html', error=error)

@app.route('/admin/invoice/<int:inv_id>/status', methods=['POST'])
@login_required
@admin_required
def update_invoice_status(inv_id):
    conn=get_db()
    conn.execute("UPDATE invoices SET status=? WHERE id=?",(request.form.get('status'),inv_id))
    conn.commit(); conn.close()
    return redirect(request.referrer or url_for('admin_panel'))

if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=8000)
