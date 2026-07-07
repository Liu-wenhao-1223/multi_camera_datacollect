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
cd ~/workspace/multi_camera_datacollect
./run_multi_camera_datacollect.sh
```

## 快捷启动

项目根目录下提供了 `multi_camera_datacollect.desktop`。这个文件适合复制到桌面作为启动图标使用，启动时会用 Python 获取当前用户的真实 Home 目录，并执行 `~/workspace/multi_camera_datacollect/run_multi_camera_datacollect.sh`。

启动器执行逻辑为：

```ini
Exec=... Path.home()/workspace/multi_camera_datacollect/run_multi_camera_datacollect.sh
```

如果双击后没有反应，查看桌面上的 `multi_camera_datacollect_launch.log`，里面会记录实际查找的目录和启动错误。

如果系统提示不信任该启动器，先右键选择允许启动，或执行：

```bash
chmod +x ~/Desktop/multi_camera_datacollect.desktop
gio set ~/Desktop/multi_camera_datacollect.desktop metadata::trusted true
```

如当前环境未安装依赖：

```bash
pip install -r requirements.txt
```

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
