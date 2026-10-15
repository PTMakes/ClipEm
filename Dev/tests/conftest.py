import os
import sys

DEV_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_INSTALLER_DIR = os.path.join(os.path.dirname(DEV_DIR), "PTMClipEm-App", "App", "Shared", "Installer")
sys.path.insert(0, DEV_DIR)
sys.path.insert(0, APP_INSTALLER_DIR)
