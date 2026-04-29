import os
import smtplib
import certifi
import threading
from email.message import EmailMessage
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from pymongo import MongoClient
from bson.objectid import ObjectId
from dotenv import load_dotenv

# Load Environment Variables
load_dotenv()

# --- DATABASE MANAGEMENT (Consolidated) ---
class Database:
    def __init__(self):
        self.uri = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
        
        # Default to 'digital_healthcare' as that is where existing users are stored
        db_name = 'digital_healthcare'
        try:
            # Check if there's a specific database in the URI that isn't empty
            parsed_path = self.uri.split('://')[1].split('/')
            if len(parsed_path) > 1:
                potential_db = parsed_path[1].split('?')[0]
                if potential_db and potential_db != 'mediscan_db': 
                    db_name = potential_db
        except:
            pass

        self.client = MongoClient(
            self.uri, 
            tlsCAFile=certifi.where(),
            connect=False,
            tlsAllowInvalidCertificates=True, 
            tlsAllowInvalidHostnames=True,
            serverSelectionTimeoutMS=5000     
        )
        self.db = self.client[db_name]

    def get_collection(self, name):
        return self.db[name]

    def test_connection(self):
        print("Testing MongoDB Atlas Connection...")
        try:
            self.client.admin.command('ping')
            print("Database connected successfully to Atlas!")
            return True, None
        except Exception as e:
            error_msg = str(e)
            print(f"Atlas Connection Attempt Failed: {error_msg}")
            
            is_whitelist_issue = any(x in error_msg.upper() for x in ["TLSV1_ALERT_INTERNAL_ERROR", "SSL HANDSHAKE FAILED", "TIMEOUT"])
            
            # Local Fallback
            try:
                self.uri = "mongodb://127.0.0.1:27017/"
                self.client = MongoClient(self.uri, serverSelectionTimeoutMS=2000)
                self.db = self.client['digital_healthcare']
                self.client.admin.command('ping')
                print("Database connected successfully to Local MongoDB!")
                return True, None
            except:
                pass
                
            return False, error_msg

db_manager = Database()
# --- END DATABASE MANAGEMENT ---

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev_key")
# Directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
ALLOWED_EXTENSIONS = {'pdf'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Create all necessary subdirectories
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(os.path.join(UPLOAD_FOLDER, 'verifications'), exist_ok=True)

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def send_system_email(subject, body_text, recipients, body_html=None):
    sender = os.getenv("MAIL_USERNAME")
    # Strip spaces from password if present (Google App Passwords usually have spaces for readability)
    password = os.getenv("MAIL_PASSWORD", "").replace(" ", "")
    
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
        self.profile_pic = user_data.get('profile_pic') # Base64 or Path

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

@app.route('/doctors')
@login_required
def doctors_page():
    users_col = db_manager.get_collection('users')
    # Use reports or a future appointments collection for treated counts
    # For now, let's assume doctors will have their counts tracked.
    doctors_list = list(users_col.find({"role": "doctor"}))
    
    # Enrich doctor data with real treated counts from the database
    # Even if they have zero, it must be the real zero.
    for doc in doctors_list:
        # In a real scenario, we'd query an 'appointments' collection.
        # Since we're building the 'best software', let's ensure the query is ready.
        appointments_col = db_manager.get_collection('appointments')
        treated_count = appointments_col.count_documents({"doctor_id": str(doc['_id']), "status": "completed"})
        doc['treated_count'] = treated_count
        
    return render_template('doctors.html', doctors=doctors_list)

@app.route('/')
def index():
    users_col = db_manager.get_collection('users')
    bookings_col = db_manager.get_collection('bookings')
    
    # Live stats from DB
    stats = {
        'total_users': users_col.count_documents({"role": "patient"}),
        'total_doctors': users_col.count_documents({"role": "doctor"}),
        'total_bookings': bookings_col.count_documents({}),
        # Mocking these as they typically come from dynamic business logic
        'cities_covered': 150,
        'satisfaction_rate': 98,
    }
    
    return render_template('index.html', stats=stats)

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
        # Send Welcome Email
        welcome_html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; padding: 20px; border: 1px solid #ddd; border-radius: 10px;">
            <h2 style="color: #2c3e50; text-align: center;">Welcome to HealthLab AI!</h2>
            <p>Dear {username},</p>
            <p>Thank you for joining our platform. We are committed to providing you with the best healthcare services at your doorstep.</p>
            {"<p style='color: #e67e22;'><strong>Note:</strong> Since you registered as a " + role + ", your account is currently <strong>Pending Verification</strong>. Our team will review your documents within 24-48 hours.</p>" if role != 'patient' else ""}
            <p>You can now log in to your dashboard to manage your healthcare needs.</p>
            <div style="text-align: center; margin: 30px 0;">
                <a href="{url_for('login', _external=True)}" style="background-color: #3498db; color: white; padding: 12px 25px; text-decoration: none; border-radius: 5px; font-weight: bold;">Login to Dashboard</a>
            </div>
            <p style="color: #7f8c8d; font-size: 12px; text-align: center;">© 2024 HealthLab AI. All rights reserved.</p>
        </div>
        """
        send_system_email(
            "Welcome to HealthLab AI",
            f"Welcome to HealthLab AI, {username}! Thank you for joining us.",
            email,
            welcome_html
        )
        
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
        
        # Notify User
        user_data = users_col.find_one({"_id": ObjectId(user_id)})
        if user_data:
            approval_html = f"""
            <div style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; padding: 20px; border: 1px solid #ddd; border-radius: 10px;">
                <h2 style="color: #27ae60; text-align: center;">Account Approved!</h2>
                <p>Hello {user_data.get('username')},</p>
                <p>Great news! Your professional account for <strong>HealthLab AI</strong> has been reviewed and <strong>Approved</strong>.</p>
                <p>You can now access all professional features on your dashboard, including accepting bookings and managing tasks.</p>
                <div style="text-align: center; margin: 30px 0;">
                    <a href="{url_for('login', _external=True)}" style="background-color: #27ae60; color: white; padding: 12px 25px; text-decoration: none; border-radius: 5px; font-weight: bold;">Go to Dashboard</a>
                </div>
                <p style="color: #7f8c8d; font-size: 12px; text-align: center;">© 2024 HealthLab AI. All rights reserved.</p>
            </div>
            """
            send_system_email("Account Approved - HealthLab AI", "Your account has been approved.", user_data.get('email'), approval_html)
            
        flash("Professional account approved and activated.")
    elif action == 'reject':
        users_col.update_one({"_id": ObjectId(user_id)}, {"$set": {"status": "Rejected", "reject_reason": reason}})
        
        # Notify User
        user_data = users_col.find_one({"_id": ObjectId(user_id)})
        if user_data:
            rejection_html = f"""
            <div style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; padding: 20px; border: 1px solid #ddd; border-radius: 10px;">
                <h2 style="color: #c0392b; text-align: center;">Verification Update</h2>
                <p>Hello {user_data.get('username')},</p>
                <p>We have reviewed your professional account documents. Unfortunately, we were unable to approve your account at this time.</p>
                <div style="background-color: #f9f9f9; padding: 15px; border-left: 4px solid #c0392b; margin: 20px 0;">
                    <strong>Reason for Rejection:</strong><br>
                    {reason}
                </div>
                <p>Please log in to your dashboard to re-upload the necessary documents or contact support if you have questions.</p>
                <div style="text-align: center; margin: 30px 0;">
                    <a href="{url_for('verify_account', _external=True)}" style="background-color: #c0392b; color: white; padding: 12px 25px; text-decoration: none; border-radius: 5px; font-weight: bold;">Update Documents</a>
                </div>
                <p style="color: #7f8c8d; font-size: 12px; text-align: center;">© 2024 HealthLab AI. All rights reserved.</p>
            </div>
            """
            send_system_email("Account Verification Update - HealthLab AI", "There is an update regarding your account verification.", user_data.get('email'), rejection_html)
            
        flash("Professional account application rejected.")
        
    return redirect(url_for('admin_verifications'))

@app.route('/download_report/<filename>')
@login_required
def download_report(filename):
    # Ensure correct headers for PDF viewing
    try:
        return send_from_directory(
            os.path.abspath(app.config['UPLOAD_FOLDER']), 
            filename, 
            mimetype='application/pdf',
            as_attachment=False
        )
    except FileNotFoundError:
        flash("Sorry, this report file could not be found.")
        return redirect(url_for('dashboard'))

@app.route('/uploads/verifications/<filename>')
@login_required
def uploaded_verification_file(filename):
    if current_user.role != 'admin': return "Unauthorized", 401
    return send_from_directory(
        os.path.join(os.path.abspath(app.config['UPLOAD_FOLDER']), 'verifications'), 
        filename,
        mimetype='application/pdf'
    )

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
    
    # Send Booking Confirmation Email
    booking_html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; padding: 20px; border: 1px solid #ddd; border-radius: 10px;">
        <h2 style="color: #2c3e50; text-align: center;">Booking Confirmed</h2>
        <p>Hi {request.form.get('full_name')},</p>
        <p>Your lab test has been successfully booked. A technician will be assigned to visit your address.</p>
        <div style="background-color: #f8f9fa; padding: 15px; border-radius: 5px; margin: 20px 0;">
            <p><strong>Test:</strong> {test_name}</p>
            <p><strong>Date:</strong> {date}</p>
            <p><strong>Time:</strong> {time}</p>
            <p><strong>Address:</strong> {address}</p>
        </div>
        <p>Please ensure someone is available at the scheduled time.</p>
        <div style="text-align: center; margin: 30px 0;">
            <a href="{url_for('booking_history', _external=True)}" style="background-color: #3498db; color: white; padding: 12px 25px; text-decoration: none; border-radius: 5px; font-weight: bold;">View My Bookings</a>
        </div>
        <p style="color: #7f8c8d; font-size: 12px; text-align: center;">© 2024 HealthLab AI. All rights reserved.</p>
    </div>
    """
    send_system_email(
        f"Booking Confirmed: {test_name}",
        f"Your booking for {test_name} on {date} at {time} is confirmed.",
        request.form.get('patient_email'),
        booking_html
    )
    
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
        
        from datetime import datetime
        reports_col = db_manager.get_collection('reports')
        reports_col.insert_one({
            "booking_id": booking_id,
            "patient_id": patient_id,
            "technician_id": current_user.id,
            "description": description,
            "pdf_url": filename,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        
        bookings_col = db_manager.get_collection('bookings')
        bookings_col.update_one({"_id": ObjectId(booking_id)}, {"$set": {"status": "completed"}})
        
        flash('Medical report (PDF) uploaded successfully!')
        
        # Notify Patient
        bookings_col = db_manager.get_collection('bookings')
        booking = bookings_col.find_one({"_id": ObjectId(booking_id)})
        if booking:
            report_html = f"""
            <div style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; padding: 20px; border: 1px solid #ddd; border-radius: 10px;">
                <h2 style="color: #3498db; text-align: center;">Medical Report Ready</h2>
                <p>Hi {booking.get('patient_name')},</p>
                <p>Your medical report for the test <strong>{booking.get('test_name')}</strong> is now available for download.</p>
                <div style="text-align: center; margin: 30px 0;">
                    <a href="{url_for('clinical_reports', _external=True)}" style="background-color: #3498db; color: white; padding: 12px 25px; text-decoration: none; border-radius: 5px; font-weight: bold;">Download Report</a>
                </div>
                <p>Stay healthy!</p>
                <p style="color: #7f8c8d; font-size: 12px; text-align: center;">© 2024 HealthLab AI. All rights reserved.</p>
            </div>
            """
            send_system_email(
                f"Medical Report Ready: {booking.get('test_name')}",
                f"Your medical report for {booking.get('test_name')} is now available in your dashboard.",
                booking.get('patient_email'),
                report_html
            )
            
        return redirect(url_for('dashboard'))
    
    flash('Invalid file type. Please upload a PDF.')
    return redirect(url_for('dashboard'))

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
        update_data = {
            "full_name": request.form.get('full_name'),
            "phone": request.form.get('phone'),
            "dob": request.form.get('dob'),
            "address": request.form.get('address'),
            "gender": request.form.get('gender'),
            "specialization": request.form.get('specialization'),
            "experience": request.form.get('experience')
        }
        
        # Handle Profile Picture Upload (Store in Database as Base64)
        if 'profile_pic' in request.files:
            file = request.files['profile_pic']
            if file and file.filename != '':
                import base64
                encoded_string = base64.b64encode(file.read()).decode('utf-8')
                update_data["profile_pic"] = f"data:{file.mimetype};base64,{encoded_string}"

        users_col.update_one(
            {"_id": ObjectId(current_user.id)},
            {"$set": update_data}
        )
        flash('Profile updated successfully!')
        return redirect(url_for('dashboard'))
    return render_template('profile.html', user=current_user)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

# Command to create admin user manually from CLI
@app.cli.command("create-admin")
def create_admin_command():
    """CLI Command to create or update the admin account."""
    email = "digitalhealthcare27@gmail.com"
    password = "HealthLabAdmin2024!"
    username = "SystemAdmin"
    
    admins_col = db_manager.get_collection('admins')
    existing = admins_col.find_one({"email": email})
    
    if existing:
        admins_col.update_one({"email": email}, {"$set": {"password": generate_password_hash(password), "username": username}})
        print(f"Admin {email} password updated.")
    else:
        admins_col.insert_one({
            "username": username,
            "email": email,
            "password": generate_password_hash(password),
            "role": "admin",
            "status": "Approved"
        })
        print(f"Admin {email} created successfully.")

if __name__ == '__main__':
    # Test DB connection on start
    success, hint = db_manager.test_connection()
    if success:
        app.run(debug=True, port=int(os.getenv("PORT", 5000)))
    else:
        if hint:
            print(hint)
        print("Could not start app: Database connection failed.")
