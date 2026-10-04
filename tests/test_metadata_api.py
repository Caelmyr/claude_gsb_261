"""元信息功能的 HTTP 端到端测试（Flask test client，不开真实端口）。

运行：python tests/test_metadata_api.py
覆盖：GET 查看（有/无 EXIF）、PUT 单图编辑与字段错误高亮、
POST 批量修改中坏图单独失败而好图照常成功、BMP 不阻塞整批、写回后重新读回。
"""
import io
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ExifTags  # noqa: E402

from server import config  # noqa: E402

_passed = 0


def check(name, cond, detail=""):
    global _passed
    assert cond, f"{name} 失败 {detail}"
    _passed += 1
    print(f"  ✔ {name}")


def make(fmt, name, exif=None):
    img = Image.new("RGB", (200, 150), (70, 40, 20))
    buf = io.BytesIO()
    if exif is not None:
        img.save(buf, fmt, exif=exif.tobytes())
    else:
        img.save(buf, fmt)
    return (io.BytesIO(buf.getvalue()), name)


def main():
    # 干净的数据目录，避免受其它测试影响
    for d in (config.IMAGES_DIR, config.META_DIR):
        shutil.rmtree(d, ignore_errors=True)
    config.ensure_dirs()

    import app as app_mod
    # 重建依赖单例（它们在导入时已绑定旧的 meta 文件）
    import importlib
    from server import api as api_mod
    importlib.reload(api_mod)
    importlib.reload(app_mod)
    client = app_mod.app.test_client()

    print("== 准备 4 张图：带 EXIF JPEG、干净 JPEG、干净 PNG、BMP ==")
    exif = Image.Exif()
    exif[0x010F] = "ACME"
    exif[0x0110] = "Cam A1"
    exif[0x0112] = 6
    sub = exif.get_ifd(ExifTags.IFD.Exif)
    sub[0x9003] = "2019:03:08 18:00:00"
    sub[0x9286] = b"UNICODE\x00" + "原图中文说明".encode("utf-16-be")

    files = [
        make("JPEG", "with.jpg", exif=exif),
        make("JPEG", "plain.jpg"),
        make("PNG", "plain.png"),
        make("BMP", "plain.bmp"),
    ]
    resp = client.post("/api/images", data={"files": files}, content_type="multipart/form-data")
    body = resp.get_json()
    check("上传 4 张成功", len(body["saved"]) == 4, body)
    ids = {r["filename"]: r["id"] for r in body["saved"]}

    print("== 查看：有 EXIF / 无 EXIF ==")
    r = client.get(f"/api/images/{ids['with.jpg']}/metadata").get_json()
    m = r["metadata"]
    check("有 EXIF 标识", m["has_exif"] is True)
    check("拍摄时间可读", m["taken_at_raw"] == "2019:03:08 18:00:00")
    check("中文说明可读", m["description"] == "原图中文说明")
    check("方向标签", m["orientation"] == 6)

    r = client.get(f"/api/images/{ids['plain.jpg']}/metadata").get_json()
    check("干净图 has_exif=False", r["metadata"]["has_exif"] is False)
    check("明确列出缺失项", "拍摄时间" in r["metadata"]["missing_labels"])

    r = client.get(f"/api/images/{ids['plain.bmp']}/metadata").get_json()
    check("BMP 标记不可写", r["metadata"]["writable"] is False)
    check("BMP 给出原因", "不支持" in r["metadata"]["unsupported_reason"])

    r = client.get("/api/images/does-not-exist/metadata")
    check("不存在返回 404", r.status_code == 404)

    print("== 单图编辑：改时间/方向/说明/GPS ==")
    resp = client.put(f"/api/images/{ids['plain.jpg']}/metadata", json={
        "taken_at": "2024-02-29 10:11:12",
        "orientation": 3,
        "description": "新写入的中文说明 🎉",
        "gps_lat": 35.6895,
        "gps_lon": 139.6917,
    })
    check("PUT 200", resp.status_code == 200, resp.get_json())
    r = resp.get_json()["metadata"]
    check("时间已写入", r["taken_at_raw"] == "2024:02:29 10:11:12", r["taken_at_raw"])
    check("方向已写入", r["orientation"] == 3)
    check("说明已写入（emoji）", r["description"] == "新写入的中文说明 🎉")
    check("GPS 已写入", abs(r["gps"]["latitude"] - 35.6895) < 2e-4, r["gps"])

    # 重新通过 GET 读文件本身，证明真的保存进了图片
    again = client.get(f"/api/images/{ids['plain.jpg']}/metadata").get_json()["metadata"]
    check("重新打开图片仍在（持久化）", again["description"] == "新写入的中文说明 🎉")
    check("缺失提示消失", "说明" not in again.get("missing_labels", []))

    print("== 单图字段错误：422 + fields 定位 ==")
    resp = client.put(f"/api/images/{ids['plain.jpg']}/metadata", json={
        "taken_at": "昨天下午", "orientation": 99,
        "gps_lat": 999, "gps_lon": 0,
    })
    check("返回 422", resp.status_code == 422, resp.status_code)
    fields = resp.get_json()["fields"]
    check("定位到时间字段", "taken_at" in fields)
    check("定位到方向字段", "orientation" in fields)
    check("定位到纬度字段", "gps_lat" in fields)

    # 错误写回不能破坏原文件
    safe = client.get(f"/api/images/{ids['plain.jpg']}/metadata").get_json()["metadata"]
    check("失败后原数据不变", safe["orientation"] == 3 and safe["taken_at_raw"] == "2024:02:29 10:11:12")

    print("== BMP 单图编辑返回明确错误 ==")
    resp = client.put(f"/api/images/{ids['plain.bmp']}/metadata", json={"orientation": 1})
    check("BMP 422", resp.status_code == 422)
    check("BMP 错误文案", "不支持" in resp.get_json()["error"])

    print("== 批量：3 张好图 + 1 张 BMP，BMP 单独失败 ==")
    resp = client.post("/api/images/metadata/batch", json={
        "image_ids": [ids["with.jpg"], ids["plain.jpg"], ids["plain.png"], ids["plain.bmp"]],
        "orientation": 6,
        "taken_at": "2022-10-01 08:30:00",
        "description": "批量写入的说明",
        "gps_lat": 30.0, "gps_lon": 120.0,
    })
    check("批量 200（不因 BMP 整批失败）", resp.status_code == 200)
    b = resp.get_json()
    check("3 成功", b["succeeded_count"] == 3, b)
    check("1 失败", b["failed_count"] == 1, b)
    failed = b["failed"][0]
    check("失败项指向 BMP", failed["image_id"] == ids["plain.bmp"], failed)
    check("失败原因明确", "不支持" in failed["error"])
    succ_ids = {s["image_id"] for s in b["succeeded"]}
    check("PNG 在成功集合", ids["plain.png"] in succ_ids)

    png_meta = client.get(f"/api/images/{ids['plain.png']}/metadata").get_json()["metadata"]
    check("PNG 批量字段写入", png_meta["orientation"] == 6
          and png_meta["description"] == "批量写入的说明"
          and abs(png_meta["gps"]["longitude"] - 120.0) < 2e-4)
    with_meta = client.get(f"/api/images/{ids['with.jpg']}/metadata").get_json()["metadata"]
    check("带 EXIF 图型号保留", with_meta["model"] == "Cam A1")
    check("带 EXIF 图时间被更新", with_meta["taken_at_raw"] == "2022:10:01 08:30:00")

    print("== 批量 items 模式：逐张不同参数，部分字段非法只坏一张 ==")
    resp = client.post("/api/images/metadata/batch", json={"items": [
        {"image_id": ids["plain.png"], "orientation": 2},
        {"image_id": ids["plain.jpg"], "orientation": 0},
        {"image_id": None},
    ]})
    b = resp.get_json()
    check("仅 1 张成功", b["succeeded_count"] == 1, b)
    check("2 张失败且互不影响", b["failed_count"] == 2, b)
    bad = next(f for f in b["failed"] if f["image_id"] == ids["plain.jpg"])
    check("坏图字段单独标出", "orientation" in bad["fields"], bad)

    print("== 写回后图像记录尺寸/大小刷新且旧引用不断 ==")
    rec = client.get(f"/api/images/{ids['plain.png']}").get_json()
    check("图像 id 保持稳定", rec["id"] == ids["plain.png"])
    check("尺寸仍可读", rec["width"] == 200 and rec["height"] == 150)

    print("== 静态页面可访问 ==")
    check("首页 200", client.get("/").status_code == 200)
    check("metadata.js 已挂载", b"metadata/batch" in client.get("/js/views/metadata.js").data)

    print("\n元信息 HTTP 测试全部通过 ✔ （%d 项断言）" % _passed)


if __name__ == "__main__":
    main()
