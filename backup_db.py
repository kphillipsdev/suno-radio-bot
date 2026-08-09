#!/usr/bin/env python3
import os
import shutil
import sqlite3
import datetime
import glob
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
DB_PATH = os.getenv("SUNO_RADIO_DB", os.path.join(_SCRIPT_DIR, "suno_radio.db"))
BACKUP_DIR = os.path.join(_SCRIPT_DIR, ".backups")
RETENTION_DAYS = 30

def backup():
    # Ensure backup directory exists
    os.makedirs(BACKUP_DIR, exist_ok=True)
    
    # Generate backup filename with timestamp
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_file = os.path.join(BACKUP_DIR, f"suno_radio_backup_{timestamp}.db")
    
    print(f"Starting backup of {DB_PATH} to {backup_file}...")
    
    try:
        # Use sqlite3's backup API for a safe backup of a live database
        src_conn = sqlite3.connect(DB_PATH)
        dst_conn = sqlite3.connect(backup_file)
        
        with dst_conn:
            src_conn.backup(dst_conn)
            
        dst_conn.close()
        src_conn.close()
        
        print("Backup completed successfully.")
    except Exception as e:
        print(f"Error during backup: {e}")
        return

    # Cleanup old backups
    cleanup()

def cleanup():
    print(f"Cleaning up backups older than {RETENTION_DAYS} days...")
    now = datetime.datetime.now()
    
    # Get all backup files
    backup_files = glob.glob(os.path.join(BACKUP_DIR, "suno_radio_backup_*.db"))
    
    for f in backup_files:
        file_path = Path(f)
        # Check file age based on creation/modification time
        file_time = datetime.datetime.fromtimestamp(file_path.stat().st_mtime)
        age = now - file_time
        
        if age.days >= RETENTION_DAYS:
            try:
                os.remove(f)
                print(f"Deleted old backup: {f}")
            except Exception as e:
                print(f"Error deleting {f}: {e}")

if __name__ == "__main__":
    backup()

