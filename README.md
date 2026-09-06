# 本地视频剪辑

一个面向 macOS 的本地视频剪辑工具。你只需要选择视频、填写起始时间和结束时间、看一遍预览，再点击“确定并导出”。视频不会上传到网络，原文件也不会被修改。

## 零基础使用方法

### 1. 下载并解压

在 GitHub 项目页面点击 **Code → Download ZIP**，下载后双击 ZIP 解压。

不要直接在 ZIP 压缩包预览窗口里运行；请先把整个项目文件夹解压到“下载”或“桌面”等普通目录。

### 2. 双击启动

打开解压后的文件夹，双击 `start.command`。

首次启动会做这些事情：

- 查找 Python 3.10 或更高版本；
- 在项目内创建独立的 `.venv` 环境；
- 安装本工具所需的 Python 依赖；
- 检查 FFmpeg 和 FFprobe；
- 启动仅供本机访问的网页，并自动在浏览器中打开。

第一次安装需要联网，可能持续几分钟。Terminal 窗口在工具运行期间需要保持打开。

### 3. 选择视频和保存目录

1. 在网页中点击“选择视频”，从 Finder 选择本地视频。
2. 点击“选择文件夹”，从 Finder 选择剪辑后视频的保存目录。这个目录会被记住，下次启动仍可使用，也可以随时更换。

### 4. 只填写两个时间

填写“起始时间”和“结束时间”。支持以下格式：

| 输入示例 | 含义 |
|---|---|
| `75.5` | 75.5 秒 |
| `01:15.500` | 1 分 15.5 秒 |
| `00:01:15.500` | 0 小时 1 分 15.5 秒 |

起始时间不能小于 0，结束时间必须晚于起始时间，并且不能超过视频总时长。使用冒号时，秒数必须小于 60；使用 `HH:MM:SS` 时，分钟数也必须小于 60。

### 5. 预览并导出

两个时间都有效后，页面会生成对应片段的预览。修改任意一个时间，预览会按新范围重新生成。

确认无误后点击“确定并导出”。完成后可以直接点击“在 Finder 中显示”。导出会创建新文件，原视频始终保留不变。

默认文件名为：

```text
原文件名_clip_起始时间-结束时间.mp4
```

例如：

```text
旅行_clip_00-00-10_000-00-00-25_500.mp4
```

如果同名文件已经存在，程序会自动添加 `_2`、`_3` 等编号，不会覆盖已有文件。常见素材默认导出为 `.mp4`，ProRes 或需要保留多声道/高位深 PCM 的素材会使用 `.mov`；透明通道、16-bit、奇数尺寸等无法安全映射到常见编码的专业素材会使用无损 FFV1 的 `.mkv`。

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

高质量重编码可能比原视频片段更大，导出速度也取决于视频时长、分辨率和 Mac 性能。
HDR 片段会优先直接播放原片；如果浏览器无法解码，才生成一份 8-bit 兼容定位预览，此时颜色只用于定位，最终成片仍走原 HDR 色彩信息的导出路径。Dolby Vision、动态 HDR 元数据、多音轨、字幕或沉浸式音频等专业素材不保证完整保留；这类文件请先备份，并抽查一小段成片。本工具默认保留主视频流和主音轨。

## 安装要求与自动处理边界

需要 macOS、Python 3.10+ 和 FFmpeg。

`start.command` 会优先查找 Apple Silicon Homebrew 的 `/opt/homebrew/bin/python3`、Intel Homebrew 的 `/usr/local/bin/python3`，再检查当前 `PATH` 中的 `python3`。

- 已安装 Homebrew 但缺少合适的 Python 时，启动脚本会尝试执行 `brew install python`。
- 已安装 Homebrew 但缺少 FFmpeg 时，启动器会尝试执行 `brew install ffmpeg`。
- 工具不会自动安装 Homebrew。若 Mac 上没有 Homebrew，请先按照 [Homebrew 官网](https://brew.sh/)安装，再重新双击 `start.command`。
- 自动安装需要网络连接，并可能要求你在 Terminal 中确认系统提示或输入当前 Mac 账号密码。
- macOS 自带的旧版 `/usr/bin/python3` 不会被替换或修改。

也可以先手动安装：

```bash
brew install python ffmpeg
```

然后检查：

```bash
python3 --version
ffmpeg -version
ffprobe -version
```

## 停止工具

正常情况下，在启动工具的 Terminal 窗口按 `Control + C`。如果已经找不到那个窗口，可以双击 `stop.command`。

导出过程中关闭工具会取消当前导出并清理未完成的临时文件；已经成功完成的文件不会被删除。

## 隐私与本地处理

- Web 服务只监听 `127.0.0.1`，不向局域网或公网开放。
- 所选视频直接从原位置读取，不会上传到服务器，也不会复制到项目目录。
- 预览和最终剪辑都由本机 FFmpeg 完成。
- 保存目录设置记录在项目内的 `data/settings.json`；视频内容不会写入该配置。
- 首次安装依赖时会访问 Homebrew 或 PyPI，但视频处理本身不需要上传素材。

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

### 提示找不到 Python、FFmpeg 或 FFprobe

先安装 Homebrew，然后执行：

```bash
brew install python ffmpeg
```

完全关闭旧 Terminal 后重新双击 `start.command`。Apple Silicon Mac 的 Homebrew 通常位于 `/opt/homebrew`。

### Finder 无法选择视频或保存目录

检查“系统设置 → 隐私与安全性 → 文件与文件夹”，允许 Terminal 访问视频所在目录。外接磁盘还需要确认磁盘已挂载且保存目录可写。

### 浏览器没有自动打开

查看启动 Terminal 中的 `Local Video Cutter is ready at ...`，把其后的本地地址复制到浏览器。默认从端口 `8777` 开始；如果端口已被占用，程序会自动选择另一个空闲端口。

### 修改时间后预览没有更新

先确认两个时间均为有效格式且没有超过视频总时长，再点击页面中的“重试预览”。如果原文件已被移动、改名或删除，请重新选择视频。

### 导出很慢或文件较大

精确剪辑需要高质量重编码，并非简单复制数据。4K、HEVC、10-bit 和长片段会明显更慢；请保持 Terminal 和网页开启，并确保保存目录有足够空间。

### 更新后仍打开旧页面

先按 `Control + C` 停止旧实例，或双击 `stop.command`，再重新运行 `start.command`。必要时在浏览器中按 `Command + Shift + R` 强制刷新。

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
python launcher.py --no-browser
```

GitHub Actions 会在 macOS 和 Ubuntu 上，分别使用 Python 3.10 与 3.13 安装 FFmpeg、检查 Python 代码并运行 `pytest`。原生 Finder 文件与文件夹选择器只在 macOS 上提供；Ubuntu CI 用于验证不依赖 Finder 的核心逻辑。

## 项目结构

```text
video_cut/
├── start.command             # macOS 双击启动入口
├── stop.command              # 安全停止本地服务
├── launcher.py               # Python 环境、依赖、FFmpeg 与进程管理
├── process_guard.py          # 首次安装阶段的子进程与锁守护
├── run.py                    # 仅供启动器调用的 Uvicorn 服务入口
├── stop.py                   # 带实例身份校验的停止逻辑
├── app/
│   ├── main.py               # FastAPI 接口与本地工作流
│   ├── media.py              # 时间解析、FFprobe、预览和精确导出
│   ├── dialogs.py            # macOS Finder 原生选择窗口
│   ├── storage.py            # 本地设置持久化
│   ├── build_info.py         # 应用名称、版本与默认端口
│   └── static/               # HTML、CSS、JavaScript 与图标
├── tests/                    # 自动化测试
├── .github/workflows/        # GitHub Actions
├── requirements.txt          # 运行依赖
├── requirements-dev.txt      # 测试依赖
├── pyproject.toml            # 项目与测试配置
└── LICENSE                   # MIT License
```

## License

[MIT](LICENSE)
