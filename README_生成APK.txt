这是 Android APK 自动构建工程。

最省事：
1. 在 GitHub 新建仓库，例如 video-collector-android
2. 把本压缩包解压后的所有文件上传到仓库根目录
3. 上传完成后点 GitHub 顶部 Actions
4. 左侧选 Build Android APK
5. 点 Run workflow
6. 等绿色成功
7. 点进入这次运行，在页面底部 Artifacts 下载 VideoCollector-APK
8. 解压后 app-debug.apk 就是可直接安装的 Android APK

APP 打开后直接进入：
https://video-collector-0d1n.onrender.com

下载按钮会调用 Android DownloadManager 保存到“下载”目录。
