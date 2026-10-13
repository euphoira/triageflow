"""Validated public contracts. Reports cannot smuggle untyped citation fields."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=300)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=20000)


class Report(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=3000)
    root_cause: str = Field(min_length=1, max_length=5000)
    confidence: Literal["low", "medium", "high"]
    evidence: list[Evidence] = Field(max_length=12)
    recommendations: list[str] = Field(max_length=12)
    test_plan: list[str] = Field(max_length=12)
    limitations: list[str] = Field(default_factory=list, max_length=12)
    verified: bool = False


class Metrics(BaseModel):
    tool_calls: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0


class Run(BaseModel):
    id: str
    project_id: str
    issue: str
    status: Literal["queued", "running", "completed", "failed", "interrupted"] = "queued"
    stage: str = "queued"
    mode: Literal["demo", "live"]
    created_at: str
    updated_at: str
    error: str | None = None
    report: Report | None = None
    metrics: Metrics = Field(default_factory=Metrics)
    plan: list[str] = Field(default_factory=list)


class CreateRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(default="demo-shop", min_length=1, max_length=80)
    issue: str = Field(min_length=10, max_length=6000)


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[str] = Field(min_length=1, max_length=6)


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepted: bool
    feedback: str = Field(max_length=3000)
