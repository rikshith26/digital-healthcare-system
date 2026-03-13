import os
import certifi
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

class Database:
    def __init__(self):
        self.uri = os.getenv("MONGO_URI")
        
        # Default to 'digital_healthcare' as that is where existing users are stored
        db_name = 'digital_healthcare'
        try:
            # Check if there's a specific database in the URI that isn't empty
            parsed_path = self.uri.split('://')[1].split('/')
            if len(parsed_path) > 1:
                potential_db = parsed_path[1].split('?')[0]
                if potential_db and potential_db != 'mediscan_db': 
                    # Only override if it's NOT the default mediscan_db from the new URI
                    db_name = potential_db
        except:
            pass

        self.client = MongoClient(
            self.uri, 
            tlsCAFile=certifi.where(),
            connect=False,
            # Robust settings for varied network conditions
            tlsAllowInvalidCertificates=True, 
            tlsAllowInvalidHostnames=True,
            serverSelectionTimeoutMS=20000,   # Increased to 20s
            heartbeatFrequencyMS=10000,
            socketTimeoutMS=20000,
            connectTimeoutMS=20000,
            retryWrites=True
        )
        self.db = self.client[db_name]

    def get_collection(self, name):
        return self.db[name]

    def test_connection(self):
        max_retries = 5 # Increased retries
        last_error = ""
        
        for attempt in range(max_retries):
            try:
                # Force connection check
                self.client.admin.command('ping')
                print(f"✅ Database connected successfully (Attempt {attempt + 1})")
                return True, None
            except Exception as e:
                last_error = str(e)
                print(f"⚠️ Connection attempt {attempt + 1} failed: {last_error}")
                if attempt < max_retries - 1:
                    import time
                    time.sleep(2) # Wait longer before retry
        
        # If all retries fail, determine if it's an IP whitelist issue
        is_whitelist_issue = any(x in last_error.upper() for x in ["TLSV1_ALERT_INTERNAL_ERROR", "SSL HANDSHAKE FAILED", "TIMEOUT"])
        
        hint = ""
        if is_whitelist_issue:
            current_ip = "Unknown"
            try:
                import requests
                current_ip = requests.get("https://api.ipify.org", timeout=5).text
            except:
                try:
                    import subprocess
                    current_ip = subprocess.check_output(["curl.exe", "-s", "ifconfig.me"], timeout=5).decode().strip()
                except: pass

            hint = (f"\n{'!'*60}\n"
                    " CRITICAL: DATABASE CONNECTION REJECTED\n"
                    f"{'!'*60}\n"
                    "Your device's IP address is likely not whitelisted in MongoDB Atlas.\n\n"
                    f"👉 YOUR CURRENT IP: {current_ip}\n\n"
                    "HOW TO FIX THIS IN 30 SECONDS:\n"
                    "1. Go to: cloud.mongodb.com\n"
                    "2. Click 'Network Access' (on the left sidebar)\n"
                    "3. Click '+ ADD IP ADDRESS'\n"
                    "4. Click 'ALLOW ACCESS FROM ANYWHERE' (for 0.0.0.0/0) \n"
                    "   OR Paste your IP: {current_ip}\n"
                    "5. Click 'Confirm' and wait 60 seconds.\n"
                    f"{'!'*60}\n")
        
        return False, f"Error: {last_error}\n{hint}"

db_manager = Database()
