# 可直接复用的提示词（比上一版更贴近真实仓库）

> 上一版把目标写成了“Blender addon”，但 `dsh-blender-plugin` 是 **DSH 宿主的 Cordis 插件**，
> 不是 Blender 的 addon。这个版本把落点、相位、能量语义和验收都写成 DSH 能执行的条件。

---

在本机安装 Blender，并为 DSH 创建一个 host-only 插件。唯一验收标准：
**背后的形状与约束不过分影响模型发挥**——约束不得在生成之前替模型做决定。

## 阶段一：安装 Blender

1. 优先 `winget install -e --id BlenderFoundation.Blender --silent --accept-package-agreements --accept-source-agreements`。
2. 如果 winget 不可用或无权限，用便携版：从清华镜像
   `https://mirrors.tuna.tsinghua.edu.cn/blender/release/Blender4.5/` 取最新的
   `blender-*-windows-x64.zip`，解压到 `D:\Blender\`。
3. 验收：`blender.exe --version` 返回 4.5.x；`blender.exe -b --factory-startup --python-expr "import numpy; print(numpy.__version__)"` 通过。
4. 把可执行文件路径写入用户环境变量 `BLENDER_PATH`，并在用户 PATH 里加一个 `blender.cmd` shim；
   记下绝对路径，插件配置要用。

## 阶段二：插件（DSH bundle，不是 Blender addon）

包名 `dsh-blender-soft`，`package.json` 声明 `dsh.bundle.patch`，`index.js` 用
`ctx.inject(['tools'], mount)` + `inner.tools.register({...})` 手动注册（不要依赖 `defineTool` 的解析）。
只做一个工具：`blender_soft`，`op=propose|energy|refine|doctor`。

### 硬性算法要求

1. **主通道是隐式场**：genome 由模型自己组合的 SDF 基元（sphere/ellipsoid/box/capsule/cylinder/torus）
   + 平滑 CSG（`union|subtract|intersect`，`k`）+ `mirror` + `knobs` 构成；surface nets 提网格。
   拓扑由零等值面涌现，不预先锁截面/轨道。
2. **参数化基元降级**：loft/sweep/patch 只做可选精修器，不能是唯一几何出口。
3. **约束是能量，不是门**：`{id, weight, schedule:{start,end}, target, tol}`；
   `t<start` 时权重严格为 0；`refine` 只动调用方显式暴露的 `tunables`；
   每个 term 的梯度先按自身尺度归一，再做回溯线搜索，保证同 `t` 总能量不升。
4. **没有 pass/fail、没有 blocked、没有 verdict**；失败维度以 `signals`（measured/target/gradient top/suggestion）回注。
5. **哈希是谱系不是身份证**：`genome_hash` 标识提议；`lineage_hash` 包含 genome+terms+tunables+seed+schedule；
   可复现的是轨迹，不是唯一顶点集。
6. **工具面只加一个**：`blender_soft` 四个 op；不要再造 10 个工具，也不要把 catalog 扩成第 229 个 op。
7. **headless 验收**：`blender -b --factory-startup --python runtime/blender_runner.py -- job.json`；
   同一 genome 跑 `mode=soft` 与 `mode=hard`：
   - soft 前几步 `max_delta_norm=0`（权重未到），之后每步 ≤ `lr`；
   - hard 只在最后一步出现一次大跳；
   - 输出两者轨迹（`max_delta_norm`、`drift_mm`、约束值）与 PNG。
8. **安装与 live 验证**：`plugin_manager install_bundle` 安装；用 `cordis_inspect_query` 的 Tool 提供者
   确认 `blender_soft` 出现在当前 Agent 的工具列表；实际调用一次，确认返回文本 + 图像 + 轨迹。
9. 最后必须回答：**这个插件仍然可能在哪个环节压缩模型发挥？用什么机制把它压到最低？**

### 反面清单（不要做）

- 不要把 `spec_hash` 当几何身份证；不要用 `pass/fail` 做生成期筛选；不要“门不过就 block”；
- 不要只换表示（loft→SDF）却保留原来的 op-first/每步过门规范；
- 不要重写上游 45k 行内核——先改 `SKILL.md` 的强制段落、`gate.py` 的相位、`deliver.py` 的默认绑定，
  再把软场作为探索层接入。
