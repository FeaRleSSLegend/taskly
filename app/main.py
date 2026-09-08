from fastapi import FastAPI

from app.routers import auth, dashboard, nodes, roadmaps

app = FastAPI(title="AI Task Roadmap Generator API", version="0.1.0")

app.include_router(auth.router)
app.include_router(roadmaps.router)
app.include_router(nodes.router)
app.include_router(dashboard.router)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
