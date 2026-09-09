Local Video Cutter v1.10.6 — Windows 11 / RTX 5090 便携版使用方法

AI 超清、模型管理、模型下载与代理设置仅在 Windows RTX/CUDA 版本提供；macOS 版本只保留剪辑、逐帧截图和永久旋转。

1. 必须先完整解压 ZIP。请把整个文件夹放到本地 SSD 的短路径可写目录，推荐 C:\LVC 或 D:\LVC；不要保留很长的压缩包目录名。
2. 不要只复制 LocalVideoCutter.exe；app、vendor、launcher_models.py 和 requirements 文件必须与 EXE 一起保留。
3. 双击 LocalVideoCutter.exe。普通便携包首次运行需联网，程序会自动准备 Python 3.12、FFmpeg Full 和独立环境；使用完整离线资源时会从本地自动准备 Microsoft Visual C++ Runtime、Python、FFmpeg 和全部依赖。
4. 本地网页服务就绪后，启动窗口会逐个询问是否准备兼容但尚未完整就绪的 AI 模型：
   - 如果需要代理，会先看到 AI download proxy [Enter=keep, s=set/change, c=clear]。直接按 Enter 沿用当前设置；输入 s 可填写 socks5://127.0.0.1:7897、socks5h://127.0.0.1:7897 或 http://127.0.0.1:7897；输入 c 清除。密码输入不会回显，保存后会先测试连接并显示延迟。
   - 输入 y 并按 Enter：立即下载该模型，并显示百分比、速度和预计剩余时间。
   - 输入 n 或直接按 Enter：跳过该模型并继续启动；以后仍可下载，基础功能不受影响。
   - 普通下载支持断点续传。如果网页下载曾失败、取消，或留有部分文件，再次运行启动器会显示 Continue, restart from zero, or skip? [c/r/N]。
   - 输入 c：从现有断点继续下载；输入 r：只清理所选模型的权重后从零重下；输入 n 或直接按 Enter：跳过。
   - 从零重下不会删除已安装的 AI runtime，也不会影响其他模型。即使本地网页服务已经在运行，再次双击 LocalVideoCutter.exe 也会显示恢复提示；如果下载仍在进行，命令行会接管其进度显示。
   - 本 Windows RTX 5090 版会询问 SeedVR2 和 SwiftVR。
5. 浏览器打开后即可使用剪辑、逐帧截图、永久旋转和 AI 超清。剪辑和截图可选保存目录；永久旋转和 AI 超清不显示目录选择器，新视频固定保存在原视频同级目录，请确保该目录可写且空间足够。“AI 模型管理”标签页可以配置、测试或清除同一份 HTTP/HTTPS/SOCKS5/SOCKS5H 下载代理，也可以逐个下载、继续下载、取消或删除，并显示每个模型的进度。删除需要再次确认，只清理所选模型的权重、VAE、校验缓存和断点文件，运行环境和其他模型会保留。代理会用于 AI 运行器、模型权重和隔离环境依赖；修改只对下一次新开始或重试生效。若本地 DNS 受限，优先使用 socks5h://。首次创建应用自身环境以及 WinGet 下载早于网页启动，仍需使用 Windows 系统代理。
6. 模型和其他巨大离线资源不会打进普通便携 ZIP。当前模型状态如下：
   - SeedVR2 3B FP16：stable、默认；支持 RTX 5090 CUDA 的 1080p、2K 和 4K；权重约 7.3 GB。
   - SwiftVR 5B BF16：experimental；仅支持 Windows 11 + RTX 5090 CUDA，并且本版本只开放 1080p；权重约 20.2 GB。
7. 只安装 SeedVR2 建议至少留出约 18 GB；同时安装 SeedVR2 与 SwiftVR 建议至少留出约 60 GB。AI 成片固定写入原视频所在磁盘，该磁盘的成片空间另算。SwiftVR 使用流式管线，不会为整段视频生成全长无损中间文件。
8. 停止程序时，可以关闭启动窗口，或双击 stop.bat。

完全离线 U 盘方式：

普通便携 ZIP 只附带离线说明，不包含大型离线资源。取得与程序版本配套的完整 offline 和 data 文件夹后，必须把两者都原样放到 LocalVideoCutter.exe 同级目录，固定位置为：

<程序文件夹>\offline\windows-rtx5090\
<程序文件夹>\data\ai\

其中必须保留 manifest.json、READY、Microsoft Visual C++ Runtime、两个模型的全部权重以及清单内全部文件的原名和层级。不要把内容摊平，不要只复制 EXE，也不要混用其他版本的离线资源。组装完成后，把整个程序文件夹一起复制到 U 盘，再从 U 盘完整复制到目标 Windows 电脑本地 SSD 的全新空目录；不要覆盖已经运行过的旧目录，运行期间也不要移动或覆盖程序文件。离线模型会被自动识别并保护，网页不会允许单独删除它们。

U 盘必须是 exFAT 或 NTFS；FAT32 无法保存离线包中超过 4 GB 的单个文件。从 Mac 向 Windows 传输时推荐 exFAT。格式化会清空 U 盘，操作前务必备份。

目标电脑必须是 Windows 11 x64、安装 NVIDIA GeForce RTX 5090，并预先安装 R580 或更新分支的 NVIDIA 驱动。离线包不包含显卡驱动；运行前请用 nvidia-smi 确认显卡和驱动。完整傻瓜式步骤见同目录 WINDOWS_OFFLINE_README.txt。

如果需要无人值守启动，可在 PowerShell 执行：

LocalVideoCutter.exe --skip-model-prompt

这个参数会跳过命令行代理、y/n 和 c/r/n 询问，不会关闭网页中的模型管理功能。

RTX 5090 需要已经正确安装 R580 或更新分支的 NVIDIA 驱动；程序不会自动安装或升级显卡驱动。模型环境使用固定的 cu130 PyTorch wheel，不需要另外安装 CUDA Toolkit、cuDNN 或 Visual Studio CUDA workload。

本版本的 RTX 5090 与 SwiftVR 适配是在 Apple M2 开发机上完成，只通过静态、mock 和无 CUDA 自动化测试，不能视为已经在真实 5090 上验收。请先用短片测试模型安装、推理、显存峰值、取消、颜色、音频与成片，再处理重要长视频。

这是未购买商业代码签名证书的未签名构建。Windows SmartScreen 可能首次显示“未知发布者”。本项目 GitHub 仓库是 private 仓库，下载 Releases 前必须登录已获访问权限的 GitHub 账号；如果 RTX 5090 电脑不方便登录，请先在有权限的电脑上下载并核对，再复制完整 ZIP。请同时下载同页对应的 .zip.sha256 文件，在 PowerShell 运行：

Get-FileHash .\LocalVideoCutter-Windows-RTX5090-v1.10.6.zip -Algorithm SHA256
Get-Content .\LocalVideoCutter-Windows-RTX5090-v1.10.6.zip.sha256

第一条命令输出中的 Hash 必须与 .zip.sha256 文件第一列的 64 位字符完全相同（忽略大小写）。只要不同，就不要解压或运行；完全相同且确认来源后，才可在 SmartScreen 中点击“更多信息”再选择“仍要运行”。

程序和模型数据默认保存在本文件夹的 .venv 和 data 目录。不要放到 Program Files 等普通用户不可写的目录，也不要在运行或处理视频时移动该文件夹。
