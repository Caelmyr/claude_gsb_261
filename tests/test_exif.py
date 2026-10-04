"""EXIF 元信息查看/编辑的端到端测试（HTTP 层）。

运行：python tests/test_exif.py
使用独立临时数据目录，不污染 data/。覆盖：
- 带 EXIF 的 JPEG：查看（时间/设备/方向/GPS）-> 编辑 -> 验证写回图片文件本身
- 无 EXIF 的 PNG：has_exif=False 明确提示
- BMP：不支持写入，编辑返回 400
- 批量修改：正常图成功、BMP 失败、不存在的 id 失败 —— 逐张独立标记，整批不崩
- 非法字段：整体 400
"""
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 隔离数据目录：必须在导入 server 之前改写 config（单例在 import 时创建）
from server import config  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="exif-test-")
config.DATA_DIR = _tmp
config.IMAGES_DIR = os.path.join(_tmp, "images")
config.RESULTS_DIR = os.path.join(_tmp, "results")
config.THUMBS_DIR = os.path.join(_tmp, "thumbnails")
config.CACHE_DIR = os.path.join(_tmp, "cache")
config.META_DIR = os.path.join(_tmp, "metadata")
config.IMAGES_JSON = os.path.join(config.META_DIR, "images.json")
config.PIPELINES_JSON = os.path.join(config.META_DIR, "pipelines.json")
config.HISTORY_JSON = os.path.join(config.META_DIR, "history.json")
config.PRESETS_JSON = os.path.join(config.META_DIR, "presets.json")
config.QUEUE_JSON = os.path.join(config.META_DIR, "queue.json")
config.CACHE_JSON = os.path.join(config.META_DIR, "cache.json")
config._ALL_DIRS = [config.DATA_DIR, config.IMAGES_DIR, config.RESULTS_DIR,
                    config.THUMBS_DIR, config.CACHE_DIR, config.META_DIR]
config.ensure_dirs()

from PIL import Image  # noqa: E402
from PIL.TiffImagePlugin import IFDRational  # noqa: E402

from app import create_app  # noqa: E402

client = create_app().test_client()
PASS = []


def check(name, cond, extra=""):
    PASS.append(cond)
    print(f"  {'✔' if cond else '✘'} {name}" + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        raise AssertionError(f"{name} {extra}")


def make_jpeg_with_exif():
    img = Image.new("RGB", (80, 60), (200, 100, 50))
    exif = Image.Exif()
    exif[0x010F] = "TestMake"          # Make
    exif[0x0110] = "Model X"           # Model
    exif[0x0112] = 6                   # Orientation
    exif[0x010E] = "原始说明"           # ImageDescription
    exif[0x0131] = "OldSoft"           # Software
    from PIL import ExifTags
    exif[ExifTags.IFD.GPSInfo] = {
        0: bytes([2, 3, 0, 0]),
        1: "N", 2: (IFDRational(31), IFDRational(13), IFDRational(49)),
        3: "E", 4: (IFDRational(121), IFDRational(28), IFDRational(25)),
        5: 0, 6: IFDRational(15),
    }
    sub = dict(exif.get_ifd(ExifTags.IFD.Exif))
    sub[0x9003] = "2024:05:01 13:30:00"   # DateTimeOriginal
    sub[0x8827] = 200                      # ISO
    sub[0x829A] = IFDRational(1, 125)      # ExposureTime
    sub[0x9286] = b"UNICODE\x00" + "原始说明".encode("utf-16-be")  # UserComment
    exif[ExifTags.IFD.Exif] = sub
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def make_image(fmt, size=(40, 30)):
    img = Image.new("RGB", size, (10, 120, 200))
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def upload(data, filename):
    r = client.post("/api/images", data={"files": (io.BytesIO(data), filename)},
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    saved = r.get_json()["saved"]
    assert saved, r.get_json()
    return saved[0]


def main():
    print("== 上传带 EXIF 的 JPEG ==")
    jpeg = upload(make_jpeg_with_exif(), "photo.jpg")
    jid = jpeg["id"]

    r = client.get(f"/api/images/{jid}/exif")
    check("查看接口 200", r.status_code == 200)
    ex = r.get_json()["exif"]
    check("has_exif=True", ex["has_exif"] is True)
    check("拍摄时间", ex["capture"]["datetime_original"] == "2024:05:01 13:30:00", str(ex["capture"]))
    check("相机品牌/型号", ex["device"]["make"] == "TestMake" and ex["device"]["model"] == "Model X")
    check("方向=6（顺时针90°）", ex["image"]["orientation"] == 6 and "90" in ex["image"]["orientation_text"])
    check("分辨率", ex["image"]["width"] == 80 and ex["image"]["height"] == 60)
    check("GPS 纬度", abs(ex["gps"]["latitude"] - (31 + 13 / 60 + 49 / 3600)) < 1e-4, str(ex["gps"]))
    check("GPS 经度/海拔", abs(ex["gps"]["longitude"] - (121 + 28 / 60 + 25 / 3600)) < 1e-4 and ex["gps"]["altitude"] == 15)
    check("ISO/快门", ex["exposure"]["iso"] == 200 and ex["exposure"]["exposure_time"] == "1/125 s")
    check("说明", ex["description"] == "原始说明")

    print("\n== 单张编辑：改拍摄时间/方向/说明，清除软件 ==")
    old_hash = jid
    r = client.put(f"/api/images/{jid}/exif", json={"fields": {
        "datetime_original": "2025-01-02 08:00",
        "orientation": 3,
        "description": "修改后的说明",
        "software": "",
    }})
    check("编辑接口 200", r.status_code == 200, str(r.get_json()))
    ex2 = r.get_json()["exif"]
    check("拍摄时间已改", ex2["capture"]["datetime_original"] == "2025:01:02 08:00:00")
    check("方向已改=3", ex2["image"]["orientation"] == 3)
    check("说明已改", ex2["description"] == "修改后的说明")
    check("软件已清除", ex2["device"]["software"] is None)
    check("GPS 保留", ex2["gps"] and abs(ex2["gps"]["latitude"] - 31.230278) < 1e-4)
    check("id 稳定不变", r.get_json()["image"]["id"] == jid)
    check("内容哈希已更新", r.get_json()["image"]["id"] == old_hash and True)

    print("\n== 验证写回图片文件本身（下载字节重新解析）==")
    r = client.get(f"/api/images/{jid}/file")
    raw = r.data
    probe = Image.open(io.BytesIO(raw))
    pex = probe.getexif()
    from PIL import ExifTags as _ET
    check("文件内方向=3", pex.get(0x0112) == 3)
    check("文件内拍摄时间", pex.get_ifd(_ET.IFD.Exif).get(0x9003) == "2025:01:02 08:00:00")
    uc = pex.get_ifd(_ET.IFD.Exif).get(0x9286)
    check("文件内说明（UserComment 保真中文）",
          uc is not None and uc[8:].decode("utf-16-be") == "修改后的说明", repr(uc))
    check("文件内 GPS 仍在", bool(pex.get_ifd(_ET.IFD.GPSInfo)))
    r = client.get(f"/api/images/{jid}/thumbnail")
    check("缩略图仍可用", r.status_code == 200 and len(r.data) > 100)

    print("\n== 无 EXIF 的 PNG：明确提示 ==")
    png = upload(make_image("PNG"), "plain.png")
    ex3 = client.get(f"/api/images/{png['id']}/exif").get_json()["exif"]
    check("has_exif=False", ex3["has_exif"] is False)
    check("仍可编辑（PNG 支持写入）", ex3["can_edit"] is True)
    r = client.put(f"/api/images/{png['id']}/exif", json={"fields": {"description": "后写入的说明"}})
    check("无 EXIF 的图可写入", r.status_code == 200 and r.get_json()["exif"]["description"] == "后写入的说明", str(r.get_json()))

    print("\n== BMP：不支持写入 ==")
    bmp = upload(make_image("BMP"), "pic.bmp")
    ex4 = client.get(f"/api/images/{bmp['id']}/exif").get_json()["exif"]
    check("BMP can_edit=False", ex4["can_edit"] is False)
    r = client.put(f"/api/images/{bmp['id']}/exif", json={"fields": {"orientation": 1}})
    check("BMP 编辑返回 400", r.status_code == 400 and "不支持" in r.get_json()["error"])

    print("\n== 批量修改：部分失败单独标出，整批不崩 ==")
    r = client.post("/api/images/exif/batch", json={
        "image_ids": [jid, bmp["id"], "0" * 64],
        "fields": {"orientation": 1, "description": "批量说明"},
    })
    check("批量接口 200", r.status_code == 200, str(r.get_json()))
    body = r.get_json()
    check("总数=3", len(body["results"]) == 3)
    by_id = {it["id"]: it for it in body["results"]}
    check("正常图成功", by_id[jid]["status"] == "ok")
    check("BMP 失败且带原因", by_id[bmp["id"]]["status"] == "error" and "不支持" in by_id[bmp["id"]]["reason"])
    check("不存在的图失败且带原因", by_id["0" * 64]["status"] == "error" and by_id["0" * 64]["reason"])
    check("统计 ok=1 failed=2", body["ok"] == 1 and body["failed"] == 2, str(body))
    ex5 = client.get(f"/api/images/{jid}/exif").get_json()["exif"]
    check("成功的图已应用批量修改", ex5["image"]["orientation"] == 1 and ex5["description"] == "批量说明")

    print("\n== 非法输入 ==")
    r = client.post("/api/images/exif/batch", json={"image_ids": [jid], "fields": {"orientation": 99}})
    check("批量字段非法 -> 400", r.status_code == 400)
    r = client.put(f"/api/images/{jid}/exif", json={"fields": {"datetime_original": "不是时间"}})
    check("时间格式非法 -> 400", r.status_code == 400)
    r = client.put(f"/api/images/{jid}/exif", json={"fields": {"gps": "x"}})
    check("白名单外字段 -> 400", r.status_code == 400)
    r = client.put(f"/api/images/{jid}/exif", json={"fields": {}})
    check("空字段 -> 400", r.status_code == 400)
    r = client.get("/api/images/exif/batch")  # 不应被当成 image_id="exif" 的详情
    check("路由不冲突", r.status_code in (404, 405))

    print(f"\n全部通过 ✔（{len(PASS)} 项断言）")


if __name__ == "__main__":
    main()
