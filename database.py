import os
import certifi
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

class Database:
    def __init__(self):
        self.uri = os.getenv("MONGO_URI")
        # Simplified configuration for better compatibility with Python 3.13 on Windows.
        # - Defaulting to standard SRV behavior (no explicit certificate flags)
        # - Using connect=False to prevent hangs on instantiation
        self.client = MongoClient(
            self.uri, 
            connect=False,
            tlsAllowInvalidCertificates=True, # Bypasses most handshake alerts
            tlsAllowInvalidHostnames=True,     # Compatibility for some networks
            serverSelectionTimeoutMS=5000     # Graceful timeout
        )
        self.db = self.client['digital_healthcare'] 

    def get_collection(self, name):
        return self.db[name]

    def test_connection(self):
        try:
            # Short timeout to avoid hanging if blocked
            self.client.admin.command('ping')
            print("Pinged your deployment. You successfully connected to MongoDB!")
            return True, None
        except Exception as e:
            error_msg = str(e)
            print(f"Connection failed: {error_msg}")
            
            # Specific hint for Atlas "Internal Error"
            if "TLSV1_ALERT_INTERNAL_ERROR" in error_msg:
                hint = ("\n----------------------------------------------------\n"
                        "🚩 TROUBLESHOOT: Atlas TLS Alert (Internal Error)\n"
                        "----------------------------------------------------\n"
                        "This is likely an IP Whitelist issue. Please:\n"
                        "1. Go to your MongoDB Atlas Dashboard.\n"
                        "2. Select 'Network Access'\n"
                        "3. Click 'Add IP Address' -> 'Add Current IP Address'\n"
                        "4. Ensure your local firewall/VPN is not blocking TCP 27017.\n"
                        "----------------------------------------------------\n")
                return False, hint
            
            return False, error_msg

db_manager = Database()
