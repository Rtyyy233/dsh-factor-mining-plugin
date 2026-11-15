# GITHUB 镜像同步流程（github同步流程）

> 2026-11-15 迁移后新版：**插件源树已从 `C:\Users\RTyyyy\Desktop\life-os\projects\dsh-factor-mining-plugin`
> 迁至 `D:\投资工作区\03-因子挖掘\插件\dsh-factor-mining-plugin`**（本目录所在仓）。流程照旧，路径全换。
> 历史：首次全量同步 2026-10-30（main=662dcfc8c，0.1.26，历史已剥离 claude 尾注）。

## 镜像仓

`git@github.com:Rtyyy233/dsh-factor-mining-plugin.git`（main；独立精选历史，与工作区仓无共同祖先，每里程碑一提交）

## 同步并集（源=工作区插件树）

| 进镜像 | 说明 |
|---|---|
| `python/` `ts/` `preset/` | 主树直拷 |
| `README.md` `DESIGN.md` `LICENSE` | 主树直拷（README 以主树为唯一新版） |
| `docs/` | 主树打底（含 internal/ 报告与 figs/） |

**排除**：`analysis/`、`unused-state-root-for-unit/`、`*.tgz`、`__pycache__/`、`node_modules/`、临时 worktree 遗留。
**红线**：新提交永不加 Claude 尼注（Co-Authored-By）；内部数据/账本/状态根永不上传。

## 步骤（临时克隆法，等价旧 worktree 法）

```bash
SRC="D:/投资工作区/03-因子挖掘/插件/dsh-factor-mining-plugin"
TMP="<临时目录>"
git clone git@github.com:Rtyyy233/dsh-factor-mining-plugin.git "$TMP/mirror"
cd "$TMP/mirror"
# 清空工作树（保留 .git/.gitignore）后复制并集：
#   python ts preset docs + README.md DESIGN.md LICENSE ← "$SRC"
git add -A && git status   # sanity：应只见预期增删
git commit -m "<里程碑说明>"
git push origin main
```

## 网络坑（实测）

- SSH 22 端口间歇抖动：`ssh -T` 都可能偶败；fetch/ls-remote 通而 push 败时**原样重试即可**。
- 443（ssh.github.com:443）与 HTTPS:443 在本网络更差；`gh` 已登录 Rtyyy233（repo scope）可作备援。
- 身份：repo config `rtyyy <tao7605271662@163.com>`。

## 最近一次试同步

2026-11-15（庚批）：源=工作区新路径，全流程跑通（结果见 `_施工\日志.md` 与完工报告）。
