from fastapi import FastAPI

from app.api.v1.routes import documents

# from app.api.v1.routes import auth, users

app = FastAPI(title="DocuMind", version="0.1.0")

app.include_router(documents.router, prefix="/api/v1")
# app.include_router(auth.router, prefix="/api/v1")
# app.include_router(users.router, prefix="/api/v1")

@app.get("/health")
def health():
    return {"status": "ok"}



