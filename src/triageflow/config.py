import os
from dataclasses import dataclass, field
from pathlib import Path

SAMPLE_ROOT = Path(__file__).parent / "samples" / "shop"


@dataclass
class Settings:
    mode: str = "demo"
    model: str = ""
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    data_dir: Path = field(default_factory=lambda: Path(".triageflow"))
    repo: Path | None = None
    max_steps: int = 10
    max_tool_calls: int = 24
    max_model_calls: int = 16

    def __post_init__(self):
        self.data_dir = Path(self.data_dir)
        if self.mode not in {"demo", "live"}:
            raise ValueError("TRIAGEFLOW_MODE must be demo or live")
        if self.mode == "live" and (not self.api_key or not self.model):
            raise ValueError("Live mode requires TRIAGEFLOW_API_KEY and TRIAGEFLOW_MODEL")
        if not 1 <= self.max_steps <= 20:
            raise ValueError("max_steps must be between 1 and 20")
        if self.repo is not None:
            self.repo = Path(self.repo).resolve(strict=True)
            if not self.repo.is_dir():
                raise ValueError("TRIAGEFLOW_REPO must be a directory")

    @classmethod
    def from_env(cls):
        return cls(
            mode=os.getenv("TRIAGEFLOW_MODE", "demo"),
            model=os.getenv("TRIAGEFLOW_MODEL", ""),
            api_key=os.getenv("TRIAGEFLOW_API_KEY", ""),
            base_url=os.getenv("TRIAGEFLOW_BASE_URL", "https://api.openai.com/v1"),
            data_dir=Path(os.getenv("TRIAGEFLOW_DATA_DIR", ".triageflow")),
            repo=Path(os.environ["TRIAGEFLOW_REPO"]) if os.getenv("TRIAGEFLOW_REPO") else None,
            max_steps=int(os.getenv("TRIAGEFLOW_MAX_STEPS", "10")),
        )

    def projects(self) -> list[dict]:
        projects = [
            {"id": "demo-shop", "name": "Demo Shop", "description": "内置订单服务 · 3 个故障场景"}
        ]
        if self.repo:
            projects.append(
                {"id": "local", "name": self.repo.name, "description": "已配置的本地仓库"}
            )
        return projects

    def root(self, project_id: str) -> Path:
        if project_id == "demo-shop":
            return SAMPLE_ROOT
        if project_id == "local" and self.repo is not None:
            return self.repo
        raise ValueError("Unknown project")
