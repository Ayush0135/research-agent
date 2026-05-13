import sys
import os
from pathlib import Path

# Add user site-packages to sys.path
pass

# Add current directory to sys.path
sys.path.append(str(Path(__file__).parent.absolute()))

import uvicorn
from api.main import app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
