# 文档总地图（DOCUMENT-MAP）

更新：2026-10-30。作用：一份文件讲清**这套系统的全部文档在哪、各讲什么、
按什么顺序读、新文档往哪放**。文档分布在三个仓，先记三个根：

- **主树仓**（下称【仓 A】）= `projects/dsh-factor-mining-plugin/`（life-os
  主树内）——**代码与文档唯一真源**：Python 引擎（桥直载零打包）、TS 四包、
  preset、docs/ 全套。2026-10-30 起架构文档套件已从 worktree 迁入主树，
  旧 worktree（`.claude/worktrees/residual-holding-correlation/`）退役只读。
- **投研仓**（下称【仓 B】）= `modules/investment/research/bull-case-anatomy/`
  （主树；其 research/ 目录是投研引擎与全部研究产物的家）。
- **公开镜像仓**（下称【仓 C】）= `github.com/Rtyyy233/dsh-factor-mining-plugin`
  （精选历史快照，与 life-os 无共同祖先；内容=【仓 A】代码∪文档的并集，
  排除 analysis/ 工作区与一切用户数据）。用途：对外发布与镜像安装
  （`git clone` 即用）。同步为手动快照（方法：临时 worktree 并集覆盖+强推，
  见主树 `analysis/glm_mining/PLAN-document-map-refresh-and-archify-diagrams-2026-10-30.md` §七）；
  最近同步点 **662dcfc8c（2026-10-30，v0.1.26）**。

---

## 一、六套主文档总表

| # | 文档套 | 位置 | 体量 | 入口 | 一句话 |
|---|---|---|---|---|---|
| ① | **Harness 架构 · 因子挖掘侧** | 【仓 A】docs/HARNESS_ARCHITECTURE.md + docs/arch/ | 总览卷 1+分篇 13 | [HARNESS_ARCHITECTURE.md](HARNESS_ARCHITECTURE.md) | 引擎**内部**机制：评估管线/多重检验墙/停走纪律/三通道送达/调度器/三原语/**推导优先与导航层**；含门禁总表、分权总表、事故博物馆 |
| ② | **Harness 架构 · 投研侧** | 【仓 B】research/docs/HARNESS_ARCHITECTURE.md + research/docs/arch/ | 总览卷 v2+分篇 49 | research/docs/HARNESS_ARCHITECTURE.md | 投研引擎内部机制：总账/开题门卫/无偏管线仪器/随机审计/交付门禁栈；一条线的一生 20 步 |
| ③ | **模型可见上下文盘点（提示词逐字）** | 【仓 A】docs/model-context/ | 4 文件 29+5 卡 | [model-context/OVERVIEW.md](model-context/OVERVIEW.md) | 每种情境下模型实际看到的**逐字上下文**（系统提示/注入全文/工具 schema/技能正文），每卡带溯源与负情境；附录 J=推导优先层五卡（2026-10-30 实跑取证） |
| ④ | **DSH 嵌入机制（外壳接线）** | 【仓 A】docs/DSH-EMBEDDING.md | 1 篇 | [DSH-EMBEDDING.md](DSH-EMBEDDING.md) | 两套系统**怎么装进宿主**：四包/profile 组合树/preset 封闭清单/三通道物理形态/投研半嵌/zcode 直驱/部署升级；含矿工门（0.1.26）与镜像安装路径 |
| ⑤ | **trail 轨迹页（研究过程叙事）** | 【仓 B】research/trail/ | 172 线 md+html | research/trail/INDEX.html | 每条研究线"从头到尾发生了什么"三拍式叙事；#N 可点击展开原始记录原文；工序与格式见 research/docs/TRAIL_FORMAT_PROPOSAL.md（已归档）与 engine/trailgen.py |
| ⑥ | **架构交互图（archify 三图）** | 【仓 A】docs/diagrams/ | 3 HTML+3 JSON IR | [system-architecture.html](diagrams/system-architecture.html) · [agent-workflow.html](diagrams/agent-workflow.html) · [data-flow.html](diagrams/data-flow.html) | 系统架构/Agent 工作流程/数据流三张交互图（单文件 HTML，节点带源码锚点，证据基线 662dcfc8c）；JSON IR 源在 docs/diagrams/src/ |

辅助面：**因子写法指南**【仓 A】docs/FACTOR_GUIDE.md（factor(env) 契约、PIT
纪律、预期预注册与镜像用法）；**过程文书**【仓 A】analysis/glm_mining/ 的
PLAN-*/REPORT-*（每个任务的任务书与战报，按日期命名）；【仓 A】docs/internal/
**分层**：2026-10-07 墙增长报告与三方法 NOTE 是**现行数值方法论依据**
（图片引用规则：与文档同目录 `figs/` 相对引用，`../../` 跨目录在查看器
不加载）；2026-08/09 的 GRID/RESEARCH/REPORT 旧档为过程档案（历史参照，
不作现行依据）。

---

## 二、按目的读（新读者路径）

- **"这两套系统怎么跑起来的"**（装机视角）→ ④ 一篇读完 → 需要内部细节再进 ①②。
- **"引擎凭什么可信"**（机制视角）→ ①（因子）或 ②（投研）总览卷的阅读路径三步走 → 门禁总表查单门 → 想看活的拓扑开 ⑥。
- **"模型到底看到什么"**（上下文视角）→ ③ OVERVIEW 进分卡；与 ① 的送达篇、④ 的嵌入篇互相引卡号。
- **"某条研究线的过程与结论"**（产物视角）→ ⑤ INDEX 按线名过滤；#N 点击回原始记录。
- **"我想写因子"** → FACTOR_GUIDE + ① 的评估管线篇（Agent 自主挖掘另读推导优先分篇）。
- **"系统长什么样"**（结构/流程/数据视角）→ ⑥ 三图按需开。

**分工铁律**：①②讲内部、④讲外壳、③讲逐字文本、⑤讲产物、⑥讲拓扑——
六套互相引用不重复内容（引卡号/篇名/图节点，不复制正文）。

---

## 三、文档间引用关系

```
④ DSH-EMBEDDING（外壳）──互链──① ②（内部）
        │                            │
        └──引卡号──→ ③ model-context（逐字）←──引卡号──┘
                          │
                          └─（投研卡 t-invest 与 ② 的 gates/audit 篇互指）

⑤ trail（仓 B）──INDEX 链──→ ② 总览卷；格式源头=research/docs/TRAIL_FORMAT_PROPOSAL.md
                              生成器=engine/trailgen.py（② 的 trailgen 篇）

⑥ diagrams（仓 A）──节点锚点──→ 主树源码（镜像 662dcfc8c 行号）；
                  总览卷/derivation-first 篇互链
```

---

## 四、新文档往哪放（治理规则）

1. **任务书/战报**（PLAN-*/REPORT-*）→【仓 A】analysis/glm_mining/，日期后缀。
2. **因子架构新篇** →【仓 A】docs/arch/ 一篇 + 总览卷目录表补一行（状态列）。
3. **投研架构新篇** →【仓 B】research/docs/arch/ 一篇 + 总览卷 v2 目录补行。
4. **改了模型可见文本**（工具描述/注入模板/技能/persona）→ 必须同步 ③ 对应卡
   （盘点是实物取证，过时即误导）；TS 工具版本号变更同样触发。
5. **trail 新线回填**（研究线新增时）→ 四步工序：`threebeat-all --export-materials
   --only <线>` → 写拍稿（照 trail_3beat_all/WRITER-PROTOCOL.md）→ `--render --check`
   → `--move-in`（见 REPORT-trail-full-and-dsh-embedding-2026-10-01.md §遗留3）。
6. **DSH 嵌入面变化**（新增包/改 profile 结构/宿主升级）→ 同步 ④ 对应节。
7. **拓扑变更再生成图**（2026-10-30 新增）：改了 bridge 分发表/驱动注入/门链/
   数据实体的任务，验收清单必须含"⑥ 对应图再生成或标注过时"——图的每个
   节点带源码锚点（证据基线=镜像修订号），源 JSON 与 HTML 同步入库
   （docs/diagrams/src/）。再生成方法：archify 技能（~/.zcode/skills/archify/），
   candidate 在 `.archify/<type>-<slug>-<ts>/candidate.json`，
   `node ~/.zcode/skills/archify/bin/archify.mjs finalize <type> <candidate> <output>
   --repo-root <镜像干净检出> --quality showcase --json`。

---

## 五、已知事项（2026-10-30 复核更新）

- **架构文档套件已迁主树**（2026-10-30 P0：DOCUMENT-MAP/HARNESS_ARCHITECTURE/
  arch/×13/model-context/×4/DSH-EMBEDDING 全部落主树 docs/；主树=唯一真源；
  旧 worktree 退役只读，其 RETIRED-README.md 有交接说明）。10-30 同步确认
  主树 docs/ 与镜像仓 docs/ 逐文件一致（78=78）。
- **本轮已刷新**：① 总览卷（10-30 修订摘要/13 篇目录/流程层 17 步/门禁总表
  补 B1 行/分权总表补自动规则行/阅读路径补 ⑥）；④ 嵌入篇（镜像安装路径+
  13 篇计数）；drive-injector 篇矿工门节（10-16 交付时已同步，本轮核验现行）；
  delivery-three-channels 相邻表（绑定=路由/名册=注入目标）；⑥ 三图新建。
- **新鲜度规则**：③ 盘点卡的取证基线是 2026-09-30 实物（附录 J 五卡为
  2026-10-30 合成面板实跑增补）——以后每次改模型可见文本，除同步卡内
  现状外，新增情境应立新卡而不是改写历史卡。
- **挂账未动**：dossier 存档页 144 条带日期前缀的 trail 死链（先于 10-01
  存在，修复属独立小任务，见 10-01 战报）。
- **实战期待补**：③ 附录 J 卡 J-5 的导航素材段逐字样本（合成 stateRoot
  试验数过少素材为空，留首轮挖掘补）；B1 enforce 自动切档与 D1 混布的
  首批 gate_audit 事件（等挖掘活动恢复）。
- 2026-10-05 旧事项清算：docs/ 根三份散落 PLAN/REPORT 已归位
  analysis/glm_mining/（2026-10-30 随 P0 落主树）；worktree ts/ 落后与
  drive.ts 头注残留两项已了结（worktree 退役；头注 2026-10-30 已清）。
