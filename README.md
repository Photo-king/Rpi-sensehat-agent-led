# agent-led — 用树莓派 Sense HAT 当 agent 状态灯

在 Mac 上用 Codex 跑任务时，局域网里的树莓派 Sense HAT 会实时显示状态：

| 灯效 | 状态 | 什么时候出现 |
| --- | --- | --- |
| 🟡 黄灯低频闪烁 | 处理中 | 你提交 prompt、agent 调用工具、子 agent 运行、上下文压缩 |
| 🟢 绿灯常亮 | 运行完毕 | agent 一轮回复结束（`Stop`） |
| 🔴 红灯快闪（黄灯 2 倍速） | 需要确认 | Codex 正要弹出授权/确认提示（`PermissionRequest`） |
| ⚫️ 灭灯 | 空闲 | 被打断、`/clear`、手动 `sense idle` |

红灯会无视开关强制亮起——需要你确认的那一刻不该被漏掉。

**多窗口**：树莓派按会话（`session_id`）分别记账，再按
`需要确认 > 处理中 > 运行完毕` 的优先级汇总。所以开几个窗口都行：
只要有一路在等授权就是红灯，只要有一路还在跑就至少黄灯，全部结束才转绿。
绿灯会一直亮着，直到下一个任务开始、你按摇杆关灯、或执行 `sense idle`。

**防竞态**：`PreToolUse` / `PermissionRequest` / `PostToolUse` 是并行发出的，
到达顺序不保证。所以服务端额外规定两条：

- 红灯只能被 `PostToolUse`（工具真的跑起来了 = 授权已处理）或 `Stop` 解除，
  `PreToolUse` 之类的事件不会把红灯压回黄灯——否则你在等授权时看到的是黄灯。
- 一轮结束后的 2 秒内，忽略掉队的 `PostToolUse`，绿灯不会被重新染黄。

注意 `sense idle` 是不带 session 的复位，会**清空所有会话**的账（包括别处
正在等授权的那盏红灯）；只想清某一路就带 session 调 `/state/idle?session=<id>`。

## 摇杆

| 操作 | 作用 |
| --- | --- |
| ↑ / ↓ | 亮度 ±1（0-8 档，按一下底行亮几颗就是几档） |
| 按下（中键） | 指示灯开 / 关；关掉后状态照常记录，下次开灯直接显示当前状态 |
| 询问模式下 ↑ 或 按下 | 同意（配合 `sense ask`） |
| 询问模式下 ↓ | 拒绝 |

亮度和开关持久化在树莓派的 `~/.config/sensehat-led/state.json`。

## 组成

```
Mac                                       树莓派 192.168.31.28
├── ~/.codex/hooks.json  ──┐              ├── systemd: sensehat-led.service
├── ~/.codex/hooks/        │  HTTP :8765  ├── /home/<pi-user>/sensehat-led/sensehat_led.py
│   └── sense_led_hook.sh ─┼─────────────▶│   ├── 直写 /dev/fbX (8x8 RGB565)
└── /opt/homebrew/bin/sense┘              │   └── 读 /dev/input/eventX 摇杆
   (→ bin/sense)                          └── 摇杆/亮度/开关都在这里处理
```

树莓派侧不依赖 `sense_hat` Python 库，直接写 framebuffer、直接读 evdev，
所以升级内核/Raspberry Pi OS 后不容易坏；framebuffer 也是按名字
（`RPi-Sense FB`）找的，插了 HDMI 导致 `fb0/fb1` 编号变化也不影响。

## 目录内容

| 路径 | 说明 |
| --- | --- |
| `bin/sense` | Mac 端命令行客户端 |
| `bin/sense-hook` | Codex hook 脚本（读 stdin 事件 JSON，转成状态） |
| `codex/hooks.json` | 安装到 `~/.codex/hooks.json` 的 hook 配置 |
| `pi/sensehat_led.py` | 树莓派上的服务（点阵屏 + 摇杆 + HTTP 接口） |
| `pi/install.sh` | 在树莓派上安装/更新 systemd 服务 |
| `pi/README.md` | 树莓派侧说明（接口、服务管理） |

## 日常用法

```bash
sense busy | done | confirm | idle     # 手动设置状态
sense status                           # 看当前状态 JSON
sense brightness up|down|1-8           # 调亮度（等价于摇杆 ↑↓）
sense toggle                           # 开关指示灯（等价于按下摇杆）
sense ask 600 "要执行 rm -rf 吗？"      # 红灯快闪，等你按摇杆回答，退出码 0=同意 1=拒绝 2=超时
sense run -- pnpm test                 # 黄灯闪 → 绿灯(成功) / 红灯(失败)
```

`sense` 已软链到 `/opt/homebrew/bin/sense`，直接在 PATH 里。
换网络/换机器时用 `SENSE_HOST=192.168.31.28 SENSE_PORT=8765 sense status` 覆盖。

## 重新部署

改完 `pi/` 下的文件后推送到树莓派：

```bash
scp pi/sensehat_led.py pi/install.sh pi/README.md pi@192.168.31.28:/tmp/sense-deploy/
ssh pi@192.168.31.28 'bash /tmp/sense-deploy/install.sh'
```

改完 `codex/hooks.json` 或 `bin/sense-hook` 后要同步到 `~/.codex/`，
**并且因为信任是按内容哈希记录的，改完需要重新在 Codex 里 `/hooks` 确认一次**：

```bash
cp bin/sense-hook ~/.codex/hooks/sense_led_hook.sh
cp codex/hooks.json ~/.codex/hooks.json
```

> 换一台 Mac 用的话，先把 `codex/hooks.json` 和 `bin/sense-hook` 里的
> `/Users/photo-king/...` 绝对路径改成你自己的家目录。

## 排错

```bash
sense ping                                  # 树莓派服务通不通
ssh pi@192.168.31.28 'journalctl -u sensehat-led -n 30'   # 服务日志
tail -f /tmp/sense-hook.log                 # hook 有没有被触发、映射成什么状态
```

| 现象 | 原因 |
| --- | --- |
| hook 一直没反应 | hooks 还没被信任：在 Codex 里执行 `/hooks` 审阅并信任；或 `~/.codex/hooks.json` 没被加载 |
| 灯不亮但 `sense status` 正常 | 指示灯被摇杆关掉了：`sense indicators on`，或按一下摇杆 |
| `sense: 连不上` | 树莓派没开机 / 服务没起 / Mac 换了网段 |

## 安全提示

- 建议用 SSH 密钥登录而不是密码：`ssh-keygen -t ed25519` + `ssh-copy-id pi@192.168.31.28`。
- 8765 端口绑定在 `0.0.0.0`，局域网内任何设备都能改灯。介意的话给服务加 `--token`（见 `pi/README.md`）。
