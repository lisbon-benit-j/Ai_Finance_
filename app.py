from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from datetime import datetime, timedelta
import sqlite3
import google.generativeai as genai
import json
import traceback

app = Flask(__name__)
app.secret_key = 'your_secret_key_here'

# SQLite database setup
DATABASE = 'finance_tracker.db'

def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL UNIQUE,
                phone TEXT NOT NULL,
                password TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                date TEXT NOT NULL,
                description TEXT,
                payment_method TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS spending_limits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                monthly_limit REAL NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS budget_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                monthly_limit REAL NOT NULL,
                UNIQUE(user_id, category),
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            )
        ''')
        conn.commit()

init_db()

# ---------------- Helper Functions ---------------- #
def get_current_month_range():
    today = datetime.now()
    first_day = today.replace(day=1)
    last_day = (first_day + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return first_day.strftime('%Y-%m-%d'), last_day.strftime('%Y-%m-%d')

def get_monthly_summary(user_id):
    first_day, last_day = get_current_month_range()
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute('''SELECT COALESCE(SUM(amount),0) as total FROM transactions 
                     WHERE user_id=? AND date BETWEEN ? AND ?''', (user_id, first_day, last_day))
        total_spent = c.fetchone()['total']

        c.execute('''SELECT category, SUM(amount) as amount FROM transactions
                     WHERE user_id=? AND date BETWEEN ? AND ? GROUP BY category''', (user_id, first_day, last_day))
        spending_by_category = {row['category']: row['amount'] for row in c.fetchall()}

        c.execute('''SELECT payment_method, SUM(amount) as amount FROM transactions
                     WHERE user_id=? AND date BETWEEN ? AND ? GROUP BY payment_method''', (user_id, first_day, last_day))
        spending_by_method = {row['payment_method']: row['amount'] for row in c.fetchall()}

        c.execute('SELECT monthly_limit FROM spending_limits WHERE user_id=?', (user_id,))
        limit_row = c.fetchone()
        monthly_limit = limit_row['monthly_limit'] if limit_row else 0

        c.execute('SELECT category, monthly_limit FROM budget_categories WHERE user_id=?', (user_id,))
        category_limits = {row['category']: row['monthly_limit'] for row in c.fetchall()}

        category_usage = {}
        for category, limit in category_limits.items():
            spent = spending_by_category.get(category, 0)
            category_usage[category] = {
                'spent': spent,
                'limit': limit,
                'remaining': max(limit - spent, 0),
                'percentage': min((spent / limit) * 100, 100) if limit > 0 else 0
            }

        return {
            'total_spent': total_spent,
            'monthly_limit': monthly_limit,
            'remaining': max(monthly_limit - total_spent, 0) if monthly_limit > 0 else 0,
            'spending_percentage': min((total_spent / monthly_limit) * 100, 100) if monthly_limit > 0 else 0,
            'spending_by_category': spending_by_category,
            'spending_by_method': spending_by_method,
            'category_limits': category_limits,
            'category_usage': category_usage
        }

# ---------------- Routes ---------------- #
@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user_id = session['user_id']
    monthly_summary = get_monthly_summary(user_id)
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute('''SELECT * FROM transactions WHERE user_id=? ORDER BY date DESC, created_at DESC LIMIT 5''', (user_id,))
        recent_transactions = c.fetchall()
    return render_template('index.html', username=session['username'],
                           monthly_summary=monthly_summary,
                           recent_transactions=recent_transactions)

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method=='POST':
        username = request.form['username']
        password = request.form['password']
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute('SELECT id, username FROM users WHERE username=? AND password=?', (username, password))
            user = c.fetchone()
        if user:
            session['user_id'] = user['id']
            session['username'] = user['username']
            flash('Login successful!','success')
            return redirect(url_for('index'))
        else:
            flash('Invalid username or password','danger')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out','info')
    return redirect(url_for('login'))

@app.route('/register', methods=['GET','POST'])
def register():
    if request.method=='POST':
        username = request.form['username']
        email = request.form['email']
        phone = request.form['phone']
        password = request.form['password']
        try:
            with get_db_connection() as conn:
                c = conn.cursor()
                c.execute('INSERT INTO users (username,email,phone,password) VALUES (?,?,?,?)',
                          (username,email,phone,password))
                conn.commit()
            flash('Registration successful! Please log in.','success')
            return redirect(url_for('login'))
        except:
            flash('Username or email already exists','danger')
    return render_template('register.html')

@app.route('/transactions')
def transactions():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user_id = session['user_id']
    
    # Get filter parameters
    category_filter = request.args.get('category')
    payment_filter = request.args.get('payment_method')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    
    # Base query
    query = '''
        SELECT * FROM transactions 
        WHERE user_id = ?
    '''
    params = [user_id]
    
    # Apply filters
    if category_filter:
        query += ' AND category = ?'
        params.append(category_filter)
    if payment_filter:
        query += ' AND payment_method = ?'
        params.append(payment_filter)
    if start_date:
        query += ' AND date >= ?'
        params.append(start_date)
    if end_date:
        query += ' AND date <= ?'
        params.append(end_date)
    
    query += ' ORDER BY date DESC, created_at DESC'
    
    with get_db_connection() as conn:
        c = conn.cursor()
        
        # Get transactions
        c.execute(query, params)
        transactions = c.fetchall()
        
        # Get distinct categories and payment methods for filters
        c.execute('SELECT DISTINCT category FROM transactions WHERE user_id = ? ORDER BY category', (user_id,))
        categories = [row['category'] for row in c.fetchall()]
        
        c.execute('SELECT DISTINCT payment_method FROM transactions WHERE user_id = ? ORDER BY payment_method', (user_id,))
        payment_methods = [row['payment_method'] for row in c.fetchall()]
    
    return render_template(
        'transactions.html',
        transactions=transactions,
        categories=categories,
        payment_methods=payment_methods,
        current_filters={
            'category': category_filter,
            'payment_method': payment_filter,
            'start_date': start_date,
            'end_date': end_date
        }
    )

# ---------------- Financial Chat ---------------- #
@app.route('/financial_chat', methods=['GET','POST'])
def financial_chat():
    if 'user_id' not in session:
        # Check if the request is expecting JSON (AJAX) or HTML (direct page load)
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.accept_mimetypes.best == 'application/json':
            return jsonify({'error':'Not authenticated'}), 401
        else:
            return redirect(url_for('login'))

    if request.method == 'POST':
        try:
            user_message = request.form['message']

            # Simple chatbot prompt without financial analysis
            prompt = f"""
            You are a helpful financial assistant. Answer the user's question directly and concisely.
            User's question: {user_message}
            """

            model = genai.GenerativeModel('gemini-2.5-flash')
            response = model.generate_content(prompt)
            chat_response = response.text
            return jsonify({'response': chat_response})
        except Exception as e:
            print(f"Error in financial_chat: {e}")
            traceback.print_exc()
            return jsonify({'error': 'An error occurred. Please try again later.'}), 500

    return render_template('financial_chat.html')

# ---------------- Budget Routes ---------------- #
@app.route('/budget', methods=['GET', 'POST'])
def budget():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user_id = session['user_id']
    
    if request.method == 'POST':
        # Monthly limit
        if 'monthly_limit' in request.form:
            monthly_limit = float(request.form['monthly_limit'])
            with get_db_connection() as conn:
                c = conn.cursor()
                c.execute('''
                    INSERT OR REPLACE INTO spending_limits 
                    (user_id, monthly_limit) 
                    VALUES (?, ?)
                ''', (user_id, monthly_limit))
                conn.commit()
            flash('Monthly spending limit updated!', 'success')
        
        # Category budgets
        elif 'category' in request.form:
            category = request.form['category']
            monthly_limit = float(request.form['category_limit'])
            with get_db_connection() as conn:
                c = conn.cursor()
                c.execute('''
                    INSERT OR REPLACE INTO budget_categories 
                    (user_id, category, monthly_limit) 
                    VALUES (?, ?, ?)
                ''', (user_id, category, monthly_limit))
                conn.commit()
            flash(f'Budget for {category} updated!', 'success')
    
    # Get current budgets
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT monthly_limit FROM spending_limits WHERE user_id = ?', (user_id,))
        monthly_limit_row = c.fetchone()
        monthly_limit = monthly_limit_row['monthly_limit'] if monthly_limit_row else 0

        c.execute('SELECT category, monthly_limit FROM budget_categories WHERE user_id = ? ORDER BY category', (user_id,))
        category_budgets = c.fetchall()

        c.execute('SELECT DISTINCT category FROM transactions WHERE user_id = ? ORDER BY category', (user_id,))
        existing_categories = [row['category'] for row in c.fetchall()]

    monthly_summary = get_monthly_summary(user_id)

    return render_template(
        'budget.html',
        monthly_limit=monthly_limit,
        category_budgets=category_budgets,
        existing_categories=existing_categories,
        monthly_summary=monthly_summary
    )

@app.route('/delete_category_budget/<string:category>', methods=['POST'])
def delete_category_budget(category):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    user_id = session['user_id']
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM budget_categories WHERE user_id = ? AND category = ?', (user_id, category))
        conn.commit()
    flash(f'Budget for {category} deleted', 'success')
    return redirect(url_for('budget'))

# ---------------- Transaction Routes ---------------- #
@app.route('/add_transaction', methods=['GET', 'POST'])
def add_transaction():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        user_id = session['user_id']
        amount = float(request.form['amount'])
        category = request.form['category']
        date = request.form['date']
        payment_method = request.form['payment_method']
        description = request.form.get('description', '')
        
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute('''
                INSERT INTO transactions 
                (user_id, amount, category, date, payment_method, description)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (user_id, amount, category, date, payment_method, description))
            conn.commit()
        
        flash('Transaction added successfully!', 'success')
        return redirect(url_for('transactions'))
    
    return render_template('add_transaction.html', default_date=datetime.now().strftime('%Y-%m-%d'))

@app.route('/edit_transaction/<int:transaction_id>', methods=['GET', 'POST'])
def edit_transaction(transaction_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user_id = session['user_id']
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute('SELECT * FROM transactions WHERE id = ? AND user_id = ?', (transaction_id, user_id))
        transaction = c.fetchone()
        if not transaction:
            flash('Transaction not found', 'danger')
            return redirect(url_for('transactions'))
        
        if request.method == 'POST':
            amount = float(request.form['amount'])
            category = request.form['category']
            date = request.form['date']
            payment_method = request.form['payment_method']
            description = request.form.get('description', '')
            
            c.execute('''
                UPDATE transactions SET amount=?, category=?, date=?, payment_method=?, description=? 
                WHERE id=?
            ''', (amount, category, date, payment_method, description, transaction_id))
            conn.commit()
            
            flash('Transaction updated successfully!', 'success')
            return redirect(url_for('transactions'))
    
    return render_template('edit_transaction.html', transaction=transaction)

@app.route('/delete_transaction/<int:transaction_id>', methods=['POST'])
def delete_transaction(transaction_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user_id = session['user_id']
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM transactions WHERE id=? AND user_id=?', (transaction_id, user_id))
        conn.commit()
    
    flash('Transaction deleted successfully', 'success')
    return redirect(url_for('transactions'))

# ---------------- Reports ---------------- #
@app.route('/reports')
def reports():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user_id = session['user_id']
    year = request.args.get('year', str(datetime.now().year))
    
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute('''
            SELECT strftime('%m', date) as month, SUM(amount) as total 
            FROM transactions WHERE user_id=? AND strftime('%Y', date)=? 
            GROUP BY month ORDER BY month
        ''', (user_id, year))
        monthly_spending = c.fetchall()
        
        c.execute('''
            SELECT category, SUM(amount) as total 
            FROM transactions WHERE user_id=? AND strftime('%Y', date)=?
            GROUP BY category ORDER BY total DESC
        ''', (user_id, year))
        category_spending = c.fetchall()
        
        c.execute('SELECT DISTINCT strftime("%Y", date) as year FROM transactions WHERE user_id=? ORDER BY year DESC', (user_id,))
        available_years = [row['year'] for row in c.fetchall()]
        if year not in available_years:
            available_years.append(year)
        available_years.sort(reverse=True)


    months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
    monthly_totals = [0]*12
    for row in monthly_spending:
        if row['month'] and row['total'] is not None:
            try:
                idx = int(row['month'])-1
                monthly_totals[idx] = row['total']
            except Exception:
                pass

    category_labels = [row['category'] for row in category_spending]
    category_data = [row['total'] for row in category_spending]

    zipped_categories = list(zip(category_labels, category_data))
    return render_template('reports.html',
                           months=months,
                           monthly_totals=monthly_totals,
                           category_labels=category_labels,
                           category_data=category_data,
                           zipped_categories=zipped_categories,
                           available_years=available_years,
                           selected_year=year)

# ---------------- Statistics ---------------- #
@app.route('/statistics')
def statistics():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user_id = session['user_id']
    monthly_summary = get_monthly_summary(user_id)
    
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute('''
            SELECT strftime('%Y-%m', date) as month_year, SUM(amount) as total 
            FROM transactions WHERE user_id=? 
            GROUP BY month_year ORDER BY month_year DESC LIMIT 6
        ''', (user_id,))
        spending_trend = c.fetchall()
        
        c.execute('SELECT category, SUM(amount) as total FROM transactions WHERE user_id=? GROUP BY category ORDER BY total DESC LIMIT 5', (user_id,))
        top_categories = c.fetchall()
        
        c.execute('SELECT AVG(monthly_total) FROM (SELECT SUM(amount) as monthly_total FROM transactions WHERE user_id=? GROUP BY strftime("%Y-%m", date))', (user_id,))
        avg_monthly_spending = c.fetchone()[0] or 0

    return render_template('statistics.html',
                           monthly_summary=monthly_summary,
                           spending_trend=spending_trend,
                           top_categories=top_categories,
                           avg_monthly_spending=avg_monthly_spending)

# ---------------- Financial Analysis ---------------- #
@app.route('/financial_analysis')
def financial_analysis():
    if 'user_id' not in session:
        # Check if the request is expecting JSON (AJAX) or HTML (direct page load)
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.accept_mimetypes.best == 'application/json':
            return jsonify({'error': 'Not authenticated'}), 401
        else:
            return redirect(url_for('login'))
    
    try:
        user_id = session['user_id']
        monthly_summary = get_monthly_summary(user_id)
        
        model = genai.GenerativeModel('gemini-2.5-flash')
        prompt = f"""
        Provide a concise financial analysis based on the following data, suitable for visualization. Present the analysis in clear, well-aligned sentences, avoiding markdown symbols like asterisks (*) or hyphens (---). Highlight key insights, trends, and actionable advice.
        Total Spent: {monthly_summary['total_spent']:.2f}
        Monthly Limit: {monthly_summary['monthly_limit']:.2f}
        Remaining Budget: {monthly_summary['remaining']:.2f}
        Spending Percentage: {monthly_summary['spending_percentage']:.2f}%
        Spending by Category: {json.dumps(monthly_summary['spending_by_category'], indent=2)}
        Spending by Payment Method: {json.dumps(monthly_summary['spending_by_method'], indent=2)}
        Category Usage: {json.dumps(monthly_summary['category_usage'], indent=2)}
        """
        response = model.generate_content(prompt)
        analysis_text = response.text
        
        # If AJAX/JSON request, return JSON
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.accept_mimetypes.best == 'application/json':
            return jsonify({'analysis': analysis_text})
        # Otherwise, render the template
        return render_template('financial_analysis.html', analysis=analysis_text)

    except Exception as e:
        print(f"Error in financial_analysis: {e}")
        traceback.print_exc()
        return jsonify({'error': 'An error occurred. Please try again later.'}), 500

# ---------------- Configure Gemini ---------------- #
API_KEY = "AIzaSyAMi0nq0Gcd2ROQ4v25lWJ-h00G4w9JCQU"
genai.configure(api_key=API_KEY)

# This is a dummy comment to force Flask to reload.
if __name__=='__main__':
    app.run(debug=True)
