# 已部署设备磁盘保护增量升级

此目录是已经部署设备的独立升级包。它只修改录制磁盘保护相关代码，不修改相机序列号、
同步模式配置、录制目录配置或已有录制数据。

支持的上游基线：

```text
6d2d7c60a82f8ea9754a6a18467289143183dcd0
```

尚未部署的设备应直接使用包含磁盘保护的新版代码，不需要运行此补丁。

## 升级后的行为

- 录制从低于 90% 开始后，磁盘占用首次达到 90% 时自动暂停。
- 90%（含）到 98%（不含）是缓冲区，可由用户手动继续录制，不会立即再次触发 90% 自动暂停。
- 达到 98% 时立即停止录制，并禁用新的录制任务。
- 录制期间每 250 毫秒检查一次，降低高速相机在阈值附近的写入过冲。
- 占用恢复到 90% 以下后，90% 自动暂停功能重新启用。
- 磁盘占用检查失败时按保护原则停止或拒绝录制。
- 单相机、普通多相机和硬件同步多相机录制使用相同的保护逻辑。

## 补丁目录内容

- `apply_upgrade.sh`：执行校验、备份、升级、验证和必要的程序重启。
- `upgrade.patch`：相对于支持基线的最小代码增量。
- `verify_upgrade.py`：在目标代码上验证 90% 暂停、缓冲区继续和 98% 禁止录制。
- `SHA256SUMS`：校验升级包在复制过程中是否损坏。

请复制整个 `disk_guard_90_98` 目录，不要只复制 `upgrade.patch` 或脚本。

## 升级前检查

1. 使用实际运行采集程序的普通用户执行升级，不要使用 `sudo`。
2. 确认目标目录包含 `main.py`、`run_multi_camera_datacollect.sh` 和 `app/`。
3. 确认目标设备已经安装项目所需的 Python 依赖。
4. 确认系统可以执行 `patch`、`sha256sum` 和 `python3`。
5. 如果现场代码在支持基线上做过其他修改，先保留这些修改并停止升级。脚本检测到不兼容代码时会拒绝覆盖。

复制完成后，先检查升级包完整性：

```bash
cd /补丁所在位置/disk_guard_90_98
sha256sum -c SHA256SUMS
```

所有文件均显示 `OK` 后再继续。

## 执行升级

目标参数必须是已部署项目的根目录，推荐使用绝对路径：

```bash
cd /补丁所在位置/disk_guard_90_98
./apply_upgrade.sh /绝对路径/multi_camera_datacollect
```

如果复制过程丢失了可执行权限，先执行：

```bash
chmod +x apply_upgrade.sh verify_upgrade.py
```

当补丁目录仍位于目标项目的 `upgrade_patches/disk_guard_90_98` 下时，也可以省略目标参数：

```bash
./apply_upgrade.sh
```

脚本按以下顺序工作：

1. 校验升级包 SHA256。
2. 确认目标是支持的原版代码或已经完整升级的代码。
3. 将将要修改的原文件备份到补丁目录的 `backups/`。
4. 应用增量补丁并运行 Python 语法检查。
5. 执行磁盘保护行为验证。
6. 如果目标程序原来正在运行，验证成功后重启该目标程序。

脚本只会重启工作目录或启动命令明确指向目标目录的
`multi-camera-datacollect.service`，不会重启同机其他测试目录中的程序。

## Python 环境

脚本默认依次使用目标项目的 `.venv/bin/python` 和系统 `python3`。如果部署使用其他环境，
通过 `PYTHON_BIN` 明确指定：

```bash
PYTHON_BIN=/path/to/python \
  ./apply_upgrade.sh /绝对路径/multi_camera_datacollect
```

指定的 Python 必须能够导入项目原有依赖，包括 PyQt6。

## 暂不重启

如需先完成升级和验证，稍后在维护窗口手动重启：

```bash
./apply_upgrade.sh /绝对路径/multi_camera_datacollect --no-restart
```

使用 `--no-restart` 时，磁盘上的代码已经升级，但当前正在运行的旧进程不会自动加载新代码。
必须在正式录制前手动重启服务或重新启动程序。

服务方式启动时可执行：

```bash
systemctl --user restart multi-camera-datacollect.service
systemctl --user is-active multi-camera-datacollect.service
```

手动运行的程序应先正常退出，再重新执行项目的 `run_multi_camera_datacollect.sh`。

## 成功标志

升级完成时应看到类似输出：

```text
upgrade verification passed: 90% soft stop, buffer restart, 98% hard stop
Upgrade disk_guard_90_98 completed successfully for /目标项目路径
```

随后确认程序可以正常连接相机，并检查录制目录仍然指向预期磁盘。无需通过写满系统盘再次验证；
升级脚本已经使用模拟磁盘占用执行边界行为测试。

## 备份、回滚和重复执行

- 成功升级后，原文件保存在补丁目录的 `backups/disk_guard_90_98_时间戳/`。
- 补丁应用、语法检查或行为验证失败时，脚本会自动恢复本次备份。
- 重复运行是安全的。脚本通过反向补丁校验确认完整升级状态，跳过重复应用并重新验证。
- 如果目标既不是支持的原版，也不是完整升级后的版本，脚本会停止且不修改目标代码。

如果升级过程中机器意外断电，应停止采集程序，保留目标目录和 `backups/`，再根据最近一次
时间戳备份恢复以下文件后重新执行升级：

```text
app/camera_page_base.py
app/camera_process.py
app/multi_camera_sync_process.py
```

如果目标中存在不完整生成的 `app/disk_guard.py`，恢复旧版时应同时移除该文件。

## 常见问题

### 提示升级包校验失败

重新复制完整的 `disk_guard_90_98` 目录，不要单独替换其中一个文件，也不要继续执行补丁。

### 提示目标代码不受支持

目标文件与支持基线不一致，或者只应用过部分补丁。不要强制使用 `patch`；先保留现场代码，
通过 Git diff 或备份确认差异后再处理。

### 提示缺少 Python 模块

使用部署程序实际使用的 Python，通过 `PYTHON_BIN` 重新执行。不要为了通过验证临时切换到
缺少相机运行依赖的解释器。

### 升级成功但程序仍是旧行为

通常是使用了 `--no-restart`，或现场程序不是从传入的目标目录启动。确认进程工作目录后，
正常退出旧进程并从升级后的项目目录重新启动。
