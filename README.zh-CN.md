# Richard's Video Kits（RVK）

[English](README.md)

一组 ComfyUI 视频节点：逐段保存视频、用一份可编辑的官方 Wan Animate 2 单段图自动处理驱动视频，并将完整片段合成为带原视频声音的成品。

当前代码版本为 **1.0.0**。本仓库包含稳定运行代码、用户说明和示例工作流，已发布到 [Comfy Registry](https://registry.comfy.org/richard34512/richards-video-kits)。当前版本的 ComfyUI Manager 可搜索 **Richard's Video Kits** 或 `richards-video-kits` 安装。

## 安装

已验证环境：**Windows / NTFS、Python 3.12、ComfyUI v0.36.0、frontend 1.52.7、`--cache-classic`**。真实 Wan 使用 RTX 4080 验证。其他组合尚未验证。

1. 在当前版本的 ComfyUI Manager 中搜索 **Richard's Video Kits** 或 `richards-video-kits`，安装 `1.0.0` 后重启 ComfyUI。若缓存频道未立即显示新项目，请刷新或切换到远程频道。
2. 只保留一份 RVK 安装。插件目录应直接包含 `__init__.py`、`rvk/` 和 `web/`，避免多嵌套一层。
3. RVK 使用 ComfyUI 已有环境中的 PyAV、NumPy 和 PyTorch，无额外 pip 依赖。最终合成要求 ComfyUI 进程的 `PATH` 能找到带 AAC 编码器的 FFmpeg；已测试 FFmpeg 7.0.2。
4. 以 `--cache-classic` 启动，刷新浏览器。搜索 RVK，应看到 Save Segment Video、Finalize Segments、Wan Animate 2 Loop Entry、Collect、Advance 五个节点，然后导入[完整循环示例](examples/wan_animate2/wan_animate2_rvk_loop.json)。示例不附带模型或素材。

首次安装可在 ComfyUI 停止时，从 ComfyUI 根目录执行：

```sh
git clone https://github.com/Richard-Wang-fs/ComfyUI-RichardsVideoKits.git custom_nodes/ComfyUI-RichardsVideoKits
```

如果已经以其他目录名安装过 RVK，先停止 ComfyUI，将旧插件目录移到 `custom_nodes` 外保留备份，再克隆，避免两份插件重复加载。已保存视频仍位于 ComfyUI 的 output 目录。

## 使用

选择参考图、完整且未裁剪的恒定帧率（CFR）驱动视频、官方模型和 prompt。在 Entry 设置段长及一个新的空输出目录；段长默认 81，可选范围为 5～16381 的 `4k+1`，较长段占用更多内存。保留示例已有连线，只 Queue 一次并保持工作流打开。

每段视频保存成功后才续排下一段。运行期间不要编辑图或更换输入；需要停止时点击 Entry 上的 **Stop RVK after current segment**。

独立片段为无声 `segment_0000.mp4` 等文件。全部完成后，Finalize 将视频流无重编码拼接，并把驱动视频第一条音轨编码为 AAC，生成 `final.mp4`。中间片段保留，已有文件不覆盖。

重启后若所有片段均完整，可导入[独立成品示例](examples/wan_animate2/finalize_existing_segments.json)，选择同一原始驱动视频并填写片段目录，无需再次运行模型。它不恢复未完成的生成任务；重新生成需使用新的空目录。

## 输入和支持边界

- 视频必须来自文件、为 CFR，且时间基能精确表示帧号。最终合成还要求音轨从视频零点开始并覆盖完整视频；无音轨、明显偏移或音轨不足均明确拒绝，已保存片段不受影响。
- 缺号、残留 partial、片段不兼容、总帧数不符或成品已存在时停止。`.partial.mp4` 不算成品；确认没有写入进程后，再按报错处理。
- 已有真实 Wan 三段含短尾、公开 Stop，以及独立 12 段 / 903 帧带 AAC 合成证据。三段短素材本身无音轨，其合成按预期拒绝。
- 约三分钟完整资源曲线、执行中中断、超过 30 分钟的单段、其他文件系统和默认 RAM-pressure cache 的同等内存表现尚未验证。
- 不保存恢复张量或永久图片序列，不自动恢复生成，不下载模型。通用 Save 和 Finalize 可独立使用。

详细操作与音轨约束见[示例说明](examples/wan_animate2/README.md)，发布维护说明见 [PUBLISHING](PUBLISHING.md)。

## 许可证

RVK 自有代码采用 [PolyForm Noncommercial 1.0.0](LICENSE)，属于**非商业用途的源码可见软件**，并非 OSI 定义的开源许可。该许可不授予商业使用权；如需商用，请联系 [Richard-Wang-fs](https://github.com/Richard-Wang-fs) 获取单独授权。

上游官方工作流模板部分保留原 [MIT 声明](licenses/ComfyUI-workflow-templates-MIT.txt)。软件许可证不授予模型、输入素材或生成媒体的权利，其适用条款另行遵守。
