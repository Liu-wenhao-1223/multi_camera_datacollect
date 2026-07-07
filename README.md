# Multi Camera Data Collect

独立版 Orbbec 多相机采集页面，从上位机 `camera page` 拆出。

## 目录

- `main.py`: 独立启动入口。
- `app/`: UI、Qt worker、Orbbec 子进程、录制工具。
- `config/app_settings.json`: 默认数据保存路径配置，默认写入项目内 `record/`。
- `config/multi_device_sync_config.json`: 三相机同步配置，填入真实相机序列号后可严格按 SN 匹配。
- `record/`: 默认录制输出目录。
- `multi_camera_datacollect.desktop`: 快捷启动按钮，默认启动路径为 `/home/workspace/multi_camera_datacollect`。

## 运行

```bash
cd /home/workspace/multi_camera_datacollect
./run_multi_camera_datacollect.sh
```

## 快捷启动

项目根目录下提供了 `multi_camera_datacollect.desktop`，适合把整个目录放到 `/home/workspace/` 后双击启动。

启动器路径已配置为：

```ini
Exec=/home/workspace/multi_camera_datacollect/run_multi_camera_datacollect.sh
Path=/home/workspace/multi_camera_datacollect
```

如果系统提示不信任该启动器，先右键选择允许启动，或执行：

```bash
chmod +x /home/workspace/multi_camera_datacollect/multi_camera_datacollect.desktop
```

如当前环境未安装依赖：

```bash
pip install -r requirements.txt
```

## 多相机同步配置

`config/multi_device_sync_config.json` 中可以填入相机序列号来固定主从关系。程序会保持 SDK/USB 枚举顺序打开相机，但会按真实 SN 给对应物理相机应用 PRIMARY/SECONDARY 配置，因此不再依赖不同机器上的 USB 枚举顺序来决定谁是主相机。

同步线连接到触发输出的那台相机应配置为：

```json
"mode": "PRIMARY",
"trigger_out_enable": true
```

其他接收触发的相机应配置为：

```json
"mode": "SECONDARY",
"trigger_out_enable": false
```

如果某个 SN 没匹配到，程序会保持 USB 顺序补齐剩余相机，并在状态栏显示实际使用的 `usb_index`、SN、mode 和 trigger_out。若已匹配到 PRIMARY，相机中未匹配到 SN 的设备会默认作为 SECONDARY，避免出现两个 PRIMARY。

## Orbbec USB 权限规则

项目根目录下提供了 `99-orbbec.rules`。首次使用或更换系统后，先把规则复制到系统 udev 目录，并重新加载规则：

```bash
sudo cp 99-orbbec.rules /etc/udev/rules.d/99-orbbec.rules
sudo udevadm control --reload-rules
sudo udevadm trigger
```

执行完成后，重新插拔 Orbbec 相机，让 udev 规则生效。规则内容为：

```udev
SUBSYSTEM=="usb", ATTR{idVendor}=="2bc5", MODE:="0666", TAG+="uaccess"
```

录制数据默认保存在 `record/` 下，每次录制会新建带时间戳的文件夹，并写入 `.bag`、相机内参 JSON；勾选点云时会额外写入 `point_clouds/`。
