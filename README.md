# HealthLab AI

HealthLab AI is a digital healthcare platform designed to facilitate coordination among medical professionals, patients, and laboratory technicians. The platform streamlines medical appointments, document management, and patient communication.

### Features

#### Patient Portal
* Appointment Scheduling: Book consultations with available medical professionals.
* Digital Payments: Process consultation fees securely online.
* Automated Assistance: Basic health inquiries handled by an integrated AI interface.
* Medical Records: Access laboratory results and digital prescriptions securely.
* Legal Compliance: Integrated mandatory Terms and Conditions and Privacy Policy agreements.

#### Medical Professional Portal
* Dashboard Interface: Centralized view of upcoming appointments and schedules.
* Prescription Management: Generate and download formatted digital prescriptions.
* Financial Tracking: Monitor consultation revenue.
* Schedule Management: Configure availability and consultation slots.

#### Laboratory Technician Portal
* Request Management: Review and accept home sample collection requests.
* Document Upload: Upload laboratory reports directly to patient profiles.

### Setup Instructions
1. Install dependencies: `pip install -r requirements.txt`
2. Configure environment variables in `.env` (including MongoDB URI).
3. Start the application: `python app.py`
4. Access the platform at `http://127.0.0.1:5000`.
