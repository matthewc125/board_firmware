import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Curated production database (51 tracked boards). Used by the app and static site.
DATABASE = os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "board_firmware.db"))

# Side archive with all sources and pruned boards — browse only, not the default app DB.
ARCHIVE_DATABASE = os.path.join(BASE_DIR, "board_firmware_full.db")

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-in-production")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "PDF2014$")
