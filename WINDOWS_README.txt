Windows 11 / RTX 5090 便携版使用方法

1. 必须先完整解压 ZIP。请把整个文件夹放到桌面、下载目录或其他可写目录。
2. 不要只复制 LocalVideoCutter.exe；app、vendor 和 requirements 文件必须与 EXE 一起保留。
3. 双击 LocalVideoCutter.exe。首次运行需联网，程序会自动准备 Python 3.12、FFmpeg Full 和独立环境。
4. 浏览器打开后即可使用剪辑、逐帧截图、永久旋转和 AI 超清。
5. 使用 AI 前，打开“AI 模型管理”标签页，提前下载约 7.3 GB 的模型并查看进度。
6. 停止程序时，可以关闭启动窗口，或双击 stop.bat。

RTX 5090 需要已经正确安装 NVIDIA 驱动；程序不会自动安装或升级显卡驱动。

这是未购买商业代码签名证书的开源构建。Windows SmartScreen 可能首次显示“未知发布者”；请只从本项目 GitHub Releases 下载，并核对同页提供的 SHA-256。确认来源后可点击“更多信息”再选择“仍要运行”。

程序和模型数据默认保存在本文件夹的 .venv 和 data 目录。不要放到 Program Files 等普通用户不可写的目录，也不要在运行或处理视频时移动该文件夹。
