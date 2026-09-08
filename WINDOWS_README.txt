Windows 11 / RTX 5090 便携版使用方法

1. 必须先完整解压 ZIP。请把整个文件夹放到桌面、下载目录或其他可写目录。
2. 不要只复制 LocalVideoCutter.exe；app、vendor、launcher_models.py 和 requirements 文件必须与 EXE 一起保留。
3. 双击 LocalVideoCutter.exe。首次运行需联网，程序会自动准备 Python 3.12、FFmpeg Full 和独立环境。
4. 本地网页服务就绪后，启动窗口会逐个询问是否准备兼容但尚未完整就绪的 AI 模型：
   - 输入 y 并按 Enter：立即下载该模型，并显示百分比、速度和预计剩余时间。
   - 输入 n 或直接按 Enter：跳过该模型并继续启动；以后仍可下载，基础功能不受影响。
   - 本 Windows RTX 5090 版会询问 SeedVR2 和 SwiftVR。FlashVSR 不会进入询问。
5. 浏览器打开后即可使用剪辑、逐帧截图、永久旋转和 AI 超清。“AI 模型管理”标签页可以逐个下载、继续下载、取消，并显示每个模型的进度。
6. 模型不会打进 ZIP。当前模型状态如下：
   - SeedVR2 3B FP16：stable、默认；支持 RTX 5090 CUDA 的 1080p、2K 和 4K；权重约 7.3 GB。
   - SwiftVR 5B BF16：experimental；仅支持 Windows 11 + RTX 5090 CUDA，并且本版本只开放 1080p；权重约 20.2 GB。
   - FlashVSR v1.1 Full：blocked；只显示原因，不能下载或运行。官方 Block-Sparse-Attention 尚未完成 Windows RTX 50 / sm_120 验证，程序不会用 dense attention 或 Tiny 模型降低质量来替代。
7. 只安装 SeedVR2 建议至少留出约 18 GB；同时安装 SeedVR2 与 SwiftVR 建议至少留出约 60 GB，成片空间另算。SwiftVR 使用流式管线，不会为整段视频生成全长无损中间文件。
8. 停止程序时，可以关闭启动窗口，或双击 stop.bat。

如果需要无人值守启动，可在 PowerShell 执行：

LocalVideoCutter.exe --skip-model-prompt

这个参数只跳过命令行 y/n 询问，不会关闭网页中的模型管理功能。

RTX 5090 需要已经正确安装 R580 或更新分支的 NVIDIA 驱动；程序不会自动安装或升级显卡驱动。模型环境使用固定的 cu130 PyTorch wheel，不需要另外安装 CUDA Toolkit、cuDNN 或 Visual Studio CUDA workload。

本版本的 RTX 5090 与 SwiftVR 适配是在 Apple M2 开发机上完成，只通过静态、mock 和无 CUDA 自动化测试，不能视为已经在真实 5090 上验收。请先用短片测试模型安装、推理、显存峰值、取消、颜色、音频与成片，再处理重要长视频。

这是未购买商业代码签名证书的未签名构建。Windows SmartScreen 可能首次显示“未知发布者”。本项目 GitHub 仓库是 private 仓库，下载 Releases 前必须登录已获访问权限的 GitHub 账号；如果 RTX 5090 电脑不方便登录，请先在有权限的电脑上下载并核对，再复制完整 ZIP。请同时下载同页对应的 .zip.sha256 文件，在 PowerShell 运行：

Get-FileHash .\LocalVideoCutter-Windows-RTX5090-v1.8.0.zip -Algorithm SHA256
Get-Content .\LocalVideoCutter-Windows-RTX5090-v1.8.0.zip.sha256

第一条命令输出中的 Hash 必须与 .zip.sha256 文件第一列的 64 位字符完全相同（忽略大小写）。只要不同，就不要解压或运行；完全相同且确认来源后，才可在 SmartScreen 中点击“更多信息”再选择“仍要运行”。

程序和模型数据默认保存在本文件夹的 .venv 和 data 目录。不要放到 Program Files 等普通用户不可写的目录，也不要在运行或处理视频时移动该文件夹。
