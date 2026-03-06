"""
Migration script to add profile_picture_url column to users table
"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "users.db")

def migrate():
    """Add profile_picture_url column if it doesn't exist"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Check if column exists
        cursor.execute("PRAGMA table_info(users)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'profile_picture_url' not in columns:
            print("Adding profile_picture_url column...")
            cursor.execute("""
                ALTER TABLE users 
                ADD COLUMN profile_picture_url TEXT
            """)
            conn.commit()
            print("✓ Migration successful: profile_picture_url column added")
        else:
            print("✓ profile_picture_url column already exists")
        
        conn.close()
    except Exception as e:
        print(f"✗ Migration failed: {e}")
        raise

if __name__ == "__main__":
    migrate()
