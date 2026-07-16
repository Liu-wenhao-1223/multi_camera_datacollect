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

如果系统提示不信任该启动器，先右键选择允许启动，或执行：

```bash
chmod +x ~/Desktop/multi_camera_datacollect.desktop
gio set ~/Desktop/multi_camera_datacollect.desktop metadata::trusted true
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

## 录制磁盘保护

程序会监控录制目录所在磁盘的占用情况：

- 从 90% 以下开始录制后，磁盘占用首次达到 90% 时自动暂停录制。
- 90%（含）到 98%（不含）为缓冲区，用户确认后可以手动继续录制。
- 达到 98% 时停止当前录制，并禁止开始新的录制任务。
- 磁盘占用恢复到 90% 以下后，自动暂停保护会重新启用。
- 无法读取磁盘占用时按保护原则停止或拒绝录制。

单相机、普通多相机和硬件同步多相机录制均使用同一套磁盘保护逻辑。

### 部署方式

- 尚未部署的设备：直接使用当前版本代码，不需要运行增量补丁。
- 已经运行旧版代码的设备：复制完整的
  [`upgrade_patches/disk_guard_90_98`](upgrade_patches/disk_guard_90_98) 目录到设备，
  按目录内 README 执行升级，无需重新部署整个项目。

已部署设备的基本升级命令如下，目标路径必须指向设备上的项目根目录：

```bash
cd /补丁所在位置/disk_guard_90_98
./apply_upgrade.sh /绝对路径/multi_camera_datacollect
```

升级脚本会校验补丁、备份原文件、应用修改并执行行为验证。完整的升级前检查、
Python 环境选择、重启、回滚和故障排查说明见补丁目录内的 README。
