# 上游源码笔记：约束发生在哪一层

对象：`sixtysevenlf/dsh-blender-plugin` v2.6.2（核对副本 commit `68e9982`，2026-10-30）。
方法：只认源码与仓库自带文档，逐条核对。

> **说明。** 本文写于 2026-10-30，起因是内部一版对上游的评估，写的时候逐条对过源码。
> 它的用途是交代本插件为什么这样设计——不是对该项目的整体评价，也不是对作者的判断；
> 上游在持续演进，文中关于具体实现缺口的描述可能已经过时，引用前请对最新代码复核。

真正要回答的问题只有一个：**这个插件在哪一层、用什么机制，在形状出现之前就替模型做了决定？**

---

## 0. 结论先行

1. **几何层并没有被锁死。** `blender_rt_do` 能跑任意 bpy；
   34 family / 228 op 里有 sculpt / vehicle / human / sweep / generator；`surface_*` 只是其中 6 个 op
   （loft / sweep / patch / tessellate / validate / export，约占 228 的 2.6%），
   而且 M3 fit 与 M5 blend/offset 尚未实现。`spec_hash` 只覆盖 `surface_*`。
2. **真正的压缩发生在规范层。** `skills/blender-modeling/SKILL.md` §1 把“op-first / 有生成器就不许从零 bpy /
   例外只有一种 / 不过门不进入下一步”写成**强制返工判据**；模型在生成任何几何之前就读到它。
   这不是几何约束，是**动作空间先验**。
3. **gate 本身比传言温和，skill 让它变硬。** `deliver.py` 的 `require_gate` 默认 false，
   `gate_deliver` 是独立 op；但 skill 把交付门提升成**每一步的内环门**（§1.1 第 7 步）。
   一个合理的出口检查，被改写成生成时的搜索方向。
4. **换成 SDF 主通道并不自动解决。** 任何低维 genotype（loft 截面、SDF 基元、latent code）都是瓶颈；
   如果规范姿态不变，SDF 只会变成新的“安全 op”，模型仍然会贴着门长。
   修复顺序必须是：**相位 → 能量语义 → 谱系哈希 → 表示多元化**，而不是表示优先。
5. **重构不需要重写 45k 行内核。** 需要改的是 5 个地方：SKILL 的强制段落、gate 的相位、deliver 的默认绑定、
   surface 的哈希语义、以及新增一个探索期软场工具。内核（audit / assembly / render / txn）可以原样保留。

一句话：**问题不是“参数化曲面太硬”，而是“探索期和交付期共用了一套交付期的法律”。**

---

## 1. 先校准：三处与源码不符的说法

| 先前的说法 | 源码事实 | 判定 |
|---|---|---|
| “loft / sweep / patch 是唯一合法的形状来源” | `src/index.ts:1683-1694` 的 `blender_rt_do` 在持久内核里执行任意 Python；`package.json:4` 写明 34 family / 228 op；`SKILL.md:0.7` 的表里有 `sculpt_* / human_* / vehicle_* / shape_* / generator_* / sweep_*`；`surface_*` 只是 6 个 op，约占 2.6% | **假**。存在硬约束的是规范，不是能力 |
| “spec_hash 把采样分布坍缩成一个点” | `runtime/surface.py:84-87` 的 `_spec_hash` 只对 `surface_*` 的 spec 做规范化；`spec_hash` 只在 `surface` 模块里出现（28 处，全部在该文件/文档）；bmesh / sculpt / rt_do 不经过它 | **机制上假**；作为激励偏差部分为真（见 §2.2） |
| “audit + visual gate 是双重形状警察” | `runtime/audit.py` 的修复是显式 op、默认 dry-run；`runtime/deliver.py:403,438-439` 的 `require_gate` 默认 false；`gate_deliver` 是 `runtime/gate.py:1066` 的独立 op | **代码层面松**；真正把它变硬的是 SKILL 的强制段落（见 §2.1） |

而且 `surface.py:11` 自己写着：`M5 blend/offset 只留接口，明确返回 ok:false`；`M3 fit` 未实现。
一个连拟合都没有的曲面包，不可能是“唯一合法来源”。把它当元凶，会修错地方。

---

## 2. 真正的三个失效点

### 2.1 规范层把“交付门”提升为“生成内环门”

关键不是 `gate.py` 会不会 block，而是模型**什么时候**读到“必须过门”：

- `SKILL.md:195`：`## 1. 架构优先：这个插件就是建模流程本身（强制）`
- `SKILL.md:209`：`7. 过门：audit_mesh → asm_scan → asm_gate/gate_run → visualreport；不过门不进入下一步。`
- `SKILL.md:212`：`允许的例外只有一种：catalog 里确实没有对应能力，且你已经用 catalog/help 证明过。`
- `SKILL.md:217-218`：`有 plan op 就不许手写等价几何/检查`；`有 blockout/shape/vehicle/human/sculpt 等生成器就不许从零 bpy 拼形状`。
- 连自由通道的描述也写着：`只补 op 没有的局部编辑；有 plan op 先用 op。`（`src/index.ts:1684`）

这四句话合起来，等价于给模型的策略加了一个**先验**：

```
π(a | s) ≈ 0   if a 不在 catalog 的 228 个 op 里
π(a | s) ≈ 0   if a 会触发 gate 的 fail/degraded
```

模型不是在“生成形状”，而是在“在合规动作序列里找一条最不容易被 repair/block 的路径”。
这正是 reward hacking 的**前置化**：约束不是输出端的筛选器，而是生成端的搜索方向。
区别只在于：这里不是模型自己发现的捷径，是 prompt 直接告诉它的。

**为什么仓库会这么写？** 因为它是为 deepseek-flash 设计的义肢。`docs/flash建模上限-整合方案.md:3`
写得很清楚：`上限 = 外部通路的覆盖面，不是模型能力`；`:92` 写：`不要训练模型的眼睛和手，要给它们外挂量具、模具和验收标准`。
对**交付**，这是对的；对**探索**，这是把“验证瓶颈”误当成“生成瓶颈”。
一个不会看图的模型需要量具；但量具不该在它还没画出第一笔时告诉它“只能画量具认识的形状”。

### 2.2 可度量性偏差：什么能被 hash / audit，什么就被生成

`spec_hash` 的代码没有锁死任何东西，但它制造了一个**选择优势**：

- `surface_*` 有 `spec_hash`、有 `surface_validate` 的三态 verdict、有 `surface_export`（`surface.py:15,300-381`）。
- `sculpt_*` / 手写 bmesh 没有等价的“可复现身份证”，只有 mesh audit。
- 而 skill 的验收文化要求 `manifest/md5`、`verdict`、`provenance`（`SKILL.md:0.8`、§1.2-1.3）。
- README 把 `spec_hash` 当卖点（`README.zh-CN.md:23`）：`同一份规格得到同一个 spec_hash 与同一批顶点`。

于是模型学到的不是“参数化曲面是唯一合法的”，而是“**可 hash 的形状更容易被验收为完成**”。
这是 Goodhart 在**表示层**的发生：不是顶点被锁，而是“可度量”的那部分几何被系统性偏爱。
一种常见直觉（“spec_hash 让采样退化成查表”）方向是对的，但机制不是“锁”，是“奖励”。
这也解释了为什么 `docs/flash建模上限-整合方案.md:12` 会记录：`长尾 5 个新族真实使用 0 次（“不熟的不敢用”）`——
不是模型不会用，是它学会了“用不熟的东西更容易被判失败”。

### 2.3 硬门/三态门的边界效应

`gate_run` 其实比“布尔门”好：它是 `pass | fail | degraded` 三态，`degraded` 明确不等于通过。
但 `gate_deliver` 在 fail 时直接 `blocked`（`gate.py:1069-1090`），而 skill 又要求每一步都过门。
把这两件事叠起来，模型面对的是一个**可行集投影**：

```
hard:  p(x) ∝ p0(x) · 1[x ∈ F]
```

可行集边界上的形状会被推到边界；不可行的中间形态（薄壁、负空间、自交的草稿）在变成想法之前就被剪掉。
本仓库的 A/B 实测（`report/soft-vs-hard.md`）就是这个边界效应的最小证据：
hard 模式把厚度从 6.64 推到 13.82（过冲），体积从 67k 推到 188k（越过 80k ±25% 的带），
单步表面漂移 7.98 mm；soft 模式单步 ≤1.42 mm，体积 89k，对称性 0.24 mm。

软约束不是“更宽松”，而是**不同的数学对象**：

```
soft:  p_t(x) ∝ p0(x) · exp(-β(t) · Σ w_i E_i(x)),  β(t) 从 0 平滑升到 1
```

`p_t` 是对 `p0` 的连续形变，保留基分布的多个模态；`p(x)1[x∈F]` 是投影/拒绝采样，会在边界上塌缩。
前者给模型的是**梯度**，后者给模型的是**判决**。这就是“约束和模型谈判”与“约束在出口把模型推回去”的差别。

### 2.4 补充：工具面与上下文税（这是仓库自己测出来的）

15 个工具 + 228 个 op + 1169 行 skill；`blender_rt_plan` 是 mega-tool，使用仪式是
`catalog → family → help → op`（`SKILL.md:0.1` 硬规则 1）。这本身是一层**过程先验**：
模型把预算花在“找对的 op”，而不是“想形状”。
仓库自己的数据已经证明这条路的边际收益在递减：`docs/flash建模上限-整合方案.md:12` 的
“长尾 5 个新族真实使用 0 次”。**op 的数量不等于行为的宽度**；当选择成本高于收益时，
模型会退回熟悉的少数 op——这不是模型懒，是接口在教它保守。

---

## 3. 为什么“换成 SDF 主通道”不等于解决

一种常见的重构方案是“用隐式生成器取代显式参数化基元”。方向有价值，但作为**第一性修复**是错的：

1. **任何 genotype 都是瓶颈。** loft 用截面序列，SDF 用基元 + 平滑 CSG，latent diffusion 用 latent code；
   它们都把无限形状空间压到低维参数空间。换表示只是换笼子的形状，不改变“笼子存在”。
2. **如果规范姿态不变，SDF 会成为新的安全 op。** 模型仍被要求 op-first、仍被要求每步过门；
   它会在 SDF 参数空间里继续最小化违规概率，而不是探索拓扑。
3. **SDF 有自己的先验，而且更隐蔽**：网格分辨率、iso-level、smooth-min 的 `k`、bounding box。
   这些不写出来，模型就看不见；写出来，它们就是新的 `spec_hash` 式参数表。
4. **真正要换的是“约束作用的时机与语义”**，表示可以多元：自由 bpy、SDF、参数化精修、sculpt 并存。
   判断标准不是“用了 SDF 没有”，而是“模型有没有在约束介入之前，先做出一个属于自己的候选”。

所以本仓库的重构顺序是：**相位（§4.1）→ 能量（§4.2）→ 谱系（§4.4）→ 表示（§4.3）**。
表示放在最后，因为它是结果，不是原因。

---

## 4. 重构蓝图：从“约束形状”到“约束分布”

### 4.1 相位状态机（最高杠杆）

把“一套法律”拆成三段，默认按任务类型选起点：

| 相位 | 允许 | 禁止 | 默认用于 |
|---|---|---|---|
| `explore` | 任意 bpy / SDF / 参数化；能量只读；无 verdict | 强制 op-first；per-step gate；spec freeze | 开放式创作、形体探索、参考图早期 |
| `converge` | schedule 升温；候选排序；负例回注 | 出口 block；把 degraded 当 pass | 结构已定、需要收敛 |
| `commit` | audit / asm_scan / gate / deliver；blocked 合法 | 把 commit 的法律带回 explore | 工程交付、认证件 |

实现上不需要新框架：任务卡里加 `phase`，SKILL 的 §1 改成 `if phase==commit` 才生效；
`gate_deliver` 要求 `phase=commit` 或显式 `commit=true`；`deliver.py` 的 `require_gate` 保持 false，
不要按注释里的“下一个 minor 默认开启”去翻——那会把整个 profile 变成 commit 模式。

### 4.2 能量注册表（替代二值门）

每个约束是一个 term：

```json
{"id":"thickness","weight":1.0,"schedule":{"start":0.30,"end":0.95},
 "target":6.0,"q":0.1}
```

```
E(x) = Σ_i w_i(t) · E_i(x)
w_i(t) = w_i · smoothstep((t - start_i) / (end_i - start_i))
```

- 探索期 `t=0`，所有 `w_i=0`：约束只测量，不塑形。
- 收敛期 `t` 上升：约束以梯度介入；权重可协商、可关、可改。
- `state` 只输出 `ok | below | above | unresolved`，是**测量标签**，不是许可。
- 梯度用有限差分（对模型暴露的 tunables），每个 term 先按自身尺度归一，再做线搜索，
  保证同 `t` 下总能量单调不升（本实现见 `runtime/energy.py` 的 `refine`）。

这一步的算法意义：把 `p(x)1[x∈F]` 换成 `p_t(x)`；模型看到的是“往哪边调”，不是“过不过”。

### 4.3 隐式主通道 + 参数化降级为精修器

- 主通道：SDF genome（基元 + 平滑 CSG + mirror），拓扑由零等值面涌现；
  模型自己组合 genome，而不是从 228 个固定 op 里选。
- `surface_loft/sweep/patch` 保留为**可选精修器**：当模型已经决定要一个放样瓶身时，用它把局部做干净；
  它不再是“唯一正确的几何来源”。
- 自由 bpy 仍然是一等公民：`rt_do` 的定位从“只补 op 没有的局部编辑”改成“探索期的默认宿主语言”。

### 4.4 哈希从“身份”变成“谱系”

保留可复现性，但换语义：

```
genome_hash  = hash(engine + resolved genome)          # 这次提议的结构身份
lineage_hash = hash(genome + terms + tunables + seed + schedule)  # 这条轨迹的指纹
```

- 同 `genome_hash` 不再意味着“必须长这样”；它只意味着“同一个提议”。
- 复现的是**轨迹**（seed / schedule / 参数路径），不是“唯一顶点集”。
- `freeze` 是 commit 期的显式动作，不是默认值。
- 这样，可复现性不再给参数化曲面额外的选择优势；sculpt 也可以有 lineage。

### 4.5 负例回注，而不是出口阻断

失败维度变成下一条提议的条件：

```json
{"term":"thickness","measured":4.1,"target":6.0,
 "suggestion":"p10 4.1 < 6; largest sensitivity: r_arm"}
```

模型在下一轮主动避开，而不是被 block 在原地。约束的存在**增加**探索维度（多了一个可调方向），
而不是减少可达形状空间。本插件的 `signals` 就是这个结构；它没有 `blocked` 字段。

### 4.6 工具面：加一个，不是加十个

- 保留 15 个工具；新增 1 个 `blender_soft`（propose / energy / refine / doctor）。
- `catalog` 从“每次第一步”降级为“需要时检索”；探索期不强制。
- 不再把 `plan` 扩成第 229 个 op；新能力优先做成一个**有明确语义边界**的工具。

---

## 5. 迁移表（文件级）

| 文件 | 动作 | 理由 |
|---|---|---|
| `skills/blender-modeling/SKILL.md` | 把 §1 改成 `phase` 条件式；新增 explore 段；保留 §0/§2-/§3 作为 commit playbook | 最高杠杆：它先于几何进入模型上下文 |
| `runtime/gate.py` | 新增 `gate_energy`（值 + 梯度 + signals，无 verdict）；`gate_deliver` 要求 `commit=true`；保留三态 | 让“测量”和“许可”分开 |
| `runtime/deliver.py` | 保持 `require_gate=False`；不要全局翻 true；把强制绑定到 commit 相位 | 注释里的“下一个 minor 默认开启”会把 profile 锁死 |
| `runtime/surface.py` | 保留；`spec_hash` → `lineage_hash`；spec 增加 `sampling`；`freeze` 显式 | 保留可复现，去掉“身份”特权 |
| `src/index.ts` | 加 1 个软场工具（或接 companion）；不扩 15 工具面 | 降低发现成本 |
| `docs/flash建模上限-整合方案.md` | 保留“外挂量具”论点，但限定在 commit；补一句“explore 的瓶颈是假设生成，不是验证” | 义肢对交付有效，对探索是笼子 |
| `runtime/audit.py` | 保留；修复仍默认 dry-run | 它不是元凶，别误伤 |

**不要做**：重写 45k 行内核；把 SDF 硬塞进 `surface_*`；删掉 gate（交付需要它）；再加 50 个 op。
**要做**：先把相位和能量语义落地，再谈表示。

---

## 6. 验收实验（本仓库已跑）

同一 genome、同一 terms、10 步，soft vs hard：

| 指标 | soft（schedule） | hard（单步闸机） |
|---|---|---|
| 最大单步参数位移（归一化） | 0.08 | 0.45 |
| 最大单步表面漂移 | 1.42 mm | 7.98 mm |
| p10 厚度（目标 ≥ 6 mm） | 6.64 → 11.08 | 6.64 → 13.82 |
| 终态体积（目标 80k ±25%） | 88,509（ok） | 187,873（above） |
| 终态对称性 rms | 0.24 mm | 1.46 mm |

解释与完整日志见 [`report/soft-vs-hard.md`](soft-vs-hard.md)。
它证明的是**机制**：软 schedule 能在前 3 步完全不动，之后以小步介入；hard 单步会过冲并牺牲另一维。
它**不**证明 LLM 层面的“创造力提升”——那需要同 prompt、多 seed、盲评的 LLM 实验；这里只把几何层的
因果链跑通，不冒充认知科学。

---

## 7. 一句话的残余风险

**最后一个仍然会压缩“发挥”的地方，是调用方替模型决定暴露哪些 tunables。**
插件能保证“不替它选结构”，但不能保证“不替它选自由度”；如果一个任务只暴露 2 个旋钮，
模型仍被关在 2 维子空间里——这时该被追问的是任务卡，不是几何内核。

---

## 附：证据索引（本地副本）

- `skills/blender-modeling/SKILL.md:195,209,212,217-218` — 强制/唯一正确/例外只有一种/op-first。
- `src/index.ts:1683-1694` — `blender_rt_do` 执行任意 Python；描述里仍写“有 plan op 先用 op”。
- `runtime/gate.py:1066-1090` — `gate_deliver` 的 blocked 语义与“给弱模型的一键强制路径”。
- `runtime/deliver.py:403,438-439` — `require_gate=False` 默认；“下一个 minor 计划默认开启”。
- `runtime/surface.py:11,15,84-87,300-381` — M3/M5 未实现；spec_hash 仅覆盖 surface。
- `package.json:4` — 15 tools / 34 family / 228 op。
- `README.zh-CN.md:23,26` — spec_hash 卖点；门不过拦住交付。
- `docs/flash建模上限-整合方案.md:3,12,92` — 义肢论；长尾 5 族使用 0 次；“不要训练眼睛和手”。
- `docs/假设驱动建模-cookbook.md:258,260` — 契约层在纯设计驱动任务里“没有触发条件”（仓库自己承认适用面有限）。
