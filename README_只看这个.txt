Video Collector - Render 云端版

目标：
部署成功后，你会得到一个公网网址。
手机、Win7、任何浏览器都能直接打开使用。

你不需要：
- Python
- CMD
- Linux
- 自己买服务器

部署需要：
1. 一个 GitHub 账号
2. 一个 Render 账号

推荐流程：
A. 把本项目上传到 GitHub 仓库
B. Render -> New -> Blueprint
C. 选择这个 GitHub 仓库
D. Render 会读取 render.yaml 自动部署
E. 部署成功后得到 https://xxxx.onrender.com
F. 手机打开这个网址即可

重要：
- 当前是“云端网页测试版”，不是 APK。
- 先验证云端解析能不能满足你的真实链接，再封装 APK。
- 只处理公开可访问、且你有权保存的内容。
- 不绕过登录、验证码、DRM 或访问控制。
- 抖音、小红书、视频号等平台规则经常变化，不能保证所有链接都能解析。
