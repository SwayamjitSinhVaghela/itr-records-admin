import os, re, sqlite3
from datetime import datetime
import json
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify

app = Flask(__name__)
app.secret_key = os.urandom(24).hex()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.config['UPLOAD_FOLDER'] = os.path.join(BASE_DIR, 'uploads')
app.config['BACKUP_FOLDER'] = os.path.join(BASE_DIR, 'backups')
DB_PATH = os.path.join(BASE_DIR, 'records.db')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['BACKUP_FOLDER'], exist_ok=True)

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

DATABASE_URL = os.environ.get('DATABASE_URL', '')

class PgResult:
    def __init__(self, cur):
        self._cur = cur
    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self._cur.description]
        return dict(zip(cols, row))
    def fetchall(self):
        rows = self._cur.fetchall()
        if not rows:
            return []
        cols = [d[0] for d in self._cur.description]
        return [dict(zip(cols, r)) for r in rows]

class DB:
    def __init__(self):
        self.is_pg = bool(DATABASE_URL)
        if self.is_pg:
            from urllib.parse import urlparse
            import pg8000.dbapi
            u = urlparse(DATABASE_URL)
            self._conn = pg8000.dbapi.connect(
                host=u.hostname, port=u.port or 5432,
                database=u.path[1:], user=u.username, password=u.password
            )
        else:
            self._conn = sqlite3.connect(DB_PATH)
            self._conn.row_factory = sqlite3.Row

    def execute(self, sql, params=None):
        if self.is_pg:
            sql = sql.replace('?', '%s')
        if self.is_pg:
            cur = self._conn.cursor()
            cur.execute(sql, params or ())
            return PgResult(cur)
        else:
            if params:
                return self._conn.execute(sql, params)
            return self._conn.execute(sql)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def insert_id(self, sql, params=None):
        if self.is_pg:
            row = self.execute(sql + ' RETURNING id', params).fetchone()
            return row['id']
        else:
            cur = self.execute(sql, params)
            return cur.lastrowid

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.commit()
        self.close()

def get_db():
    return DB()

def init_db():
    with get_db() as db:
        if db.is_pg:
            db.execute('''
                CREATE TABLE IF NOT EXISTS records (
                    id SERIAL PRIMARY KEY,
                    pan TEXT,
                    name TEXT,
                    taxes_paid DOUBLE PRECISION DEFAULT 0,
                    refund_amount DOUBLE PRECISION DEFAULT 0,
                    mobile TEXT,
                    fee_amount DOUBLE PRECISION DEFAULT 0,
                    pdf_filename TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    paid INTEGER DEFAULT 0
                )
            ''')
        else:
            db.execute('''
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pan TEXT,
                    name TEXT,
                    taxes_paid REAL DEFAULT 0,
                    refund_amount REAL DEFAULT 0,
                    mobile TEXT,
                    fee_amount REAL DEFAULT 0,
                    pdf_filename TEXT,
                    created_at TEXT DEFAULT (datetime('now','localtime')),
                    paid INTEGER DEFAULT 0
                )
            ''')
            try:
                db.execute("ALTER TABLE records ADD COLUMN mobile TEXT")
            except sqlite3.OperationalError:
                pass
        # add paid column if missing (both pg and sqlite)
        try:
            db.execute("ALTER TABLE records ADD COLUMN IF NOT EXISTS paid INTEGER DEFAULT 0")
        except:
            try:
                db.execute("ALTER TABLE records ADD COLUMN paid INTEGER DEFAULT 0")
            except:
                pass
init_db()

@app.template_filter('inr')
def inr_format(value):
    if value is None or value == 0:
        return '0'
    negative = value < 0
    v = abs(value)
    s = f'{v:.2f}' if isinstance(v, float) and v != int(v) else str(int(v))
    parts = s.split('.')
    int_part = parts[0]
    dec_part = '.' + parts[1] if len(parts) > 1 else ''
    if len(int_part) <= 3:
        res = int_part
    else:
        res = int_part[-3:]
        rest = int_part[:-3]
        while rest:
            res = rest[-2:] + ',' + res
            rest = rest[:-2]
    return ('-' if negative else '') + '₹ ' + res + dec_part

@app.template_filter('wa_url')
def wa_url_filter(rec):
    from urllib.parse import quote
    try:
        mobile = str(rec['mobile'] or '').strip()
    except (KeyError, TypeError, AttributeError):
        mobile = ''
    if len(mobile) < 6:
        return ''
    try:
        amount = abs(float(rec['refund_amount'] or 0))
    except (KeyError, TypeError, AttributeError):
        amount = 0
    msg = f'आपके IT रिटर्न की राशि ₹{amount:,.0f} बकाया है। कृपया भुगतान करें।'
    return f'https://wa.me/91{mobile}?text={quote(msg)}'

def _clean_name(raw):
    raw = raw.strip().rstrip(',;.:')
    raw = re.sub(r'^(Name|Assessee|Assesse|Taxpayer)\s*(of\s*(the\s*)?)?\s*', '', raw, flags=re.IGNORECASE).strip()
    raw = re.sub(r'^(Shri|Smt|Mr|Ms|Mrs|M/s)\.?\s+', '', raw, flags=re.IGNORECASE).strip()
    return raw

def extract_pdf_data(pdf_path):
    data = {'pan': '', 'name': '', 'taxes_paid': 0.0, 'refund_amount': 0.0}
    if not pdfplumber:
        return data
    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = ''
            for page in pdf.pages:
                pt = page.extract_text()
                if pt:
                    text += pt + '\n'

            m = re.search(r'[A-Z]{5}[0-9]{4}[A-Z]', text)
            if m:
                data['pan'] = m.group(0)

            name = ''
            patterns = [
                r'Name\s*(?:of\s+(?:the\s+)?(?:Assessee|Taxpayer|Assesse))?\s*[:\-–—]?\s*(.+?)(?:\n|$)',
                r'Assessee\s*Name\s*[:\-–—]?\s*(.+?)(?:\n|$)',
                r'Taxpayer\s*Name\s*[:\-–—]?\s*(.+?)(?:\n|$)',
                r'Name\s*[:\-–—]\s*(.+?)(?:\n|$)',
                r'Name\s+(?!of|the)([A-Z].+?)(?:\n|$)',
            ]
            for pat in patterns:
                m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
                if m:
                    c = _clean_name(m.group(1))
                    if len(c) > 2 and not re.match(r'^[\d\s\W]+$', c):
                        name = c
                        break

            if not name and data['pan']:
                lines = text.split('\n')
                for idx, line in enumerate(lines):
                    if data['pan'] in line and idx + 1 < len(lines):
                        candidate = _clean_name(lines[idx + 1])
                        if candidate and len(candidate) > 2 and not re.match(r'^[\d\s\W]+$', candidate):
                            name = candidate
                            break

            if not name:
                for line in text.split('\n'):
                    line = _clean_name(line)
                    if re.match(r'^[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+$', line) and len(line) > 5:
                        name = line
                        break
            data['name'] = name

            lines = text.split('\n')
            for line in lines:
                line = line.strip()
                m = re.search(r'(\d{1,2})\s+(\(?\s*-?\s*\)?)?\s*([\d,]+(?:\.\d{2})?)\s*$', line)
                if m:
                    row_num = int(m.group(1))
                    sign_raw = (m.group(2) or '').strip()
                    val_str = m.group(3)
                    val = float(val_str.replace(',', ''))
                    if sign_raw in ('-', '(-)', '(-', '-)') or '(' in sign_raw:
                        val = -val
                    if row_num == 7:
                        data['taxes_paid'] = val
                    elif row_num == 8:
                        data['refund_amount'] = val
    except Exception as e:
        print(f"PDF parse error: {e}")
    return data

def _row_to_dict(r):
    if isinstance(r, dict):
        return dict(r)
    return {k: v for k, v in zip(r.keys(), r)}

@app.route('/')
def index():
    q = request.args.get('q', '').strip()
    with get_db() as db:
        if q:
            like = f'%{q}%'
            records = db.execute(
                'SELECT * FROM records WHERE pan LIKE ? OR name LIKE ? OR mobile LIKE ? ORDER BY created_at DESC',
                (like, like, like)
            ).fetchall()
        else:
            records = db.execute('SELECT * FROM records ORDER BY created_at DESC').fetchall()
    enriched = []
    for r in records:
        rec = _row_to_dict(r)
        rec['paid'] = int(rec['paid']) if rec.get('paid') is not None else 0
        enriched.append(rec)
    return render_template('index.html', records=enriched, q=q)

@app.route('/upload', methods=['GET', 'POST'])
def upload():
    new_rec = None
    if request.method == 'POST':
        file = request.files.get('pdf')
        if not file or file.filename == '':
            flash('No file selected.', 'error')
            return redirect(url_for('upload'))
        if not file.filename.lower().endswith('.pdf'):
            flash('Only PDF files allowed.', 'error')
            return redirect(url_for('upload'))
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"{ts}_{file.filename}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        extracted = extract_pdf_data(filepath)

        with get_db() as db:
            new_id = db.insert_id(
                'INSERT INTO records (pan, name, taxes_paid, refund_amount, pdf_filename) VALUES (?,?,?,?,?)',
                (extracted['pan'], extracted['name'], extracted['taxes_paid'], extracted['refund_amount'], filename)
            )
            new_rec = db.execute('SELECT * FROM records WHERE id=?', (new_id,)).fetchone()

    return render_template('upload.html', new_rec=new_rec)

@app.route('/finalize/<int:rid>', methods=['POST'])
def finalize(rid):
    mobile = request.form.get('mobile', '').strip()
    fee_amount = float(request.form.get('fee_amount', '0') or 0)
    with get_db() as db:
        db.execute('UPDATE records SET mobile=?, fee_amount=? WHERE id=?', (mobile, fee_amount, rid))
        rec = db.execute('SELECT pdf_filename FROM records WHERE id=?', (rid,)).fetchone()
    if rec and rec['pdf_filename']:
        p = os.path.join(app.config['UPLOAD_FOLDER'], rec['pdf_filename'])
        if os.path.exists(p):
            os.remove(p)
    flash('Record saved!', 'success')
    return redirect(url_for('index'))

@app.route('/add', methods=['GET', 'POST'])
def add():
    if request.method == 'POST':
        pan = request.form.get('pan','').strip().upper()
        name = request.form.get('name','').strip()
        taxes_paid = float(request.form.get('taxes_paid','0') or 0)
        refund_amount = float(request.form.get('refund_amount','0') or 0)
        mobile = request.form.get('mobile','').strip()
        fee_amount = float(request.form.get('fee_amount','0') or 0)
        with get_db() as db:
            db.execute(
                'INSERT INTO records (pan,name,taxes_paid,refund_amount,mobile,fee_amount) VALUES (?,?,?,?,?,?)',
                (pan, name, taxes_paid, refund_amount, mobile, fee_amount)
            )
        flash('Record added.', 'success')
        return redirect(url_for('index'))
    return render_template('add_record.html')

@app.route('/edit/<int:rid>', methods=['GET','POST'])
def edit(rid):
    with get_db() as db:
        r = db.execute('SELECT * FROM records WHERE id=?', (rid,)).fetchone()
        if not r:
            flash('Not found.', 'error')
            return redirect(url_for('index'))
        if request.method == 'POST':
            db.execute(
                'UPDATE records SET pan=?,name=?,taxes_paid=?,refund_amount=?,mobile=?,fee_amount=? WHERE id=?',
                (request.form['pan'].strip().upper(), request.form['name'].strip(),
                 float(request.form['taxes_paid'] or 0), float(request.form['refund_amount'] or 0),
                 request.form['mobile'].strip(), float(request.form['fee_amount'] or 0), rid)
            )
            flash('Updated.', 'success')
            return redirect(url_for('index'))
    return render_template('edit_record.html', record=r)

@app.route('/toggle-status/<int:rid>', methods=['POST'])
def toggle_status(rid):
    with get_db() as db:
        r = db.execute('SELECT paid FROM records WHERE id=?', (rid,)).fetchone()
        if not r:
            flash('Not found.', 'error')
            return redirect(url_for('index'))
        current = int(r['paid'] or 0)
        new_val = 0 if current else 1
        db.execute('UPDATE records SET paid=? WHERE id=?', (new_val, rid))
    flash('Status updated.', 'success')
    return redirect(url_for('index'))

@app.route('/delete/<int:rid>')
def delete(rid):
    with get_db() as db:
        r = db.execute('SELECT pdf_filename FROM records WHERE id=?', (rid,)).fetchone()
        if r and r['pdf_filename']:
            p = os.path.join(app.config['UPLOAD_FOLDER'], r['pdf_filename'])
            if os.path.exists(p):
                os.remove(p)
        db.execute('DELETE FROM records WHERE id=?', (rid,))
    flash('Deleted.', 'success')
    return redirect(url_for('index'))

@app.route('/export')
def export_csv():
    import csv, io
    with get_db() as db:
        records = db.execute('SELECT * FROM records ORDER BY created_at DESC').fetchall()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['ID','PAN','Name','Taxes Paid','Refund','Mobile','Fee Amount','Source','Date','Status'])
    labels = {0:'Unpaid',1:'Paid',2:'Partial'}
    for r in records:
        src = 'PDF' if r['pdf_filename'] else 'Manual'
        w.writerow([r['id'], r['pan'], r['name'], r['taxes_paid'], r['refund_amount'], r['mobile'], r['fee_amount'], src, r['created_at'], labels.get(r['paid'],'Unpaid')])
    from flask import Response
    return Response(out.getvalue(), mimetype='text/csv', headers={'Content-Disposition':'attachment;filename=itr_records.csv'})

@app.route('/backup')
def backup_db():
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = os.path.join(app.config['BACKUP_FOLDER'], f'records_{ts}.db')
    import shutil
    shutil.copy2(DB_PATH, backup_path)
    flash(f'Database backed up to backups/records_{ts}.db', 'success')
    return redirect(url_for('index'))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
