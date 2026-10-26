import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("copilot.api:app", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8080")))
