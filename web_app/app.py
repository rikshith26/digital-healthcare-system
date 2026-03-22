import os
import smtplib
from email.message import EmailMessage
import threading
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

def send_system_email(subject, body_text, recipients, body_html=None):
    sender = os.getenv("MAIL_USERNAME")
    password = os.getenv("MAIL_PASSWORD")
    if not sender or not password:
        print("Error: Mail credentials missing from .env")
        return False
        
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = f"HealthLab AI <{sender}>"
    msg['To'] = ", ".join(recipients) if isinstance(recipients, list) else recipients
    
    # Set plain text as a fallback
    msg.set_content(body_text)
    
    # Attach modern HTML formatting if provided
    if body_html:
        msg.add_alternative(body_html, subtype='html')
    
    def async_send():
        try:
            with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
                smtp.login(sender, password)
                smtp.send_message(msg)
            print(f"Success: Email sent to {msg['To']}")
        except Exception as e:
            print(f"Failed to send email: {e}")
            
    threading.Thread(target=async_send).start()
    return True

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
        self.status = user_data.get('status', 'Approved')
        self.reject_reason = user_data.get('reject_reason', '')
        self.verification_docs = user_data.get('verification_docs', [])

    @property
    def is_profile_complete(self):
        if self.role != 'patient':
            return True
        required_fields = [self.full_name, self.phone, self.dob, self.address, self.gender]
        return all(field and str(field).strip() for field in required_fields)

@login_manager.user_loader
def load_user(user_id):
    # Try Admins collection first
    admins_col = db_manager.get_collection('admins')
    admin_data = admins_col.find_one({"_id": ObjectId(user_id)})
    if admin_data:
        return User(admin_data)
        
    # Then fall back to general Users collection
    users_col = db_manager.get_collection('users')
    user_data = users_col.find_one({"_id": ObjectId(user_id)})
    if user_data:
        return User(user_data)
    return None

@app.before_request
def check_verification():
    if current_user.is_authenticated and current_user.role != 'admin':
        if getattr(current_user, 'status', 'Approved') in ['Pending Verification', 'Under Review', 'Rejected'] and current_user.role != 'patient':
            allowed_endpoints = ['verify_account', 'logout', 'static']
            if request.endpoint and request.endpoint not in allowed_endpoints:
                return redirect(url_for('verify_account'))

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    chat_redirect = request.args.get('chat_redirect')
    consult_redirect = request.args.get('consult_redirect')
    if chat_redirect:
        flash('Please login to our site so that you can access the AI Health Assistant.')
    if consult_redirect:
        flash('Please login to our site to consult with our specialized doctors.')

    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        next_url = request.args.get('next')
        
        # Search Admins collection first, then Users
        admins_col = db_manager.get_collection('admins')
        user_data = admins_col.find_one({"email": email})
        
        if not user_data:
            users_col = db_manager.get_collection('users')
            user_data = users_col.find_one({"email": email})
        
        if user_data and check_password_hash(user_data['password'], password):
            user = User(user_data)
            login_user(user)
            if next_url:
                # If we were going to the dashboard or index, add the open_chat/open_consult param
                sep = '?' if '?' not in next_url else '&'
                if consult_redirect:
                    return redirect(next_url + sep + "open_consult=true")
                if chat_redirect:
                    return redirect(next_url + sep + "open_chat=true")
                return redirect(next_url)
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
        
        admins_col = db_manager.get_collection('admins')
        users_col = db_manager.get_collection('users')
        if users_col.find_one({"email": email}) or admins_col.find_one({"email": email}):
            flash('Email already exists')
            return redirect(url_for('register'))
        
        hashed_password = generate_password_hash(password)
        
        status = 'Approved' if role == 'patient' else 'Pending Verification'
        users_col.insert_one({
            "username": username,
            "email": email,
            "password": hashed_password,
            "role": role,
            "status": status
        })
        flash('Registration successful. Please login.')
        return redirect(url_for('login'))
        
    return render_template('register.html')

@app.route('/dashboard')
@login_required
def dashboard():
    bookings_col = db_manager.get_collection('bookings')
    
    if current_user.role == 'patient':
        return render_template('dashboard.html', user=current_user)
    elif current_user.role == 'admin':
        # Admin Dashboard View
        users_col = db_manager.get_collection('users')
        admins_col = db_manager.get_collection('admins')
        
        all_regular_users = list(users_col.find())
        all_admins = list(admins_col.find())
        all_users = all_regular_users + all_admins
        
        all_bookings = list(bookings_col.find().sort("_id", -1))
        
        # Calculate some stats
        total_patients = sum(1 for u in all_users if u.get('role') == 'patient')
        total_techs = sum(1 for u in all_users if u.get('role') == 'technician')
        total_doctors = sum(1 for u in all_users if u.get('role') == 'doctor')
        total_admins = len(all_admins)
        
        # Mock Revenue / Active tests
        active_tests = sum(1 for b in all_bookings if b.get('status') in ['pending', 'accepted'])
        
        stats = {
            'total_users': len(all_users),
            'total_patients': total_patients,
            'total_techs': total_techs,
            'total_doctors': total_doctors,
            'total_admins': total_admins,
            'active_tests': active_tests,
            'total_bookings': len(all_bookings)
        }
        
        return render_template('admin_dashboard.html', user=current_user, users=all_users, bookings=all_bookings, stats=stats)
    else:
        # Technician/Doctor view
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
    
    bookings_col = db_manager.get_collection('bookings')
    my_bookings = list(bookings_col.find({"patient_id": current_user.id}).sort("_id", -1))
    
    return render_template('book_test.html', user=current_user, bookings=my_bookings)

@app.route('/user-data')
@login_required
def user_data_page():
    if current_user.role != 'admin':
        return redirect(url_for('dashboard'))
        
    users_col = db_manager.get_collection('users')
    admins_col = db_manager.get_collection('admins')
    
    all_regular_users = list(users_col.find())
    all_admins = list(admins_col.find())
    all_users = all_regular_users + all_admins
    
    return render_template('user_data.html', user=current_user, users=all_users)

@app.route('/verify-account', methods=['GET', 'POST'])
@login_required
def verify_account():
    if current_user.role in ['patient', 'admin'] or current_user.status == 'Approved':
        return redirect(url_for('dashboard'))
        
    if request.method == 'POST':
        users_col = db_manager.get_collection('users')
        docs_uploaded = []
        
        # Save uploaded files
        upload_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'verifications')
        os.makedirs(upload_dir, exist_ok=True)
            
        for key in request.files:
            file = request.files[key]
            if file and file.filename != '':
                filename = secure_filename(f"{current_user.id}_{key}_{file.filename}")
                file.save(os.path.join(upload_dir, filename))
                docs_uploaded.append({"document_type": key, "filename": filename})
                
        # Update user with form data and status
        form_data = dict(request.form)
        users_col.update_one(
            {"_id": ObjectId(current_user.id)},
            {"$set": {
                "status": "Under Review", 
                "verification_details": form_data,
                "verification_docs": current_user.verification_docs + docs_uploaded
            }}
        )
        flash('Documents submitted successfully. Approval may take 24-48 hours.')
        return redirect(url_for('verify_account'))
        
    return render_template('verify_account.html', user=current_user)

@app.route('/admin/verifications')
@login_required
def admin_verifications():
    if current_user.role != 'admin':
        return redirect(url_for('dashboard'))
        
    users_col = db_manager.get_collection('users')
    pending_users = list(users_col.find({"status": {"$in": ["Pending Verification", "Under Review", "Rejected"]}, "role": {"$ne": "patient"}}))
    return render_template('admin_verifications.html', user=current_user, pending_users=pending_users)

@app.route('/admin/verify/<user_id>', methods=['POST'])
@login_required
def process_verification(user_id):
    if current_user.role != 'admin': return "Unauthorized", 401
    action = request.form.get('action')
    reason = request.form.get('reason', '')
    users_col = db_manager.get_collection('users')
    
    if action == 'approve':
        users_col.update_one({"_id": ObjectId(user_id)}, {"$set": {"status": "Approved"}})
        flash("Professional account approved and activated.")
    elif action == 'reject':
        users_col.update_one({"_id": ObjectId(user_id)}, {"$set": {"status": "Rejected", "reject_reason": reason}})
        flash("Professional account application rejected.")
        
    return redirect(url_for('admin_verifications'))

@app.route('/uploads/verifications/<filename>')
@login_required
def uploaded_verification_file(filename):
    if current_user.role != 'admin': return "Unauthorized", 401
    return send_from_directory(os.path.join(app.config['UPLOAD_FOLDER'], 'verifications'), filename)

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

@app.route('/booking-history')
@login_required
def booking_history():
    if current_user.role != 'patient':
        return redirect(url_for('dashboard'))
    
    bookings_col = db_manager.get_collection('bookings')
    my_bookings = list(bookings_col.find({"patient_id": current_user.id}).sort("_id", -1))
    
    return render_template('booking_history.html', user=current_user, bookings=my_bookings)

@app.route('/clinical-reports')
@login_required
def clinical_reports():
    if current_user.role != 'patient':
        return redirect(url_for('dashboard'))
    
    reports_col = db_manager.get_collection('reports')
    my_reports = list(reports_col.find({"patient_id": current_user.id}).sort("_id", -1))
    
    return render_template('clinical_reports.html', user=current_user, reports=my_reports)


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
    success, hint = db_manager.test_connection()
    if success:
        app.run(debug=True, port=int(os.getenv("PORT", 5000)))
    else:
        if hint:
            print(hint)
        print("Could not start app: Database connection failed.")
