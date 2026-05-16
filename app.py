import os
import smtplib
import certifi
import threading
from email.message import EmailMessage
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, jsonify
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
    if not user_id: return None
    try:
        uid = ObjectId(user_id)
    except:
        return None
    
    for col_name in ['admins', 'users', 'doctors', 'technicians']:
        user_data = db_manager.get_collection(col_name).find_one({"_id": uid})
        if user_data:
            user = User(user_data)
            user.collection = col_name
            return user
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
    # Search for doctors in both 'users' and 'doctors' collections
    doctors_from_users = list(db_manager.get_collection('users').find({"role": "doctor"}))
    doctors_from_doctors = list(db_manager.get_collection('doctors').find())
    doctors_list = doctors_from_users + doctors_from_doctors
    for doc in doctors_list:
        doc['_id'] = str(doc['_id'])
        
    # Get patient's appointments (optional but good for context)
    appointments_col = db_manager.get_collection('appointments')
    appointments = list(appointments_col.find({"patient_id": current_user.id}).sort("_id", -1))
    
    return render_template('doctors.html', user=current_user, doctors=doctors_list, appointments=appointments)

@app.route('/get_doctors_by_specialization/<specialization>')
@login_required
def get_doctors_by_specialization(specialization):
    users_col = db_manager.get_collection('users')
    doctors = list(users_col.find({
        "role": "doctor", 
        "specialization": {"$regex": f"^{specialization}$", "$options": "i"}
    }))
    
    for doc in doctors:
        doc['_id'] = str(doc['_id'])
    
    return jsonify({"doctors": doctors})

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
        'total_doctors': db_manager.get_collection('users').count_documents({'role': 'doctor'}) + db_manager.get_collection('doctors').count_documents({}),
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
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password')
        next_url = request.args.get('next')
        
        user_data = None
        found_col = None
        for col_name in ['admins', 'users', 'doctors', 'technicians']:
            col = db_manager.get_collection(col_name)
            user_data = col.find_one({"email": email})
            if user_data:
                found_col = col_name
                break
        
        if user_data:
            if check_password_hash(user_data.get('password', ''), password):
                user = User(user_data)
                user.collection = found_col
                login_user(user)
                
                if next_url:
                    sep = '?' if '?' not in next_url else '&'
                    if consult_redirect:
                        return redirect(next_url + sep + "open_consult=true")
                    if chat_redirect:
                        return redirect(next_url + sep + "open_chat=true")
                    return redirect(next_url)
                return redirect(url_for('dashboard'))
            else:
                flash('Invalid password', 'error')
        else:
            flash('No account found with this email', 'error')
            
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email', '').strip().lower()
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
        
        for key in request.files:
            file = request.files[key]
            if file and file.filename != '':
                filename = secure_filename(f"{current_user.id}_{key}_{file.filename}")
                file_id = db_manager.fs.put(file.read(), filename=filename, content_type='application/pdf')
                docs_uploaded.append({"document_type": key, "filename": filename, "file_id": str(file_id)})
                
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
        
        user_data = users_col.find_one({"_id": ObjectId(user_id)})
        if user_data:
            approval_html = f"""
            <div style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; padding: 20px; border: 1px solid #ddd; border-radius: 10px;">
                <h2 style="color: #27ae60; text-align: center;">Account Approved!</h2>
                <p>Hello {user_data.get('username')},</p>
                <p>Great news! Your professional account for <strong>HealthLab AI</strong> has been reviewed and <strong>Approved</strong>.</p>
                <p>You can now access all professional features on your dashboard.</p>
                <div style="text-align: center; margin: 30px 0;">
                    <a href="{url_for('login', _external=True)}" style="background-color: #27ae60; color: white; padding: 12px 25px; text-decoration: none; border-radius: 5px; font-weight: bold;">Go to Dashboard</a>
                </div>
            </div>
            """
            send_system_email("Account Approved - HealthLab AI", "Your account has been approved.", user_data.get('email'), approval_html)
            
        flash("Professional account approved and activated.")
    elif action == 'reject':
        users_col.update_one({"_id": ObjectId(user_id)}, {"$set": {"status": "Rejected", "reject_reason": reason}})
        flash("Professional account application rejected.")
        
    return redirect(url_for('admin_verifications'))

@app.route('/download_report/<filename>')
@login_required
def download_report(filename):
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
    
    file = request.files['report_pdf']
    if file and allowed_file(file.filename):
        filename = secure_filename(f"{booking_id}_{file.filename}")
        file_id = db_manager.fs.put(file.read(), filename=filename, content_type='application/pdf')
        
        reports_col = db_manager.get_collection('reports')
        reports_col.insert_one({
            "booking_id": booking_id,
            "patient_id": patient_id,
            "technician_id": current_user.id,
            "description": description,
            "pdf_url": filename,
            "file_id": str(file_id),
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        
        bookings_col = db_manager.get_collection('bookings')
        bookings_col.update_one({"_id": ObjectId(booking_id)}, {"$set": {"status": "completed"}})
        
        flash('Medical report uploaded successfully!')
        return redirect(url_for('dashboard'))
    
    flash('Invalid file type.')
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
    flash('Order accepted!')
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
    flash('Sample collected!')
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
    bookings_col = db_manager.get_collection('bookings')
    my_reports = list(reports_col.find({"patient_id": current_user.id}).sort("_id", -1))
    
    for report in my_reports:
        booking = bookings_col.find_one({"_id": ObjectId(report['booking_id'])})
        report['test_name'] = booking.get('test_name', 'Laboratory Analysis') if booking else 'Laboratory Analysis'
    
    return render_template('clinical_reports.html', user=current_user, reports=my_reports)

@app.route('/doctor/add_slot', methods=['POST'])
@login_required
def add_slot():
    if current_user.role != 'doctor':
        return redirect(url_for('dashboard'))
    
    date = request.form.get('date')
    time = request.form.get('time')
    
    slots_col = db_manager.get_collection('slots')
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
    slots_col.delete_one({"_id": ObjectId(slot_id), "doctor_id": current_user.id, "status": "available"})
    flash('Slot deleted.')
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
        flash('Slot no longer available.')
        return redirect(url_for('doctors_page'))
    
    slots_col.update_one({"_id": ObjectId(slot_id)}, {"$set": {"status": "booked"}})
    appointments_col.insert_one({
        "doctor_id": doctor_id,
        "doctor_name": slot.get('doctor_name'),
        "patient_id": current_user.id,
        "patient_name": current_user.full_name or current_user.username,
        "date": slot.get('date'),
        "time": slot.get('time'),
        "status": "scheduled",
        "fee": 500,
        "payment_status": "paid"
    })
    flash('Appointment booked successfully!')
    return redirect(url_for('dashboard'))

@app.route('/save_prescription', methods=['POST'])
@login_required
def save_prescription():
    if current_user.role != 'doctor':
        return redirect(url_for('dashboard'))
    
    appointment_id = request.form.get('appointment_id')
    diagnosis = request.form.get('diagnosis')
    medicines = [] # Handle medicine list here
    
    prescriptions_col = db_manager.get_collection('prescriptions')
    appointments_col = db_manager.get_collection('appointments')
    
    prescriptions_col.insert_one({
        "appointment_id": ObjectId(appointment_id),
        "doctor_id": current_user.id,
        "patient_id": request.form.get('patient_id'),
        "diagnosis": diagnosis,
        "date": datetime.datetime.now()
    })
    
    appointments_col.update_one({"_id": ObjectId(appointment_id)}, {"$set": {"status": "completed"}})
    flash('Prescription sent!')
    return redirect(url_for('dashboard'))

@app.route('/view_prescription/<appt_id>')
@login_required
def view_prescription(appt_id):
    prescriptions_col = db_manager.get_collection('prescriptions')
    prescription = prescriptions_col.find_one({"appointment_id": ObjectId(appt_id)})
    return render_template('prescription.html', prescription=prescription)

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        target_collection = getattr(current_user, 'collection', 'users')
        col = db_manager.get_collection(target_collection)
        update_data = {
            "full_name": request.form.get('full_name'),
            "phone": request.form.get('phone'),
            "dob": request.form.get('dob'),
            "address": request.form.get('address'),
            "gender": request.form.get('gender'),
            "specialization": request.form.get('specialization'),
            "hospital_name": request.form.get('hospital_name')
        }
        
        if 'profile_pic' in request.files:
            file = request.files['profile_pic']
            if file and file.filename != '':
                import base64
                update_data["profile_pic"] = f"data:{file.mimetype};base64,{base64.b64encode(file.read()).decode('utf-8')}"

        col.update_one({"_id": ObjectId(current_user.id)}, {"$set": update_data})
        flash('Profile updated!')
        return redirect(url_for('dashboard'))
    return render_template('profile.html', user=current_user)

@app.route('/search')
@login_required
def search():
    query = request.args.get('q', '').strip()
    doctors_from_users = list(db_manager.get_collection('users').find({"role": "doctor", "full_name": {"$regex": query, "$options": "i"}}))
    doctors_from_doctors = list(db_manager.get_collection('doctors').find({"full_name": {"$regex": query, "$options": "i"}}))
    return render_template('search_results.html', query=query, results={'doctors': doctors_from_users + doctors_from_doctors})


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

def calculate_age(dob_str):
    if not dob_str: return "N/A"
    try:
        import datetime
        dob = datetime.datetime.strptime(dob_str, "%Y-%m-%d")
        today = datetime.datetime.now()
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    except:
        return "N/A"

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
    my_appointments = list(appointments_col.find({"doctor_id": current_user.id}).sort("_id", -1))
    import datetime
    current_date = datetime.datetime.now().strftime('%d %b %Y')
    
    return render_template('doctor_appointments.html', user=current_user, appointments=my_appointments, current_date=current_date)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

if __name__ == '__main__':
    # Test DB connection on start
    success, hint = db_manager.test_connection()
    if success:
        port = int(os.environ.get("PORT", 5000))
        app.run(host='0.0.0.0', port=port)
    else:
        if hint:
            print(hint)
        print("Could not start app: Database connection failed.")
