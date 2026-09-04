# 混合语言实验结论

更新时间：2026-08-31  
本文件是 `experiments/` 下唯一的实验记录，所有结论和对应试听音频已收敛到这里。

## 固定条件

| 项目 | 值 |
| --- | --- |
| GPT | `s1v3.ckpt` |
| SoVITS | `v2Pro/s2Gv2Pro.pth` |
| 参考文本 | `这条路走到头，左转就到了。` |
| 语言模式 | `segment_mixed` |
| seed | `424242` |
| 推理约束 | 每条一次 GPT/SoVITS 推理、一次 acoustic decode |

## 核心结论

1. 五语种 `segment_mixed` 可以在固定 D 组模型上完成一次混合推理，不依赖音频拼接。
2. 非末段终止标点在内部归一化为边界 pause、末段保留原文标点，解决了高风险短句漏读问题。
3. 自动动态 EOS 在 WebUI 听测中获得正向反馈；手动 `mixed_eos_floor=11` 只作为诊断参数，会加速语速并导致短句漏读。
4. 动态 EOS/cap 在 short 回归中无 hard-cap；prefix 回归仍有语速漂移和 6 次 cap 命中，不能宣称全量稳定。
5. `pause_after_ms` 当前是布尔效果（有/无 pause marker），不代表精确毫秒时长。

## 对应试听音频

| 文件 | 结论 | 说明 |
| --- | --- | --- |
| `baseline_five_forward.wav` | 结论 1 | 五语种正向顺序，单次推理基线 |
| `ablation_product_demo_short_3.wav` | 结论 2 | 去掉非末段句号后，`预览完成后再导出。`恢复完整 |
| `ablation_city_narration_short_1.wav` | 结论 2 | 去掉非末段句号后，`清晨的海风吹过老街。`恢复完整 |
| `regression_product_demo_short_3.wav` | 结论 2 | 最终回归确认，EOS=11 高风险条件下无破坏 |
| `regression_city_narration_short_1.wav` | 结论 2 | 最终回归确认 |
| `prefix_product_demo_prefix_5.wav` | 结论 4 | 长 prefix 代表样本，存在语速漂移风险 |
| `short_product_demo_short_1.wav` | 结论 4 | short 回归代表，动态 EOS/cap 无 hard-cap |
| `pause_alternating_pause_0.wav` | 结论 5 | 无 pause marker 对照 |
| `pause_alternating_pause_120.wav` | 结论 5 | 有 pause marker 对照；与 80/200 无本质差异 |

## 关键量化数据

| 实验 | 样本数 | 总时长 | 总 tokens | hard-cap |
| --- | ---: | ---: | ---: | ---: |
| 基线 | 5 | 42.63 s | 1052 | 0 |
| 位置矩阵 | 21 | 153.03 s | 3768 | 0 |
| Pause 对照 | 12 | 113.20 s | 2797 | 0 |
| EOS=11 归档 | 5 | 66.35 s | 1645 | 0 |
| 标点消融 | 2 | 17.30 s | 427 | 0 |
| 动态 EOS/cap | 5 | 63.99 s | 1586 | 0 |
| Prefix cap | 25 | 309.51 s | 7669 | 6 |
| Short cap | 15 | 132.17 s | 3263 | 0 |
| 标点回归 v2 | 2 | 17.98 s | 444 | 0 |

## 生产候选规则

用户可见原文不变，内部仅把非末段终止性标点归一化为边界 pause，最后一个片段保留原句末标点。该规则已通过高风险短句回归，但仍需完整五主题矩阵回归后才能提升为生产默认。

## 已收敛

以下材料已并入本文档或由本文档替代后删除：

- 8 个实验子目录及其 `run_config.json`
- 各批次的 `manifest.jsonl`、`listening.html`、`report.md`、`analysis.md`
- `CONSOLIDATED_REPORT.md`、`README.md`、基线 `README.md`
- 未入选代表音频的其他 83 个 WAV
