import sqlite3
import os

DB_DIR = "/DATA/my_prog/docu_motion/data"

def migrate_db():
    for filename in os.listdir(DB_DIR):
        if filename.endswith(".db"):
            db_path = os.path.join(DB_DIR, filename)
            print(f"Migrating: {db_path}")
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            cursor.execute("PRAGMA table_info(slides)")
            columns = [col[1] for col in cursor.fetchall()]

            if "slide_type" not in columns:
                cursor.execute("ALTER TABLE slides ADD COLUMN slide_type VARCHAR(20) DEFAULT 'image'")
                print("  + slide_type")
            if "video_filename" not in columns:
                cursor.execute("ALTER TABLE slides ADD COLUMN video_filename VARCHAR(300) DEFAULT ''")
                print("  + video_filename")
            if "volume" not in columns:
                cursor.execute("ALTER TABLE slides ADD COLUMN volume FLOAT DEFAULT 1.0")
                print("  + volume")
            if "subtitles" not in columns:
                cursor.execute("ALTER TABLE slides ADD COLUMN subtitles TEXT DEFAULT '[]'")
                print("  + subtitles")

            conn.commit()
            conn.close()
            print(f"Done: {db_path}")

if __name__ == "__main__":
    migrate_db()
