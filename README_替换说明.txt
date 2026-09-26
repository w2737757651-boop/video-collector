Video Collector V2 - 抖音/小红书公开页面适配 + 下载

本版本新增：
1. 抖音公开页面 Adapter：
   - 先解析分享短链
   - 读取公开页面内嵌状态数据
   - 优先查找 _ROUTER_DATA / RENDER_DATA
   - 找到视频资源后返回
   - 失败时回退 yt-dlp

2. 小红书公开页面 Adapter：
   - 支持 xhslink.cn / xhslink.com
   - 若短链跳到 login?redirectPath=...，只恢复其中公开笔记 URL
   - 读取公开页面 window.__INITIAL_STATE__
   - 找到可访问视频资源后返回
   - 失败时回退 yt-dlp

3. 下载：
   - 解析成功后每个视频格式增加“下载”按钮
   - 下载由本服务器重新解析来源链接，再代理公开媒体流
   - 不允许客户端直接指定任意媒体 URL

边界：
- 不注入账号 Cookie
- 不绕过登录
- 不解验证码
- 不绕过 DRM / 访问控制
- 平台一旦要求额外验证，会明确报错

GitHub 替换：
- app/main.py
- static/index.html
- requirements.txt
- Dockerfile

提交后 Render 会自动重新部署。
