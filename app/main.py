"""FastAPI application entry point."""

from fastapi import FastAPI

app = FastAPI(title="DayN-NetBox Bridge")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
