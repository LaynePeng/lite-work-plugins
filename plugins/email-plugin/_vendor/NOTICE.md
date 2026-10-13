# vendored 文件说明

本目录存放**原样复制**的第三方/标准库文件，仅作为冻结宿主的回退（fallback）。
插件优先使用宿主自带的同名模块；只有宿主未捆绑时才加载这里。

## imaplib.py

| 项 | 值 |
| --- | --- |
| 来源 | CPython 标准库 `Lib/imaplib.py`（Python 3.14） |
| 模块版本 | `__version__ = "2.60"` |
| 版权 | Python Software Foundation |
| 许可 | PSF License Agreement（见 https://docs.python.org/3/license.html） |
| 文件校验 | md5 `2026c2ba79f97d8506cd2c892a9d694d`（72151 字节，2085 行） |
| 修改 | 无。原样复制，未做任何改动 |

**为何需要**：lite-work 打包版是 PyInstaller 冻结进程，只静态分析主程序的
import，运行时才加载的插件 import 看不到——`imaplib` 因此可能缺席
（`smtplib` 恰好被传递依赖捎带进包，故未暴露）。升级宿主可用
`--hidden-import imaplib` 根治后，本文件即可删除。

**许可提示**：本仓库整体为 MIT，而此文件为 PSF 许可。二者并存不冲突
（PSF 允许再分发），但**不得改写或删除本源文件与版权信息**。
