import argparse
import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv

from triageflow.config import Settings
from triageflow.demo import CASES
from triageflow.engine import Engine, markdown_report
from triageflow.evaluation import evaluate
from triageflow.providers import ProviderError
from triageflow.store import Store


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(prog="triageflow", description="Evidence-first bug triage")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Start the local dashboard")
    serve.add_argument("--port", type=int, default=8000)
    demo = commands.add_parser("demo", help="Run an offline fixture; never calls a model")
    demo.add_argument("--case", choices=[c["id"] for c in CASES], default="coupon")
    demo.add_argument("--output", type=Path)
    run_parser = commands.add_parser("run", help="Analyze an issue using configured mode")
    run_parser.add_argument("issue")
    run_parser.add_argument("--project", default="demo-shop")
    run_parser.add_argument("--output", type=Path)
    eval_parser = commands.add_parser("eval", help="Run the 8 synthetic fixture regressions")
    eval_parser.add_argument("--output", type=Path, default=Path("artifacts/eval-results.json"))
    args = parser.parse_args()
    try:
        settings = Settings() if args.command == "demo" else Settings.from_env()
        if args.command == "serve":
            import uvicorn

            from triageflow.api import create_app

            uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port, workers=1)
        elif args.command == "eval":
            result = asyncio.run(evaluate(settings, args.output))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            raise SystemExit(0 if result["passed"] == result["total"] else 1)
        else:
            case = next((c for c in CASES if c["id"] == getattr(args, "case", "")), None)
            issue = case["issue"] if case else args.issue
            project = "demo-shop" if case else args.project
            if not 10 <= len(issue.strip()) <= 6000:
                raise ValueError("Issue must contain 10 to 6000 characters")
            settings.root(project)
            store = Store(settings.data_dir / "runs.sqlite3")
            run = store.create(project, issue, settings.mode)
            run = asyncio.run(Engine(settings, store).execute(run.id))
            if run.status != "completed":
                raise ValueError(run.error)
            report = markdown_report(run)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(report, encoding="utf-8")
            print(report)
    except (ValueError, OSError, ProviderError) as exc:
        parser.exit(1, f"triageflow: {exc}\n")


if __name__ == "__main__":
    main()
