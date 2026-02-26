import os
import certifi
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

class Database:
    def __init__(self):
        self.uri = os.getenv("MONGO_URI")
        self.client = MongoClient(self.uri, tlsCAFile=certifi.where())
        # Force the database name to digital_healthcare for the review
        self.db = self.client['digital_healthcare'] 

    def get_collection(self, name):
        return self.db[name]

    def test_connection(self):
        try:
            self.client.admin.command('ping')
            print("Pinged your deployment. You successfully connected to MongoDB!")
            return True
        except Exception as e:
            print(f"Connection failed: {e}")
            return False

db_manager = Database()
