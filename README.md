# 单盘灾备恢复服务（Disk Recovery API）

把灾备站收到的**增量包**还原成虚拟机的 **raw 磁盘镜像**。以 4 KiB 块为最小单位，
预置一份 raw 基线镜像（版本 1），之后每次提交只保存本批变动块，链式还原出任意最新版本。

## 核心语义

- **块大小固定 4 KiB**。每个块支持两种操作：
  - `write`：用提交的内容覆盖该块（base64 编码，解码后须为 4 KiB；镜像末块按真实长度）；
  - `discard`：丢弃该块，读取时返回零（按末块真实长度补零）。
- **未列出的块沿用父版**，沿版本链向上回溯，直到基线。
- 提交体携带**目标整盘镜像的 SHA-256**：服务端流式重算整盘摘要，
  **仅当摘要吻合且父版本仍为当前版本**时才发布新版本，返回新版号。
- **原子提交**：变动块先写 `tmp/` 暂存并 fsync，再原子挪入、写元数据、原子替换版本指针；
  任何一步失败都保留原版，失败批次的暂存与块文件会被清理。
- **崩溃恢复**：重启时清理暂存、自基线起逐版校验版本链（元数据 + 块文件齐备），
  只发布最后一个**完整提交**的版本；不完整的尾部版本不会被提供。
- **流式下载快照隔离**：`GET /image` 在响应开始时固定版本，下载期间即使有新版本提交，
  已开始的下载仍返回原版内容；新请求才返回新版。响应头带回版号与摘要。
- **NBD 只读导出**：QEMU 可把任一已提交版本当作只读块设备按需读取（见下文「NBD 只读导出」）。

## NBD 只读导出（QEMU 直读任意已提交版本）

设置环境变量 `NBD_PORT`（docker-compose 已默认 `10809`）即与 HTTP 同进程启动 NBD 服务，
引导演练流程：在 `/docs` 用 `GET /version` 查当前版号，然后用该**十进制版号作为导出名**连接：

```bash
# 查看当前版号（也可在 http://localhost:8000/docs 里操作）
curl http://localhost:8000/version        # => {"version": 2, ...}

# qemu-io 读取版本 2 的任意区间（跨块、非对齐均可）
qemu-io -r -c "read -v 0 4096" nbd://localhost:10809/2

# qemu-img 把版本 2 整盘导出为 raw 文件
qemu-img convert -O raw nbd://localhost:10809/2 disk-v2.raw

# 旧版同样可连（版本 1 基线）
qemu-img info nbd://localhost:10809/1
```

协议与语义：

- **fixed newstyle 握手**：支持 `NBD_OPT_GO` / `NBD_OPT_EXPORT_NAME` / `NBD_OPT_INFO` /
  `NBD_OPT_LIST` / `NBD_OPT_ABORT`；structured reply、TLS、meta context 等选项一律回
  `NBD_REP_ERR_UNSUP` / `NBD_REP_ERR_POLICY`，qemu 会自动回退到简单应答。
- **只接受已提交版**：导出名必须是 `1..当前版` 的十进制数字（空名 = 当前版），
  其余一律拒绝（`NBD_REP_ERR_UNKNOWN`；`NBD_OPT_EXPORT_NAME` 按协议硬断开）。
- **版本钉住**：连接在握手时选定版本，之后提交新版不影响已连接客户端，仍读所选版。
- **按需读取**：只访问请求范围涉及的块，跨块自动拼接；丢弃块读零；末块按真实长度返回。
- **错误语义**：越界读返回 `NBD_EOVERFLOW`；写/修剪类请求返回 `NBD_EROFS`（导出恒为只读，
  qemu 默认读写方式打开会在握手期即被拒，需加 `-r`）。
- **慢连接隔离**：每连接独立线程收发，单个连接收包变慢只阻塞该线程，
  其他连接照常读盘，`/recover/commit` 照常提交增量。
- 单请求上限 32 MiB（`NBD_INFO_BLOCK_SIZE` 通告 min=1 / preferred=4096 / max=32MiB）。
- 注意：qemu 块层以 512 字节扇区为粒度记录设备大小，非 512 对齐的镜像会被向上取整并
  尝试读取填充区（服务端按设计对越界读回 `EOVERFLOW`）。真实磁盘镜像天然扇区对齐，
  不受影响；字节级精确的末尾请用 `GET /image` 或 NBD 直接读取。

## 存储布局

```
data/
├── baseline.raw          # 预置 raw 基线（版本 1 的完整内容）
├── current.json          # 指向最后完整提交版本的指针（原子替换）
├── v000001/meta.json     # 每版元数据：父版、镜像大小、本批变动块清单、整盘 SHA-256
├── v000002/meta.json
├── blocks/
│   └── b0000000005_v000002   # 每个变动块单独落盘（块号_版本号），丢弃块不占数据
└── tmp/                  # 提交暂存，崩溃/重启即清理
```

每批只落盘**变动块 + 一个 meta.json + current 指针**，不产生整盘副本。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- HTTP API 监听 `http://localhost:8000`，NBD 只读导出监听 `10809`（`NBD_PORT` 置空可关闭）
- 交互式文档（Swagger UI，可直接提交）：`http://localhost:8000/docs`
- ReDoc：`http://localhost:8000/redoc`
- 数据持久化在命名卷 `recovery-data`（容器内 `/data`）
- 基线大小通过环境变量 `BASELINE_SIZE` 调整（默认 `16MiB`，支持 `KiB/MiB/GiB` 或字节数）

## 本地开发

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 1) 预置 raw 基线（幂等，已存在且大小一致时直接复用）
python -m app.init_baseline --data-dir ./data --size 16MiB

# 2) 启动 API（NBD_PORT 非空时同进程启动 NBD 只读导出）
RECOVERY_DATA_DIR=./data NBD_PORT=10809 uvicorn app.main:app --reload --port 8000
```

## API

### `POST /recover/commit` — 提交增量并发布新版本

```json
{
  "parent_version": 1,
  "target_sha256": "<还原后整盘镜像期望的 SHA-256，64 位十六进制>",
  "changes": [
    {"index": 3, "op": "write", "data_base64": "<base64，解码后 4096 字节>"},
    {"index": 10, "op": "discard"}
  ]
}
```

成功 `201`：

```json
{"version": 2, "parent_version": 1, "sha256": "...", "image_size": 16777216, "block_count": 4096}
```

错误语义：

| 情况 | 状态码 |
| --- | --- |
| 摘要不吻合 / 块越界 / 重复块号 / base64 或长度非法 | `400` |
| 父版本不是当前版本（并发冲突，需基于最新版重新生成增量包） | `409` |
| 请求体不符合 schema | `422` |

### `GET /image` — 流式下载当前 raw 镜像

响应头：`Content-Length`（镜像字节数）、`X-Image-Version`（版号）、
`X-Image-Sha256`（该版摘要）、`Content-Disposition`。下载开始即固定版本快照。

### 其它

- `GET /version`：当前版号、镜像大小、块数、SHA-256
- `GET /health`：健康检查
- `GET /docs`、`GET /openapi.json`：交互式文档与 OpenAPI 描述

## 测试

```bash
pip install -r requirements.txt httpx pytest
python -m pytest -q
```

覆盖 43 个用例，包括：链式继承、丢弃读零、覆盖、丢弃后重写、过期父版 409、
摘要不符 400 且原版保留、只落盘变动块、失败回滚后同版本号重试、读/提交并发安全、
崩溃暂存清理与版本回退、缺失块文件的版本被跳过、真实 uvicorn 下的下载快照隔离、
重启后只提供最后完整版本；NBD 侧：fixed newstyle 握手（GO/EXPORT_NAME/INFO/LIST/ABORT）、
只接受已提交版、旧版可连、跨块/非对齐读、丢弃块读零、末块真实长度、越界 EOVERFLOW、
写/修剪 EROFS、流水线请求按序应答、版本钉住、多连接并发、慢连接期间换版与提交隔离。
`qemu-io`/`qemu-img` 在 PATH 中时，还会跑真实 QEMU 客户端集成用例
（qemu-img 逐版整盘导出比对、qemu-io 跨块读与写拒绝）。

另有不依赖测试框架的真实冒烟脚本：

```bash
python -m app.init_baseline --data-dir /tmp/demo --size 256KiB
RECOVERY_DATA_DIR=/tmp/demo NBD_PORT=10809 uvicorn app.main:app --port 8000 &
python scripts/smoke_http.py            # HTTP 侧，默认打 http://127.0.0.1:8011，可用 SMOKE_BASE 覆盖
python scripts/smoke_nbd.py             # NBD 侧，默认连 127.0.0.1:10809，可用 NBD_ADDR 覆盖
```

## 设计取舍与限制

- 单进程单 worker（compose 已固定 `--workers 1`）。提交串行化依赖进程内锁，
  并辅以 `fcntl` 文件锁防止同机多进程写同一数据目录；多副本水平扩展需替换为外部分布式锁。
- NBD 与 HTTP 同进程：每连接一个线程，读路径只持细粒度锁、发送期间不持锁，
  因此慢连接不会阻塞其他连接与增量提交；版本数据一旦发布即不可变，旧版读取无需快照拷贝。
- 版本链顺序读取，单块读取最坏为 O(版本数)；读路径有块句柄 LRU 缓存。
  面向灾备恢复场景（版本增量提交、整盘流式下载为主），不做后台压实（compaction）。
- 整盘摘要在每次提交时按块流式重算，内存占用恒定为块级，不缓存整盘镜像。
- 基线镜像内容为确定性生成的可复现图案，便于核对；如需植入自有镜像，
  可直接替换 `baseline.raw` 并重建 `v000001/meta.json` 与 `current.json`。
