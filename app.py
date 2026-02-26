import os
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from database import db_manager
from bson.objectid import ObjectId

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev_key")
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'pdf'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

class User(UserMixin):
    def __init__(self, user_data):
        self.id = str(user_data['_id'])
        self.email = user_data.get('email')
        self.username = user_data.get('username')
        self.role = user_data.get('role', 'patient')
        # Profile fields
        self.full_name = user_data.get('full_name')
        self.phone = user_data.get('phone')
        self.dob = user_data.get('dob')
        self.address = user_data.get('address')
        self.gender = user_data.get('gender')

    @property
    def is_profile_complete(self):
        if self.role != 'patient':
            return True
        required_fields = [self.full_name, self.phone, self.dob, self.address, self.gender]
        return all(field and str(field).strip() for field in required_fields)

@login_manager.user_loader
def load_user(user_id):
    users_col = db_manager.get_collection('users')
    user_data = users_col.find_one({"_id": ObjectId(user_id)})
    if user_data:
        return User(user_data)
    return None

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        users_col = db_manager.get_collection('users')
        user_data = users_col.find_one({"email": email})
        
        if user_data and check_password_hash(user_data['password'], password):
            user = User(user_data)
            login_user(user)
            return redirect(url_for('dashboard'))
        
        flash('Invalid email or password')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        role = request.form.get('role', 'patient')
        
        users_col = db_manager.get_collection('users')
        if users_col.find_one({"email": email}):
            flash('Email already exists')
            return redirect(url_for('register'))
        
        hashed_password = generate_password_hash(password)
        users_col.insert_one({
            "username": username,
            "email": email,
            "password": hashed_password,
            "role": role
        })
        flash('Registration successful. Please login.')
        return redirect(url_for('login'))
        
    return render_template('register.html')

@app.route('/dashboard')
@login_required
def dashboard():
    bookings_col = db_manager.get_collection('bookings')
    
    if current_user.role == 'patient':
        # Dashboard only shows active/pending stuff if needed, but per request we're moving history
        return render_template('dashboard.html', user=current_user)
    else:
        # Technician view: split into New, Accepted, and Collected
        pending_bookings = list(bookings_col.find({"status": "pending"}))
        my_tasks = list(bookings_col.find({
            "status": {"$in": ["accepted", "collected"]},
            "tech_id": current_user.id
        }))
        return render_template('dashboard.html', user=current_user, pending=pending_bookings, tasks=my_tasks)

@app.route('/book-test')
@login_required
def book_test_page():
    if current_user.role != 'patient':
        return redirect(url_for('dashboard'))
    return render_template('book_test.html', user=current_user)

@app.route('/book', methods=['POST'])
@login_required
def book_test():
    if current_user.role != 'patient':
        return redirect(url_for('dashboard'))
    
    if not current_user.is_profile_complete:
        flash('Please complete your profile details before booking a lab technician.')
        return redirect(url_for('profile'))
        
    test_name = request.form.get('test_name')
    date = request.form.get('date')
    time = request.form.get('time')
    address = request.form.get('address')
    
    bookings_col = db_manager.get_collection('bookings')
    bookings_col.insert_one({
        "patient_id": current_user.id,
        "patient_name": request.form.get('full_name'),
        "patient_email": request.form.get('patient_email'),
        "patient_dob": request.form.get('dob'),
        "test_name": test_name,
        "date": date,
        "time": time,
        "address": address,
        "notes": request.form.get('notes'),
        "status": "pending"
    })
    flash('Lab test booked successfully!')
    return redirect(url_for('dashboard'))

@app.route('/upload_report', methods=['POST'])
@login_required
def upload_report():
    if current_user.role != 'technician':
        return redirect(url_for('dashboard'))
        
    booking_id = request.form.get('booking_id')
    patient_id = request.form.get('patient_id')
    description = request.form.get('description')
    
    # Handle PDF Upload
    if 'report_pdf' not in request.files:
        flash('No file part')
        return redirect(request.url)
    
    file = request.files['report_pdf']
    if file.filename == '':
        flash('No selected file')
        return redirect(request.url)
        
    if file and allowed_file(file.filename):
        filename = secure_filename(f"{booking_id}_{file.filename}")
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(file_path)
        
        reports_col = db_manager.get_collection('reports')
        reports_col.insert_one({
            "booking_id": booking_id,
            "patient_id": patient_id,
            "technician_id": current_user.id,
            "description": description,
            "pdf_url": filename,
            "timestamp": "2024-02-23" # Mock timestamp
        })
        
        bookings_col = db_manager.get_collection('bookings')
        bookings_col.update_one({"_id": ObjectId(booking_id)}, {"$set": {"status": "completed"}})
        
        flash('Medical report (PDF) uploaded successfully!')
        return redirect(url_for('dashboard'))
    
    flash('Invalid file type. Please upload a PDF.')
    return redirect(url_for('dashboard'))

@app.route('/download_report/<filename>')
@login_required
def download_report(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/accept_booking/<booking_id>')
@login_required
def accept_booking(booking_id):
    if current_user.role != 'technician':
        return redirect(url_for('dashboard'))
    
    bookings_col = db_manager.get_collection('bookings')
    bookings_col.update_one(
        {"_id": ObjectId(booking_id)}, 
        {"$set": {"status": "accepted", "tech_id": current_user.id}}
    )
    flash('Order accepted! Please proceed to collection.')
    return redirect(url_for('dashboard'))

@app.route('/collect_sample/<booking_id>')
@login_required
def collect_sample(booking_id):
    if current_user.role != 'technician':
        return redirect(url_for('dashboard'))
    
    bookings_col = db_manager.get_collection('bookings')
    bookings_col.update_one(
        {"_id": ObjectId(booking_id)}, 
        {"$set": {"status": "collected"}}
    )
    flash('Sample collected successfully! Proceed to lab testing.')
    return redirect(url_for('dashboard'))

@app.route('/history')
@login_required
def history():
    if current_user.role != 'patient':
        return redirect(url_for('dashboard'))
    
    bookings_col = db_manager.get_collection('bookings')
    reports_col = db_manager.get_collection('reports')
    
    my_bookings = list(bookings_col.find({"patient_id": current_user.id}).sort("_id", -1))
    my_reports = list(reports_col.find({"patient_id": current_user.id}).sort("_id", -1))
    
    return render_template('history.html', user=current_user, bookings=my_bookings, reports=my_reports)

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        users_col = db_manager.get_collection('users')
        users_col.update_one(
            {"_id": ObjectId(current_user.id)},
            {"$set": {
                "full_name": request.form.get('full_name'),
                "phone": request.form.get('phone'),
                "dob": request.form.get('dob'),
                "address": request.form.get('address'),
                "gender": request.form.get('gender')
            }}
        )
        flash('Profile updated successfully!')
        return redirect(url_for('dashboard'))
    return render_template('profile.html', user=current_user)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

if __name__ == '__main__':
    # Test DB connection on start
    if db_manager.test_connection():
        app.run(debug=True, port=int(os.getenv("PORT", 5000)))
    else:
        print("Could not start app: Database connection failed.")
