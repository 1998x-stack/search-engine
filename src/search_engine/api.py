"""Minimal API scaffold: intentionally does not pretend to serve search results."""
from fastapi import FastAPI, HTTPException

app = FastAPI(
    title="Search Engine (architecture scaffold)",
    version="0.0.1",
    description="Only liveness is available until the P0 implementation phase.",
)


@app.get("/health/live")
def health_live() -> dict[str, str]:
    return {"status": "alive", "phase": "design-scaffold"}


@app.get("/v1/search")
def search_not_implemented() -> None:
    raise HTTPException(
        status_code=501,
        detail="Search is not implemented; see docs/09-ROADMAP-EVALUATION.md",
    )
