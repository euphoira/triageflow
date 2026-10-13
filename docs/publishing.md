# 发布到 GitHub

代码准备好后，在你自己的 GitHub 账号中建立公共仓库。建议名称 `triageflow`；如果重名，
可用 `triageflow-agent`。项目描述：

> Evidence-first bug triage agents with bounded tools, durable checkpoints and reproducible evals.

建议 Topics：`agent`、`multi-agent`、`llm`、`tool-calling`、`fastapi`、`python`、`evaluation`。

## 发布前

```bash
python -m pytest -q
python -m ruff check src tests
triageflow eval
git status --short
```

确认 `.env`、`.triageflow/`、私有源码、密钥和本机路径未进入待提交文件。
文档中不要把 Demo 指标写成真实模型效果。MIT 许可证已经包含在项目中。

## 有 GitHub CLI

先完成 `gh auth login`。如果本地还没有提交，配置你自己的 Git 作者身份并提交：

```bash
git add .
git commit -m "feat: add evidence-first agent triage workbench"
gh repo create triageflow --public --source=. --remote=origin --push
```

账号已有同名仓库时不要重复创建，先确认正确地址，再设置 remote。

## 用 GitHub 网页创建

1. 在 GitHub 新建 `triageflow` 公共仓库，不预置 README、License 或 gitignore。
2. 用页面提供的真实仓库地址执行（替换 YOUR_USERNAME）：

```bash
git remote add origin https://github.com/YOUR_USERNAME/triageflow.git
git push -u origin main
```

3. 查看 Actions 的第一次 CI 结果，设置仓库 Description / Topics，将仓库 Pin 到个人主页。
4. 录制一段实际操作视频或截图放入 README，发布 v0.1.0 Release。

如果 Codex 中的 GitHub 插件已完成连接，可以让当前对话继续处理发布。尚未连接时，
本地 Git 仓库不代表已经创建了 GitHub 远端仓库。
