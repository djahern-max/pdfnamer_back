"""
main.py  –  PDF Auto-Namer (standalone service)

Start:  uvicorn main:app --reload
"""

import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers.pdf_namer import router as pdf_namer_router
from routers.admin import router as admin_router

app = FastAPI(
    title="PDF Auto-Namer",
    description="AI-powered PDF naming with per-tenant pattern learning.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(pdf_namer_router)
app.include_router(admin_router)

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/routes")
async def list_routes():
    return [
        {"path": route.path, "methods": list(route.methods)}
        for route in app.routes
        if hasattr(route, "methods")
    ]
