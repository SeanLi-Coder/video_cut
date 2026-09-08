# 本地视频剪辑、逐帧截图、永久旋转与 AI 超清

一个面向 macOS 和 Windows 11 的跨平台本地视频处理工具，提供“剪辑视频”“逐帧截图”“永久旋转”和“AI 超清”四种模式，并带独立的 AI 模型预下载与进度管理页面。macOS 双击 `start.command`，Windows 便携版双击 `LocalVideoCutter.exe` 即可启动；视频不会上传，原文件也不会被修改。

## 零基础使用方法

### 1. 下载并解压

本仓库是 private 仓库。打开 Releases、下载 Release 文件或使用 **Code → Download ZIP** 前，必须先登录已获本仓库访问权限的 GitHub 账号。Windows 11 / RTX 5090 用户建议打开 [GitHub Releases](https://github.com/SeanLi-Coder/video_cut/releases/latest)，下载名称以 `LocalVideoCutter-Windows-RTX5090` 开头的 ZIP；macOS 或需要源码的用户可以在项目页面点击 **Code → Download ZIP**。如果 RTX 5090 电脑不方便登录 GitHub，请先在有权限的电脑上下载并核对文件，再把完整 ZIP 复制到 Windows 电脑。

不要直接在 ZIP 压缩包预览窗口里运行，也不要只复制其中的 EXE；请先把整个文件夹解压到“下载”或“桌面”等普通可写目录。

### 2. 双击启动

打开解压后的文件夹，根据系统双击启动：

- macOS：`start.command`
- Windows 11 便携版：`LocalVideoCutter.exe`
- Windows 11 源码版：`start.bat`

首次启动会做这些事情：

- macOS 查找 Python 3.10 或更高版本；Windows 查找 Python 3.12，缺少时尝试用 WinGet 自动安装；
- 在项目内创建独立的 `.venv` 环境；
- 安装本工具所需的 Python 依赖；
- 检查 FFmpeg 和 FFprobe；
- 启动仅供本机访问的网页，并自动在浏览器中打开。

第一次安装需要联网，可能持续几分钟。启动时打开的 Terminal 或 Windows Console 窗口在工具运行期间需要保持打开。

### 3. 提前下载 AI 模型（可选）

服务启动后，Terminal 或 Windows Console 会逐个询问是否立即准备当前设备兼容但尚未完整就绪的 AI 模型。输入 `y` 并按 Enter 会安装所需环境、下载缺少的文件，并显示百分比、速度和预计剩余时间；输入 `n` 或直接按 Enter 会跳过这个模型并继续启动，不会禁用基础视频功能，也不会阻止以后再下载。Apple Silicon 只会询问稳定版 SeedVR2；Windows RTX 5090 还会询问实验版 SwiftVR。处于 blocked 状态的 FlashVSR 不会进入启动询问。

普通下载支持断点续传。如果网页下载曾失败、取消，或磁盘上留有部分文件，下次运行启动器会显示 `Continue, restart from zero, or skip? [c/r/N]`：输入 `c` 从已下载位置继续，输入 `r` 只清理当前这个模型的权重并从零重下，输入 `n` 或直接按 Enter 跳过。清理不会删除已安装的 AI runtime，也不会影响其他模型。即使本地网页服务已经在运行，再次运行 `start.command`、`LocalVideoCutter.exe` 或 `start.bat` 也会先显示可恢复下载的模型，然后再重新打开网页。

如果下载模型需要代理，启动窗口会在第一个新下载或恢复任务之前显示当前设置：在 `AI download proxy [Enter=keep, s=set/change, c=clear]` 处直接按 Enter 沿用，输入 `s` 后可填写例如 `socks5://127.0.0.1:7897` 或 `http://127.0.0.1:7897`，输入 `c` 则清除。代理账号可选，密码输入不会回显；保存后启动器会先做一次连接测试并显示延迟，再继续询问是否下载模型。只有正在进行的任务不会重新询问或中途切换代理。

也可以全部跳过，等浏览器打开后进入独立的“AI 模型管理”标签页。页面上方可以填写、测试、保存或清除同一份下载代理；下方每个模型都有自己的状态、兼容性说明和下载按钮，无需先选择视频或保存目录。页面会显示实际检测到的设备名称、后端、显存或统一内存，以及当前阶段、百分比、已下载容量、下载速度、已用时间和预计剩余时间。切换回“视频处理”不会中断；刷新页面后也会自动找回正在运行的任务。取消后已下载部分会保留，下次点击“继续下载”会断点续传。全部文件通过 SHA-256 校验后，模型才能运行。

应用内代理支持 `http://`、`https://`、`socks5://` 和 `socks5h://`，会同时用于 AI 运行器、模型权重和隔离环境依赖下载。`socks5://` 在本机解析目标域名；如果本地 DNS 也受限，可改用由代理端解析域名的 `socks5h://`。每个任务在开始时固定代理设置，页面中途修改只对下一次新开始或重试生效。代理设置保存在本机项目的 `data/settings.json`，API 和页面不会回传已保存的密码；在共用电脑或复制整个项目目录前，建议先点“清除代理”。首次创建应用自身的 `.venv`、Homebrew 或 WinGet 下载发生在网页服务启动之前，因此这几个最早步骤仍需使用系统/终端代理。

SeedVR2 权重约 7.3 GB，SwiftVR 权重约 20.2 GB，运行环境还会另外占用空间。Apple Silicon 只安装 SeedVR2 建议至少留出约 12 GB，Windows RTX 5090 只安装 SeedVR2 建议至少留出约 18 GB；Windows 同时安装 SeedVR2 与 SwiftVR 建议至少留出约 60 GB。启动时预下载不是强制步骤：SeedVR2 可以在第一次任务时自动准备；SwiftVR 必须先在启动窗口输入 `y`，或稍后到“AI 模型管理”完成准备，网页才会允许开始 SwiftVR 任务。

### 4. 选择视频和保存目录

1. 在网页中点击“选择视频”，从系统文件选择窗口选取本地视频。
2. 点击“选择文件夹”，从系统文件夹选择窗口选取成片或截图的保存目录。这个目录会被记住，下次启动仍可使用，也可以随时更换。

永久旋转不需要选择保存目录：新视频固定生成在原视频同级目录，方便立即找到。

### 5. 选择功能

页面顶部可以选择：

- **剪辑视频**：把所选范围保存为一段新视频。
- **逐帧截图**：把所选范围内的每一帧保存为无损图片；填写或修改起始时间后，结束时间默认自动设为起点后 5 秒（视频剩余不足 5 秒时到视频结尾）。仍可手动修改结束时间，但单次范围最多 5 秒，超过时页面会立即提示并禁止开始。
- **永久旋转**：把整段视频顺时针旋转 90°、180°、270° 或 360°，方向真正写进画面并生成同级新文件。
- **AI 超清**：先选择当前设备上可运行的模型，再处理整段视频。稳定版 SeedVR2 3B FP16 支持 1080p、2K QHD 和 4K UHD；Windows RTX 5090 上的实验版 SwiftVR 5B BF16 当前只开放 1080p。

剪辑和逐帧截图始终只有“起始时间”和“结束时间”两个文本参数。支持以下格式：

| 输入示例 | 含义 |
|---|---|
| `75.5` | 75.5 秒 |
| `01:15.500` | 1 分 15.5 秒 |
| `00:01:15.500` | 0 小时 1 分 15.5 秒 |

起始时间不能小于 0，结束时间必须晚于起始时间，并且不能超过视频总时长。使用冒号时，秒数必须小于 60；使用 `HH:MM:SS` 时，分钟数也必须小于 60。

永久旋转不需要填写时间，只需点击 90°、180°、270° 或 360°。右侧画面会立即按新角度预览；换一个角度，预览也会立即更新。360° 看起来与原方向相同，但仍会生成一份已固化方向、已清除旋转标记的新视频。

AI 超清也不需要填写时间。选择一个目标清晰度即可；右侧播放的是整段原片内容预览，不是假装实时生成的 AI 效果。AI 成片需要完整计算后才能查看。

### 6. 预览并输出

两个时间都有效后，页面会生成对应片段的预览。修改任意一个时间，预览会按新范围重新生成；逐帧截图模式下修改起始时间时，结束时间也会自动跟随为起点后 5 秒。

剪辑模式下点击“确定并导出”；逐帧截图模式下点击“确定并截图”；永久旋转模式下点击“确定并旋转”；AI 模式下点击“确定并开始 AI 超清”。完成后可以直接点击“在文件夹中显示”。四种操作都会创建新内容，原视频始终保留不变。

运行中的剪辑、逐帧截图、永久旋转和 AI 超清会显示动态预计剩余时间。开始阶段尚无足够速度样本时会显示“正在估算”，进入校验、音频封装等短暂阶段后会显示“正在收尾”；预计时间会根据实际处理速度持续修正，并不是完成时间承诺。

剪辑视频的默认文件名为：

```text
原文件名_clip_起始时间-结束时间.mp4
```

例如：

```text
旅行_clip_00-00-10_000-00-00-25_500.mp4
```

如果同名文件已经存在，程序会自动添加 `_2`、`_3` 等编号，不会覆盖已有文件。常见素材默认导出为 `.mp4`，ProRes 或需要保留多声道/高位深 PCM 的素材会使用 `.mov`；透明通道、16-bit、奇数尺寸等无法安全映射到常见编码的专业素材会使用无损 FFV1 的 `.mkv`。

逐帧截图会放入单独文件夹，例如：

```text
旅行_frames_00-00-10_000-00-00-12_500/
├── frame_000001.png
├── frame_000002.png
└── ...
```

同名截图文件夹也会自动添加 `_2`、`_3`，不会混进已有文件夹或覆盖旧图片。

永久旋转的默认文件名为：

```text
原文件名_rotated_角度.mp4
```

例如 `旅行_rotated_90.mp4`。如果素材需要 MOV 或无损 FFV1 容器，扩展名会自动改为 `.mov` 或 `.mkv`；同名时同样自动添加 `_2`、`_3`，绝不覆盖旧文件。

AI 超清的默认文件名为：

```text
原文件名_ai_1080p.mp4
原文件名_ai_2k.mp4
原文件名_ai_4k.mp4
```

如果主音轨无法安全放进 MP4，程序会按音频编码自动改用 MOV 或 MKV；同名文件同样自动编号。

## AI 超清：模型选择与使用边界

本项目使用固定 revision 和 SHA-256 的模型目录，不会在失败后偷偷改成 FP8、GGUF、逐帧 Real-ESRGAN 或其他低质量替代方案。**SeedVR2 3B FP16** 仍是稳定版和默认选项；Windows RTX 5090 另外提供实验性的 **SwiftVR 5B BF16**。**FlashVSR v1.1 Full** 的模型卡可以选择查看技术状态与 blocked 原因，但当前不可下载、不可运行。

| 模型 | 状态 | 可用设备 | 当前目标档位 | 权重下载 | 启动时询问 |
|---|---|---|---|---:|---|
| SeedVR2 3B FP16 | stable、默认 | Apple Silicon MPS；Windows 11 RTX 5090 CUDA | 1080p、2K、4K | 约 7.3 GB | 是 |
| SwiftVR 5B BF16 | experimental | 仅 Windows 11 RTX 5090 CUDA | 仅 1080p | 约 20.2 GB | 是 |
| FlashVSR v1.1 Full | blocked | RTX 5090 的 CUDA 页面中仅可见 | 暂不开放 | 不允许下载 | 否 |

SeedVR2 会同时利用相邻帧恢复细节，能减少逐帧图像模型常见的纹理闪烁。选择 3B FP16 不是为了省时间：SeedVR2 官方论文的专家盲测中，蒸馏后的普通 3B 模型相对普通 7B 模型，在 Visual Quality 和包含时序一致性的 Overall Quality 上都高出 16%。后加的 7B sharp checkpoint 没有对应官方盲测，不能把“更锐”直接当成“总体更好”。依据见 [SeedVR2 论文](https://arxiv.org/html/2506.05301)和[官方项目](https://github.com/ByteDance-Seed/SeedVR)。这里的“默认”表示本项目当前成熟、跨 MPS/CUDA 的质量优先方案，并不是声称它对所有素材和硬件绝对领先。

SwiftVR 是 5B BF16 的流式一阶段视频修复模型。本版本把它作为 Windows RTX 5090 的可选实验路径，并且只开放 1080p；它不会取代稳定默认值，也不会在 SeedVR2 失败时自动接管。它的安装、模型文件和任务进度与 SeedVR2 独立。当前适配是在 Apple M2 开发机完成的，还没有经过真实 RTX 5090 推理验收，因此请先用短片核对显存峰值、速度、帧数、颜色、音频和成片质量，再决定是否处理长片。

[FlashVSR](https://github.com/OpenImagingLab/FlashVSR) 的论文结果值得关注，但 Full 高质量路径依赖 [Block-Sparse-Attention](https://github.com/mit-han-lab/Block-Sparse-Attention)。官方 BSA 目前尚未完成 Windows RTX 50 / `sm_120` 的兼容性与质量验证，所以本项目将 FlashVSR 标为 blocked：模型卡会说明原因，但下载和运行按钮保持禁用，也不会出现在启动下载询问中。程序不会用 dense attention 或 Tiny 模型冒充 Full 质量版本。

目标档位定义如下；2K 在本工具中明确指常见的 QHD，而不是 DCI 2K：

| 页面档位 | 横屏边界 | 竖屏边界 |
|---|---:|---:|
| 1080p | 1920 × 1080 | 1080 × 1920 |
| 2K QHD | 2560 × 1440 | 1440 × 2560 |
| 4K UHD | 3840 × 2160 | 2160 × 3840 |

画面保持原始宽高比，不拉伸、不裁边；超宽或其他比例会落在对应边界以内。同分辨率可以做 restoration；如果某个档位会缩小原片，该按钮会禁用。

稳定或实验模型支持以下本地加速路径：

- Apple Silicon Mac：SeedVR2 使用 PyTorch MPS。
- Windows 11 + NVIDIA GeForce RTX 5090：SeedVR2 使用 PyTorch `2.12.1` 的 CUDA 13.0（`cu130`）官方 wheel；SwiftVR 使用隔离的 PyTorch `2.10.0` `cu130` 环境。

SeedVR2 的两条路径使用完全相同的 3B FP16 权重和 PyTorch SDPA attention。RTX 5090 路径不会默认换成 FP8，也不会默认安装 SageAttention、Apex、FlashAttention 或额外 Triton kernel；这样可以保留 FP16 质量并避免把一次性编译、第三方二进制兼容性变成稳定路径的前置条件。程序不会因显存不足静默降低模型精度，也不会把一个模型的任务悄悄换给另一个模型。

启动器会在本地网页服务就绪后检查当前设备兼容、允许启动提示且尚未完整准备的模型。若至少有一个任务尚未开始，会先显示 `AI download proxy [Enter=keep, s=set/change, c=clear]`，网页和命令行共用这份设置。新下载随后显示 `Download this model now? [y/N]`；检测到失败、取消或残留的部分文件时，会改为显示 `Continue, restart from zero, or skip? [c/r/N]`。`c` 使用 HTTP Range 从断点续传，`r` 只删除所选模型的已有权重和 `.download` 文件后从零重下，`n` 或直接按 Enter 跳过。重下不会删除该模型的 runtime 或任何其他模型。某个模型准备失败不会阻止基础工具启动。已有实例运行时再次启动程序，也会检查并显示恢复提示；如果同一模型仍在下载，则直接在命令行接管其进度显示。自动化或无人值守启动可传入 `--skip-model-prompt`，它会同时跳过命令行代理与模型询问，不会隐藏网页模型管理功能。

也可以在独立的“AI 模型管理”标签页按模型提前安装、继续下载或取消。安装过程会创建隔离环境，下载固定 revision 的运行器与权重，并逐文件校验 SHA-256。SeedVR2 下载约 7.3 GB 的 3B FP16 与 VAE 权重；SwiftVR 下载约 20.2 GB 的 5B BF16 权重。blocked 的 FlashVSR 不会开始下载。

模型管理页支持查看每个模型的实时百分比、下载容量、速度、耗时和预计剩余时间。中途取消会停止下载或安装进程；已完整下载的文件会保留，`.download` 临时文件也会保留，普通重试会自动断点续传。如果用户在启动窗口明确选择从零重下，程序只会清理所选模型的权重和校验记录，保留 runtime 与其他模型。AI 视频任务中途取消时会停止整个 AI/FFmpeg 进程组并删除未完成成片。已通过完整性检查的模型不会在下次启动时重复安装或下载。

Apple Silicon 有不同统一内存容量，RTX 5090 提供独立显存。SeedVR2 会根据当前设备和目标尺寸调整同一个 FP16 模型的时序 batch，不会降低模型精度；RTX 5090 默认对 1080p、2K、4K 分别使用 `21`、`13`、`5` 帧 batch。1080p 的 `21` 来自上游 24 GB+ FP16 推荐配置，2K 与 4K 按像素量和 32 GB 显存保守缩小。batch 越高通常越有利于时序一致性，但三档仍必须在实机用短片验证显存峰值。1080p、2K、4K 的内存需求相差很大，4K 仍可能因可用内存不足而明确失败。SwiftVR 当前只允许 1080p，不会为了接受 2K/4K 请求而静默改变参数。4K 是本工具的输出尺寸档位，不代表任一模型对任意素材都给出了 4K 质量保证。关闭占用大量内存或显存的软件后重试，或选择较低目标档位。时序生成模型在重度退化或大幅运动素材上仍可能恢复失败，原本已经很清晰的素材也可能被过度生成或锐化；AI 生成的细节不是原片中可证明存在的真实细节。

> RTX 5090 验收状态：SeedVR2 CUDA 与 SwiftVR 的当前适配代码是在 Apple M2 开发机上完成，可通过静态检查、mock 设备测试和无 CUDA 的自动化测试，但这些不能替代真实显卡运行。正式处理重要长视频前，仍必须在实际的 Windows 11 + RTX 5090 电脑上完成各模型安装、短片推理、显存峰值、取消任务和成片校验；在这一步完成前，不应把“M2 上测试通过”理解为“5090 实机已经验收”。

AI 输入不再限定为 BT.709 limited YUV。所有输入都会由 FFmpeg Full 的 `libplacebo` 转换为 BT.709 primaries、sRGB transfer、full-range 的 RGB 工作画面，再送入用户明确选择的模型。SeedVR2 与 SwiftVR 都使用逐帧流式管线，不会为整段视频生成全长无损中间文件。full-range、BT.601/P3/BT.2020 SDR、RGB 以及 10/12-bit 素材都走同一条受控管线；普通 SDR 缺少色彩标签时，会按 RGB/HD/NTSC SD/PAL SD 的分辨率与帧率规则推断，并在开始前显示具体假设。原文件不会修改。

HDR/PQ/HLG、Dolby Vision 和 HDR10+ 也可以处理，但当前可运行模型的工作空间是 SDR：程序会使用 `libplacebo` 的 BT.2446 Method A tone mapping 与感知式 gamut mapping 转为 16-bit sRGB 工作空间后再增强。最终文件名会带 `_sdr`，并且成片不再是 HDR；原 HDR 峰值亮度、广色域和动态元数据无法保留。若 HDR 的 transfer 标签缺失、无法确定是 PQ 还是 HLG，程序会停止而不盲猜。方向未固化、透明通道、非方形像素、隔行、VFR 或未知像素格式仍会明确停止。带旋转标记的视频请先用本工具的“永久旋转”处理；AAC 的 ADTS/MPEG-TS 输入也会停止，因为换容器时无法保证音频包逐字节不变。

AI 视频从模型输出的 sRGB full-range RGB 显式进行 transfer、matrix、range 和色度采样转换，生成 BT.709 limited 成片；编码使用 CPU `libx265`、10-bit HEVC、`preset slow`、`CRF 10`，不使用 VideoToolbox 快速硬编。这一步也可能很慢。主音轨从原片 bit-for-bit stream copy，不重新压缩，并在完成前逐包计算 SHA-256 指纹核对。AI 会重建视频画面，因此“超清”不可能是原视频码流无损。

## 画质与音质说明

最终成片采用精确剪辑，保持源视频的分辨率和帧率。时间可以输入到毫秒，但视频只能在实际帧边界处切分，因此最终边界仍受源视频帧率限制。

- 普通视频使用 H.264、`CRF 14`，属于视觉无损级高保真输出。
- 常见的单声道/双声道音频使用 ALAC 无损编码，避免导出过程再次产生有损音频压缩；如果源音频原本就是 AAC 等有损格式，ALAC 不会恢复已经丢失的信息。
- HEVC/H.265 或 10-bit/12-bit 素材会继续使用 HEVC，并在 FFmpeg 支持时保留原像素格式，不会强制降成 8-bit H.264。
- ProRes 素材按帧内编码特性直接复制视频码流，保留其像素格式（包括 ProRes 4444 透明通道）；音频按下一条规则处理。
- 透明通道、超过 12-bit、奇数尺寸或其他无法安全映射的专业画面会改用 FFV1 无损编码；文件会更大，但不会为了兼容性静默丢失位深或透明度。
- 单声道/双声道音频使用 ALAC；多声道或超过 24-bit 的音频改用匹配精度的 PCM，并在导出后核对声道数、布局、采样率与位深。

如果多声道源文件本身没有声明声道布局，工具会明确停止，而不是猜测声道顺序后静默导出。

这里的“保持清晰程度”不等于逐字节复制。除适合逐帧切分的 ProRes 外，多数视频会重新编码，因此不是 bit-identical，也不是数学意义上的视频无损。对常见长 GOP 视频，任意切点的精确剪辑与完全不重编码不能同时保证：单纯使用 stream copy 虽然不重编码，但起点通常只能落在关键帧附近。本工具优先保证你输入的剪辑范围准确，并使用高质量或无损编码避免可察觉的降质。

高质量重编码可能比原视频片段更大，导出速度也取决于视频时长、分辨率和电脑性能。
剪辑模式下，HDR 片段会优先直接播放原片；如果浏览器无法解码，才生成一份 8-bit 兼容定位预览，此时颜色只用于定位，剪辑成片仍走原 HDR 色彩信息的导出路径。Dolby Vision、动态 HDR 元数据、多音轨、字幕或沉浸式音频等专业素材不保证完整保留；这类文件请先备份，并抽查一小段成片。本工具默认保留主视频流和主音轨。

永久旋转会把方向真正烘焙到每一帧中，因此视频画面必须解码后重新编码，无法同时做到视频码流逐字节不变。工具会优先保留源编码家族、帧率、位深、像素格式、色彩标记、静态 HDR 信息和画面清晰度；FFV1 继续使用 FFV1，H.264/HEVC 使用高质量编码，无法安全映射的专业格式改用无损 FFV1。90° 与 270° 必然交换宽高，180° 与 360° 保持宽高。原音频直接 stream copy，不进行二次音频编码。

为避免静默损坏专业素材，永久旋转会先快速扫描整段视频的显示元数据；隔行扫描视频以及含 Dolby Vision/HDR10+ 动态元数据的视频会在写入画面前明确停止并提示。ProRes 4444 的高位深或透明通道无法安全写回原容器时，会改用 FFV1/MKV；不兼容的 MOV data/timecode 轨不会被塞进 MKV，主视频、音频和常规元数据仍会保留。永久旋转默认处理主视频流；额外视频轨和封面图不在保证范围内。

逐帧截图不会使用 JPEG，也不会缩放或人为补帧：程序会把时间戳位于所选 `[起始时间, 结束时间)` 范围内的每一个真实视频帧，按原始显示宽高进行无损保存。普通素材使用 PNG；高于 8-bit 的素材使用 16-bit PNG，透明通道会保留；极少见的浮点画面使用 OpenEXR。截图必须先解码，YUV 视频也需要转换为图片可表示的颜色格式，因此图片不是原压缩码流的逐字节副本。HDR/Dolby Vision 的动态显示元数据无法完整放入普通 PNG，不同看图软件的颜色显示可能不同。

5 秒的高分辨率、高帧率逐帧截图也可能占用数 GB 空间。开始前程序会保守检查可用空间；空间不足时会停止并提示，不会悄悄降低尺寸或改用有损图片。

## 安装要求与自动处理边界

基础功能支持 macOS 和 Windows 11：macOS 需要 Python 3.10 或更高版本，Windows 启动器固定使用 Python 3.12；两者都需要包含所需编码器的 FFmpeg。AI 超清还需要带 `libplacebo` 与 `zscale` 的 FFmpeg Full，以及下面一种设备：

- Apple Silicon（M 系列芯片）、足够的统一内存和至少约 12 GB 模型/环境磁盘空间；`libplacebo` 还需要 MoltenVK。此设备只运行 SeedVR2。
- NVIDIA GeForce RTX 5090，以及 R580 或更新分支的 NVIDIA 驱动。只安装 SeedVR2 建议至少留出约 18 GB；同时安装约 20.2 GB 权重的 SwiftVR 建议至少留出约 60 GB。两个 CUDA 模型使用各自固定的 `cu130` PyTorch 环境，wheel 已包含所需 CUDA 用户态运行库，**不需要另外安装 CUDA Toolkit**。

成片目录需要单独的输出空间。RTX 5090 的驱动不会由本工具自动升级；如果 `nvidia-smi` 不可用或驱动过旧，基础视频功能仍可使用，AI 模型页会显示具体原因。

### macOS

`start.command` 会优先查找 Apple Silicon Homebrew 的 `/opt/homebrew/bin/python3`、Intel Homebrew 的 `/usr/local/bin/python3`，再检查当前 `PATH` 中的 `python3`。

- 已安装 Homebrew 但缺少合适的 Python 时，启动脚本会尝试执行 `brew install python`。
- Apple Silicon Mac 缺少 FFmpeg Full 或 MoltenVK 时，启动器会分别尝试执行 `brew install ffmpeg-full` 和 `brew install molten-vk`；`ffmpeg-full` 是 keg-only，不需要手动 link。基础功能在安装失败时仍可使用。
- 工具不会自动安装 Homebrew。若 Mac 上没有 Homebrew，请先按照 [Homebrew 官网](https://brew.sh/)安装，再重新双击 `start.command`。
- 自动安装需要网络连接，并可能要求你在 Terminal 中确认系统提示或输入当前 Mac 账号密码。
- macOS 自带的旧版 `/usr/bin/python3` 不会被替换或修改。

也可以先手动安装 macOS 依赖：

```bash
brew install python ffmpeg-full molten-vk
```

然后检查：

```bash
python3 --version
/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg -version
/opt/homebrew/opt/ffmpeg-full/bin/ffprobe -version
```

### Windows 11 + RTX 5090

推荐使用 Releases 中的 Windows 便携包：完整解压后双击 `LocalVideoCutter.exe`。只需要复制这个解压后的完整文件夹，不需要 Git，也不需要手动执行命令。源码包仍可双击 `start.bat` 启动。

便携版 EXE 是一键启动器，旁边的 `app`、`vendor` 和 requirements 文件是程序本体的一部分，不能只单独复制 EXE。启动器会查找兼容的 Python，建立项目内独立环境，并检查 FFmpeg、FFprobe、`nvidia-smi` 和 RTX 5090。缺少 Python 3.12 或 FFmpeg 时会在可用的情况下通过 WinGet 自动安装；请保留启动窗口，按其中提示处理系统确认。模型不会打进 ZIP。

本地网页服务就绪后，启动窗口会依次询问是否准备尚未完整就绪的 SeedVR2 和实验版 SwiftVR。输入 `y` 安装环境、下载缺少文件并在窗口中查看进度，输入 `n` 或直接按 Enter 跳过；跳过后浏览器仍会正常打开。以后可随时在网页“AI 模型管理”中逐个下载、继续下载、取消并查看进度。网页下载失败、取消或留下部分文件后，再次运行 `LocalVideoCutter.exe` 会显示 `Continue, restart from zero, or skip? [c/r/N]`：`c` 断点续传，`r` 只清理该模型权重后从零重下，`n` 或直接按 Enter 跳过。清理不会动 runtime 或其他模型；即使网页服务已经在运行，再次双击启动器也会显示恢复提示。FlashVSR 是 blocked 状态，不会询问或下载。如果需要无人值守启动，可在 PowerShell 中运行 `LocalVideoCutter.exe --skip-model-prompt`，但普通用户直接双击即可。

便携包应放在桌面、下载目录或其他普通用户可写目录，不要放入 `Program Files`。程序数据会写在便携包内部的 `.venv` 和 `data` 目录，因此移动到另一台 Windows 电脑时应复制整个文件夹；第一次在新电脑上仍会按该机器重新准备运行环境。

请先从 [NVIDIA 官方驱动页面](https://www.nvidia.com/Download/index.aspx)安装 R580 或更新分支驱动并重启。无需下载 CUDA Toolkit、cuDNN 或 Visual Studio CUDA workload；AI 模型页会分别在隔离环境中安装 SeedVR2 所需的 PyTorch `2.12.1` `cu130` 和 SwiftVR 所需的 PyTorch `2.10.0` `cu130`。可以在 PowerShell 先检查：

```powershell
nvidia-smi
py -3.12 --version
ffmpeg -version
```

`nvidia-smi` 应明确列出 `NVIDIA GeForce RTX 5090`。进入网页的“AI 模型管理”后，“运行设备”也应显示 RTX 5090、CUDA 后端和显存；如果页面仍显示不可用，先不要运行长片。当前 Windows CUDA 适配尚未在真实 RTX 5090 上完成验收，尤其是 experimental 的 SwiftVR，请先用几秒钟短片测试，不能把网页显示“兼容”理解为已在这台显卡上验证成片。

## 停止工具

正常情况下，在启动工具的终端窗口按 `Control + C`。如果已经找不到那个窗口，macOS 双击 `stop.command`，Windows 双击 `stop.bat`；Windows 便携版也支持在命令行执行 `LocalVideoCutter.exe --stop`。

导出过程中关闭工具会取消当前导出并清理未完成的临时文件；已经成功完成的文件不会被删除。AI 任务的完整模型缓存不会随取消而删除。

## 隐私与本地处理

- Web 服务只监听 `127.0.0.1`，不向局域网或公网开放。
- 所选视频直接从原位置读取，不会上传到服务器，也不会复制到项目目录。
- 预览、最终剪辑、逐帧截图和永久旋转都由本机 FFmpeg 完成；AI 超清由本机已选择的 SeedVR2 或 SwiftVR、对应的 PyTorch MPS/CUDA 环境，以及 FFmpeg 完成。blocked 的 FlashVSR 不会执行。
- 保存目录设置记录在项目内的 `data/settings.json`；视频内容不会写入该配置。
- 首次安装依赖时会访问 Homebrew 或 PyPI；第一次 AI 任务还会从固定 GitHub/Hugging Face 地址下载运行器和模型，但视频素材始终不会上传。

请只处理你本人拥有、已获授权或法律允许使用的视频。

## 常见问题

### 双击 `start.command` 提示没有权限

打开 Terminal，把项目中的 `start.command` 拖进 Terminal 窗口，在命令最前面补上 `chmod +x ` 后按 Enter；也可以进入项目目录执行：

```bash
chmod +x start.command stop.command
```

随后重新双击启动。

### macOS 阻止打开脚本

在 Finder 中按住 Control 点击 `start.command`，选择“打开”。如果仍被阻止，到“系统设置 → 隐私与安全性”查看并确认对应提示。只应对从你信任的仓库下载的文件执行此操作。

### macOS 提示找不到 Python、FFmpeg 或 FFprobe

先安装 Homebrew，然后执行：

```bash
brew install python ffmpeg-full molten-vk
```

完全关闭旧 Terminal 后重新双击 `start.command`。Apple Silicon Mac 的 Homebrew 通常位于 `/opt/homebrew`。

### Windows 无法识别 RTX 5090 或提示 CUDA 不可用

先在 PowerShell 运行 `nvidia-smi`。如果没有命令、未列出 RTX 5090，或驱动分支低于 R580，请从 NVIDIA 官网安装新驱动并重启。只安装 CUDA Toolkit 不能代替显卡驱动，本项目本身也不需要 CUDA Toolkit。

如果 `nvidia-smi` 正常，但模型页仍不可用，请关闭旧的启动窗口，重新运行 `LocalVideoCutter.exe`（源码版运行 `start.bat`），让模型各自的 AI 环境完成校验。SeedVR2 使用 PyTorch `2.12.1` `cu130`，SwiftVR 使用 PyTorch `2.10.0` `cu130`；不要把系统里另一个 Python 环境的 `torch` 版本当成本项目环境。FlashVSR 显示 blocked 属于预期状态，并不是重新安装 PyTorch 就能解除。

### Windows SmartScreen 显示“未知发布者”

当前 GitHub Release 没有商业代码签名证书，Windows 可能在第一次运行时显示 SmartScreen 提示。请只使用本仓库 Releases 中的 ZIP，并同时下载同页对应的 `.zip.sha256` 文件；确认来源后点击“更多信息”再选择“仍要运行”。SHA-256 可以在 PowerShell 中检查：

```powershell
Get-FileHash .\LocalVideoCutter-Windows-RTX5090-v1.9.0.zip -Algorithm SHA256
Get-Content .\LocalVideoCutter-Windows-RTX5090-v1.9.0.zip.sha256
```

第一条命令输出中的 `Hash` 必须与 `.zip.sha256` 文件第一列的 64 位字符完全相同（忽略大小写）。只要不同，就不要解压或运行该文件，应重新下载并再次核对。

### 无法选择视频或保存目录

macOS 请检查“系统设置 → 隐私与安全性 → 文件与文件夹”，允许 Terminal 访问视频所在目录。Windows 请检查文件是否仍被其他程序独占，以及目标目录是否允许当前账号写入。外接磁盘还需要确认磁盘已挂载且保存目录可写。

### 浏览器没有自动打开

查看启动 Terminal 中的 `Local Video Cutter is ready at ...`，把其后的本地地址复制到浏览器。默认从端口 `8777` 开始；如果端口已被占用，程序会自动选择另一个空闲端口。

### 修改时间后预览没有更新

先确认两个时间均为有效格式且没有超过视频总时长，再点击页面中的“重试预览”。如果原文件已被移动、改名或删除，请重新选择视频。

### 导出、截图或旋转很慢、文件较大

精确剪辑和永久旋转都需要高质量重编码，并非简单复制数据。4K、HEVC、10-bit 和长视频会明显更慢；旋转整段视频需要的空间按原视频所在磁盘计算。逐帧截图使用无损图片，即使只有 5 秒，4K/60 fps 也可能产生数百张图片和数 GB 数据；请保持 Terminal 和网页开启，并确保保存位置有足够空间。为保证长 GOP 视频不漏掉目标范围开头的帧，截图会从视频轨起点精确解码到所选位置，因此截取长视频靠后的范围也可能需要等待；页面会持续显示状态，并可随时安全取消。

### AI 超清一直很慢，或者提示内存/显存不足

这是预期行为。SeedVR2 3B FP16 和 SwiftVR 5B BF16 都是数十亿参数的时序生成模型，本项目优先质量而不是速度，并且最终还会用 CPU 做高质量 10-bit HEVC 编码。几分钟原片可能需要数小时甚至更久。页面会持续显示安装、下载、AI 计算、回封装和验证阶段；可以安全取消。

如果提示 MPS 统一内存或 CUDA 显存不足，请先关闭大型应用和其他 GPU 程序，再选择较低目标档位。程序不会静默换成量化模型或另一个模型。即使同为 M 系列芯片，不同统一内存配置对 4K 的可行性也不同；RTX 5090 上的其他显存占用也会影响可用 batch。SwiftVR 只支持 1080p，无法通过选择 2K/4K 来改善其质量。

### 更新后仍打开旧页面

先按 `Control + C` 停止旧实例，或运行当前系统的停止脚本，再重新运行启动脚本。必要时在浏览器中强制刷新：macOS 通常是 `Command + Shift + R`，Windows 通常是 `Control + F5`。

## 开发与测试

创建开发环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

运行测试：

```bash
python -m pytest
```

让启动器完成依赖、FFmpeg、单实例锁和安全退出检查，但不自动打开浏览器：

```bash
python launcher.py --no-browser --skip-model-prompt
```

GitHub Actions 会在 macOS、Windows 和 Ubuntu 环境中检查 Python 代码并运行可执行的测试。全部平台测试通过后，Windows runner 才会构建便携 ZIP；它会把成品解压到带空格和中文的路径，以 `--skip-model-prompt` 启动 EXE、检查网页与便携数据目录，再通过同一个 EXE 安全停止。自动化测试能够覆盖平台分支、命令生成和模拟设备探测，但云端 runner 没有 RTX 5090，不能替代前文的真实 5090 推理验收。

## 项目结构

```text
video_cut/
├── start.command             # macOS 双击启动入口
├── stop.command              # 安全停止本地服务
├── start.bat                 # Windows 双击启动入口
├── stop.bat                  # Windows 安全停止入口
├── windows_exe.py            # Windows 便携 EXE 的最小入口
├── launcher.py               # macOS Python 环境、依赖、FFmpeg 与进程管理
├── launcher_windows.py       # Windows 环境、依赖、FFmpeg 与进程管理
├── launcher_models.py        # 启动时逐个询问并显示 AI 模型下载进度
├── process_guard.py          # 首次安装阶段的子进程与锁守护
├── run.py                    # 仅供启动器调用的 Uvicorn 服务入口
├── stop.py                   # 带实例身份校验的停止逻辑
├── app/
│   ├── main.py               # FastAPI 接口与本地工作流
│   ├── media.py              # 时间解析、FFprobe、预览、精确导出、逐帧截图与永久旋转
│   ├── ai_models.py          # 固定 revision、校验值、状态、后端与目标档位的模型目录
│   ├── ai_enhance.py         # 多模型环境、预下载、AI 任务、音轨回封装与成片验证
│   ├── dialogs.py            # 系统原生文件与文件夹选择窗口
│   ├── storage.py            # 本地设置持久化
│   ├── build_info.py         # 应用名称、版本与默认端口
│   ├── paths.py              # 源码与便携运行时路径分离
│   └── static/               # HTML、CSS、JavaScript 与图标
├── tests/                    # 自动化测试
├── .github/workflows/        # GitHub Actions
├── requirements.txt          # 运行依赖
├── requirements-ai.txt       # Apple Silicon MPS 的固定 AI 依赖入口
├── requirements-ai-cuda.txt  # SeedVR2 的 RTX 5090 CUDA 13.0 固定依赖
├── requirements-ai-swiftvr-cuda.txt # SwiftVR 的隔离 CUDA 13.0 固定依赖
├── requirements-ai-common.txt# 两种 AI 后端共用的固定依赖
├── requirements-dev.txt      # 测试依赖
├── requirements-build.txt    # Windows 便携包固定构建依赖
├── scripts/                  # Windows 便携包构建与成品冒烟测试
├── vendor/                   # 固定上游适配器、质量补丁与第三方许可说明
├── pyproject.toml            # 项目与测试配置
└── LICENSE                   # MIT License
```

## License 与第三方组件

[MIT](LICENSE)

SeedVR2、SwiftVR 与 FlashVSR 的上游代码和模型使用各自项目声明的 Apache License 2.0。本仓库不把大模型权重打进源码包或 Windows 便携 ZIP；SeedVR2 和 SwiftVR 由用户选择后从固定 revision 下载并校验，blocked 的 FlashVSR 不会下载或运行。适配器、许可副本、固定来源和修改说明见 [`vendor/`](vendor/)。
