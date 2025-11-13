from typing import Optional, Any, Dict
from pydantic import BaseModel, HttpUrl, Field


class AnalyzeRequest(BaseModel):
    url: HttpUrl = Field(..., description="Full URL to analyze")
    keyword: Optional[str] = Field(None, description="Optional focus keyword")


class AnalyzeResponse(BaseModel):
    ok: bool
    url: HttpUrl
    keyword: Optional[str] = None
    report_path: str
    report: Dict[str, Any]


class HealthResponse(BaseModel):
    ok: bool
    ts: int
