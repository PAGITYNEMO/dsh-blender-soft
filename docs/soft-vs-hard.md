# Soft vs hard：同一形状、同一组约束、两种介入方式

> 目的：证明“约束在候选出现后以小步谈判”与“在出口处一次性推回去”是两种不同的数学对象，
> 而不是两种参数风格。几何层可复现；LLM 层的创造力差异不在本实验范围内。

## 设置

- 同一个 SDF genome：球体 + 左右镜像胶囊臂（`mirror:"x"`）+ 中心圆柱通孔；`surface nets` 64³，bounds ±75 mm。
- 同一组 terms：`thickness`（target 6 mm，q=0.10）、`symmetry`（x=0，weight 0.4）、`volume`（target 80,000 mm³，tol 25%）。
- 同一个优化器：有限差分梯度、每 term 归一化、5 次回溯线搜索、5 个 tunables。
- 唯一差别：`mode=soft` 的权重按 schedule 从 t=0 升到 1；`mode=hard` 把所有 term 的 schedule 压到 `{start:0.98,end:1.0}`，并在最后一步用 0.5 的大步长一次性施加。

## 结果

| 指标 | soft（schedule） | hard（单步闸机） |
|---|---|---|
| 最大单步参数位移（归一化） | 0.080 | 0.455 |
| 最大单步表面漂移 | 1.42 mm | 7.98 mm |
| p10 厚度（目标 ≥6 mm） | 11.075685 mm | 13.815079 mm |
| 终态体积（目标 80k ±25%） | 88,509 mm³（ok） | 187,873 mm³（above） |
| 终态对称性 rms | 0.237 mm | 1.458 mm |

soft 的轨迹（前 3 步权重为 0，形状完全不动）：

| step | t | 最大 Δ | drift | p10 厚度 |
|---:|---:|---:|---:|---:|
| 0 | 0.0 | — | — | 6.64 |
| 1 | 0.1111 | 0.000 | 0.00 | 6.64 |
| 2 | 0.2222 | 0.000 | 0.00 | 6.64 |
| 3 | 0.3333 | 0.080 | 0.87 | 6.62 |
| 4 | 0.4444 | 0.080 | 1.42 | 8.92 |
| 5 | 0.5556 | 0.080 | 0.95 | 10.23 |
| 6 | 0.6667 | 0.080 | 0.67 | 10.36 |
| 7 | 0.7778 | 0.080 | 0.40 | 10.99 |
| 8 | 0.8889 | 0.020 | 0.39 | 10.64 |
| 9 | 1.0 | 0.040 | 0.34 | 11.08 |

hard 的轨迹（前 9 步完全不动，最后一步跳过 7.98 mm）：

| step | t | 最大 Δ | drift | p10 厚度 |
|---:|---:|---:|---:|---:|
| 0 | 0.0 | — | — | 6.64 |
| 1 | 0.0 | 0.000 | 0.00 | 6.64 |
| 2 | 0.0 | 0.000 | 0.00 | 6.64 |
| 3 | 0.0 | 0.000 | 0.00 | 6.64 |
| 4 | 0.0 | 0.000 | 0.00 | 6.64 |
| 5 | 0.0 | 0.000 | 0.00 | 6.64 |
| 6 | 0.0 | 0.000 | 0.00 | 6.64 |
| 7 | 0.0 | 0.000 | 0.00 | 6.64 |
| 8 | 0.0 | 0.000 | 0.00 | 6.64 |
| 9 | 1.0 | 0.455 | 7.98 | 13.82 |

## 图像证据

soft：自由提议 → 精修后

![free](../dsh-blender-soft/runs/demo_soft/preview_00_free.png)

![soft refined](../dsh-blender-soft/runs/demo_soft/preview_02_refined.png)

hard：同一起点，单步闸机后（厚度达标，但体积越带、对称性变差）

![hard refined](../dsh-blender-soft/runs/demo_hard/preview_02_refined.png)

## 解释

1. **soft 前 3 步不动**：`thickness.schedule.start=0.30`，在 t<0.30 时 `w_eff=0`。
   这不是“收敛慢”，而是设计目标：先让模型拥有一个不受约束的候选，再让约束介入。
2. **soft 每步 ≤0.08（归一化）**：线搜索把任何会让同 t 总能量上升的步长砍掉；drift 0.26–1.45 mm。
3. **hard 单步 Δ=0.455、drift=7.98 mm**：所有权重一次性施加，等价于可行集投影/拒绝采样；
   它确实把 p10 厚度推过目标，但体积从 67k 冲到 188k（越过 80k ±25%），对称性从 0.24 变 1.46。
   这就是硬门的边界效应：满足一个维度，牺牲另一个维度。
4. **soft 终态体积 88,509（ok）**：因为 volume term 一直在场（schedule 0→0.4），
   它和 thickness 同时被优化，而不是先被忽略、最后被 gate 补一刀。

## 复跑

```bash
cd dsh-blender-soft
D:/Blender/blender-4.5.15-windows-x64/blender.exe -b --factory-startup \
  --python runtime/blender_runner.py -- runs/demo_soft/job.json
D:/Blender/blender-4.5.15-windows-x64/blender.exe -b --factory-startup \
  --python runtime/blender_runner.py -- runs/demo_hard/job.json
```

> 口径：drift 用有符号距离场的双向表面距离（`energy.field_drift`），不是顶点云 Chamfer；
> 后者在稀疏采样下会把 0.5 mm 的偏移读成 1.5 mm，已在本仓库测试中弃用。
