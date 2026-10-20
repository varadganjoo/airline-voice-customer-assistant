"""Run the Python backend locally; npm start builds browser assets first."""
import os
from pathlib import Path
import sys

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=int(os.getenv("PORT", "3000")), log_level="info")
