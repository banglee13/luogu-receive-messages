# 洛谷私信通知助手（桌面版）

在电脑上后台监听洛谷私信，收到新消息时弹出 Windows 系统通知。基于原油猴脚本改写，去掉了浏览器插件的多标签页选主逻辑，改为独立桌面程序。

## 一、直接运行（开发/调试用）

1. 安装 Python 3.9+（Windows 官网下载，安装时勾选 "Add python.exe to PATH"）
2. 打开命令行，进入本文件夹，安装依赖：
   ```
   pip install -r requirements.txt
   ```
3. 运行：
   ```
   python main.py
   ```

## 二、打包成独立 exe（推荐给不装 Python 的电脑用）

双击运行 `build_exe.bat`，会自动安装依赖并调用 PyInstaller 打包。
打包完成后，`dist\LuoguPMNotifier.exe` 就是可以独立运行的程序，把它复制到任意电脑上（Windows）即可双击使用，不需要再装 Python。

> 首次打包耗时较长（1-2 分钟），耐心等待。

## 三、如何获取 `__client_id` 和 `_uid`

1. 浏览器登录 https://www.luogu.com.cn
2. 按 `F12` 打开开发者工具
3. 切换到 **Application（应用）** 或 **存储** 选项卡
4. 左侧找到 **Cookies → https://www.luogu.com.cn**
5. 在列表中找到两行，复制它们的 **Value（值）**：
   - `__client_id`
   - `_uid`
6. 程序首次启动时会弹出输入框，把这两个值分别粘贴进去，点击"保存并连接"

⚠️ **这两个值等同于你的登录凭证**，任何人拿到都能以你的身份访问账号。
- 不要把它们发给别人
- 不要把生成的 `config.json` 文件分享或上传到网上（比如 GitHub）
- 如果怀疑泄露，去洛谷网站重新登录一次（部分登录方式会使旧 client_id 失效）可以降低风险

## 四、文件说明

- `main.py`：程序主体
- `config.json`：程序自动生成，保存你的 `__client_id` / `_uid`（以及 WebSocket 服务器地址缓存），**不要泄露此文件**
- `requirements.txt`：Python 依赖列表
- `build_exe.bat`：一键打包成 exe 的脚本

## 五、暂停/恢复接收消息

主界面底部有一个"⏸ 暂停接收消息"按钮，点击后会切换为"▶ 已暂停，点击恢复接收"。
- 暂停期间：WebSocket 连接仍然保持（不会频繁重连），但收到的消息会被直接忽略——不弹通知、不记日志、不计数
- 这个状态**不会**保存到 `config.json`，每次重新打开程序都会恢复为默认的"接收"状态

## 六、隐私模式

主界面用户信息卡片下方有一个"隐私模式"开关，默认关闭。
- **关闭**：通知和窗口日志正常显示发送人姓名、头像、私信内容
- **开启**：所有通知和日志都只显示"你有一条新的消息"，不显示发送人是谁、内容是什么，也不显示头像
- 开关状态会保存到 `config.json`，下次打开程序自动保持上次的设置

适合在有其他人可能看到你屏幕的场合使用。

## 七、常见问题

**Q: 报错 SSL: CERTIFICATE_VERIFY_FAILED / unable to get local issuer certificate？**
A: 这是部分 Windows 电脑（尤其是精简版 Python 环境或某些国产杀毒软件拦截证书链）常见问题，程序本身找不到系统根证书。现在已经修复：程序会用 `certifi` 库自带的证书包来验证 HTTPS/WSS 连接，不再依赖系统证书。请确保执行过 `pip install -r requirements.txt`（里面包含了 `certifi`），如果是用打包好的 exe，需要用**最新版**重新打包一次。

**Q: 登录信息校验失败？**
A: 大概率是 Cookie 已过期（重新登录网页后 `__client_id` 一般不变，但保险起见建议重新复制一遍两个值），或者复制时带了多余空格。点击主界面右上角"重新设置账号"重新输入即可。

**Q: 没有弹出系统通知？**
A: 确认 `pip install win11toast` 是否安装成功；同时检查 Windows 系统设置里"通知和操作"是否允许该程序发通知。也可以点击主界面的"测试通知"按钮排查。

**Q: 想开机自启动？**
A: 把打包好的 `LuoguPMNotifier.exe` 的快捷方式放进 `Win+R` 输入 `shell:startup` 打开的文件夹里即可。

## 八、原理简述

- 用你提供的 `__client_id` / `_uid` 作为 Cookie 向洛谷发起认证请求，校验登录态并获取当前用户信息
- 连接 `wss://ws.luogu.com.cn/ws`，加入 `chat` 频道（`channel_param` 为你的 uid）
- 服务器有新私信时会推送 `server_broadcast` 类型消息，程序解析后弹通知、记日志
- 断线后按 2s → 4s → 8s → ... 最多 60s 的间隔自动重连，避免频繁请求触发限流
