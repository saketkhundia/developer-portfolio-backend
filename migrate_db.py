#!/usr/bin/env python3
"""
Migrate existing database to add device tracking columns
"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "users.db")

def migrate():
    print("=" * 60)
    print("Database Migration: Adding Device Tracking")
    print("=" * 60)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        # Check if columns already exist
        cursor.execute("PRAGMA table_info(sessions)")
        columns = [row[1] for row in cursor.fetchall()]
        
        print(f"\nExisting columns: {columns}")
        
        # Add new columns if they don't exist
        new_columns = [
            ("device_name", "TEXT"),
            ("device_type", "TEXT"),
            ("ip_address", "TEXT"),
            ("user_agent", "TEXT"),
            ("is_active", "INTEGER DEFAULT 1")
        ]
        
        added = []
        for col_name, col_type in new_columns:
            if col_name not in columns:
                try:
                    cursor.execute(f"ALTER TABLE sessions ADD COLUMN {col_name} {col_type}")
                    added.append(col_name)
                    print(f"✓ Added column: {col_name}")
                except sqlite3.OperationalError as e:
                    print(f"⚠ Could not add {col_name}: {e}")
        
        if not added:
            print("\n✓ All columns already exist - no migration needed")
        else:
            conn.commit()
            print(f"\n✓ Successfully added {len(added)} columns")
        
        # Verify final schema
        cursor.execute("PRAGMA table_info(sessions)")
        final_columns = [row[1] for row in cursor.fetchall()]
        print(f"\nFinal columns: {final_columns}")
        
        print("\n" + "=" * 60)
        print("✓ Migration Complete!")
        print("=" * 60)
        
    except Exception as e:
        print(f"\n✗ Migration failed: {e}")
        conn.rollback()
    finally:
        conn.close()

if __name__ == "__main__":
    if os.path.exists(DB_PATH):
        migrate()
    else:
        print("Database not found. It will be created with correct schema on first run.")
