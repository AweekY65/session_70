# pyramid-tiler

纯本地的图像金字塔（image pyramid）与切片（tile）生成工具。所有输入图片、
缩放层、tile、manifest 和中间缓存只存在于**本地文件或内存**中，不依赖地图
服务器、CDN、云存储或任何外部服务。

## 安装与依赖

- Python ≥ 3.10
- `numpy`、`Pillow`（运行），`pytest`（仅测试）

## 使用方法

```bash
# 生成金字塔（命令行）
python -m pyramid_tiler input.png -o out/ \
    --tile-size 256 --resample bilinear --min-size 256

# 校验已有输出与 manifest 是否一致（检测缺失/损坏 tile）
python -m pyramid_tiler input.png -o out/ --verify
```

Python API：

```python
from pyramid_tiler import PyramidConfig, build_pyramid, verify_manifest

result = build_pyramid("input.jpg", "out/", PyramidConfig(
    tile_size=256, resample="bilinear", min_size=256))
print(result.tiles_written, result.tiles_reused)
assert verify_manifest("out/") == []
```

支持读取常见 PNG / JPEG（以及 Pillow 可解码的其他格式），内部统一为
RGB / RGBA 处理；tile 一律以无损 PNG 写出，保证哈希稳定。

## 层级算法

- level 0 为原图；每一级宽高各取上一级的 **1/2（向上取整，最小为 1）**：
  `w[n+1] = max(1, ceil(w[n] / 2))`。
- 当某一级满足 `max(w, h) <= min_size` 时停止，该最小层也包含在金字塔中。
- 例如 253×177、`min_size=32` 的层级为：
  `253×177 → 127×89 → 64×45 → 32×23`。

## 缩放方式

`nearest` 与 `bilinear` 均用 numpy 实现，采用标准的像素中心映射
（`src = (dst + 0.5) * scale - 0.5`），bilinear 在 float64 下插值并做
round-half-up。实现无任何随机性，**相同输入必得到逐位相同的结果**
（manifest 中的哈希可复现）。

## 坐标与边缘 tile 规则

- 坐标原点在每级图像的**左上角**，tile `(x, y)` 覆盖像素区域
  `[x*ts, x*ts+ts) × [y*ts, y*ts+ts)`（`ts` 为 tile size）。
- 每级 tile 数为 `ceil(w/ts) × ceil(h/ts)`。
- **所有 tile 文件都是完整的 `ts × ts`**。边缘不足一个 tile 的区域用
  **零值 padding**（RGBA 为透明黑，RGB 为黑色）；真实内容尺寸记录在
  manifest 的 `valid_width` / `valid_height` 字段中，裁剪时按该尺寸读取即可。

输出目录结构：

```
out/
  manifest.json
  tiles/level_00/tile_0_0.png ...
  tiles/level_01/...
```

## Manifest

`manifest.json` 记录校验与增量重建所需的全部信息：

- `source`：原图文件名、尺寸、色彩模式、源文件 SHA-256；
- `config`：tile size、缩放方式、min size、tile 格式；
- `levels[]`：每级的宽高、tile 行列数，以及每个 tile 的
  坐标 `(x, y)`、文件路径、文件尺寸、`valid_width/valid_height`、
  字节数和 **SHA-256 哈希**。

## 增量重建与原子性

- 再次运行时，若 manifest 中的源文件哈希与配置均匹配，则逐个校验 tile：
  文件存在且哈希一致的 tile **直接跳过（不重写）**；缺失或损坏的 tile
  只重新生成对应项。源图或配置变化会触发整体重建。
- 每个 tile 先写入同目录临时文件，`fsync` 后用 `os.replace` **原子替换**；
  `manifest.json` 最后同样以原子方式写入。因此生成中途被中断时，磁盘上的
  manifest 永远是上一份完整一致的状态，绝不会把半文件误认为有效；
  残留的 `*.tmp` 临时文件会在下次运行时被自动清理。

## 运行测试

测试在终端内生成小型测试图像并输出校验结果，不会打开任何图片窗口：

```bash
python -m pytest tests/ -v        # 或
python -m unittest discover tests -v
```

覆盖场景：奇数尺寸层级、边缘 tile 的 padding/裁剪、nearest 与 bilinear
的差异与确定性、增量重建（不重写未变化 tile）、缺失/损坏 tile 的定点重生成、
中途崩溃后的 manifest 一致性与恢复、JPEG 输入、manifest 字段与哈希校验。
