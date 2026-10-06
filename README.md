# tilepyramid — 本地图像金字塔与切片生成工具

纯本地实现：输入图片、缩放层、tile、manifest 与缓存只存在于本地文件或内存中，
不依赖地图服务器、CDN、云存储或任何外部服务。支持读取常见 PNG / JPEG。

## 安装与依赖

- Python 3.10+，依赖 `Pillow` 与 `numpy`，测试使用 `pytest`。

## 使用方法

```bash
python -m tilepyramid build <输入图片> -o <输出目录> \
    [--tile-size 256] [--min-size 256] \
    [--resample nearest|bilinear] [--edge crop|pad]
```

输出目录结构：

```
<输出目录>/manifest.json
<输出目录>/tiles/<level>/<x>_<y>.png
```

## 层级算法

- 层级从 **0（最粗糙）** 编号到 **L（原图，全分辨率）**。
- 每向下一层，宽高分别做 ceil 减半：`(n + 1) // 2`，奇数尺寸因此得到确定的处理。
- 当某一层的宽和高都 `<= min_size` 时停止，该层即第 0 层。
- 每一层都直接由原图按目标尺寸一次性缩放生成（而非逐级迭代缩放），
  避免累积误差，保证相同输入得到稳定（逐字节一致）的结果。
- 缩放支持 `nearest` 与 `bilinear` 两种，均基于像素中心约定用 float64 实现，
  结果确定可复现。

## 坐标与边缘规则

- tile 坐标 `(x, y)` 为层级内的列、行索引（从 0 开始），像素原点为该层左上角，
  tile 覆盖 `[x*tile_size, (x+1)*tile_size) × [y*tile_size, (y+1)*tile_size)`。
- 边缘不足一个完整 tile 的区域由 `--edge` 决定：
  - `crop`（默认）：边缘 tile 按实际尺寸裁剪保存（宽/高小于 tile_size）；
  - `pad`：边缘 tile 用 `pad_color`（默认黑色）补齐到完整 tile_size。
- 所有 tile 一律输出为无损 PNG，保证哈希稳定。

## manifest

`manifest.json` 记录校验与增量重建所需的全部信息：

- `source`：原图文件名、尺寸、SHA-256；
- `config`：tile_size、min_size、resample、edge、pad_color；
- `levels[]`：每层的 `level`、宽高、`scale`（相对第 0 层的放大倍数）及 `tiles[]`；
- `tiles[]`：每个 tile 的坐标 `(x, y)`、文件路径、实际尺寸和文件 SHA-256。

## 增量重建与原子写入

- 重建时比对 manifest 中的源图哈希与配置：一致则逐个校验 tile 文件的 SHA-256，
  未变化的 tile 直接复用，**损坏或缺失的 tile 只重新生成对应文件**；
  源图或配置变化则整体重建，并清理不再被引用的旧 tile。
- 所有文件（tile 与 manifest）都先写入同目录临时文件，再用 `os.replace` 原子替换；
  manifest 最后写入，因此生成中断不会留下被 manifest 误认为有效的半文件。
  下次构建开始时自动清理残留的临时文件。

## 运行测试

```bash
python -m pytest tests/ -v
```

测试在终端内生成小型测试图像并输出校验结果，不会打开任何图片窗口。覆盖场景：
奇数尺寸、边缘 tile（crop/pad）、nearest 与 bilinear 两种缩放及其稳定性、
增量重建、tile 损坏/缺失后的重新生成、源图变化后的整体重建、
中途失败（崩溃后不产生有效 manifest、可恢复重建）以及 JPEG 输入。
