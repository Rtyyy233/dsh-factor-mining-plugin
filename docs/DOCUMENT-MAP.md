# 文档总地图（DOCUMENT-MAP）

更新：2026-10-05。作用：一份文件讲清**这套系统的全部文档在哪、各讲什么、
按什么顺序读、新文档往哪放**。文档分布在两个仓库，先记两个根：

- **插件仓**（下称【仓 A】）= `projects/dsh-factor-mining-plugin/`（当前 worktree；
  主树同名路径为提交态——**架构文档全套只在 worktree**，见 §五 已知事项）。
- **投研仓**（下称【仓 B】）= `modules/investment/research/bull-case-anatomy/`
  （主树；其 research/ 目录是投研引擎与全部研究产物的家）。

---

## 一、四套主文档总表

| # | 文档套 | 位置 | 体量 | 入口 | 一句话 |
|---|---|---|---|---|---|
| ① | **Harness 架构 · 因子挖掘侧** | 【仓 A】docs/HARNESS_ARCHITECTURE.md + docs/arch/ | 总览卷 1+分篇 13 | [HARNESS_ARCHITECTURE.md](HARNESS_ARCHITECTURE.md) | 引擎**内部**机制：评估管线/多重检验墙/停走纪律/三通道送达/调度器/三原语；含门禁总表、分权总表、事故博物馆 |
| ② | **Harness 架构 · 投研侧** | 【仓 B】research/docs/HARNESS_ARCHITECTURE.md + research/docs/arch/ | 总览卷 v2+分篇 49 | research/docs/HARNESS_ARCHITECTURE.md | 投研引擎内部机制：总账/开题门卫/无偏管线仪器/随机审计/交付门禁栈；一条线的一生 20 步 |
| ③ | **模型可见上下文盘点（提示词逐字）** | 【仓 A】docs/model-context/ | 4 文件 29 卡 | [model-context/OVERVIEW.md](model-context/OVERVIEW.md) | 每种情境下模型实际看到的**逐字上下文**（系统提示/注入全文/工具 schema/技能正文），每卡带溯源与负情境 |
| ④ | **DSH 嵌入机制（外壳接线）** | 【仓 A】docs/DSH-EMBEDDING.md | 1 篇 | [DSH-EMBEDDING.md](DSH-EMBEDDING.md) | 两套系统**怎么装进宿主**：四包/profile 组合树/preset 封闭清单/三通道物理形态/投研半嵌/zcode 直驱/部署升级 |
| ⑤ | **trail 轨迹页（研究过程叙事）** | 【仓 B】research/trail/ | 172 线 md+html | research/trail/INDEX.html | 每条研究线"从头到尾发生了什么"三拍式叙事；#N 可点击展开原始记录原文；工序与格式见 research/docs/TRAIL_FORMAT_PROPOSAL.md（已归档）与 engine/trailgen.py |

辅助面：**因子写法指南**【仓 A】docs/FACTOR_GUIDE.md（factor(env) 契约与
PIT 纪律）；**过程文书**【仓 A】analysis/glm_mining/ 的 PLAN-*/REPORT-*
（每个任务的任务书与战报，按日期命名）；【仓 A】docs/internal/ 为早期
内部过程档案（历史封存，不作现行依据）。

---

## 二、按目的读（新读者路径）

- **"这两套系统怎么跑起来的"**（装机视角）→ ④ 一篇读完 → 需要内部细节再进 ①②。
- **"引擎凭什么可信"**（机制视角）→ ①（因子）或 ②（投研）总览卷的阅读路径三步走 → 门禁总表查单门。
- **"模型到底看到什么"**（上下文视角）→ ③ OVERVIEW 进分卡；与 ① 的送达篇、④ 的嵌入篇互相引卡号。
- **"某条研究线的过程与结论"**（产物视角）→ ⑤ INDEX 按线名过滤；#N 点击回原始记录。
- **"我想写因子"** → FACTOR_GUIDE + ① 的评估管线篇。

**分工铁律**：①②讲内部、④讲外壳、③讲逐字文本、⑤讲产物——四套互相
引用不重复内容（引卡号/篇名，不复制正文）。

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
```

---

## 四、新文档往哪放（治理规则）

1. **任务书/战报**（PLAN-*/REPORT-*）→【仓 A】analysis/glm_mining/，日期后缀。
2. **因子架构新篇** →【仓 A】docs/arch/ 一篇 + 总览卷目录表补一行（状态列）。
3. **投研架构新篇** →【仓 B】research/docs/arch/ 一篇 + 总览卷 v2 目录补行。
4. **改了模型可见文本**（工具描述/注入模板/技能/persona）→ 必须同步 ③ 对应卡
   （盘点是实物取证，过时即误导）。
5. **trail 新线回填**（研究线新增时）→ 四步工序：`threebeat-all --export-materials
   --only <线>` → 写拍稿（照 trail_3beat_all/WRITER-PROTOCOL.md）→ `--render --check`
   → `--move-in`（见 REPORT-trail-full-and-dsh-embedding-2026-10-01.md §遗留3）。
6. **DSH 嵌入面变化**（新增包/改 profile 结构/宿主升级）→ 同步 ④ 对应节。

---

## 五、已知事项（2026-10-05 复核更新）

- **架构文档全套已在 worktree 分支入册**（提交 dfd4749e，2026-10-03/04：
  总览卷+DSH-EMBEDDING+model-context 盘点卡首次 commit，并同步了 10-03
  源码变更——may_stop 退役/死线落盘/连注上限删除/三原语新篇）；主树
  plugin 路径仍无这批文件、且还带着已从 worktree 移走的三份散件——合并
  主树时以 worktree 为准。
- **2026-10-05 对表复核**：dfd4749e 入册的是 10-01 初稿，未含 10-03 变更的
  若干现在时陈述——本次已修订 DSH-EMBEDDING（通道表/状态表/重启清单/
  七问/通宵操作法）、delivery-three-channels、drive-injector（三处连注
  上限残留）、model-context f-dsh 七处与 f-zcode 两处（历史语境保留、
  现状以注记标明）；门禁总表与 mining-never-stops/deadline-mechanism/
  three-primitives 在 dfd4749e 已同步，无需再动。
- **新鲜度规则**：③ 盘点卡的取证基线是 2026-09-30 实物（部分卡片带
  "现状（2026-10-03）"注记）——以后每次改模型可见文本，除同步卡内
  现状外，新增情境应立新卡而不是改写历史卡。
- 2026-10-05 整理动作：docs/ 根三份散落 PLAN/REPORT git mv 至
  analysis/glm_mining/，与同族文书归位；无入链断裂。
- 挂账未动：dossier 存档页 144 条带日期前缀的 trail 死链（先于 10-01
  存在，修复属独立小任务，见 10-01 战报）；**worktree 的 ts/ 源码落后主树**
  （工具描述等 10-03 重写在主树 7b9671fc 一线，worktree 仍是旧文案——
  同步/合并时以主树为准；f-dsh 卡内引文已加历史注记标明）；ts/drive.ts
  头部注释仍写 "max N consecutive injections (default 5)"（代码行为已删
  上限，注释未清，随下次包更新顺手清）。
