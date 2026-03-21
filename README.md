# HealAI - Next-Generation Digital Healthcare Platform

HealAI (formerly HealthLab AI) is a premium, modern healthcare web application designed to bridge the gap between patients, medical professionals, and AI-driven health insights. It offers a sleek, glassmorphic UI where users can seamlessly book tests, view results, and manage administrative capabilities securely.

## 🌟 Key Features

* **Multi-Role Authentication System:**
  * **Patients:** Can securely register, update profiles, book home-collection lab tests, and view historical medical reports.
  * **Doctors & Technicians:** Dedicated dashboards to accept pending bookings, mark samples as collected, and securely upload PDF medical reports directly to patient profiles.
  * **Super Admins:** An exclusive, private Admin Dashboard accessible only to the 5 core owners. It features comprehensive platform statistics, live test tracking, and a unified User Management Directory.

* **Beautiful, Modern Aesthetics:**
  * Fully redesigned with the 'HealAI Light' aesthetic, featuring pristine white cards, dynamic hover effects, and a highly responsive layout.
  * Integration with Lucide Icons and Inter typography for a clean, professional feel.

* **Robut Backend Architecture:**
  * Built on Python Flask with a centralized `app.py` architecture.
  * Cloud Database powered by MongoDB Atlas, equipped with an intelligent automatic fallback to Local Server (`localhost:27017`) if ISP or IP whitelist issues block the Atlas connection.

## 🚀 How to Run Locally

1. **Install Python:** Ensure Python 3.x is installed on your machine.
2. **Clone/Download the Code:** Ensure you are in the root directory containing `app.py`.
3. **Install Dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
4. **Setup Environment Variables:** Ensure you have a `.env` file inside the `web_app/` directory containing your `MONGO_URI` and `SECRET_KEY`.
5. **Start the Application:**
   Run the following command from the root directory:
   ```bash
   python app.py
   ```
6. **Access the Platform:** Open your browser and navigate to `http://127.0.0.1:5000`.

## 🛠️ Tech Stack

* **Backend:** Python (Flask), Flask-Login, Werkzeug Security
* **Database:** MongoDB (PyMongo)
* **Frontend:** HTML5, Modern CSS (TailwindCSS framework via CDN), Vanilla JavaScript
* **Icons & Fonts:** Lucide Icons, Google Fonts (Inter)

---
*Built with precision to modernize the patient experience.*
