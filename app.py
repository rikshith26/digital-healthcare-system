import os
import sys

# Define the absolute path to the web_app directory
web_app_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web_app')

# Explicitly change the current working directory to web_app before running anything
# This ensures Flask finds "templates" and "static" exactly as if we navigated there.
os.chdir(web_app_dir)
sys.path.insert(0, web_app_dir)

# Import and run the app from web_app/app.py
from app import app, db_manager

if __name__ == '__main__':
    # Test DB connection on start
    success, hint = db_manager.test_connection()
    if success:
        app.run(debug=True, port=int(os.getenv("PORT", 5000)))
    else:
        if hint:
            print(hint)
        print("Could not start app: Database connection failed.")
