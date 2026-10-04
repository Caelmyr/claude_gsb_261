"""元信息（EXIF）查看与编辑的独立测试。

运行：python tests/test_metadata.py
覆盖：读取整理、中文说明（UserComment）、字段级校验、JPEG/PNG/WebP 无损
写回（像素字节保持不变）、删除字段、BMP/GIF 拒绝写、批量隔离失败。
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ExifTags  # noqa: E402
from PIL.ExifTags import GPS as GP  # noqa: E402

from server import config  # noqa: E402
from server.image_store import ImageStore  # noqa: E402
from server import metadata as md  # noqa: E402

_passed = 0


def check(name, cond, detail=""):
    global _passed
    assert cond, f"{name} 失败 {detail}"
    _passed += 1
    print(f"  ✔ {name}")


def make_image(fmt, with_exif=False, text="中文相机测试"):
    img = Image.new("RGB", (120, 80), (30, 60, 90))
    buf = io.BytesIO()
    if with_exif:
        exif = Image.Exif()
        exif[0x010F] = "TestCam"
        exif[0x0110] = "X-Pro 9000"
        exif[0x0112] = 6
        sub = exif.get_ifd(ExifTags.IFD.Exif)
        sub[0xA434] = "Zoom 24-70mm f/2.8"
        sub[0x9003] = "2023:07:15 09:30:00"
        sub[0x9004] = "2023:07:15 09:30:05"
        sub[0x829A] = 1 / 250.0         # 曝光时间
        sub[0x829D] = 2.8               # F 值
        sub[0x8827] = 400               # ISO
        sub[0x920A] = 35.0              # 焦距
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
        gps[GP.GPSLatitudeRef] = "N"
        gps[GP.GPSLatitude] = (31.0, 14.0, 30.0)
        gps[GP.GPSLongitudeRef] = "E"
        gps[GP.GPSLongitude] = (121.0, 28.0, 0.0)
        gps[GP.GPSAltitude] = (125, 1)
        sub[0x9286] = b"UNICODE\x00" + text.encode("utf-16-be")
        img.save(buf, fmt, exif=exif.tobytes())
    else:
        img.save(buf, fmt)
    return buf.getvalue()


def pixel_stream(data, fmt):
    """提取与编码无关的像素特征，用于证明写回没有重编码图像内容。"""
    im = Image.open(io.BytesIO(data))
    return list(im.convert("RGB").getdata())[::377]


def main():
    config.ensure_dirs()
    store = ImageStore()

    print("== 读取：带 EXIF 的 JPEG ==")
    rec = store.save_upload(make_image("JPEG", with_exif=True), "exif.jpg")
    info = md.read_metadata(store.file_path(rec["id"]))
    check("拍摄时间", info["taken_at_raw"] == "2023:07:15 09:30:00", info["taken_at_raw"])
    check("时间输入框格式", info["taken_at_input"] == "2023-07-15T09:30:00", info["taken_at_input"])
    check("厂商/型号", info["make"] == "TestCam" and info["model"] == "X-Pro 9000")
    check("方向=6", info["orientation"] == 6 and "90" in info["orientation_label"])
    check("GPS 纬度", abs(info["gps"]["latitude"] - 31.2416667) < 1e-5, info["gps"])
    check("GPS 经度", abs(info["gps"]["longitude"] - 121.4666667) < 1e-5)
    check("GPS 海拔", info["gps"]["altitude"] == 125.0)
    check("中文说明", info["description"] == "中文相机测试", info["description"])
    check("曝光参数", info["exposure"]["iso"] == 400 and info["exposure"]["f_number"] == 2.8,
          info["exposure"])
    check("有原始标签表", len(info["raw_tags"]) >= 8, len(info["raw_tags"]))
    check("无缺失提示", info["missing_fields"] == [], info["missing_fields"])
    check("可写", info["writable"] is True)

    print("== 读取：无 EXIF 的 PNG（明确提示缺失）==")
    rec2 = store.save_upload(make_image("PNG"), "clean.png")
    info2 = md.read_metadata(store.file_path(rec2["id"]))
    check("has_exif=False", info2["has_exif"] is False)
    check("缺失字段齐全", set(info2["missing_fields"]) ==
          {"taken_at", "make", "model", "lens", "orientation", "gps", "description"},
          info2["missing_fields"])
    check("缺失中文标签", "拍摄时间" in info2["missing_labels"])
    check("GPS 为空", info2["gps"] is None)

    print("== JPEG 无损写回 ==")
    jpg = make_image("JPEG", with_exif=True)
    im = Image.open(io.BytesIO(jpg))
    # 找出扫描数据起点，写回后 SOS 之后的字节必须逐字节一致
    p, n = 2, len(jpg)
    while p < n:
        if jpg[p] != 0xFF:
            break
        while jpg[p] == 0xFF:
            p += 1
        m = jpg[p]
        p += 1
        if m == 0xDA:
            break
        seg = int.from_bytes(jpg[p:p + 2], "big")
        p += seg
    scan_before = jpg[p - 2:]

    tmp = os.path.join(config.IMAGES_DIR, "t-jpeg.jpg")
    with open(tmp, "wb") as f:
        f.write(jpg)
    out = md.apply_metadata(tmp, {"taken_at": "2025-01-02 03:04:05", "orientation": 3,
                                  "description": "改后的中文说明",
                                  "gps_lat": -33.8688, "gps_lon": 151.2093})
    p2, n2 = 2, len(out)
    while p2 < n2:
        if out[p2] != 0xFF:
            break
        while out[p2] == 0xFF:
            p2 += 1
        m = out[p2]
        p2 += 1
        if m == 0xDA:
            break
        seg = int.from_bytes(out[p2:p2 + 2], "big")
        p2 += seg
    scan_after = out[p2 - 2:]
    check("扫描数据逐字节一致（无重编码）", scan_before == scan_after,
          f"{len(scan_before)} vs {len(scan_after)}")

    info3 = None
    with open(tmp, "wb") as f:
        f.write(out)
    info3 = md.read_metadata(tmp)
    check("新拍摄时间", info3["taken_at_raw"] == "2025:01:02 03:04:05")
    check("新方向", info3["orientation"] == 3)
    check("新中文说明", info3["description"] == "改后的中文说明")
    check("新 GPS 南纬", info3["gps"]["latitude"] < 0 and abs(info3["gps"]["latitude"] + 33.8688) < 2e-4,
          info3["gps"])
    check("厂商等其它字段保留", info3["make"] == "TestCam")
    os.unlink(tmp)

    print("== JPEG 清除字段（时间/GPS/说明/方向）==")
    tmp = os.path.join(config.IMAGES_DIR, "t-jpeg2.jpg")
    with open(tmp, "wb") as f:
        f.write(jpg)
    out = md.apply_metadata(tmp, {"taken_at": None, "gps_lat": "", "gps_lon": "",
                                  "description": None, "orientation": None})
    with open(tmp, "wb") as f:
        f.write(out)
    info4 = md.read_metadata(tmp)
    check("时间已清除", info4["taken_at"] is None)
    check("GPS 已清除", info4["gps"] is None)
    check("说明已清除", info4["description"] is None)
    check("方向已清除", info4["orientation"] is None)
    check("型号仍在", info4["model"] == "X-Pro 9000")
    check("现在提示缺拍摄时间", "taken_at" in info4["missing_fields"])
    os.unlink(tmp)

    print("== 原本无 EXIF 的图写入 ==")
    tmp = os.path.join(config.IMAGES_DIR, "t-clean.jpg")
    with open(tmp, "wb") as f:
        f.write(make_image("JPEG"))
    out = md.apply_metadata(tmp, {"taken_at": "2024:06:01 12:00:00", "orientation": 8,
                                  "description": "从零添加", "gps_lat": 39.9, "gps_lon": 116.4})
    with open(tmp, "wb") as f:
        f.write(out)
    info5 = md.read_metadata(tmp)
    check("新建时间", info5["taken_at_raw"] == "2024:06:01 12:00:00")
    check("新建方向", info5["orientation"] == 8)
    check("新建说明", info5["description"] == "从零添加")
    check("新建 GPS", abs(info5["gps"]["latitude"] - 39.9) < 2e-4)
    os.unlink(tmp)

    print("== 字段级校验（批量隔离的基础）==")
    tmp = os.path.join(config.IMAGES_DIR, "t-val.jpg")
    with open(tmp, "wb") as f:
        f.write(make_image("JPEG", with_exif=True))
    for bad, expect_field in [
        ({"taken_at": "not-a-date"}, "taken_at"),
        ({"orientation": 9}, "orientation"),
        ({"gps_lat": 200, "gps_lon": 0}, "gps_lat"),
        ({"gps_lat": "abc", "gps_lon": 0}, "gps_lat"),
        ({"description": "x" * 2001}, "description"),
    ]:
        try:
            md.apply_metadata(tmp, bad)
            raise AssertionError(f"{bad} 应报错")
        except md.MetadataError as exc:
            check(f"非法字段定位 {expect_field}", expect_field in exc.fields, exc.fields)
    # 非法写入后原文件不动
    untouched = md.read_metadata(tmp)
    check("非法写入不影响原文件", untouched["orientation"] == 6)
    os.unlink(tmp)

    print("== PNG / WebP 无损写回 ==")
    for fmt in ("PNG", "WEBP"):
        data = make_image(fmt, with_exif=True)
        tmp = os.path.join(config.IMAGES_DIR, f"t-meta.{fmt.lower()}")
        with open(tmp, "wb") as f:
            f.write(data)
        before_pixels = pixel_stream(data, fmt)
        out = md.apply_metadata(tmp, {"taken_at": "2022:12:31 23:59:00", "orientation": 1,
                                      "description": f"{fmt} 中文", "gps_lat": 10.0,
                                      "gps_lon": 20.0})
        after_pixels = pixel_stream(out, fmt)
        check(f"{fmt} 像素一致", before_pixels == after_pixels)
        with open(tmp, "wb") as f:
            f.write(out)
        info6 = md.read_metadata(tmp)
        check(f"{fmt} 时间写入", info6["taken_at_raw"] == "2022:12:31 23:59:00")
        check(f"{fmt} 中文说明", info6["description"] == f"{fmt} 中文")
        check(f"{fmt} GPS", info6["gps"] and abs(info6["gps"]["longitude"] - 20.0) < 2e-4)
        # 再改一次，确认重复写不丢信息
        out2 = md.apply_metadata(tmp, {"orientation": 2})
        with open(tmp, "wb") as f:
            f.write(out2)
        info7 = md.read_metadata(tmp)
        check(f"{fmt} 二次写保留时间", info7["taken_at_raw"] == "2022:12:31 23:59:00")
        check(f"{fmt} 二次写更新方向", info7["orientation"] == 2)
        os.unlink(tmp)

    print("== WebP 无 VP8X 也能写 ==")
    simple = make_image("WEBP")
    tmp = os.path.join(config.IMAGES_DIR, "t-simple.webp")
    with open(tmp, "wb") as f:
        f.write(simple)
    out = md.apply_metadata(tmp, {"taken_at": "2020:01:01 00:00:00", "orientation": 6})
    with open(tmp, "wb") as f:
        f.write(out)
    check("简单 WebP 可回读", md.read_metadata(tmp)["orientation"] == 6)
    os.unlink(tmp)

    print("== BMP/GIF 拒绝写 ==")
    for fmt, ext in (("BMP", "bmp"), ("GIF", "gif")):
        tmp = os.path.join(config.IMAGES_DIR, f"t-no.{ext}")
        Image.new("RGB", (32, 32)).save(tmp, fmt)
        try:
            md.apply_metadata(tmp, {"orientation": 1})
            raise AssertionError(f"{fmt} 应拒绝")
        except md.MetadataError as exc:
            check(f"{fmt} 明确报错", "不支持" in str(exc))
        info8 = md.read_metadata(tmp)
        check(f"{fmt} 可读且标注不可写", info8["writable"] is False and info8.get("unsupported_reason"))
        os.unlink(tmp)

    print("\n元信息测试全部通过 ✔ （%d 项断言）" % _passed)


if __name__ == "__main__":
    main()
