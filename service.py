"""Run only while Kodi is awake. Missed intervals catch up on next start."""
from resources.lib.runtime import run_service

if __name__ == '__main__':
    run_service()
