import os
import smtplib
import certifi
import threading
from email.message import EmailMessage
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, jsonify, session
from authlib.integrations.flask_client import OAuth
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from pymongo import MongoClient
from bson.objectid import ObjectId
from dotenv import load_dotenv
import datetime
import gridfs
import io
import random
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

# OAuth Setup
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=os.getenv('GOOGLE_CLIENT_ID'),
    client_secret=os.getenv('GOOGLE_CLIENT_SECRET'),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

# Enable automatic template reloading and disable caching for instant updates on refresh
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
app.jinja_env.auto_reload = True
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
        verif = user_data.get('verification_details') or {}
        if not isinstance(verif, dict): verif = {}
        self.full_name = user_data.get('full_name') or verif.get('full_name')
        self.phone = user_data.get('phone') or verif.get('phone_number')
        self.dob = user_data.get('dob')
        self.address = user_data.get('address')
        self.gender = user_data.get('gender')
        self.status = user_data.get('status', 'Approved')
        self.reject_reason = user_data.get('reject_reason', '')
        self.verification_docs = user_data.get('verification_docs', [])
        self.profile_pic = user_data.get('profile_pic') # Base64 or Path
        self.hospital_name = user_data.get('hospital_name') or verif.get('previous_hospital')
        self.specialization = user_data.get('specialization') or verif.get('specialization')
        self.terms_accepted = user_data.get('terms_accepted', False)
        self.terms_accepted_on = user_data.get('terms_accepted_on')

    @property
    def formatted_dob(self):
        if not self.dob:
            return ""
        try:
            parts = str(self.dob).strip().split('-')
            if len(parts) == 3 and len(parts[0]) == 4:
                return f"{parts[2]}-{parts[1]}-{parts[0]}"
        except:
            pass
        return str(self.dob)

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

@app.route('/api/accept_terms', methods=['POST'])
@login_required
def accept_terms_api():
    try:
        uid = ObjectId(current_user.id)
        current_time = datetime.datetime.now()
        
        # Determine which collection the user belongs to
        col_name = getattr(current_user, 'collection', None)
        if not col_name:
            for c in ['admins', 'users', 'doctors', 'technicians']:
                if db_manager.get_collection(c).find_one({"_id": uid}):
                    col_name = c
                    break
                    
        if col_name:
            db_manager.get_collection(col_name).update_one(
                {"_id": uid},
                {"$set": {
                    "terms_accepted": True,
                    "terms_accepted_on": current_time
                }}
            )
            return jsonify({"success": True, "message": "Terms accepted successfully."})
        return jsonify({"success": False, "message": "User collection not found."}), 400
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

def normalize_doctor(doc):
    verif = doc.get('verification_details') or {}
    if not isinstance(verif, dict):
        verif = {}
    full_name = doc.get('full_name') or verif.get('full_name') or doc.get('username', 'Doctor')
    specialization = doc.get('specialization') or verif.get('specialization') or 'General Physician'
    experience = doc.get('experience') or verif.get('experience_years') or '5'
    phone = doc.get('phone') or verif.get('phone_number') or ''
    hospital = doc.get('hospital_name') or verif.get('previous_hospital') or ''
    profile_pic = doc.get('profile_pic')
    fee = doc.get('fee', 500)
    
    return {
        '_id': str(doc.get('_id')),
        'username': doc.get('username'),
        'email': doc.get('email'),
        'full_name': full_name,
        'specialization': specialization,
        'experience': experience,
        'phone': phone,
        'hospital_name': hospital,
        'profile_pic': profile_pic,
        'fee': fee,
        'status': doc.get('status', 'Approved')
    }

@app.route('/doctors')
@login_required
def doctors_page():
    # Search for doctors in both 'users' and 'doctors' collections
    doctors_from_users = list(db_manager.get_collection('users').find({
        "role": "doctor",
        "status": {"$ne": "Rejected"}
    }))
    doctors_from_doctors = list(db_manager.get_collection('doctors').find({
        "status": {"$ne": "Rejected"}
    }))
    
    seen_ids = set()
    doctors_list = []
    for doc in doctors_from_users + doctors_from_doctors:
        normalized = normalize_doctor(doc)
        if normalized['_id'] not in seen_ids:
            seen_ids.add(normalized['_id'])
            doctors_list.append(normalized)
        
    # Get patient's appointments (optional but good for context)
    appointments_col = db_manager.get_collection('appointments')
    appointments = list(appointments_col.find({"patient_id": current_user.id}).sort("_id", -1))
    
    return render_template('doctors.html', user=current_user, doctors=doctors_list, appointments=appointments)

@app.route('/get_doctors_by_specialization/<specialization>')
@login_required
def get_doctors_by_specialization(specialization):
    specialization = specialization.strip()
    is_all = specialization.lower() in ['all', 'any', 'all specialists', 'view all', 'all doctors']
    
    # Flexible matching patterns
    if 'dent' in specialization.lower():
        regex_pattern = r'dent'
    elif 'fever' in specialization.lower() or 'physician' in specialization.lower() or 'cold' in specialization.lower():
        regex_pattern = r'general|physician|mbbs|internal'
    elif 'skin' in specialization.lower() or 'derma' in specialization.lower():
        regex_pattern = r'derma|skin'
    elif 'heart' in specialization.lower() or 'cardio' in specialization.lower():
        regex_pattern = r'cardio|heart'
    elif 'preg' in specialization.lower() or 'gynec' in specialization.lower():
        regex_pattern = r'gynec|obstet'
    elif 'kidney' in specialization.lower() or 'uro' in specialization.lower():
        regex_pattern = r'uro|kidney|nephro'
    elif 'child' in specialization.lower() or 'pedia' in specialization.lower():
        regex_pattern = r'pedia|child'
    elif 'mental' in specialization.lower() or 'psych' in specialization.lower():
        regex_pattern = r'psych'
    elif 'emerg' in specialization.lower():
        regex_pattern = r'emerg'
    else:
        regex_pattern = specialization

    users_col = db_manager.get_collection('users')
    doctors_col = db_manager.get_collection('doctors')
    
    if is_all:
        users_docs = list(users_col.find({"role": "doctor", "status": {"$ne": "Rejected"}}))
        docs_docs = list(doctors_col.find({"status": {"$ne": "Rejected"}}))
    else:
        query_condition = {
            "$or": [
                {"specialization": {"$regex": regex_pattern, "$options": "i"}},
                {"verification_details.specialization": {"$regex": regex_pattern, "$options": "i"}}
            ]
        }
        users_docs = list(users_col.find({
            "role": "doctor",
            "status": {"$ne": "Rejected"},
            **query_condition
        }))
        docs_docs = list(doctors_col.find({
            "status": {"$ne": "Rejected"},
            **query_condition
        }))
    
    doctors = []
    seen_ids = set()
    for doc in users_docs + docs_docs:
        normalized = normalize_doctor(doc)
        if normalized['_id'] not in seen_ids:
            seen_ids.add(normalized['_id'])
            doctors.append(normalized)
    
    return jsonify({"doctors": doctors})

@app.route('/get_doctor_slots/<doctor_id>')
@login_required
def get_doctor_slots(doctor_id):
    slots_col = db_manager.get_collection('slots')
    appointments_col = db_manager.get_collection('appointments')
    
    query = {
        "$or": [{"doctor_id": str(doctor_id)}]
    }
    if ObjectId.is_valid(doctor_id):
        query["$or"].append({"doctor_id": ObjectId(doctor_id)})
        
    query["status"] = "available"
    slots = list(slots_col.find(query).sort("date", 1))
    
    # Check if this specific patient has priority with this doctor
    has_priority = appointments_col.find_one({"patient_id": current_user.id, "doctor_id": str(doctor_id), "revisit": True}) is not None
    
    # Convert ObjectId to string for JSON serialization
    for slot in slots:
        slot['_id'] = str(slot['_id'])
        slot['doctor_id'] = str(slot.get('doctor_id', ''))
        
    return jsonify({"slots": slots, "has_priority": has_priority})

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

@app.route('/privacy-policy')
def privacy_policy():
    return render_template('privacy_policy.html')

@app.route('/terms-and-conditions')
def terms_and_conditions():
    return render_template('terms.html')

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
                
                redirect_url = url_for('dashboard')
                if next_url:
                    sep = '?' if '?' not in next_url else '&'
                    if consult_redirect:
                        redirect_url = next_url + sep + "open_consult=true"
                    elif chat_redirect:
                        redirect_url = next_url + sep + "open_chat=true"
                    else:
                        redirect_url = next_url

                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return jsonify({"success": True, "redirect": redirect_url})
                return redirect(redirect_url)
            else:
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return jsonify({"success": False, "message": "Invalid password"})
                flash('Invalid password', 'error')
        else:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({"success": False, "message": "No account found with this email"})
            flash('No account found with this email', 'error')
            
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password')
        role = request.form.get('role', 'patient')
        terms_accepted_val = request.form.get('terms_accepted')
        terms_accepted = True if terms_accepted_val in ['on', 'true', '1'] else False
        terms_accepted_on = datetime.datetime.now() if terms_accepted else None
        
        admins_col = db_manager.get_collection('admins')
        users_col = db_manager.get_collection('users')
        if users_col.find_one({"email": email}) or admins_col.find_one({"email": email}):
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({"success": False, "message": "Email already exists"})
            flash('Email already exists')
            return redirect(url_for('register'))
        
        hashed_password = generate_password_hash(password)
        
        status = 'Approved' if role == 'patient' else 'Pending Verification'
        users_col.insert_one({
            "username": username,
            "email": email,
            "password": hashed_password,
            "role": role,
            "status": status,
            "terms_accepted": terms_accepted,
            "terms_accepted_on": terms_accepted_on
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
        
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"success": True})
            
        flash('Registration successful. Please login.')
        return redirect(url_for('login'))
        
    return render_template('register.html')

@app.route('/auth/google')
def google_login():
    google_client_id = os.getenv('GOOGLE_CLIENT_ID', '').strip()
    google_client_secret = os.getenv('GOOGLE_CLIENT_SECRET', '').strip()
    if not google_client_id or not google_client_secret:
        flash('Google Sign-In is not configured yet. Please configure GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in the .env file.', 'error')
        return redirect(url_for('login'))

    next_url = request.args.get('next')
    if next_url:
        session['oauth_next_url'] = next_url

    redirect_uri = url_for('google_callback', _external=True)
    return google.authorize_redirect(redirect_uri)

@app.route('/auth/google/callback')
def google_callback():
    google_client_id = os.getenv('GOOGLE_CLIENT_ID', '').strip()
    google_client_secret = os.getenv('GOOGLE_CLIENT_SECRET', '').strip()
    if not google_client_id or not google_client_secret:
        flash('Google Sign-In is not configured yet.', 'error')
        return redirect(url_for('login'))

    try:
        token = google.authorize_access_token()
    except Exception as e:
        flash(f'Google authentication failed: {str(e)}', 'error')
        return redirect(url_for('login'))

    user_info = token.get('userinfo')
    if not user_info:
        try:
            resp = google.get('https://www.googleapis.com/oauth2/v3/userinfo')
            user_info = resp.json()
        except Exception:
            flash('Unable to retrieve user information from Google.', 'error')
            return redirect(url_for('login'))

    email = user_info.get('email', '').strip().lower()
    if not email:
        flash('Google account did not provide a valid email.', 'error')
        return redirect(url_for('login'))

    user_data = None
    found_col = None
    for col_name in ['admins', 'users', 'doctors', 'technicians']:
        col = db_manager.get_collection(col_name)
        user_data = col.find_one({"email": email})
        if user_data:
            found_col = col_name
            break

    if user_data:
        update_fields = {}
        if not user_data.get('google_id') and user_info.get('sub'):
            update_fields['google_id'] = user_info.get('sub')
        if not user_data.get('profile_pic') and user_info.get('picture'):
            update_fields['profile_pic'] = user_info.get('picture')
        if update_fields:
            db_manager.get_collection(found_col).update_one({"_id": user_data['_id']}, {"$set": update_fields})
            user_data.update(update_fields)

        user = User(user_data)
        user.collection = found_col
        login_user(user)
        flash(f"Welcome back, {user.username or user.email}!", "success")
    else:
        username = user_info.get('name') or email.split('@')[0]
        new_user = {
            "username": username,
            "email": email,
            "full_name": user_info.get('name') or username,
            "role": "patient",
            "status": "Approved",
            "google_id": user_info.get('sub'),
            "profile_pic": user_info.get('picture'),
            "terms_accepted": True,
            "terms_accepted_on": datetime.datetime.now(),
            "created_at": datetime.datetime.now()
        }
        users_col = db_manager.get_collection('users')
        res = users_col.insert_one(new_user)
        new_user['_id'] = res.inserted_id

        welcome_html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; padding: 20px; border: 1px solid #ddd; border-radius: 10px;">
            <h2 style="color: #2c3e50; text-align: center;">Welcome to HealthLab AI!</h2>
            <p>Dear {username},</p>
            <p>Thank you for signing in with Google. We are committed to providing you with modern healthcare services at your fingertips.</p>
            <p>You can now manage your appointments and health records through your dashboard.</p>
            <div style="text-align: center; margin: 30px 0;">
                <a href="{url_for('dashboard', _external=True)}" style="background-color: #3498db; color: white; padding: 12px 25px; text-decoration: none; border-radius: 5px; font-weight: bold;">Go to Dashboard</a>
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

        user = User(new_user)
        user.collection = 'users'
        login_user(user)
        flash(f"Account successfully created! Welcome, {username}.", "success")

    next_url = session.pop('oauth_next_url', None)
    return redirect(next_url or url_for('dashboard'))

@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        
        user_data = None
        for col_name in ['admins', 'users', 'doctors', 'technicians']:
            user_data = db_manager.get_collection(col_name).find_one({"email": email})
            if user_data:
                break
                
        if user_data:
            otp = str(random.randint(100000, 999999))
            expires_at = datetime.datetime.now() + datetime.timedelta(seconds=60)
            
            db_manager.get_collection('otps').update_one(
                {"email": email},
                {"$set": {"otp": otp, "expires_at": expires_at}},
                upsert=True
            )
            
            otp_html = f"""
            <div style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; padding: 20px; border: 1px solid #ddd; border-radius: 10px;">
                <h2 style="color: #2c3e50; text-align: center;">Password Reset OTP</h2>
                <p>You requested to reset your password.</p>
                <p>Your 6-digit OTP is:</p>
                <h1 style="text-align: center; color: #e74c3c; letter-spacing: 5px;">{otp}</h1>
                <p>This OTP is valid for exactly <strong>60 seconds</strong>.</p>
                <p>If you did not request this, please ignore this email.</p>
            </div>
            """
            send_system_email("Your Password Reset OTP", f"Your OTP is {otp}", email, otp_html)
            
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({"success": True})
            
            flash('An OTP has been sent to your email. It will expire in 60 seconds.')
            return render_template('forgot_password.html', otp_sent=True, email=email)
        else:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({"success": False, "message": "No account found with this email."})
            flash('No account found with this email.', 'error')
            
    return render_template('forgot_password.html', otp_sent=False)

@app.route('/verify_otp', methods=['POST'])
def verify_otp():
    email = request.form.get('email')
    otp = request.form.get('otp')
    
    otp_record = db_manager.get_collection('otps').find_one({"email": email})
    
    if otp_record:
        if datetime.datetime.now() > otp_record['expires_at']:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({"success": False, "message": "OTP has expired. Please request a new one."})
            flash('OTP has expired. Please request a new one.', 'error')
            return redirect(url_for('forgot_password'))
        elif otp_record['otp'] == otp:
            db_manager.get_collection('otps').delete_one({"email": email})
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({"success": True})
            return render_template('reset_password.html', email=email)
        else:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({"success": False, "message": "Invalid OTP. Please try again."})
            flash('Invalid OTP. Please try again.', 'error')
            return render_template('forgot_password.html', otp_sent=True, email=email)
    else:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"success": False, "message": "Invalid request."})
        flash('Invalid request.', 'error')
        return redirect(url_for('forgot_password'))

@app.route('/reset_password', methods=['POST'])
def reset_password():
    email = request.form.get('email')
    password = request.form.get('password')
    confirm_password = request.form.get('confirm_password')
    
    if password != confirm_password:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"success": False, "message": "Passwords do not match."})
        flash('Passwords do not match.', 'error')
        return render_template('reset_password.html', email=email)
        
    hashed_password = generate_password_hash(password)
    
    updated = False
    for col_name in ['admins', 'users', 'doctors', 'technicians']:
        col = db_manager.get_collection(col_name)
        if col.find_one({"email": email}):
            col.update_one({"email": email}, {"$set": {"password": hashed_password}})
            updated = True
            break
            
    if updated:
        alert_html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; padding: 20px; border: 1px solid #ddd; border-radius: 10px;">
            <h2 style="color: #c0392b; text-align: center;">Security Alert: Password Changed</h2>
            <p>Hello,</p>
            <p>Your account password has been successfully updated.</p>
            <p><strong>If you did not make this change, please check and secure your account immediately.</strong></p>
            <p>Thank you,<br>HealthLab AI Security Team</p>
        </div>
        """
        send_system_email("Security Alert: Password Changed", "Your password was changed. If it wasn't you, please secure your account.", email, alert_html)
        
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"success": True})
            
        flash('Password updated successfully. You can now login.')
        return redirect(url_for('login'))
    else:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"success": False, "message": "Error updating password."})
        flash('Error updating password.', 'error')
        return redirect(url_for('forgot_password'))

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
        
        # Get my appointments
        my_appointments = list(appointments_col.find({"patient_id": current_user.id}).sort("_id", -1))
        
        # Get my reports
        reports_col = db_manager.get_collection('reports')
        my_reports = list(reports_col.find({"patient_id": current_user.id}).sort("_id", -1))
        for report in my_reports:
            booking = bookings_col.find_one({"_id": ObjectId(report['booking_id'])})
            report['test_name'] = booking.get('test_name', 'Laboratory Analysis') if booking else 'Laboratory Analysis'
            
        # Get my prescriptions (for daily medicines)
        prescriptions_col = db_manager.get_collection('prescriptions')
        my_prescriptions = list(prescriptions_col.find({"patient_id": current_user.id}).sort("date", -1))
        daily_medicines = []
        for pres in my_prescriptions:
            if 'medicines' in pres:
                daily_medicines.extend(pres['medicines'])
                
        # Generate Activity Timeline
        timeline = []
        for appt in my_appointments:
            try:
                date_obj = datetime.datetime.strptime(f"{appt['date']} {appt['time']}", "%Y-%m-%d %H:%M")
                timeline.append({
                    "title": "Appointment Scheduled" if appt['status'] == 'scheduled' else "Appointment Completed",
                    "desc": f"Consultation with {appt['doctor_name']}",
                    "time": date_obj,
                    "type": "appointment",
                    "status": appt['status']
                })
            except:
                pass
        for rep in my_reports:
            try:
                date_obj = datetime.datetime.strptime(rep['timestamp'], "%Y-%m-%d %H:%M:%S")
                timeline.append({
                    "title": "Report Generated",
                    "desc": f"{rep['test_name']} results uploaded.",
                    "time": date_obj,
                    "type": "report",
                    "status": "completed"
                })
            except:
                pass
        # Sort timeline descending
        timeline.sort(key=lambda x: x['time'], reverse=True)
        # Only take top 5
        timeline = timeline[:5]
        
        return render_template('dashboard.html', 
                               user=current_user, 
                               priority_doctors=priority_doctors, 
                               appointments=my_appointments,
                               reports=my_reports,
                               daily_medicines=daily_medicines,
                               timeline=timeline)
    elif current_user.role == 'doctor':
        # Doctor Dashboard View
        all_slots = list(slots_col.find({"doctor_id": current_user.id, "status": {"$in": ["available", "booked"]}}).sort("date", 1))
        
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
    today_date = datetime.date.today().strftime('%Y-%m-%d')
    
    return render_template('book_test.html', user=current_user, bookings=my_bookings, today_date=today_date)

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
        user_data = users_col.find_one({"_id": ObjectId(user_id)})
        verif = user_data.get('verification_details') if user_data else {}
        if not isinstance(verif, dict): verif = {}
        
        update_fields = {"status": "Approved"}
        if verif.get('full_name'):
            update_fields["full_name"] = verif.get('full_name')
        if verif.get('specialization'):
            update_fields["specialization"] = verif.get('specialization')
        if verif.get('phone_number'):
            update_fields["phone"] = verif.get('phone_number')
        if verif.get('previous_hospital'):
            update_fields["hospital_name"] = verif.get('previous_hospital')
        if verif.get('experience_years'):
            update_fields["experience"] = verif.get('experience_years')
            
        users_col.update_one({"_id": ObjectId(user_id)}, {"$set": update_fields})
        
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

@app.route('/uploaded_verification_file/<filename>')
@login_required
def uploaded_verification_file(filename):
    if current_user.role != 'admin':
        if not filename.startswith(str(current_user.id)):
            flash("Unauthorized access to document.")
            return redirect(url_for('dashboard'))
            
    try:
        file_data = db_manager.fs.get_last_version(filename=filename)
        content_type = getattr(file_data, 'content_type', None) or 'application/pdf'
        if filename.lower().endswith('.png'):
            content_type = 'image/png'
        elif filename.lower().endswith(('.jpg', '.jpeg')):
            content_type = 'image/jpeg'
            
        return send_file(
            io.BytesIO(file_data.read()),
            mimetype=content_type,
            as_attachment=False,
            download_name=filename
        )
    except Exception as e:
        flash("Sorry, this verification document could not be found.")
        return redirect(url_for('admin_verifications') if current_user.role == 'admin' else url_for('dashboard'))

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
        if request.is_json:
            return jsonify({"success": False, "message": "Only patients can book lab tests."}), 403
        return redirect(url_for('dashboard'))
    
    if not current_user.is_profile_complete:
        msg = 'Please complete your profile details before booking a lab technician.'
        if request.is_json:
            return jsonify({"success": False, "message": msg, "redirect": url_for('profile')}), 400
        flash(msg)
        return redirect(url_for('profile'))
        
    data = request.get_json(silent=True) or request.form
    
    test_name = data.get('test_name', 'Malaria Screening')
    date = data.get('date')
    time = data.get('time')
    address = data.get('address') or current_user.address
    notes = data.get('notes', '')
    test_price = str(data.get('test_price', '149'))
    amount_paid = str(data.get('amount_paid', test_price))
    payment_method = data.get('payment_method', 'UPI')
    payment_status = data.get('payment_status', 'Paid')
    transaction_id = data.get('transaction_id') or f"HLAB-{int(datetime.datetime.now().timestamp())}"
    
    patient_name = data.get('full_name') or current_user.full_name or current_user.username
    patient_email = data.get('patient_email') or current_user.email
    patient_dob = data.get('dob') or current_user.dob
    
    bookings_col = db_manager.get_collection('bookings')
    booking_doc = {
        "patient_id": current_user.id,
        "patient_name": patient_name,
        "patient_email": patient_email,
        "patient_dob": patient_dob,
        "test_name": test_name,
        "test_price": test_price,
        "amount_paid": amount_paid,
        "date": date,
        "time": time,
        "address": address,
        "notes": notes,
        "payment_method": payment_method,
        "payment_status": payment_status,
        "transaction_id": transaction_id,
        "booking_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "pending"
    }
    result = bookings_col.insert_one(booking_doc)
    booking_id = str(result.inserted_id)
    ref_id = booking_id[-6:].upper()
    
    # Send confirmation email
    try:
        conf_html = f"""
        <div style="font-family: 'Inter', system-ui, sans-serif; max-width: 600px; margin: 0 auto; padding: 24px; border: 1px solid #e2e8f0; border-radius: 16px; background: #ffffff;">
            <div style="text-align: center; margin-bottom: 24px;">
                <h2 style="color: #2563eb; margin: 0; font-size: 24px; font-weight: 800;">HealthLab AI</h2>
                <p style="color: #64748b; font-size: 14px; margin: 4px 0 0;">Home Sample Collection & Diagnostic Services</p>
            </div>
            
            <div style="background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 12px; padding: 18px; margin-bottom: 24px; text-align: center;">
                <div style="color: #16a34a; font-size: 20px; font-weight: 700; margin-bottom: 6px;">✓ Lab Test Booked Successfully!</div>
                <div style="color: #334155; font-size: 14px;">Booking Reference: <strong style="color: #0f172a; font-size: 15px;">#{ref_id}</strong></div>
            </div>
            
            <h4 style="margin: 0 0 12px; color: #0f172a; font-size: 16px;">Booking Summary</h4>
            <table style="width: 100%; border-collapse: collapse; margin-bottom: 24px; font-size: 14px;">
                <tr style="border-bottom: 1px solid #f1f5f9;">
                    <td style="padding: 10px 0; color: #64748b;">Patient Name:</td>
                    <td style="padding: 10px 0; font-weight: 600; color: #0f172a; text-align: right;">{patient_name}</td>
                </tr>
                <tr style="border-bottom: 1px solid #f1f5f9;">
                    <td style="padding: 10px 0; color: #64748b;">Diagnostic Test:</td>
                    <td style="padding: 10px 0; font-weight: 600; color: #0f172a; text-align: right;">{test_name}</td>
                </tr>
                <tr style="border-bottom: 1px solid #f1f5f9;">
                    <td style="padding: 10px 0; color: #64748b;">Scheduled Schedule:</td>
                    <td style="padding: 10px 0; font-weight: 600; color: #0f172a; text-align: right;">{date} • {time}</td>
                </tr>
                <tr style="border-bottom: 1px solid #f1f5f9;">
                    <td style="padding: 10px 0; color: #64748b;">Collection Address:</td>
                    <td style="padding: 10px 0; font-weight: 600; color: #0f172a; text-align: right;">{address}</td>
                </tr>
                <tr style="border-bottom: 1px solid #f1f5f9;">
                    <td style="padding: 10px 0; color: #64748b;">Payment Method:</td>
                    <td style="padding: 10px 0; font-weight: 600; color: #0f172a; text-align: right;">{payment_method} ({payment_status})</td>
                </tr>
                <tr>
                    <td style="padding: 12px 0; color: #0f172a; font-weight: 700; font-size: 15px;">Total Paid:</td>
                    <td style="padding: 12px 0; font-weight: 800; color: #2563eb; text-align: right; font-size: 18px;">₹{amount_paid}</td>
                </tr>
            </table>
            
            <div style="background: #eff6ff; border-left: 4px solid #2563eb; border-radius: 8px; padding: 14px 16px; margin-bottom: 24px;">
                <p style="margin: 0 0 6px; font-weight: 700; color: #1e40af; font-size: 14px;">Next Steps & Reports Notification</p>
                <p style="margin: 0; color: #1e3a8a; font-size: 13px; line-height: 1.5;">Our certified technician will arrive at your location to collect the sample. Once the laboratory finishes analysis, your digital reports will be uploaded directly to your HealthLab AI account and you will receive an instant notification.</p>
            </div>
            
            <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px; padding: 16px; text-align: center; color: #475569; font-size: 13px; line-height: 1.6;">
                <p style="margin: 0 0 6px; font-weight: 600; color: #0f172a;">A Reassuring Note For Your Health</p>
                <em>"A medical report is simply a compass for your health, not a verdict on your vitality. Taking charge of your health today is an act of self-care. You are in good hands!"</em>
            </div>
            
            <p style="color: #94a3b8; font-size: 12px; text-align: center; margin-top: 24px;">HealthLab AI • Smart Healthcare Systems</p>
        </div>
        """
        if patient_email:
            send_system_email("Lab Test Confirmed - HealthLab AI", f"Your lab test #{ref_id} is confirmed.", patient_email, conf_html)
    except Exception as e:
        print(f"Failed to send confirmation email: {e}")
        
    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({
            "success": True,
            "booking_id": booking_id,
            "ref_id": ref_id,
            "message": "Lab test booked successfully!"
        })
        
    flash('Lab test booked successfully!')
    return redirect(url_for('booking_history'))

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

@app.route('/book-appointment', methods=['GET', 'POST'])
@app.route('/book_appointment', methods=['GET', 'POST'])
@app.route('/book_doctor', methods=['GET', 'POST'])
@login_required
def book_appointment():
    if current_user.role != 'patient':
        if request.is_json:
            return jsonify({"success": False, "message": "Only patients can book doctor appointments."}), 403
        flash('Only registered patients can book doctor appointments.')
        return redirect(url_for('dashboard'))
    
    if request.method == 'GET':
        doctor_id = request.args.get('doctor_id', '').strip()
        users_col = db_manager.get_collection('users')
        doctors_col = db_manager.get_collection('doctors')
        
        doctors_from_users = list(users_col.find({
            "role": "doctor",
            "status": {"$ne": "Rejected"}
        }))
        doctors_from_doctors = list(doctors_col.find({
            "status": {"$ne": "Rejected"}
        }))
        
        seen_ids = set()
        doctors_list = []
        for doc in doctors_from_users + doctors_from_doctors:
            normalized = normalize_doctor(doc)
            if normalized['_id'] not in seen_ids:
                seen_ids.add(normalized['_id'])
                doctors_list.append(normalized)
                
        today_date = datetime.date.today().strftime('%Y-%m-%d')
        return render_template('book_appointment.html',
                               user=current_user,
                               doctors=doctors_list,
                               selected_doctor_id=doctor_id,
                               today_date=today_date)
                               
    # Handle POST submission (from AJAX multi-step wizard or form)
    data = request.get_json(silent=True) or request.form
    
    doctor_id = str(data.get('doctor_id') or '').strip()
    slot_id = data.get('slot_id')
    consultation_type = data.get('consultation_type', 'virtual') # 'virtual' or 'in_person'
    date = data.get('date') or datetime.date.today().strftime('%Y-%m-%d')
    time = data.get('time') or '10:00 AM'
    symptoms = data.get('symptoms', '')
    patient_phone = data.get('patient_phone') or current_user.phone or ''
    patient_email = data.get('patient_email') or current_user.email or ''
    patient_name = data.get('patient_name') or current_user.full_name or current_user.username
    fee_val = data.get('fee') or 500
    try:
        fee = int(float(fee_val))
    except (ValueError, TypeError):
        fee = 500
    payment_method = data.get('payment_method', 'UPI')
    payment_status = 'paid' if payment_method != 'Pay at Clinic' else 'pay_at_clinic'
    transaction_id = data.get('transaction_id') or f"HLAB-APT-{int(datetime.datetime.now().timestamp())}"

    # Lookup doctor details
    slots_col = db_manager.get_collection('slots')
    appointments_col = db_manager.get_collection('appointments')
    users_col = db_manager.get_collection('users')
    doctors_col = db_manager.get_collection('doctors')
    
    doc_record = None
    if ObjectId.is_valid(doctor_id):
        doc_record = users_col.find_one({"_id": ObjectId(doctor_id)}) or doctors_col.find_one({"_id": ObjectId(doctor_id)})
    if not doc_record and doctor_id:
        doc_record = users_col.find_one({"_id": doctor_id}) or doctors_col.find_one({"_id": doctor_id})
        
    doctor_name = "Medical Specialist"
    doctor_specialization = "Consultant"
    doctor_hospital = ""
    if doc_record:
        norm = normalize_doctor(doc_record)
        doctor_name = norm.get('full_name') or doctor_name
        doctor_specialization = norm.get('specialization') or doctor_specialization
        doctor_hospital = norm.get('hospital_name') or ""
        if not data.get('fee'):
            fee = norm.get('fee', 500)
    
    slot_obj_id = None
    if slot_id and ObjectId.is_valid(slot_id):
        slot_obj_id = ObjectId(slot_id)
        slot = slots_col.find_one({"_id": slot_obj_id})
        if slot and slot.get('doctor_name'):
            doctor_name = slot.get('doctor_name')
            
    appt_doc = {
        "doctor_id": doctor_id,
        "doctor_name": doctor_name,
        "doctor_specialization": doctor_specialization,
        "doctor_hospital": doctor_hospital,
        "patient_id": current_user.id,
        "patient_name": patient_name,
        "patient_email": patient_email,
        "patient_phone": patient_phone,
        "date": date,
        "time": time,
        "status": "scheduled",
        "consultation_type": consultation_type, # 'virtual' or 'in_person'
        "symptoms": symptoms,
        "fee": fee,
        "payment_method": payment_method,
        "payment_status": payment_status,
        "transaction_id": transaction_id,
        "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    if slot_obj_id:
        appt_doc["slot_id"] = slot_obj_id
        
    appt_res = appointments_col.insert_one(appt_doc)
    appt_id = str(appt_res.inserted_id)
    ref_id = f"HL-APT-{int(datetime.datetime.now().timestamp()) % 100000:05d}"
    
    if slot_obj_id:
        slots_col.update_one({"_id": slot_obj_id}, {"$set": {
            "status": "booked",
            "patient_id": current_user.id,
            "patient_name": patient_name,
            "appointment_id": appt_res.inserted_id
        }})
    else:
        # Also create a booked slot entry so the doctor's calendar tracks this appointment
        try:
            slots_col.insert_one({
                "doctor_id": ObjectId(doctor_id) if ObjectId.is_valid(doctor_id) else doctor_id,
                "doctor_name": doctor_name,
                "date": date,
                "time": time,
                "status": "booked",
                "patient_id": current_user.id,
                "patient_name": patient_name,
                "appointment_id": appt_res.inserted_id
            })
        except Exception:
            pass

    # Send confirmation email
    try:
        mode_text = "Virtual Video Consultation (HD Video Room opens 10m before slot)" if consultation_type == 'virtual' else f"In-Person Clinic Visit ({doctor_hospital or 'Medical Center'})"
        conf_html = f"""
        <div style="font-family: 'Inter', system-ui, sans-serif; max-width: 600px; margin: 0 auto; padding: 24px; border: 1px solid #e2e8f0; border-radius: 0px; background: #ffffff;">
            <div style="text-align: center; margin-bottom: 24px;">
                <h2 style="color: #2563eb; margin: 0; font-size: 24px; font-weight: 800;">HealthLab AI</h2>
                <p style="color: #64748b; font-size: 14px; margin-top: 4px;">Doctor Consultation Booking Confirmed</p>
            </div>
            <div style="background: #f8fafc; border: 1px solid #e2e8f0; padding: 16px; margin-bottom: 20px;">
                <p style="margin: 0 0 8px; font-size: 13px; color: #64748b; font-weight: 700;">APPOINTMENT REFERENCE: <span style="color: #0f172a; font-weight: 800;">#{ref_id}</span></p>
                <p style="margin: 0; font-size: 16px; font-weight: 800; color: #0f172a;">Dr. {doctor_name} ({doctor_specialization})</p>
                <p style="margin: 4px 0 0; font-size: 13px; color: #475569;">Mode: <strong>{mode_text}</strong></p>
                <p style="margin: 4px 0 0; font-size: 13px; color: #475569;">Date & Time: <strong>{date} at {time}</strong></p>
                <p style="margin: 4px 0 0; font-size: 13px; color: #475569;">Consultation Fee: <strong>₹{fee} ({payment_status.upper()})</strong></p>
            </div>
            <div style="background: #eff6ff; border-left: 4px solid #2563eb; padding: 14px 16px; margin-bottom: 20px;">
                <p style="margin: 0 0 6px; font-weight: 700; color: #1e40af; font-size: 14px;">A Reassuring Message From HealthLab Care</p>
                <p style="margin: 0; color: #1e3a8a; font-size: 13px; line-height: 1.5;">"Taking proactive steps towards your health is an act of courage and self-love. Our medical experts are dedicated to empathetic, precise care to guide you back to optimum vitality."</p>
            </div>
        </div>
        """
        if patient_email:
            send_system_email("Doctor Appointment Confirmed - HealthLab AI", f"Your appointment #{ref_id} with Dr. {doctor_name} is confirmed.", patient_email, conf_html)
    except Exception as e:
        print(f"Failed to send appointment confirmation email: {e}")

    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({
            "success": True,
            "message": "Appointment booked successfully!",
            "appointment_id": appt_id,
            "ref_id": ref_id,
            "doctor_name": doctor_name,
            "consultation_type": consultation_type,
            "date": date,
            "time": time,
            "redirect": url_for('dashboard')
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
    
    med_names = request.form.getlist('med_name[]')
    med_dosages = request.form.getlist('med_dosage[]')
    med_freqs = request.form.getlist('med_freq[]')
    med_durations = request.form.getlist('med_duration[]')
    med_instructions = request.form.getlist('med_instructions[]')
    
    medicines = []
    for i in range(len(med_names)):
        if med_names[i].strip():
            medicines.append({
                "name": med_names[i],
                "dosage": med_dosages[i] if i < len(med_dosages) else '',
                "frequency": med_freqs[i] if i < len(med_freqs) else '',
                "duration": med_durations[i] if i < len(med_durations) else '',
                "instructions": med_instructions[i] if i < len(med_instructions) else ''
            })
    
    prescriptions_col = db_manager.get_collection('prescriptions')
    appointments_col = db_manager.get_collection('appointments')
    
    appt = appointments_col.find_one({"_id": ObjectId(appointment_id)})
    patient_id = appt.get('patient_id') if appt else request.form.get('patient_id')
    
    prescriptions_col.insert_one({
        "appointment_id": ObjectId(appointment_id),
        "doctor_id": current_user.id,
        "patient_id": patient_id,
        "diagnosis": diagnosis,
        "medicines": medicines,
        "date": datetime.datetime.now()
    })
    
    appointments_col.update_one({"_id": ObjectId(appointment_id)}, {"$set": {"status": "completed"}})
    
    # Retire the linked slot to "completed" so it automatically disappears from the active Manage Slots view
    slots_col = db_manager.get_collection('slots')
    if appt and appt.get('slot_id'):
        slots_col.update_one({"_id": ObjectId(appt['slot_id'])}, {"$set": {"status": "completed"}})
    elif appt:
        slots_col.update_one({
            "doctor_id": appt.get('doctor_id'),
            "date": appt.get('date'),
            "time": appt.get('time'),
            "status": "booked"
        }, {"$set": {"status": "completed"}})
        
    flash('Prescription sent!')
    return redirect(url_for('dashboard'))

@app.route('/view_prescription/<appt_id>')
@login_required
def view_prescription(appt_id):
    prescriptions_col = db_manager.get_collection('prescriptions')
    prescription = prescriptions_col.find_one({"appointment_id": ObjectId(appt_id)})
    return render_template('prescription.html', prescription=prescription)

@app.route('/profile_pic/<filename>')
def get_profile_pic(filename):
    try:
        file_data = db_manager.fs.get_last_version(filename=filename)
        return send_file(
            io.BytesIO(file_data.read()),
            mimetype=file_data.content_type,
            as_attachment=False
        )
    except gridfs.errors.NoFile:
        return "", 404

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
                filename = secure_filename(f"profile_{current_user.id}_{file.filename}")
                db_manager.fs.put(file.read(), filename=filename, content_type=file.mimetype)
                update_data["profile_pic"] = url_for('get_profile_pic', filename=filename)

        col.update_one({"_id": ObjectId(current_user.id)}, {"$set": update_data})
        flash('Profile updated!')
        return redirect(url_for('dashboard'))
    return render_template('profile.html', user=current_user)

@app.route('/search')
@login_required
def search():
    query = request.args.get('q', '').strip()
    if not query:
        return render_template('search_results.html', query=query, results={'doctors': [], 'reports': [], 'bookings': [], 'users': []})
        
    search_regex = {"$regex": query, "$options": "i"}
    or_conditions = [
        {"full_name": search_regex},
        {"username": search_regex},
        {"specialization": search_regex},
        {"hospital_name": search_regex},
        {"verification_details.full_name": search_regex},
        {"verification_details.specialization": search_regex},
        {"verification_details.previous_hospital": search_regex}
    ]
    
    # Also handle "dentist" / "dental" queries
    if 'dent' in query.lower():
        or_conditions.extend([
            {"specialization": {"$regex": "dent", "$options": "i"}},
            {"verification_details.specialization": {"$regex": "dent", "$options": "i"}}
        ])
        
    doc_match_condition = {"$or": or_conditions}
    
    doctors_from_users = list(db_manager.get_collection('users').find({
        "role": "doctor",
        "status": {"$ne": "Rejected"},
        **doc_match_condition
    }))
    doctors_from_doctors = list(db_manager.get_collection('doctors').find({
        "status": {"$ne": "Rejected"},
        **doc_match_condition
    }))
    
    seen_ids = set()
    doctors_results = []
    for doc in doctors_from_users + doctors_from_doctors:
        norm = normalize_doctor(doc)
        if norm['_id'] not in seen_ids:
            seen_ids.add(norm['_id'])
            doctors_results.append(norm)
            
    reports = []
    bookings = []
    if current_user.role == 'patient':
        try:
            reports = list(db_manager.get_collection('reports').find({
                "patient_id": current_user.id,
                "description": search_regex
            }))
            for r in reports: r['_id'] = str(r['_id'])
        except Exception:
            reports = []
            
        try:
            bookings = list(db_manager.get_collection('bookings').find({
                "patient_id": current_user.id,
                "test_name": search_regex
            }))
            for b in bookings: b['_id'] = str(b['_id'])
        except Exception:
            bookings = []
        
    return render_template('search_results.html', query=query, results={
        'doctors': doctors_results,
        'reports': reports,
        'bookings': bookings,
        'users': []
    })


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
    appointments_col = db_manager.get_collection('appointments')

    # Only fetch active slots (available or booked, excluding completed)
    my_slots = list(slots_col.find({
        "doctor_id": current_user.id,
        "status": {"$in": ["available", "booked"]}
    }).sort("date", 1))

    # Enrich booked slots with patient info if missing
    for slot in my_slots:
        if slot.get('status') == 'booked':
            if not slot.get('patient_name'):
                appt = appointments_col.find_one({
                    "doctor_id": current_user.id,
                    "date": slot.get('date'),
                    "time": slot.get('time'),
                    "status": "scheduled"
                })
                if appt:
                    slot['patient_name'] = appt.get('patient_name')
                    slot['appointment_id'] = str(appt.get('_id'))

    available_count = sum(1 for s in my_slots if s.get('status') == 'available')
    booked_count = sum(1 for s in my_slots if s.get('status') == 'booked')
    total_count = len(my_slots)

    return render_template(
        'manage_slots.html',
        user=current_user,
        slots=my_slots,
        total_count=total_count,
        available_count=available_count,
        booked_count=booked_count
    )

@app.route('/ai_assistant')
@login_required
def ai_assistant():
    reports_col = db_manager.get_collection('reports')
    bookings_col = db_manager.get_collection('bookings')
    
    my_reports = []
    if current_user.role == 'patient':
        reports_cursor = reports_col.find({"patient_id": current_user.id}).sort("_id", -1)
        for rep in reports_cursor:
            booking = bookings_col.find_one({"_id": ObjectId(rep['booking_id'])}) if 'booking_id' in rep and rep['booking_id'] else None
            test_name = booking.get('test_name', 'Laboratory Analysis') if booking else 'Laboratory Analysis'
            my_reports.append({
                "_id": str(rep['_id']),
                "test_name": test_name,
                "timestamp": rep.get('timestamp', 'Recent'),
                "description": rep.get('description', ''),
                "pdf_url": rep.get('pdf_url', '')
            })
            
    selected_report_id = request.args.get('report_id', '').strip()
    return render_template('ai_assistant.html', user=current_user, reports=my_reports, selected_report_id=selected_report_id)

chatbot_pipeline = None

import requests

@app.route('/api/chat', methods=['POST'])
@login_required
def api_chat():
    data = request.get_json()
    user_message = (data.get('message') or '').strip() if data else ''
    report_id = (data.get('report_id') or '').strip() if data else ''
    
    if not user_message and not report_id:
        return jsonify({"error": "No message or report provided"}), 400

    report_context = ""
    attached_report_info = None
    if report_id:
        try:
            reports_col = db_manager.get_collection('reports')
            bookings_col = db_manager.get_collection('bookings')
            
            query = {"_id": ObjectId(report_id)}
            if current_user.role == 'patient':
                query["patient_id"] = current_user.id
                
            report_doc = reports_col.find_one(query)
            if report_doc:
                booking = bookings_col.find_one({"_id": ObjectId(report_doc.get('booking_id'))}) if report_doc.get('booking_id') else None
                test_name = booking.get('test_name', 'Laboratory Diagnostic Test') if booking else 'Laboratory Diagnostic Test'
                
                # Extract text from GridFS PDF if present
                pdf_text = ""
                file_data = None
                try:
                    if 'file_id' in report_doc and report_doc['file_id']:
                        file_data = db_manager.fs.get(ObjectId(report_doc['file_id']))
                except Exception as fid_err:
                    print("Notice: GridFS get by file_id error:", fid_err)

                if not file_data and 'pdf_url' in report_doc and report_doc['pdf_url']:
                    try:
                        file_data = db_manager.fs.get_last_version(filename=report_doc['pdf_url'])
                    except Exception as fn_err:
                        try:
                            f_doc = db_manager.get_collection('fs.files').find_one({"filename": report_doc['pdf_url']})
                            if f_doc:
                                file_data = db_manager.fs.get(f_doc['_id'])
                        except:
                            pass

                if file_data:
                    try:
                        import pypdf
                        reader = pypdf.PdfReader(io.BytesIO(file_data.read()))
                        page_texts = []
                        for p in reader.pages[:8]:
                            txt = p.extract_text()
                            if txt:
                                page_texts.append(txt.strip())
                        pdf_text = "\n".join(page_texts).strip()
                    except Exception as p_err:
                        print("Notice: PDF text parsing error:", p_err)
                
                report_context = f"\n\n[ATTACHED CLINICAL REPORT DETAILS]\n- Test Name: {test_name}\n- Date Uploaded: {report_doc.get('timestamp', 'N/A')}\n- Laboratory/Technician Notes: {report_doc.get('description', 'Standard diagnostic review completed')}"
                if pdf_text:
                    clean_pdf = " ".join(pdf_text.split())
                    report_context += f"\n- Extracted PDF Document Text:\n\"\"\"\n{clean_pdf[:3000]}\n\"\"\""
                else:
                    report_context += "\n- Extracted PDF Document Text: [No embedded text layer in PDF - analyze based on test parameters and technician notes]"
                report_context += "\n[END OF REPORT DETAILS]\n"
                
                attached_report_info = {
                    "id": str(report_doc['_id']),
                    "test_name": test_name,
                    "timestamp": report_doc.get('timestamp', '')
                }
        except Exception as r_err:
            print("Error retrieving report for AI analysis:", r_err)

    try:
        # Check for API Key
        groq_token = os.getenv("GROQ_API_KEY")
        if not groq_token:
            return jsonify({"error": "Missing API Key", "details": "Please add GROQ_API_KEY to your .env file."}), 500

        API_URL = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {groq_token}",
            "Content-Type": "application/json"
        }

        # Available high-performance models on Groq with fallback
        default_model = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
        candidate_models = [default_model, "openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b", "groq/compound-mini"]
        models_to_try = []
        for m in candidate_models:
            if m and m not in models_to_try:
                models_to_try.append(m)

        if report_context:
            system_prompt = (
                "You are a Senior Medical AI Specialist for HealthLab AI. "
                "The patient has provided their diagnostic lab test report along with the extracted text from their uploaded PDF document.\n"
                "Your job is to thoroughly analyze the PDF contents, laboratory observations, and test parameters.\n"
                "Provide a detailed, well-structured clinical analysis formatted with markdown headers and bullet points:\n\n"
                "### 1. Test Overview\n"
                "Explain what this test evaluates and what report was uploaded.\n\n"
                "### 2. Analysis of Report & Document Content\n"
                "Analyze the extracted text from the PDF document and the lab technician notes. Highlight key values, observations, indicators, or findings (normal, elevated, or abnormal).\n\n"
                "### 3. What This Means For You\n"
                "Translate the medical parameters and notes into clear, calm, and easily understandable language.\n\n"
                "### 4. Recommended Next Steps & Lifestyle Guidance\n"
                "Provide practical diet, wellness, or precautionary measures, and outline what the patient should discuss with their physician.\n\n"
                "Maintain a reassuring, professional, and compassionate medical tone throughout."
            )
            max_tokens = 850
            if not user_message:
                full_user_content = f"Please thoroughly analyze my attached diagnostic report and explain all findings, values, and notes from the PDF document.{report_context}"
            else:
                full_user_content = f"{user_message}\n\nPlease read and thoroughly analyze the following attached report data and PDF document content:\n{report_context}"
        else:
            system_prompt = (
                "You are a highly intelligent, empathetic, and professional medical AI assistant for HealthLab AI. "
                "Greet the user naturally. Keep your answers brief, clear, and informative (maximum 2-4 short sentences). "
                "Provide helpful medical insights, but always remind the user to consult a real doctor for serious issues."
            )
            max_tokens = 256
            full_user_content = user_message

        last_error = None
        for model in models_to_try:
            payload = {
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": system_prompt
                    },
                    {
                        "role": "user",
                        "content": full_user_content
                    }
                ],
                "temperature": 0.5 if report_context else 0.7,
                "max_tokens": max_tokens,
                "top_p": 0.95
            }

            response = requests.post(API_URL, headers=headers, json=payload)
            
            if response.status_code == 200:
                output_data = response.json()
                reply = output_data["choices"][0]["message"]["content"].strip()
                return jsonify({
                    "reply": reply, 
                    "model": model,
                    "attached_report": attached_report_info
                })
            else:
                last_error = response.json()
                error_code = last_error.get('error', {}).get('code', '')
                if error_code == 'model_not_found':
                    continue
                else:
                    return jsonify({"error": "API Error", "details": str(last_error)}), 500

        return jsonify({"error": "API Error", "details": str(last_error)}), 500

    except Exception as e:
        print(f"Chatbot Error: {e}")
        return jsonify({"error": "Network error while connecting to AI service.", "details": str(e)}), 500


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
    # Test DB connection on start but don't block app startup
    success, hint = db_manager.test_connection()
    if not success:
        print(f"Warning: Database connection failed during startup: {hint}")
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True, use_reloader=True)
