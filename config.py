import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "board_firmware.db"))

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-in-production")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "PDF2014$")
