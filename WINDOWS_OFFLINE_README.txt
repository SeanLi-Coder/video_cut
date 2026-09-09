Local Video Cutter v1.11.0 — Windows 11 / RTX 5090 完全离线 U 盘使用说明

这份说明用于没有网络的 Windows 11 + NVIDIA GeForce RTX 5090 电脑。普通 Windows 便携 ZIP 只包含程序和本说明，不包含体积很大的 Python、FFmpeg、Microsoft Visual C++ Runtime、AI runtime、wheel 或模型文件；完整离线资源由 offline 和 data\ai 两部分组成，必须另外取得，并且必须与程序版本匹配。

一、在联网电脑上准备

1. 完整解压 Windows 便携 ZIP，不要只取出 LocalVideoCutter.exe。
2. 取得与这个程序版本配套的完整 offline 文件夹和 data 文件夹。两者缺一不可。
3. 把 offline 和 data 两个文件夹都放到 LocalVideoCutter.exe 同级目录。固定路径必须是：

   <程序文件夹>\offline\windows-rtx5090\
   <程序文件夹>\data\ai\

   最低限度应能看到：

   <程序文件夹>\offline\windows-rtx5090\manifest.json
   <程序文件夹>\offline\windows-rtx5090\READY
   <程序文件夹>\offline\windows-rtx5090\runtime\vc_redist.x64.exe
   <程序文件夹>\data\ai\models\seedvr2_ema_3b_fp16.safetensors
   <程序文件夹>\data\ai\engines\swiftvr-5b-bf16\models\transformer\diffusion_pytorch_model.safetensors

   其余文件和子目录必须保持提供时的原名与层级。不要把 windows-rtx5090 或 data\ai 里的文件摊平到程序根目录，不要重命名，也不要混用其他版本的离线资源。
4. 最终应复制整个程序文件夹，而不是分别挑选 EXE、模型或 wheel。程序文件夹内的 app、vendor、requirements 文件、WINDOWS_README.txt、WINDOWS_OFFLINE_README.txt、offline 和 data 都必须一起保留。

二、准备 U 盘

U 盘必须使用 exFAT 或 NTFS。不能使用 FAT32，因为离线包包含超过 4 GB 的单个文件；macOS 的 APFS/HFS+ 也不能作为普通 Windows 传输盘直接使用。从 Mac 准备 U 盘时推荐 exFAT，从 Windows 准备时也可以使用 NTFS。

格式化会清空 U 盘。只有确实需要更换文件系统时才格式化，并先备份盘内已有文件。

把组装好的整个程序文件夹复制到 U 盘，等待复制完全结束后安全推出。不要在复制尚未结束时拔出 U 盘。

三、目标 Windows 电脑的必要条件

- Windows 11 x64。
- NVIDIA GeForce RTX 5090。
- 已安装 R580 或更新分支的 NVIDIA 驱动，并已重启电脑。
- 本地磁盘有足够空间：只准备 SeedVR2 建议至少 18 GB；同时准备 SeedVR2 和 SwiftVR 建议至少 60 GB，视频成片空间另算。

离线包不会安装或升级 NVIDIA 驱动，也不能用 CUDA Toolkit 代替显卡驱动。如果目标电脑不能联网，请提前从 NVIDIA 官方渠道另行准备驱动安装程序。启动程序前可在 PowerShell 检查：

nvidia-smi

输出必须明确列出 NVIDIA GeForce RTX 5090，且驱动满足 R580 或更新分支；否则先不要安装模型或运行 AI 超清。

四、复制并启动

1. 在目标 Windows 电脑上，把 U 盘里的整个程序文件夹复制到本地 SSD 的短路径全新空目录，推荐直接使用 C:\LVC 或 D:\LVC。不要放在多层目录或保留很长的压缩包名称，不要覆盖以前运行过的旧程序文件夹，不要只复制 LocalVideoCutter.exe，也不要放进 Program Files。
2. 确认固定离线路径仍是：

   <程序文件夹>\offline\windows-rtx5090\
   <程序文件夹>\data\ai\

3. 双击 LocalVideoCutter.exe。程序会先核对 READY、manifest.json、程序版本和目标平台，再在使用每项资源前核对其大小和 SHA-256；校验不通过时不会使用该文件。Microsoft Visual C++ Runtime、Python、FFmpeg 以及应用依赖都会优先从离线包准备。若 Windows 显示用户账户控制确认，请允许 Microsoft Visual C++ Runtime 或 Python 安装程序运行。
4. 首次准备环境和模型会花较长时间，即使没有网络也请保持启动窗口开启。模型安装完成后，在网页中先用几秒钟短片验收，再处理重要长视频。AI 超清模式支持一次多选最多 100 个视频并按顺序串行处理；某个视频失败时会保留错误提示并自动继续下一个，成功成片分别写入各自原视频同级目录。

建议不要直接从 U 盘长期运行：模型安装、缓存和输出会产生大量读写。复制到本地 SSD 后运行更稳定，也更快。
程序启动和运行期间不要移动、覆盖或同步 offline、data\ai 以及整个程序文件夹；需要重新复制时，请先退出程序并使用另一个全新空目录。
为防止 U 盘复制损坏被旧缓存掩盖，每次重新启动程序都会先完整校验本地 AI 模型；这段时间只读取本地 SSD，不是在联网下载。

五、常见问题

提示找不到离线包：检查 offline 和 data 是否都与 LocalVideoCutter.exe 同级，并确认没有多套一层目录。正确的是 <程序文件夹>\offline\windows-rtx5090\manifest.json 和 <程序文件夹>\data\ai\models\...。

提示 READY、manifest 或资源校验失败：离线包不完整、复制损坏或版本不匹配。不要手工修改 manifest.json 或 READY；从可信来源重新复制与当前程序版本配套的完整 offline 和 data 文件夹。

为什么不能删除单个离线模型：完整离线包中的模型就是可恢复的离线源。程序会自动把它们识别为已下载，并在网页中禁用单独删除，避免误删后无法恢复。如需释放全部空间，请退出程序后删除整个离线程序文件夹。

仍然尝试联网：先确认完整 offline 与 data\ai 文件夹同时存在且通过校验。普通便携 ZIP 本身不是完全离线包，不能只复制普通 ZIP 后期待所有 AI 依赖离线安装。

无法识别 RTX 5090：在 PowerShell 运行 nvidia-smi。离线资源只包含应用依赖，不包含 NVIDIA 显卡驱动。

模型或安装很慢：这是本地解压、校验与安装过程，不代表程序正在联网。请从本地 SSD 运行，并保持足够可用空间。

提示 Windows 路径过长：先退出程序，把整个程序文件夹真正移动并改名为 C:\LVC 或 D:\LVC 后重试；不要只创建快捷方式。模型文件无需重新下载或复制。
