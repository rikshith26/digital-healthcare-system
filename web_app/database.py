import os
import certifi
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

class Database:
    def __init__(self):
        self.uri = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
        
        db_name = 'digital_healthcare'
        try:
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
            print("âœ… Database connected successfully to Atlas!")
            return True, None
        except Exception as e:
            error_msg = str(e)
            print(f"âš ï¸ Atlas Connection Attempt Failed: {error_msg}")
            
            is_whitelist_issue = any(x in error_msg.upper() for x in ["TLSV1_ALERT_INTERNAL_ERROR", "SSL HANDSHAKE FAILED", "TIMEOUT"])
            
            if is_whitelist_issue:
                print("\n" + "="*60)
                print(" ATLAS CONNECTION BLOCKED (LIKELY IP WHITELIST ISSUES)")
                print("="*60)

            print("\n------------------------------------------------------------")
            print("ðŸ”„ FALLING BACK TO LOCAL MONGODB (localhost:27017) ðŸ”„")
            print("------------------------------------------------------------")
            try:
                self.uri = "mongodb://127.0.0.1:27017/"
                self.client = MongoClient(self.uri, serverSelectionTimeoutMS=2000)
                self.db = self.client['digital_healthcare']
                self.client.admin.command('ping')
                print("âœ… Database connected successfully to Local MongoDB!")
                return True, None
            except Exception as e2:
                print(f"âš ï¸ Local MongoDB connection failed: {e2}")
                
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
                        f"ðŸ‘‰ YOUR CURRENT IP: {current_ip}\n\n"
                        "HOW TO FIX THIS IN 30 SECONDS:\n"
                        "1. Go to: cloud.mongodb.com\n"
                        "2. Click 'Network Access' (on the left sidebar)\n"
                        "3. Click '+ ADD IP ADDRESS'\n"
                        "4. Click 'ALLOW ACCESS FROM ANYWHERE' (for 0.0.0.0/0) \n"
                        f"   OR Paste your IP: {current_ip}\n"
                        "5. Click 'Confirm' and wait 60 seconds.\n"
                        f"{'!'*60}\n")
            
            return False, f"Could not connect to Atlas or Local MongoDB.\n{hint}"

db_manager = Database()
