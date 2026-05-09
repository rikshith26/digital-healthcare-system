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
import datetime
import gridfs
import io
from flask import send_file

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
        self.fs = gridfs.GridFS(self.db)

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
# Configuration
ALLOWED_EXTENSIONS = {'pdf'}

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
        self.hospital_name = user_data.get('hospital_name')
        self.specialization = user_data.get('specialization')

    @property
    def is_profile_complete(self):
        if self.role == 'admin':
            return True
        
        # Patient requirements
        if self.role == 'patient':
            required_fields = [self.full_name, self.phone, self.dob, self.address, self.gender]
            return all(field and str(field).strip() for field in required_fields)
            
        # Doctor/Technician requirements
        if self.role == 'doctor':
            required_fields = [self.full_name, self.phone, self.hospital_name, self.specialization, self.profile_pic]
            return all(field and str(field).strip() for field in required_fields)
            
        # Default for others
        required_fields = [self.full_name, self.phone]
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
        
        # Add available slots count
        slots_col = db_manager.get_collection('slots')
        doc['available_slots_count'] = slots_col.count_documents({"doctor_id": str(doc['_id']), "status": "available"})
        
    # Get patient's appointments and sort them robustly
    appointments_col = db_manager.get_collection('appointments')
    appointments = list(appointments_col.find({"patient_id": current_user.id}))
    
    # Sort: Completed on top, then by completed_at (if exists), then by date/time
    def sort_key(appt):
        is_completed = 1 if appt.get('status') == 'completed' else 0
        comp_time = appt.get('completed_at', datetime.datetime.min)
        return (is_completed, comp_time)

    appointments.sort(key=sort_key, reverse=True)
    
    return render_template('doctors.html', doctors=doctors_list, appointments=appointments)

@app.route('/get_doctor_slots/<doctor_id>')
@login_required
def get_doctor_slots(doctor_id):
    slots_col = db_manager.get_collection('slots')
    appointments_col = db_manager.get_collection('appointments')
    
    slots = list(slots_col.find({"doctor_id": doctor_id, "status": "available"}).sort("date", 1))
    
    # Check if this specific patient has priority with this doctor
    has_priority = appointments_col.find_one({"patient_id": current_user.id, "doctor_id": doctor_id, "revisit": True}) is not None
    
    # Convert ObjectId to string for JSON serialization
    for slot in slots:
        slot['_id'] = str(slot['_id'])
        
    return {"slots": slots, "has_priority": has_priority}

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
    # Strict profile check for doctors
    if current_user.role == 'doctor' and not current_user.is_profile_complete:
        flash('Please complete your professional profile and upload a profile picture to access the dashboard.')
        return redirect(url_for('profile'))

    bookings_col = db_manager.get_collection('bookings')
    appointments_col = db_manager.get_collection('appointments')
    slots_col = db_manager.get_collection('slots')
    
    if current_user.role == 'patient':
        # Check if user has any priority/revisit marks across all doctors
        revisit_priorities = list(appointments_col.find({"patient_id": current_user.id, "revisit": True}))
        # Create a list of doctor IDs where user has priority
        priority_doctors = [str(a['doctor_id']) for a in revisit_priorities]
        
        # Get my appointments (scheduled and completed)
        my_appointments = list(appointments_col.find({"patient_id": current_user.id}).sort("_id", -1))
        
        return render_template('dashboard.html', user=current_user, priority_doctors=priority_doctors, appointments=my_appointments)
    elif current_user.role == 'doctor':
        # Doctor Dashboard View
        all_slots = list(slots_col.find({"doctor_id": current_user.id}).sort("date", 1))
        
        # Filter out past slots
        now = datetime.datetime.now()
        my_slots = []
        for slot in all_slots:
            try:
                slot_datetime = datetime.datetime.strptime(f"{slot['date']} {slot['time']}", "%Y-%m-%d %H:%M")
                if slot_datetime > now:
                    my_slots.append(slot)
            except:
                my_slots.append(slot)
                
        # Find active appointments for this doctor
        my_appointments = list(appointments_col.find({"doctor_id": current_user.id, "status": "scheduled"}).sort("_id", -1))
        
        # Calculate Total Earnings based on payment_status 'paid'
        paid_appointments = list(appointments_col.find({"doctor_id": current_user.id, "payment_status": "paid"}))
        total_earnings = sum(appt.get('fee', 0) for appt in paid_appointments)
        
        current_date = datetime.datetime.now().strftime('%d %b %Y')
        return render_template('dashboard.html', user=current_user, slots=my_slots, appointments=my_appointments, total_earnings=total_earnings, current_date=current_date)
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
        
        # Save uploaded files to Database
            
        for key in request.files:
            file = request.files[key]
            if file and file.filename != '':
                filename = secure_filename(f"{current_user.id}_{key}_{file.filename}")
                # Store in GridFS
                file_id = db_manager.fs.put(file.read(), filename=filename, content_type='application/pdf')
                docs_uploaded.append({"document_type": key, "filename": filename, "file_id": str(file_id)})
                
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
    # Ensure correct headers for PDF viewing from GridFS
    try:
        file_data = db_manager.fs.get_last_version(filename=filename)
        return send_file(
            io.BytesIO(file_data.read()),
            mimetype='application/pdf',
            as_attachment=False,
            download_name=filename
        )
    except gridfs.errors.NoFile:
        flash("Sorry, this report file could not be found.")
        return redirect(url_for('dashboard'))

@app.route('/uploads/verifications/<filename>')
@login_required
def uploaded_verification_file(filename):
    if current_user.role != 'admin': return "Unauthorized", 401
    try:
        file_data = db_manager.fs.get_last_version(filename=filename)
        return send_file(
            io.BytesIO(file_data.read()),
            mimetype='application/pdf',
            as_attachment=False,
            download_name=filename
        )
    except gridfs.errors.NoFile:
        return "File not found", 404

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
        # Store in GridFS
        file_id = db_manager.fs.put(file.read(), filename=filename, content_type='application/pdf')
        
        from datetime import datetime
        reports_col = db_manager.get_collection('reports')
        reports_col.insert_one({
            "booking_id": booking_id,
            "patient_id": patient_id,
            "technician_id": current_user.id,
            "description": description,
            "pdf_url": filename,
            "file_id": str(file_id),
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

# --- DOCTOR SLOT & APPOINTMENT MANAGEMENT ---

@app.route('/doctor/add_slot', methods=['POST'])
@login_required
def add_slot():
    if current_user.role != 'doctor':
        return redirect(url_for('dashboard'))
    
    date = request.form.get('date')
    time = request.form.get('time')
    
    if not date or not time:
        flash('Please provide both date and time.')
        return redirect(url_for('dashboard'))
    
    slots_col = db_manager.get_collection('slots')
    
    # Check if slot already exists
    if slots_col.find_one({"doctor_id": current_user.id, "date": date, "time": time}):
        flash('This slot already exists.')
        return redirect(url_for('dashboard'))
        
    slots_col.insert_one({
        "doctor_id": current_user.id,
        "doctor_name": current_user.full_name or current_user.username,
        "date": date,
        "time": time,
        "status": "available"
    })
    flash('New slot added successfully!')
    return redirect(url_for('dashboard'))

@app.route('/doctor/delete_slot/<slot_id>')
@login_required
def delete_slot(slot_id):
    if current_user.role != 'doctor':
        return redirect(url_for('dashboard'))
    
    slots_col = db_manager.get_collection('slots')
    slot = slots_col.find_one({"_id": ObjectId(slot_id), "doctor_id": current_user.id})
    
    if slot and slot.get('status') == 'available':
        slots_col.delete_one({"_id": ObjectId(slot_id)})
        flash('Slot deleted.')
    else:
        flash('Cannot delete a booked or non-existent slot.')
        
    return redirect(url_for('dashboard'))

@app.route('/doctor/mark_revisit/<appointment_id>', methods=['POST'])
@login_required
def mark_revisit(appointment_id):
    if current_user.role != 'doctor':
        return redirect(url_for('dashboard'))
    
    appointments_col = db_manager.get_collection('appointments')
    appointments_col.update_one(
        {"_id": ObjectId(appointment_id), "doctor_id": current_user.id},
        {"$set": {"revisit": True}}
    )
    flash('Patient marked for revisit priority.')
    return redirect(url_for('dashboard'))

@app.route('/book_appointment', methods=['POST'])
@login_required
def book_appointment():
    if current_user.role != 'patient':
        return redirect(url_for('dashboard'))
    
    slot_id = request.form.get('slot_id')
    doctor_id = request.form.get('doctor_id')
    
    slots_col = db_manager.get_collection('slots')
    appointments_col = db_manager.get_collection('appointments')
    
    slot = slots_col.find_one({"_id": ObjectId(slot_id), "status": "available"})
    
    if not slot:
        flash('This slot is no longer available.')
        return redirect(url_for('doctors_page'))
    
    # Mark slot as booked
    slots_col.update_one({"_id": ObjectId(slot_id)}, {"$set": {"status": "booked"}})
    
    # Create appointment
    appointments_col.insert_one({
        "doctor_id": doctor_id,
        "doctor_name": slot.get('doctor_name'),
        "patient_id": current_user.id,
        "patient_name": current_user.full_name or current_user.username,
        "date": slot.get('date'),
        "time": slot.get('time'),
        "status": "scheduled",
        "revisit": False,
        "fee": 500,
        "payment_status": "paid" # Simulated Razorpay success
    })
    
    # --- SEND PREMIUM EMAIL NOTIFICATION ---
    doctor_name = slot.get('doctor_name')
    appointment_date = slot.get('date')
    appointment_time = slot.get('time')
    patient_name = current_user.full_name or current_user.username
    
    email_html = f"""
    <div style="font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: auto; background-color: #ffffff; border: 1px solid #f0f0f0; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.05);">
        <!-- Header -->
        <table style="width: 100%; background-color: #f8fafc; border-bottom: 1px solid #f0f0f0; border-collapse: collapse;">
            <tr>
                <td style="padding: 25px 35px; vertical-align: middle;">
                    <div style="display: inline-block; vertical-align: middle;">
                        <h1 style="margin: 0; font-size: 20px; font-weight: 800; color: #1e293b; letter-spacing: -0.5px;">HealthLab <span style="font-weight: 400; color: #94a3b8;">AI</span></h1>
                        <p style="margin: 0; font-size: 10px; color: #64748b; font-weight: 700; text-transform: uppercase; letter-spacing: 0.1em;">Your Health. Our Priority.</p>
                    </div>
                </td>
            </tr>
        </table>

        <div style="padding: 40px 35px;">
            <!-- Confirmation Badge -->
            <div style="text-align: center; margin-bottom: 30px;">
                <div style="background-color: #eff6ff; width: 60px; height: 60px; border-radius: 30px; line-height: 60px; display: inline-block; text-align: center;">
                    <span style="color: #3b82f6; font-size: 28px; vertical-align: middle;">&#10003;</span>
                </div>
                <h2 style="margin: 15px 0 0; font-size: 28px; font-weight: 800; color: #1e293b;">Booking Confirmed</h2>
                <p style="margin: 5px 0 0; color: #64748b; font-size: 15px;">Your health is our top priority.</p>
            </div>

            <p style="color: #1e293b; font-size: 16px;">Hello <strong>{patient_name}</strong>,</p>
            <p style="color: #64748b; font-size: 15px; line-height: 1.6;">Great news! Your consultation with <strong>Dr. {doctor_name}</strong> has been successfully scheduled. Here are your appointment details:</p>

            <!-- Details Card -->
            <div style="border: 1px solid #f0f0f0; border-radius: 16px; padding: 25px; margin: 30px 0;">
                <table style="width: 100%; border-collapse: collapse;">
                    <tr>
                        <td style="width: 50px; vertical-align: middle; padding-bottom: 20px;">
                            <div style="background-color: #f1f5f9; width: 42px; height: 42px; border-radius: 21px; line-height: 42px; text-align: center;">
                                <span style="font-size: 18px; vertical-align: middle;">👤</span>
                            </div>
                        </td>
                        <td style="padding-bottom: 20px; padding-left: 15px;">
                            <div style="color: #94a3b8; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em;">Doctor</div>
                            <div style="color: #1e293b; font-size: 15px; font-weight: 700;">Dr. {doctor_name}</div>
                        </td>
                    </tr>
                    <tr>
                        <td style="width: 50px; vertical-align: middle; padding-bottom: 20px;">
                            <div style="background-color: #f1f5f9; width: 42px; height: 42px; border-radius: 21px; line-height: 42px; text-align: center;">
                                <span style="font-size: 18px; vertical-align: middle;">📅</span>
                            </div>
                        </td>
                        <td style="padding-bottom: 20px; padding-left: 15px;">
                            <div style="color: #94a3b8; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em;">Date</div>
                            <div style="color: #1e293b; font-size: 15px; font-weight: 700;">{appointment_date}</div>
                        </td>
                    </tr>
                    <tr>
                        <td style="width: 50px; vertical-align: middle;">
                            <div style="background-color: #f1f5f9; width: 42px; height: 42px; border-radius: 21px; line-height: 42px; text-align: center;">
                                <span style="font-size: 18px; vertical-align: middle;">🕒</span>
                            </div>
                        </td>
                        <td style="padding-left: 15px;">
                            <div style="color: #94a3b8; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em;">Time</div>
                            <div style="color: #1e293b; font-size: 15px; font-weight: 700;">{appointment_time}</div>
                        </td>
                    </tr>
                </table>
            </div>

            <!-- Info Sections -->
            <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
                <tr>
                    <td style="width: 30px; vertical-align: top; font-size: 18px;">ℹ️</td>
                    <td style="padding-left: 10px; color: #64748b; font-size: 14px; line-height: 1.5;">Please arrive 10 minutes early to ensure a smooth check-in process.</td>
                </tr>
            </table>
            <table style="width: 100%; border-collapse: collapse; margin-bottom: 40px; padding-top: 15px; border-top: 1px solid #f8fafc;">
                <tr>
                    <td style="width: 30px; vertical-align: top; font-size: 18px; padding-top: 15px;">💙</td>
                    <td style="padding-left: 10px; padding-top: 15px; color: #64748b; font-size: 14px; line-height: 1.5;">If you need to reschedule, please visit <a href="{url_for('dashboard', _external=True)}" style="color: #3b82f6; text-decoration: none; font-weight: 600;">your dashboard</a>.</td>
                </tr>
            </table>
        </div>

        <!-- Footer -->
        <div style="background-color: #f8fafc; padding: 30px; text-align: center; border-top: 1px solid #f0f0f0;">
            <p style="margin: 0; color: #94a3b8; font-size: 12px; font-weight: 600;">Thank you for choosing <strong>HealthLab AI</strong>.</p>
            <p style="margin: 5px 0 0; color: #94a3b8; font-size: 12px;">We're here for your health, always.</p>
            <p style="margin: 25px 0 0; color: #cbd5e1; font-size: 10px; text-transform: uppercase; letter-spacing: 0.1em; font-weight: 700;">This is a system generated message.</p>
        </div>
    </div>
    """
    
    send_system_email(
        f"Appointment Confirmed: Dr. {doctor_name}",
        f"Your appointment with Dr. {doctor_name} on {appointment_date} at {appointment_time} is confirmed.",
        current_user.email,
        email_html
    )
    
    flash('Appointment booked successfully! Your booking has already done and no need to pay any kind of money to the doctor at the hospital.', 'success')
    return redirect(url_for('dashboard'))

def calculate_age(dob_str):
    if not dob_str: return "N/A"
    try:
        dob = datetime.datetime.strptime(dob_str, "%Y-%m-%d")
        today = datetime.datetime.now()
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    except:
        return "N/A"

@app.route('/save_prescription', methods=['POST'])
@login_required
def save_prescription():
    if current_user.role != 'doctor':
        return redirect(url_for('dashboard'))
    
    appointment_id = request.form.get('appointment_id')
    diagnosis = request.form.get('diagnosis')
    notes = request.form.get('notes')
    follow_up = request.form.get('follow_up')
    
    # Handle multiple medicines
    med_names = request.form.getlist('med_name[]')
    med_freqs = request.form.getlist('med_freq[]')
    med_durations = request.form.getlist('med_duration[]')
    med_dosages = request.form.getlist('med_dosage[]')
    med_instructions = request.form.getlist('med_instructions[]')
    
    symptoms = request.form.get('symptoms')
    
    medicines = []
    for name, freq, dur, dose, inst in zip(med_names, med_freqs, med_durations, med_dosages, med_instructions):
        if name.strip():
            medicines.append({
                "name": name,
                "frequency": freq,
                "duration": dur,
                "dosage": dose,
                "instructions": inst
            })
    
    appointments_col = db_manager.get_collection('appointments')
    prescriptions_col = db_manager.get_collection('prescriptions')
    users_col = db_manager.get_collection('users')
    
    # Get appointment details
    appt = appointments_col.find_one({"_id": ObjectId(appointment_id)})
    if not appt:
        flash('Appointment not found.', 'error')
        return redirect(url_for('dashboard'))
    
    # Fetch Patient Profile Data
    patient_user = users_col.find_one({"_id": ObjectId(appt['patient_id'])})
    patient_age = calculate_age(patient_user.get('dob')) if patient_user else "N/A"
    patient_address = patient_user.get('address', 'N/A') if patient_user else "N/A"
    patient_gender = patient_user.get('gender', 'N/A') if patient_user else "N/A"
    patient_phone = patient_user.get('phone', 'N/A') if patient_user else "N/A"
    
    # Save prescription
    prescriptions_col.insert_one({
        "appointment_id": ObjectId(appointment_id),
        "doctor_id": current_user.id,
        "doctor_name": current_user.full_name or current_user.username,
        "doctor_specialization": current_user.specialization,
        "doctor_hospital": current_user.hospital_name,
        "patient_id": appt['patient_id'],
        "patient_name": appt['patient_name'],
        "patient_age": patient_age,
        "patient_gender": patient_gender,
        "patient_phone": patient_phone,
        "patient_address": patient_address,
        "symptoms": symptoms,
        "diagnosis": diagnosis,
        "medicines": medicines,
        "notes": notes,
        "follow_up": follow_up,
        "date": datetime.datetime.now()
    })
    
    # Finalize appointment
    appointments_col.update_one(
        {"_id": ObjectId(appointment_id)},
        {"$set": {
            "status": "completed",
            "completed_at": datetime.datetime.now()
        }}
    )
    
    flash('Prescription sent successfully!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/view_prescription/<appt_id>')
@login_required
def view_prescription(appt_id):
    prescriptions_col = db_manager.get_collection('prescriptions')
    prescription = prescriptions_col.find_one({"appointment_id": ObjectId(appt_id)})
    
    if not prescription:
        flash('Prescription not found.', 'error')
        return redirect(url_for('dashboard'))
    
    # Verify access
    if current_user.role == 'patient' and str(prescription['patient_id']) != str(current_user.id):
        flash('Access denied.', 'error')
        return redirect(url_for('dashboard'))
    elif current_user.role == 'doctor' and str(prescription['doctor_id']) != str(current_user.id):
        flash('Access denied.', 'error')
        return redirect(url_for('dashboard'))
        
    return render_template('prescription.html', prescription=prescription)


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
            "experience": request.form.get('experience'),
            "hospital_name": request.form.get('hospital_name')
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

@app.route('/search')
@login_required
def search():
    query = request.args.get('q', '').strip()
    if not query:
        return redirect(url_for('dashboard'))
    
    results = {
        'doctors': [],
        'reports': [],
        'bookings': [],
        'users': []
    }
    
    # 1. Search Doctors (Available to all)
    users_col = db_manager.get_collection('users')
    results['doctors'] = list(users_col.find({
        "role": "doctor",
        "$or": [
            {"username": {"$regex": query, "$options": "i"}},
            {"full_name": {"$regex": query, "$options": "i"}},
            {"specialization": {"$regex": query, "$options": "i"}}
        ]
    }))
    
    # 2. Search Patient-specific data
    if current_user.role == 'patient':
        # Search Reports
        reports_col = db_manager.get_collection('reports')
        results['reports'] = list(reports_col.find({
            "patient_id": current_user.id,
            "$or": [
                {"description": {"$regex": query, "$options": "i"}},
                {"pdf_url": {"$regex": query, "$options": "i"}}
            ]
        }))
        
        # Search Bookings
        bookings_col = db_manager.get_collection('bookings')
        results['bookings'] = list(bookings_col.find({
            "patient_id": current_user.id,
            "$or": [
                {"test_name": {"$regex": query, "$options": "i"}},
                {"address": {"$regex": query, "$options": "i"}}
            ]
        }))
        
    # 3. Admin Search (Users)
    if current_user.role == 'admin':
        results['users'] = list(users_col.find({
            "$or": [
                {"username": {"$regex": query, "$options": "i"}},
                {"email": {"$regex": query, "$options": "i"}},
                {"role": {"$regex": query, "$options": "i"}}
            ]
        }))

    return render_template('search_results.html', query=query, results=results)

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

@app.route('/doctor/manage_slots')
@login_required
def manage_slots():
    if current_user.role != 'doctor':
        return redirect(url_for('dashboard'))
    
    slots_col = db_manager.get_collection('slots')
    my_slots = list(slots_col.find({"doctor_id": current_user.id}).sort("date", 1))
    return render_template('manage_slots.html', user=current_user, slots=my_slots)

@app.route('/ai_assistant')
@login_required
def ai_assistant():
    return render_template('ai_assistant.html', user=current_user)

@app.route('/doctor/earnings')
@login_required
def doctor_earnings():
    if current_user.role != 'doctor':
        return redirect(url_for('dashboard'))
    
    appointments_col = db_manager.get_collection('appointments')
    # Only count earnings where payment_status is 'paid'
    paid_appointments = list(appointments_col.find({
        "doctor_id": current_user.id, 
        "payment_status": "paid"
    }).sort("_id", -1))
    
    total_earnings = sum(appt.get('fee', 0) for appt in paid_appointments)
    return render_template('doctor_earnings.html', user=current_user, appointments=paid_appointments, total_earnings=total_earnings)

@app.route('/doctor/appointments')
@login_required
def doctor_appointments():
    if current_user.role != 'doctor':
        return redirect(url_for('dashboard'))
    
    appointments_col = db_manager.get_collection('appointments')
    # Show all appointments for the doctor
    my_appointments = list(appointments_col.find({"doctor_id": current_user.id}).sort("_id", -1))
    current_date = datetime.datetime.now().strftime('%d %b %Y')
    
    return render_template('doctor_appointments.html', user=current_user, appointments=my_appointments, current_date=current_date)

if __name__ == '__main__':
    # Test DB connection on start
    success, hint = db_manager.test_connection()
    if success:
        app.run(debug=True, port=int(os.getenv("PORT", 5000)))
    else:
        if hint:
            print(hint)
        print("Could not start app: Database connection failed.")
