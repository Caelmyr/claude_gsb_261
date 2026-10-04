"""照片隐藏信息（EXIF / PNG 文本块）的读取与写回。

设计要点：

- 读取：Pillow 解析 EXIF（0 号 IFD、Exif 子 IFD、GPS 子 IFD），整理成前端可直接
  展示的结构（拍摄时间、设备、分辨率、方向、GPS、拍摄参数……），并保留完整原始
  标签表；中文说明存放在 UserComment（UNICODE 前缀，ASCII 的 ImageDescription
  无法承载中文，会被写成 ``?``）。
- 写回：JPEG / PNG / WebP 采用「块级无损手术」——只解析容器结构、替换元数据
  数据块，像素码流原样保留，不存在任何重编码导致的画质损失；TIFF 走 Pillow
  重存（其压缩本身无损）；BMP / GIF 不支持 EXIF，明确报错而不是静默丢弃。
- 校验按字段分别收集错误，批量场景下一张图的非法字段不影响其它图。
"""
import io
import struct
import zlib

from PIL import Image, ExifTags
from PIL.ExifTags import Base as EB, GPS as GP

# ---------------------------------------------------------------------------
# 标签与常量
# ---------------------------------------------------------------------------
ORIENTATION_LABELS = {
    1: "正常（无旋转）",
    2: "水平翻转",
    3: "旋转 180°",
    4: "垂直翻转",
    5: "顺时针 90° + 水平翻转",
    6: "顺时针 90°",
    7: "逆时针 90° + 水平翻转",
    8: "逆时针 90°",
}

WRITABLE_FORMATS = {"JPEG", "PNG", "WEBP", "TIFF"}

_T_DATETIME = 0x0132          # DateTime
_T_DESCRIPTION = 0x010E       # ImageDescription（ASCII，不支持中文）
_T_ORIENTATION = 0x0112
_T_GPS_IFD = 0x8825
_T_USER_COMMENT = 0x9286
_T_DATETIME_ORIGINAL = 0x9003
_T_DATETIME_DIGITIZED = 0x9004

_MISSING_LABELS = {
    "taken_at": "拍摄时间",
    "make": "设备厂商",
    "model": "设备型号",
    "lens": "镜头型号",
    "orientation": "方向",
    "gps": "GPS 定位",
    "description": "说明",
}

_DESCRIPTION_TEXT_KEYS = ("Description", "ImageDescription", "Comment")


class MetadataError(Exception):
    """元数据读写错误。fields 为 {字段: 错误信息}，用于单字段高亮。"""

    def __init__(self, message, fields=None):
        super().__init__(message)
        self.fields = fields or {}


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------
def read_metadata(path):
    """读取一张图片的全部可展示元数据。图片本身损坏时抛 MetadataError。"""
    try:
        im = Image.open(path)
    except Exception as exc:  # noqa: BLE001
        raise MetadataError(f"无法打开图像：{exc}")

    with im:
        fmt = (im.format or "").upper()
        width, height = im.size
        try:
            exif = im.getexif()
            exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
            gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo)
        except Exception as exc:  # noqa: BLE001  EXIF 块损坏时图片仍可看
            raise MetadataError(f"图片可以打开，但其 EXIF 数据已损坏：{exc}")
        png_text = dict(getattr(im, "text", {}) or {}) if fmt == "PNG" else {}

        has_exif = len(exif) > 0 or bool(png_text)

        taken_at_raw, taken_at, taken_at_input = _read_datetime(exif_ifd, exif)
        gps = _read_gps(gps_ifd)
        description = _read_description(exif_ifd, exif, png_text)

        info = {
            "format": im.format,
            "mime": _mime_for(fmt),
            "width": width,
            "height": height,
            "writable": fmt in WRITABLE_FORMATS,
            "has_exif": has_exif,
            "taken_at": taken_at,
            "taken_at_raw": taken_at_raw,
            "taken_at_input": taken_at_input,
            "make": exif.get(EB.Make),
            "model": exif.get(EB.Model),
            "lens": exif_ifd.get(EB.LensModel) or exif_ifd.get(0xA434),
            "software": exif.get(EB.Software),
            "artist": exif.get(EB.Artist),
            "copyright": exif.get(EB.Copyright),
            "orientation": exif.get(EB.Orientation),
            "orientation_label": ORIENTATION_LABELS.get(exif.get(EB.Orientation)),
            "pixel_width": exif_ifd.get(EB.ExifImageWidth),
            "pixel_height": exif_ifd.get(EB.ExifImageHeight),
            "description": description,
            "gps": gps,
            "exposure": _read_exposure(exif_ifd),
            "raw_tags": _read_raw_tags(exif, exif_ifd, gps_ifd),
        }

    if not info["writable"]:
        info["unsupported_reason"] = (
            f"{fmt or '该'} 格式不支持写入 EXIF 信息，仅支持 JPEG / PNG / WebP / TIFF"
        )

    missing = [key for key in _MISSING_LABELS if _is_missing(info, key)]
    info["missing_fields"] = missing
    info["missing_labels"] = [_MISSING_LABELS[k] for k in missing]
    return info


def _is_missing(info, key):
    if key == "gps":
        return info["gps"] is None
    return not info.get(key)


def _read_datetime(exif_ifd, exif):
    """返回 (EXIF 原始字符串, ISO 字符串, datetime-local 输入值)。"""
    raw = exif_ifd.get(_T_DATETIME_ORIGINAL)
    if not raw:
        raw = exif_ifd.get(_T_DATETIME_DIGITIZED)
    if not raw:
        raw = exif.get(_T_DATETIME)
    if not raw:
        return None, None, None
    dt = _parse_exif_datetime(str(raw))
    if dt is None:
        return str(raw), None, None
    return (str(raw), dt.astimezone().isoformat(timespec="seconds"),
            dt.strftime("%Y-%m-%dT%H:%M:%S"))


def _read_gps(gps_ifd):
    if not gps_ifd:
        return None
    lat = _gps_to_decimal(gps_ifd.get(GP.GPSLatitude), gps_ifd.get(GP.GPSLatitudeRef))
    lon = _gps_to_decimal(gps_ifd.get(GP.GPSLongitude), gps_ifd.get(GP.GPSLongitudeRef))
    if lat is None or lon is None:
        return None
    alt = gps_ifd.get(GP.GPSAltitude)
    alt_above = gps_ifd.get(GP.GPSAltitudeRef, 0)
    altitude = None
    if alt is not None:
        try:
            altitude = round(float(alt) * (-1 if int(alt_above) == 1 else 1), 2)
        except (TypeError, ValueError):
            altitude = None
    return {
        "latitude": round(lat, 7),
        "longitude": round(lon, 7),
        "altitude": altitude,
        "lat_ref": gps_ifd.get(GP.GPSLatitudeRef),
        "lon_ref": gps_ifd.get(GP.GPSLongitudeRef),
        "dms": f"{_dms_text(gps_ifd.get(GP.GPSLatitude), gps_ifd.get(GP.GPSLatitudeRef), 'NS')}　"
               f"{_dms_text(gps_ifd.get(GP.GPSLongitude), gps_ifd.get(GP.GPSLongitudeRef), 'EW')}",
    }


def _read_description(exif_ifd, exif, png_text):
    raw = exif_ifd.get(_T_USER_COMMENT)
    if isinstance(raw, (bytes, bytearray)):
        text = _decode_user_comment(bytes(raw))
        if text:
            return text
    elif raw:
        return str(raw)
    if exif.get(_T_DESCRIPTION):
        return str(exif.get(_T_DESCRIPTION))
    for key in _DESCRIPTION_TEXT_KEYS:
        if png_text.get(key):
            return png_text[key]
    return None


def _decode_user_comment(raw):
    """按 UserComment 的 8 字节编码前缀解码。"""
    if len(raw) < 8:
        return None
    prefix, body = raw[:8], raw[8:]
    try:
        if prefix == b"UNICODE\x00":
            return body.decode("utf-16-be").rstrip("\x00").strip() or None
        if prefix == b"ASCII\x00\x00\x00":
            return body.decode("ascii", "replace").rstrip("\x00").strip() or None
        if prefix == b"\x00" * 8 or prefix == b"JIS\x00\x00\x00\x00\x00":
            return body.decode("utf-8", "replace").rstrip("\x00").strip() or None
    except UnicodeDecodeError:
        return None
    return None


def _read_exposure(e):
    def num(tag):
        v = e.get(tag)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    out = {
        "f_number": num(EB.FNumber),
        "exposure_time": num(EB.ExposureTime),
        "iso": e.get(EB.ISOSpeedRatings) or e.get(0x8827),
        "focal_length": num(EB.FocalLength),
        "focal_length_35mm": e.get(EB.FocalLengthIn35mmFilm),
        "exposure_program": e.get(EB.ExposureProgram),
        "white_balance": {0: "自动白平衡", 1: "手动白平衡"}.get(e.get(EB.WhiteBalance)),
        "flash": e.get(EB.Flash),
    }
    if out["exposure_time"]:
        t = out["exposure_time"]
        out["exposure_time_text"] = f"1/{round(1 / t)} 秒" if t < 0.5 else f"{t:g} 秒"
    if out["f_number"]:
        out["f_number_text"] = f"f/{out['f_number']:g}"
    if out["focal_length"]:
        out["focal_length_text"] = f"{out['focal_length']:g} mm"
    return {k: v for k, v in out.items() if v not in (None, "")}


def _read_raw_tags(exif, exif_ifd, gps_ifd):
    """完整原始标签表（供「查看全部隐藏信息」折叠面板）。"""
    rows = []

    def add(section, tag_id, value, name_map):
        if tag_id in (_T_USER_COMMENT, 0x927C):  # 说明与 MakerNote 已单独处理/为二进制大块
            if tag_id == 0x927C:
                rows.append((section, name_map.get(tag_id, f"0x{tag_id:04X}"),
                             f"<二进制 {len(value)} 字节>"))
            return
        name = name_map.get(tag_id, f"0x{tag_id:04X}")
        rows.append((section, name, _fmt_tag_value(value)))

    for k, v in exif.items():
        add("基础", k, v, ExifTags.TAGS)
    for k, v in exif_ifd.items():
        add("拍摄参数", k, v, ExifTags.TAGS)
    for k, v in gps_ifd.items():
        add("GPS", k, v, ExifTags.GPSTAGS)
    return [{"section": s, "name": n, "value": v} for s, n, v in rows]


def _fmt_tag_value(v):
    if isinstance(v, bytes):
        return f"<二进制 {len(v)} 字节>"
    if isinstance(v, tuple) and v:
        parts = [_fmt_tag_value(x) for x in v[:6]]
        text = ", ".join(parts) + (" …" if len(v) > 6 else "")
        return f"[{text}]"
    text = str(v)
    return text if len(text) <= 100 else text[:100] + "…"


# ---------------------------------------------------------------------------
# 字段校验 / 更新
# ---------------------------------------------------------------------------
def apply_metadata(path, updates):
    """按 updates 修改图片元数据，返回新的文件字节。

    updates 可含：taken_at / orientation / description / gps_lat / gps_lon，
    值为 None 表示清除该字段。字段级校验错误抛 MetadataError(fields=...)。
    """
    field_errors = {}
    dt_value = _validate_datetime(updates.get("taken_at"), field_errors)
    orientation = _validate_orientation(updates.get("orientation"), field_errors)
    description = _validate_description(updates.get("description"), field_errors)
    lat, lon = _validate_gps(updates, field_errors)
    if field_errors:
        raise MetadataError("部分字段不合法", fields=field_errors)

    try:
        im = Image.open(path)
    except Exception as exc:  # noqa: BLE001
        raise MetadataError(f"无法打开图像：{exc}")

    with im:
        fmt = (im.format or "").upper()
        if fmt not in WRITABLE_FORMATS:
            raise MetadataError(
                f"{fmt} 格式不支持写入元数据，请转换为 JPEG / PNG / WebP / TIFF 后再试"
            )
        dims = im.size
        try:
            exif = im.getexif()
        except Exception:  # noqa: BLE001  旧 EXIF 损坏时从空开始重建
            exif = Image.Exif()

        if "taken_at" in updates:
            _set_datetime(exif, dt_value)
        if "orientation" in updates:
            _set_orientation(exif, orientation)
        if "description" in updates:
            _set_description(exif, description)
        if "gps_lat" in updates or "gps_lon" in updates:
            _set_gps(exif, lat, lon)

        payload = exif.tobytes()
        tiff = payload[6:] if payload.startswith(b"Exif\x00\x00") else payload
        if tiff in (b"", b"MM\x00*\x00\x00\x00\x08\x00\x00"):
            # Pillow 空 Exif 的固定壳，视为「无 EXIF」
            tiff = b""

    with open(path, "rb") as f:
        data = f.read()

    if fmt == "JPEG":
        return _jpeg_write_exif(data, tiff)
    if fmt == "PNG":
        return _png_write_exif(data, tiff, description if "description" in updates else None)
    if fmt == "WEBP":
        return _webp_write_exif(data, tiff, dims)
    # TIFF：Pillow 重存（TIFF 压缩无损）
    with Image.open(io.BytesIO(data)) as im2:
        out = io.BytesIO()
        im2.save(out, "TIFF", exif=(b"Exif\x00\x00" + tiff if tiff else b""))
        return out.getvalue()


def _validate_datetime(value, errors):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        errors["taken_at"] = "时间格式无法识别"
        return None
    dt = _parse_exif_datetime(value)
    if dt is None:
        errors["taken_at"] = "时间格式应为 YYYY-MM-DD HH:MM:SS"
        return None
    return dt


def _validate_orientation(value, errors):
    if value is None or value == "":
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        errors["orientation"] = "方向必须是 1–8 的整数"
        return None
    if not 1 <= v <= 8:
        errors["orientation"] = "方向必须是 1–8 的整数"
        return None
    return v


def _validate_description(value, errors):
    if value is None:
        return None
    if not isinstance(value, str):
        errors["description"] = "说明必须是文本"
        return None
    if len(value) > 2000:
        errors["description"] = "说明最长 2000 字"
    return value


def _validate_gps(updates, errors):
    """两张坐标必须同时出现且同时合法；任一为空即清除。"""
    lat_raw = updates.get("gps_lat")
    lon_raw = updates.get("gps_lon")
    if lat_raw is None and lon_raw is None:
        return None, None
    if str(lat_raw).strip() == "" and str(lon_raw).strip() == "":
        return None, None

    def parse(v, key, lo, hi, name):
        try:
            x = float(v)
        except (TypeError, ValueError):
            errors[key] = f"{name}必须是数字"
            return None
        if not lo <= x <= hi:
            errors[key] = f"{name}超出范围（{lo} ~ {hi}）"
            return None
        return x

    return parse(lat_raw, "gps_lat", -90, 90, "纬度"), parse(lon_raw, "gps_lon", -180, 180, "经度")


def _set_datetime(exif, dt):
    exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
    if dt is None:
        exif.pop(_T_DATETIME, None)
        exif_ifd.pop(_T_DATETIME_ORIGINAL, None)
        exif_ifd.pop(_T_DATETIME_DIGITIZED, None)
        return
    stamp = dt.strftime("%Y:%m:%d %H:%M:%S")
    exif[_T_DATETIME] = stamp
    exif_ifd[_T_DATETIME_ORIGINAL] = stamp
    exif_ifd[_T_DATETIME_DIGITIZED] = stamp


def _set_orientation(exif, orientation):
    if orientation is None:
        exif.pop(_T_ORIENTATION, None)
    else:
        exif[_T_ORIENTATION] = orientation


def _set_description(exif, description):
    exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
    exif.pop(_T_DESCRIPTION, None)
    exif_ifd.pop(_T_USER_COMMENT, None)
    if not description:
        return
    # ASCII 标签只接受 ASCII；非 ASCII 中文走 UserComment(UNICODE)
    if all(ord(c) < 128 for c in description):
        exif[_T_DESCRIPTION] = description
    exif_ifd[_T_USER_COMMENT] = b"UNICODE\x00" + description.encode("utf-16-be")


def _set_gps(exif, lat, lon):
    if lat is None or lon is None:
        exif.pop(_T_GPS_IFD, None)
        return
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    gps.clear()
    gps[GP.GPSLatitudeRef] = "N" if lat >= 0 else "S"
    gps[GP.GPSLatitude] = _decimal_to_dms(abs(lat))
    gps[GP.GPSLongitudeRef] = "E" if lon >= 0 else "W"
    gps[GP.GPSLongitude] = _decimal_to_dms(abs(lon))


# ---------------------------------------------------------------------------
# 容器格式无损手术
# ---------------------------------------------------------------------------
def _jpeg_write_exif(data, tiff):
    """替换 JPEG 中的 APP1/Exif 段，像素扫描数据原样保留。"""
    if data[:2] != b"\xff\xd8":
        raise MetadataError("不是有效的 JPEG 文件")
    kept = bytearray()
    p, n = 2, len(data)
    sos_start = None
    while p < n:
        if data[p] != 0xFF:
            raise MetadataError("JPEG 段结构解析失败")
        while p < n and data[p] == 0xFF:
            p += 1
        marker = data[p]
        p += 1
        if marker == 0xDA:  # SOS：之后全部是扫描数据
            sos_start = p - 2
            break
        if marker == 0xD9:  # EOI
            sos_start = p - 2
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue  # 无长度段
        if p + 2 > n:
            raise MetadataError("JPEG 段结构解析失败")
        seg_len = struct.unpack(">H", data[p:p + 2])[0]
        seg = data[p - 2:p + seg_len]
        # 丢弃旧的 Exif APP1（XMP 等其它 APP1 保留）
        if not (marker == 0xE1 and data[p + 2:p + 8] == b"Exif\x00\x00"):
            kept += seg
        p += seg_len
    if sos_start is None:
        raise MetadataError("JPEG 缺少扫描数据")

    out = bytearray(b"\xff\xd8")
    if tiff:
        app1 = b"\xff\xe1" + struct.pack(">H", len(tiff) + 8) + b"Exif\x00\x00" + tiff
        if len(app1) > 65535:
            raise MetadataError("元数据过大，无法写入 JPEG")
        out += app1
    out += kept
    out += data[sos_start:]
    return bytes(out)


def _iter_png_chunks(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise MetadataError("不是有效的 PNG 文件")
    p, n = 8, len(data)
    while p + 12 <= n:
        length = struct.unpack(">I", data[p:p + 4])[0]
        ctype = data[p + 4:p + 8]
        body = data[p + 8:p + 8 + length]
        yield ctype, body
        p += 12 + length
        if ctype == b"IEND":
            break


def _png_chunk(ctype, body):
    return (struct.pack(">I", len(body)) + ctype + body
            + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF))


def _png_itxt_description(text):
    keyword = b"Description\x00"
    return keyword + b"\x00\x00\x00\x00" + text.encode("utf-8")


def _png_write_exif(data, tiff, description):
    """替换 PNG 的 eXIf 块，并同步 Description 文本块（原生阅读器友好）。"""
    out = bytearray(data[:8])
    inserted = False
    for ctype, body in _iter_png_chunks(data):
        if ctype == b"eXIf":
            continue
        if ctype in (b"tEXt", b"zTXt", b"iTXt"):
            kw = body.split(b"\x00", 1)[0]
            if kw in (b"Description", b"ImageDescription", b"Comment"):
                continue
        out += _png_chunk(ctype, body)
        if ctype == b"IHDR" and not inserted:
            if tiff:
                out += _png_chunk(b"eXIf", tiff)
            if description:
                out += _png_chunk(b"iTXt", _png_itxt_description(description))
            inserted = True
    if not inserted:
        raise MetadataError("PNG 缺少 IHDR 块")
    return bytes(out)


def _webp_chunk(fourcc, body):
    pad = b"\x00" if len(body) & 1 else b""
    return fourcc + struct.pack("<I", len(body)) + body + pad


def _webp_write_exif(data, tiff, dims):
    """替换 WebP RIFF 容器中的 EXIF 块（有 VP8X 时直接改标志位，
    没有则包一层最小 VP8X；VP8/VP8L 码流原样保留）。"""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise MetadataError("不是有效的 WebP 文件")

    chunks = []
    p, n = 12, len(data)
    while p + 8 <= n:
        fourcc = data[p:p + 4]
        size = struct.unpack("<I", data[p + 4:p + 8])[0]
        body = data[p + 8:p + 8 + size]
        chunks.append((fourcc, bytearray(body)))
        p += 8 + size + (size & 1)

    has_vp8x = any(c == b"VP8X" for c, _ in chunks)
    ordered = []
    for fourcc, body in chunks:
        if fourcc == b"EXIF":
            continue
        if fourcc == b"VP8X":
            if tiff:
                body[0] |= 0x08       # EXIF 标志位
            else:
                body[0] &= ~0x08
        ordered.append((fourcc, bytes(body)))

    if not has_vp8x and tiff:
        w, h = dims
        payload = bytes([0x08, 0, 0, 0])
        payload += struct.pack("<I", max(w - 1, 0))[:3]
        payload += struct.pack("<I", max(h - 1, 0))[:3]
        ordered.insert(0, (b"VP8X", payload))

    if tiff:
        # EXIF 块放在码流块之后（符合 WebP 容器推荐顺序）
        idx = 0
        for i, (c, _) in enumerate(ordered):
            if c in (b"VP8 ", b"VP8L", b"ANMF"):
                idx = i + 1
        ordered.insert(idx if idx else len(ordered), (b"EXIF", tiff))

    payload = b"".join(_webp_chunk(c, b) for c, b in ordered)
    return b"RIFF" + struct.pack("<I", len(payload) + 4) + b"WEBP" + payload


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _parse_exif_datetime(text):
    """把 EXIF/ISO 时间字符串解析为带本地时区的 datetime；失败返回 None。"""
    from datetime import datetime, timezone
    s = text.strip().replace("T", " ")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.astimezone()
        except ValueError:
            continue
    try:  # ISO 8601（可能带时区）
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc).astimezone()
        return dt.astimezone()
    except ValueError:
        return None


def _gps_to_decimal(values, ref):
    if not values or len(values) != 3:
        return None
    try:
        d, m, s = (float(x) for x in values)
    except (TypeError, ValueError):
        return None
    v = d + m / 60.0 + s / 3600.0
    if ref in ("S", "W"):
        v = -v
    return v


def _decimal_to_dms(value):
    deg = int(value)
    minutes_full = (value - deg) * 60
    minutes = int(minutes_full)
    seconds = round((minutes_full - minutes) * 60, 3)
    if seconds >= 60:
        seconds -= 60
        minutes += 1
    if minutes >= 60:
        minutes -= 60
        deg += 1
    return (float(deg), float(minutes), float(seconds))


def _dms_text(values, ref, refs):
    if not values or len(values) != 3:
        return "-"
    try:
        d, m, s = (float(x) for x in values)
    except (TypeError, ValueError):
        return "-"
    return f"{int(d)}°{int(m)}′{s:.1f}″{ref or ''}"


def _mime_for(fmt):
    return {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp",
            "TIFF": "image/tiff", "GIF": "image/gif", "BMP": "image/bmp"}.get(fmt, "application/octet-stream")
