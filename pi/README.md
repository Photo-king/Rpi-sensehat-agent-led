# Sense HAT 状态灯

把树莓派上的 Sense HAT 当成 agent 的状态指示灯，从局域网内任意机器控制。

## 状态

| 状态 | 灯效 | 含义 |
| --- | --- | --- |
| `busy` | 黄灯低频闪烁（0.65s 亮 / 0.65s 灭） | 处理中 |
| `done` | 绿灯常亮 | 运行完毕 |
| `confirm` | 红灯常亮（强制亮灯） | 需要确认 |
| `idle` | 全灭 | 空闲 |

## 摇杆

| 操作 | 作用 |
| --- | --- |
| 向上 | 亮度 +1（0-8 档，按一下底行亮几颗就是几档） |
| 向下 | 亮度 -1 |
| 按下（中键） | 指示灯开 / 关（关掉后状态照样记录，再开灯就显示当前状态） |
| 询问模式中 向上 或 按下 | 同意 |
| 询问模式中 向下 | 拒绝 |

亮度和开关记在 `~/.config/sensehat-led/state.json`，重启服务或重启机器都保留。
`confirm` 是唯一会无视开关强制亮灯的状态——需要你确认的时候不该被漏掉。

## HTTP 接口

端口 `8765`，绑定 `0.0.0.0`（仅局域网可达）。

```
GET /                            状态 JSON
GET /ping
GET /state/<idle|busy|done|confirm>
GET /brightness/<0-8|up|down>
GET /enable/<on|off|toggle>
GET /ask?timeout=900             红灯常亮并阻塞等待摇杆回答 -> {"answer":"yes|no|timeout"}
GET /answer/<yes|no>
```

想加口令：编辑 `/etc/systemd/system/sensehat-led.service`，在 `ExecStart` 末尾加
`--token 你的口令`，然后 `sudo systemctl daemon-reload && sudo systemctl restart sensehat-led`；
客户端用 `SENSE_TOKEN=你的口令 sense status`。

## 服务管理

```bash
sudo systemctl status sensehat-led
sudo systemctl restart sensehat-led
journalctl -u sensehat-led -f
```

自检（红、绿、黄、白各闪一下）：

```bash
python3 ~/sensehat-led/sensehat_led.py --selftest
```
