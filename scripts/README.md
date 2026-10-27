# Contribution GIF updater

`update_contributions.py` 从 GitHub 公开的 contributions 页面读取真实数据，重新生成：

- `assets/contrib-anim.gif`（浅色 / 漫画逐格渐显）
- `assets/contrib-night-anim.gif`（深色 / 每格一颗流星、同时 3–4 颗）

同时更新 `README.md` 里的 `alt="… contributions in the last year"` 和 `assets/contributions.json` 数据快照。

## 本地运行

```bash
pip install -r scripts/requirements.txt
python scripts/update_contributions.py
# 只看数据、不写文件：
python scripts/update_contributions.py --dry-run
```

Windows / Linux / macOS 都可以跑。SVG 渲染用 `resvg-py`，字体已经放在
`scripts/fonts/`，不依赖系统字体或 cairo。

## 数据来源

`https://github.com/users/<user>/contributions` 的公开 HTML，不需要 token 或 secret。
页面里的每日 `data-date` / `data-level` 和 tooltip 计数会被解析，并与页面顶部的
总数交叉校验；天数不在 360–375、日期不连续、或总数对不上时脚本直接报错退出，
不会提交坏图。私有贡献已经由 GitHub 以匿名计数包含在内，所以总数和个人主页一致。

## 自动更新

`.github/workflows/update-contributions.yml`：

- **每周一 09:17（北京时间）** 自动运行（cron `17 1 * * 1`，UTC）
- 也可以在 Actions 页面手动点 **Run workflow**
- 用仓库自带的 `GITHUB_TOKEN` 提交，不需要额外 PAT
- 只在 GIF / README / 快照有变化时 commit

改频率只需要改 cron：

| 需求 | cron |
|---|---|
| 每周一 09:17 | `17 1 * * 1` |
| 每天 09:17 | `17 1 * * *` |
| 每周一、四 09:17 | `17 1 * * 1,4` |

## 目录

```
scripts/
  update_contributions.py   生成脚本
  requirements.txt          pillow / resvg-py 固定版本
  templates/*.svg           原主题的静态卡片模板（{{TOTAL}} 等占位符）
  fonts/*.ttf               Noto Sans + JetBrains Mono（OFL）
```
